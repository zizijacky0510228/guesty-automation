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
import smtplib
import ssl
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from guesty_automation import (
    DATA_DIR,
    GuestyClient,
    GuestyError,
    ROOT,
    allowed_property_tokens,
    assert_guest_is_waiting,
    deep_get,
    env_bool,
    escalation_reasons,
    is_guest_post,
    is_host_post,
    load_dotenv,
    matches_scope_text,
    post_body,
    post_created_at,
    post_sender,
    property_scope_text,
    reservation_property_scope_text,
    sanitize_send_module,
    sort_posts,
    strip_html,
)


EVENT_LOG = DATA_DIR / "webhook_events.jsonl"
PROCESSED_EVENTS = DATA_DIR / "webhook_processed_events.json"
WEBHOOK_ALERTS = DATA_DIR / "webhook_restriction_alerts.md"
WEBHOOK_EMAIL_LOG = DATA_DIR / "webhook_alert_emails.jsonl"
DEFAULT_ALERT_EMAIL_TO = "info@zhanhongltd.com"
APP_VERSION = "webhook-ai-review-2026-05-04"
OWNER_RULES_PATH = ROOT / "OWNER_RULES.md"
REPLY_STYLE_PATH = DATA_DIR / "reply_style.md"
REPLY_EXAMPLES_PATH = DATA_DIR / "reply_examples.json"
AI_SOFT_ESCALATION_REASONS = {"property_detail_or_setup", "unclear_or_unsupported"}
DEFAULT_AI_MODEL = "gpt-5.4-nano"


def env_int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


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


def normalized_body(body: str) -> str:
    return " ".join(body.lower().replace("\n", " ").split())


def hard_escalation_reasons(body: str) -> list[str]:
    return [reason for reason in escalation_reasons(body) if reason not in AI_SOFT_ESCALATION_REASONS]


def is_question_or_request(body: str) -> bool:
    lowered = normalized_body(body)
    request_markers = [
        "?",
        "can i",
        "can we",
        "could i",
        "could we",
        "would it be possible",
        "is it possible",
        "i would like to ask",
        "i'd like to request",
        "i would like to request",
        "please prepare",
        "will we be able",
        "do you",
        "where",
        "what",
        "when",
        "how",
        "is there",
        "are there",
        "available",
        "availability",
        "price",
        "fee",
        "code",
        "password",
        "address",
        "parking",
        "driveway",
        "street parking",
        "luggage",
        "bags",
        "drop off",
        "leave our",
        "check-in at",
        "check in at",
        "early check",
        "washer",
        "dryer",
        "two rooms",
        "prepare the",
        "separate rooms",
    ]
    return any(marker in lowered for marker in request_markers)


def simple_reply_for(body: str) -> str | None:
    lowered = normalized_body(body)
    if is_question_or_request(body):
        return None
    if "thank" in lowered or "thanks" in lowered:
        return "Our pleasure and thank you for the update!"
    if "going to" in lowered or "on my way" in lowered:
        return "Thank you for the update! Safe travels, and please feel free to reach out if you need any assistance."
    if "ok" in lowered or "okay" in lowered:
        return "Thank you!"
    if lowered in {"got it", "sounds good", "all good", "perfect"}:
        return "Thank you!"
    return None


def uncertainty_reasons(body: str) -> list[str]:
    if is_question_or_request(body):
        return ["unsupported_answer"]
    if len(body.split()) > 25:
        return ["unsupported_answer"]
    return []


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return default


