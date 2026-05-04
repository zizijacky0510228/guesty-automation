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

- Only handles property/listing nicknames containing `3505`, `383`, `2171`, or `6550`.
- Ignores out-of-scope properties completely.
- Dry-runs guest replies by default.
- Set `GUESTY_WEBHOOK_SEND_ENABLED=true` only after deployment testing.
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
