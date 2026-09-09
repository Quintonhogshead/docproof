from __future__ import annotations

import json
import types
import urllib.parse
from pathlib import Path

from app.watch import native_corrections as native
from app.watch.drive import DriveFile
from app.watch.settings import WatchSettings
from app.watch.state import WatchState
from app.watch.tick import TickReport

from .fakes import drive_entry, fake_drive


def test_native_source_selection_is_numeric_and_refuses_ties():
    entries = [
        DriveFile("a", "Johnson - Book 9.indd", "application/octet-stream"),
        DriveFile("b", "Johnson - Book 10.indd", "application/octet-stream"),
    ]
    assert native.pick_source(entries, "Johnson")[0].id == "b"
    tied = entries + [DriveFile("c", "Johnson - Book 10.indd", "application/octet-stream")]
    assert native.pick_source(tied, "Johnson") == (None, "tie")
    assert native.newer_export(
        [DriveFile("out", "Johnson - Book 11.indd", "application/octet-stream"),
         DriveFile("new", "Johnson - Book 12.indd", "application/octet-stream")],
        "Johnson", 10).id == "new"


def test_native_submission_marker_is_not_treated_as_a_file_url():
    ws = WatchSettings(corrections_native_submission_property="submission_id",
                       hubspot_corrections_file_property="files",
                       hubspot_corrections_text_property="notes")
    record = types.SimpleNamespace(properties={
        "submission_id": "2026-09-09T12:00:00Z",
        "files": "https://example.test/a.pdf;https://example.test/b.pdf",
        "notes": "fix the running head",
    })
    urls, text, marker = native._submission_properties(ws, record)
    assert urls == ["https://example.test/a.pdf", "https://example.test/b.pdf"]
    assert text == "fix the running head"
    assert marker == "2026-09-09T12:00:00Z"


def test_native_run_uploads_verified_outputs_once_and_resumes_receipts(tmp_path, monkeypatch):
    folder = "interior"
    files = {
        folder: {**drive_entry("Interior Design", mime="application/vnd.google-apps.folder"),
                 "parents": ["root"]},
        "source": {**drive_entry("Johnson - Book 10.indd", mime="application/octet-stream"),
                   "parents": [folder]},
    }
    opener = fake_drive(files, hubspot={"Johnson": {
        "docproof": "Ready for Corrections", "lastname": "Johnson",
        "native_folder": folder, "submission_id": "sub-1",
        "corr_file": "https://api-na1.hubspot.com/form-integrations/v1/uploaded-files/signed-url-redirect/221489330197?filename=notes.pdf",
    }})
    opener.content["source"] = b"original-indd"
    opener.content["221489330197"] = b"corrections"
    calls = []

    def workflow(source, attachments, text, work_dir, rules=None):
        calls.append((source, list(attachments), dict(rules or {})))
        out = Path(work_dir)
        out.mkdir(parents=True, exist_ok=True)
        indd = out / "actual.indd"
        pdf = out / "actual.pdf"
        report = out / "correction-report.json"
        indd.write_bytes(b"corrected")
        pdf.write_bytes(b"pdf")
        report.write_text("{}", encoding="utf-8")
        return {"status": "verified", "needs_designer": False,
                "reasons": [], "output_indd": str(indd),
                "output_pdf": str(pdf), "report": str(report)}

    monkeypatch.setattr(native, "_call_workflow", workflow)
    ws = WatchSettings(
        folder_id="root", client_id="client", client_secret="secret",
        hubspot_enabled=True, hubspot_object="0-970",
        hubspot_status_property="docproof", hubspot_key_property="lastname",
        hubspot_first_property="firstname", hubspot_last_property="lastname",
        corrections_enabled=True, corrections_engine="native",
        corrections_native_folder_property="native_folder",
        corrections_native_submission_property="submission_id",
        hubspot_corrections_file_property="corr_file",
        hubspot_corrections_ready_value="Ready for Corrections",
        corrections_native_verified_value="verified",
        corrections_native_auto_upload=True,
    )
    report = TickReport()
    state = WatchState(tmp_path / "state.json")
    native.run_stage("drive-token", tmp_path, ws, state, None, None,
                     mock=False, opener=opener, hs_token="hubspot-token",
                     report=report)
    assert len(calls) == 1
    assert {name for name in report.uploaded} == {
        "Johnson - Book 11.indd", "Johnson - Book 11.pdf",
        "Johnson - Book 11.report.json",
    }
    assert opener.hubspot["hs-Johnson"]["properties"]["docproof"] == "verified"

    # The CRM gate moved on, so a retry neither invokes the native workflow nor
    # creates a second set of outputs.
    second = TickReport()
    native.run_stage("drive-token", tmp_path, ws, state, None, None,
                     mock=False, opener=opener, hs_token="hubspot-token",
                     report=second)
    assert len(calls) == 1


