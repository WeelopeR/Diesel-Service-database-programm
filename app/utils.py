"""Справочники, форматирование и разбор данных форм."""

import calendar
import math
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

PAYMENT_METHODS = {"cash": "Наличные", "card": "Карта", "bank": "Безнал (р/с)"}

# Фильтр заказ-нарядов по оплате. «Долг» — техника выдана, а оплачено не всё.
PAYMENT_FILTERS = {
    "debt": "Долг (выдан, не оплачен)",
    "unpaid": "Не оплачен полностью",
    "paid": "Оплачен полностью",
}

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


def current_year():
    return date.today().year


def order_label(number, year):
    """Полный номер заказ-наряда для записей в истории: «15/2026»."""
    return f"{number}/{year}"


def fmt_order_no(value, year=None):
    """Номер для показа: в текущем году — «15», в прошлых — «15/2025».
    Принимает строку заказ-наряда (с полями number и year) или номер и год."""
    if hasattr(value, "keys"):
        number, year = value["number"], value["year"]
    else:
        number = value
    if number is None or _empty(number):
        return ""
    if year and int(year) != current_year():
        return order_label(number, year)
    return str(number)


# Шаг округления цены продажи: всегда вверх до ближайших 50 ₽.
PRICE_STEP = 50


def price_with_markup(purchase_price, markup_percent):
    """Цена продажи по наценке: от 100 ₽ — всегда вверх до 50 ₽
    (120 → 150, 151 → 200, 1 521 → 1 550), дешевле 100 ₽ — не округляется."""
    # Сначала до копеек, чтобы 1000 × 1,3 = 1300,0000000002 не превратилось в 1400.
    return round_sale_price(float(purchase_price or 0) * (1 + float(markup_percent or 0) / 100))


def round_sale_price(value):
    """Округление цены продажи: от 100 ₽ — вверх до 50 ₽, дешевле — до копеек."""
    raw = money_round(value)
    if raw <= 0:
        return 0.0
    if raw < 100:
        return raw
    return float(math.ceil(raw / PRICE_STEP) * PRICE_STEP)


def plural(n, one, few, many):
    """Склонение по числу: 1 позиция, 2 позиции, 5 позиций."""
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def like(text):
    """Параметр для поиска через LOWER_RU(...) LIKE ? без учёта регистра."""
    return f"%{text.strip().lower()}%"


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
