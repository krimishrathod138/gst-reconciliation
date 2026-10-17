"""
User accounts and login.

* Passwords are stored only as salted PBKDF2-HMAC-SHA256 hashes (Python standard
  library, 200,000 iterations); plain passwords are never written anywhere.
* Password policy: at least 8 characters with letters and digits.
* After 5 wrong passwords an account is locked for 15 minutes.
* Roles: ``admin`` (manage users and settings) and ``user``.  The last active
  admin cannot be deactivated or demoted.
* There is no default password: on first start the app asks you to create the
  first admin account.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import func, select

from modules import ReconError
from modules.database import Database, User

ROLES = ("admin", "user")
ITERATIONS = 200_000
MAX_FAILED = 5
LOCK_MINUTES = 15
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,49}$")


@dataclass(frozen=True)
class AuthUser:
    """The logged-in user as kept in the UI session (no password material)."""

    username: str
    full_name: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def display(self) -> str:
        return self.full_name or self.username


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #
def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    """Return ``pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>``."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of *password* against a hash from :func:`hash_password`."""
    try:
        algo, iterations, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def check_password_policy(password: str) -> None:
    """Raise :class:`ReconError` if *password* is too weak."""
    if len(password) < 8:
        raise ReconError("Password must be at least 8 characters long.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ReconError("Password must contain both letters and digits.")


def normalise_username(username: str) -> str:
    name = (username or "").strip().lower()
    if not _USERNAME_RE.match(name):
        raise ReconError("Username must be 3-50 characters: letters, digits, '.', '_' or '-'.")
    return name


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class AuthService:
    """User management on top of :class:`modules.database.Database`."""

    def __init__(self, db: Database, iterations: int = ITERATIONS) -> None:
        self.db = db
        self.iterations = iterations

    # ---------------- queries
    def has_users(self) -> bool:
        with self.db.session() as s:
            return (s.scalar(select(func.count()).select_from(User)) or 0) > 0

    def list_users(self) -> pd.DataFrame:
        with self.db.session() as s:
            rows = s.scalars(select(User).order_by(User.username)).all()
        return pd.DataFrame([{"username": u.username, "full_name": u.full_name, "role": u.role,
                              "active": u.is_active, "last_login": u.last_login, "created_at": u.created_at,
                              "created_by": u.created_by,
                              "locked": bool(u.locked_until and u.locked_until > datetime.now())} for u in rows],
                            columns=["username", "full_name", "role", "active", "last_login", "created_at",
                                     "created_by", "locked"])

    def _active_admins(self, s) -> int:
        return s.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.is_active)) or 0

    # ---------------- commands
    def create_user(self, username: str, password: str, role: str = "user", full_name: str = "",
                    created_by: str = "") -> AuthUser:
        """Create an account.  The very first account is always an admin."""
        name = normalise_username(username)
        check_password_policy(password)
        if role not in ROLES:
            raise ReconError(f"Role must be one of: {', '.join(ROLES)}.")
        with self.db.session() as s, s.begin():
            if s.scalars(select(User).where(User.username == name)).first() is not None:
                raise ReconError(f"User '{name}' already exists.")
            if (s.scalar(select(func.count()).select_from(User)) or 0) == 0:
                role = "admin"
            s.add(User(username=name, full_name=full_name.strip(), role=role, created_by=created_by,
                       password_hash=hash_password(password, iterations=self.iterations)))
        return AuthUser(name, full_name.strip(), role)

    def authenticate(self, username: str, password: str) -> AuthUser:
        """Check credentials; raises :class:`ReconError` with a safe message on failure."""
        name = (username or "").strip().lower()
        generic = "Invalid username or password."
        error: str | None = None
        # Decide inside the transaction, raise only after it commits - otherwise the
        # failed-attempt counter would be rolled back and lockout could never happen.
        with self.db.session() as s, s.begin():
            user = s.scalars(select(User).where(User.username == name)).first()
            now = datetime.now()
            if user is None:
                verify_password(password, hash_password("dummy-password1", iterations=self.iterations))  # equal timing
                error = generic
            elif user.locked_until and user.locked_until > now:
                minutes = int((user.locked_until - now).total_seconds() // 60) + 1
                error = f"Account locked after too many failed attempts. Try again in {minutes} minute(s)."
            elif not verify_password(password, user.password_hash):
                user.failed_attempts = (user.failed_attempts or 0) + 1
                error = generic
                if user.failed_attempts >= MAX_FAILED:
                    user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                    user.failed_attempts = 0
                    error = f"Too many failed attempts - account locked for {LOCK_MINUTES} minutes."
            elif not user.is_active:
                error = "This account is deactivated. Please contact your administrator."
            else:
                user.failed_attempts, user.locked_until, user.last_login = 0, None, now
                result = AuthUser(user.username, user.full_name, user.role)
        if error:
            raise ReconError(error)
        return result

    def change_password(self, username: str, old_password: str, new_password: str) -> None:
        self.authenticate(username, old_password)
        self.reset_password(username, new_password)

    def reset_password(self, username: str, new_password: str) -> None:
        """Set a new password (admin action, or after verifying the old one); also unlocks the account."""
        check_password_policy(new_password)
        with self.db.session() as s, s.begin():
            user = self._get(s, username)
            user.password_hash = hash_password(new_password, iterations=self.iterations)
            user.failed_attempts, user.locked_until = 0, None

    def set_active(self, username: str, active: bool) -> None:
        with self.db.session() as s, s.begin():
            user = self._get(s, username)
            if not active and user.role == "admin" and user.is_active and self._active_admins(s) <= 1:
                raise ReconError("You cannot deactivate the last active admin.")
            user.is_active = active

    def set_role(self, username: str, role: str) -> None:
        if role not in ROLES:
            raise ReconError(f"Role must be one of: {', '.join(ROLES)}.")
        with self.db.session() as s, s.begin():
            user = self._get(s, username)
            if user.role == "admin" and role != "admin" and user.is_active and self._active_admins(s) <= 1:
                raise ReconError("You cannot remove the admin role from the last active admin.")
            user.role = role

    def update_name(self, username: str, full_name: str) -> None:
        with self.db.session() as s, s.begin():
            self._get(s, username).full_name = full_name.strip()

    @staticmethod
    def _get(s, username: str) -> User:
        user = s.scalars(select(User).where(User.username == (username or "").strip().lower())).first()
        if user is None:
            raise ReconError(f"User '{username}' not found.")
        return user
