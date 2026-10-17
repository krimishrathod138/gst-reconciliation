"""
Account recovery for GST Reconciliation (run on the computer where the app is installed).

Shows the usernames and lets you set a new password for one of them - use it when
you have forgotten your username or password and no other admin can reset it.
The password is typed hidden and stored only as a hash; the account is also unlocked.

    .venv\\Scripts\\python.exe reset_password.py        (or double-click reset_password.bat)
"""
from __future__ import annotations

import getpass
import sys

from modules import ReconError
from modules.auth import AuthService
from modules.database import Database


def main() -> int:
    auth = AuthService(Database())
    users = auth.list_users()
    if users.empty:
        print("No accounts exist yet - start the app and create the administrator account.")
        return 0
    print("\nAccounts in the GST Reconciliation database:\n")
    for i, u in enumerate(users.itertuples(), start=1):
        status = "active" if u.active else "deactivated"
        print(f"  {i}. {u.username:<20} {u.full_name or '':<25} {u.role:<6} {status}{'  (locked)' if u.locked else ''}")
    choice = input("\nNumber of the account to reset (Enter to quit): ").strip()
    if not choice:
        return 0
    if not choice.isdigit() or not 1 <= int(choice) <= len(users):
        print("Invalid choice.")
        return 1
    username = users.iloc[int(choice) - 1]["username"]
    for _ in range(3):
        pw = getpass.getpass(f"New password for '{username}' (8+ characters, letters and digits): ")
        if pw != getpass.getpass("Type it again: "):
            print("The passwords do not match - try again.")
            continue
        try:
            auth.reset_password(username, pw)
            if not users.iloc[int(choice) - 1]["active"]:
                auth.set_active(username, True)
        except ReconError as exc:
            print(exc)
            continue
        print(f"\nDone. Sign in with username '{username}' and your new password.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
