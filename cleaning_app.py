#!/usr/bin/env python3
"""Small cleaning task web app mounted by the Guesty webhook service."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo


APP_KEY_PREFIX = "cleaning-app"
STATUS_VALUES = ("unassigned", "assigned", "in_progress", "done", "issue")
STATUS_LABELS = {
    "unassigned": "未分配",
    "assigned": "已分配",
    "in_progress": "进行中",
    "done": "已完成",
    "issue": "有问题",
}


@dataclass
class WebResponse:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] | None = None


def json_response(status: int, payload: dict[str, Any]) -> WebResponse:
    return WebResponse(
        status=status,
        body=json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
    )


def html_response(body: str, status: int = 200) -> WebResponse:
    return WebResponse(status=status, body=body.encode("utf-8"), content_type="text/html; charset=utf-8")


def redirect_response(location: str) -> WebResponse:
    return WebResponse(status=302, body=b"", content_type="text/plain; charset=utf-8", headers={"Location": location})


def now_iso() -> str:
    return datetime.now(report_timezone()).isoformat()


def report_timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("REPORT_TIMEZONE", "America/Vancouver").strip() or "America/Vancouver")


def get_state_store() -> Any:
    from guesty_cleaning_report import state_store

    return state_store()


def today_iso() -> str:
    return datetime.now(report_timezone()).date().isoformat()


def key_for(name: str) -> str:
    return f"{APP_KEY_PREFIX}:{name}"


def tasks_key(task_date: str) -> str:
    return key_for(f"tasks:{task_date}")


def cleaners_key() -> str:
    return key_for("cleaners")


def assignment_rules_key() -> str:
    return key_for("assignment-rules")


def clean_text(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").replace("\r", "\n").split())[:limit]


def clean_multiline(value: Any, limit: int = 1200) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:limit]


def normalize_date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return today_iso()
    datetime.strptime(raw, "%Y-%m-%d")
    return raw


def admin_token() -> str:
    return os.getenv("CLEANING_ADMIN_TOKEN", "").strip() or os.getenv("GUESTY_WEBHOOK_SECRET", "").strip()


def token_matches(actual: str, expected: str) -> bool:
    return bool(actual and expected and hmac.compare_digest(actual, expected))


def query_params(raw_path: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(raw_path).query)


def first_query(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name)
    if not values:
        return default
    return str(values[0] or "").strip()


def requested_date(query: dict[str, list[str]]) -> str:
    try:
        return normalize_date(first_query(query, "date"))
    except ValueError:
        return today_iso()


def requested_lang(query: dict[str, list[str]]) -> str:
    return "en" if first_query(query, "lang").lower() == "en" else "zh"


def admin_page_url(view: str, token: str, task_date: str, lang: str) -> str:
    paths = {
        "tasks": "/cleaning/admin",
        "cleaners": "/cleaning/admin/cleaners",
        "rules": "/cleaning/admin/rules",
    }
    query = urlencode({"token": token, "date": task_date, "lang": lang})
    return f"{paths.get(view, paths['tasks'])}?{query}"


def task_id(task_date: str, address: str, listing_id: str = "") -> str:
    raw = "|".join([task_date, clean_text(listing_id, 120).lower(), clean_text(address, 240).lower()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def default_task(task_date: str, address: str, listing_id: str = "", turnover: bool = False) -> dict[str, Any]:
    task_id_value = task_id(task_date, address, listing_id)
    return {
        "id": task_id_value,
        "date": task_date,
        "address": clean_text(address, 240),
        "listing_id": clean_text(listing_id, 120),
        "turnover": bool(turnover),
        "assigned_to": "",
        "status": "unassigned",
        "admin_note": "",
        "cleaner_note": "",
        "source": "manual",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }


def load_cleaners(store: Any) -> list[dict[str, Any]]:
    data = store.get_json(cleaners_key()) or {}
    rows = data.get("cleaners") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    cleaners = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cleaner_id = clean_text(row.get("id"), 80)
        name = clean_text(row.get("name"), 120)
        token = clean_text(row.get("token"), 160)
        if cleaner_id and name and token:
            cleaners.append(
                {
                    "id": cleaner_id,
                    "name": name,
                    "token": token,
                    "active": row.get("active") is not False,
                    "created_at": clean_text(row.get("created_at"), 80),
                    "updated_at": clean_text(row.get("updated_at"), 80),
                }
            )
    return cleaners


def save_cleaners(store: Any, cleaners: list[dict[str, Any]]) -> None:
    store.set_json(cleaners_key(), {"cleaners": cleaners, "updated_at": now_iso()})


def cleaner_public(cleaner: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": cleaner["id"],
        "name": cleaner["name"],
        "active": cleaner.get("active") is not False,
        "created_at": cleaner.get("created_at", ""),
        "updated_at": cleaner.get("updated_at", ""),
    }


def admin_cleaner_public(cleaner: dict[str, Any], base_url: str) -> dict[str, Any]:
    public = cleaner_public(cleaner)
    public["worker_url"] = worker_url(base_url, cleaner)
    return public


def worker_url(base_url: str, cleaner: dict[str, Any]) -> str:
    query = urlencode({"cleaner": cleaner["id"], "token": cleaner["token"]})
    return f"{base_url.rstrip('/')}/cleaning/worker?{query}"


def active_cleaner_ids(store: Any) -> set[str]:
    return {cleaner["id"] for cleaner in load_cleaners(store) if cleaner.get("active") is not False}


def active_assignment_rules(store: Any) -> list[dict[str, Any]]:
    cleaner_ids = active_cleaner_ids(store)
    return [rule for rule in load_assignment_rules(store) if rule.get("assigned_to") in cleaner_ids]


def normalized_match_text(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]+", "", clean_text(value, 500).upper())


def assignment_rule_tokens(match_text: str) -> list[str]:
    raw_tokens = re.split(r"[,，;；\n]+", str(match_text or ""))
    return [token for token in (normalized_match_text(raw) for raw in raw_tokens) if len(token) >= 2]


def normalize_assignment_rule(row: dict[str, Any]) -> dict[str, Any] | None:
    match_text = clean_multiline(row.get("match"), 500)
    assigned_to = clean_text(row.get("assigned_to"), 80)
    if not match_text or not assigned_to or not assignment_rule_tokens(match_text):
        return None
    rule_id = clean_text(row.get("id"), 80)
    if not rule_id:
        rule_id = hashlib.sha256(f"{match_text}|{assigned_to}|{time.time()}".encode()).hexdigest()[:12]
    name = clean_text(row.get("name"), 120) or clean_text(match_text.splitlines()[0], 120)
    return {
        "id": rule_id,
        "name": name,
        "match": match_text,
        "assigned_to": assigned_to,
        "active": row.get("active") is not False,
        "created_at": clean_text(row.get("created_at"), 80) or now_iso(),
        "updated_at": clean_text(row.get("updated_at"), 80) or now_iso(),
    }


def load_assignment_rules(store: Any) -> list[dict[str, Any]]:
    data = store.get_json(assignment_rules_key()) or {}
    rows = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    rules = []
    for row in rows:
        if isinstance(row, dict):
            rule = normalize_assignment_rule(row)
            if rule:
                rules.append(rule)
    return rules


def save_assignment_rules(store: Any, rules: list[dict[str, Any]]) -> None:
    normalized = [rule for rule in (normalize_assignment_rule(row) for row in rules) if rule]
    store.set_json(assignment_rules_key(), {"rules": normalized, "updated_at": now_iso()})


def assignment_rule_public(rule: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": rule["id"],
        "name": rule["name"],
        "match": rule["match"],
        "assigned_to": rule["assigned_to"],
        "active": rule.get("active") is not False,
        "created_at": rule.get("created_at", ""),
        "updated_at": rule.get("updated_at", ""),
    }


def find_assignment_rule(rules: list[dict[str, Any]], address: str) -> dict[str, Any] | None:
    address_text = normalized_match_text(address)
    if not address_text:
        return None
    for rule in rules:
        if rule.get("active") is False:
            continue
        for token in assignment_rule_tokens(rule.get("match", "")):
            if address_text.startswith(token) or token in address_text:
                return rule
    return None


def apply_assignment_rule(task: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any]:
    if task.get("assigned_to"):
        return task
    rule = find_assignment_rule(rules, task.get("address", ""))
    if not rule:
        task["assignment_rule_id"] = ""
        task["assignment_rule_name"] = ""
        return task
    task["assigned_to"] = rule["assigned_to"]
    if task.get("status") == "unassigned":
        task["status"] = "assigned"
    task["assignment_rule_id"] = rule["id"]
    task["assignment_rule_name"] = rule["name"]
    return task


def normalize_task(task_date: str, row: dict[str, Any]) -> dict[str, Any] | None:
    address = clean_text(row.get("address"), 240)
    if not address:
        return None
    listing_id = clean_text(row.get("listing_id"), 120)
    task = default_task(task_date, address, listing_id, bool(row.get("turnover")))
    task["id"] = clean_text(row.get("id"), 80) or task["id"]
    task["assigned_to"] = clean_text(row.get("assigned_to"), 80)
    task["status"] = clean_text(row.get("status"), 40) if row.get("status") in STATUS_VALUES else "unassigned"
    task["admin_note"] = clean_multiline(row.get("admin_note"), 1200)
    task["cleaner_note"] = clean_multiline(row.get("cleaner_note"), 1200)
    task["source"] = clean_text(row.get("source"), 40) or "manual"
    task["assignment_rule_id"] = clean_text(row.get("assignment_rule_id"), 80)
    task["assignment_rule_name"] = clean_text(row.get("assignment_rule_name"), 120)
    task["created_at"] = clean_text(row.get("created_at"), 80) or now_iso()
    task["updated_at"] = clean_text(row.get("updated_at"), 80) or now_iso()
    if task["assigned_to"] and task["status"] == "unassigned":
        task["status"] = "assigned"
    if not task["assigned_to"] and task["status"] == "assigned":
        task["status"] = "unassigned"
    return task


def load_tasks(store: Any, task_date: str) -> list[dict[str, Any]]:
    data = store.get_json(tasks_key(task_date)) or {}
    rows = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    tasks = []
    for row in rows:
        if isinstance(row, dict):
            task = normalize_task(task_date, row)
            if task:
                tasks.append(task)
    return sorted(tasks, key=lambda item: (item.get("status") == "done", item.get("address", "")))


def save_tasks(store: Any, task_date: str, tasks: list[dict[str, Any]]) -> None:
    normalized = [task for task in (normalize_task(task_date, row) for row in tasks) if task]
    store.set_json(tasks_key(task_date), {"date": task_date, "tasks": normalized, "updated_at": now_iso()})


def sync_tasks_from_snapshot(store: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    task_date = normalize_date(snapshot.get("report_date"))
    existing = {task["id"]: task for task in load_tasks(store, task_date)}
    assignment_rules = active_assignment_rules(store)
    synced_ids: set[str] = set()
    synced: list[dict[str, Any]] = []
    for item in snapshot.get("items", []):
        if not isinstance(item, dict):
            continue
        address = clean_text(item.get("address"), 240)
        if not address:
            continue
        listing_id = clean_text(item.get("listing_id"), 120)
        task = default_task(task_date, address, listing_id, bool(item.get("turnover")))
        previous = existing.get(task["id"])
        if previous:
            task["assigned_to"] = previous.get("assigned_to", "")
            task["status"] = previous.get("status", task["status"])
            task["admin_note"] = previous.get("admin_note", "")
            task["cleaner_note"] = previous.get("cleaner_note", "")
            task["assignment_rule_id"] = previous.get("assignment_rule_id", "")
            task["assignment_rule_name"] = previous.get("assignment_rule_name", "")
            task["created_at"] = previous.get("created_at", task["created_at"])
        else:
            task = apply_assignment_rule(task, assignment_rules)
        task["source"] = "guesty"
        task["updated_at"] = now_iso()
        synced_ids.add(task["id"])
        synced.append(task)
    manual_tasks = [
        task
        for task in existing.values()
        if task.get("source") == "manual" and task["id"] not in synced_ids
    ]
    save_tasks(store, task_date, manual_tasks + synced)
    return {"date": task_date, "synced": len(synced), "manual": len(manual_tasks)}


def is_admin(query: dict[str, list[str]]) -> bool:
    return token_matches(first_query(query, "token"), admin_token())


def get_worker(store: Any, query: dict[str, list[str]]) -> dict[str, Any] | None:
    cleaner_id = first_query(query, "cleaner")
    token = first_query(query, "token")
    for cleaner in load_cleaners(store):
        if cleaner.get("active") is False:
            continue
        if cleaner["id"] == cleaner_id and token_matches(token, cleaner["token"]):
            return cleaner
    return None


def read_json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("JSON body must be an object.")
    return parsed


def base_url(headers: dict[str, str]) -> str:
    proto = headers.get("x-forwarded-proto") or "https"
    host = headers.get("host") or "guesty-automation.onrender.com"
    return f"{proto}://{host}"


def app_state_for_admin(store: Any, query: dict[str, list[str]], headers: dict[str, str]) -> WebResponse:
    if not is_admin(query):
        return json_response(401, {"error": "unauthorized"})
    task_date = requested_date(query)
    cleaners = [admin_cleaner_public(cleaner, base_url(headers)) for cleaner in load_cleaners(store)]
    return json_response(
        200,
        {
            "role": "admin",
            "date": task_date,
            "statuses": STATUS_LABELS,
            "cleaners": cleaners,
            "assignment_rules": [assignment_rule_public(rule) for rule in load_assignment_rules(store)],
            "tasks": load_tasks(store, task_date),
        },
    )


def app_state_for_worker(store: Any, query: dict[str, list[str]]) -> WebResponse:
    worker = get_worker(store, query)
    if not worker:
        return json_response(401, {"error": "unauthorized"})
    task_date = requested_date(query)
    tasks = [task for task in load_tasks(store, task_date) if task.get("assigned_to") == worker["id"]]
    return json_response(
        200,
        {
            "role": "worker",
            "date": task_date,
            "statuses": STATUS_LABELS,
            "cleaner": cleaner_public(worker),
            "tasks": tasks,
        },
    )


def add_cleaner(store: Any, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    name = clean_text(body.get("name"), 120)
    if not name:
        raise ValueError("Cleaner name is required.")
    cleaners = load_cleaners(store)
    cleaner_id = clean_text(body.get("id"), 80) or hashlib.sha256(f"{name}|{time.time()}".encode()).hexdigest()[:10]
    if any(cleaner["id"] == cleaner_id for cleaner in cleaners):
        raise ValueError("Cleaner already exists.")
    cleaner = {
        "id": cleaner_id,
        "name": name,
        "token": clean_text(body.get("token"), 160) or secrets.token_urlsafe(18),
        "active": True,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    cleaners.append(cleaner)
    save_cleaners(store, cleaners)
    return {"cleaner": admin_cleaner_public(cleaner, base_url(headers))}


def update_cleaner(store: Any, body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    cleaner_id = clean_text(body.get("id"), 80)
    cleaners = load_cleaners(store)
    for cleaner in cleaners:
        if cleaner["id"] != cleaner_id:
            continue
        if "name" in body:
            cleaner["name"] = clean_text(body.get("name"), 120) or cleaner["name"]
        if "active" in body:
            cleaner["active"] = bool(body.get("active"))
        if body.get("rotate_token"):
            cleaner["token"] = secrets.token_urlsafe(18)
        cleaner["updated_at"] = now_iso()
        save_cleaners(store, cleaners)
        return {"cleaner": admin_cleaner_public(cleaner, base_url(headers))}
    raise ValueError("Cleaner not found.")


def validate_rule_cleaner(store: Any, cleaner_id: str) -> None:
    if cleaner_id not in active_cleaner_ids(store):
        raise ValueError("Active cleaner is required.")


def add_assignment_rule(store: Any, body: dict[str, Any]) -> dict[str, Any]:
    assigned_to = clean_text(body.get("assigned_to"), 80)
    validate_rule_cleaner(store, assigned_to)
    rule = normalize_assignment_rule(
        {
            "name": body.get("name"),
            "match": body.get("match"),
            "assigned_to": assigned_to,
            "active": True,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    )
    if not rule:
        raise ValueError("Address group and cleaner are required.")
    rules = load_assignment_rules(store)
    if any(existing.get("active") is not False and normalized_match_text(existing.get("match")) == normalized_match_text(rule["match"]) for existing in rules):
        raise ValueError("Assignment rule already exists.")
    rules.append(rule)
    save_assignment_rules(store, rules)
    return {"assignment_rule": assignment_rule_public(rule)}


def update_assignment_rule(store: Any, body: dict[str, Any]) -> dict[str, Any]:
    rule_id = clean_text(body.get("id"), 80)
    rules = load_assignment_rules(store)
    for rule in rules:
        if rule["id"] != rule_id:
            continue
        if "name" in body:
            rule["name"] = clean_text(body.get("name"), 120) or rule["name"]
        if "match" in body:
            rule["match"] = clean_multiline(body.get("match"), 500) or rule["match"]
        if "assigned_to" in body:
            assigned_to = clean_text(body.get("assigned_to"), 80)
            validate_rule_cleaner(store, assigned_to)
            rule["assigned_to"] = assigned_to
        if "active" in body:
            rule["active"] = bool(body.get("active"))
        rule["updated_at"] = now_iso()
        normalized = normalize_assignment_rule(rule)
        if not normalized:
            raise ValueError("Address group and cleaner are required.")
        rules = [normalized if existing["id"] == rule_id else existing for existing in rules]
        save_assignment_rules(store, rules)
        return {"assignment_rule": assignment_rule_public(normalized)}
    raise ValueError("Assignment rule not found.")


def delete_assignment_rule(store: Any, body: dict[str, Any]) -> dict[str, Any]:
    rule_id = clean_text(body.get("id"), 80)
    rules = load_assignment_rules(store)
    kept = [rule for rule in rules if rule["id"] != rule_id]
    if len(kept) == len(rules):
        raise ValueError("Assignment rule not found.")
    save_assignment_rules(store, kept)
    return {"deleted": rule_id}


def apply_assignment_rules_to_existing_tasks(store: Any, body: dict[str, Any]) -> dict[str, Any]:
    task_date = normalize_date(body.get("date"))
    rules = active_assignment_rules(store)
    tasks = load_tasks(store, task_date)
    changed = 0
    for task in tasks:
        before = (task.get("assigned_to", ""), task.get("status", ""), task.get("assignment_rule_id", ""))
        apply_assignment_rule(task, rules)
        after = (task.get("assigned_to", ""), task.get("status", ""), task.get("assignment_rule_id", ""))
        if after != before:
            task["updated_at"] = now_iso()
            changed += 1
    if changed:
        save_tasks(store, task_date, tasks)
    return {"date": task_date, "changed": changed}


def upsert_task(store: Any, body: dict[str, Any]) -> dict[str, Any]:
    task_date = normalize_date(body.get("date"))
    tasks = load_tasks(store, task_date)
    task_id_value = clean_text(body.get("id"), 80)
    existing = next((task for task in tasks if task["id"] == task_id_value), None)
    is_new = existing is None
    if not existing:
        address = clean_text(body.get("address"), 240)
        if not address:
            raise ValueError("Address is required.")
        existing = default_task(task_date, address, clean_text(body.get("listing_id"), 120), bool(body.get("turnover")))
        existing["source"] = "manual"
        tasks.append(existing)
    if "address" in body:
        existing["address"] = clean_text(body.get("address"), 240) or existing["address"]
    if "listing_id" in body:
        existing["listing_id"] = clean_text(body.get("listing_id"), 120)
    if "turnover" in body:
        existing["turnover"] = bool(body.get("turnover"))
    if "assigned_to" in body:
        assigned_to = clean_text(body.get("assigned_to"), 80)
        if assigned_to != existing.get("assigned_to", ""):
            existing["assignment_rule_id"] = ""
            existing["assignment_rule_name"] = ""
        existing["assigned_to"] = assigned_to
    if "status" in body and body.get("status") in STATUS_VALUES:
        existing["status"] = str(body["status"])
    if "admin_note" in body:
        existing["admin_note"] = clean_multiline(body.get("admin_note"), 1200)
    if is_new and "assigned_to" not in body:
        apply_assignment_rule(existing, active_assignment_rules(store))
    if existing["assigned_to"] and existing["status"] == "unassigned":
        existing["status"] = "assigned"
    if not existing["assigned_to"] and existing["status"] == "assigned":
        existing["status"] = "unassigned"
    existing["updated_at"] = now_iso()
    save_tasks(store, task_date, tasks)
    return {"task": existing}


def delete_task(store: Any, body: dict[str, Any]) -> dict[str, Any]:
    task_date = normalize_date(body.get("date"))
    task_id_value = clean_text(body.get("id"), 80)
    tasks = load_tasks(store, task_date)
    kept = [task for task in tasks if task["id"] != task_id_value]
    if len(kept) == len(tasks):
        raise ValueError("Task not found.")
    save_tasks(store, task_date, kept)
    return {"deleted": task_id_value}


def update_worker_task(store: Any, worker: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    task_date = normalize_date(body.get("date"))
    task_id_value = clean_text(body.get("id"), 80)
    tasks = load_tasks(store, task_date)
    for task in tasks:
        if task["id"] != task_id_value:
            continue
        if task.get("assigned_to") != worker["id"]:
            raise PermissionError("Task is not assigned to this cleaner.")
        if body.get("status") in {"assigned", "in_progress", "done", "issue"}:
            task["status"] = str(body["status"])
        if "cleaner_note" in body:
            task["cleaner_note"] = clean_multiline(body.get("cleaner_note"), 1200)
        task["updated_at"] = now_iso()
        save_tasks(store, task_date, tasks)
        return {"task": task}
    raise ValueError("Task not found.")


def handle_api_get(path: str, query: dict[str, list[str]], headers: dict[str, str]) -> WebResponse | None:
    store = get_state_store()
    if path == "/api/cleaning/admin":
        return app_state_for_admin(store, query, headers)
    if path == "/api/cleaning/worker":
        return app_state_for_worker(store, query)
    return None


def handle_api_post(path: str, query: dict[str, list[str]], headers: dict[str, str], body: bytes) -> WebResponse | None:
    store = get_state_store()
    try:
        payload = read_json_body(body)
        if path == "/api/cleaning/admin":
            if not is_admin(query):
                return json_response(401, {"error": "unauthorized"})
            action = clean_text(payload.get("action"), 80)
            if action == "add_cleaner":
                return json_response(200, add_cleaner(store, payload, headers))
            if action == "update_cleaner":
                return json_response(200, update_cleaner(store, payload, headers))
            if action == "add_assignment_rule":
                return json_response(200, add_assignment_rule(store, payload))
            if action == "update_assignment_rule":
                return json_response(200, update_assignment_rule(store, payload))
            if action == "delete_assignment_rule":
                return json_response(200, delete_assignment_rule(store, payload))
            if action == "apply_assignment_rules":
                return json_response(200, apply_assignment_rules_to_existing_tasks(store, payload))
            if action == "upsert_task":
                return json_response(200, upsert_task(store, payload))
            if action == "delete_task":
                return json_response(200, delete_task(store, payload))
            return json_response(400, {"error": "unknown_action"})
        if path == "/api/cleaning/worker":
            worker = get_worker(store, query)
            if not worker:
                return json_response(401, {"error": "unauthorized"})
            return json_response(200, update_worker_task(store, worker, payload))
    except PermissionError as exc:
        return json_response(403, {"error": str(exc)})
    except (ValueError, json.JSONDecodeError) as exc:
        return json_response(400, {"error": str(exc)})
    return None


def handle_cleaning_request(method: str, raw_path: str, headers: dict[str, str], body: bytes = b"") -> WebResponse | None:
    parsed = urlparse(raw_path)
    path = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)
    if method == "GET":
        if path == "/cleaning":
            return redirect_response("/cleaning/admin")
        admin_views = {
            "/cleaning/admin": "tasks",
            "/cleaning/admin/tasks": "tasks",
            "/cleaning/admin/cleaners": "cleaners",
            "/cleaning/admin/rules": "rules",
        }
        if path in admin_views:
            return html_response(
                render_admin_page(
                    first_query(query, "token"),
                    requested_date(query),
                    requested_lang(query),
                    admin_views[path],
                )
            )
        if path == "/cleaning/worker":
            return html_response(
                render_worker_page(
                    first_query(query, "cleaner"),
                    first_query(query, "token"),
                    requested_date(query),
                    requested_lang(query),
                )
            )
        return handle_api_get(path, query, headers)
    if method == "POST":
        return handle_api_post(path, query, headers, body)
    return None


def page_shell(title: str, body: str, config: dict[str, Any], script: str) -> str:
    doc_lang = "en" if config.get("lang") == "en" else "zh-CN"
    return f"""<!doctype html>