def historical_reply_examples(limit: int | None = None) -> list[dict[str, str]]:
    if limit is None:
        limit = env_int("GUESTY_AI_EXAMPLE_LIMIT", 3, 0, 12)
    if limit <= 0:
        return []
    if not REPLY_EXAMPLES_PATH.exists():
        return []
    try:
        data = json.loads(REPLY_EXAMPLES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    examples = data.get("examples") if isinstance(data, dict) else None
    if not isinstance(examples, list):
        return []
    rows = []
    for item in examples[-limit:]:
        if not isinstance(item, dict):
            continue
        guest = str(item.get("guestMessage") or "").strip()
        host = str(item.get("hostReply") or "").strip()
        max_chars = env_int("GUESTY_AI_EXAMPLE_CHARS", 280, 80, 800)
        if guest and host:
            rows.append({"guest": guest[:max_chars], "host": host[:max_chars]})
    return rows


def conversation_history(client: GuestyClient | None, cid: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if client is not None and cid:
        try:
            posts = sort_posts(client.posts(cid))
        except GuestyError:
            posts = []
        post_limit = env_int("GUESTY_AI_CONTEXT_POST_LIMIT", 8, 2, 24)
        body_chars = env_int("GUESTY_AI_HISTORY_BODY_CHARS", 700, 160, 1600)
        for post in posts[-post_limit:]:
            body = post_body(post)
            if not body:
                continue
            if is_guest_post(post):
                sender = "guest"
            elif is_host_post(post):
                sender = "host"
            else:
                sender = post_sender(post)
            rows.append(
                {
                    "createdAt": post_created_at(post),
                    "sender": sender,
                    "body": body[:body_chars],
                }
            )
    if not rows:
        rows.append(
            {
                "createdAt": str(deep_get(payload, "message.createdAt") or ""),
                "sender": "guest",
                "body": message_body(payload)[: env_int("GUESTY_AI_HISTORY_BODY_CHARS", 700, 160, 1600)],
            }
        )
    return rows


def ai_reply_enabled() -> bool:
    return env_bool("GUESTY_AI_REPLY_ENABLED", False)


def openai_model() -> str:
    return os.getenv("OPENAI_MODEL", DEFAULT_AI_MODEL).strip() or DEFAULT_AI_MODEL


def ai_min_confidence() -> float:
    raw = os.getenv("GUESTY_AI_MIN_CONFIDENCE", "0.78").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.78


def openai_configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def openai_chat_json(messages: list[dict[str, str]]) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = openai_model()
    if not api_key:
        raise GuestyError("Missing OPENAI_API_KEY for AI guest reply generation.")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,
        "max_completion_tokens": env_int("OPENAI_MAX_COMPLETION_TOKENS", 350, 80, 1000),
        "response_format": {"type": "json_object"},
    }
    reasoning_effort = os.getenv("OPENAI_REASONING_EFFORT", "").strip()
    if reasoning_effort:
        payload["reasoning_effort"] = reasoning_effort
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=os.getenv("OPENAI_CHAT_URL", "https://api.openai.com/v1/chat/completions"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=body,
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise GuestyError(f"OpenAI API HTTP {exc.code}: {raw}") from exc
    except urllib.error.URLError as exc:
        raise GuestyError(f"OpenAI API request failed: {exc.reason}") from exc

    content = deep_get(data, "choices.0.message.content")
    if not isinstance(content, str) or not content.strip():
        raise GuestyError("OpenAI API returned no message content.")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise GuestyError(f"OpenAI API returned invalid JSON: {content[:300]}") from exc
    if not isinstance(parsed, dict):
        raise GuestyError("OpenAI API JSON response must be an object.")
    return parsed


def ai_reply_decision(
    client: GuestyClient | None,
    payload: dict[str, Any],
    cid: str,
    body: str,
    preliminary_reasons: list[str],
) -> dict[str, Any]:
    if not ai_reply_enabled():
        reply = simple_reply_for(body)
        if reply:
            return {"action": "send", "reply": reply, "confidence": 0.95, "reasons": []}
        return {
            "action": "email_owner",
            "reply": "",
            "confidence": 1,
            "reasons": preliminary_reasons or uncertainty_reasons(body) or ["unsupported_answer"],
            "ownerSummary": "Message needs owner review because the safe local template could not answer it.",
        }

    if not openai_configured():
        return {
            "action": "email_owner",
            "reply": "",
            "confidence": 1,
            "reasons": ["ai_not_configured"],
            "ownerSummary": "AI reply generation is enabled but OPENAI_API_KEY is missing.",
        }

    latest_chars = env_int("GUESTY_AI_LATEST_MESSAGE_CHARS", 900, 200, 2000)
    context = {
        "propertyScopeTokens": allowed_property_tokens(),
        "reservationId": reservation_id(payload),
        "conversationId": cid,
        "latestGuestMessage": body[:latest_chars],
        "preliminaryRestrictionReasons": preliminary_reasons,
        "conversationHistory": conversation_history(client, cid, payload),
        "ownerRules": read_text(OWNER_RULES_PATH)[: env_int("GUESTY_AI_OWNER_RULES_CHARS", 3200, 800, 8000)],
        "replyStyle": read_text(REPLY_STYLE_PATH)[: env_int("GUESTY_AI_REPLY_STYLE_CHARS", 1400, 0, 4000)],
        "historicalReplyExamples": historical_reply_examples(),
    }
    system_prompt = (
        "You are a careful short-term-rental guest messaging assistant. "
        "Read the whole latest guest message and the conversation history before deciding. "
        "Return only JSON with keys: action, reply, confidence, reasons, ownerSummary. "
        "action must be either send or email_owner. "
        "If action is send, reply must directly answer every guest question/request in the latest message, "
        "use the guest's language, match the host's concise friendly style, and avoid unsupported promises. "
        "Only send when the answer is clearly supported by owner rules, conversation history, historical replies, "
        "or universally safe hospitality language. "
        "If any detail is unknown, property-specific, policy-specific, about dates, refunds, compensation, "
        "early check-in/access, luggage drop-off/storage, parking, access codes/passwords, availability, "
        "room setup, safety, damage, legal/medical issues, or off-platform booking, choose email_owner unless "
        "the exact answer is clearly present in the supplied context. "
        "Never answer only with a generic acknowledgement when the guest asked a question. "
        "Keep replies under 90 words unless the guest asked multiple simple questions."
    )
    decision = openai_chat_json(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
        ]
    )
    action = str(decision.get("action") or "").strip()
    reply = str(decision.get("reply") or "").strip()
    try:
        confidence = float(decision.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0
    reasons = decision.get("reasons")
    if not isinstance(reasons, list):
        reasons = []
    clean_reasons = [str(reason) for reason in reasons if str(reason).strip()]
    if action != "send" or not reply or confidence < ai_min_confidence():
        return {
            "action": "email_owner",
            "reply": "",
            "confidence": confidence,
            "reasons": clean_reasons or preliminary_reasons or ["unsupported_answer"],
            "ownerSummary": str(decision.get("ownerSummary") or "AI chose owner review."),
        }
    if "?" in body and len(reply.split()) < 3:
        return {
            "action": "email_owner",
            "reply": "",
            "confidence": confidence,
            "reasons": ["generic_or_incomplete_reply"],
            "ownerSummary": "AI reply looked too generic for a guest question.",
        }
    return {
        "action": "send",
        "reply": reply,
        "confidence": confidence,
        "reasons": clean_reasons,
        "ownerSummary": str(decision.get("ownerSummary") or ""),
    }


def render_alert(payload: dict[str, Any], reasons: list[str], owner_summary: str = "") -> str:
    recommended = owner_summary.strip() or "Please confirm the correct answer, then reply to the guest in Guesty."
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
            recommended,
            "",
        ]
    )


