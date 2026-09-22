"""What the Warden looks at, once per tick.

Five sources describe the whole system: Fly (both process groups), DocWatch's
own admin view of itself, HubSpot, the native InDesign worker on the Mini,
and Quinton's inbox. Each is collected by its own function, and each is
failure-isolated — a HubSpot outage must never hide whether Galley is stuck,
so every collector's exception becomes that section's own `"error"` string
rather than an exception that takes the rest of the snapshot down with it.

`collect()` is the only thing `tick.py` calls. `run` and `opener` are
injected exactly the way the rest of this repo injects them (`app/watch/
drive.py`, `app/watch/hubspot.py`): a `subprocess.run`-shaped callable for
the `fly` and `launchctl`/`osascript` commands, and a `urllib`-opener-shaped
callable for HTTP. Neither collector touches the network directly in a test.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("docproof.app.warden.snapshot")

#: Fly's log lines carry ANSI colour codes when read from a terminal-shaped
#: pipe; stripped before anything greps or stores them.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
#: The machine id `fly logs` prints in `<source>[<machine id>]`. Fly's own
#: "source" label (`app`, `proxy`, `runner`, ...) is NOT the process group —
#: a log line for the *agent* machine still says `app[<agent machine id>]`,
#: because that label names the log stream, not who emitted it. The machine
#: id is the only reliable handle, so every line is matched back to its
#: machine via the id->process-group map built from `fly machine list`.
_MACHINE_ID_RE = re.compile(r"\[([0-9a-f]{6,})\]")

#: The two Fly process groups this app runs, and the only two snapshot
#: buckets a machine or a log line can land in.
_GROUPS = ("app", "agent")


def _open_url(request: urllib.request.Request, timeout: int = 30):
    """The one place this module touches HTTP directly. Overridable in tests
    exactly like `app/watch/hubspot.py::_open_url`."""
    return urllib.request.urlopen(request, timeout=timeout)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# Fly
# --------------------------------------------------------------------------

def _fly_run(run, fly_bin: str, args: list[str], *, timeout: int = 30):
    """Run one `fly` subcommand. Returns (stdout, "") or (None, why-it-failed);
    never raises — a `fly` outage is this section's `error`, not a crash."""
    try:
        proc = run([fly_bin, *args], capture_output=True, text=True,
                   timeout=timeout)
    except Exception as e:                                    # noqa: BLE001
        return None, f"could not run `{fly_bin} {' '.join(args)}`: {e}"
    if proc.returncode != 0:
        detail = (getattr(proc, "stderr", "") or getattr(proc, "stdout", "")
                  or f"exit {proc.returncode}").strip()
        return None, f"`{fly_bin} {' '.join(args)}` failed: {detail}"
    return proc.stdout or "", ""


def _process_group(machine: dict) -> str:
    meta = ((machine.get("config") or {}).get("metadata") or {})
    group = str(meta.get("fly_process_group") or "").strip()
    if group in _GROUPS:
        return group
    env_group = str(((machine.get("config") or {}).get("env") or {})
                    .get("FLY_PROCESS_GROUP", "")).strip()
    return env_group if env_group in _GROUPS else "app"


def _machine_checks(machine: dict) -> list[dict]:
    out = []
    for check in machine.get("checks") or []:
        if not isinstance(check, dict):
            continue
        out.append({"name": check.get("name") or check.get("type") or "",
                    "status": check.get("status") or check.get("output") or ""})
    return out