def test_form_poll_paginates_and_keeps_configured_file_and_notes_separate(monkeypatch):
    answers = iter([
        {"results": [{"submittedAt": "2026-09-01T00:00:00Z", "values": [
            {"name": "firstname", "value": "Quinton"},
            {"name": "lastname", "value": "Johnson"},
            {"name": "files", "value": "https://example.test/one.pdf"},
            {"name": "notes", "value": "first note"},
        ]}], "paging": {"next": {"after": "1"}}},
        {"results": [{"submittedAt": "2026-09-02T00:00:00Z", "values": [
            {"name": "firstname", "value": "Quinton"},
            {"name": "lastname", "value": "Johnson"},
            {"name": "files", "value": "https://example.test/two.pdf"},
            {"name": "notes", "value": "second note"},
        ]}]},
    ])
    monkeypatch.setattr(native.hubspot, "_json_call", lambda *a, **k: next(answers))
    assert len(native.form_submissions("hs", "form", opener=lambda request: None)) == 2
    ws = WatchSettings(hubspot_first_property="firstname", hubspot_last_property="lastname",
                       hubspot_corrections_file_property="files",
                       hubspot_corrections_text_property="notes",
                       corrections_native_form_file_property="files",
                       corrections_native_form_notes_property="notes",
                       corrections_native_form_poll=True)
    record = types.SimpleNamespace(id="r1", properties={"firstname": "Quinton", "lastname": "Johnson"})
    rows = [
        {"submittedAt": "2026-09-01", "values": [
            {"name": "firstname", "value": "Quinton"}, {"name": "lastname", "value": "Johnson"},
            {"name": "files", "value": "https://example.test/a.pdf"}, {"name": "notes", "value": "one"}]},
        {"submittedAt": "2026-09-02", "values": [
            {"name": "firstname", "value": "Quinton"}, {"name": "lastname", "value": "Johnson"},
            {"name": "files", "value": "https://example.test/b.pdf"}, {"name": "notes", "value": "two"}]},
    ]
    events = native._events_for(record, rows, ws)
    assert [(e[0], e[1]) for e in events] == [(["https://example.test/a.pdf"], "one"),
                                               (["https://example.test/b.pdf"], "two")]


def test_form_cutoff_handles_hubspot_millisecond_timestamps():
    ws = WatchSettings(hubspot_first_property="firstname", hubspot_last_property="lastname",
                       hubspot_corrections_file_property="files",
                       hubspot_corrections_text_property="notes",
                       corrections_native_form_file_property="files",
                       corrections_native_form_notes_property="notes",
                       corrections_native_start_after="2026-09-01T00:00:00Z")
    record = types.SimpleNamespace(id="r1", properties={"firstname": "Quinton", "lastname": "Johnson"})
    rows = [{"submittedAt": 1788307200000, "values": [
        {"name": "firstname", "value": "Quinton"}, {"name": "lastname", "value": "Johnson"},
        {"name": "files", "value": ""}, {"name": "notes", "value": "new"}]},
            {"submittedAt": 1756684800000, "values": [
        {"name": "firstname", "value": "Quinton"}, {"name": "lastname", "value": "Johnson"},
        {"name": "files", "value": ""}, {"name": "notes", "value": "old"}]}]
    assert [event[1] for event in native._events_for(record, rows, ws)] == ["new"]


def test_form_poll_does_not_stop_at_the_first_hundred(monkeypatch):
    pages = iter([
        {"results": [{"id": str(i)} for i in range(100)], "paging": {"next": {"after": "100"}}},
        {"results": [{"id": "100"}]},
    ])
    monkeypatch.setattr(native.hubspot, "_json_call", lambda *a, **k: next(pages))
    assert len(native.form_submissions("hs", "form", opener=lambda request: None)) == 101


