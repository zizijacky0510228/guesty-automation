# Guesty Automation Runbook

## Files

- `OWNER_RULES.md`: owner rules and escalation conditions.
- `data/reply_examples.json`: historical guest-message / host-reply examples.
- `data/reply_style.md`: generated style profile.
- `data/pending_review.md`: latest triage output.

## Regular Check

The Codex thread automation runs this every 5 minutes so guest messages are handled promptly without constant polling cost:

```bash
python3 guesty_automation.py review-new --out data/pending_review.md
```

Then:

- Scope: only process conversations whose property/listing nickname matches tokens `3505`, `383`, `2171`, `6550`, `2030`, or `5553`; ignore all other properties completely.
- For `NEEDS_OWNER_REVIEW`, do not reply to the guest. Tell the owner in Chinese what happened and why it triggered a restriction.
- For `DRAFT_ELIGIBLE`, read `OWNER_RULES.md`, `data/reply_style.md`, and `data/reply_examples.json`, then send a reply in the learned style.
- Ordinary Booking.com/platform questions can be answered directly with conservative platform-guidance language unless the guest asks for a reservation change, refund, early checkout, direct/off-platform booking, unavailable access details, confirmed price, or confirmed availability.
- Before sending, confirm the latest message is still from the guest.
- For `NEEDS_OWNER_REVIEW`, render new unnotified restriction alerts with:

```bash
python3 guesty_automation.py restriction-alerts --out data/restriction_alerts.md
```

Send the rendered body by Gmail to `info@zhanhongltd.com`, then mark those exact alerts as sent:

```bash
python3 guesty_automation.py restriction-alerts --mark-sent --out data/restriction_alerts.md
```

For true instant push instead of 5-minute checks, configure Guesty message webhooks to call a public HTTPS endpoint that runs the same review/send logic.

## Sending

The script is dry-run by default:

```bash
python3 guesty_automation.py send --conversation-id CONVERSATION_ID --body "Reply body"
```

To actually send, `GUESTY_SEND_ENABLED=true` must be set in `.env` and the command must include `--confirm-send`.

## Cleaning Reports

The cleaning report now runs in Render as `guesty-cleaning-cloud-scheduler`.
Render wakes it every 30 minutes, and the script only performs work in the
configured Vancouver-time windows:

```bash
python3 guesty_cleaning_report.py --mode schedule
```

- 20:00: send tomorrow's cleaning report and save the baseline snapshot.
- 10:30: compare today's current cleaning set against the previous 20:00 baseline and send an update only when something changed.

The baseline snapshot is stored in Render Key Value via `CLEANING_STATE_REDIS_URL`.