<html lang="{doc_lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #17202a;
      --muted: #637083;
      --line: #d9dee7;
      --accent: #0f766e;
      --danger: #b42318;
      --warn: #a15c00;
      --ok: #16794c;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
      line-height: 1.45;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 2;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 20px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.94);
      backdrop-filter: blur(10px);
    }}
    h1 {{ margin: 0; font-size: 20px; font-weight: 650; }}
    h2 {{ margin: 0 0 10px; font-size: 16px; font-weight: 650; }}
    main {{ max-width: 1180px; margin: 0 auto; padding: 18px; }}
    .toolbar {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
    .grid {{ display: grid; grid-template-columns: minmax(0, 1fr); gap: 14px; }}
    .admin-grid {{ grid-template-columns: minmax(0, 1fr); align-items: start; }}
    .nav {{
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }}
    .nav-link {{
      min-height: 36px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 7px 11px;
      text-decoration: none;
      font-weight: 650;
    }}
    .nav-link.active {{ background: #e7f5f2; border-color: #9ed8cf; color: var(--accent); }}
    section, dialog {{
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }}
    section {{ padding: 14px; }}
    label {{ display: grid; gap: 5px; color: var(--muted); font-size: 12px; font-weight: 600; }}
    input, select, textarea {{
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 7px 9px;
      font: inherit;
    }}
    textarea {{ min-height: 68px; resize: vertical; }}
    button {{
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      padding: 7px 11px;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }}
    button.primary {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
    button.danger {{ color: var(--danger); }}
    button:disabled {{ opacity: .55; cursor: default; }}
    .list {{ display: grid; gap: 8px; }}
    .task {{
      display: grid;
      grid-template-columns: minmax(210px, 1fr) 150px 140px minmax(160px, .75fr) auto;
      gap: 8px;
      align-items: start;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }}
    .worker-task {{ grid-template-columns: minmax(220px, 1fr) 150px minmax(180px, .75fr) auto; }}
    .address {{ font-weight: 650; }}
    .meta {{ color: var(--muted); font-size: 12px; margin-top: 3px; }}
    .pill {{
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 2px 8px;
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }}
    .pill.turnover {{ color: var(--warn); border-color: #f3c989; background: #fff8e6; }}
    .pill.done {{ color: var(--ok); border-color: #b8dfc7; background: #eefaf2; }}
    .row {{ display: grid; gap: 9px; }}
    .two {{ grid-template-columns: 1fr 1fr; }}
    .muted {{ color: var(--muted); }}
    .error {{ color: var(--danger); font-weight: 650; }}
    .empty {{ padding: 22px; color: var(--muted); text-align: center; border: 1px dashed var(--line); border-radius: 8px; }}
    .cleaner {{
      display: grid;
      gap: 8px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .copy-row {{ display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }}
    .copy-row input {{ font-size: 12px; }}
    .top-link {{ color: var(--accent); text-decoration: none; font-weight: 650; }}
    @media (max-width: 820px) {{
      header {{ align-items: stretch; flex-direction: column; }}
      main {{ padding: 12px; }}
      .admin-grid, .task, .worker-task, .two {{ grid-template-columns: 1fr; }}
      .toolbar {{ align-items: stretch; }}
      .toolbar > * {{ flex: 1 1 160px; }}
      .nav {{ align-items: stretch; }}
      .nav > * {{ flex: 1 1 140px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    <div class="toolbar">{body["header"] if isinstance(body, dict) else ""}</div>
  </header>
  <main>{body["main"] if isinstance(body, dict) else body}</main>
  <script>window.CLEANING_APP = {json.dumps(config, ensure_ascii=False)};</script>
  <script>{script}</script>
</body>
</html>"""


def render_admin_page(token: str, task_date: str, lang: str, view: str) -> str:
    lang = "en" if lang == "en" else "zh"
    view = view if view in {"tasks", "cleaners", "rules"} else "tasks"
    text = {
        "zh": {
            "title": "清洁任务后台",
            "date": "日期",
            "reload": "刷新",
            "tasks": "任务分配",
            "cleaners": "清洁员",
            "rules": "自动分配规则",
            "worker": "清洁员入口",
            "switch": "English",
            "add_task": "新增任务",
            "name": "姓名",
            "add_cleaner": "新增清洁员",
            "rule_name": "地址组名称",
            "rule_match": "匹配地址",
            "rule_placeholder": "1348 或 1348,1346",
            "assign_to": "分配给",
            "add_rule": "新增规则",
            "apply_rules": "套用到未分配任务",
        },
        "en": {
            "title": "Cleaning Admin",
            "date": "Date",
            "reload": "Refresh",
            "tasks": "Task Assignments",
            "cleaners": "Cleaners",
            "rules": "Auto Rules",
            "worker": "Worker Entry",
            "switch": "中文",
            "add_task": "Add Task",
            "name": "Name",
            "add_cleaner": "Add Cleaner",
            "rule_name": "Group Name",
            "rule_match": "Match Address",
            "rule_placeholder": "1348 or 1348,1346",
            "assign_to": "Assign To",
            "add_rule": "Add Rule",
            "apply_rules": "Apply to Unassigned Tasks",
        },
    }[lang]
    nav_items = [
        ("tasks", text["tasks"]),
        ("cleaners", text["cleaners"]),
        ("rules", text["rules"]),
    ]
    nav_links = "\n".join(
        f'<a class="nav-link {"active" if key == view else ""}" data-admin-view="{key}" href="{html.escape(admin_page_url(key, token, task_date, lang))}">{html.escape(label)}</a>'
        for key, label in nav_items
    )
    header = f"""
          <nav class="nav">{nav_links}</nav>
          <label>{html.escape(text['date'])}<input id="dateInput" type="date"></label>
          <button class="primary" id="reloadBtn">{html.escape(text['reload'])}</button>
          <button id="langToggle" type="button">{html.escape(text['switch'])}</button>
          <a class="top-link" href="/cleaning/worker">{html.escape(text['worker'])}</a>
        """
    if view == "cleaners":
        main = f"""
          <div class="grid admin-grid">
            <section>
              <h2>{html.escape(text['cleaners'])}</h2>
              <form id="addCleanerForm" class="row">
                <label>{html.escape(text['name'])}<input id="newCleanerName" autocomplete="off"></label>
                <button class="primary" type="submit">{html.escape(text['add_cleaner'])}</button>
              </form>
              <div id="message" class="muted" style="margin-top:10px"></div>
              <div id="cleanerList" class="list" style="margin-top:12px"></div>
            </section>
          </div>
        """
    elif view == "rules":
        main = f"""
          <div class="grid admin-grid">
            <section>
              <h2>{html.escape(text['rules'])}</h2>
              <form id="addRuleForm" class="row">
                <div class="two row">
                  <label>{html.escape(text['rule_name'])}<input id="newRuleName" autocomplete="off" placeholder="1348"></label>
                  <label>{html.escape(text['assign_to'])}<select id="newRuleCleaner"></select></label>
                </div>
                <label>{html.escape(text['rule_match'])}<input id="newRuleMatch" autocomplete="off" placeholder="{html.escape(text['rule_placeholder'])}"></label>
                <div class="toolbar">
                  <button class="primary" type="submit">{html.escape(text['add_rule'])}</button>
                  <button id="applyRulesBtn" type="button">{html.escape(text['apply_rules'])}</button>
                </div>
              </form>
              <div id="message" class="muted" style="margin-top:10px"></div>
              <div id="ruleList" class="list" style="margin-top:12px"></div>
            </section>
          </div>
        """
    else:
        main = f"""
          <div class="grid admin-grid">
            <section>
              <div class="toolbar" style="justify-content:space-between;margin-bottom:12px">
                <h2>{html.escape(text['tasks'])}</h2>
                <button class="primary" id="addTaskBtn">{html.escape(text['add_task'])}</button>
              </div>
              <div id="message" class="muted"></div>
              <div id="taskList" class="list" style="margin-top:10px"></div>
            </section>
          </div>
        """
    body = {
        "header": header,
        "main": main,
    }
    return page_shell(text["title"], body, {"token": token, "date": task_date, "lang": lang, "view": view}, ADMIN_SCRIPT)


def render_worker_page(cleaner_id: str, token: str, task_date: str, lang: str) -> str:
    lang = "en" if lang == "en" else "zh"
    text = {
        "zh": {
            "title": "我的清洁任务",
            "date": "日期",
            "reload": "刷新",
            "switch": "English",
            "tasks": "我的清洁任务",
        },
        "en": {
            "title": "My Cleaning Tasks",
            "date": "Date",
            "reload": "Refresh",
            "switch": "中文",
            "tasks": "My Cleaning Tasks",
        },
    }[lang]
    body = {
        "header": f"""
          <label>{html.escape(text['date'])}<input id="dateInput" type="date"></label>
          <button class="primary" id="reloadBtn">{html.escape(text['reload'])}</button>
          <button id="langToggle" type="button">{html.escape(text['switch'])}</button>
        """,
        "main": f"""
          <section>
            <div class="toolbar" style="justify-content:space-between;margin-bottom:12px">
              <h2 id="workerTitle">{html.escape(text['tasks'])}</h2>
              <span id="message" class="muted"></span>
            </div>
            <div id="taskList" class="list"></div>
          </section>
        """,
    }
    return page_shell(text["title"], body, {"cleaner": cleaner_id, "token": token, "date": task_date, "lang": lang}, WORKER_SCRIPT)


ADMIN_SCRIPT = r"""
const config = window.CLEANING_APP;
const state = { cleaners: [], assignment_rules: [], tasks: [], statuses: {} };
const lang = config.lang === "en" ? "en" : "zh";
const view = config.view || "tasks";
const copy = {
  zh: {
    addTaskPrompt: "输入清洁地址",
    allCleaners: "选择清洁员",
    applyDone: "已套用 {count} 个未分配任务",
    assigned: "已分配",
    copied: "链接已复制",
    copyLink: "复制链接",
    deleteRuleConfirm: "删除这个规则？",
    deleteTaskConfirm: "删除这个任务？",
    emptyCleaners: "暂无清洁员",
    emptyRules: "暂无规则",
    emptyTasks: "暂无任务",
    inactive: "停用",
    matchAddress: "匹配地址",
    nameLabel: "名称",
    loadedCleaners: "已加载 {count} 个清洁员",
    loadedRules: "已加载 {count} 条规则",
    loadedTasks: "已加载 {count} 个任务",
    loading: "读取中...",
    manual: "manual",
    noCleaner: "未分配",
    ordinaryClean: "普通清洁",
    refreshFailed: "读取失败",
    resetLink: "重置链接",
    assignTo: "分配给",
    rule: "规则",
    save: "保存",
    sameDay: "同日入住",
    sourceGuesty: "Guesty",
    sourceManual: "手动",
    status_assigned: "已分配",
    status_done: "已完成",
    status_in_progress: "进行中",
    status_issue: "有问题",
    status_unassigned: "未分配",
    stop: "停用",
    start: "启用",
    unassignedOption: "未分配",
    active: "启用",
    delete: "删除",
    note: "备注",
  },
  en: {
    addTaskPrompt: "Enter the cleaning address",
    allCleaners: "Select cleaner",
    applyDone: "Applied rules to {count} unassigned task(s)",
    assigned: "Assigned",
    copied: "Link copied",
    copyLink: "Copy Link",
    deleteRuleConfirm: "Delete this rule?",
    deleteTaskConfirm: "Delete this task?",
    emptyCleaners: "No cleaners yet",
    emptyRules: "No rules yet",
    emptyTasks: "No tasks yet",
    inactive: "Inactive",
    matchAddress: "Match Address",
    nameLabel: "Name",
    loadedCleaners: "Loaded {count} cleaner(s)",
    loadedRules: "Loaded {count} rule(s)",
    loadedTasks: "Loaded {count} task(s)",
    loading: "Loading...",
    manual: "manual",
    noCleaner: "Unassigned",
    ordinaryClean: "Standard Clean",
    refreshFailed: "Could not load",
    resetLink: "Reset Link",
    assignTo: "Assign To",
    rule: "Rule",
    save: "Save",
    sameDay: "Same-day Turnover",
    sourceGuesty: "Guesty",
    sourceManual: "Manual",
    status_assigned: "Assigned",
    status_done: "Done",
    status_in_progress: "In Progress",
    status_issue: "Issue",
    status_unassigned: "Unassigned",
    stop: "Disable",
    start: "Enable",
    unassignedOption: "Unassigned",
    active: "Active",
    delete: "Delete",
    note: "Notes",
  },
};
const statusOrder = ["unassigned", "assigned", "in_progress", "done", "issue"];
const adminPaths = { tasks: "/cleaning/admin", cleaners: "/cleaning/admin/cleaners", rules: "/cleaning/admin/rules" };
const t = (key) => copy[lang][key] || copy.zh[key] || key;
const fmt = (template, values) => template.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? "");
const qs = () => new URLSearchParams({ token: config.token || "", date: document.querySelector("#dateInput").value, lang });
const api = () => `/api/cleaning/admin?${qs().toString()}`;
const el = (id) => document.querySelector(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const say = (text, error=false) => {
  const message = el("#message");
  if (!message) return;
  message.textContent = text;
  message.className = error ? "error" : "muted";
};

document.addEventListener("DOMContentLoaded", () => {
  el("#dateInput").value = config.date;
  el("#reloadBtn").addEventListener("click", load);
  el("#dateInput").addEventListener("change", () => {
    updateAdminLinks();
    load();
  });
  el("#langToggle").addEventListener("click", switchLang);
  if (el("#addCleanerForm")) el("#addCleanerForm").addEventListener("submit", addCleaner);
  if (el("#addRuleForm")) el("#addRuleForm").addEventListener("submit", addRule);
  if (el("#applyRulesBtn")) el("#applyRulesBtn").addEventListener("click", applyRules);
  if (el("#addTaskBtn")) el("#addTaskBtn").addEventListener("click", () => saveTask({ address: prompt(t("addTaskPrompt")) || "" }));
  updateAdminLinks();
  load();
});

function adminUrl(targetView, targetLang = lang) {
  const params = new URLSearchParams({
    token: config.token || "",
    date: el("#dateInput").value || config.date,
    lang: targetLang,
  });
  return `${adminPaths[targetView] || adminPaths.tasks}?${params.toString()}`;
}

function updateAdminLinks() {
  document.querySelectorAll("[data-admin-view]").forEach((link) => {
    link.href = adminUrl(link.dataset.adminView);
  });
}

function switchLang() {
  location.href = adminUrl(view, lang === "en" ? "zh" : "en");
}

async function request(path, body) {
  const res = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

async function load() {
  try {
    say(t("loading"));
    const data = await request(api());
    Object.assign(state, data);
    if (el("#cleanerList")) renderCleaners();
    if (el("#ruleList") || el("#newRuleCleaner")) renderRules();
    if (el("#taskList")) renderTasks();
    if (view === "cleaners") {
      say(fmt(t("loadedCleaners"), { count: state.cleaners.length }));
    } else if (view === "rules") {
      say(fmt(t("loadedRules"), { count: state.assignment_rules.length }));
    } else {
      say(fmt(t("loadedTasks"), { count: state.tasks.length }));
    }
  } catch (err) {
    say(err.message, true);
  }
}

async function addCleaner(event) {
  event.preventDefault();
  const name = el("#newCleanerName").value.trim();
  if (!name) return;
  try {
    await request(api(), { action: "add_cleaner", name });
    el("#newCleanerName").value = "";
    await load();
  } catch (err) {
    say(err.message, true);
  }
}

function renderCleaners() {
  const box = el("#cleanerList");
  if (!state.cleaners.length) {
    box.innerHTML = `<div class="empty">${esc(t("emptyCleaners"))}</div>`;
    return;
  }
  box.innerHTML = state.cleaners.map((cleaner) => `
    <div class="cleaner">
      <div><strong>${esc(cleaner.name)}</strong> <span class="pill">${cleaner.active ? esc(t("active")) : esc(t("inactive"))}</span></div>
      <div class="copy-row">
        <input readonly value="${esc(cleaner.worker_url)}">
        <button type="button" data-copy="${esc(cleaner.worker_url)}">${esc(t("copyLink"))}</button>
      </div>
      <div class="toolbar">
        <button type="button" data-rotate="${esc(cleaner.id)}">${esc(t("resetLink"))}</button>
        <button type="button" data-toggle="${esc(cleaner.id)}">${cleaner.active ? esc(t("stop")) : esc(t("start"))}</button>
      </div>
    </div>
  `).join("");
  box.querySelectorAll("[data-copy]").forEach((button) => button.addEventListener("click", async () => {
    await navigator.clipboard.writeText(button.dataset.copy);
    say(t("copied"));
  }));
  box.querySelectorAll("[data-rotate]").forEach((button) => button.addEventListener("click", () => updateCleaner(button.dataset.rotate, { rotate_token: true })));
  box.querySelectorAll("[data-toggle]").forEach((button) => {
    const cleaner = state.cleaners.find((row) => row.id === button.dataset.toggle);
    button.addEventListener("click", () => updateCleaner(button.dataset.toggle, { active: !cleaner.active }));
  });
}

async function updateCleaner(id, fields) {
  try {
    await request(api(), { action: "update_cleaner", id, ...fields });
    await load();
  } catch (err) {
    say(err.message, true);
  }
}

function activeCleanerOptions(selected = "") {
  const options = [`<option value="">${esc(t("allCleaners"))}</option>`].concat(
    state.cleaners
      .filter((cleaner) => cleaner.active || cleaner.id === selected)
      .map((cleaner) => `<option value="${esc(cleaner.id)}"${cleaner.id === selected ? " selected" : ""}>${esc(cleaner.name)}${cleaner.active ? "" : ` (${esc(t("inactive"))})`}</option>`)
  );
  return options.join("");
}

function cleanerName(id) {
  const cleaner = state.cleaners.find((item) => item.id === id);
  return cleaner ? cleaner.name : t("noCleaner");
}

function renderRules() {
  if (el("#newRuleCleaner")) el("#newRuleCleaner").innerHTML = activeCleanerOptions();
  const box = el("#ruleList");
  if (!box) return;
  if (!state.assignment_rules.length) {
    box.innerHTML = `<div class="empty">${esc(t("emptyRules"))}</div>`;
    return;
  }
  box.innerHTML = state.assignment_rules.map((rule) => `
    <div class="cleaner" data-rule-id="${esc(rule.id)}">
      <label>${esc(t("nameLabel"))}<input data-rule-field="name" value="${esc(rule.name)}"></label>
      <label>${esc(t("matchAddress"))}<input data-rule-field="match" value="${esc(rule.match)}"></label>
      <label>${esc(t("assignTo"))}<select data-rule-field="assigned_to">${activeCleanerOptions(rule.assigned_to)}</select></label>
      <div class="toolbar">
        <span class="pill">${rule.active ? esc(t("active")) : esc(t("inactive"))}</span>
        <button class="primary" type="button" data-rule-save="${esc(rule.id)}">${esc(t("save"))}</button>
        <button type="button" data-rule-toggle="${esc(rule.id)}">${rule.active ? esc(t("stop")) : esc(t("start"))}</button>
        <button class="danger" type="button" data-rule-delete="${esc(rule.id)}">${esc(t("delete"))}</button>
      </div>
      <div class="meta">${esc(rule.match)} -> ${esc(cleanerName(rule.assigned_to))}</div>
    </div>
  `).join("");
  box.querySelectorAll("[data-rule-save]").forEach((button) => button.addEventListener("click", () => saveRule(button.dataset.ruleSave)));
  box.querySelectorAll("[data-rule-toggle]").forEach((button) => {
    const rule = state.assignment_rules.find((item) => item.id === button.dataset.ruleToggle);
    button.addEventListener("click", () => updateRule(button.dataset.ruleToggle, { active: !rule.active }));
  });
  box.querySelectorAll("[data-rule-delete]").forEach((button) => button.addEventListener("click", () => deleteRule(button.dataset.ruleDelete)));
}

async function addRule(event) {
  event.preventDefault();
  const name = el("#newRuleName").value.trim();
  const match = el("#newRuleMatch").value.trim();
  const assigned_to = el("#newRuleCleaner").value;
  if (!match || !assigned_to) return;
  try {
    await request(api(), { action: "add_assignment_rule", name, match, assigned_to });
    el("#newRuleName").value = "";
    el("#newRuleMatch").value = "";
    el("#newRuleCleaner").value = "";
    await load();
  } catch (err) {
    say(err.message, true);
  }
}

function rulePayload(id) {
  const row = el(`#ruleList [data-rule-id="${CSS.escape(id)}"]`);
  return {
    action: "update_assignment_rule",
    id,
    name: row.querySelector('[data-rule-field="name"]').value,
    match: row.querySelector('[data-rule-field="match"]').value,
    assigned_to: row.querySelector('[data-rule-field="assigned_to"]').value,
  };
}

async function saveRule(id) {
  try {
    await request(api(), rulePayload(id));
    await load();
  } catch (err) {
    say(err.message, true);
  }
}

async function updateRule(id, fields) {
  try {
    await request(api(), { action: "update_assignment_rule", id, ...fields });
    await load();
  } catch (err) {
    say(err.message, true);
  }
}

async function deleteRule(id) {
  if (!confirm(t("deleteRuleConfirm"))) return;
  try {
    await request(api(), { action: "delete_assignment_rule", id });
    await load();
  } catch (err) {
    say(err.message, true);
  }
}

async function applyRules() {
  try {
    const data = await request(api(), { action: "apply_assignment_rules", date: el("#dateInput").value });
    await load();
    say(fmt(t("applyDone"), { count: data.changed || 0 }));
  } catch (err) {
    say(err.message, true);
  }
}

function sourceLabel(source) {
  if (source === "guesty") return t("sourceGuesty");
  if (source === "manual") return t("sourceManual");
  return source || "";
}

function statusLabel(value) {
  return t(`status_${value}`) || state.statuses[value] || value;
}

function renderTasks() {
  const box = el("#taskList");
  if (!state.tasks.length) {
    box.innerHTML = `<div class="empty">${esc(t("emptyTasks"))}</div>`;
    return;
  }
  box.innerHTML = state.tasks.map((task) => `
    <div class="task" data-id="${esc(task.id)}">
      <div>
        <div class="address">${esc(task.address)}</div>
        <div class="meta">${task.turnover ? `<span class="pill turnover">${esc(t("sameDay"))}</span>` : `<span class="pill">${esc(t("ordinaryClean"))}</span>`} <span class="pill">${esc(sourceLabel(task.source))}</span> ${task.assignment_rule_name ? `<span class="pill">${esc(t("rule"))} ${esc(task.assignment_rule_name)}</span>` : ""}</div>
      </div>
      <select data-field="assigned_to">${taskCleanerOptions(task.assigned_to || "")}</select>
      <select data-field="status">${statusOptions(task.status || "unassigned")}</select>
      <textarea data-field="admin_note" placeholder="${esc(t("note"))}">${esc(task.admin_note || "")}</textarea>
      <div class="toolbar">
        <button class="primary" type="button" data-save="${esc(task.id)}">${esc(t("save"))}</button>
        <button class="danger" type="button" data-delete="${esc(task.id)}">${esc(t("delete"))}</button>
      </div>
    </div>
  `).join("");
  for (const task of state.tasks) {
    const row = box.querySelector(`[data-id="${CSS.escape(task.id)}"]`);
    row.querySelector('[data-field="assigned_to"]').value = task.assigned_to || "";
    row.querySelector('[data-field="status"]').value = task.status || "unassigned";
  }
  box.querySelectorAll("[data-save]").forEach((button) => button.addEventListener("click", () => saveRow(button.dataset.save)));
  box.querySelectorAll("[data-delete]").forEach((button) => button.addEventListener("click", () => deleteTask(button.dataset.delete)));
}

function taskCleanerOptions(selected = "") {
  const choices = [`<option value=""${selected ? "" : " selected"}>${esc(t("unassignedOption"))}</option>`].concat(
    state.cleaners
      .filter((cleaner) => cleaner.active || cleaner.id === selected)
      .map((cleaner) => `<option value="${esc(cleaner.id)}"${cleaner.id === selected ? " selected" : ""}>${esc(cleaner.name)}${cleaner.active ? "" : ` (${esc(t("inactive"))})`}</option>`)
  );
  return choices.join("");
}

function statusOptions(selected = "unassigned") {
  return statusOrder.map((value) => `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(statusLabel(value))}</option>`).join("");
}

function rowPayload(id) {
  const task = state.tasks.find((item) => item.id === id);
  const row = el(`#taskList [data-id="${CSS.escape(id)}"]`);
  return {
    action: "upsert_task",
    date: el("#dateInput").value,
    id,
    address: task.address,
    listing_id: task.listing_id || "",
    turnover: Boolean(task.turnover),
    assigned_to: row.querySelector('[data-field="assigned_to"]').value,
    status: row.querySelector('[data-field="status"]').value,
    admin_note: row.querySelector('[data-field="admin_note"]').value,
  };
}

async function saveRow(id) {
  try {
    await request(api(), rowPayload(id));
    await load();
  } catch (err) {
    say(err.message, true);
  }
}

async function saveTask(fields) {
  const address = (fields.address || "").trim();
  if (!address) return;
  try {
    await request(api(), { action: "upsert_task", date: el("#dateInput").value, address, ...fields });
    await load();
  } catch (err) {
    say(err.message, true);
  }
}

async function deleteTask(id) {
  if (!confirm(t("deleteTaskConfirm"))) return;
  try {
    await request(api(), { action: "delete_task", date: el("#dateInput").value, id });
    await load();
  } catch (err) {
    say(err.message, true);
  }
}
"""


WORKER_SCRIPT = r"""
const config = window.CLEANING_APP;
const state = { tasks: [], statuses: {}, cleaner: null };
const lang = config.lang === "en" ? "en" : "zh";
const copy = {
  zh: {
    done: "已完成",
    emptyTasks: "今天没有分配给你的任务",
    loadedTasks: "已加载 {count} 个任务",
    loading: "读取中...",
    note: "备注",
    ordinaryClean: "普通清洁",
    sameDay: "同日入住",
    status_assigned: "已分配",
    status_done: "已完成",
    status_in_progress: "进行中",
    status_issue: "有问题",
    status_unassigned: "未分配",
    taskTitle: "{name} 的清洁任务",
    unableToLoad: "无法读取任务",
    update: "更新",
  },
  en: {
    done: "Done",
    emptyTasks: "No tasks assigned to you today",
    loadedTasks: "Loaded {count} task(s)",
    loading: "Loading...",
    note: "Notes",
    ordinaryClean: "Standard Clean",
    sameDay: "Same-day Turnover",
    status_assigned: "Assigned",
    status_done: "Done",
    status_in_progress: "In Progress",
    status_issue: "Issue",
    status_unassigned: "Unassigned",
    taskTitle: "{name}'s Cleaning Tasks",
    unableToLoad: "Could not load tasks",
    update: "Update",
  },
};
const statusOrder = ["assigned", "in_progress", "done", "issue"];
const t = (key) => copy[lang][key] || copy.zh[key] || key;
const fmt = (template, values) => template.replace(/\{(\w+)\}/g, (_, key) => values[key] ?? "");
const qs = () => new URLSearchParams({ cleaner: config.cleaner || "", token: config.token || "", date: document.querySelector("#dateInput").value, lang });
const api = () => `/api/cleaning/worker?${qs().toString()}`;
const el = (id) => document.querySelector(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const say = (text, error=false) => { el("#message").textContent = text; el("#message").className = error ? "error" : "muted"; };

document.addEventListener("DOMContentLoaded", () => {
  el("#dateInput").value = config.date;
  el("#reloadBtn").addEventListener("click", load);
  el("#dateInput").addEventListener("change", load);
  el("#langToggle").addEventListener("click", switchLang);
  load();
});

function switchLang() {
  const params = new URLSearchParams({
    cleaner: config.cleaner || "",
    token: config.token || "",
    date: el("#dateInput").value || config.date,
    lang: lang === "en" ? "zh" : "en",
  });
  location.href = `/cleaning/worker?${params.toString()}`;
}

async function request(body) {
  const res = await fetch(api(), {
    method: body ? "POST" : "GET",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

async function load() {
  try {
    say(t("loading"));
    const data = await request();
    Object.assign(state, data);
    el("#workerTitle").textContent = fmt(t("taskTitle"), { name: state.cleaner.name });
    renderTasks();
    say(fmt(t("loadedTasks"), { count: state.tasks.length }));
  } catch (err) {
    say(err.message, true);
    el("#taskList").innerHTML = `<div class="empty">${esc(t("unableToLoad"))}</div>`;
  }
}

function statusLabel(value) {
  return t(`status_${value}`) || state.statuses[value] || value;
}

function renderTasks() {
  const box = el("#taskList");
  if (!state.tasks.length) {
    box.innerHTML = `<div class="empty">${esc(t("emptyTasks"))}</div>`;
    return;
  }
  box.innerHTML = state.tasks.map((task) => `
    <div class="task worker-task" data-id="${esc(task.id)}">
      <div>
        <div class="address">${esc(task.address)}</div>
        <div class="meta">${task.turnover ? `<span class="pill turnover">${esc(t("sameDay"))}</span>` : `<span class="pill">${esc(t("ordinaryClean"))}</span>`} ${task.status === "done" ? `<span class="pill done">${esc(t("done"))}</span>` : ""}</div>
        ${task.admin_note ? `<div class="meta">${esc(task.admin_note)}</div>` : ""}
      </div>
      <select data-field="status">${statusOptions(task.status === "unassigned" ? "assigned" : task.status)}</select>
      <textarea data-field="cleaner_note" placeholder="${esc(t("note"))}">${esc(task.cleaner_note || "")}</textarea>
      <button class="primary" type="button" data-save="${esc(task.id)}">${esc(t("update"))}</button>
    </div>
  `).join("");
  for (const task of state.tasks) {
    const row = box.querySelector(`[data-id="${CSS.escape(task.id)}"]`);
    row.querySelector('[data-field="status"]').value = task.status === "unassigned" ? "assigned" : task.status;
  }
  box.querySelectorAll("[data-save]").forEach((button) => button.addEventListener("click", () => saveTask(button.dataset.save)));
}

function statusOptions(selected) {
  return statusOrder.map((value) => `<option value="${esc(value)}"${value === selected ? " selected" : ""}>${esc(statusLabel(value))}</option>`).join("");
}

async function saveTask(id) {
  const row = el(`#taskList [data-id="${CSS.escape(id)}"]`);
  try {
    await request({
      id,
      date: el("#dateInput").value,
      status: row.querySelector('[data-field="status"]').value,
      cleaner_note: row.querySelector('[data-field="cleaner_note"]').value,
    });
    await load();
  } catch (err) {
    say(err.message, true);
  }
}
"""
