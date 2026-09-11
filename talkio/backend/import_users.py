#!/usr/bin/env python3
"""Create accounts from a spreadsheet, once.

    python import_users.py users.xlsx
    python import_users.py users.xlsx --dry-run
    python import_users.py users.xlsx --admin boss@sportstech.de

The sheet needs an `email` column. `name` and `admin` are optional. Anything else
is ignored, so an existing staff list usually works as-is.

    email                      | name    | admin
    ---------------------------|---------|------
    bala@sportstech.de         | Bala    |
    satha@sportstech.de        | Satha   | yes

Why it does not read passwords from the sheet
---------------------------------------------
A spreadsheet of passwords is the weakest part of any system built around one: it
gets emailed, synced, and copied to laptops, and nothing records who opened it.
This generates a strong password per person instead, stores only a hash, and prints
the passwords once so you can hand them over. Then delete the printout.

Everyone is created with must_change_password, so the password you hand out stops
working the moment they sign in — nothing you or the sheet ever saw stays valid.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import database as dbx
import passwords as pw


def read_rows(path: Path) -> list[dict]:
    """Read .xlsx or .csv. The header row names the columns; case and spacing in
    the headers are ignored, because real sheets have 'E-Mail' and 'Full Name'."""
    if path.suffix.lower() in (".csv", ".txt"):
        import csv
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return [_normalise(r) for r in csv.DictReader(fh)]

    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("Reading .xlsx needs openpyxl:  pip install openpyxl\n"
                 "Or save the sheet as CSV and pass that instead.")

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        sys.exit("That sheet is empty.")
    header = [str(h or "").strip().lower() for h in rows[0]]
    out = []
    for raw in rows[1:]:
        if not any(raw):
            continue
        out.append(_normalise(dict(zip(header, raw))))
    return out


def _normalise(row: dict) -> dict:
    """Map the header names people actually use onto email/name/admin."""
    lower = {str(k or "").strip().lower(): v for k, v in row.items()}
    def pick(*names):
        for n in names:
            if lower.get(n) not in (None, ""):
                return str(lower[n]).strip()
        return ""
    return {
        "email": pick("email", "e-mail", "mail", "email address", "login"),
        "name": pick("name", "full name", "fullname", "display name", "person"),
        "admin": pick("admin", "is admin", "administrator", "role").lower(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sheet", help="users.xlsx or users.csv")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would happen and change nothing")
    ap.add_argument("--admin", action="append", default=[],
                    help="also make this email an admin (repeatable)")
    ap.add_argument("--reset-existing", action="store_true",
                    help="also issue a new password to people who already have one")
    args = ap.parse_args()

    path = Path(args.sheet)
    if not path.exists():
        return print(f"No such file: {path}") or 1

    rows = read_rows(path)
    if not rows:
        return print("No rows found. Is there a header row with an 'email' column?") or 1

    pw.init_password_tables()
    admins = {pw.normalise_email(a) for a in args.admin}

    # Create the tables first. On a fresh install the very first thing this script
    # did was query `users`, so it crashed before importing anything — including
    # in --dry-run, where nothing should be needed at all.
    dbx.init_db()
    pw.init_password_tables()

    seen, results, skipped = set(), [], []
    for row in rows:
        email = pw.normalise_email(row["email"])
        if not email or "@" not in email:
            skipped.append((row["email"] or "(blank)", "not an email address"))
            continue
        if email in seen:
            skipped.append((email, "listed twice"))
            continue
        seen.add(email)

        existing = pw.find_by_email(email)
        if existing and existing.get("password_hash") and not args.reset_existing:
            skipped.append((email, "already has a password — use --reset-existing to replace it"))
            continue

        make_admin = email in admins or row["admin"] in ("yes", "y", "true", "1", "admin")
        password = pw.generate_password()

        if args.dry_run:
            results.append((email, row["name"] or email.split("@")[0],
                            "(dry run)", make_admin, bool(existing)))
            continue

        user = existing or dbx.upsert_user(email, row["name"] or email.split("@")[0])
        pw.set_password(user["id"], password, must_change=True)
        if make_admin:
            pw.set_admin(user["id"], True)
        results.append((email, user["name"], password, make_admin, bool(existing)))

    # ---- output ----
    if args.dry_run:
        print(f"\nDRY RUN — nothing was written. {len(results)} account(s) would be set up.\n")
    else:
        print(f"\n{len(results)} account(s) ready. Passwords are shown ONCE — copy them now.\n")

    if results:
        w = max(len(r[0]) for r in results) + 2
        print(f"{'email'.ljust(w)}{'name'.ljust(18)}{'password'.ljust(24)}notes")
        print("-" * (w + 18 + 24 + 20))
        for email, name, password, admin, existed in results:
            notes = " ".join(filter(None, [
                "ADMIN" if admin else "",
                "existing account" if existed else "new",
            ]))
            print(f"{email.ljust(w)}{name[:16].ljust(18)}{password.ljust(24)}{notes}")

    if skipped:
        print(f"\nSkipped {len(skipped)}:")
        for email, why in skipped:
            print(f"  {email} — {why}")

    if results and not args.dry_run:
        print("\nEveryone must change this password at first sign-in, so it stops working "
              "as soon as they use it.")
        print("Delete this output once the passwords have been handed over — the database "
              "keeps only a hash and they cannot be recovered.")
        if pw.admin_count() == 0:
            print("\nNOTE: no admin was set. Re-run with --admin your@email to grant it, "
                  "or the account-management endpoints stay open to the first caller.")
    return 0


if __name__ == "__main__":
    sys.exit(main())