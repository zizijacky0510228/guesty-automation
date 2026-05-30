#!/usr/bin/env python3
"""Generate and send Guesty cleaning reports from Render or locally."""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from guesty_automation import DATA_DIR, GuestyClient, GuestyError, extract_items, load_dotenv


RESERVATION_FIELDS = (
    "_id status checkInDateLocalized checkOutDateLocalized "
    "listingId listing listing.title listing.nickname"
)

PROPERTY_GROUPS = [
    ("1348/1346", ("1348.5", "1348", "1346.5", "1346ADU", "1346")),
    ("1374/1376", ("1374", "1376")),
    ("1221/1223.5", ("1221.5", "1221", "1223.5")),
    ("1606", ("1606",)),
    ("1495", ("1495",)),
    ("383", ("383",)),
    ("6550", ("6550",)),
    ("3505", ("3505",)),
    ("2171", ("2171",)),
]

STAR = " ⭐️"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate Guesty cleaning reports")
    parser.add_argument(
        "--mode",
        choices=("report", "baseline", "delta", "schedule"),
        default="report",
        help="report/baseline sends the next-day report, delta compares current tasks to the saved 20:00 baseline, schedule no-ops unless inside a configured time window",
    )
    parser.add_argument("--schedule", action="store_true", help="Shortcut for --mode schedule")
    parser.add_argument("--check-updates", action="store_true", help="Shortcut for --mode delta")
    parser.add_argument("--day-offset", type=int, help="Days from today for the report date")
    parser.add_argument("--date", help="Explicit report date in YYYY-MM-DD format")
    parser.add_argument("--no-email", action="store_true")
    parser.add_argument("--no-wecom", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print only; do not send email or WeCom messages")
    parser.add_argument("--force-schedule", action="store_true", help="Run due scheduled actions even if already marked done")
    return parser.parse_args()


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def bool_env(*names: str, default: bool = False) -> bool:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def int_env(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    raw = env(name, str(default))
    try:
        value = int(raw)
    except ValueError:
        raise GuestyError(f"{name} must be a number.") from None
    if value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def report_timezone() -> ZoneInfo:
    return ZoneInfo(env("REPORT_TIMEZONE", "America/Vancouver") or "America/Vancouver")


def report_date_from_args(args: argparse.Namespace, default_offset: int) -> date:
    if args.date:
        return date.fromisoformat(args.date)
    offset = args.day_offset if args.day_offset is not None else default_offset
    return (datetime.now(report_timezone()) + timedelta(days=offset)).date()


def normalize_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "data", "items", "tasks", "reservations"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = normalize_results(value)
            if nested:
                return nested
    return extract_items(payload)


def fetch_reservations_by_localized_date(client: GuestyClient, field: str, report_date: date) -> list[dict[str, Any]]:
    filters = json.dumps(
        [
            {"field": field, "operator": "$eq", "value": report_date.isoformat()},
            {"operator": "$in", "field": "status", "value": ["confirmed", "reserved"]},
        ],
        separators=(",", ":"),
    )

    reservations: list[dict[str, Any]] = []
    limit = 100
    skip = 0
    while True:
        payload = client.api(
            "GET",
            "/reservations",
            params={
                "fields": RESERVATION_FIELDS,
                "filters": filters,
                "sort": "_id",
                "limit": limit,
                "skip": skip,
            },
        )
        page = normalize_results(payload)
        reservations.extend(page)
        total = payload.get("count") if isinstance(payload, dict) else None
        if len(page) < limit or (isinstance(total, int) and len(reservations) >= total):
            break
        skip += limit
    return reservations


def pick_value(data: dict[str, Any], *names: str) -> str:
    lower_map = {key.lower(): key for key in data}
    for name in names:
        key = lower_map.get(name.lower())
        if not key:
            continue
        value = data.get(key)
        if isinstance(value, dict):
            return str(value.get("title") or value.get("name") or value.get("fullName") or "")
        if value not in (None, ""):
            return str(value)
    return ""


def clean_address(text: str) -> str:
    text = str(text or "").strip()
    text = re.split(r"\s+/\s+", text, maxsplit=1)[0]
    text = re.sub(r"\bNo\.?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\bUnit\.?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(\d{3,5})\s+([A-Za-z])\s*/\s*", r"\1\2 ", text)
    text = re.sub(r"\(\s*1\s*/\s*2\s*\)", ".5", text)
    text = re.sub(r"(?<=\d)\s+1\s*/\s*2\b", ".5", text)
    text = re.sub(r"\b1\s*/\s*2\b", ".5", text)
    text = re.sub(r"\(([^)]+)\)", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+-", "-", text)
    text = re.sub(r"-\s+", "-", text)
    text = re.sub(r"\s+,", ",", text)
    return text.strip(" -,\t")


def reservation_listing_id(reservation: dict[str, Any]) -> str:
    listing = reservation.get("listing")
    return (
        str(reservation.get("listingId") or "")
        or (str(listing.get("_id") or "") if isinstance(listing, dict) else "")
    )


def reservation_address(reservation: dict[str, Any]) -> str:
    listing = reservation.get("listing")
    if isinstance(listing, dict):
        value = listing.get("nickname") or listing.get("title")
        if value:
            return clean_address(str(value))
    return clean_address(pick_value(reservation, "listingId") or "Untitled reservation")


def natural_sort_key(text: str) -> list[Any]:
    parts = re.split(r"(\d+(?:\.\d+)?)", text.lower())
    return [float(part) if re.fullmatch(r"\d+(?:\.\d+)?", part) else part for part in parts]


def property_group_key(address: str) -> str:
    normalized = address.upper().replace(" ", "")
    for group_name, prefixes in PROPERTY_GROUPS:
        if any(normalized.startswith(prefix.upper()) for prefix in prefixes):
            return group_name
    return "Other"


def build_report(reservations: list[dict[str, Any]], report_date: date, turnover_listing_ids: set[str]) -> str:
    if not reservations:
        return f"Awaiting Cleaning ({report_date.strftime('%m-%d-%Y')})"

    grouped: dict[str, list[dict[str, Any]]] = {}
    for reservation in reservations:
        address = reservation_address(reservation)
        grouped.setdefault(property_group_key(address), []).append(reservation)

    lines: list[str] = []
    group_order = [group_name for group_name, _ in PROPERTY_GROUPS] + ["Other"]
    for group_name in group_order:
        group_reservations = grouped.get(group_name)
        if not group_reservations:
            continue
        if lines:
            lines.append("")
        lines.append(f"Awaiting Cleaning ({report_date.strftime('%m-%d-%Y')})")
        for reservation in sorted(group_reservations, key=lambda item: natural_sort_key(reservation_address(item))):
            listing_id = reservation_listing_id(reservation)
            suffix = STAR if listing_id in turnover_listing_ids else ""
            lines.append(f"{reservation_address(reservation)}{suffix}")
    return "\n".join(lines)


def build_snapshot(reservations: list[dict[str, Any]], report_date: date, turnover_listing_ids: set[str]) -> dict[str, Any]:
    items = []
    for reservation in sorted(reservations, key=lambda item: natural_sort_key(reservation_address(item))):
        listing_id = reservation_listing_id(reservation)
        items.append(
            {
                "listing_id": listing_id,
                "address": reservation_address(reservation),
                "turnover": listing_id in turnover_listing_ids,
            }
        )
    return {
        "report_date": report_date.isoformat(),
        "generated_at": datetime.now(report_timezone()).isoformat(),
        "items": items,
    }


def snapshot_map(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not snapshot:
        return {}
    return {
        str(item.get("address") or ""): item
        for item in snapshot.get("items", [])
        if isinstance(item, dict) and item.get("address")
    }


def build_change_report(previous_snapshot: dict[str, Any], current_snapshot: dict[str, Any], report_date: date) -> str:
    previous = snapshot_map(previous_snapshot)
    current = snapshot_map(current_snapshot)

    new_cleanings = sorted(
        [address for address in current if address not in previous],
        key=natural_sort_key,
    )
    removed_cleanings = sorted(
        [address for address in previous if address not in current],
        key=natural_sort_key,
    )
    new_turnovers = sorted(
        [
            address
            for address, item in current.items()
            if item.get("turnover") and (address not in previous or not previous[address].get("turnover"))
        ],
        key=natural_sort_key,
    )
    removed_turnovers = sorted(
        [
            address
            for address, item in previous.items()
            if item.get("turnover") and address in current and not current[address].get("turnover")
        ],
        key=natural_sort_key,
    )

    if not new_cleanings and not removed_cleanings and not new_turnovers and not removed_turnovers:
        return ""

    lines = [f"Cleaning Update ({report_date.strftime('%m-%d-%Y')})", ""]
    if new_cleanings:
        lines.append("New cleaning:")
        lines.extend(new_cleanings)
        lines.append("")
    if removed_cleanings:
        lines.append("Removed cleaning:")
        lines.extend(removed_cleanings)
        lines.append("")
    if new_turnovers:
        lines.append("New turnover cleaning:")
        lines.extend(f"{address}{STAR}" for address in new_turnovers)
        lines.append("")
    if removed_turnovers:
        lines.append("No longer turnover cleaning:")
        lines.extend(removed_turnovers)
    return "\n".join(lines).rstrip()


def collect_report_data(client: GuestyClient, report_date: date) -> tuple[str, dict[str, Any]]:
    checkout_reservations = fetch_reservations_by_localized_date(client, "checkOutDateLocalized", report_date)
    checkin_reservations = fetch_reservations_by_localized_date(client, "checkInDateLocalized", report_date)
    checkin_listing_ids = {
        listing_id
        for listing_id in (reservation_listing_id(reservation) for reservation in checkin_reservations)
        if listing_id
    }
    turnover_listing_ids = {
        listing_id
        for listing_id in (reservation_listing_id(reservation) for reservation in checkout_reservations)
        if listing_id in checkin_listing_ids
    }
    report = build_report(checkout_reservations, report_date, turnover_listing_ids)
    snapshot = build_snapshot(checkout_reservations, report_date, turnover_listing_ids)
    return report, snapshot


class StateStore:
    def get_text(self, key: str) -> str | None:
        raise NotImplementedError

    def set_text(self, key: str, value: str) -> None:
        raise NotImplementedError

    def get_json(self, key: str) -> dict[str, Any] | None:
        value = self.get_text(key)
        if not value:
            return None
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def set_json(self, key: str, value: dict[str, Any]) -> None:
        self.set_text(key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))


class FileStateStore(StateStore):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        filename = re.sub(r"[^A-Za-z0-9_.-]+", "_", key).strip("_")
        return self.root / f"{filename}.json"

    def get_text(self, key: str) -> str | None:
        path = self.path_for(key)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def set_text(self, key: str, value: str) -> None:
        self.path_for(key).write_text(value, encoding="utf-8")


class RedisStateStore(StateStore):
    def __init__(self, url: str, prefix: str = "cleaning") -> None:
        self.url = url
        self.prefix = prefix.strip(":") or "cleaning"
        self.timeout = int_env("CLEANING_STATE_REDIS_TIMEOUT_SECONDS", 10, 1, 60)

    def namespaced(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    def get_text(self, key: str) -> str | None:
        result = self.execute("GET", self.namespaced(key))
        if result is None:
            return None
        if isinstance(result, bytes):
            return result.decode("utf-8")
        return str(result)

    def set_text(self, key: str, value: str) -> None:
        self.execute("SET", self.namespaced(key), value)

    def execute(self, *parts: str) -> Any:
        parsed = urllib.parse.urlparse(self.url)
        host = parsed.hostname
        if not host:
            raise GuestyError("CLEANING_STATE_REDIS_URL is missing a host.")
        port = parsed.port or (6380 if parsed.scheme == "rediss" else 6379)
        try:
            raw_socket = socket.create_connection((host, port), timeout=self.timeout)
            sock: socket.socket | ssl.SSLSocket
            if parsed.scheme == "rediss":
                sock = ssl.create_default_context().wrap_socket(raw_socket, server_hostname=host)
            else:
                sock = raw_socket
            with sock:
                password = urllib.parse.unquote(parsed.password or "")
                username = urllib.parse.unquote(parsed.username or "")
                if password and username:
                    self.send_command(sock, "AUTH", username, password)
                    self.read_response(sock)
                elif password:
                    self.send_command(sock, "AUTH", password)
                    self.read_response(sock)

                db = parsed.path.strip("/")
                if db:
                    self.send_command(sock, "SELECT", db)
                    self.read_response(sock)

                self.send_command(sock, *parts)
                return self.read_response(sock)
        except OSError as exc:
            raise GuestyError(f"Redis state store request failed: {exc}") from exc

    @staticmethod
    def send_command(sock: socket.socket | ssl.SSLSocket, *parts: str) -> None:
        payload = [f"*{len(parts)}\r\n".encode("utf-8")]
        for part in parts:
            encoded = str(part).encode("utf-8")
            payload.append(f"${len(encoded)}\r\n".encode("utf-8"))
            payload.append(encoded + b"\r\n")
        sock.sendall(b"".join(payload))

    @classmethod
    def read_response(cls, sock: socket.socket | ssl.SSLSocket) -> Any:
        prefix = cls.read_exact(sock, 1)
        if prefix == b"+":
            return cls.read_line(sock).decode("utf-8")
        if prefix == b"-":
            raise GuestyError(f"Redis state store error: {cls.read_line(sock).decode('utf-8')}")
        if prefix == b":":
            return int(cls.read_line(sock).decode("utf-8"))
        if prefix == b"$":
            length = int(cls.read_line(sock).decode("utf-8"))
            if length == -1:
                return None
            data = cls.read_exact(sock, length)
            cls.read_exact(sock, 2)
            return data
        if prefix == b"*":
            count = int(cls.read_line(sock).decode("utf-8"))
            return [cls.read_response(sock) for _ in range(count)]
        raise GuestyError(f"Unexpected Redis response prefix: {prefix!r}")

    @staticmethod
    def read_line(sock: socket.socket | ssl.SSLSocket) -> bytes:
        chunks = []
        while True:
            char = sock.recv(1)
            if not char:
                raise GuestyError("Redis state store closed the connection.")
            chunks.append(char)
            if b"".join(chunks[-2:]) == b"\r\n":
                return b"".join(chunks[:-2])

    @staticmethod
    def read_exact(sock: socket.socket | ssl.SSLSocket, length: int) -> bytes:
        chunks = []
        remaining = length
        while remaining:
            chunk = sock.recv(remaining)
            if not chunk:
                raise GuestyError("Redis state store closed the connection.")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


def state_store() -> StateStore:
    redis_url = first_env("CLEANING_STATE_REDIS_URL", "REDIS_URL")
    if redis_url:
        return RedisStateStore(redis_url, first_env("CLEANING_STATE_KEY_PREFIX", default="cleaning"))
    root = Path(first_env("CLEANING_STATE_DIR", default=str(DATA_DIR / "cleaning_state")))
    return FileStateStore(root)


def snapshot_key(report_date: date) -> str:
    return f"snapshot:{report_date.isoformat()}"


def last_run_key(mode: str, report_date: date) -> str:
    return f"last-run:{mode}:{report_date.isoformat()}"


def send_email(subject: str, body: str) -> None:
    host = first_env("CLEANING_SMTP_HOST", "SMTP_HOST", "GUESTY_ALERT_SMTP_HOST")
    port = int(first_env("CLEANING_SMTP_PORT", "SMTP_PORT", "GUESTY_ALERT_SMTP_PORT", default="587"))
    username = first_env("CLEANING_SMTP_USERNAME", "SMTP_USERNAME", "GUESTY_ALERT_SMTP_USERNAME")
    password = first_env("CLEANING_SMTP_PASSWORD", "SMTP_PASSWORD", "GUESTY_ALERT_SMTP_PASSWORD")
    sender = first_env("CLEANING_EMAIL_FROM", "EMAIL_FROM", "GUESTY_ALERT_EMAIL_FROM", default=username)
    recipient = first_env("CLEANING_EMAIL_TO", "EMAIL_TO")

    missing = [
        name
        for name, value in {
            "CLEANING_SMTP_HOST": host,
            "CLEANING_SMTP_USERNAME": username,
            "CLEANING_SMTP_PASSWORD": password,
            "CLEANING_EMAIL_FROM": sender,
            "CLEANING_EMAIL_TO": recipient,
        }.items()
        if not value
    ]
    if missing:
        raise GuestyError(f"Missing cleaning email configuration: {', '.join(missing)}")

    message = MIMEText(body, "plain", "utf-8")
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient

    use_ssl = bool_env("CLEANING_SMTP_SSL", "SMTP_SSL", default=False)
    use_starttls = bool_env("CLEANING_SMTP_STARTTLS", "SMTP_STARTTLS", "GUESTY_ALERT_SMTP_STARTTLS", default=not use_ssl)
    context = ssl.create_default_context()

    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            server.login(username, password)
            server.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            if use_starttls:
                server.starttls(context=context)
            server.login(username, password)
            server.send_message(message)


def send_wecom_webhook(body: str) -> None:
    webhook_url = first_env("CLEANING_WECOM_WEBHOOK_URL", "WECOM_WEBHOOK_URL")
    if not webhook_url:
        raise GuestyError("Missing WECOM webhook URL.")
    payload = json.dumps({"msgtype": "text", "text": {"content": body}}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def maybe_notify(subject: str, body: str, args: argparse.Namespace) -> None:
    if args.dry_run:
        return
    if bool_env("CLEANING_SEND_EMAIL", "SEND_EMAIL", default=False) and not args.no_email:
        send_email(subject, body)
    if bool_env("CLEANING_SEND_WECOM", "SEND_WECOM", default=False) and not args.no_wecom:
        send_wecom_webhook(body)


def run_report(args: argparse.Namespace, store: StateStore, client: GuestyClient, *, default_offset: int = 1) -> str:
    target_date = report_date_from_args(args, default_offset)
    report, snapshot = collect_report_data(client, target_date)
    store.set_json(snapshot_key(target_date), snapshot)
    is_baseline = getattr(args, "mode", "") == "baseline"
    subject = (
        f"明天清洁任务 {target_date.isoformat()}"
        if is_baseline or (default_offset == 1 and (args.day_offset in (None, 1)) and not args.date)
        else f"清洁任务 {target_date.isoformat()}"
    )
    maybe_notify(subject, report, args)
    return report


def missing_baseline_report(target_date: date) -> str:
    return (
        f"Cleaning Update ({target_date.strftime('%m-%d-%Y')})\n\n"
        f"No 20:00 baseline snapshot was found for {target_date.isoformat()}, "
        "so the 10:30 comparison could not run. The cloud job should create the "
        "baseline at 20:00 Vancouver time before this check."
    )


def run_delta(args: argparse.Namespace, store: StateStore, client: GuestyClient) -> str:
    target_date = report_date_from_args(args, 0)
    baseline = store.get_json(snapshot_key(target_date))
    if not baseline:
        body = missing_baseline_report(target_date)
        if bool_env("CLEANING_ALERT_ON_MISSING_BASELINE", default=True):
            maybe_notify(f"清洁任务更新 {target_date.isoformat()}", body, args)
        return body

    _report, current_snapshot = collect_report_data(client, target_date)
    change_report = build_change_report(baseline, current_snapshot, target_date)
    store.set_json(f"delta:{target_date.isoformat()}:{int(time.time())}", current_snapshot)
    if change_report:
        maybe_notify(f"清洁任务更新 {target_date.isoformat()}", change_report, args)
        return change_report
    return f"No cleaning changes for {target_date.isoformat()}"


def inside_window(now: datetime, hour: int, minute: int, window_minutes: int) -> bool:
    start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    end = start + timedelta(minutes=window_minutes)
    return start <= now < end


def run_schedule(args: argparse.Namespace, store: StateStore, client: GuestyClient) -> str:
    now = datetime.now(report_timezone())
    window = int_env("CLEANING_SCHEDULE_WINDOW_MINUTES", 59, 1, 120)
    outputs: list[str] = []

    baseline_date = (now + timedelta(days=1)).date()
    baseline_due = inside_window(
        now,
        int_env("CLEANING_BASELINE_HOUR", 20, 0, 23),
        int_env("CLEANING_BASELINE_MINUTE", 0, 0, 59),
        window,
    )
    baseline_done_key = last_run_key("baseline", baseline_date)
    if baseline_due and (args.force_schedule or not store.get_text(baseline_done_key)):
        scheduled_args = argparse.Namespace(
            **{**vars(args), "mode": "baseline", "date": baseline_date.isoformat(), "day_offset": None}
        )
        outputs.append(run_report(scheduled_args, store, client, default_offset=1))
        store.set_text(baseline_done_key, datetime.now(report_timezone()).isoformat())

    delta_date = now.date()
    delta_due = inside_window(
        now,
        int_env("CLEANING_DELTA_HOUR", 10, 0, 23),
        int_env("CLEANING_DELTA_MINUTE", 30, 0, 59),
        window,
    )
    delta_done_key = last_run_key("delta", delta_date)
    if delta_due and (args.force_schedule or not store.get_text(delta_done_key)):
        scheduled_args = argparse.Namespace(
            **{**vars(args), "mode": "delta", "date": delta_date.isoformat(), "day_offset": None}
        )
        if store.get_json(snapshot_key(delta_date)):
            outputs.append(run_delta(scheduled_args, store, client))
            store.set_text(delta_done_key, datetime.now(report_timezone()).isoformat())
        else:
            missing_done_key = last_run_key("delta-missing-baseline", delta_date)
            if args.force_schedule or not store.get_text(missing_done_key):
                outputs.append(run_delta(scheduled_args, store, client))
                store.set_text(missing_done_key, datetime.now(report_timezone()).isoformat())
            else:
                outputs.append(f"Missing baseline already reported for {delta_date.isoformat()}")

    if outputs:
        return "\n\n---\n\n".join(outputs)
    return f"No scheduled cleaning action at {now.isoformat()}"


def main() -> int:
    load_dotenv()
    args = parse_args()
    mode = args.mode
    if args.schedule:
        mode = "schedule"
    if args.check_updates:
        mode = "delta"

    store = state_store()
    client = GuestyClient()
    if mode in {"report", "baseline"}:
        output = run_report(args, store, client, default_offset=1)
    elif mode == "delta":
        output = run_delta(args, store, client)
    else:
        output = run_schedule(args, store, client)
    print(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
