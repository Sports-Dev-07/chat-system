#!/usr/bin/env python3
"""Show who can sign in. A read-only check — changes nothing.

    python list_users.py
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

import database as dbx
import passwords as pw

dbx.init_db()
pw.init_password_tables()

print(f"database: {dbx.DB_PATH}")
print(f"exists:   {dbx.DB_PATH.exists()}"
      + (f"  ({dbx.DB_PATH.stat().st_size // 1024} KB)" if dbx.DB_PATH.exists() else ""))
print()

with dbx.db() as conn:
    rows = conn.execute(
        """SELECT email, name, is_admin, must_change_password,
                  password_hash != '' AS can_log_in
           FROM users WHERE is_bot = 0 ORDER BY LOWER(email)""").fetchall()

if not rows:
    print("No people in the database yet.")
    print("Create the first account with:")
    print("    python create_user.py you@sportstech.de --name YourName")
    raise SystemExit(0)

print(f"{'email':<34} {'name':<16} {'can sign in':<12} {'admin':<6} must change")
print("-" * 86)
for r in rows:
    print(f"{r['email']:<34} {(r['name'] or ''):<16} "
          f"{('YES' if r['can_log_in'] else 'no — needs a password'):<12} "
          f"{('yes' if r['is_admin'] else ''):<6} "
          f"{'yes' if r['must_change_password'] else ''}")
print()
usable = sum(1 for r in rows if r["can_log_in"])
print(f"{usable} of {len(rows)} can sign in. Admins: {pw.admin_count()}.")
if not usable:
    print("Nobody has a password, which is why the login screen rejects everything.")