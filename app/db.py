"""Работа с базой данных SQLite."""

import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DEFAULT_SETTINGS = {
    "company_name": "Дизельный сервис",
    "company_address": "",
    "company_phone": "",
    "company_requisites": "",
    "order_footer": (
        "Гарантия на выполненные работы — 30 дней при соблюдении условий эксплуатации. "
        "Претензии по комплектности и внешнему виду принимаются при получении техники."
    ),
}


def connect(path):
    conn = sqlite3.connect(path, detect_types=0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["DATABASE"])
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(path):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        conn.commit()
    finally:
        conn.close()


def query(sql, params=(), one=False):
    cur = get_db().execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def scalar(sql, params=()):
    row = get_db().execute(sql, params).fetchone()
    return row[0] if row else None


def execute(sql, params=()):
    """Выполняет запрос без commit и возвращает lastrowid."""
    cur = get_db().execute(sql, params)
    return cur.lastrowid


def commit():
    get_db().commit()


def get_settings():
    rows = query("SELECT key, value FROM settings")
    data = dict(DEFAULT_SETTINGS)
    data.update({r["key"]: r["value"] or "" for r in rows})
    return data


def set_setting(key, value):
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


class Page:
    """Страница результатов для длинных списков."""

    def __init__(self, rows, page, per_page, total):
        self.rows = rows
        self.page = page
        self.per_page = per_page
        self.total = total
        self.pages = max(1, (total + per_page - 1) // per_page)

    @property
    def has_prev(self):
        return self.page > 1

    @property
    def has_next(self):
        return self.page < self.pages


def paginate(sql, params, page, per_page=50):
    total = scalar(f"SELECT COUNT(*) FROM ({sql})", params) or 0
    page = max(1, min(page, max(1, (total + per_page - 1) // per_page)))
    rows = query(f"{sql} LIMIT ? OFFSET ?", (*params, per_page, (page - 1) * per_page))
    return Page(rows, page, per_page, total)