def collect_fly(config, *, run) -> dict:
    """Both process groups' machines, images, releases and recent logs.

    `config` needs `fly_bin` and `fly_app`; `config.thresholds.fly_log_lines`
    (if present) caps how many trailing lines each group's log keeps."""
    fly_bin = getattr(config, "fly_bin", "") or "fly"
    app = getattr(config, "fly_app", "")
    thresholds = getattr(config, "thresholds", None)
    log_lines = getattr(thresholds, "fly_log_lines", 200) if thresholds else 200

    out = {"app": {"machines": [], "image": "", "release": {}},
           "agent": {"machines": [], "image": "", "release": {}},
           "logs": {"app": [], "agent": []}}
    errors = []

    stdout, err = _fly_run(run, fly_bin, ["machine", "list", "-a", app, "--json"])
    id_group: dict[str, str] = {}
    if stdout is None:
        errors.append(err)
    else:
        try:
            machines = json.loads(stdout)
        except (TypeError, ValueError) as e:
            errors.append(f"`fly machine list` returned unreadable JSON: {e}")
            machines = []
        for m in machines if isinstance(machines, list) else []:
            if not isinstance(m, dict):
                continue
            group = _process_group(m)
            mid = str(m.get("id", ""))
            id_group[mid] = group
            bucket = out[group]
            bucket["machines"].append({
                "id": mid, "state": m.get("state", ""),
                "region": m.get("region", ""),
                "updated_at": m.get("updated_at", ""),
                "checks": _machine_checks(m),
            })
            image = (m.get("config") or {}).get("image") or ""
            if image and not bucket["image"]:
                bucket["image"] = image
            meta = ((m.get("config") or {}).get("metadata") or {})
            try:
                version = int(meta.get("fly_release_version"))
            except (TypeError, ValueError):
                version = None
            if version is not None:
                current = bucket["release"].get("version")
                if current is None or version >= current:
                    bucket["release"] = {"version": version,
                                         "created_at": m.get("updated_at", "")}

    stdout, err = _fly_run(run, fly_bin, ["logs", "-a", app, "--no-tail"])
    if stdout is None:
        errors.append(err)
    else:
        for raw_line in stdout.splitlines():
            line = _ANSI_RE.sub("", raw_line)
            match = _MACHINE_ID_RE.search(line)
            group = id_group.get(match.group(1)) if match else None
            if group in out["logs"]:
                out["logs"][group].append(line)
    for group in _GROUPS:
        out["logs"][group] = out["logs"][group][-log_lines:]

    if errors:
        out["error"] = "; ".join(errors)
    return out


# --------------------------------------------------------------------------
# DocWatch
# --------------------------------------------------------------------------

def collect_docwatch(config, *, secrets, opener=_open_url) -> dict:
    """GET `{app_url}/api/watch/warden`, bearer-authenticated with the
    `warden_token` secret. The route (Agent C's `warden_payload`) already
    returns exactly the shape the rest of the snapshot expects, so this is a
    fetch-and-pass-through, not a reshaping."""
    token = secrets.get("warden_token")
    if not token:
        return {"error": "no warden_token configured; cannot reach DocWatch's warden endpoint"}
    url = getattr(config, "app_url", "").rstrip("/") + "/api/watch/warden"
    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with opener(request) as response:
            body = response.read()
    except urllib.error.HTTPError as e:
        return {"error": f"DocWatch answered {e.code} to the warden endpoint."}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {"error": f"could not reach DocWatch: {getattr(e, 'reason', e)}"}
    try:
        payload = json.loads(body)
    except (TypeError, ValueError) as e:
        return {"error": f"DocWatch sent something unreadable: {e}"}
    if not isinstance(payload, dict):
        return {"error": "DocWatch sent an unexpected answer to the warden endpoint."}
    return payload


# --------------------------------------------------------------------------
# HubSpot
# --------------------------------------------------------------------------

def _form_field(row: dict, name: str) -> str:
    for value in row.get("values") or []:
        if isinstance(value, dict) and value.get("name") == name:
            return str(value.get("value", ""))
    # Some submission shapes (e.g. already-flattened test fixtures) carry the
    # field directly rather than under `values`.
    return str(row.get(name, ""))


