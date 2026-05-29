# Guesty Cleaning Cloud Schedule

This project also runs the cleaning report in Render, so it does not depend on
the Mac being awake or online.

## Schedule

Render runs `guesty-cleaning-cloud-scheduler` every 30 minutes:

```bash
python3 guesty_cleaning_report.py --mode schedule
```

The script uses `REPORT_TIMEZONE=America/Vancouver` and only performs work in
these local-time windows:

- `20:00`: generate tomorrow's cleaning report, send it, and save the baseline snapshot for that report date.
- `10:30`: generate today's current cleaning set and compare it with the previous 20:00 baseline. If anything changed, send a cleaning update.

The 10:30 comparison reports:

- New cleaning
- Removed cleaning
- New turnover cleaning
- No longer turnover cleaning

## State

The 20:00 baseline is saved in Render Key Value through `CLEANING_STATE_REDIS_URL`.
Use a paid Key Value instance for persistence; free Key Value instances do not
persist data to disk.

If `CLEANING_STATE_REDIS_URL` is not configured, the script falls back to local
files under `data/cleaning_state`, which is useful for local tests but is not a
durable cloud setup.

## Manual Commands

Generate and send tomorrow's baseline report:

```bash
python3 guesty_cleaning_report.py --mode baseline
```

Compare today's current cleaning set against the saved baseline:

```bash
python3 guesty_cleaning_report.py --mode delta
```

Preview without sending:

```bash
python3 guesty_cleaning_report.py --mode baseline --dry-run
python3 guesty_cleaning_report.py --mode delta --dry-run
```

## Render Environment

The cron job reuses Guesty and SMTP credentials from the existing
`guesty-automation` web service. Required values:

```text
GUESTY_CLIENT_ID
GUESTY_CLIENT_SECRET
CLEANING_STATE_REDIS_URL
CLEANING_SEND_EMAIL=true
CLEANING_EMAIL_TO=jacky.s@zhanhongltd.com,info@zhanhongltd.com
CLEANING_SMTP_HOST=smtp.gmail.com
CLEANING_SMTP_PORT=587
CLEANING_SMTP_USERNAME
CLEANING_SMTP_PASSWORD
CLEANING_EMAIL_FROM
CLEANING_SMTP_STARTTLS=true
```

After the cloud cron is verified, disable the old local Mac `launchd`/Codex
cleaning automations to avoid duplicate emails.
