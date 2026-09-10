"""Desktop worker entry point that runs only native interior correction jobs."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import threading

from .workflow import save_json


def collect_once(home: Path, *, get_key=None, opener=None):
    """Capture HubSpot form events without refreshing Drive or running a book."""
    from app.settings import get_api_key
    from app.watch import hubspot, native_intake, native_queue
    from app.watch.settings import HUBSPOT_KEY, WatchSettings

    home = Path(home).resolve()
    try:
        ws = WatchSettings.load(home)
        if not ws.corrections_enabled or ws.corrections_engine != "native":
            return {"state": "paused"}
        if not ws.corrections_native_form_poll or not ws.corrections_native_start_after:
            raise ValueError("Configure the correction form and its start date before starting the collector.")
        read = get_key or get_api_key
        token = read(HUBSPOT_KEY)
        if not token:
            raise ValueError("Connect HubSpot before starting the collector.")
        return native_intake.collect(home, ws, token,
                                     opener=opener or hubspot._open_url)
    except Exception as exc:
        # The collector runs in a daemon thread; persist only a bounded type
        # marker so a provider error can never put credentials in local state.
        native_queue.record_error(home, f"{type(exc).__name__}: native collection failed")
        raise


def _collector_loop(home: Path, interval: int, stop: threading.Event,
                    *, get_key=None, opener=None):
    """Keep intake alive while the serialized native worker is busy."""
    delay = max(60, int(interval))
    while not stop.is_set():
        try:
            collect_once(home, get_key=get_key, opener=opener)
        except Exception:
            # collect_once records a sanitized error; the daemon must not take
            # down the worker when HubSpot is unavailable for one poll.
            pass
        if stop.wait(delay):
            return


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
