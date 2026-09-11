"""Passwords and login throttling.

Design, and why
---------------
* **No self-registration.** An account exists only because an admin created it, so
  "only people on the list can log in" is enforced by there being no other way in.
  There is no signup endpoint to lock down later.

* **Nobody can read a password, including you.** Only a scrypt hash is stored. A
  forgotten password is reset, never looked up. That means a copy of the database
  is not a copy of everyone's credentials.

* **The handed-out password dies on first use.** Accounts are created with
  `must_change_password`, so whatever you wrote on a sheet or in a message stops
  working the moment the person signs in. Nothing you ever saw stays valid.

* **Throttled.** Five wrong attempts locks that account for fifteen minutes. The
  backend is reachable from the internet, and without this a weak password falls
  to a script in minutes.

* **scrypt, from the standard library.** No new dependency to install or keep
  patched. Parameters below cost ~100ms per attempt, which is unnoticeable when
  logging in and ruinous when guessing.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import string

import database as dbx

# ~100ms and 16MB per hash on a normal machine. Raising N raises both.
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
DK_LEN = 32
SALT_LEN = 16

MAX_ATTEMPTS = 5
LOCKOUT_SECONDS = 15 * 60
MIN_PASSWORD_LEN = 10

SCHEMA = """
ALTER TABLE users ADD COLUMN password_hash TEXT DEFAULT '';
ALTER TABLE users ADD COLUMN must_change_password INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN failed_attempts INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN locked_until REAL DEFAULT 0;
ALTER TABLE users ADD COLUMN password_set_at REAL DEFAULT 0;
"""


# The break-glass administrator, configured in .env rather than hardcoded.
# A shipped default password is the most common way systems get taken over: it
# ends up on a public host with the vendor default still in place. So the email
# has a default and the PASSWORD does not — one is generated and printed once if
# you don't set it.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@sportstech.local").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()


def ensure_builtin_admin() -> dict:
    """Create or update the dedicated admin account at startup.

    Kept separate from ordinary staff accounts: it exists so administration is
    possible even if every other account is locked out.
    """
    generated = ""
    password = ADMIN_PASSWORD
    if not password:
        password = generate_password()
        generated = password

    existing = find_by_email(ADMIN_EMAIL)
    if existing:
        user_id = existing["id"]
        # Only rewrite the password when one was given explicitly, or when the
        # account has none — otherwise every restart would invalidate a password
        # the administrator had already changed.
        if ADMIN_PASSWORD or not existing.get("password_hash"):
            set_password(user_id, password, must_change=not ADMIN_PASSWORD)
        else:
            generated = ""
    else:
        user = dbx.upsert_user(ADMIN_EMAIL, "Administrator")
        user_id = user["id"]
        set_password(user_id, password, must_change=not ADMIN_PASSWORD)
    set_admin(user_id, True)
    return {"email": ADMIN_EMAIL, "user_id": user_id, "generated": generated}


def init_password_tables():
    """Additive columns, run one at a time so an existing one doesn't stop the rest."""
    import sqlite3
    with dbx.db() as conn:
        for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # already there


# ------------------------------------------------------------------ hashing

def hash_password(password: str) -> str:
    """scrypt$N$r$p$salt$hash — self-describing, so the parameters can be raised
    later without invalidating hashes made today."""
    salt = secrets.token_bytes(SALT_LEN)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DK_LEN)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time compare. Returns False for anything malformed rather than
    raising, so a corrupt row can't be used to probe behaviour."""
    if not stored or not password:
        return False
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except Exception:
        return False
    return hmac.compare_digest(dk, expected)


def needs_rehash(stored: str) -> bool:
    """True when a hash was made with weaker parameters than we now use."""
    try:
        scheme, n, r, p, _, _ = stored.split("$")
        return scheme != "scrypt" or int(n) < SCRYPT_N
    except Exception:
        return True


# ------------------------------------------------------------------ strength

COMMON = {
    "password", "password1", "123456789", "qwertyuiop", "sportstech",
    "letmein123", "welcome123", "changeme123", "admin12345", "iloveyou1",
}


# The word part of the most-guessed passwords. Checked after stripping digits and
# symbols, so `Passw0rd!` and `password123` are both caught.
SYMBOLS = "!@#$%^&*()-_=+[]{};:,.?/\\|~<>"
SYMBOL_EXAMPLES = "! @ # - _ ?"

