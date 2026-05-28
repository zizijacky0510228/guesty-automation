# Guesty Webhook Setup

This project includes a small local receiver:

```bash
python3 guesty_webhook_server.py --host 0.0.0.0 --port 8765
```

Endpoint:

```text
POST /webhooks/guesty/messages
```

Guesty event:

```text
reservation.messageReceived
```

Important behavior:

- Only handles property/listing nicknames containing `3505`, `383`, `2171`, `6550`, `2030`, or `5553`.
- Ignores out-of-scope properties completely.
- Dry-runs guest replies by default.
- With `GUESTY_AI_REPLY_ENABLED=true`, reads the latest guest message, conversation history, owner rules, and any local reply style/examples before deciding whether to send or email the owner.
- Set `GUESTY_WEBHOOK_SEND_ENABLED=true` only after deployment testing.
- Sends restriction-condition emails to `info@zhanhongltd.com` through SMTP.
- Optional shared secret: set `GUESTY_WEBHOOK_SECRET` and send it in `X-Guesty-Webhook-Secret`.

Local test:

```bash
python3 guesty_webhook_server.py --host 127.0.0.1 --port 8765
```

Then POST one of the files in `test_payloads/` to:

```text
http://127.0.0.1:8765/webhooks/guesty/messages
```

For production, deploy it to a public HTTPS host and subscribe the HTTPS URL in Guesty.

Render settings:

```text
Build Command: echo "No build step required"
Start Command: python3 guesty_webhook_server.py --host 0.0.0.0 --port $PORT
```

AI variables required for context-aware replies:

```text
GUESTY_AI_REPLY_ENABLED=true
GUESTY_AI_MIN_CONFIDENCE=0.78
GUESTY_AI_CONTEXT_POST_LIMIT=8
GUESTY_AI_HISTORY_BODY_CHARS=700
GUESTY_AI_EXAMPLE_LIMIT=3
GUESTY_AI_APPROVED_ANSWERS_CHARS=12000
OPENAI_MAX_COMPLETION_TOKENS=350
OPENAI_API_KEY=<OpenAI API key>
OPENAI_MODEL=gpt-5.4-nano
```

SMTP variables required for restriction-condition email alerts:

```text
GUESTY_ALERT_EMAIL_ENABLED=true
GUESTY_ALERT_EMAIL_TO=info@zhanhongltd.com
GUESTY_ALERT_EMAIL_FROM=<sender email>
GUESTY_ALERT_SMTP_HOST=smtp.gmail.com
GUESTY_ALERT_SMTP_PORT=587
GUESTY_ALERT_SMTP_USERNAME=<sender email>
GUESTY_ALERT_SMTP_PASSWORD=<app password or SMTP password>
GUESTY_ALERT_SMTP_STARTTLS=true
```

After deploy, test:

```text
https://guesty-automation.onrender.com/health
```
