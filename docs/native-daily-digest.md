# Native corrections daily email

The daily digest runs separately from the InDesign worker. It reads the native queue and delivery receipts, without invoking a model, opening InDesign, uploading files or writing CRM records.

Each email lists newly delivered books with InDesign/spreadsheet links, current review holds, jobs in progress, waiting submissions and current system issues. Completed batches are included until one digest has a confirmed Gmail receipt. Review holds stay visible in subsequent daily summaries until resolved. Quiet days still receive one email.

The configured IANA time zone handles daylight-saving changes. After sleeping through the scheduled time, the laptop sends one catch-up email when it wakes. That counts as the current calendar day's email; missed days are not replayed separately, and the normal scheduled time resumes the following day.

## Windows installation

In the signed-in desktop, with the existing Google connection carrying `gmail.send`:

```powershell
python tools/install_native_digest_windows.py --home C:\path\to\desktop-worker --recipient person@example.com --time 17:00 --timezone America/New_York --install
```

This creates only **DocProof Interior Daily Digest**, using the interactive Windows user. It starts at sign-in and checks the clock once per minute. It does not reinstall, restart or change the live InDesign worker. Without `--install`, it only stages the launcher/task definition and does not enable email.

Settings and delivery receipts live under `watch/daily-digest`. `config.json` contains the enabled flag, single recipient, time, time zone and activation timestamp. `status.json` records the last check and next scheduled time. Set `enabled` to false to stop future mail. Google tokens remain in the existing credential store.

## Duplicate prevention and recovery

A separate process lock and a durable per-day outbox reserve the send before contacting Gmail. A process crash or ambiguous send response never causes a second automatic attempt that day. Check the sender's Sent folder before any manual recovery. The daily digest continues on subsequent days; it never sends a burst of missed daily emails.

An authentication or queue-read failure before the send boundary may retry because no email was attempted. Failures at the send boundary are shown as unconfirmed in `status.json`. Message content excludes raw provider exception bodies, tokens, source document text and correction attachments. Email failure cannot interrupt corrections processing.

The Gmail sender uses the existing send-only authorization and requires a message ID from the [Gmail send endpoint](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/send) before marking delivery confirmed. A running local task cannot send while the PC is asleep, offline or signed out.