COMMON_BASES = {
    "password", "passwort", "qwerty", "qwertz", "letmein", "welcome", "admin",
    "administrator", "iloveyou", "sunshine", "monkey", "dragon", "football",
    "baseball", "princess", "shadow", "master", "superman", "trustno", "login",
    "abc", "test", "guest", "changeme", "secret", "hello", "freedom", "whatever",
}


def password_problem(password: str, email: str = "") -> str:
    """Empty string when acceptable, otherwise the reason.

    House rules, in this order:
      1. starts with a capital letter
      2. contains at least one number
      3. contains at least one symbol
      4. long enough
      5. isn't an obvious guess (a common word, or mostly your own name)

    Rules 1-3 are the requested pattern. Rules 4-5 stay because complexity alone
    doesn't stop `Sportstech2026!` — which satisfies every one of the first three.
    """
    pw = password or ""

    if not pw:
        return "Enter a password."
    if pw.strip() != pw:
        return "It can't start or end with a space."
    if not pw[0].isupper():
        return "Start it with a capital letter."
    if not any(ch.isdigit() for ch in pw):
        return "Include at least one number."
    if not any(ch in SYMBOLS for ch in pw):
        return f"Include at least one symbol, such as {SYMBOL_EXAMPLES}."
    if len(pw) < MIN_PASSWORD_LEN:
        return f"Use at least {MIN_PASSWORD_LEN} characters."
    if len(pw) > 200:
        return "That's longer than 200 characters."

    # A common word dressed up still fails in seconds: Passw0rd! ticks every box
    # above, and it is one of the most-guessed passwords there is.
    # Two readings, because either can hide a common word:
    #   "password123!" -> strip the padding      -> "password..."
    #   "Passw0rd!"    -> undo the 0-for-o swap  -> "password"
    # And a PREFIX match rather than an exact one, because digits translated back
    # into letters leave debris on the end ("qwertyiee") that an exact match on
    # the whole string would miss.
    stripped = "".join(ch for ch in pw.lower() if ch.isalpha())
    unleet = "".join(ch for ch in pw.lower().translate(str.maketrans({
        "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t",
        "@": "a", "$": "s"})) if ch.isalpha())
    for base in COMMON_BASES:
        for reading in (stripped, unleet):
            # The base has to be essentially the whole thing, not merely present:
            # "Rowing-password-machine-9!" is fine, "Password123!" is not.
            if reading.startswith(base) and len(reading) - len(base) <= 4:
                return (f"'{base}' is one of the first words anyone tries, even with "
                        "a number and symbol added. Pick something unrelated.")
    if pw.lower() in COMMON:
        return "That password is one of the most common ones — pick something else."
    if len(set(pw)) < 5:
        return "Use a few more different characters — this one repeats too much."

    # Your own name or company is only a problem when it IS most of the password.
    def too_much_of_it(word: str, what: str) -> str:
        if not word or len(word) < 4 or word not in pw.lower():
            return ""
        remainder = pw.lower().replace(word, "")
        meaningful = "".join(ch for ch in remainder if ch.isalpha())
        if len(remainder) >= MIN_PASSWORD_LEN and len(meaningful) >= 4:
            return ""
        return (f"'{word}' is most of this password, and it's {what} — so it's one "
                "of the first things anyone would try. Add more that isn't related "
                "to you.")

    problem = too_much_of_it((email or "").split("@")[0].lower(), "part of your email")
    if problem:
        return problem
    return too_much_of_it((email or "").split("@")[-1].split(".")[0].lower(),
                          "your company name")


def password_rules() -> list[str]:
    """The rules in plain words, so the UI can show them instead of guessing."""
    return [
        "Starts with a capital letter",
        "Contains at least one number",
        f"Contains at least one symbol ({SYMBOL_EXAMPLES})",
        f"At least {MIN_PASSWORD_LEN} characters",
        "Not a common word or mostly your own name",
    ]


def generate_password(words: int = 4) -> str:
    """Readable and strong, AND satisfying password_problem().

    The generator has to obey the same rules as everyone else — a hand-out
    password the app then refuses at first sign-in would be an infuriating bug.
    Shape: Capital word, hyphens, a number, a symbol.
    """
    alphabet = string.ascii_lowercase
    for _ in range(20):
        parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(words)]
        candidate = ("-".join(parts).capitalize()
                     + "-" + str(secrets.randbelow(90) + 10)
                     + secrets.choice("!@#?"))
        if not password_problem(candidate):
            return candidate
    # Unreachable in practice; a valid fallback beats returning something refused.
    return "Setup-" + str(secrets.randbelow(9000) + 1000) + "-change-me!"


