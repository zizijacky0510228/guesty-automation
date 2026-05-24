#!/usr/bin/env python3
"""Small Guesty Open API helper for safe guest-message automation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html import unescape
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
CACHE_DIR = ROOT / ".cache"
TOKEN_CACHE = CACHE_DIR / "guesty_token.json"
DATA_DIR = ROOT / "data"
STYLE_PATH = DATA_DIR / "reply_style.md"
EXAMPLES_PATH = DATA_DIR / "reply_examples.json"
PENDING_REVIEW_PATH = DATA_DIR / "pending_review.md"
RESTRICTION_ALERTS_PATH = DATA_DIR / "restriction_alerts.md"
NOTIFIED_RESTRICTIONS_PATH = DATA_DIR / "notified_restrictions.json"
DEFAULT_ALLOWED_PROPERTY_TOKENS = "3505,383,2171,6550"
DEFAULT_PUBLIC_WEBHOOK_URL = "https://guesty-automation.onrender.com/webhooks/guesty/messages"


class GuestyError(RuntimeError):
    pass


def load_dotenv(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def allowed_property_tokens() -> list[str]:
    raw = os.getenv("GUESTY_ALLOWED_PROPERTY_TOKENS", DEFAULT_ALLOWED_PROPERTY_TOKENS)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise GuestyError(f"Missing required environment variable: {name}")
    return value


def safe_json_loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise GuestyError(f"Invalid JSON in environment value: {exc}") from exc


def url_with_query_secret(url: str, secret: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "secret"]
    query.append(("secret", secret))
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))


def masked_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    masked_query = [
        (key, "***" if key.lower() in {"secret", "token", "key"} else value)
        for key, value in query
    ]
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(masked_query)))


def mask_secrets(value: Any) -> Any:
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    if isinstance(value, dict):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in {"secret", "signingsecret", "signing_secret"}:
                masked[key] = "***"
            elif key.lower() == "url" and isinstance(item, str):
                masked[key] = masked_url(item)
            else:
                masked[key] = mask_secrets(item)
        return masked
    return value


def request_json(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 30,
) -> Any:
    req = urllib.request.Request(url=url, method=method, headers=headers or {}, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise GuestyError(f"Guesty API HTTP {exc.code}: {raw}") from exc
    except urllib.error.URLError as exc:
        raise GuestyError(f"Guesty API request failed: {exc.reason}") from exc


@dataclass
class Token:
    access_token: str
    expires_at: float
    scope: str
    token_type: str

    @property
    def is_fresh(self) -> bool:
        return time.time() < self.expires_at - 30 * 60


class GuestyClient:
    def __init__(self) -> None:
        self.client_id = require_env("GUESTY_CLIENT_ID")
        self.client_secret = require_env("GUESTY_CLIENT_SECRET")
        self.base_url = os.getenv("GUESTY_BASE_URL", "https://open-api.guesty.com/v1").rstrip("/")
        self.token_url = os.getenv("GUESTY_TOKEN_URL", "https://open-api.guesty.com/oauth2/token")
        self._token: Token | None = None

    def token(self) -> Token:
        if self._token and self._token.is_fresh:
            return self._token

        cached = self._read_cached_token()
        if cached and cached.is_fresh:
            self._token = cached
            return cached

        payload = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "scope": "open-api",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        ).encode("utf-8")

        data = request_json(
            "POST",
            self.token_url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=payload,
        )

        token = Token(
            access_token=data["access_token"],
            expires_at=time.time() + int(data.get("expires_in", 86400)),
            scope=data.get("scope", ""),
            token_type=data.get("token_type", "Bearer"),
        )
        self._write_cached_token(token)
        self._token = token
        return token

    def _read_cached_token(self) -> Token | None:
        if not TOKEN_CACHE.exists():
            return None
        try:
            data = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            return Token(**data)
        except (OSError, TypeError, json.JSONDecodeError):
            return None

    def _write_cached_token(self, token: Token) -> None:
        CACHE_DIR.mkdir(exist_ok=True)
        TOKEN_CACHE.write_text(json.dumps(token.__dict__, indent=2), encoding="utf-8")
        try:
            TOKEN_CACHE.chmod(0o600)
        except OSError:
            pass

    def api(self, method: str, path: str, params: dict[str, Any] | None = None, body: Any = None) -> Any:
        token = self.token()
        url = f"{self.base_url}{path}"
        if params:
            clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
            url += "?" + urllib.parse.urlencode(clean_params)

        encoded_body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"{token.token_type} {token.access_token}",
        }
        if body is not None:
            encoded_body = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        return request_json(method, url, headers=headers, body=encoded_body)

    def conversations(self, limit: int, unread_only: bool = True) -> list[dict[str, Any]]:
        filters: list[dict[str, Any]] = [
            {"field": "type", "operator": "$eq", "value": "guest"},
            {"field": "status", "operator": "$eq", "value": "OPEN"},
        ]
        if unread_only:
            filters.append({"field": "read", "operator": "$eq", "value": False})

        fallback_filters = [
            filters,
            [item for item in filters if item["field"] != "read"],
            [{"field": "type", "operator": "$eq", "value": "guest"}],
            [],
        ]
        last_error: GuestyError | None = None
        for candidate_filters in fallback_filters:
            try:
                conversations = self._conversation_query(limit, candidate_filters)
                has_read_filter = any(item.get("field") == "read" for item in candidate_filters)
                if conversations or not (unread_only and has_read_filter):
                    return conversations
            except GuestyError as exc:
                last_error = exc
        if last_error:
            raise last_error
        return []

    def _conversation_query(self, limit: int, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
        base_params: dict[str, Any] = {
            "sort": "-modifiedAt",
            "limit": limit,
        }
        if filters:
            base_params["filters"] = json.dumps(filters, separators=(",", ":"))

        field_sets = [
            "_id guest reservation status state assignee priority createdAt modifiedAt meta read",
            None,
        ]
        last_error: GuestyError | None = None
        for fields in field_sets:
            params = dict(base_params)
            if fields:
                params["fields"] = fields
            try:
                data = self.api("GET", "/communication/conversations", params=params)
                return extract_items(data)
            except GuestyError as exc:
                last_error = exc
        if last_error:
            raise last_error
        return []

    def posts(self, conversation_id: str) -> list[dict[str, Any]]:
        data = self.api("GET", f"/communication/conversations/{conversation_id}/posts")
        return extract_items(data)

    def reservation(self, reservation_id: str, fields: str | None = None) -> dict[str, Any]:
        params = {"fields": fields} if fields else None
        data = self.api("GET", f"/reservations/{reservation_id}", params=params)
        return data if isinstance(data, dict) else {}

    def send_message(self, conversation_id: str, body: str, module: dict[str, Any]) -> Any:
        return self.api(
            "POST",
            f"/communication/conversations/{conversation_id}/send-message",
            body={"module": module, "body": body},
        )

    def webhooks(self) -> list[dict[str, Any]]:
        return extract_items(self.api("GET", "/webhooks"))

    def create_webhook(self, url: str, events: list[str]) -> Any:
        return self.api("POST", "/webhooks", body={"url": url, "events": events})

    def delete_webhook(self, webhook_id: str) -> Any:
        return self.api("DELETE", f"/webhooks/{webhook_id}")

    def infer_message_module(self, conversation_id: str) -> dict[str, Any] | None:
        for post in reversed(sort_posts(self.posts(conversation_id))):
            sample = module_sample(post)
            if isinstance(sample, dict) and sample.get("type"):
                return sample
        return None


def extract_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("data", "items", "results", "conversations", "posts"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_items(value)
            if nested:
                return nested
    return []


def deep_get(data: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        current: Any = data
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
                current = current[int(part)]
            else:
                current = None
                break
        if current not in (None, "", []):
            return current
    return None


def strip_html(text: str) -> str:
    out = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    pieces: list[str] = []
    in_tag = False
    for char in out:
        if char == "<":
            in_tag = True
        elif char == ">":
            in_tag = False
        elif not in_tag:
            pieces.append(char)
    return " ".join(unescape("".join(pieces)).split())


def post_body(post: dict[str, Any]) -> str:
    value = deep_get(post, "body", "message", "text", "content", "payload.body", "payload.text")
    if isinstance(value, dict):
        value = deep_get(value, "body", "text", "html")
    if value is None:
        return ""
    return strip_html(str(value))


def searchable_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True).lower()
    return str(value or "").lower()


def matches_property_scope(conversation: dict[str, Any], posts: list[dict[str, Any]]) -> bool:
    tokens = allowed_property_tokens()
    if not tokens:
        return True
    haystack = property_scope_text(conversation)
    return any(token in haystack for token in tokens)


def property_scope_text(conversation: dict[str, Any]) -> str:
    parts = [
        deep_get(conversation, "listing.nickname"),
        deep_get(conversation, "reservation.listing.nickname"),
        deep_get(conversation, "meta.listing.nickname"),
        deep_get(conversation, "meta.reservations.0.listing.nickname"),
    ]
    return "\n".join(searchable_text(part) for part in parts if part not in (None, "", []))


def reservation_property_scope_text(reservation: dict[str, Any]) -> str:
    parts = [
        deep_get(reservation, "listing.nickname"),
        deep_get(reservation, "listing.nickname.nickname"),
    ]
    return "\n".join(searchable_text(part) for part in parts if part not in (None, "", []))


def matches_scope_text(scope_text: str) -> bool:
    tokens = allowed_property_tokens()
    if not tokens:
        return True
    return any(token in scope_text.lower() for token in tokens)


def property_scope_debug(conversation: dict[str, Any]) -> dict[str, Any]:
    text = property_scope_text(conversation)
    return {
        "conversationId": str(conversation.get("_id") or conversation.get("id") or ""),
        "reservation": reservation_label(conversation),
        "scopeText": text[:1000],
        "matchedTokens": [token for token in allowed_property_tokens() if token in text],
        "metaKeys": sorted(conversation.get("meta", {}).keys()) if isinstance(conversation.get("meta"), dict) else [],
    }


def guest_name(conversation: dict[str, Any]) -> str:
    value = deep_get(
        conversation,
        "guest.fullName",
        "guest.name",
        "guest.firstName",
        "meta.guestName",
        "reservation.guest.fullName",
    )
    return str(value or "Unknown guest")


def reservation_label(conversation: dict[str, Any]) -> str:
    value = deep_get(
        conversation,
        "reservation.confirmationCode",
        "reservation._id",
        "meta.reservations.0.confirmationCode",
    )
    return str(value or "No reservation label")


def post_sender(post: dict[str, Any]) -> str:
    value = deep_get(post, "sentBy", "sender", "type", "from")
    if isinstance(value, dict):
        value = deep_get(value, "name", "type", "_id")
    return str(value or "unknown")


def post_created_at(post: dict[str, Any]) -> str:
    value = deep_get(post, "createdAt", "sentAt", "date", "timestamp")
    return str(value or "")


def normalize_sender(sender: str) -> str:
    return sender.strip().lower().replace("_", "").replace("-", "")


def is_host_post(post: dict[str, Any]) -> bool:
    sender = normalize_sender(post_sender(post))
    return sender in {"host", "user", "fromhost", "fromguesty"} or "host" in sender


def is_guest_post(post: dict[str, Any]) -> bool:
    sender = normalize_sender(post_sender(post))
    return sender in {"guest", "fromguest", "fromthirdparty"} or "guest" in sender


def module_sample(post: dict[str, Any]) -> Any:
    return deep_get(post, "module", "payload.module", "sentVia", "communicationModule")


def sanitize_send_module(module: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {"type", "reservationId", "to", "cc", "bcc"}
    cleaned = {key: value for key, value in module.items() if key in allowed_keys}
    if "type" not in cleaned:
        raise GuestyError("Inferred message module is missing required type.")
    return cleaned


def conversation_status(conversation: dict[str, Any]) -> str:
    value = conversation.get("status") or deep_get(conversation, "state.status")
    return str(value or "unknown")


def conversation_read(conversation: dict[str, Any]) -> str:
    value = conversation.get("read")
    if value is None:
        value = deep_get(conversation, "state.read")
    if value is None:
        return "unknown"
    return str(value)


def sort_posts(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(posts, key=lambda post: post_created_at(post) or "")


def collect_reply_examples(client: GuestyClient, limit: int) -> dict[str, Any]:
    conversations = client.conversations(limit=limit, unread_only=False)
    examples: list[dict[str, Any]] = []
    modules: list[Any] = []

    for conversation in conversations:
        conversation_id = str(conversation.get("_id") or conversation.get("id") or "")
        if not conversation_id:
            continue
        posts = sort_posts(client.posts(conversation_id))
        last_guest_message = ""
        for post in posts:
            body = post_body(post)
            if not body:
                continue
            if is_guest_post(post):
                last_guest_message = body
            elif is_host_post(post):
                sample = module_sample(post)
                if isinstance(sample, dict) and sample not in modules:
                    modules.append(sample)
                if last_guest_message:
                    examples.append(
                        {
                            "conversationId": conversation_id,
                            "reservation": reservation_label(conversation),
                            "guestMessage": last_guest_message,
                            "hostReply": body,
                            "hostReplyCreatedAt": post_created_at(post),
                            "module": sample if isinstance(sample, dict) else None,
                        }
                    )

    return {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "sourceConversationLimit": limit,
        "exampleCount": len(examples),
        "moduleSamples": modules,
        "examples": examples,
    }


def detect_language(text: str) -> str:
    chinese_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    ascii_letters = sum(1 for char in text if char.isascii() and char.isalpha())
    if chinese_chars > ascii_letters / 4:
        return "Chinese"
    return "English"


def first_words(text: str, count: int = 4) -> str:
    words = text.strip().split()
    return " ".join(words[:count])


def make_style_profile(examples_data: dict[str, Any]) -> str:
    examples = examples_data.get("examples", [])
    host_replies = [str(item.get("hostReply", "")).strip() for item in examples if item.get("hostReply")]
    languages: dict[str, int] = {}
    openings: dict[str, int] = {}
    lengths: list[int] = []
    for reply in host_replies:
        languages[detect_language(reply)] = languages.get(detect_language(reply), 0) + 1
        opening = first_words(reply)
        if opening:
            openings[opening] = openings.get(opening, 0) + 1
        lengths.append(len(reply.split()))

    dominant_language = max(languages, key=languages.get) if languages else "English"
    avg_words = round(sum(lengths) / len(lengths)) if lengths else 0
    top_openings = sorted(openings.items(), key=lambda item: item[1], reverse=True)[:8]

    lines = [
        "# Guesty Reply Style",
        "",
        f"Generated: {examples_data.get('generatedAt', '')}",
        f"Training examples: {examples_data.get('exampleCount', 0)}",
        "",
        "## Learned Defaults",
        "",
        f"- Default guest-facing language: {dominant_language}",
        f"- Average historical reply length: about {avg_words} words",
        "- Tone: match the historical host replies in `data/reply_examples.json` closely.",
        "- Keep replies direct, helpful, and guest-service oriented.",
        "- Use the guest's language when the guest clearly writes in another language.",
        "- Do not invent property details, fees, policies, or availability.",
        "",
        "## Common Openings",
        "",
    ]
    if top_openings:
        for opening, count in top_openings:
            lines.append(f"- `{opening}` ({count})")
    else:
        lines.append("- Not enough historical replies yet.")

    lines.extend(
        [
            "",
            "## Restriction Conditions: Tell Owner",
            "",
            "- Any request to change booking dates, extend a stay, move dates, or alter a reservation.",
            "- Refunds, compensation, discounts, waived fees, claims, chargebacks, or payment disputes.",
            "- Early checkout / leaving early / shortened stay.",
            "- Cancellations, platform disputes, direct booking, or off-platform booking questions.",
            "- Requests to confirm real availability, whether all rooms are booked, or whether a room/date is available.",
            "- Missing access codes, passwords, or property details that are not clearly available in history.",
            "- Safety, damage, injury, police, medical, illegal activity, or urgent complaints.",
            "- Any answer that is not clearly supported by historical replies or known policy.",
            "",
            "## Automation Rule",
            "",
            "Reply directly to ordinary guest messages.",
            "If a message hits any restriction condition, do not send a guest reply. Prepare a short summary and ask the owner.",
        ]
    )
    return "\n".join(lines) + "\n"


ESCALATION_RULES = {
    "date_change": [
        "change date",
        "change dates",
        "change my date",
        "change my dates",
        "change booking date",
        "change booking dates",
        "change reservation date",
        "change reservation dates",
        "modify date",
        "modify dates",
        "modify reservation",
        "reschedule",
        "move my booking",
        "different dates",
        "extend",
        "extension",
        "shorten",
        "改日期",
        "更改日期",
        "改期",
        "延住",
    ],
    "refund": [
        "refund",
        "money back",
        "reimburse",
        "reimbursement",
        "compensation",
        "discount",
        "waive",
        "chargeback",
        "退款",
        "退钱",
        "赔偿",
        "补偿",
        "折扣",
    ],
    "early_checkout": [
        "early checkout",
        "early check-out",
        "check out early",
        "checkout early",
        "leave early",
        "leaving early",
        "shorten my stay",
        "提前退房",
        "提前离开",
    ],
    "pre_arrival_access": [
        "luggage",
        "leave our luggage",
        "leave luggage",
        "drop off luggage",
        "drop off bags",
        "store luggage",
        "check the property",
        "see the property",
        "view the property",
        "visit the property",
        "check the room",
        "see the room",
        "view the room",
        "before my parents arrive",
        "before arrival",
        "before check-in",
        "before check in",
        "early check-in",
        "early check in",
        "check-in at 12",
        "check in at 12",
        "提前看房",
        "提前进房",
        "提前进入",
        "入住前",
    ],
    "property_detail_or_setup": [
        "parking",
        "driveway",
        "street parking",
        "washer",
        "dryer",
        "two rooms",
        "prepare the two rooms",
        "separate rooms",
    ],
    "human_action_required": [
        "please bring",
        "can you bring",
        "could you bring",
        "bring me",
        "bring us",
        "please provide extra",
        "can you provide extra",
        "could you provide extra",
        "please provide more",
        "can you provide more",
        "could you provide more",
        "can we have extra",
        "could we have extra",
        "can we have more",
        "could we have more",
        "need more",
        "extra towel",
        "extra towels",
        "fresh towel",
        "fresh towels",
        "clean towel",
        "clean towels",
        "more towels",
        "need towels",
        "can i get towels",
        "can we get towels",
        "could i get towels",
        "could we get towels",
        "extra blanket",
        "more blankets",
        "extra pillow",
        "more pillows",
        "extra sheet",
        "extra sheets",
        "fresh sheet",
        "fresh sheets",
        "clean sheet",
        "clean sheets",
        "bed sheet",
        "bed sheets",
        "bedsheet",
        "bedsheets",
        "linen",
        "linens",
        "bedding",
        "duvet cover",
        "pillowcase",
        "pillowcases",
        "need toilet paper",
        "more toilet paper",
        "out of toilet paper",
        "ran out of toilet paper",
        "need paper towel",
        "more paper towel",
        "out of paper towel",
        "please prepare",
        "prepare the two rooms",
        "separate rooms",
        "can you arrange",
        "could you arrange",
        "please arrange",
        "late checkout",
        "late check-out",
        "change room",
        "move room",
        "different room",
        "another room",
        "send someone",
        "someone come",
        "maintenance",
        "technician",
        "repair",
        "fix",
        "broken",
        "not working",
        "doesn't work",
        "doesnt work",
        "isn't working",
        "is not working",
        "no hot water",
        "no heat",
        "no electricity",
        "leak",
        "leaking",
        "clogged",
        "blocked toilet",
        "toilet is blocked",
        "toilet is clogged",
        "dirty",
        "not clean",
        "unclean",
        "cleaning issue",
        "needs cleaning",
        "trash is full",
        "garbage is full",
        "take out trash",
        "take out garbage",
        "trash bags",
        "garbage bags",
        "locked out",
        "can't get in",
        "cannot get in",
        "noise",
        "noisy",
        "loud",
        "neighbor",
        "neighbour",
        "请送",
        "能送",
        "多给",
        "需要更多",
        "需要毛巾",
        "多给毛巾",
        "送毛巾",
        "换毛巾",
        "干净毛巾",
        "需要纸巾",
        "需要卫生纸",
        "没纸了",
        "需要毯子",
        "需要枕头",
        "需要床品",
        "需要床单",
        "换床单",
        "被套",
        "枕套",
        "请安排",
        "帮忙安排",
        "请准备",
        "帮忙准备",
        "维修",
        "修理",
        "坏了",
        "不能用",
        "没热水",
        "漏水",
        "堵了",
        "不干净",
        "垃圾满了",
        "垃圾袋",
        "被锁",
        "进不去",
        "噪音",
        "太吵",
        "邻居",
    ],
    "cancellation_or_dispute": [
        "cancel",
        "cancellation",
        "dispute",
        "claim",
        "resolution center",
        "取消",
        "纠纷",
        "投诉",
    ],
    "pricing_or_platform": [
        "did you receive it",
        "receive the email",
        "received the email",
        "sending you an email",
        "sent you an email",
        "price confirmation",
        "confirm the price",
        "approve the price",
        "payment",
        "pay outside",
        "book without",
        "rebook without",
        "direct booking",
        "off platform",
        "outside the platform",
        "确认价格",
        "付款",
        "平台外",
        "直接预订",
    ],
    "availability_confirmation": [
        "from tomorrow",
        "all rooms are booked",
        "all rooms booked",
        "rooms are booked",
        "room is booked",
        "is it booked",
        "are they booked",
        "any room available",
        "any rooms available",
        "room available",
        "rooms available",
        "is there availability",
        "no availability",
        "available from",
        "available tomorrow",
        "确认有房",
        "有房吗",
        "订满",
        "满房",
        "没房",
    ],
    "missing_access_details": [
        "access code",
        "entry code",
        "door code",
        "gate password",
        "door password",
        "bedroom door",
        "password for the gate",
        "password for the doors",
        "门禁",
        "密码",
        "房门密码",
    ],
    "unclear_or_unsupported": [
        "no hay más debajo",
        "ya no hay más",
    ],
}
SOFT_ESCALATION_REASONS = {"property_detail_or_setup", "unclear_or_unsupported"}


def escalation_reasons(text: str) -> list[str]:
    lowered = text.lower()
    reasons = []
    for reason, keywords in ESCALATION_RULES.items():
        if any(keyword in lowered for keyword in keywords):
            reasons.append(reason)
    return reasons


def hard_escalation_reasons(text: str) -> list[str]:
    return [reason for reason in escalation_reasons(text) if reason not in SOFT_ESCALATION_REASONS]


def latest_guest_message(posts: list[dict[str, Any]]) -> dict[str, Any] | None:
    sorted_items = sort_posts(posts)
    for post in reversed(sorted_items):
        body = post_body(post).lower()
        if "status changed to canceled" in body or "status changed to cancelled" in body:
            return None
        if post_body(post) and is_guest_post(post):
            return post
        if post_body(post) and is_host_post(post):
            return None
    return None


def assert_guest_is_waiting(client: GuestyClient, conversation_id: str) -> None:
    if latest_guest_message(client.posts(conversation_id)) is None:
        raise GuestyError(
            "Latest waiting message is not from the guest; skipping to avoid duplicate or stale reply."
        )


def assert_conversation_in_scope(client: GuestyClient, conversation_id: str) -> None:
    conversations = client.conversations(limit=100, unread_only=False)
    conversation = next(
        (item for item in conversations if str(item.get("_id") or item.get("id") or "") == conversation_id),
        None,
    )
    posts = client.posts(conversation_id)
    if conversation is not None and matches_property_scope(conversation, posts):
        return
    if conversation is None:
        raise GuestyError("Conversation metadata was not available for property-scope verification.")
    raise GuestyError(
        "Conversation does not match allowed property scope; skipping to avoid replying to an unapproved property."
    )


def render_report(conversations: list[dict[str, Any]], posts_by_conversation: dict[str, list[dict[str, Any]]]) -> str:
    lines = [
        "# Guesty Inbox Report",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Conversations: {len(conversations)}",
        "",
    ]

    for index, conversation in enumerate(conversations, start=1):
        conversation_id = str(conversation.get("_id") or conversation.get("id") or "")
        posts = posts_by_conversation.get(conversation_id, [])
        latest = posts[-1] if posts else {}
        latest_body = post_body(latest)
        if len(latest_body) > 600:
            latest_body = latest_body[:597] + "..."

        lines.extend(
            [
                f"## {index}. {guest_name(conversation)}",
                "",
                f"- Conversation ID: `{conversation_id}`",
                f"- Reservation: {reservation_label(conversation)}",
                f"- Status: {conversation_status(conversation)}",
                f"- Priority: {conversation.get('priority', 'unknown')}",
                f"- Read: {conversation_read(conversation)}",
                f"- Latest sender: {post_sender(latest) if latest else 'none'}",
                "",
                "Latest message:",
                "",
                latest_body or "_No message body found in API response._",
                "",
            ]
        )

    return "\n".join(lines)


def cmd_test_auth(_: argparse.Namespace) -> int:
    client = GuestyClient()
    token = client.token()
    print("Guesty authentication succeeded.")
    print(f"Token type: {token.token_type}")
    print(f"Scope: {token.scope}")
    print(f"Expires at: {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(token.expires_at))}")
    return 0


def cmd_inbox_report(args: argparse.Namespace) -> int:
    client = GuestyClient()
    conversations = client.conversations(limit=args.limit, unread_only=not args.include_read)
    posts_by_conversation: dict[str, list[dict[str, Any]]] = {}
    for conversation in conversations:
        conversation_id = str(conversation.get("_id") or conversation.get("id") or "")
        if conversation_id:
            posts_by_conversation[conversation_id] = client.posts(conversation_id)

    report = render_report(conversations, posts_by_conversation)
    if args.out:
        out_path = Path(args.out)
        if not out_path.is_absolute():
            out_path = ROOT / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        print(f"Wrote report to {out_path}")
    else:
        print(report)
    return 0


def cmd_learn_style(args: argparse.Namespace) -> int:
    client = GuestyClient()
    examples_data = collect_reply_examples(client, args.limit)

    examples_out = Path(args.examples_out)
    if not examples_out.is_absolute():
        examples_out = ROOT / examples_out
    examples_out.parent.mkdir(parents=True, exist_ok=True)
    examples_out.write_text(json.dumps(examples_data, indent=2, ensure_ascii=False), encoding="utf-8")

    style = make_style_profile(examples_data)
    style_out = Path(args.out)
    if not style_out.is_absolute():
        style_out = ROOT / style_out
    style_out.parent.mkdir(parents=True, exist_ok=True)
    style_out.write_text(style, encoding="utf-8")

    print(f"Wrote {examples_data['exampleCount']} reply examples to {examples_out}")
    print(f"Wrote style profile to {style_out}")
    module_count = len(examples_data.get("moduleSamples", []))
    print(f"Found {module_count} message module sample(s)")
    return 0


def render_pending_review(items: list[dict[str, Any]]) -> str:
    lines = [
        "# Guesty Pending Review",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"Pending guest messages: {len(items)}",
        "",
    ]
    for index, item in enumerate(items, start=1):
        reasons = item["escalationReasons"]
        action = "NEEDS_OWNER_REVIEW" if reasons else "DRAFT_ELIGIBLE"
        body = item["latestGuestMessage"]
        if len(body) > 1200:
            body = body[:1197] + "..."
        lines.extend(
            [
                f"## {index}. {action}",
                "",
                f"- Conversation ID: `{item['conversationId']}`",
                f"- Reservation: {item['reservation']}",
                f"- Status: {item['status']}",
                f"- Read: {item['read']}",
                f"- Reasons: {', '.join(reasons) if reasons else 'low-risk candidate'}",
                "",
                "Guest message:",
                "",
                body,
                "",
            ]
        )
    return "\n".join(lines)


def parse_pending_review(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    in_message = False
    message_lines: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("## "):
            if current:
                current["latestGuestMessage"] = "\n".join(message_lines).strip()
                items.append(current)
            current = {"action": line.split(". ", 1)[-1].strip()}
            in_message = False
            message_lines = []
            continue
        if current is None:
            continue
        if line.startswith("- Conversation ID:"):
            current["conversationId"] = line.split("`")[1] if "`" in line else line.split(":", 1)[1].strip()
        elif line.startswith("- Reservation:"):
            current["reservation"] = line.split(":", 1)[1].strip()
        elif line.startswith("- Reasons:"):
            reasons = line.split(":", 1)[1].strip()
            current["reasons"] = [item.strip() for item in reasons.split(",") if item.strip()]
        elif line == "Guest message:":
            in_message = True
            message_lines = []
        elif in_message:
            message_lines.append(line)

    if current:
        current["latestGuestMessage"] = "\n".join(message_lines).strip()
        items.append(current)
    return items


def restriction_signature(item: dict[str, Any]) -> str:
    raw = "|".join(
        [
            str(item.get("conversationId", "")),
            ",".join(item.get("reasons", [])),
            str(item.get("latestGuestMessage", "")),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_notified_restrictions() -> dict[str, Any]:
    if not NOTIFIED_RESTRICTIONS_PATH.exists():
        return {}
    try:
        return json.loads(NOTIFIED_RESTRICTIONS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_notified_restrictions(data: dict[str, Any]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    NOTIFIED_RESTRICTIONS_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def render_restriction_alert_email(items: list[dict[str, Any]]) -> str:
    lines = [
        "Guesty restriction-condition messages need owner review.",
        "",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"New restriction messages: {len(items)}",
        "",
    ]
    for index, item in enumerate(items, start=1):
        lines.extend(
            [
                f"{index}. Reservation: {item.get('reservation', 'unknown')}",
                f"Conversation ID: {item.get('conversationId', 'unknown')}",
                f"Restriction reason: {', '.join(item.get('reasons', []))}",
                "",
                "Guest message:",
                str(item.get("latestGuestMessage", "")),
                "",
                "Recommended next step:",
                "Please confirm the correct answer, then reply to the guest in Guesty.",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def cmd_restriction_alerts(args: argparse.Namespace) -> int:
    pending_path = Path(args.pending)
    if not pending_path.is_absolute():
        pending_path = ROOT / pending_path
    if not pending_path.exists():
        raise GuestyError(f"Pending review file not found: {pending_path}")

    items = [
        item for item in parse_pending_review(pending_path.read_text(encoding="utf-8"))
        if item.get("action") == "NEEDS_OWNER_REVIEW"
    ]
    notified = load_notified_restrictions()
    new_items = [item for item in items if restriction_signature(item) not in notified]

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_restriction_alert_email(new_items), encoding="utf-8")

    if args.mark_sent:
        sent_at = time.strftime("%Y-%m-%d %H:%M:%S %Z")
        for item in new_items:
            notified[restriction_signature(item)] = {
                "sentAt": sent_at,
                "conversationId": item.get("conversationId"),
                "reservation": item.get("reservation"),
                "reasons": item.get("reasons", []),
            }
        save_notified_restrictions(notified)

    print(f"New restriction alerts: {len(new_items)}")
    print(f"Wrote alert email body to {out_path}")
    return 0


def cmd_review_new(args: argparse.Namespace) -> int:
    client = GuestyClient()
    conversations = client.conversations(limit=args.limit, unread_only=not args.include_read)
    items: list[dict[str, Any]] = []

    for conversation in conversations:
        conversation_id = str(conversation.get("_id") or conversation.get("id") or "")
        if not conversation_id:
            continue
        posts = client.posts(conversation_id)
        if not matches_property_scope(conversation, posts):
            continue
        latest_guest = latest_guest_message(posts)
        if not latest_guest:
            continue
        body = post_body(latest_guest)
        reasons = hard_escalation_reasons(body)
        items.append(
            {
                "conversationId": conversation_id,
                "reservation": reservation_label(conversation),
                "status": conversation_status(conversation),
                "read": conversation_read(conversation),
                "latestGuestMessage": body,
                "latestGuestMessageCreatedAt": post_created_at(latest_guest),
                "escalationReasons": reasons,
            }
        )

    report = render_pending_review(items)
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")

    owner_review_count = sum(1 for item in items if item["escalationReasons"])
    draft_count = len(items) - owner_review_count
    print(f"Wrote pending review to {out_path}")
    print(f"Pending: {len(items)} | Needs owner: {owner_review_count} | Draft eligible: {draft_count}")
    return 0


def summarize_shape(data: Any) -> Any:
    if isinstance(data, list):
        return {"type": "list", "length": len(data), "first": summarize_shape(data[0]) if data else None}
    if isinstance(data, dict):
        summary: dict[str, Any] = {"type": "dict", "keys": sorted(data.keys())}
        for key in ("data", "items", "results", "conversations", "posts", "count", "total", "cursor"):
            if key in data:
                value = data[key]
                if isinstance(value, (list, dict)):
                    summary[key] = summarize_shape(value)
                else:
                    summary[key] = value
        return summary
    return {"type": type(data).__name__}


def cmd_debug_conversations(args: argparse.Namespace) -> int:
    client = GuestyClient()
    data = client.api("GET", "/communication/conversations", params={"limit": args.limit})
    print(json.dumps(summarize_shape(data), indent=2, ensure_ascii=False))
    print(f"Extracted items: {len(extract_items(data))}")
    return 0


def cmd_debug_posts(args: argparse.Namespace) -> int:
    client = GuestyClient()
    posts = sort_posts(client.posts(args.conversation_id))
    rows = []
    for post in posts[-args.limit :]:
        rows.append(
            {
                "createdAt": post_created_at(post),
                "sender": post_sender(post),
                "isGuest": is_guest_post(post),
                "isHost": is_host_post(post),
                "keys": sorted(post.keys()),
                "bodyPreview": post_body(post)[:80],
            }
        )
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


def cmd_debug_scope(args: argparse.Namespace) -> int:
    client = GuestyClient()
    rows = []
    for conversation in client.conversations(limit=args.limit, unread_only=not args.include_read):
        if args.conversation_id:
            conversation_id = str(conversation.get("_id") or conversation.get("id") or "")
            if conversation_id != args.conversation_id:
                continue
        rows.append(property_scope_debug(conversation))
    print(json.dumps(rows, indent=2, ensure_ascii=False))
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    module = safe_json_loads(os.getenv("GUESTY_SEND_MODULE_JSON"), None)
    client = GuestyClient()
    if not isinstance(module, dict):
        module = client.infer_message_module(args.conversation_id)
    if not isinstance(module, dict):
        raise GuestyError("Could not infer the Guesty message module for this conversation.")
    module = sanitize_send_module(module)

    if not args.confirm_send:
        print("Dry run only. Message was not sent.")
        print(f"Conversation ID: {args.conversation_id}")
        print(f"Body: {args.body}")
        print(f"Module type: {module.get('type', 'unknown')}")
        return 0

    assert_conversation_in_scope(client, args.conversation_id)
    assert_guest_is_waiting(client, args.conversation_id)
    client.send_message(args.conversation_id, args.body, module)
    print("Message sent.")
    return 0


def cmd_list_webhooks(_: argparse.Namespace) -> int:
    client = GuestyClient()
    print(json.dumps(mask_secrets(client.webhooks()), indent=2, ensure_ascii=False))
    return 0


def cmd_create_webhook(args: argparse.Namespace) -> int:
    public_url = args.url or os.getenv("GUESTY_WEBHOOK_URL", DEFAULT_PUBLIC_WEBHOOK_URL)
    if "secret=" not in public_url:
        secret = require_env("GUESTY_WEBHOOK_SECRET")
        public_url = url_with_query_secret(public_url, secret)

    events = args.event or ["reservation.messageReceived"]
    print(f"Webhook URL: {masked_url(public_url)}")
    print(f"Events: {', '.join(events)}")
    if not args.confirm_create:
        print("Dry run only. Re-run with --confirm-create to create this webhook in Guesty.")
        return 0

    client = GuestyClient()
    for webhook in client.webhooks():
        existing_url = str(webhook.get("url") or "")
        existing_events = webhook.get("events")
        if existing_url == public_url and isinstance(existing_events, list):
            missing_events = [event for event in events if event not in existing_events]
            if not missing_events:
                print("Webhook already exists with the requested event.")
                print(json.dumps(mask_secrets(webhook), indent=2, ensure_ascii=False))
                return 0

    result = client.create_webhook(public_url, events)
    print("Webhook created.")
    print(json.dumps(mask_secrets(result), indent=2, ensure_ascii=False))
    return 0


def cmd_delete_webhook(args: argparse.Namespace) -> int:
    if not args.confirm_delete:
        print("Dry run only. Re-run with --confirm-delete to delete this Guesty webhook.")
        print(f"Webhook ID: {args.webhook_id}")
        return 0
    client = GuestyClient()
    result = client.delete_webhook(args.webhook_id)
    print("Webhook deleted.")
    print(json.dumps(mask_secrets(result), indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Guesty guest-message automation helper")
    sub = parser.add_subparsers(dest="command", required=True)

    test_auth = sub.add_parser("test-auth", help="Validate Guesty API credentials")
    test_auth.set_defaults(func=cmd_test_auth)

    inbox_report = sub.add_parser("inbox-report", help="Generate a report for open guest conversations")
    inbox_report.add_argument("--limit", type=int, default=int(os.getenv("GUESTY_CONVERSATION_LIMIT", "25")))
    inbox_report.add_argument("--include-read", action="store_true")
    inbox_report.add_argument("--out", help="Optional output markdown path")
    inbox_report.set_defaults(func=cmd_inbox_report)

    learn_style = sub.add_parser("learn-style", help="Export historical host replies and a style profile")
    learn_style.add_argument("--limit", type=int, default=100)
    learn_style.add_argument("--out", default=str(STYLE_PATH.relative_to(ROOT)))
    learn_style.add_argument("--examples-out", default=str(EXAMPLES_PATH.relative_to(ROOT)))
    learn_style.set_defaults(func=cmd_learn_style)

    review_new = sub.add_parser("review-new", help="Classify latest guest messages for owner review or drafting")
    review_new.add_argument("--limit", type=int, default=50)
    review_new.add_argument("--include-read", action="store_true")
    review_new.add_argument("--out", default=str(PENDING_REVIEW_PATH.relative_to(ROOT)))
    review_new.set_defaults(func=cmd_review_new)

    restriction_alerts = sub.add_parser(
        "restriction-alerts",
        help="Render unnotified restriction-condition messages for email notification",
    )
    restriction_alerts.add_argument("--pending", default=str(PENDING_REVIEW_PATH.relative_to(ROOT)))
    restriction_alerts.add_argument("--out", default=str(RESTRICTION_ALERTS_PATH.relative_to(ROOT)))
    restriction_alerts.add_argument("--mark-sent", action="store_true")
    restriction_alerts.set_defaults(func=cmd_restriction_alerts)

    debug_conversations = sub.add_parser(
        "debug-conversations",
        help="Print response shape for conversations without guest message content",
    )
    debug_conversations.add_argument("--limit", type=int, default=1)
    debug_conversations.set_defaults(func=cmd_debug_conversations)

    debug_posts = sub.add_parser("debug-posts", help="Print sender/type shape for recent conversation posts")
    debug_posts.add_argument("--conversation-id", required=True)
    debug_posts.add_argument("--limit", type=int, default=6)
    debug_posts.set_defaults(func=cmd_debug_posts)

    debug_scope = sub.add_parser("debug-scope", help="Print property-scope fields used for filtering")
    debug_scope.add_argument("--conversation-id")
    debug_scope.add_argument("--limit", type=int, default=20)
    debug_scope.add_argument("--include-read", action="store_true")
    debug_scope.set_defaults(func=cmd_debug_scope)

    send = sub.add_parser("send", help="Send a message to a conversation")
    send.add_argument("--conversation-id", required=True)
    send.add_argument("--body", required=True)
    send.add_argument("--confirm-send", action="store_true")
    send.set_defaults(func=cmd_send)

    list_webhooks = sub.add_parser("list-webhooks", help="List Guesty webhook subscriptions")
    list_webhooks.set_defaults(func=cmd_list_webhooks)

    create_webhook = sub.add_parser("create-webhook", help="Create a Guesty webhook subscription")
    create_webhook.add_argument("--url", help="Public webhook URL. Defaults to the Render Guesty webhook URL.")
    create_webhook.add_argument("--event", action="append", help="Guesty webhook event. Can be repeated.")
    create_webhook.add_argument("--confirm-create", action="store_true")
    create_webhook.set_defaults(func=cmd_create_webhook)

    delete_webhook = sub.add_parser("delete-webhook", help="Delete a Guesty webhook subscription")
    delete_webhook.add_argument("--webhook-id", required=True)
    delete_webhook.add_argument("--confirm-delete", action="store_true")
    delete_webhook.set_defaults(func=cmd_delete_webhook)

    return parser


def main() -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except GuestyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
