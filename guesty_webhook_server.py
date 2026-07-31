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
import re
import smtplib
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from cleaning_app import handle_cleaning_request
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
    latest_guest_message,
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
APP_VERSION = "webhook-disabled-cleaning-app-2026-07-31"
OWNER_RULES_PATH = ROOT / "OWNER_RULES.md"
APPROVED_ANSWERS_PATH = ROOT / "APPROVED_ANSWERS.md"
REPLY_STYLE_PATH = DATA_DIR / "reply_style.md"
REPLY_EXAMPLES_PATH = DATA_DIR / "reply_examples.json"
AI_SOFT_ESCALATION_REASONS = {"property_detail_or_setup", "unclear_or_unsupported"}
DEFAULT_AI_MODEL = "gpt-5.4-nano"
RECENT_OWNER_EXAMPLE_CACHE: dict[str, Any] = {"expiresAt": 0.0, "rows": []}
RECENT_OWNER_EXAMPLE_LOCK = threading.Lock()
GUESTY_RATE_LIMIT: dict[str, Any] = {"until": 0.0, "message": ""}
GUESTY_RATE_LIMIT_LOCK = threading.Lock()
GUESTY_RETRY_AFTER_RE = re.compile(r"Retry-After\s+(\d+(?:\.\d+)?)s", re.IGNORECASE)
CHECK_IN_REFERENCE_KEYWORDS = (
    "check-in",
    "check in",
    "checkin",
    "self check",
    "access code",
    "entry code",
    "door code",
    "gate code",
    "password",
    "address",
    "floor",
    "room",
    "unit",
    "building",
    "lockbox",
    "wifi",
    "wi-fi",
    "parking",
    "入住",
    "门禁",
    "密码",
    "地址",
    "楼层",
    "房间",
)


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


def guesty_rate_limit_fallback_seconds() -> int:
    return env_int("GUESTY_API_RATE_LIMIT_COOLDOWN_SECONDS", 3600, 60, 86400)


def parse_guesty_retry_after_seconds(message: str) -> float | None:
    match = GUESTY_RETRY_AFTER_RE.search(message)
    if not match:
        return None
    try:
        return max(0.0, float(match.group(1)))
    except ValueError:
        return None


def guesty_rate_limit_status() -> dict[str, Any]:
    now = time.time()
    with GUESTY_RATE_LIMIT_LOCK:
        until = float(GUESTY_RATE_LIMIT.get("until") or 0)
        message = str(GUESTY_RATE_LIMIT.get("message") or "")
    remaining = max(0.0, until - now)
    return {
        "active": remaining > 0,
        "until": until if remaining > 0 else 0,
        "remainingSeconds": int(remaining),
        "message": message if remaining > 0 else "",
    }


def guesty_rate_limit_active() -> bool:
    return bool(guesty_rate_limit_status()["active"])


def set_guesty_rate_limit_from_error(exc: GuestyError, source: str) -> bool:
    message = str(exc)
    if "HTTP 429" not in message and "TOO_MANY_REQUESTS" not in message:
        return False

    seconds = parse_guesty_retry_after_seconds(message)
    if seconds is None or seconds <= 0:
        seconds = float(guesty_rate_limit_fallback_seconds())
    until = time.time() + seconds
    with GUESTY_RATE_LIMIT_LOCK:
        GUESTY_RATE_LIMIT["until"] = max(float(GUESTY_RATE_LIMIT.get("until") or 0), until)
        GUESTY_RATE_LIMIT["message"] = message[:500]
        active_until = float(GUESTY_RATE_LIMIT["until"])
    until_text = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(active_until))
    print(
        f"Guesty API rate-limit cooldown active from {source} until {until_text}; "
        "webhook will not call Guesty during cooldown.",
        file=sys.stderr,
    )
    return True


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
    if lowered in {"👍", "👍🏻", "👍🏼", "👍🏽", "👍🏾", "👍🏿", "👌", "🙏"}:
        return "Thank you!"
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


def safe_fallback_reply(body: str) -> str:
    if is_question_or_request(body):
        return (
            "Thank you for reaching out. We will do our best to assist based on the booking details "
            "and the check-in information provided. Please feel free to let us know if there is anything "
            "specific you would like us to check."
        )
    return "Thank you for the update!"


