"""Mac worker entry point that runs only native interior correction jobs."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .workflow import save_json


def poll_once(home: Path, *, get_key=None, opener=None, local_only=False):
    from app.settings import get_api_key
    from app.watch import drive, native_corrections
    from app.watch.settings import GOOGLE_KEY, HUBSPOT_KEY, WatchSettings
    from app.watch.state import note_tick
    from app.watch.tick import TickReport

    home = Path(home).resolve()
    ws = WatchSettings.load(home)
    if local_only:
        ws.corrections_native_auto_upload = False
        ws.hubspot_write_back = False
    report = TickReport()
    receipt = {"started_at": datetime.now(timezone.utc).isoformat(), "state": "checking",
               "local_only": local_only, "drive_uploads_enabled": ws.corrections_native_auto_upload}
    path = home / "native-worker.json"
    save_json(path, receipt)
    try:
        if not ws.corrections_enabled or ws.corrections_engine != "native":
            receipt["state"] = "paused"
            return report
        if not ws.corrections_native_form_poll or not ws.corrections_native_start_after:
            raise ValueError("Configure the correction form and its start date before starting this worker.")
        if bool(ws.corrections_native_form_book_property) != bool(ws.corrections_native_project_book_property):
            raise ValueError("Configure both the form and Project book-title fields.")
        if not ws.client_id or not ws.client_secret or not ws.folder_id:
            raise ValueError("The Google Drive connection and author folder must be configured.")
        read = get_key or get_api_key
        refresh, hubspot = read(GOOGLE_KEY), read(HUBSPOT_KEY)
        if not refresh or not hubspot:
            raise ValueError("Connect Google Drive and HubSpot before starting this worker.")
        call = opener or drive._open_url
        token = drive.refresh_access_token(ws.client_id, ws.client_secret, refresh, opener=call)
        note_tick(home)
        native_corrections.run_stage(token, home, ws, None, None, None,
                                     mock=False, opener=call, hs_token=hubspot, report=report)
        receipt["state"] = "attention" if report.failed or report.needs_human else "idle"
        return report
    except Exception as exc:
        receipt.update(state="error", error=str(exc))
        raise
    finally:
        receipt.update(finished_at=datetime.now(timezone.utc).isoformat(), report=asdict(report))
        save_json(path, receipt)
