"""`docproof-admin`, driven the way an administrator would from a terminal.
Passwords come through a patched getpass so no test types at a real prompt, and
every command runs against a database in tmp_path."""
from __future__ import annotations

import pytest

from app import admin
from app.accounts import Accounts
from app.settings import Paths


@pytest.fixture
def home(tmp_path):
    return tmp_path


@pytest.fixture
def accounts(home):
    return Accounts(Paths(home).users_db)


def _run(home, *argv, password=None):
    return admin.main(["--home", str(home), *argv])


@pytest.fixture(autouse=True)
def _password(monkeypatch):
    # Default password prompt answer; individual tests override the sequence.
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "password1")


def test_add_user_then_verify(home, accounts):
    assert _run(home, "add-user", "editor@press.com") == 0
    assert accounts.verify_credentials("editor@press.com", "password1")


def test_add_admin_with_cap(home, accounts):
    assert _run(home, "add-user", "--admin", "--cap", "30", "boss@press.com") == 0
    user = accounts.get_by_email("boss@press.com")
    assert user.is_admin and user.monthly_cap == 30.0


def test_add_user_rejects_duplicate(home):
    assert _run(home, "add-user", "q@press.com") == 0
    assert _run(home, "add-user", "q@press.com") == admin.FAILED


def test_password_mismatch_aborts(home, monkeypatch):
    answers = iter(["password1", "different"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert _run(home, "add-user", "q@press.com") == admin.FAILED


def test_reset_password(home, accounts, monkeypatch):
    _run(home, "add-user", "q@press.com")
    answers = iter(["freshpassword", "freshpassword"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    assert _run(home, "reset-password", "q@press.com") == 0
    assert accounts.verify_credentials("q@press.com", "password1") is None
    assert accounts.verify_credentials("q@press.com", "freshpassword")


def test_disable_and_enable(home, accounts):
    _run(home, "add-user", "q@press.com")
    _run(home, "disable-user", "q@press.com")
    assert accounts.verify_credentials("q@press.com", "password1") is None
    _run(home, "enable-user", "q@press.com")
    assert accounts.verify_credentials("q@press.com", "password1")


def test_set_and_clear_cap(home, accounts):
    _run(home, "add-user", "q@press.com")
    _run(home, "set-cap", "q@press.com", "12.50")
    assert accounts.get_by_email("q@press.com").monthly_cap == 12.5
    _run(home, "set-cap", "q@press.com")
    assert accounts.get_by_email("q@press.com").monthly_cap is None


def test_commands_on_missing_user_fail(home):
    assert _run(home, "disable-user", "ghost@press.com") == admin.FAILED


def test_list_users(home, capsys):
    _run(home, "add-user", "--admin", "boss@press.com")
    capsys.readouterr()
    _run(home, "list-users")
    out = capsys.readouterr().out
    assert "boss@press.com" in out and "admin" in out