def is_check_in_reference(body: str) -> bool:
    lowered = body.lower()
    return any(keyword in lowered for keyword in CHECK_IN_REFERENCE_KEYWORDS)


def post_identity(post: dict[str, Any]) -> str:
    value = deep_get(post, "_id", "id", "createdAt")
    if value:
        return str(value)
    return hashlib.sha256(post_body(post).encode("utf-8")).hexdigest()


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


def recent_owner_reply_examples(client: GuestyClient | None, limit: int | None = None) -> list[dict[str, str]]:
    if client is None:
        return []
    if guesty_rate_limit_active():
        return []
    if limit is None:
        limit = env_int("GUESTY_AI_RECENT_OWNER_EXAMPLE_LIMIT", 12, 0, 20)
    if limit <= 0:
        return []

    now = time.time()
    with RECENT_OWNER_EXAMPLE_LOCK:
        if now < float(RECENT_OWNER_EXAMPLE_CACHE.get("expiresAt") or 0):
            cached_rows = RECENT_OWNER_EXAMPLE_CACHE.get("rows")
            if isinstance(cached_rows, list):
                return cached_rows[:limit]

    scan_limit = env_int("GUESTY_AI_RECENT_OWNER_EXAMPLE_SCAN_LIMIT", 100, 5, 200)
    max_chars = env_int("GUESTY_AI_RECENT_OWNER_EXAMPLE_CHARS", 500, 120, 1000)
    rows: list[dict[str, str]] = []

    try:
        conversations = client.conversations(limit=scan_limit, unread_only=False)
    except GuestyError as exc:
        set_guesty_rate_limit_from_error(exc, "recent_owner_reply_examples.conversations")
        return []

    for conversation in conversations:
        scope_text = property_scope_text(conversation)
        if not matches_scope_text(scope_text):
            continue
        conversation_id_value = str(conversation.get("_id") or conversation.get("id") or "")
        if not conversation_id_value:
            continue
        try:
            posts = sort_posts(client.posts(conversation_id_value))
        except GuestyError as exc:
            if set_guesty_rate_limit_from_error(exc, "recent_owner_reply_examples.posts"):
                break
            continue

        last_guest_body = ""
        last_guest_at = ""
        for post in posts:
            body = post_body(post).strip()
            if not body:
                continue
            if is_guest_post(post):
                last_guest_body = body
                last_guest_at = post_created_at(post)
                continue
            if not is_host_post(post) or not last_guest_body:
                continue
            if post.get("isAutomatic") is True:
                last_guest_body = ""
                last_guest_at = ""
                continue
            if len(body) > 900:
                last_guest_body = ""
                last_guest_at = ""
                continue
            rows.append(
                {
                    "conversationId": conversation_id_value,
                    "reservation": str(
                        deep_get(
                            conversation,
                            "reservation.confirmationCode",
                            "reservation._id",
                            "meta.reservations.0.confirmationCode",
                        )
                        or ""
                    ),
                    "propertyScope": scope_text[:120],
                    "guest": last_guest_body[:max_chars],
                    "host": body[:max_chars],
                    "guestCreatedAt": last_guest_at,
                    "hostCreatedAt": post_created_at(post),
                }
            )
            last_guest_body = ""
            last_guest_at = ""

    cache_ttl = env_int("GUESTY_AI_RECENT_OWNER_EXAMPLE_CACHE_SECONDS", 600, 60, 3600)
    with RECENT_OWNER_EXAMPLE_LOCK:
        RECENT_OWNER_EXAMPLE_CACHE["expiresAt"] = now + cache_ttl
        RECENT_OWNER_EXAMPLE_CACHE["rows"] = rows[:limit]
    return rows[:limit]