# ------------------------------------------------------------------ accounts

def set_password(user_id: str, password: str, must_change: bool = False) -> bool:
    with dbx.db() as conn:
        cur = conn.execute(
            """UPDATE users SET password_hash = ?, must_change_password = ?,
                                failed_attempts = 0, locked_until = 0,
                                password_set_at = ?
               WHERE id = ?""",
            (hash_password(password), 1 if must_change else 0, dbx.now(), user_id))
        return cur.rowcount > 0


def clear_password(user_id: str) -> bool:
    """Removes the ability to log in without deleting the account or its messages."""
    with dbx.db() as conn:
        return conn.execute("UPDATE users SET password_hash = '' WHERE id = ?",
                            (user_id,)).rowcount > 0


def set_admin(user_id: str, is_admin: bool) -> bool:
    with dbx.db() as conn:
        return conn.execute("UPDATE users SET is_admin = ? WHERE id = ?",
                            (1 if is_admin else 0, user_id)).rowcount > 0


def is_admin(user_id: str) -> bool:
    with dbx.db() as conn:
        row = conn.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        return bool(row and row["is_admin"])


def admin_count() -> int:
    with dbx.db() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM users WHERE is_admin = 1 AND is_bot = 0").fetchone()["c"]


def normalise_email(email: str) -> str:
    return (email or "").strip().lower()


def find_by_email(email: str) -> dict | None:
    with dbx.db() as conn:
        row = conn.execute("SELECT * FROM users WHERE LOWER(email) = ? AND is_bot = 0",
                           (normalise_email(email),)).fetchone()
        return dict(row) if row else None


# ------------------------------------------------------------------ login

class LoginError(Exception):
    def __init__(self, message: str, retry_after: int = 0):
        super().__init__(message)
        self.retry_after = retry_after


def attempt_login(email: str, password: str) -> dict:
    """Return the user on success, raise LoginError otherwise.

    The failure message is identical whether the account doesn't exist, has no
    password, or the password is wrong — otherwise the endpoint tells an attacker
    which addresses are real accounts.
    """
    generic = "That email and password don't match an account."
    user = find_by_email(email)

    if not user or not user.get("password_hash"):
        # Still spend the time a real check would, so a missing account isn't
        # detectable from how fast the answer comes back.
        verify_password(password or "x", hash_password("timing-equaliser"))
        raise LoginError(generic)

    locked_until = float(user.get("locked_until") or 0)
    if locked_until > dbx.now():
        wait = int(locked_until - dbx.now())
        raise LoginError(
            f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).",
            retry_after=wait)

    if not verify_password(password, user["password_hash"]):
        with dbx.db() as conn:
            attempts = int(user.get("failed_attempts") or 0) + 1
            lock = dbx.now() + LOCKOUT_SECONDS if attempts >= MAX_ATTEMPTS else 0
            conn.execute("UPDATE users SET failed_attempts = ?, locked_until = ? WHERE id = ?",
                         (attempts, lock, user["id"]))
        if attempts >= MAX_ATTEMPTS:
            raise LoginError(
                f"Too many failed attempts. Locked for {LOCKOUT_SECONDS // 60} minutes.",
                retry_after=LOCKOUT_SECONDS)
        raise LoginError(generic)

    with dbx.db() as conn:
        conn.execute("UPDATE users SET failed_attempts = 0, locked_until = 0 WHERE id = ?",
                     (user["id"],))
        # Transparently upgrade a hash made with older parameters.
        if needs_rehash(user["password_hash"]):
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                         (hash_password(password), user["id"]))
    return find_by_email(email)


def change_password(user_id: str, current: str, new: str) -> str:
    """Empty string on success, otherwise the reason. Requires the current password
    even when a change is being forced, so a left-open session can't be used to
    take the account over."""
    with dbx.db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        return "No such account."
    user = dict(row)
    if not verify_password(current, user.get("password_hash") or ""):
        return "Your current password is wrong."
    problem = password_problem(new, user.get("email", ""))
    if problem:
        return problem
    if verify_password(new, user["password_hash"]):
        return "That's the same password you already have."
    set_password(user_id, new, must_change=False)
    return ""