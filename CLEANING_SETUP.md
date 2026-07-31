# Guesty Cleaning Cloud Schedule

This project also runs the cleaning report in Render, so it does not depend on
the Mac being awake or online.

## Schedule

Render runs `guesty-cleaning-cloud-scheduler` only around the Vancouver 20:00
local-time window. Because Render cron uses UTC, the Blueprint schedule covers
both daylight and standard time:

```cron
0 3,4 * * *
```

The cron runs this command:

```bash
python3 guesty_cleaning_report.py --mode schedule
```

The script uses `REPORT_TIMEZONE=America/Vancouver` and still only performs
work during the first 10 minutes of this local-time window:

- `20:00`: generate tomorrow's cleaning report, send it, and save the baseline snapshot for that report date.

The 10:30 comparison is paused with `CLEANING_DELTA_ENABLED=false` to reduce
Guesty API usage and prioritize the 20:00 report.

If Guesty returns a long `Retry-After` rate limit, the cron saves a cooldown
marker in Render Key Value and skips later cleaning runs until that cooldown
expires. This avoids repeated Guesty API calls while the account or cloud IP is
rate-limited.

## State

The 20:00 baseline is saved in Render Key Value through `CLEANING_STATE_REDIS_URL`.
Use a paid Key Value instance for persistence; free Key Value instances do not
persist data to disk.

If `CLEANING_STATE_REDIS_URL` is not configured, the script falls back to local
files under `data/cleaning_state`, which is useful for local tests but is not a
durable cloud setup.

## Cleaning App

The web service also serves a small cleaning task app:

```text
/cleaning/admin?token=ADMIN_TOKEN
/cleaning/admin/cleaners?token=ADMIN_TOKEN
/cleaning/admin/rules?token=ADMIN_TOKEN
/cleaning/worker?cleaner=CLEANER_ID&token=CLEANER_TOKEN
```

Admins can add cleaners, create manual tasks, assign tasks, and edit status or
notes. Each cleaner gets a private worker link generated from their cleaner ID
and token. Worker API responses are filtered on the server, so cleaners only
receive tasks assigned to their own ID.

The admin app has separate pages for task assignments, cleaners, and
auto-assignment rules. Add `lang=en` to any app URL, or use the language button,
to switch between Chinese and English.

Admins can also create address-group auto-assignment rules. For example, a rule
with match text `1348` assigns any new task whose address contains `1348` to the
selected cleaner. Use comma-separated match text such as `1348,1346` when one
cleaner should own a wider group. Rules apply to newly synced/newly created
tasks and can be manually applied to currently unassigned tasks from the admin
page. Existing assigned tasks are not overwritten.

The web service must use the same Render Key Value instance as the cron job:

```text
CLEANING_STATE_REDIS_URL
CLEANING_STATE_KEY_PREFIX=guesty-cleaning
REPORT_TIMEZONE=America/Vancouver
CLEANING_ADMIN_TOKEN
```

If `CLEANING_ADMIN_TOKEN` is not set, the app falls back to `GUESTY_WEBHOOK_SECRET`
for the admin URL token.

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
CLEANING_DELTA_ENABLED=false
CLEANING_SCHEDULE_WINDOW_MINUTES=10
```

After the cloud cron is verified, disable the old local Mac `launchd`/Codex
cleaning automations to avoid duplicate emails.
