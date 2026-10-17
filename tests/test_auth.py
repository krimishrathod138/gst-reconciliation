"""Tests for login and user management (modules.auth)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from modules import ReconError
from modules.auth import AuthService, check_password_policy, hash_password, verify_password
from modules.database import Database, User


@pytest.fixture
def auth(tmp_path):
    return AuthService(Database(f"sqlite:///{(tmp_path / 'a.db').as_posix()}"), iterations=1_000)


def test_hashing():
    h = hash_password("Secret123", iterations=1_000)
    assert h.startswith("pbkdf2_sha256$1000$") and "Secret123" not in h
    assert verify_password("Secret123", h) and not verify_password("secret123", h)
    assert hash_password("Secret123", iterations=1_000) != h          # random salt
    assert not verify_password("x", "garbage")


@pytest.mark.parametrize("pw", ["short1", "lettersonly", "12345678", ""])
def test_password_policy(pw):
    with pytest.raises(ReconError):
        check_password_policy(pw)


def test_first_user_is_admin_and_login(auth):
    assert not auth.has_users()
    first = auth.create_user("Asha", "Secret123", role="user", full_name="Asha Rao")
    assert first.role == "admin" and first.username == "asha"          # first account is forced to admin
    assert auth.has_users()
    u = auth.authenticate(" ASHA ", "Secret123")
    assert u.is_admin and u.display == "Asha Rao"
    assert auth.list_users().loc[0, "last_login"] is not None


def test_admin_adds_user_and_duplicates_rejected(auth):
    auth.create_user("admin", "Admin1234")
    ravi = auth.create_user("ravi", "Ravi12345", role="user", created_by="admin")
    assert ravi.role == "user" and not ravi.is_admin
    with pytest.raises(ReconError, match="already exists"):
        auth.create_user("Ravi", "Other1234")
    with pytest.raises(ReconError, match="Username"):
        auth.create_user("r!", "Other1234")
    users = auth.list_users().set_index("username")
    assert users.loc["ravi", "created_by"] == "admin"


def test_wrong_password_and_unknown_user_same_message(auth):
    auth.create_user("admin", "Admin1234")
    for name, pw in (("admin", "wrong1234"), ("nobody", "Admin1234")):
        with pytest.raises(ReconError, match="Invalid username or password"):
            auth.authenticate(name, pw)


def test_lockout_after_five_failures(auth):
    auth.create_user("admin", "Admin1234")
    for _ in range(4):
        with pytest.raises(ReconError, match="Invalid"):
            auth.authenticate("admin", "bad")
    with pytest.raises(ReconError, match="locked"):
        auth.authenticate("admin", "bad")
    with pytest.raises(ReconError, match="locked"):
        auth.authenticate("admin", "Admin1234")                          # even the right password
    with auth.db.session() as s, s.begin():                              # lock expires
        s.scalars(select(User)).first().locked_until = datetime.now() - timedelta(seconds=1)
    assert auth.authenticate("admin", "Admin1234").username == "admin"


def test_deactivate_and_roles(auth):
    auth.create_user("admin", "Admin1234")
    auth.create_user("ravi", "Ravi12345")
    auth.set_active("ravi", False)
    with pytest.raises(ReconError, match="deactivated"):
        auth.authenticate("ravi", "Ravi12345")
    auth.set_active("ravi", True)
    assert auth.authenticate("ravi", "Ravi12345")
    with pytest.raises(ReconError, match="last active admin"):
        auth.set_active("admin", False)
    with pytest.raises(ReconError, match="last active admin"):
        auth.set_role("admin", "user")
    auth.set_role("ravi", "admin")
    auth.set_role("admin", "user")                                       # allowed now: ravi is an admin
    assert auth.authenticate("admin", "Admin1234").role == "user"


def test_password_change_and_reset(auth):
    auth.create_user("admin", "Admin1234")
    with pytest.raises(ReconError, match="Invalid"):
        auth.change_password("admin", "wrong", "NewPass123")
    auth.change_password("admin", "Admin1234", "NewPass123")
    assert auth.authenticate("admin", "NewPass123")
    auth.reset_password("admin", "Reset12345")
    with pytest.raises(ReconError):
        auth.authenticate("admin", "NewPass123")
    with pytest.raises(ReconError, match="at least 8"):
        auth.reset_password("admin", "x1")