def test_form_poll_uses_hubspot_safe_page_limit(monkeypatch):
    seen = []

    def answer(request, **_kwargs):
        seen.append(int(urllib.parse.parse_qs(
            urllib.parse.urlparse(request.full_url).query)["limit"][0]))
        return {"results": [{"id": "one"}]}

    monkeypatch.setattr(native.hubspot, "_json_call", answer)
    assert native.form_submissions("hs", "form", opener=lambda request: None) == [{"id": "one"}]
    assert seen == [50]


def test_blocked_and_completed_jobs_do_not_starve_a_pending_delivery(tmp_path, monkeypatch):
    root = tmp_path / "native_jobs"
    native._write_json(root / "blocked" / "job.json", {
        "job_id": "blocked", "status": "verified", "blocked": "source_changed",
        "folder_id": "f", "source_id": "s", "record_id": "r"})
    native._write_json(root / "complete" / "job.json", {
        "job_id": "complete", "status": "verified", "folder_id": "f",
        "source_id": "s", "record_id": "r2", "artifact_hashes": {"x": "h"},
        "uploaded": {"x": "drive-x"}, "crm_written": False})
    native._write_json(root / "pending" / "job.json", {
        "job_id": "pending", "status": "verified", "folder_id": "f",
        "source_id": "s", "record_id": "r3", "artifact_hashes": {"x": "h"},
        "uploaded": {}})
    source = DriveFile("s", "Johnson - Book 10.indd", "application/octet-stream")
    monkeypatch.setattr(native.drive, "list_folder", lambda *a, **k: [source])
    ws = WatchSettings(hubspot_write_back=False)
    jobs = native._pending_jobs("drive", tmp_path, ws, opener=lambda request: None)
    assert [work[0].id for work in jobs] == ["r3"]


def test_same_author_projects_can_be_resolved_by_explicit_book_field():
    ws = WatchSettings(hubspot_first_property="firstname", hubspot_last_property="lastname",
                       corrections_native_form_first_property="firstname",
                       corrections_native_form_last_property="lastname",
                       corrections_native_form_book_property="which_book",
                       corrections_native_project_book_property="book_title",
                       corrections_native_form_file_property="files",
                       corrections_native_form_notes_property="notes")
    record = types.SimpleNamespace(id="r2", properties={
        "firstname": "Quinton", "lastname": "Johnson", "book_title": "Book Two"})
    row = {"recordId": "r2", "submittedAt": 1788307200000, "values": [
        {"name": "firstname", "value": "Quinton"}, {"name": "lastname", "value": "Johnson"},
        {"name": "which_book", "value": "Book Two"}, {"name": "notes", "value": "fix"}]}
    assert native._events_for(record, [row], ws)[0][1] == "fix"
    row["values"][2]["value"] = "Book One"
    assert native._events_for(record, [row], ws) == []


def test_book_title_match_allows_unicode_punctuation_drift_but_not_words():
    ws = WatchSettings(
        hubspot_first_property="firstname", hubspot_last_property="lastname",
        corrections_native_form_first_property="firstname",
        corrections_native_form_last_property="lastname",
        corrections_native_form_book_property="which_book",
        corrections_native_project_book_property="book_title",
        corrections_native_form_notes_property="notes")
    record = types.SimpleNamespace(id="r1", properties={
        "firstname": "Quinton", "lastname": "Johnson",
        "book_title": "99 to 1: The Quantum Bridge"})
    row = {"submittedAt": 1788307200000, "values": [
        {"name": "firstname", "value": "Quinton"},
        {"name": "lastname", "value": "Johnson"},
        {"name": "which_book", "value": "99 to 1 The Quantum Bridge"},
        {"name": "notes", "value": "fix"}]}
    assert native._events_for(record, [row], ws)
    row["values"][2]["value"] = "99 to 1 The Quantum Bridges"
    assert not native._events_for(record, [row], ws)


