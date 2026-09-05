"""User accounts for the web build.

The desktop app has no users — it is one person at one Mac, and nothing here
runs. The web build has many, so it needs somewhere to keep who may sign in
and what each of them has spent. That is this file: a single SQLite database
beside the rest of a home directory's state, and the password hashing that
guards it.

There is no sign-up. Accounts are made by an administrator from the command
line (`docproof-admin`, see app/admin.py) — the press decides who gets in, not
whoever finds the URL. So the surface here is create/verify/list, not register.

Every method opens its own connection and closes it. SQLite in WAL mode is
happy with that, it sidesteps the "one connection per thread" rule entirely
(the server and its worker threads all touch this), and the cost is nothing at
the handful-of-users scale this is built for.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("docproof.app.accounts")

# scrypt parameters. N is the work factor; 2**14 is the interactive-login
# figure from the RFC, tuned to cost a fraction of a second here and a great
# deal to anyone testing passwords in bulk. Raising N later is safe: old hashes
# carry their own parameters in the stored string, so they still verify.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_BYTES = 32
# A password to hash when no user matched, so a wrong email costs the same time
# as a wrong password and the response can't be used to enumerate accounts.
_DUMMY_HASH = ("scrypt$" + "00" * _SALT_BYTES + "$" + "00" * _KEY_BYTES)

MIN_PASSWORD = 8
CURRENT_SCHEMA = 1


class AccountError(Exception):
    """Something the caller asked for cannot be done: a duplicate email, a
    password too short, a user that isn't there. The message is written to be
    shown to whoever typed the command or filled the form."""


@dataclass(frozen=True)
class User:
    id: str
    email: str
    is_admin: bool
    disabled: bool
    created_at: str
    # A per-user spend ceiling in USD for the current month, or None to fall
    # back to the server-wide default. The cap is read here and enforced where
    # jobs are submitted; this table only stores it.
    monthly_cap: float | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt if salt is not None else os.urandom(_SALT_BYTES)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N,
                         r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_BYTES)
    return f"scrypt${salt.hex()}${key.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, key_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    candidate = _hash_password(password, bytes.fromhex(salt_hex))
    # compare_digest, not ==, so a match can't be timed byte by byte.
    return hmac.compare_digest(candidate, stored)


def _row_to_user(row: sqlite3.Row) -> User:
    return User(id=row["id"], email=row["email"],
                is_admin=bool(row["is_admin"]), disabled=bool(row["disabled"]),
                created_at=row["created_at"], monthly_cap=row["monthly_cap"])


class Accounts:
    """The users table, and the password checking that stands in front of it."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # WAL lets the readers (page loads) run while a writer (a new account)
        # holds the file; without it the threaded server would serialise on
        # every request.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id            TEXT PRIMARY KEY,
                    email         TEXT UNIQUE NOT NULL COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    is_admin      INTEGER NOT NULL DEFAULT 0,
                    disabled      INTEGER NOT NULL DEFAULT 0,
                    created_at    TEXT NOT NULL,
                    monthly_cap   REAL
                );
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER NOT NULL
                );
                """
            )
            # The version row is the seam every future migration turns on — and
            # the same table later connector tokens (Drive, HubSpot) will be
            # added under. Stamp it once, on a fresh database.
            if conn.execute("SELECT COUNT(*) FROM schema_version"
                            ).fetchone()[0] == 0:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)",
                             (CURRENT_SCHEMA,))


    def create_user(self, email: str, password: str, *,
                    is_admin: bool = False,
                    monthly_cap: float | None = None) -> User:
        """Add an account. Raises AccountError on a bad email, a short
        password, or an address that already exists."""
        email = _normalize_email(email)
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise AccountError(f"{email!r} is not an email address")
        if len(password) < MIN_PASSWORD:
            raise AccountError(
                f"Password must be at least {MIN_PASSWORD} characters")
        user_id = str(uuid.uuid4())
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO users (id, email, password_hash, is_admin, "
                    "disabled, created_at, monthly_cap) "
                    "VALUES (?, ?, ?, ?, 0, ?, ?)",
                    (user_id, email, _hash_password(password),
                     int(is_admin), _now(), monthly_cap))
        except sqlite3.IntegrityError:
            # The UNIQUE constraint on email is the only one that can trip.
            raise AccountError(f"An account for {email} already exists")
        log.info("Created %saccount %s", "admin " if is_admin else "", email)
        got = self.get_user(user_id)
        assert got is not None                    # just written, in this method
        return got

    def set_password(self, user_id: str, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise AccountError(
                f"Password must be at least {MIN_PASSWORD} characters")
        self._must_update(user_id, "password_hash = ?",
                          (_hash_password(password),))

    def set_disabled(self, user_id: str, disabled: bool) -> None:
        self._must_update(user_id, "disabled = ?", (int(disabled),))

    def set_admin(self, user_id: str, is_admin: bool) -> None:
        self._must_update(user_id, "is_admin = ?", (int(is_admin),))

    def set_cap(self, user_id: str, monthly_cap: float | None) -> None:
        self._must_update(user_id, "monthly_cap = ?", (monthly_cap,))

    def _must_update(self, user_id: str, assignment: str,
                     params: tuple) -> None:
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE users SET {assignment} WHERE id = ?",
                              (*params, user_id))
            if cur.rowcount == 0:
                raise AccountError("No such user")


    def verify_credentials(self, email: str, password: str) -> User | None:
        """Return the user for a correct email+password, or None. A disabled
        account never verifies. Runs the same hashing whether or not the email
        exists, so timing does not leak which addresses are real."""
        email = _normalize_email(email)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        stored = row["password_hash"] if row else _DUMMY_HASH
        ok = _verify_password(password, stored)
        if not ok or row is None or row["disabled"]:
            return None
        return _row_to_user(row)

    def get_user(self, user_id: str) -> User | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row) if row else None

    def get_by_email(self, email: str) -> User | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?",
                              (_normalize_email(email),)).fetchone()
        return _row_to_user(row) if row else None

    def list_users(self) -> list[User]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM users ORDER BY created_at").fetchall()
        return [_row_to_user(r) for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
