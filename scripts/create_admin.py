"""scripts/create_admin.py — creates the single exempt admin account.

Deliberately NOT an API route: exemption from payment can't be something
reachable from the public internet, or it would defeat the entire paywall.
This is a script you run yourself, once, from a machine or shell you control
(e.g. Render's own "Shell" tab on your web service).

Usage (from the mvp/ directory, or Render's Shell tab):
    python -m scripts.create_admin
"""
import sys, os, getpass
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db import SessionLocal, init_db
from app.models import OrgMember
from app.services import auth

ADMIN_NAME = "Emmanuel Egaga"
ADMIN_EMAIL = "egagaemmy@gmail.com"
ADMIN_COMPANY = "Kabod Global Resources"


def main() -> None:
    init_db()
    db = SessionLocal()
    if db.query(OrgMember).filter_by(email=ADMIN_EMAIL).first():
        print(f"An account with {ADMIN_EMAIL} already exists — nothing to do.")
        print("If you need to reset its password, use the normal 'Forgot password' flow instead.")
        return

    password = getpass.getpass(f"Choose a password for {ADMIN_EMAIL}: ")
    if len(password) < 8:
        print("Password must be at least 8 characters. Run this again.")
        return
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords didn't match. Run this again.")
        return

    member, token = auth.create_exempt_admin(db, ADMIN_COMPANY, ADMIN_NAME, ADMIN_EMAIL, password)
    print(f"\nAdmin account created: {ADMIN_NAME} <{ADMIN_EMAIL}>")
    print(f"Organization: {ADMIN_COMPANY} (billing_status=exempt — never gated, unlimited on every tier)")
    print("Log in normally on the website with this email and the password you just chose.")


if __name__ == "__main__":
    main()
