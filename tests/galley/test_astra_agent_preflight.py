"""Missing final-review authentication must not consume a Claude book run."""
import pytest

from galley import agent, codex_runner, driver
from galley.astra_review import AstraReviewError


def test_subscription_login_checked_before_any_book_phase(tmp_path, monkeypatch):
    calls = []

    def unavailable():
        calls.append("auth")
        raise AstraReviewError("Worker needs its ChatGPT login")

    monkeypatch.setattr(codex_runner, "check_login", unavailable)
    monkeypatch.setattr(driver.Driver, "run", lambda self: calls.append("book"))
    with pytest.raises(AstraReviewError, match="ChatGPT login"):
        agent._run_driver(book=tmp_path / "book.docx", slug="test", workspace_root=tmp_path)
    assert calls == ["auth"]


def test_explicit_api_review_does_not_require_subscription_login(tmp_path, monkeypatch):
    from galley import intake
    calls = []
    monkeypatch.setattr(codex_runner, "check_login", lambda: calls.append("auth"))
    monkeypatch.setattr(driver.Driver, "run", lambda self: calls.append("book"))
    monkeypatch.setattr(intake, "format_for_proof", lambda book, ws, **kw: book)
    agent._run_driver(book=tmp_path / "book.docx", slug="test", workspace_root=tmp_path,
                      astra_transport="api")
    assert calls == ["book"]
