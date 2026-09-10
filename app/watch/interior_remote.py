"""One paired corrections computer: bounded status and an acknowledged on/off switch."""
from __future__ import annotations

from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import threading
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

TOKEN_ENV = 'DOCPROOF_INTERIOR_TOKEN'
ROUTE = '/api/watch/interior-computer'
MAX_BYTES = 16384
STALE_SECONDS = 120
_lock = threading.RLock()


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Digest(Record):
    enabled: bool = False
    recipient: str = Field(default='', max_length=254)
    time: str = Field(default='', max_length=5)
    timezone: str = Field(default='', max_length=80)
    state: Literal['disabled', 'scheduled', 'sent', 'delivery_uncertain', 'preparation_failed', 'error', 'unknown'] = 'unknown'
    checked_at: str | None = Field(default=None, max_length=50)
    next_at: str | None = Field(default=None, max_length=50)
    last_sent_at: str | None = Field(default=None, max_length=50)


class Worker(Record):
    state: Literal['paused', 'checking', 'idle', 'error', 'attention'] = 'paused'
    started_at: str | None = Field(default=None, max_length=50)
    finished_at: str | None = Field(default=None, max_length=50)


class Counts(Record):
    waiting: int = Field(default=0, ge=0, le=10000000)
    review: int = Field(default=0, ge=0, le=10000000)
    running: int = Field(default=0, ge=0, le=10000000)
    delivered: int = Field(default=0, ge=0, le=10000000)


class Heartbeat(Record):
    device_id: str = Field(pattern=r'^[A-Za-z0-9_-]{16,80}$')
    applied_revision: int = Field(ge=0)
    enabled: bool
    configured_enabled: bool
    quiet_seconds: int = Field(ge=10800, le=604800)
    auto_upload: bool
    worker: Worker
    digest: Digest
    counts: Counts
    queue_error: bool = False


class Switch(Record):
    enabled: StrictBool


def read(home):
    try:
        value = json.loads((Path(home) / 'interior-computer.json').read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def _save(home, value):
    from docproof.interior.workflow import save_json
    save_json(Path(home) / 'interior-computer.json', value)


def status(home, *, now=None):
    value = read(home)
    if not value:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        age = (now - datetime.fromisoformat(value['received_at'])).total_seconds()
        stale = not 0 <= age <= STALE_SECONDS
    except (ValueError, KeyError, TypeError):
        stale = True
    beat = value['heartbeat']
    desired = value['desired']
    pending = beat['applied_revision'] != desired['revision'] or beat['enabled'] != desired['enabled']
    digest = dict(beat['digest'])
    try:
        digest_age = (now - datetime.fromisoformat(digest['checked_at'])).total_seconds()
        digest['stale'] = not 0 <= digest_age <= 180
    except (ValueError, TypeError):
        digest['stale'] = True
    return {'desired': desired, 'received_at': value['received_at'], 'stale': stale,
            'pending': pending, **{k: v for k, v in beat.items() if k != 'device_id'}, 'digest': digest}


def set_enabled(home, enabled):
    with _lock:
        value = read(home)
        if not value:
            raise HTTPException(409, 'The corrections computer has not connected yet.')
        if value['desired']['enabled'] != enabled:
            value['desired'] = {'enabled': enabled, 'revision': value['desired']['revision'] + 1}
            _save(home, value)
        return value['desired']


def gate(request: Request):
    expected = os.environ.get(TOKEN_ENV, '')
    if len(expected) < 32:
        raise HTTPException(403, 'Corrections computer connection is not configured.')
    scheme, _, token = request.headers.get('authorization', '').partition(' ')
    if scheme.lower() != 'bearer' or not hmac.compare_digest(token, expected):
        raise HTTPException(401, 'Corrections computer authentication failed.')


def register(app, may_manage):
    @app.post(ROUTE, dependencies=[Depends(gate)])
    async def heartbeat(request: Request):
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_BYTES:
                raise HTTPException(413, 'Computer status is too large.')
        try:
            beat = Heartbeat.model_validate_json(raw).model_dump()
        except (ValueError, ValidationError):
            # Pydantic's detailed errors echo inputs. Never echo a supplied secret.
            raise HTTPException(400, 'Invalid corrections computer status.') from None
        with _lock:
            value = read(app.state.watch.home)
            if value and value['heartbeat']['device_id'] != beat['device_id']:
                raise HTTPException(409, 'A different corrections computer is already connected.')
            desired = value.get('desired') or {'enabled': beat['configured_enabled'], 'revision': 1}
            if beat['applied_revision'] > desired['revision']:
                raise HTTPException(409, 'Computer control revision is ahead of the server.')
            if not value:
                # The paired computer owns corrections. The Fly watcher must
                # not process the same submissions using its older settings.
                from .settings import WatchSettings
                ws = WatchSettings.load(app.state.watch.home)
                ws.corrections_enabled = False
                ws.save(app.state.watch.home)
            _save(app.state.watch.home, {'desired': desired, 'heartbeat': beat,
                'received_at': datetime.now(timezone.utc).isoformat()})
            return desired

    @app.put(ROUTE, dependencies=[Depends(may_manage)])
    def control(body: Switch):
        set_enabled(app.state.watch.home, body.enabled)
        return status(app.state.watch.home)
