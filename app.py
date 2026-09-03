#!/usr/bin/env python3
"""Small self-hosted UR vacancy monitor with email notifications."""

from __future__ import annotations

import html
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import smtplib
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from ur_check import DEFAULT_URL, fetch_rooms, filtered


def load_env_file(path: Path) -> int:
    """Load a small dotenv-style file without overriding real environment variables."""
    if not path.is_file():
        return 0
    loaded = 0
    with path.open(encoding="utf-8-sig") as config:
        for line_number, raw_line in enumerate(config, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                raise ValueError(f"配置文件第 {line_number} 行缺少等号")
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"配置文件第 {line_number} 行变量名无效: {key!r}")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if key not in os.environ:
                os.environ[key] = value
                loaded += 1
    return loaded


CONFIG_FILE = Path(os.getenv("CONFIG_FILE", "/config/ur-monitor.env"))
CONFIG_VALUES_LOADED = load_env_file(CONFIG_FILE)
LOG = logging.getLogger("ur-monitor")
DB_PATH = Path(os.getenv("DATABASE_PATH", "/data/ur-monitor.db"))
PROPERTY_URLS = [u.strip() for u in os.getenv("UR_URLS", DEFAULT_URL).split(",") if u.strip()]
CHECK_INTERVAL = max(300, int(os.getenv("CHECK_INTERVAL_SECONDS", "600")))
MIN_RENT = int(os.environ["MIN_RENT"]) if os.getenv("MIN_RENT") else None
MAX_RENT = int(os.environ["MAX_RENT"]) if os.getenv("MAX_RENT") else None
LAYOUTS = [x.strip() for x in os.getenv("LAYOUTS", "").split(",") if x.strip()]
NOTIFY_ON_FIRST_RUN = os.getenv("NOTIFY_ON_FIRST_RUN", "false").lower() in {"1", "true", "yes"}
PORT = int(os.getenv("PORT", "8080"))
LOG_PATH = Path(os.getenv("LOG_PATH", "/data/ur-monitor.log"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", "5242880"))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))
JST = timezone(timedelta(hours=9), name="JST")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS room_state (
        property_url TEXT NOT NULL, room_id TEXT NOT NULL, payload TEXT NOT NULL,
        active INTEGER NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        PRIMARY KEY (property_url, room_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS checks (
        property_url TEXT PRIMARY KEY, checked_at TEXT, room_count INTEGER,
        error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS change_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        property_url TEXT NOT NULL, room_id TEXT NOT NULL,
        event_type TEXT NOT NULL, detected_at TEXT NOT NULL,
        payload TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS check_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        property_url TEXT NOT NULL, checked_at TEXT NOT NULL,
        room_count INTEGER NOT NULL, added_count INTEGER NOT NULL DEFAULT 0,
        removed_count INTEGER NOT NULL DEFAULT 0,
        duration_ms INTEGER NOT NULL DEFAULT 0, error TEXT)""")
    history_columns = {row[1] for row in db.execute("PRAGMA table_info(check_history)")}
    if "updated_count" not in history_columns:
        db.execute("ALTER TABLE check_history ADD COLUMN updated_count INTEGER NOT NULL DEFAULT 0")
    db.execute("""CREATE TABLE IF NOT EXISTS notification_outbox (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
        subject TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, sent_at TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_time ON change_events(detected_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_events_property ON change_events(property_url, detected_at)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_check_history_time ON check_history(checked_at)")
    db.commit()
    return db


@contextmanager
def db_session():
    db = connect()
    try:
        yield db
        db.commit()
    finally:
        db.close()


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


class JSTFormatter(logging.Formatter):
    """Format container logs in Japan Standard Time instead of UTC."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        value = datetime.fromtimestamp(record.created, JST)
        return value.strftime(datefmt) if datefmt else value.isoformat(timespec="milliseconds")


def property_label(url: str) -> str:
    return Path(urlparse(url).path).stem


def send_email(subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST")
    recipients = [x.strip() for x in os.getenv("EMAIL_TO", "").split(",") if x.strip()]
    sender = os.getenv("EMAIL_FROM") or os.getenv("SMTP_USER")
    if not host or not recipients or not sender:
        message = (
            "邮件配置不完整 "
            f"(SMTP_HOST={bool(host)}, EMAIL_FROM={bool(sender)}, EMAIL_TO数量={len(recipients)})"
        )
        LOG.error(
            "%s",
            message,
        )
        raise RuntimeError(message)
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, ", ".join(recipients)
    msg.set_content(body)
    port = int(os.getenv("SMTP_PORT", "587"))
    use_ssl = os.getenv("SMTP_SSL", "false").lower() in {"1", "true", "yes"}
    smtp_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    LOG.info("准备发送邮件：subject=%r, recipients=%d, smtp=%s:%d", subject, len(recipients), host, port)
    try:
        with smtp_class(host, port, timeout=30) as smtp:
            if not use_ssl and os.getenv("SMTP_STARTTLS", "true").lower() in {"1", "true", "yes"}:
                smtp.starttls()
            if os.getenv("SMTP_USER"):
                smtp.login(os.environ["SMTP_USER"], os.getenv("SMTP_PASSWORD", ""))
            smtp.send_message(msg)
        LOG.info("邮件发送成功：subject=%r, recipients=%d", subject, len(recipients))
    except Exception:
        LOG.exception("邮件发送失败：subject=%r, smtp=%s:%d", subject, host, port)
        raise


def room_lines(rooms: list[dict]) -> str:
    lines = []
    for r in rooms:
        total = r["rent_yen"] + r["common_fee_yen"]
        lines.append(
            f"- {r['room']} | {r['layout']} | {r['area']} | {r['floor']}/{r['building_floors']}\n"
            f"  ¥{r['rent_yen']:,} + ¥{r['common_fee_yen']:,} = ¥{total:,}/月\n"
            f"  押金: {r['deposit']} | 礼金: {r['key_money']}\n"
            f"  {r['detail_url']}"
        )
    return "\n".join(lines) or "（无）"


def deliver_pending_notifications() -> None:
    """Send durable queued messages; failures remain pending for the next round."""
    with db_session() as db:
        pending = db.execute(
            "SELECT id, subject, body FROM notification_outbox WHERE status='pending' ORDER BY id LIMIT 20"
        ).fetchall()
    for item in pending:
        try:
            send_email(item["subject"], item["body"])
        except Exception as exc:
            with db_session() as db:
                db.execute(
                    "UPDATE notification_outbox SET attempts=attempts+1, last_error=? WHERE id=?",
                    (str(exc), item["id"]),
                )
            LOG.warning("邮件保留在待发队列，下一轮重试：outbox_id=%d", item["id"])
            continue
        with db_session() as db:
            db.execute(
                "UPDATE notification_outbox SET status='sent', attempts=attempts+1, sent_at=?, last_error=NULL WHERE id=?",
                (now_iso(), item["id"]),
            )


def log_rooms(url: str, rooms: list[dict]) -> None:
    """Write the complete, currently available room inventory to the log."""
    label = property_label(url)
    if not rooms:
        LOG.info("房源信息：property=%s, rooms=0（无符合条件的空房）", label)
        return

    LOG.info("房源信息：property=%s, rooms=%d", label, len(rooms))
    for room in rooms:
        total = room["rent_yen"] + room["common_fee_yen"]
        LOG.info(
            "房源明细：property=%s, room=%s, layout=%s, area=%s, floor=%s/%s, "
            "rent=¥%s, common_fee=¥%s, total=¥%s/月, deposit=%s, key_money=%s, url=%s",
            label,
            room["room"],
            room["layout"],
            room["area"],
            room["floor"],
            room["building_floors"],
            f'{room["rent_yen"]:,}',
            f'{room["common_fee_yen"]:,}',
            f"{total:,}",
            room["deposit"],
            room["key_money"],
            room["detail_url"],
        )


def check_property(url: str) -> None:
    checked_at = now_iso()
    started = time.monotonic()
    LOG.info("开始检查：property=%s, url=%s", property_label(url), url)
    try:
        rooms = filtered(fetch_rooms(url), MIN_RENT, MAX_RENT, LAYOUTS)
        log_rooms(url, rooms)
        current = {r["id"]: r for r in rooms}
        with db_session() as db:
            existing_rows = db.execute(
                "SELECT room_id, payload, active FROM room_state WHERE property_url=?", (url,)
            ).fetchall()
            existing = {r["room_id"]: r for r in existing_rows}
            was_initialized = bool(existing_rows) or db.execute(
                "SELECT 1 FROM checks WHERE property_url=?", (url,)
            ).fetchone() is not None
            new_ids = set(current) - {k for k, v in existing.items() if v["active"]}
            gone_ids = {k for k, v in existing.items() if v["active"]} - set(current)
            updated_ids = {
                room_id for room_id in set(current) & set(existing)
                if existing[room_id]["active"]
                and json.loads(existing[room_id]["payload"]) != current[room_id]
            }

            event_type = "added" if was_initialized else "baseline"
            for room_id in new_ids:
                db.execute(
                    """INSERT INTO change_events
                    (property_url, room_id, event_type, detected_at, payload)
                    VALUES (?, ?, ?, ?, ?)""",
                    (url, room_id, event_type, checked_at,
                     json.dumps(current[room_id], ensure_ascii=False)),
                )
            for room_id in gone_ids:
                db.execute(
                    """INSERT INTO change_events
                    (property_url, room_id, event_type, detected_at, payload)
                    VALUES (?, ?, 'removed', ?, ?)""",
                    (url, room_id, checked_at, existing[room_id]["payload"]),
                )
            for room_id in updated_ids:
                db.execute(
                    """INSERT INTO change_events
                    (property_url, room_id, event_type, detected_at, payload)
                    VALUES (?, ?, 'updated', ?, ?)""",
                    (url, room_id, checked_at, json.dumps(current[room_id], ensure_ascii=False)),
                )

            db.execute("UPDATE room_state SET active=0 WHERE property_url=?", (url,))
            for room_id, room in current.items():
                payload = json.dumps(room, ensure_ascii=False)
                db.execute("""INSERT INTO room_state
                    (property_url, room_id, payload, active, first_seen, last_seen)
                    VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(property_url, room_id) DO UPDATE SET
                    payload=excluded.payload, active=1, last_seen=excluded.last_seen""",
                    (url, room_id, payload, checked_at, checked_at))
            db.execute("""INSERT INTO checks(property_url, checked_at, room_count, error)
                VALUES (?, ?, ?, NULL) ON CONFLICT(property_url) DO UPDATE SET
                checked_at=excluded.checked_at, room_count=excluded.room_count, error=NULL""",
                (url, checked_at, len(rooms)))
            db.execute(
                """INSERT INTO check_history
                (property_url, checked_at, room_count, added_count, removed_count, updated_count, duration_ms, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
                (url, checked_at, len(rooms), len(new_ids), len(gone_ids), len(updated_ids),
                 int((time.monotonic() - started) * 1000)),
            )
            added = [current[i] for i in new_ids]
            removed = [json.loads(existing[i]["payload"]) for i in gone_ids]
            updated = [current[i] for i in updated_ids]
            should_notify = was_initialized or NOTIFY_ON_FIRST_RUN
            if should_notify and (new_ids or updated_ids):
                subject = (
                    f"[UR房源变化] {property_label(url)} 当前{len(rooms)}套 "
                    f"(+{len(added)} / ~{len(updated)})"
                )
                body = (
                    f"检查时间: {checked_at}\n物件页面: {url}\n"
                    f"变化: 新增或重新上架 {len(added)} 套 / 资料更新 {len(updated)} 套\n\n"
                    f"【新增房源】\n{room_lines(added)}\n\n"
                    f"【资料有变化】\n{room_lines(updated)}\n\n"
                    f"【当前全部可用房源（{len(rooms)}套）】\n{room_lines(rooms)}\n\n"
                    "UR 官网的空房信息可能不是实时状态，并按先到先得处理；请打开详情链接确认。"
                )
                db.execute(
                    "INSERT INTO notification_outbox(created_at, subject, body) VALUES (?, ?, ?)",
                    (checked_at, subject, body),
                )
        LOG.info(
            "检查完成：property=%s, rooms=%d, added=%d, removed=%d, updated=%d, checked_at=%s",
            property_label(url), len(rooms), len(new_ids), len(gone_ids), len(updated_ids), checked_at,
        )
    except Exception as exc:
        LOG.exception("检查失败: %s", url)
        with db_session() as db:
            db.execute("""INSERT INTO checks(property_url, checked_at, room_count, error)
                VALUES (?, ?, 0, ?) ON CONFLICT(property_url) DO UPDATE SET
                checked_at=excluded.checked_at, error=excluded.error""", (url, checked_at, str(exc)))
            db.execute(
                """INSERT INTO check_history
                (property_url, checked_at, room_count, added_count, removed_count, updated_count, duration_ms, error)
                VALUES (?, ?, 0, 0, 0, 0, ?, ?)""",
                (url, checked_at, int((time.monotonic() - started) * 1000), str(exc)),
            )


def run_checks() -> None:
    LOG.info("本轮检查开始：properties=%d", len(PROPERTY_URLS))
    for url in PROPERTY_URLS:
        check_property(url)
    deliver_pending_notifications()
    LOG.info("本轮检查结束")


def scheduler() -> None:
    LOG.info("定时检查器启动：interval=%d秒", CHECK_INTERVAL)
    while True:
        started = time.monotonic()
        run_checks()
        time.sleep(max(1, CHECK_INTERVAL - (time.monotonic() - started)))


def dashboard() -> str:
    cards = []
    with db_session() as db:
        for url in PROPERTY_URLS:
            check = db.execute("SELECT * FROM checks WHERE property_url=?", (url,)).fetchone()
            rows = db.execute("SELECT payload, first_seen FROM room_state WHERE property_url=? AND active=1 ORDER BY room_id", (url,)).fetchall()
            rooms = [(json.loads(r["payload"]), r["first_seen"]) for r in rows]
            items = "".join(
                f'<a class="room" href="{html.escape(r[0]["detail_url"])}" target="_blank" rel="noopener">'
                f'<b>{html.escape(r[0]["room"])}</b><span>{html.escape(r[0]["layout"])} · {html.escape(r[0]["area"])} · {html.escape(r[0]["floor"])}</span>'
                f'<strong>¥{r[0]["rent_yen"]:,} <small>+ ¥{r[0]["common_fee_yen"]:,}</small></strong></a>'
                for r in rooms
            ) or '<p class="empty">目前没有符合条件的空房</p>'
            status = html.escape(check["error"] if check and check["error"] else "正常")
            checked = html.escape(check["checked_at"] if check else "尚未检查")
            cards.append(f'<section><header><div><h2>{property_label(url)}</h2><a href="{html.escape(url)}" target="_blank">打开 UR 官网 ↗</a></div><em>{len(rooms)} 套</em></header><p class="meta">最后检查：{checked} · 状态：{status}</p>{items}</section>')
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60"><title>UR 房源监控</title><style>
    *{{box-sizing:border-box}} body{{margin:0;background:#f4f1eb;color:#24221f;font:15px system-ui,sans-serif}} main{{max-width:880px;margin:auto;padding:42px 18px}} h1{{font-size:28px;margin:0 0 8px}} .lead{{color:#706b62;margin:0 0 28px}} section{{background:#fff;border:1px solid #ded9d0;border-radius:16px;padding:22px;margin:18px 0;box-shadow:0 5px 24px #4238170d}} header{{display:flex;justify-content:space-between;align-items:start}} h2{{margin:0 0 5px}} header a{{color:#6e6556}} em{{background:#263f36;color:#fff;border-radius:99px;padding:7px 12px;font-style:normal}} .meta{{color:#8a8378;font-size:13px;border-bottom:1px solid #eee9e1;padding-bottom:15px}} .room{{display:grid;grid-template-columns:1fr 1fr auto;gap:12px;align-items:center;padding:16px 2px;border-bottom:1px solid #eee9e1;text-decoration:none;color:inherit}} .room:last-child{{border:0}} .room span{{color:#6f695f}} .room strong{{color:#a44a2b;font-size:18px}} small{{color:#7d7569;font-weight:400}} .empty{{color:#8a8378;padding:18px 0 0}} @media(max-width:600px){{.room{{grid-template-columns:1fr auto}}.room span{{grid-row:2}}}}
    </style></head><body><main><h1>UR 房源监控</h1><p class="lead">每 {CHECK_INTERVAL // 60} 分钟自动检查；页面每分钟刷新。</p>{''.join(cards)}</main></body></html>"""


def history_data(table: str, limit: int = 500) -> list[dict]:
    """Return recent check or change rows for JSON export."""
    if table not in {"change_events", "check_history"}:
        raise ValueError("unsupported history table")
    limit = max(1, min(limit, 5000))
    with db_session() as db:
        rows = db.execute(
            f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = [dict(row) for row in rows]
    if table == "change_events":
        for row in result:
            row["room"] = json.loads(row.pop("payload"))
    return result


def json_response(data: object) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            body, content_type, status = b"ok\n", "text/plain; charset=utf-8", 200
        elif path == "/api/events":
            body = json_response({"events": history_data("change_events")})
            content_type, status = "application/json; charset=utf-8", 200
        elif path == "/api/checks":
            body = json_response({"checks": history_data("check_history")})
            content_type, status = "application/json; charset=utf-8", 200
        elif path in {"/", "/index.html"}:
            body, content_type, status = dashboard().encode(), "text/html; charset=utf-8", 200
        else:
            body, content_type, status = b"not found\n", "text/plain; charset=utf-8", 404
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        LOG.info("http: " + fmt, *args)


def configure_logging() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    formatter = JSTFormatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logfile = RotatingFileHandler(
        LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    logfile.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    root.addHandler(console)
    root.addHandler(logfile)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="UR 房源监控服务")
    parser.add_argument("--check-once", action="store_true", help="立即检查一次并退出")
    parser.add_argument("--test-email", action="store_true", help="发送测试邮件并退出")
    args = parser.parse_args()
    configure_logging()
    LOG.info("UR Monitor 启动：pid=%d, log=%s", os.getpid(), LOG_PATH)
    LOG.info("配置文件：path=%s, loaded_values=%d", CONFIG_FILE, CONFIG_VALUES_LOADED)
    connect().close()
    if args.test_email:
        send_email("[UR房源监控] 测试邮件", f"邮件配置正常。\n发送时间: {now_iso()}")
        LOG.info("测试邮件已发送")
        return
    if args.check_once:
        run_checks()
        return
    threading.Thread(target=scheduler, daemon=True, name="checker").start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    LOG.info("Dashboard: http://0.0.0.0:%d, interval=%ds", PORT, CHECK_INTERVAL)
    server.serve_forever()


if __name__ == "__main__":
    main()