def test_artifacts_must_stay_inside_job_and_include_report(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    indd, pdf = out / "x.indd", out / "x.pdf"
    indd.write_bytes(b"i")
    pdf.write_bytes(b"p")
    result = {"output_indd": str(indd), "output_pdf": str(pdf),
              "report": str(tmp_path / "outside.json")}
    assert len(native._artifact_paths(result, indd, out, "Johnson - Book 11.indd")) == 2


def test_native_upload_failure_resumes_without_reinvoking_workflow(tmp_path, monkeypatch):
    folder = "interior"
    files = {
        folder: {**drive_entry("Interior Design", mime="application/vnd.google-apps.folder"), "parents": ["root"]},
        "source": {**drive_entry("Johnson - Book 10.indd", mime="application/octet-stream"), "parents": [folder]},
    }
    opener = fake_drive(files, fail={"upload": RuntimeError("temporary Drive outage")}, hubspot={"Johnson": {
        "docproof": "Ready for Corrections", "lastname": "Johnson", "native_folder": folder,
        "submission_id": "submission-1", "corr_file": "https://api-na1.hubspot.com/form-integrations/v1/uploaded-files/signed-url-redirect/221489330197?filename=notes.pdf"}})
    opener.content["source"] = b"source"
    opener.content["221489330197"] = b"notes"
    calls = []

    def workflow(source, attachments, text, work_dir, rules=None):
        calls.append(1)
        out = Path(work_dir); out.mkdir(parents=True, exist_ok=True)
        for name, body in (("a.indd", b"i"), ("a.pdf", b"p"), ("correction-report.json", b"{}")):
            (out / name).write_bytes(body)
        return {"status": "verified", "needs_designer": False, "reasons": [],
                "output_indd": str(out / "a.indd"), "output_pdf": str(out / "a.pdf"),
                "report": str(out / "correction-report.json")}

    monkeypatch.setattr(native, "_call_workflow", workflow)
    ws = WatchSettings(folder_id="root", client_id="c", client_secret="s", hubspot_enabled=True,
                       hubspot_object="0-970", hubspot_status_property="docproof",
                       hubspot_key_property="lastname", hubspot_first_property="firstname",
                       hubspot_last_property="lastname", corrections_enabled=True,
                       corrections_engine="native", corrections_native_folder_property="native_folder",
                       corrections_native_submission_property="submission_id",
                       hubspot_corrections_file_property="corr_file",
                       hubspot_corrections_ready_value="Ready for Corrections",
                       corrections_native_verified_value="verified",
                       corrections_native_auto_upload=True)
    state = WatchState(tmp_path / "state.json")
    first = TickReport()
    native.run_stage("drive", tmp_path, ws, state, None, None, mock=False, opener=opener,
                     hs_token="hubspot", report=first)
    assert calls == [1] and first.failed
    opener.hubspot["hs-Johnson"]["properties"]["corr_file"] = "https://api-na1.hubspot.com/form-integrations/v1/uploaded-files/signed-url-redirect/221489330198?filename=changed.pdf"
    next((tmp_path / "native_jobs").glob("*/output/a.pdf")).write_bytes(b"tampered")
    second = TickReport()
    native.run_stage("drive", tmp_path, ws, state, None, None, mock=False, opener=opener,
                     hs_token="hubspot", report=second)
    assert calls == [1]
    assert second.failed and not second.uploaded


def test_form_driven_native_discovery_does_not_require_corrections_status(tmp_path, monkeypatch):
    folder = "interior"
    files = {
        folder: {**drive_entry("Interior Design", mime="application/vnd.google-apps.folder"), "parents": ["root"]},
        "source": {**drive_entry("Johnson - Book 10.indd", mime="application/octet-stream"), "parents": [folder]},
    }
    opener = fake_drive(files, hubspot={"Johnson": {
        "docproof": "Do Nothing", "firstname": "Quinton", "lastname": "Johnson",
        "native_folder": folder}})
    opener.content["source"] = b"source"
    monkeypatch.setattr(native, "form_submissions", lambda *a, **k: [{
        "submittedAt": "2026-09-10T00:00:00Z", "conversionId": "c-1",
        "values": [{"name": "firstname", "value": "Quinton"},
                   {"name": "lastname", "value": "Johnson"},
                   {"name": "files", "value": ""},
                   {"name": "notes", "value": "move the running head"}],
    }])
    def workflow(source, attachments, text, work_dir, rules=None):
        out = Path(work_dir); out.mkdir(parents=True, exist_ok=True)
        for name, body in (("a.indd", b"i"), ("a.pdf", b"p"), ("correction-report.json", b"{}")):
            (out / name).write_bytes(body)
        assert text == "move the running head"
        return {"status": "verified", "needs_designer": False, "reasons": [],
                "output_indd": str(out / "a.indd"), "output_pdf": str(out / "a.pdf"),
                "report": str(out / "correction-report.json")}
    monkeypatch.setattr(native, "_call_workflow", workflow)
    ws = WatchSettings(folder_id="root", client_id="c", client_secret="s", hubspot_enabled=True,
                       hubspot_object="0-970", hubspot_status_property="docproof",
                       hubspot_key_property="lastname", hubspot_first_property="firstname",
                       hubspot_last_property="lastname", corrections_enabled=True,
                       corrections_engine="native", corrections_native_form_poll=True,
                       corrections_native_start_after="2026-09-09T00:00:00Z",
                       corrections_native_folder_property="native_folder",
                       corrections_native_form_file_property="files",
                       corrections_native_form_notes_property="notes",
                       hubspot_corrections_file_property="files",
                       hubspot_corrections_text_property="notes",
                       corrections_native_status_property="native_status",
                       corrections_native_verified_value="Verified",
                       corrections_native_auto_upload=True)
    report = TickReport()
    native.run_stage("drive", tmp_path, ws, WatchState(tmp_path / "state.json"), None, None,
                     mock=False, opener=opener, hs_token="hubspot", report=report)
    assert report.corrected and opener.hubspot["hs-Johnson"]["properties"]["native_status"] == "Verified"


def test_manual_attachment_request_resumes_from_exact_cached_file(tmp_path, monkeypatch):
    folder = "interior"
    files = {
        folder: {**drive_entry("Interior Design", mime="application/vnd.google-apps.folder"),
                 "parents": ["root"]},
        "source": {**drive_entry("Johnson - Book 10.indd", mime="application/octet-stream"),
                   "parents": [folder]},
    }
    opener = fake_drive(files)
    opener.content["source"] = b"source"
    source = DriveFile("source", "Johnson - Book 10.indd", "application/octet-stream")
    record = native.hubspot.HubSpotRecord("record-1", {
        "firstname": "Quinton", "lastname": "Johnson", "native_folder": folder,
    })
    ws = WatchSettings(
        folder_id="root", hubspot_object="0-970", hubspot_first_property="firstname",
        hubspot_last_property="lastname", corrections_enabled=True,
        corrections_engine="native", corrections_native_folder_property="native_folder",
        corrections_native_auto_upload=False)
    event = (["https://api-na1.hubspot.com/form-integrations/v1/uploaded-files/signed-url-redirect/221406960965?filename=notes.pdf"],
             "fix", "1788969178266|submission-1")
    manual = tmp_path / "manual.pdf"
    manual.write_bytes(b"manual attachment")
    calls = []

    def unavailable(*_args, **_kwargs):
        raise native.native_files.ManualAttachmentRequired("221406960965", "notes.pdf")

    monkeypatch.setattr(native.native_files, "cached_file", lambda *_args: None)
    monkeypatch.setattr(native.native_files, "download_file", unavailable)
    monkeypatch.setattr(native, "_asset_sibling_folders", lambda *args, **kwargs: [])
    report = TickReport()
    work = (record, folder, native.NativeSource(source, 10, "Johnson"), [source], event)
    native._run_one("drive", tmp_path, ws, work, mock=False, opener=opener,
                    hs_token="hubspot", report=report)
    job_path = next((tmp_path / "native_jobs").glob("*/job.json"))
    job = json.loads(job_path.read_text("utf-8"))
    assert job["status"] == "awaiting_attachment"
    assert job["missing_attachments"][0]["file_id"] == "221406960965"
    assert not calls

    def workflow(source_path, attachments, text, work_dir, rules=None):
        calls.append((source_path, list(attachments), text))
        out = Path(work_dir); out.mkdir(parents=True, exist_ok=True)
        for name, body in (("a.indd", b"i"), ("a.pdf", b"p"),
                           ("correction-report.json", b"{}")):
            (out / name).write_bytes(body)
        return {"status": "verified", "needs_designer": False, "reasons": [],
                "output_indd": str(out / "a.indd"),
                "output_pdf": str(out / "a.pdf"),
                "report": str(out / "correction-report.json")}

    monkeypatch.setattr(native, "_call_workflow", workflow)
    monkeypatch.setattr(native.native_files, "cached_file", lambda *_args: manual)
    monkeypatch.setattr(native.native_files, "download_file",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("cached attachment should be reused")))
    second = TickReport()
    native._run_one("drive", tmp_path, ws, work, mock=False, opener=opener,
                    hs_token="hubspot", report=second)
    assert len(calls) == 1
    assert calls[0][1][0].read_bytes() == manual.read_bytes()
    job = json.loads(job_path.read_text("utf-8"))
    assert job["status"] == "verified" and not job.get("missing_attachments")