def conversation_history(client: GuestyClient | None, cid: str, payload: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if client is not None and cid and not guesty_rate_limit_active():
        try:
            posts = sort_posts(client.posts(cid))
        except GuestyError as exc:
            set_guesty_rate_limit_from_error(exc, "conversation_history.posts")
            posts = []
        post_limit = env_int("GUESTY_AI_CONTEXT_POST_LIMIT", 8, 2, 24)
        body_chars = env_int("GUESTY_AI_HISTORY_BODY_CHARS", 700, 160, 1600)
        checkin_limit = env_int("GUESTY_AI_CHECKIN_REFERENCE_LIMIT", 3, 0, 8)
        checkin_body_chars = env_int("GUESTY_AI_CHECKIN_BODY_CHARS", 1400, 300, 2400)
        checkin_posts = [
            post
            for post in posts
            if not is_guest_post(post) and is_check_in_reference(post_body(post))
        ][-checkin_limit:]
        selected_posts = []
        seen_posts: set[str] = set()
        for post in checkin_posts + posts[-post_limit:]:
            key = post_identity(post)
            if key in seen_posts:
                continue
            seen_posts.add(key)
            selected_posts.append(post)
        for post in sort_posts(selected_posts):
            body = post_body(post)
            if not body:
                continue
            if is_guest_post(post):
                sender = "guest"
            elif is_host_post(post):
                sender = "host"
            else:
                sender = post_sender(post)
            limit = checkin_body_chars if is_check_in_reference(body) else body_chars
            rows.append(
                {
                    "createdAt": post_created_at(post),
                    "sender": sender,
                    "body": body[:limit],
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


def webhook_send_enabled() -> bool:
    return env_bool("GUESTY_WEBHOOK_SEND_ENABLED", False)


def webhook_automation_disabled() -> bool:
    return not webhook_send_enabled() and not ai_reply_enabled()


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
            "action": "send",
            "reply": safe_fallback_reply(body),
            "confidence": 0.6,
            "reasons": preliminary_reasons or uncertainty_reasons(body) or ["fallback_reply"],
            "ownerSummary": "AI replies are disabled, so a cautious non-restriction fallback reply was used.",
        }

    if not openai_configured():
        return {
            "action": "send",
            "reply": safe_fallback_reply(body),
            "confidence": 0.55,
            "reasons": ["ai_not_configured"],
            "ownerSummary": "AI reply generation is enabled but OPENAI_API_KEY is missing; used fallback.",
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
        "approvedAnswers": read_text(APPROVED_ANSWERS_PATH)[: env_int("GUESTY_AI_APPROVED_ANSWERS_CHARS", 5000, 1000, 12000)],
        "replyStyle": read_text(REPLY_STYLE_PATH)[: env_int("GUESTY_AI_REPLY_STYLE_CHARS", 1400, 0, 4000)],
        "historicalReplyExamples": historical_reply_examples(),
        "recentOwnerReplyExamples": recent_owner_reply_examples(client),
    }
    system_prompt = (
        "You are a careful short-term-rental guest messaging assistant. "
        "Read the whole latest guest message and the conversation history before deciding. "
        "Return only JSON with keys: action, reply, confidence, reasons, ownerSummary. "
        "action must be either send or email_owner. "
        "Use email_owner only for hard owner-restriction conditions listed in ownerRules or preliminaryRestrictionReasons, "
        "such as date changes, refunds/compensation/payment disputes, early checkout, direct/off-platform booking, "
        "guest requests that require a human to physically handle, coordinate, approve, arrange, repair, clean, deliver supplies, "
        "change rooms, resolve noise/neighbor issues, or send someone to the property, "
        "confirmed availability/price/payment/order status, access codes/passwords when not clearly available, "
        "or safety/damage/legal/medical/serious complaint issues. "
        "If action is send, reply must be in English regardless of the guest's language, directly answer every "
        "guest question/request in the latest message, match the host's concise friendly style, and avoid unsupported promises. "
        "For check-in, address, access code, password, floor, room, Wi-Fi, parking, or arrival-instruction questions, "
        "answer only from the automated check-in information or host messages included in conversationHistory; "
        "do not infer or invent missing details. If the needed detail is not present, choose email_owner. "
        "Never promise to provide, bring, deliver, arrange, prepare, or restock extra towels, linens, bedding, "
        "sheets, pillows, blankets, paper products, toiletries, or other supplies; the owner only provides the "
        "initial set/batch prepared for the current stay. "
        "When declining a guest request that cannot be fulfilled, use a polite apologetic tone and say the host "
        "is sorry for the inconvenience, but do not soften it into a promise, arrangement, delivery, or compensation. "
        "For non-restriction questions, prefer sending a helpful reply using owner rules, conversation history, "
        "recent owner replies, historical replies, approved answers, or universally safe hospitality language. "
        "Approved answers are the highest-priority reusable owner-confirmed knowledge. "
        "Treat every non-automatic host/owner reply in conversationHistory and recentOwnerReplyExamples as owner memory. "
        "For similar future questions, reuse the latest owner-approved substance and wording when it does not conflict "
        "with ownerRules. If owner memory conflicts with older historical examples, follow ownerRules first, then the "
        "latest owner replies, then approved answers and older historical examples. "
        "If a non-restriction detail is not exact, do not invent it; send a cautious service reply instead of "
        "emailing the owner. If the guest is asking the host to do something operational, choose email_owner. "
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
    if action != "send" or not reply:
        return {
            "action": "send",
            "reply": safe_fallback_reply(body),
            "confidence": max(confidence, 0.6),
            "reasons": clean_reasons or preliminary_reasons or ["fallback_reply"],
            "ownerSummary": str(decision.get("ownerSummary") or "AI did not provide a sendable reply; used fallback."),
        }
    if confidence < ai_min_confidence() and not preliminary_reasons:
        return {
            "action": "send",
            "reply": reply,
            "confidence": confidence,
            "reasons": clean_reasons or ["low_confidence_non_restriction"],
            "ownerSummary": str(decision.get("ownerSummary") or "Low-confidence non-restriction reply allowed."),
        }
    if "?" in body and len(reply.split()) < 3:
        return {
            "action": "send",
            "reply": safe_fallback_reply(body),
            "confidence": confidence,
            "reasons": ["generic_or_incomplete_reply"],
            "ownerSummary": "AI reply looked too generic for a guest question; used fallback.",
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


def rate_limit_owner_summary(status: dict[str, Any]) -> str:
    until = float(status.get("until") or 0)
    remaining = int(status.get("remainingSeconds") or 0)
    until_text = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(until)) if until else "unknown"
    return (
        f"Guesty API is rate-limited until {until_text} "
        f"(about {max(1, (remaining + 59) // 60)} minutes remaining). "
        "Automatic reply skipped without making more Guesty API calls. "
        "Please review this message and reply manually in Guesty."
    )


def handle_guesty_rate_limited_payload(
    payload: dict[str, Any],
    signature: str,
    processed: dict[str, Any],
) -> dict[str, Any]:
    status = guesty_rate_limit_status()
    reasons = ["guesty_rate_limited"]
    owner_summary = rate_limit_owner_summary(status)
    alert_body = render_alert(payload, reasons, owner_summary)
    append_alert(payload, reasons, owner_summary)
    email_status = "disabled"
    email_error = ""
    if env_bool("GUESTY_ALERT_EMAIL_ENABLED", True):
        try:
            send_alert_email(alert_body)
            email_status = "sent"
        except GuestyError as exc:
            email_status = "failed"
            email_error = str(exc)
            print(f"Guesty rate-limit alert email failed: {exc}", file=sys.stderr)

    processed[signature] = {
        "status": "guesty_rate_limited",
        "reasons": reasons,
        "email": email_status,
        "emailError": email_error,
        "rateLimitUntil": status.get("until"),
        "processedAt": time.time(),
    }
    save_processed_events(processed)
    return {
        "status": "needs_owner_review",
        "reason": "guesty_rate_limited",
        "reasons": reasons,
        "email": email_status,
        "emailError": email_error,
        "rateLimitUntil": status.get("until"),
        "rateLimitRemainingSeconds": status.get("remainingSeconds"),
    }


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

    if webhook_automation_disabled():
        processed[signature] = {
            "status": "ignored",
            "reason": "webhook_automation_disabled",
            "processedAt": time.time(),
        }
        save_processed_events(processed)
        return {"status": "ignored", "reason": "webhook_automation_disabled"}

    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    payload_scope_text = property_scope_text(conversation)
    if payload_scope_text and not matches_scope_text(payload_scope_text):
        processed[signature] = {"status": "ignored", "reason": "out_of_scope", "processedAt": time.time()}
        save_processed_events(processed)
        return {
            "status": "ignored",
            "reason": "out_of_scope",
            "allowedTokens": allowed_property_tokens(),
            "scopeText": payload_scope_text,
        }

    if guesty_rate_limit_active():
        return handle_guesty_rate_limited_payload(payload, signature, processed)

    needs_client = not payload_scope_text
    client = GuestyClient() if needs_client else None
    try:
        scope_text = webhook_scope_text(client, payload)
    except GuestyError as exc:
        if set_guesty_rate_limit_from_error(exc, "webhook_scope_text"):
            return handle_guesty_rate_limited_payload(payload, signature, processed)
        raise
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

    if not webhook_send_enabled():
        processed[signature] = {
            "status": "dry_run_reply",
            "reply": reply,
            "confidence": decision.get("confidence"),
            "processedAt": time.time(),
        }
        save_processed_events(processed)
        return {"status": "dry_run_reply", "reply": reply, "confidence": decision.get("confidence")}

    if guesty_rate_limit_active():
        return handle_guesty_rate_limited_payload(payload, signature, processed)

    try:
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
    except GuestyError as exc:
        if set_guesty_rate_limit_from_error(exc, "webhook_send_reply"):
            return handle_guesty_rate_limited_payload(payload, signature, processed)
        raise

    processed[signature] = {
        "status": "sent",
        "reply": reply,
        "confidence": decision.get("confidence"),
        "processedAt": time.time(),
    }
    save_processed_events(processed)
    return {"status": "sent", "reply": reply, "confidence": decision.get("confidence")}


def reservation_id_from_conversation(conversation: dict[str, Any]) -> str:
    value = deep_get(
        conversation,
        "reservation._id",
        "reservation.id",
        "meta.reservations.0._id",
        "meta.reservations.0.id",
    )
    return str(value or "")


def backstop_payload(conversation: dict[str, Any], latest_guest: dict[str, Any]) -> dict[str, Any]:
    message = dict(latest_guest)
    if not message.get("body"):
        message["body"] = post_body(latest_guest)
    if not message.get("type"):
        message["type"] = "fromGuest"
    reservation_value = (
        deep_get(message, "module.reservationId", "reservationId")
        or reservation_id_from_conversation(conversation)
    )
    return {
        "event": "reservation.messageReceived",
        "conversation": conversation,
        "message": message,
        "reservationId": str(reservation_value or ""),
        "source": "backstop",
    }


def process_backstop_once() -> dict[str, Any]:
    if guesty_rate_limit_active():
        return {"checked": 0, "handled": [], "errors": [], "skipped": "guesty_rate_limited"}

    client = GuestyClient()
    limit = env_int("GUESTY_BACKSTOP_CONVERSATION_LIMIT", 50, 5, 300)
    checked = 0
    handled: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    try:
        conversations = client.conversations(limit=limit, unread_only=False)
    except GuestyError as exc:
        if set_guesty_rate_limit_from_error(exc, "backstop.conversations"):
            return {"checked": 0, "handled": [], "errors": [], "skipped": "guesty_rate_limited"}
        raise

    for conversation in conversations:
        conversation_id_value = str(conversation.get("_id") or conversation.get("id") or "")
        if not conversation_id_value:
            continue
        try:
            posts = client.posts(conversation_id_value)
            latest_guest = latest_guest_message(posts)
            if not latest_guest:
                continue
            checked += 1
            payload = backstop_payload(conversation, latest_guest)
            result = process_payload(payload)
            if result.get("status") not in {"ignored"}:
                handled.append(
                    {
                        "conversationId": conversation_id_value,
                        "status": str(result.get("status")),
                        "reservationId": str(payload.get("reservationId") or ""),
                    }
                )
        except Exception as exc:  # noqa: BLE001 - keep one bad conversation from stopping the sweep
            if isinstance(exc, GuestyError) and set_guesty_rate_limit_from_error(exc, "backstop.posts"):
                errors.append({"conversationId": conversation_id_value, "error": str(exc)})
                break
            errors.append({"conversationId": conversation_id_value, "error": str(exc)})

    return {"checked": checked, "handled": handled, "errors": errors}


def start_backstop_thread() -> None:
    if not env_bool("GUESTY_BACKSTOP_ENABLED", False):
        print("Guesty webhook backstop sweep disabled.")
        return

    interval = env_int("GUESTY_BACKSTOP_INTERVAL_SECONDS", 300, 60, 86400)
    start_delay = env_int("GUESTY_BACKSTOP_START_DELAY_SECONDS", 20, 0, 600)

    def run_loop() -> None:
        if start_delay:
            time.sleep(start_delay)
        while True:
            try:
                result = process_backstop_once()
                if result["handled"] or result["errors"]:
                    print(f"Guesty webhook backstop sweep: {json.dumps(result, ensure_ascii=False)}")
            except Exception as exc:  # noqa: BLE001 - long-running service should keep serving webhooks
                print(f"Guesty webhook backstop sweep failed: {exc}")
            time.sleep(interval)

    thread = threading.Thread(target=run_loop, name="guesty-backstop", daemon=True)
    thread.start()
    print(f"Guesty webhook backstop sweep enabled every {interval} seconds.")


class GuestyWebhookHandler(BaseHTTPRequestHandler):
    server_version = "GuestyWebhook/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        cleaning_response = handle_cleaning_request("GET", self.path, self.request_headers())
        if cleaning_response is not None:
            self.write_response(
                cleaning_response.status,
                cleaning_response.body,
                cleaning_response.content_type,
                cleaning_response.headers,
            )
            return

        if path == "/health":
            rate_limit_status = guesty_rate_limit_status()
            self.write_json(
                200,
                {
                    "status": "ok",
                    "version": APP_VERSION,
                    "cleaningAppEnabled": True,
                    "sendEnabled": webhook_send_enabled(),
                    "alertEmailEnabled": env_bool("GUESTY_ALERT_EMAIL_ENABLED", True),
                    "aiReplyEnabled": ai_reply_enabled(),
                    "webhookAutomationDisabled": webhook_automation_disabled(),
                    "aiConfigured": openai_configured(),
                    "aiModel": openai_model(),
                    "propertyScopeTokens": allowed_property_tokens(),
                    "aiContextPostLimit": env_int("GUESTY_AI_CONTEXT_POST_LIMIT", 8, 2, 24),
                    "aiCheckInReferenceLimit": env_int("GUESTY_AI_CHECKIN_REFERENCE_LIMIT", 3, 0, 8),
                    "aiExampleLimit": env_int("GUESTY_AI_EXAMPLE_LIMIT", 3, 0, 12),
                    "openaiMaxCompletionTokens": env_int("OPENAI_MAX_COMPLETION_TOKENS", 350, 80, 1000),
                    "backstopEnabled": env_bool("GUESTY_BACKSTOP_ENABLED", False),
                    "backstopIntervalSeconds": env_int("GUESTY_BACKSTOP_INTERVAL_SECONDS", 300, 60, 86400),
                    "backstopConversationLimit": env_int("GUESTY_BACKSTOP_CONVERSATION_LIMIT", 50, 5, 300),
                    "recentOwnerExampleLimit": env_int("GUESTY_AI_RECENT_OWNER_EXAMPLE_LIMIT", 12, 0, 20),
                    "recentOwnerExampleScanLimit": env_int("GUESTY_AI_RECENT_OWNER_EXAMPLE_SCAN_LIMIT", 100, 5, 200),
                    "guestyRateLimit": rate_limit_status,
                    "aiEscalationMode": "hard_restrictions_only",
                    "humanActionRequestsEscalate": True,
                },
            )
            return
        self.write_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/cleaning/"):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            response = handle_cleaning_request("POST", self.path, self.request_headers(), raw)
            if response is None:
                self.write_json(404, {"error": "not_found"})
                return
            self.write_response(
                response.status,
                response.body,
                response.content_type,
                response.headers,
            )
            return

        if path != "/webhooks/guesty/messages":
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
        self.write_response(status, encoded, "application/json; charset=utf-8")

    def write_response(
        self,
        status: int,
        body: bytes,
        content_type: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def request_headers(self) -> dict[str, str]:
        return {key.lower(): value for key, value in self.headers.items()}

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
    start_backstop_thread()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
