# Interior corrections website control

The existing **Automations → Interior corrections** switch controls the paired
Windows laptop. The row shows On or Off only after the computer acknowledges
the current revision. A pending request or stale connection has its own label.
The drawer shows the last connection, queue check/counts, three-hour quiet
period, verified-only delivery policy and daily email schedule/receipts.

Turning off prevents new book work and further uploads. An open InDesign job
finishes locally and keeps its immutable result; an HTTP upload already in
progress can finish. A later On resumes the saved batch without re-editing or
duplicating files. The queue check runs every five minutes. Daily email remains
independent and continues when corrections are off.

The separate **DocProof Interior Website Connection** task sends a small
heartbeat every 15 seconds. The HTTPS response renews a 90-second local lease.
After a connection failure, the lease expires and new book work/uploads pause.
The website marks status stale after 120 seconds and separately flags an email
process that has not checked in for three minutes. Signing out or sleeping
stops the desktop tasks. The next sign-in starts them again.

## Pairing and deployment

Use a dedicated random secret of at least 32 characters. Store it in Fly as
`DOCPROOF_INTERIOR_TOKEN`, and in the Windows user's Credential Manager under
service `docproof-interior-computer`, username equal to the installation's
random device ID. No HubSpot, Google or model credential is shared by this
connection. `watch/interior-remote/config.json` holds only the HTTPS site URL
and device ID. The local `lease.json` is separate from WatchSettings.

Install `tools/install_interior_sync_windows.py --home <worker-home> --install`
in the signed-in Windows desktop after pairing. The worker must run the
matching `remote_control`, `poller`, `native_intake` and `native_corrections`
changes. Restart it only while the native-jobs lock can be acquired. Never
interrupt an active InDesign job just to update the control connection.

The first authenticated heartbeat preserves the laptop's configured on/off
state and binds that one device ID. It disables corrections in the Fly
watcher's own settings to prevent a second worker processing those submissions.
Other automations are unaffected. On/off is the only remotely writable laptop
setting; the drawer hides server-side connection/book controls that cannot
configure that laptop. The original web watcher behavior stays available for
installations without a paired computer.

Only POST `/api/watch/interior-computer` bypasses session login, behind the
dedicated bearer gate. Its strict, size-bounded payload allows counts,
timestamps and digest settings, not source content, attachments, arbitrary
paths or provider error bodies. PUT and the existing `/api/watch` switch need
an administrator session. The computer token grants no other API access.
Revisions are monotonic and an older heartbeat cannot undo an operator change.
HTTPS redirects are refused so credentials cannot move to another host.

No models run and no emails are sent by status synchronization. The daily
email's existing durable outbox remains the sole sender.
