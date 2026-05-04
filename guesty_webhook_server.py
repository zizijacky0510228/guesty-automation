#!/usr/bin/env python3
"""Local Guesty message webhook receiver.

This is intentionally small and dependency-free so it can run locally or on a
simple HTTPS host. It dry-runs by default. Set GUESTY_WEBHOOK_SEND_ENABLED=true
only after deploying and testing the public endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from guesty_automation import (
    DATA_DIR,
    GuestyClient,
    GuestyError,
    allowed_property_tokens,
    deep_get,
    env_bool,
    escalation_reasons,
    is_guest_post,
    load_dotenv,
    matches_scope_text,
    post_body,
    property_scope_text,
    reservation_property_scope_text,
    sanitize_send_module,
    strip_html,
)


EVENT_LOG = DATA_DIR / "webhook_events.jsonl"
PROCESSED_EVENTS = DATA_DIR / "webhook_processed_events.json"
WEBHOOK_ALERTS = DATA_DIR / "webhook_restriction_alerts.md"


def load_processed_events() -> dict[str, Any]:
    if not PROCESSED_EVENTS.exists():
        return {}
    try:
        return json.loads(PROCESSED_EVENTS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_processed_events(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    PROCESSED_EVENTS.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def event_signature(payload: dict[str, Any]) -> str:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    raw = "|".join(
        [
            str(payload.get("event", "")),
            str(payload.get("reservationId", "")),
            str(deep_get(message, "id", "_id", "postId", "createdAt")),
            strip_html(str(message.get("body", "")))[:500],
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def message_body(payload: dict[str, Any]) -> str:
    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    return strip_html(str(message.get("body", "")))


def message_is_from_guest(payload: dict[str, Any]) -> bool:
    message = payload.get("message")
    if not isinstance(message, dict):
        return False
    webhook_type = str(message.get("type", "")).lower().replace("_", "").replace("-", "")
    if webhook_type in {"fromguest", "guest"}:
        return True
    return is_guest_post(message)


def conversation_id(payload: dict[str, Any]) -> str:
    conversation = payload.get("conversation")
    if not isinstance(conversation, dict):
        return ""
    return str(conversation.get("_id") or conversation.get("id") or "")


def reservation_id(payload: dict[str, Any]) -> str:
    value = payload.get("reservationId")
    if value:
        return str(value)
    message = payload.get("message")
    if isinstance(message, dict) and message.get("reservationId"):
        return str(message["reservationId"])
    return ""


def webhook_scope_text(client: GuestyClient | None, payload: dict[str, Any]) -> str:
    conversation = payload.get("conversation")
    scope_text = property_scope_text(conversation if isinstance(conversation, dict) else {})
    if scope_text or client is None:
        return scope_text

    rid = reservation_id(payload)
    if not rid:
        return ""
    reservation = client.reservation(rid, fields="listing.nickname confirmationCode")
    return reservation_property_scope_text(reservation)


def simple_reply_for(body: str) -> str:
    lowered = body.lower()
    if "thank" in lowered or "thanks" in lowered:
        return "Our pleasure and thank you for the update!"
    if "going to" in lowered or "on my way" in lowered:
        return "Thank you for the update! Safe travels, and please feel free to reach out if you need any assistance."
    if "ok" in lowered or "okay" in lowered:
        return "Thank you!"
    return "Thank you for your message. Please feel free to let us know if you need any assistance."


def render_alert(payload: dict[str, Any], reasons: list[str]) -> str:
    return "\n".join(
        [
            "Guesty restriction-condition message needs owner review.",
            "",
            f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
            f"Reservation ID: {reservation_id(payload) or 'unknown'}",
            f"Conversation ID: {conversation_id(payload) or 'unknown'}",
            f"Restriction reason: {', '.join(reasons)}",
            "",
            "Guest message:",
            message_body(payload),
            "",
            "Recommended next step:",
            "Please confirm the correct answer, then reply to the guest in Guesty.",
            "",
        ]
    )


def append_alert(payload: dict[str, Any], reasons: list[str]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with WEBHOOK_ALERTS.open("a", encoding="utf-8") as file:
        file.write(render_alert(payload, reasons))
        file.write("\n---\n\n")


def process_payload(payload: dict[str, Any]) -> dict[str, Any]:
    append_jsonl(EVENT_LOG, {"receivedAt": time.time(), "payload": payload})

    if payload.get("event") != "reservation.messageReceived":
        return {"status": "ignored", "reason": "unsupported_event"}
    if not message_is_from_guest(payload):
        return {"status": "ignored", "reason": "not_guest_message"}

    signature = event_signature(payload)
    processed = load_processed_events()
    if signature in processed:
        return {"status": "ignored", "reason": "duplicate"}

    needs_client = not property_scope_text(payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {})
    client = GuestyClient() if needs_client or env_bool("GUESTY_WEBHOOK_SEND_ENABLED", False) else None
    scope_text = webhook_scope_text(client, payload)
    if not matches_scope_text(scope_text):
        processed[signature] = {"status": "ignored", "reason": "out_of_scope", "processedAt": time.time()}
        save_processed_events(processed)
        return {
            "status": "ignored",
            "reason": "out_of_scope",
            "allowedTokens": allowed_property_tokens(),
            "scopeText": scope_text,
        }

    body = message_body(payload)
    reasons = escalation_reasons(body)
    if reasons:
        append_alert(payload, reasons)
        processed[signature] = {"status": "needs_owner_review", "reasons": reasons, "processedAt": time.time()}
        save_processed_events(processed)
        return {"status": "needs_owner_review", "reasons": reasons, "alertPath": str(WEBHOOK_ALERTS)}

    reply = simple_reply_for(body)
    if not env_bool("GUESTY_WEBHOOK_SEND_ENABLED", False):
        processed[signature] = {"status": "dry_run_reply", "reply": reply, "processedAt": time.time()}
        save_processed_events(processed)
        return {"status": "dry_run_reply", "reply": reply}

    if client is None:
        client = GuestyClient()
    cid = conversation_id(payload)
    if not cid:
        raise GuestyError("Webhook payload is missing conversation ID.")
    module = deep_get(payload, "message.module")
    if not isinstance(module, dict):
        module = client.infer_message_module(cid)
    if not isinstance(module, dict):
        raise GuestyError("Could not infer Guesty send module from webhook payload.")

    client.send_message(cid, reply, sanitize_send_module(module))
    processed[signature] = {"status": "sent", "reply": reply, "processedAt": time.time()}
    save_processed_events(processed)
    return {"status": "sent", "reply": reply}


class GuestyWebhookHandler(BaseHTTPRequestHandler):
    server_version = "GuestyWebhook/0.1"

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health":
            self.write_json(200, {"status": "ok"})
            return
        self.write_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/webhooks/guesty/messages":
            self.write_json(404, {"error": "not_found"})
            return

        if not self.authorized():
            self.write_json(401, {"error": "unauthorized"})
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
            result = process_payload(payload)
            self.write_json(200, result)
        except Exception as exc:  # noqa: BLE001 - return clean webhook response
            self.write_json(500, {"error": str(exc)})

    def authorized(self) -> bool:
        secret = os.getenv("GUESTY_WEBHOOK_SECRET", "").strip()
        if not secret:
            return True
        header_secret = self.headers.get("X-Guesty-Webhook-Secret", "").strip()
        query_secret = parse_qs(urlparse(self.path).query).get("secret", [""])[0].strip()
        return header_secret == secret or query_secret == secret

    def write_json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Run a Guesty message webhook receiver")
    parser.add_argument("--host", default=os.getenv("GUESTY_WEBHOOK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("GUESTY_WEBHOOK_PORT", "8765")))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), GuestyWebhookHandler)
    print(f"Guesty webhook receiver listening on http://{args.host}:{args.port}")
    print("POST /webhooks/guesty/messages")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
