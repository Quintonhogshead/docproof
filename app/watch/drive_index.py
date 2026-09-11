"""Persistent metadata inventory for formatting, refreshed from Drive changes.

Only an initial/recovery inventory walks the entire tree. Normal passes read
the change feed; a folder newly moved into scope gets its own subtree scanned.
The checkpoint and inventory are replaced together, only after a complete sync.
Preview uses the same sync in memory and never advances the saved checkpoint.
"""
from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import urllib.error

from . import drive

log = logging.getLogger(__name__)
VERSION = 1
FILENAME = "formatting-drive-index.json"


def _missing(exc):
    return isinstance(exc.__cause__, urllib.error.HTTPError) and exc.__cause__.code == 404


def _walk(token, roots, opener, *, exclude, visited=()):
    pending = deque(roots)
    visited = set(visited) | {exclude}
    with ThreadPoolExecutor(max_workers=4) as pool:
        while pending:
            batch = []
            while pending and len(batch) < 4:
                folder = pending.popleft()
                if folder not in visited:
                    visited.add(folder)
                    batch.append(folder)
            futures = [(folder, pool.submit(drive.list_folder, token, folder,
                                            opener=opener)) for folder in batch]
            for folder, future in futures:
                # An incomplete subtree must never be saved as a complete index.
                listing = future.result()
                for file in listing:
                    if file.id != exclude and not file.trashed:
                        yield replace(file, parents=(folder,))
                        if file.is_folder:
                            pending.append(file.id)


class Inventory:
    def __init__(self, root, exclude, files, cursor, scope, drive_id):
        self.root, self.exclude = root, exclude
        self.files, self.cursor = files, cursor
        self.scope, self.drive_id = scope, drive_id

    def folders(self):
        grouped = defaultdict(list)
        for file in self.files.values():
            if file.id != self.root:
                for parent in file.parents:
                    grouped[parent].append(file)
        # Sorting makes deferred work deterministic across restarts.
        return sorted(grouped.items())

    def save(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": VERSION, "scope": self.scope, "cursor": self.cursor,
                "files": {fid: asdict(file) for fid, file in self.files.items()}}
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(data, stream, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def _load(path, scope):
    try:
        data = json.loads(path.read_text())
        if data["version"] != VERSION or data["scope"] != scope:
            return None
        if not isinstance(data["cursor"], str) or not data["cursor"]:
            return None
        files = {}
        for fid, raw in data["files"].items():
            file = drive.DriveFile(**{**raw, "parents": tuple(raw["parents"])})
            if (file.id != fid or not isinstance(file.name, str)
                    or not isinstance(file.app_properties, dict)
                    or not all(isinstance(parent, str) for parent in file.parents)):
                return None
            files[fid] = file
        return files, data["cursor"]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _apply(token, inventory, changes, opener):
    files = dict(inventory.files)
    old_folders = {fid for fid, file in files.items() if file.is_folder}
    removed = set()
    for change in changes:
        if change.get("changeType") == "drive":
            if change.get("driveId") == inventory.drive_id and change.get("removed"):
                raise drive.DriveError("The watched Shared Drive is no longer accessible.")
            continue
        fid = change.get("fileId")
        if not fid:
            raise drive.DriveError("Google Drive returned a change without a file ID.")
        if change.get("removed"):
            removed.add(fid)
            files.pop(fid, None)
            # If access returns later in this batch, inventory the subtree
            # again: descendants may have changed while it was inaccessible.
            old_folders.discard(fid)
        else:
            raw = change.get("file")
            if not isinstance(raw, dict) or raw.get("id") != fid:
                raise drive.DriveError("Google Drive returned a change without its file details.")
            removed.discard(fid)
            files[fid] = drive.DriveFile.from_api(raw)

    if inventory.root in removed or files[inventory.root].trashed:
        raise drive.DriveError("The watched folder is no longer accessible.")

    membership = {inventory.exclude: False}
    resolving = set()

    def within(fid):
        if fid in removed:
            return False
        if fid in membership:
            return membership[fid]
        if fid in resolving:
            return False
        file = files.get(fid)
        if file is None:
            try:
                file = drive.get_file(token, fid, opener=opener)
            except drive.DriveError as exc:
                if not _missing(exc):
                    raise
                membership[fid] = False
                return False
            files[fid] = file
        resolving.add(fid)
        member = (not file.trashed and
                  (fid == inventory.root or any(within(parent) for parent in file.parents)))
        resolving.remove(fid)
        membership[fid] = member
        return member

    # Resolve the whole batch before pruning: a child event may precede its
    # parent, and a moved folder's unchanged descendants have no own events.
    for fid in list(files):
        within(fid)
    files = {fid: file for fid, file in files.items() if membership.get(fid)}
    new_folders = [fid for fid, file in files.items()
                   if file.is_folder and fid not in old_folders]
    for file in _walk(token, new_folders, opener, exclude=inventory.exclude):
        files[file.id] = file
    inventory.files = files


def sync(home, ws, token, refresh, *, opener, persist=False):
    """Build/read the inventory without downloading or changing any Drive file."""
    root = drive.get_file(token, ws.folder_id, opener=opener)
    if not root.is_folder or root.trashed:
        raise drive.DriveError("The watched folder is unavailable or in the bin.")
    scope = hashlib.sha256(json.dumps([ws.folder_id, ws.archive_folder_id,
                                     ws.client_id, refresh, root.drive_id]).encode()).hexdigest()
    path = Path(home) / FILENAME
    saved = _load(path, scope)
    if saved is not None and ws.folder_id not in saved[0]:
        saved = None
    if saved is not None:
        files, cursor = saved
        files[root.id] = root
        inventory = Inventory(root.id, ws.archive_folder_id, files, cursor, scope, root.drive_id)
        try:
            changes, checkpoint = drive.list_changes(token, cursor, drive_id=root.drive_id,
                                                     opener=opener)
        except drive.InvalidChangeToken:
            saved = None
    if saved is None:
        log.info("Building the initial formatting inventory for %s", root.id)
        # Start BEFORE the walk so uploads/moves during it cannot fall in a gap.
        cursor = drive.start_change_token(token, drive_id=root.drive_id, opener=opener)
        files = {root.id: root}
        for file in _walk(token, [root.id], opener, exclude=ws.archive_folder_id):
            files[file.id] = file
        inventory = Inventory(root.id, ws.archive_folder_id, files, cursor, scope, root.drive_id)
        changes, checkpoint = drive.list_changes(token, cursor, drive_id=root.drive_id,
                                                 opener=opener)
    _apply(token, inventory, changes, opener)
    inventory.cursor = checkpoint
    if persist:
        inventory.save(path)
    log.info("Formatting inventory ready: %d files; %d Drive changes", len(inventory.files), len(changes))
    return inventory


def folder_is_current(token, folder, ws, *, opener):
    """Recheck location just before delivery, including moves of an ancestor."""
    visited = {ws.archive_folder_id}
    pending = [folder]
    while pending:
        fid = pending.pop()
        if fid in visited:
            continue
        visited.add(fid)
        try:
            file = drive.get_file(token, fid, opener=opener)
        except drive.DriveError as exc:
            if _missing(exc):
                return False
            raise
        if file.is_folder and not file.trashed:
            if fid == ws.folder_id:
                return True
            pending.extend(file.parents)
    return False