def collect_hubspot(config, *, secrets, opener=_open_url, docwatch: dict | None = None,
                    journal=None) -> dict:
    """Projects (object `0-970`) touched since the last cursor, plus the
    corrections form's submissions when a form id is on the DocWatch payload.

    Property names come from the DocWatch payload's `watch` dict
    (`hubspot_first_property` etc.), with the fallbacks the brief names."""
    from app.watch import hubspot as hubspotlib

    token = secrets.get("hubspot")
    if not token:
        return {"error": "no hubspot token configured"}

    watch = ((docwatch or {}).get("watch") or {}) if isinstance(docwatch, dict) else {}
    first_prop = watch.get("hubspot_first_property") or "author_first_name"
    last_prop = watch.get("hubspot_last_property") or "author_last_name"
    key_prop = watch.get("hubspot_key_property") or ""
    status_prop = watch.get("hubspot_status_property") or ""

    cursor = journal.get("last_hubspot_cursor") if journal is not None else None
    since_ms = 0
    if cursor:
        try:
            since_ms = int(float(cursor) * 1000)
        except (TypeError, ValueError):
            since_ms = 0

    props = [p for p in (first_prop, last_prop, key_prop, status_prop,
                         "createdate", "hs_lastmodifieddate", "hubspot_owner_id")
             if p]

    projects: list[dict] = []
    error = None
    try:
        after = None
        while True:
            body = {
                "filterGroups": [{"filters": [{
                    "propertyName": "hs_lastmodifieddate", "operator": "GTE",
                    "value": str(since_ms)}]}],
                "properties": props, "limit": 100,
                "sorts": [{"propertyName": "hs_lastmodifieddate",
                          "direction": "ASCENDING"}],
            }
            if after:
                body["after"] = after
            request = hubspotlib._request(
                f"{hubspotlib.API}/crm/v3/objects/0-970/search", token,
                data=json.dumps(body).encode(), method="POST")
            answer = hubspotlib._json_call(request, opener=opener,
                                           what="search the Projects object")
            for raw in answer.get("results") or []:
                rec = hubspotlib.HubSpotRecord.from_api(raw)
                p = rec.properties
                projects.append({
                    "id": rec.id,
                    "created_at": p.get("createdate", ""),
                    "updated_at": p.get("hs_lastmodifieddate", ""),
                    "author_first": p.get(first_prop, ""),
                    "author_last": p.get(last_prop, ""),
                    "book_title": p.get(key_prop, "") if key_prop else "",
                    "created_by": p.get("hubspot_owner_id", ""),
                    "owner": p.get("hubspot_owner_id", ""),
                    "status": p.get(status_prop, "") if status_prop else "",
                })
            after = ((answer.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                break
    except hubspotlib.HubSpotError as e:
        error = str(e)

    result: dict = {"projects": projects}

    form_id = (watch.get("corrections_native_form_id")
              or watch.get("corrections_form_id") or "")
    if form_id:
        try:
            rows = hubspotlib.form_submissions(token, form_id, opener=opener)
            submissions = []
            for row in rows:
                submissions.append({
                    "submitted_at": row.get("submittedAt", ""),
                    "firstname": _form_field(row, "firstname"),
                    "lastname": _form_field(row, "lastname"),
                    "book_title": (_form_field(row, "book")
                                  or _form_field(row, "book_title")),
                    "project_id": _form_field(row, "project_id"),
                })
            result["form_submissions"] = submissions
        except hubspotlib.HubSpotError as e:
            error = f"{error}; {e}" if error else str(e)

    if error:
        result["error"] = error
    return result


# --------------------------------------------------------------------------
# Native (Mini-local InDesign worker)
# --------------------------------------------------------------------------

def _read_json(path: Path):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def _launchctl_status(run, label: str) -> dict:
    uid = os.getuid() if hasattr(os, "getuid") else 0
    try:
        proc = run(["launchctl", "print", f"gui/{uid}/{label}"],
                   capture_output=True, text=True, timeout=10)
    except Exception:                                          # noqa: BLE001
        return {"running": False, "pid": None}
    out = getattr(proc, "stdout", "") or ""
    running = getattr(proc, "returncode", 1) == 0 and "state = running" in out
    match = re.search(r"\bpid\s*=\s*(\d+)", out)
    return {"running": running, "pid": int(match.group(1)) if match else None}


def collect_native(config, *, run) -> dict:
    """The Mini's own InDesign worker: the two login agents, the worker and
    intake receipts, the batch ledger, and an Apple-Events liveness probe.
    Only collected at all when `config.interior_home` names an install."""
    interior_home = getattr(config, "interior_home", "") or ""
    if not interior_home:
        return {"installed": False, "agents": {}, "worker": None, "intake": None,
                "indesign": {"alive": False, "version": "", "error": ""},
                "batches": []}

    watch_home = Path(interior_home) / "watch"
    labels = ("com.docproof.interior-review-worker", "com.docproof.interior-review-ui")
    agents = {label: _launchctl_status(run, label) for label in labels}
    worker = _read_json(watch_home / "native-worker.json")
    intake = _read_json(watch_home / "native-intake.json")

    batches: list[dict] = []
    error = None
    try:
        from app.watch import native_queue
        status = native_queue.status(watch_home)
        for row in status.get("batches") or []:
            batches.append({
                "id": row.get("batch_id", ""),
                "state": row.get("state", ""),
                "book": row.get("project_id", ""),
                "hold_reason": row.get("reason", "") or "",
                "updated_at": row.get("created_at", ""),
            })
    except Exception as e:                                     # noqa: BLE001
        error = f"could not read the native batch ledger: {e}"

    indesign = {"alive": False, "version": "", "error": ""}
    try:
        proc = run(["osascript", "-e",
                   'tell application id "com.adobe.InDesign" to version'],
                   capture_output=True, text=True, timeout=10)
        if getattr(proc, "returncode", 1) == 0:
            indesign = {"alive": True, "version": (proc.stdout or "").strip(),
                       "error": ""}
        else:
            indesign["error"] = (getattr(proc, "stderr", "")
                                 or getattr(proc, "stdout", "")
                                 or "InDesign did not answer.").strip()
    except Exception as e:                                     # noqa: BLE001
        indesign["error"] = str(e)

    result = {"installed": True, "agents": agents, "worker": worker,
              "intake": intake, "indesign": indesign, "batches": batches}
    if error:
        result["error"] = error
    return result


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def collect_email(config, *, secrets, opener=_open_url, now=None) -> dict:
    """Quinton's inbox and the notify mailbox's replies, through
    `app.warden.messaging.gmail` — stubbed to `{"error": ...}` when that
    module (Agent D's) is not there yet, so this section never breaks the
    rest of the snapshot while messaging is still being built."""
    try:
        from app.warden.messaging import gmail
    except ImportError:
        return {"error": "gmail module missing"}

    since = (now or _now_utc()) - timedelta(hours=24)
    result: dict = {}
    errors = []
    try:
        result["inbox"] = list(gmail.inbox(config, secrets, since=since, opener=opener))
    except Exception as e:                                     # noqa: BLE001
        errors.append(f"could not read the inbox: {e}")
        result["inbox"] = []
    try:
        result["replies"] = list(gmail.replies(config, secrets, since=since, opener=opener))
    except Exception as e:                                     # noqa: BLE001
        errors.append(f"could not read replies: {e}")
        result["replies"] = []
    if errors:
        result["error"] = "; ".join(errors)
    return result


# --------------------------------------------------------------------------
# collect() / save()
# --------------------------------------------------------------------------

_EMPTY_JOURNAL_SUMMARY = {"open_findings": [], "open_requests": [],
                          "recent_actions": [], "paused": False,
                          "quiet_until": None, "last_tick_at": None}


def _isolated(name: str, fn) -> dict:
    """Run one collector; any exception it did not already turn into an
    `"error"` string becomes one here instead, so one dead source never takes
    the rest of the snapshot down with it."""
    try:
        result = fn()
    except Exception as e:                                     # noqa: BLE001
        log.warning("the %s collector crashed", name, exc_info=True)
        return {"error": f"the {name} collector crashed: {e}"}
    return result if isinstance(result, dict) else {"error": f"the {name} collector returned something unexpected"}


def collect(config, *, secrets, run, opener=_open_url, now=None, journal=None) -> dict:
    """Everything the Warden knows this tick, one JSON-shaped dict. See
    `docs/monitoring-agent-plan.md` and `WARDEN_BRIEF.md` for the schema."""
    from docproof import __version__ as warden_version

    moment = now or _now_utc()
    snapshot: dict = {"at": _iso(moment), "warden_version": warden_version}

    snapshot["fly"] = _isolated("fly", lambda: collect_fly(config, run=run))
    snapshot["docwatch"] = _isolated(
        "docwatch", lambda: collect_docwatch(config, secrets=secrets, opener=opener))
    snapshot["hubspot"] = _isolated(
        "hubspot", lambda: collect_hubspot(
            config, secrets=secrets, opener=opener,
            docwatch=snapshot["docwatch"], journal=journal))
    snapshot["native"] = _isolated("native", lambda: collect_native(config, run=run))
    snapshot["email"] = _isolated(
        "email", lambda: collect_email(config, secrets=secrets, opener=opener, now=moment))

    if journal is not None:
        try:
            snapshot["journal"] = journal.summary()
        except Exception:                                       # noqa: BLE001
            log.warning("journal.summary() failed", exc_info=True)
            snapshot["journal"] = dict(_EMPTY_JOURNAL_SUMMARY)
    else:
        snapshot["journal"] = dict(_EMPTY_JOURNAL_SUMMARY)

    return snapshot


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".warden-snap-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_TS_SAFE_RE = re.compile(r"[^0-9A-Za-z_-]")


def save(home: str | Path, snapshot: dict, *, keep: int = 200) -> Path:
    """Write `snapshots/latest.json` and a timestamped copy, pruning to the
    last `keep`. Returns the timestamped copy's path."""
    snap_dir = Path(home) / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    text = json.dumps(snapshot, indent=2, ensure_ascii=False)
    at = str(snapshot.get("at") or _iso(_now_utc()))
    stamp = _TS_SAFE_RE.sub("-", at)
    target = snap_dir / f"{stamp}.json"

    _atomic_write(snap_dir / "latest.json", text)
    _atomic_write(target, text)

    files = sorted((p for p in snap_dir.glob("*.json") if p.name != "latest.json"),
                   key=lambda p: p.name)
    for stale in files[:max(0, len(files) - keep)]:
        try:
            stale.unlink()
        except OSError:
            pass

    return target
