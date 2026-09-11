#!/usr/bin/env python3
"""Create an account you can sign in with.

    python create_user.py
    python create_user.py satha@sportstech.de --admin
    python create_user.py bala@sportstech.de --name Bala --password "my own password"

Run it once to make the first admin; after that you can add everyone else from
Settings -> People & access inside the app, or in bulk with import_users.py.

With no arguments it asks for the details. If you don't supply a password one is
generated and printed — that is the only time it is shown, because only a hash is
stored and it cannot be recovered.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import database as dbx        # noqa: E402
import passwords as pw        # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("email", nargs="?", help="the address they sign in with")
    ap.add_argument("--name", default="", help="display name")
    ap.add_argument("--password", default="",
                    help="set one yourself; omit to have one generated")
    ap.add_argument("--admin", action="store_true",
                    help="can manage other people (the first account gets this anyway)")
    ap.add_argument("--no-change", action="store_true",
                    help="don't force a password change at first sign-in")
    args = ap.parse_args()

    # Tables first: on a fresh install nothing exists yet.
    dbx.init_db()
    pw.init_password_tables()

    email = pw.normalise_email(args.email or input("Email: ").strip())
    if "@" not in email or len(email) < 5:
        print(f"'{email}' doesn't look like an email address.", file=sys.stderr)
        return 1

    name = args.name.strip() or email.split("@")[0]

    password = args.password
    if not password and sys.stdin.isatty() and not args.email:
        # Interactive run: offer to choose one, but generating is the better default.
        typed = getpass.getpass("Password (leave blank to generate one): ")
        password = typed.strip()

    generated = False
    if not password:
        password = pw.generate_password()
        generated = True
    else:
        problem = pw.password_problem(password, email)
        if problem:
            print(f"That password won't do: {problem}", file=sys.stderr)
            return 1

    existing = pw.find_by_email(email)
    if existing and existing.get("password_hash"):
        print(f"{email} already has a password. Use --password to replace it, or reset "
              "it from Settings -> People & access.", file=sys.stderr)
        return 1

    user = existing or dbx.upsert_user(email, name)
    if name and user.get("name") != name:
        with dbx.db() as conn:
            conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user["id"]))

    # The first account is always an admin — otherwise nobody could grant it and
    # the People panel would be unreachable forever.
    first = pw.admin_count() == 0
    pw.set_password(user["id"], password, must_change=not args.no_change)
    if args.admin or first:
        pw.set_admin(user["id"], True)

    print()
    print("=" * 58)
    print(f"  Email     {email}")
    print(f"  Password  {password}")
    print(f"  Admin     {'yes' if (args.admin or first) else 'no'}"
          + ("   (first account)" if first and not args.admin else ""))
    print("=" * 58)
    if generated:
        print("This password is shown once — only a hash is stored.")
    if not args.no_change:
        print("You'll be asked to choose a new password at first sign-in.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())