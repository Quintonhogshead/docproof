"""`docproof-admin` — make and manage the web build's accounts.

There is no sign-up page, on purpose: the press decides who gets in. This is
how they do it. The first command anyone runs on a fresh server is

    docproof-admin add-user --admin you@yourpress.com

which creates the account that then creates everyone else's.

`--home` is the same home the server reads (DOCPROOF_HOME), so on the server
these commands and the running app see one database. Passwords are typed at a
prompt, never passed as an argument — an argument would land in shell history
and the process list.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from .accounts import Accounts, AccountError
from .settings import Paths, default_root

OK, USAGE, FAILED = 0, 2, 1


class _Abort(Exception):
    """A command could not run, with the message the user should see. Caught in
    `main` so every path leaves through a return code, not a raised exit."""


def _accounts(home: str | None) -> Accounts:
    root = Path(home).expanduser() if home else default_root()
    return Accounts(Paths(root).users_db)


def _prompt_password(confirm: bool = True) -> str:
    pw = getpass.getpass("Password: ")
    if confirm and pw != getpass.getpass("Repeat password: "):
        raise _Abort("Passwords did not match.")
    return pw


def _find(accounts: Accounts, email: str):
    user = accounts.get_by_email(email)
    if user is None:
        raise _Abort(f"No account for {email}.")
    return user


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="docproof-admin",
        description="Create and manage DocProof web accounts.")
    ap.add_argument("--home", help="where accounts live "
                                   "(default: DOCPROOF_HOME, else the app's "
                                   "usual home directory)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    add = sub.add_parser("add-user", help="create an account")
    add.add_argument("email")
    add.add_argument("--admin", action="store_true",
                     help="let this account manage other accounts")
    add.add_argument("--cap", type=float, metavar="USD",
                     help="monthly spend ceiling for this user")

    sub.add_parser("list-users", help="show every account")

    rp = sub.add_parser("reset-password", help="set a new password")
    rp.add_argument("email")

    dis = sub.add_parser("disable-user", help="block an account from signing in")
    dis.add_argument("email")

    ena = sub.add_parser("enable-user", help="let a blocked account sign in again")
    ena.add_argument("email")

    cap = sub.add_parser("set-cap", help="change a user's monthly spend ceiling")
    cap.add_argument("email")
    cap.add_argument("usd", nargs="?", default=None,
                     help="the ceiling in dollars; omit to clear it")

    args = ap.parse_args(argv)
    accounts = _accounts(args.home)
    try:
        return _dispatch(accounts, args)
    except _Abort as e:
        print(e, file=sys.stderr)
        return FAILED


def _dispatch(accounts: Accounts, args) -> int:
    if args.cmd == "add-user":
        try:
            user = accounts.create_user(args.email, _prompt_password(),
                                        is_admin=args.admin,
                                        monthly_cap=args.cap)
        except AccountError as e:
            raise _Abort(str(e))
        role = "admin" if user.is_admin else "user"
        print(f"Created {role} {user.email}.")
        return OK

    if args.cmd == "list-users":
        users = accounts.list_users()
        if not users:
            print("No accounts yet. Create one with `add-user`.")
            return OK
        for u in users:
            marks = " ".join(filter(None, [
                "admin" if u.is_admin else "",
                "disabled" if u.disabled else "",
                f"cap ${u.monthly_cap:.2f}" if u.monthly_cap is not None else ""]))
            print(f"{u.email}{'  (' + marks + ')' if marks else ''}")
        return OK

    if args.cmd == "reset-password":
        user = _find(accounts, args.email)
        try:
            accounts.set_password(user.id, _prompt_password())
        except AccountError as e:
            raise _Abort(str(e))
        print(f"Password changed for {user.email}.")
        return OK

    if args.cmd in ("disable-user", "enable-user"):
        user = _find(accounts, args.email)
        accounts.set_disabled(user.id, args.cmd == "disable-user")
        print(f"{'Disabled' if args.cmd == 'disable-user' else 'Enabled'} "
              f"{user.email}.")
        return OK

    if args.cmd == "set-cap":
        user = _find(accounts, args.email)
        cap = None if args.usd is None else float(args.usd)
        accounts.set_cap(user.id, cap)
        print(f"Cap for {user.email} is now "
              f"{'cleared' if cap is None else f'${cap:.2f}'}.")
        return OK

    return USAGE


if __name__ == "__main__":
    raise SystemExit(main())