def append_alert(payload: dict[str, Any], reasons: list[str], owner_summary: str = "") -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with WEBHOOK_ALERTS.open("a", encoding="utf-8") as file:
        file.write(render_alert(payload, reasons, owner_summary))
        file.write("\n---\n\n")


def alert_email_to() -> str:
    return os.getenv("GUESTY_ALERT_EMAIL_TO", DEFAULT_ALERT_EMAIL_TO).strip() or DEFAULT_ALERT_EMAIL_TO


def smtp_port() -> int:
    raw = os.getenv("GUESTY_ALERT_SMTP_PORT", "587").strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise GuestyError("GUESTY_ALERT_SMTP_PORT must be a number.") from exc


def send_alert_email(body: str) -> None:
    host = os.getenv("GUESTY_ALERT_SMTP_HOST", "").strip()
    username = os.getenv("GUESTY_ALERT_SMTP_USERNAME", "").strip()
    password = os.getenv("GUESTY_ALERT_SMTP_PASSWORD", "").strip()
    sender = os.getenv("GUESTY_ALERT_EMAIL_FROM", username).strip()
    recipient = alert_email_to()
    subject = os.getenv("GUESTY_ALERT_EMAIL_SUBJECT", "Guesty限制条件消息需要确认").strip()

    missing = [
        name
        for name, value in {
            "GUESTY_ALERT_SMTP_HOST": host,
            "GUESTY_ALERT_SMTP_USERNAME": username,
            "GUESTY_ALERT_SMTP_PASSWORD": password,
            "GUESTY_ALERT_EMAIL_FROM": sender,
        }.items()
        if not value
    ]
    if missing:
        raise GuestyError(f"Missing email configuration for restriction alert: {', '.join(missing)}")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    port = smtp_port()
    use_ssl = env_bool("GUESTY_ALERT_SMTP_SSL", False)
    use_starttls = env_bool("GUESTY_ALERT_SMTP_STARTTLS", not use_ssl)
    context = ssl.create_default_context()

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            if use_starttls:
                smtp.starttls(context=context)
            smtp.login(username, password)
            smtp.send_message(message)

    append_jsonl(
        WEBHOOK_EMAIL_LOG,
        {
            "sentAt": time.time(),
            "to": recipient,
            "subject": subject,
        },
    )


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
    client = GuestyClient() if needs_client else None
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

    cid = conversation_id(payload)
    body = message_body(payload)
    preliminary_reasons = escalation_reasons(body)
    hard_reasons = hard_escalation_reasons(body)
    simple_reply = simple_reply_for(body)
    if hard_reasons:
        decision = {
            "action": "email_owner",
            "reply": "",
            "confidence": 1,
            "reasons": hard_reasons,
            "ownerSummary": "Message triggered a hard owner-review restriction, so AI was skipped to save tokens.",
        }
    elif simple_reply:
        decision = {
            "action": "send",
            "reply": simple_reply,
            "confidence": 0.95,
            "reasons": [],
            "ownerSummary": "",
        }
    else:
        if ai_reply_enabled() and openai_configured() and client is None:
            client = GuestyClient()
        decision = ai_reply_decision(client, payload, cid, body, preliminary_reasons)
    action = str(decision.get("action") or "")
    reply = str(decision.get("reply") or "").strip()
    reasons = [str(reason) for reason in decision.get("reasons", []) if str(reason).strip()]
    owner_summary = str(decision.get("ownerSummary") or "").strip()

    if hard_reasons:
        action = "email_owner"
        for reason in hard_reasons:
            if reason not in reasons:
                reasons.append(reason)

    if action != "send":
        if not reasons:
            reasons = ["unsupported_answer"]
        alert_body = render_alert(payload, reasons, owner_summary)
        append_alert(payload, reasons, owner_summary)
        email_status = "disabled"
        if env_bool("GUESTY_ALERT_EMAIL_ENABLED", True):
            send_alert_email(alert_body)
            email_status = "sent"
        processed[signature] = {
            "status": "needs_owner_review",
            "reasons": reasons,
            "email": email_status,
            "confidence": decision.get("confidence"),
            "processedAt": time.time(),
        }
        save_processed_events(processed)
        return {
            "status": "needs_owner_review",
            "reasons": reasons,
            "alertPath": str(WEBHOOK_ALERTS),
            "email": email_status,
            "confidence": decision.get("confidence"),
        }

    if not env_bool("GUESTY_WEBHOOK_SEND_ENABLED", False):
        processed[signature] = {
            "status": "dry_run_reply",
            "reply": reply,
            "confidence": decision.get("confidence"),
            "processedAt": time.time(),
        }
        save_processed_events(processed)
        return {"status": "dry_run_reply", "reply": reply, "confidence": decision.get("confidence")}

    if client is None:
        client = GuestyClient()
    if not cid:
        raise GuestyError("Webhook payload is missing conversation ID.")
    assert_guest_is_waiting(client, cid)
    module = deep_get(payload, "message.module")
    if not isinstance(module, dict):
        module = client.infer_message_module(cid)
    if not isinstance(module, dict):
        raise GuestyError("Could not infer Guesty send module from webhook payload.")

    client.send_message(cid, reply, sanitize_send_module(module))
    processed[signature] = {
        "status": "sent",
        "reply": reply,
        "confidence": decision.get("confidence"),
        "processedAt": time.time(),
    }
    save_processed_events(processed)
    return {"status": "sent", "reply": reply, "confidence": decision.get("confidence")}


class GuestyWebhookHandler(BaseHTTPRequestHandler):
    server_version = "GuestyWebhook/0.1"

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health":
            self.write_json(
                200,
                {
                    "status": "ok",
                    "version": APP_VERSION,
                    "sendEnabled": env_bool("GUESTY_WEBHOOK_SEND_ENABLED", False),
                    "alertEmailEnabled": env_bool("GUESTY_ALERT_EMAIL_ENABLED", True),
                    "aiReplyEnabled": ai_reply_enabled(),
                    "aiConfigured": openai_configured(),
                    "aiModel": openai_model(),
                    "aiContextPostLimit": env_int("GUESTY_AI_CONTEXT_POST_LIMIT", 8, 2, 24),
                    "aiExampleLimit": env_int("GUESTY_AI_EXAMPLE_LIMIT", 3, 0, 12),
                    "openaiMaxCompletionTokens": env_int("OPENAI_MAX_COMPLETION_TOKENS", 350, 80, 1000),
                },
            )
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
