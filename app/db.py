"""Работа с базой данных SQLite."""

import sqlite3
from pathlib import Path

from flask import current_app, g

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

DEFAULT_SETTINGS = {
    "company_name": "Дизель Сервис",
    "company_address": "",
    "company_phone": "",
    "company_requisites": "",
    "order_footer": (
        "Гарантия на выполненные работы — 30 дней при соблюдении условий эксплуатации. "
        "Претензии по комплектности и внешнему виду принимаются при получении техники."
    ),
}


SCHEMA_VERSION = 3

# Типы запчастей, которые создаются при первом запуске (потом их можно менять).
DEFAULT_PART_CATEGORIES = [
    "Автоэлектрика",
    "Гидравлика",
    "Запчасти для ремонта импортной мех. техники",
    "Запчасти для ремонта отечественной мех. техники",
    "Расходные материалы",
    "Фильтра",
    "Готовые форсунки на продажу",
]


def _lower(value):
    return value.lower() if isinstance(value, str) else value


def _digits(value):
    return "".join(ch for ch in value if ch.isdigit()) if isinstance(value, str) else value


def connect(path):
    conn = sqlite3.connect(path, detect_types=0)
    conn.row_factory = sqlite3.Row
    # Встроенный LOWER в SQLite не понимает кириллицу — поиск «иванов» не нашёл бы «Иванов».
    conn.create_function("LOWER_RU", 1, _lower, deterministic=True)
    # Только цифры телефона: «912 345» найдёт «+7 (912) 345-67-89».
    conn.create_function("DIGITS", 1, _digits, deterministic=True)
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
        existing = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'orders'"
        ).fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0] if existing else 0
        if existing:
            _migrate(conn, version)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        if version < 2:
            for position, name in enumerate(DEFAULT_PART_CATEGORIES, start=1):
                conn.execute("INSERT OR IGNORE INTO part_categories (name, position) VALUES (?, ?)",
                             (name, position))
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
            )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def _columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn, table):
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone())


def _migrate(conn, version):
    """Обновляет базу, созданную прошлой версией программы, без потери данных."""
    if version < 1:
        # Нумерация заказ-нарядов по годам.
        if "number" not in _columns(conn, "orders"):
            conn.execute("ALTER TABLE orders ADD COLUMN year INTEGER")
            conn.execute("ALTER TABLE orders ADD COLUMN number INTEGER")
        counters = {}
        for row in conn.execute("SELECT id, created_at FROM orders ORDER BY id").fetchall():
            year = int(row["created_at"][:4])
            counters[year] = counters.get(year, 0) + 1
            conn.execute("UPDATE orders SET year = ?, number = ? WHERE id = ?",
                         (year, counters[year], row["id"]))
        # Вход только по паролю руководителя: переносим пароль из учётной записи.
        has_password = conn.execute(
            "SELECT 1 FROM settings WHERE key = 'admin_password_hash'").fetchone()
        if not has_password and "password_hash" in _columns(conn, "employees"):
            row = conn.execute(
                "SELECT password_hash FROM employees WHERE role = 'admin' "
                "AND password_hash IS NOT NULL AND is_active = 1 ORDER BY id LIMIT 1"
            ).fetchone()
            if row:
                conn.execute("INSERT INTO settings (key, value) VALUES ('admin_password_hash', ?)",
                             (row["password_hash"],))
        conn.execute("UPDATE settings SET value = 'Дизель Сервис' "
                     "WHERE key = 'company_name' AND value = 'Дизельный сервис'")
    if version < 2 and _table_exists(conn, "parts"):
        # Типы запчастей и наценка. У старых позиций цена продажи остаётся ручной.
        columns = _columns(conn, "parts")
        if "category_id" not in columns:
            conn.execute("ALTER TABLE parts ADD COLUMN category_id INTEGER "
                         "REFERENCES part_categories (id) ON DELETE SET NULL")
        if "markup_percent" not in columns:
            conn.execute("ALTER TABLE parts ADD COLUMN markup_percent REAL")
    if version < 3 and _table_exists(conn, "parts") and "markup_percent" in _columns(conn, "parts"):
        # Новое правило цены: вверх до сотни, дешевле 100 ₽ — без округления.
        from .utils import price_with_markup
        for row in conn.execute("SELECT id, purchase_price, markup_percent FROM parts "
                                "WHERE markup_percent IS NOT NULL").fetchall():
            conn.execute("UPDATE parts SET sale_price = ? WHERE id = ?",
                         (price_with_markup(row["purchase_price"], row["markup_percent"]),
                          row["id"]))


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
