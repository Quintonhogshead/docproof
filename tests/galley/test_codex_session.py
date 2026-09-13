"""Exercise concurrent JSON-RPC over a real subprocess with an offline fake server."""
from concurrent.futures import ThreadPoolExecutor
import json
import sys
import time

import pytest

from galley import codex_runner as cr
from galley.codex_session import SubscriptionSession
from galley.astra_review import AstraReviewError

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}


@pytest.fixture
def server(tmp_path, monkeypatch):
    binary = tmp_path / "fake-codex"
    log = tmp_path / "calls.jsonl"
    binary.write_text("#!" + sys.executable + "\n" + '''
import json, sys, threading, time
from pathlib import Path
lock = threading.Lock()
threads = {}
active = 0
counter = 0
log = Path(''' + repr(str(log)) + ''')
def send(row):
    with lock:
        print(json.dumps(row), flush=True)
def finish(tid, turn, prompt):
    global active
    # Wait until another independent model request has started.
    deadline = time.monotonic() + 1.5
    while active < 2 and time.monotonic() < deadline and prompt != "solo":
        time.sleep(.005)
    if prompt == "disconnect":
        import os
        os._exit(2)
    if prompt == "timeout":
        time.sleep(3)
    send({"method": "turn/started", "params": {"threadId": tid, "turn": {"id": turn}}})
    kind = "commandExecution" if prompt == "tool" else "agentMessage"
    send({"method": "item/completed", "params": {"threadId": tid, "turnId": turn,
          "item": {"id": "message", "type": kind, "phase": "final_answer", "text": json.dumps({"answer": prompt})}}})
    send({"method": "thread/tokenUsage/updated", "params": {"threadId": tid, "turnId": turn,
          "tokenUsage": {"total": {"inputTokens": 100, "cachedInputTokens": 10, "outputTokens": 5}}}})
    send({"method": "turn/completed", "params": {"threadId": tid, "turn": {"id": turn, "status": "completed"}}})
    with lock:
        active -= 1
for line in sys.stdin:
    row = json.loads(line)
    method, p = row.get("method"), row.get("params", {})
    if "id" not in row: continue
    result = {}
    if method == "stall":
        send({"id": row["id"], "result": {}})
        time.sleep(4)
        continue
    if method == "account/read":
        result = {"account": {"type": "chatgpt"}}
    elif method == "thread/start":
        counter += 1
        tid = "thread-" + str(counter)
        result = {"thread": {"id": tid}, "instructionSources": []}
        assert p["ephemeral"] is True and p["approvalPolicy"] == "never" and p["sandbox"] == "read-only"
        assert not any(p["config"]["features"].values())
    elif method == "turn/start":
        tid = p["threadId"]
        turn = "turn-" + tid
        prompt = p["input"][0]["text"]
        with lock:
            active += 1
            with log.open("a") as f:
                f.write(json.dumps({"thread": tid, "active": active, "model": p["model"]}) + "\\n")
        result = {"turn": {"id": turn}}
        send({"id": row["id"], "result": result})
        threading.Thread(target=finish, args=(tid, turn, prompt), daemon=True).start()
        continue
    send({"id": row["id"], "result": result})
''')
    binary.chmod(0o700)
    monkeypatch.setenv("GALLEY_CODEX_HOME", str(tmp_path / "auth-home"))
    return str(binary), log


def call(tmp_path, server, session, key, prompt=None, **options):
    return cr.run_structured(prompt or key, SCHEMA, tmp_path / "work", request_id=key,
                             codex_bin=server[0], session=session, no_tools=True,
                             timeout_seconds=5, **options)


def test_shared_login_allows_simultaneous_isolated_turns_and_cached_resume(server, tmp_path):
    session = SubscriptionSession()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, tmp_path, server, session, key) for key in ("one", "two")]
            assert [f.result(timeout=8) for f in futures] == [{"answer": "one"}, {"answer": "two"}]
        activity = [json.loads(line) for line in server[1].read_text().splitlines()]
        assert max(row["active"] for row in activity) == 2
        assert len({row["thread"] for row in activity}) == 2
        for key in ("one", "two"):
            directory = cr.request_directory(tmp_path / "work", key)
            saved = json.loads((directory / "receipt.json").read_text())
            assert saved["process_exited"] is False and saved["turn_terminal"] is True
            assert saved["usage"]["output_tokens"] == 5
            assert saved["event_counts"]["turn.completed"] == 1
            assert call(tmp_path, server, session, key) == {"answer": key}
        assert len(server[1].read_text().splitlines()) == 2
    finally:
        session.close()
    assert session.process.poll() is not None


def test_same_request_still_submits_only_once(server, tmp_path):
    session = SubscriptionSession()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(call, tmp_path, server, session, "same", "solo") for _ in range(2)]
            assert all(f.result(timeout=8) == {"answer": "solo"} for f in results)
        assert len(server[1].read_text().splitlines()) == 1
    finally:
        session.close()


def test_forbidden_tool_turn_never_certifies(server, tmp_path):
    session = SubscriptionSession()
    try:
        with pytest.raises(AstraReviewError, match="tools|turn"):
            call(tmp_path, server, session, "tool")
        receipt = json.loads((cr.request_directory(tmp_path / "work", "tool") / "receipt.json").read_text())
        assert receipt["status"] == "operational_failure"
        assert not (cr.request_directory(tmp_path / "work", "tool") / "result.json").exists()
    finally:
        session.close()


def test_disconnection_unblocks_every_reader_without_resubmission(server, tmp_path):
    session = SubscriptionSession()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, tmp_path, server, session, key) for key in ("disconnect", "timeout")]
            for future in futures:
                with pytest.raises(AstraReviewError):
                    future.result(timeout=8)
        assert len(server[1].read_text().splitlines()) == 2
    finally:
        session.close()


def test_completed_server_turn_can_recover_bookkeeping_interruption(server, tmp_path):
    session = SubscriptionSession()
    try:
        assert call(tmp_path, server, session, "one", "solo") == {"answer": "solo"}
        directory = cr.request_directory(tmp_path / "work", "one")
        receipt = json.loads((directory / "receipt.json").read_text())
        receipt["status"] = "running"
        receipt.pop("result_sha256")
        (directory / "result.json").unlink()
        cr._atomic(directory / "receipt.json", receipt)
        assert call(tmp_path, server, session, "one", "solo") == {"answer": "solo"}
        assert len(server[1].read_text().splitlines()) == 1
    finally:
        session.close()



def test_unresponsive_server_cannot_block_rpc_deadline_inside_pipe_write(server, tmp_path):
    session = SubscriptionSession()
    try:
        session.check_login(server[0], cr.codex_home(), time.monotonic() + 3)
        session._rpc("stall", {}, time.monotonic() + 2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(session._rpc, "large", {"text": "x" * 2_000_000}, time.monotonic() + .1)
            with pytest.raises(TimeoutError):
                future.result(timeout=2)
            assert future.done()
    finally:
        session.close()
