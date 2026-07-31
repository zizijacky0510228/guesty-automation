#!/usr/bin/env python3
"""Small cleaning task web app mounted by the Guesty webhook service."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
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
            task["created_at"] = previous.get("created_at", task["created_at"])
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


def upsert_task(store: Any, body: dict[str, Any]) -> dict[str, Any]:
    task_date = normalize_date(body.get("date"))
    tasks = load_tasks(store, task_date)
    task_id_value = clean_text(body.get("id"), 80)
    existing = next((task for task in tasks if task["id"] == task_id_value), None)
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
        existing["assigned_to"] = clean_text(body.get("assigned_to"), 80)
    if "status" in body and body.get("status") in STATUS_VALUES:
        existing["status"] = str(body["status"])
    if "admin_note" in body:
        existing["admin_note"] = clean_multiline(body.get("admin_note"), 1200)
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
        if path == "/cleaning/admin":
            return html_response(render_admin_page(first_query(query, "token"), requested_date(query)))
        if path == "/cleaning/worker":
            return html_response(render_worker_page(first_query(query, "cleaner"), first_query(query, "token"), requested_date(query)))
        return handle_api_get(path, query, headers)
    if method == "POST":
        return handle_api_post(path, query, headers, body)
    return None


def page_shell(title: str, body: str, config: dict[str, Any], script: str) -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
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
    .admin-grid {{ grid-template-columns: 280px minmax(0, 1fr); align-items: start; }}
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


def render_admin_page(token: str, task_date: str) -> str:
    body = {
        "header": """
          <label>日期<input id="dateInput" type="date"></label>
          <button class="primary" id="reloadBtn">刷新</button>
          <a class="top-link" href="/cleaning/worker">清洁员入口</a>
        """,
        "main": """
          <div class="grid admin-grid">
            <section>
              <h2>清洁员</h2>
              <form id="addCleanerForm" class="row">
                <label>姓名<input id="newCleanerName" autocomplete="off"></label>
                <button class="primary" type="submit">新增清洁员</button>
              </form>
              <div id="cleanerList" class="list" style="margin-top:12px"></div>
            </section>
            <section>
              <div class="toolbar" style="justify-content:space-between;margin-bottom:12px">
                <h2>任务分配</h2>
                <button class="primary" id="addTaskBtn">新增任务</button>
              </div>
              <div id="message" class="muted"></div>
              <div id="taskList" class="list" style="margin-top:10px"></div>
            </section>
          </div>
        """,
    }
    return page_shell("清洁任务后台", body, {"token": token, "date": task_date}, ADMIN_SCRIPT)


def render_worker_page(cleaner_id: str, token: str, task_date: str) -> str:
    body = {
        "header": """
          <label>日期<input id="dateInput" type="date"></label>
          <button class="primary" id="reloadBtn">刷新</button>
        """,
        "main": """
          <section>
            <div class="toolbar" style="justify-content:space-between;margin-bottom:12px">
              <h2 id="workerTitle">我的清洁任务</h2>
              <span id="message" class="muted"></span>
            </div>
            <div id="taskList" class="list"></div>
          </section>
        """,
    }
    return page_shell("我的清洁任务", body, {"cleaner": cleaner_id, "token": token, "date": task_date}, WORKER_SCRIPT)


ADMIN_SCRIPT = r"""
const config = window.CLEANING_APP;
const state = { cleaners: [], tasks: [], statuses: {} };
const qs = () => new URLSearchParams({ token: config.token || "", date: document.querySelector("#dateInput").value });
const api = () => `/api/cleaning/admin?${qs().toString()}`;
const el = (id) => document.querySelector(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const say = (text, error=false) => { el("#message").textContent = text; el("#message").className = error ? "error" : "muted"; };

document.addEventListener("DOMContentLoaded", () => {
  el("#dateInput").value = config.date;
  el("#reloadBtn").addEventListener("click", load);
  el("#dateInput").addEventListener("change", load);
  el("#addCleanerForm").addEventListener("submit", addCleaner);
  el("#addTaskBtn").addEventListener("click", () => saveTask({ address: prompt("输入清洁地址") || "" }));
  load();
});

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
    say("读取中...");
    const data = await request(api());
    Object.assign(state, data);
    renderCleaners();
    renderTasks();
    say(`已加载 ${state.tasks.length} 个任务`);
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
    box.innerHTML = `<div class="empty">暂无清洁员</div>`;
    return;
  }
  box.innerHTML = state.cleaners.map((cleaner) => `
    <div class="cleaner">
      <div><strong>${esc(cleaner.name)}</strong> <span class="pill">${cleaner.active ? "启用" : "停用"}</span></div>
      <div class="copy-row">
        <input readonly value="${esc(cleaner.worker_url)}">
        <button type="button" data-copy="${esc(cleaner.worker_url)}">复制链接</button>
      </div>
      <div class="toolbar">
        <button type="button" data-rotate="${esc(cleaner.id)}">重置链接</button>
        <button type="button" data-toggle="${esc(cleaner.id)}">${cleaner.active ? "停用" : "启用"}</button>
      </div>
    </div>
  `).join("");
  box.querySelectorAll("[data-copy]").forEach((button) => button.addEventListener("click", () => navigator.clipboard.writeText(button.dataset.copy)));
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

