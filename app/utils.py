"""Справочники, форматирование и разбор данных форм."""

import calendar
from datetime import date, datetime

from flask import request
from jinja2 import Undefined

ORDER_STATUSES = {
    "new": "Принят",
    "diagnostics": "Диагностика",
    "in_progress": "В работе",
    "waiting_parts": "Ждёт запчасти",
    "done": "Готов",
    "issued": "Выдан",
    "cancelled": "Отменён",
}
ACTIVE_STATUSES = ("new", "diagnostics", "in_progress", "waiting_parts", "done")
COMPLETED_STATUSES = ("done", "issued")
# Заказ-наряд в этих статусах нельзя редактировать, пока статус не изменят.
LOCKED_STATUSES = ("issued", "cancelled")

ROLES = {
    "admin": "Руководитель",
    "manager": "Приёмщик / менеджер",
    "master": "Мастер",
}

PAYMENT_METHODS = {"cash": "Наличные", "card": "Карта", "bank": "Безнал (р/с)"}

STOCK_KINDS = {
    "receipt": "Приход",
    "order": "В заказ-наряд",
    "return": "Возврат на склад",
    "writeoff": "Списание",
    "adjust": "Инвентаризация",
}

CLIENT_KINDS = {"person": "Физлицо", "company": "Организация"}

INCOME_CATEGORIES = ["Оплата заказ-наряда", "Продажа запчастей", "Прочий доход"]
EXPENSE_CATEGORIES = [
    "Закупка запчастей",
    "Зарплата",
    "Аренда",
    "Коммунальные услуги",
    "Налоги и взносы",
    "Оборудование и инструмент",
    "Расходные материалы",
    "Реклама",
    "Прочие расходы",
]
CATEGORY_ORDER_PAYMENT = "Оплата заказ-наряда"
CATEGORY_SALARY = "Зарплата"
CATEGORY_PURCHASE = "Закупка запчастей"

MONTHS = [
    "", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]


# ---------- Разбор значений из форм ----------

def form_str(name, default=""):
    value = request.form.get(name, default)
    return value.strip() if isinstance(value, str) else default


def to_float(value, default=0.0):
    if value is None:
        return default
    text = str(value).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def to_int(value, default=None):
    if value is None:
        return default
    text = str(value).strip().replace(" ", "")
    if not text:
        return default
    try:
        return int(float(text.replace(",", ".")))
    except ValueError:
        return default


def form_float(name, default=0.0):
    return to_float(request.form.get(name), default)


def form_int(name, default=None):
    return to_int(request.form.get(name), default)


def money_round(value):
    return round(float(value or 0) + 0.0, 2)


def parse_date(value, default=None):
    if not value:
        return default
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except ValueError:
            continue
    return default


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return date.today().isoformat()


def month_bounds(day=None):
    day = day or date.today()
    last = calendar.monthrange(day.year, day.month)[1]
    return date(day.year, day.month, 1), date(day.year, day.month, last)


def period_from_request():
    """Период из параметров ?start=&end= (по умолчанию — текущий месяц)."""
    default_start, default_end = month_bounds()
    start = parse_date(request.args.get("start"), default_start)
    end = parse_date(request.args.get("end"), default_end)
    if start > end:
        start, end = end, start
    return start, end


def period_presets():
    today = date.today()
    this_start, this_end = month_bounds(today)
    prev_day = this_start.replace(day=1)
    prev_month = (prev_day.month - 2) % 12 + 1
    prev_year = prev_day.year - (1 if prev_day.month == 1 else 0)
    prev_start, prev_end = month_bounds(date(prev_year, prev_month, 1))
    return [
        ("Сегодня", today, today),
        ("Этот месяц", this_start, this_end),
        ("Прошлый месяц", prev_start, prev_end),
        ("Этот год", date(today.year, 1, 1), date(today.year, 12, 31)),
    ]


# ---------- Форматирование для шаблонов ----------

def _group_thousands(text):
    return f"{text}".replace(",", " ")


def _empty(value):
    return value is None or value == "" or isinstance(value, Undefined)


def fmt_money(value, currency=True):
    value = 0.0 if _empty(value) else float(value)
    text = _group_thousands(f"{value:,.2f}").replace(".", ",")
    if text.endswith(",00"):
        text = text[:-3]
    return f"{text} ₽" if currency else text


def fmt_qty(value):
    value = 0.0 if _empty(value) else float(value)
    if value == int(value):
        return _group_thousands(f"{int(value):,}")
    return f"{value:.3f}".rstrip("0").rstrip(".").replace(".", ",")


def fmt_num(value):
    """Число для поля ввода: без лишних нулей, с точкой."""
    if _empty(value):
        return ""
    value = float(value)
    if value == int(value):
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def fmt_date(value):
    if not value:
        return ""
    if isinstance(value, (date, datetime)):
        return value.strftime("%d.%m.%Y")
    text = str(value)
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return text


def fmt_datetime(value):
    if not value:
        return ""
    text = str(value)
    try:
        return datetime.strptime(text[:16], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return fmt_date(text)


def order_number(order_id):
    return f"{int(order_id):06d}"


def vehicle_title(v):
    if v is None:
        return ""
    keys = v.keys() if hasattr(v, "keys") else v
    parts = [v["make"], v["model"]] if "make" in keys else []
    title = " ".join(p for p in parts if p)
    plate = v["plate"] if "plate" in keys else None
    if plate:
        title = f"{title} · {plate}" if title else plate
    return title or "Техника без названия"
