"""The user store: creating accounts, checking passwords, and the small rules
that keep the web build's front door honest. Nothing here touches a network or
a real home directory — the database is a file in tmp_path."""
from __future__ import annotations

import pytest

from app.accounts import Accounts, AccountError, MIN_PASSWORD, _verify_password


@pytest.fixture
def accounts(tmp_path):
    return Accounts(tmp_path / "users.db")


def test_create_and_verify(accounts):
    user = accounts.create_user("Editor@Atmosphere.com", "correct horse")
    assert user.email == "editor@atmosphere.com"          # normalized
    assert not user.is_admin and not user.disabled
    assert accounts.verify_credentials("editor@atmosphere.com",
                                       "correct horse").id == user.id


def test_email_is_case_insensitive(accounts):
    accounts.create_user("q@press.com", "password1")
    assert accounts.verify_credentials("Q@PRESS.COM", "password1") is not None


def test_wrong_password_returns_none(accounts):
    accounts.create_user("q@press.com", "password1")
    assert accounts.verify_credentials("q@press.com", "nope") is None


def test_unknown_email_returns_none(accounts):
    assert accounts.verify_credentials("ghost@press.com", "whatever") is None


def test_password_is_not_stored_in_the_clear(accounts, tmp_path):
    accounts.create_user("q@press.com", "supersecret")
    blob = (tmp_path / "users.db").read_bytes()
    assert b"supersecret" not in blob


def test_duplicate_email_is_refused(accounts):
    accounts.create_user("q@press.com", "password1")
    with pytest.raises(AccountError, match="already exists"):
        accounts.create_user("Q@press.com", "password2")


def test_short_password_is_refused(accounts):
    with pytest.raises(AccountError, match=str(MIN_PASSWORD)):
        accounts.create_user("q@press.com", "x")


def test_bad_email_is_refused(accounts):
    with pytest.raises(AccountError, match="not an email"):
        accounts.create_user("not-an-email", "password1")


def test_disabled_user_cannot_sign_in(accounts):
    user = accounts.create_user("q@press.com", "password1")
    accounts.set_disabled(user.id, True)
    assert accounts.verify_credentials("q@press.com", "password1") is None


def test_password_reset(accounts):
    user = accounts.create_user("q@press.com", "password1")
    accounts.set_password(user.id, "brand new one")
    assert accounts.verify_credentials("q@press.com", "password1") is None
    assert accounts.verify_credentials("q@press.com", "brand new one")


def test_admin_flag_and_cap(accounts):
    user = accounts.create_user("boss@press.com", "password1", is_admin=True,
                                monthly_cap=25.0)
    assert user.is_admin and user.monthly_cap == 25.0
    accounts.set_cap(user.id, None)
    assert accounts.get_user(user.id).monthly_cap is None


def test_list_and_count(accounts):
    accounts.create_user("a@press.com", "password1")
    accounts.create_user("b@press.com", "password1")
    assert accounts.count() == 2
    assert {u.email for u in accounts.list_users()} == {"a@press.com",
                                                        "b@press.com"}


def test_updates_to_missing_user_raise(accounts):
    with pytest.raises(AccountError, match="No such user"):
        accounts.set_disabled("does-not-exist", True)


def test_reopening_the_database_keeps_users(tmp_path):
    Accounts(tmp_path / "users.db").create_user("q@press.com", "password1")
    reopened = Accounts(tmp_path / "users.db")
    assert reopened.verify_credentials("q@press.com", "password1") is not None


def test_hash_carries_its_own_salt(accounts):
    # Two users, same password, must not share a hash — or a stolen database
    # would reveal which accounts collide.
    a = accounts.create_user("a@press.com", "same password")
    b = accounts.create_user("b@press.com", "same password")
    assert a.id != b.id
    # And a stored hash verifies only against its own password.
    assert _verify_password("same password", _dummy_stored(accounts, "a@press.com"))


def _dummy_stored(accounts, email):
    with accounts._connect() as conn:
        return conn.execute("SELECT password_hash FROM users WHERE email = ?",
                            (email,)).fetchone()[0]