function renderTasks() {
  const box = el("#taskList");
  if (!state.tasks.length) {
    box.innerHTML = `<div class="empty">暂无任务</div>`;
    return;
  }
  const cleanerOptions = [`<option value="">未分配</option>`].concat(state.cleaners.filter(c => c.active).map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`)).join("");
  const statusOptions = Object.entries(state.statuses).map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("");
  box.innerHTML = state.tasks.map((task) => `
    <div class="task" data-id="${esc(task.id)}">
      <div>
        <div class="address">${esc(task.address)}</div>
        <div class="meta">${task.turnover ? `<span class="pill turnover">同日入住</span>` : `<span class="pill">普通清洁</span>`} <span class="pill">${esc(task.source)}</span></div>
      </div>
      <select data-field="assigned_to">${cleanerOptions}</select>
      <select data-field="status">${statusOptions}</select>
      <textarea data-field="admin_note" placeholder="备注">${esc(task.admin_note || "")}</textarea>
      <div class="toolbar">
        <button class="primary" type="button" data-save="${esc(task.id)}">保存</button>
        <button class="danger" type="button" data-delete="${esc(task.id)}">删除</button>
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
  if (!confirm("删除这个任务？")) return;
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
const qs = () => new URLSearchParams({ cleaner: config.cleaner || "", token: config.token || "", date: document.querySelector("#dateInput").value });
const api = () => `/api/cleaning/worker?${qs().toString()}`;
const el = (id) => document.querySelector(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const say = (text, error=false) => { el("#message").textContent = text; el("#message").className = error ? "error" : "muted"; };

document.addEventListener("DOMContentLoaded", () => {
  el("#dateInput").value = config.date;
  el("#reloadBtn").addEventListener("click", load);
  el("#dateInput").addEventListener("change", load);
  load();
});

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
    say("读取中...");
    const data = await request();
    Object.assign(state, data);
    el("#workerTitle").textContent = `${state.cleaner.name} 的清洁任务`;
    renderTasks();
    say(`已加载 ${state.tasks.length} 个任务`);
  } catch (err) {
    say(err.message, true);
    el("#taskList").innerHTML = `<div class="empty">无法读取任务</div>`;
  }
}

function renderTasks() {
  const box = el("#taskList");
  if (!state.tasks.length) {
    box.innerHTML = `<div class="empty">今天没有分配给你的任务</div>`;
    return;
  }
  const statusOptions = Object.entries(state.statuses)
    .filter(([value]) => value !== "unassigned")
    .map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("");
  box.innerHTML = state.tasks.map((task) => `
    <div class="task worker-task" data-id="${esc(task.id)}">
      <div>
        <div class="address">${esc(task.address)}</div>
        <div class="meta">${task.turnover ? `<span class="pill turnover">同日入住</span>` : `<span class="pill">普通清洁</span>`} ${task.status === "done" ? `<span class="pill done">已完成</span>` : ""}</div>
        ${task.admin_note ? `<div class="meta">${esc(task.admin_note)}</div>` : ""}
      </div>
      <select data-field="status">${statusOptions}</select>
      <textarea data-field="cleaner_note" placeholder="备注">${esc(task.cleaner_note || "")}</textarea>
      <button class="primary" type="button" data-save="${esc(task.id)}">更新</button>
    </div>
  `).join("");
  for (const task of state.tasks) {
    const row = box.querySelector(`[data-id="${CSS.escape(task.id)}"]`);
    row.querySelector('[data-field="status"]').value = task.status === "unassigned" ? "assigned" : task.status;
  }
  box.querySelectorAll("[data-save]").forEach((button) => button.addEventListener("click", () => saveTask(button.dataset.save)));
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
