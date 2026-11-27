"""Склад: номенклатура по типам запчастей, наценка, приход, расход, остатки,
списание, инвентаризация и история движения."""

import re
import sqlite3
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import db
from ..stock import StockError, move_stock
from ..utils import (
    CATEGORY_PURCHASE, fmt_money, form_float, form_str, like, money_round, parse_date,
    period_presets, price_with_markup, round_sale_price, to_float, to_int, today_str,
)

bp = Blueprint("warehouse", __name__, url_prefix="/warehouse")

# Приход — поступления от поставщиков и излишки по инвентаризации (в т. ч. начальный остаток).
# Расход — выдача в заказ-наряды за вычетом возвратов, списания и недостачи.
INCOMING_SQL = "CASE WHEN kind = 'receipt' OR (kind = 'adjust' AND quantity > 0) " \
               "THEN quantity ELSE 0 END"
OUTGOING_SQL = "CASE WHEN kind IN ('order', 'return', 'writeoff') OR " \
               "(kind = 'adjust' AND quantity < 0) THEN -quantity ELSE 0 END"

SORTS = {
    "sku": "sku COLLATE NOCASE",
    "name": "name COLLATE NOCASE",
    "unit": "unit COLLATE NOCASE",
    "markup": "markup_percent",
    "price": "sale_price",
    "in": "incoming",
    "out": "outgoing",
    "balance": "balance",
}

STOCK_STATES = {
    "in": "Есть в наличии",
    "out": "Нет в наличии",
    "low": "Заканчиваются (≤ минимума)",
    "moved": "Было движение за период",
    "idle": "Без движения за период",
}

RANGE_FIELDS = ("markup", "price", "in", "out", "balance")
RANGE_COLUMNS = {"markup": "markup_percent", "price": "sale_price", "in": "incoming",
                 "out": "outgoing", "balance": "balance"}
FILTER_KEYS = ("q", "sku", "name", "unit", "stock", "show", "start", "end",
               *[f"{k}_{b}" for k in RANGE_FIELDS for b in ("min", "max")])


def get_part(part_id):
    part = db.query(
        "SELECT p.*, pc.name AS category_name FROM parts p "
        "LEFT JOIN part_categories pc ON pc.id = p.category_id WHERE p.id = ?",
        (part_id,), one=True,
    )
    if part is None:
        abort(404)
    return part


def categories():
    return db.query("SELECT * FROM part_categories ORDER BY position, name COLLATE NOCASE")


def part_form_data():
    purchase = money_round(max(0, form_float("purchase_price")))
    markup_text = form_str("markup_percent")
    markup = max(0.0, to_float(markup_text)) if markup_text else None
    sale = price_with_markup(purchase, markup) if markup is not None \
        else money_round(max(0, form_float("sale_price")))
    category_id = to_int(request.form.get("category_id"))
    if category_id and not db.scalar("SELECT 1 FROM part_categories WHERE id = ?", (category_id,)):
        category_id = None
    return {
        "sku": form_str("sku"),
        "name": form_str("name"),
        "brand": form_str("brand"),
        "category_id": category_id,
        "unit": form_str("unit") or "шт",
        "purchase_price": purchase,
        "markup_percent": markup,
        "sale_price": sale,
        "min_quantity": max(0, form_float("min_quantity")),
        "location": form_str("location"),
        "notes": form_str("notes"),
        "is_active": 1 if request.form.get("is_active") else 0,
    }


PART_COLUMNS = ("sku", "name", "brand", "category_id", "unit", "purchase_price", "markup_percent",
                "sale_price", "min_quantity", "location", "notes", "is_active")


def render_form(part, edit=False):
    return render_template("warehouse/form.html", part=part, edit=edit, categories=categories())


def apply_receipt_price(part_id, price):
    """Приход подорожавшей позиции: новая цена закупки в карточке и пересчёт цены продажи.

    С наценкой — по наценке, без неё — с той же разницей между закупкой и продажей,
    что была. Округление обычное: от 100 ₽ — вверх до 50 ₽. Если закупка не подорожала,
    карточка не меняется и цена продажи не снижается.
    Возвращает (старая, новая) цену продажи, если она изменилась, иначе None.
    """
    part = db.query("SELECT purchase_price, markup_percent, sale_price FROM parts WHERE id = ?",
                    (part_id,), one=True)
    old_purchase = part["purchase_price"] or 0
    old_sale = part["sale_price"] or 0
    if price <= old_purchase + 0.009:
        return None
    if part["markup_percent"] is not None:
        sale = price_with_markup(price, part["markup_percent"])
    elif old_purchase > 0 and old_sale > 0:
        sale = round_sale_price(price * old_sale / old_purchase)
    else:
        sale = old_sale
    sale = max(sale, old_sale)
    db.execute("UPDATE parts SET purchase_price = ?, sale_price = ? WHERE id = ?",
               (price, sale, part_id))
    return (old_sale, sale) if sale > old_sale + 0.009 else None


# ---------- Список с фильтрами ----------

def normalize_sku(text):
    return re.sub(r"[\s\-./]", "", text.lower())


def get_scope(scope):
    """Страница склада: тип запчастей (по id), «none» — без типа, «all» — все позиции."""
    if scope == "all":
        return {"key": "all", "id": None, "title": "Все позиции"}
    if scope == "none":
        return {"key": "none", "id": None, "title": "Без типа"}
    category = db.query("SELECT * FROM part_categories WHERE id = ?", (to_int(scope),), one=True)
    if category is None:
        abort(404)
    return {"key": str(category["id"]), "id": category["id"], "title": category["name"]}


def build_stock_query(args, scope):
    """Позиции склада с приходом/расходом за период и остатком на конец периода."""
    f = {k: (args.get(k) or "").strip() for k in FILTER_KEYS}
    start = parse_date(f["start"])
    end = parse_date(f["end"]) or date.today()
    # На странице «Все позиции» можно группировать по типам. Форма шлёт group=0 (скрытое
    # поле) и group=1 (галочка); ссылка без параметра — группировка включена.
    f["group"] = scope["key"] == "all" and ("group" not in args or "1" in args.getlist("group"))
    f["sort"] = args.get("sort") if args.get("sort") in SORTS else "name"
    f["dir"] = "desc" if args.get("dir") == "desc" else "asc"

    inner = f"""
        SELECT p.*, pc.name AS category_name, COALESCE(pc.position, 9999) AS category_position,
               COALESCE(m.incoming, 0) AS incoming, COALESCE(m.outgoing, 0) AS outgoing,
               ROUND(p.quantity - COALESCE(a.after_qty, 0), 3) AS balance
        FROM parts p
        LEFT JOIN part_categories pc ON pc.id = p.category_id
        LEFT JOIN (SELECT part_id, SUM({INCOMING_SQL}) AS incoming, SUM({OUTGOING_SQL}) AS outgoing
                   FROM stock_movements WHERE date(created_at) BETWEEN ? AND ?
                   GROUP BY part_id) m ON m.part_id = p.id
        LEFT JOIN (SELECT part_id, SUM(quantity) AS after_qty FROM stock_movements
                   WHERE date(created_at) > ? GROUP BY part_id) a ON a.part_id = p.id
    """
    params = [start.isoformat() if start else "0001-01-01", end.isoformat(), end.isoformat()]

    where = []
    if scope["key"] == "none":
        where.append("category_id IS NULL")
    elif scope["id"]:
        where.append("category_id = ?")
        params.append(scope["id"])
    if f["show"] == "archived":
        where.append("is_active = 0")
    elif f["show"] != "all":
        where.append("is_active = 1")
    sku_sql = ("REPLACE(REPLACE(REPLACE(REPLACE(LOWER_RU(COALESCE(sku, '')), ' ', ''), "
               "'-', ''), '.', ''), '/', '')")
    text_sql = ("LOWER_RU(name || ' ' || COALESCE(brand, '') || ' ' || "
                "COALESCE(location, '') || ' ' || COALESCE(notes, ''))")
    if f["q"]:
        # Быстрый поиск с обзора склада: каждое слово — в артикуле или в названии.
        for word in f["q"].split():
            where.append(f"({sku_sql} LIKE ? OR {text_sql} LIKE ?)")
            params += [f"%{normalize_sku(word)}%", like(word)]
    if f["sku"]:
        where.append(f"{sku_sql} LIKE ?")
        params.append(f"%{normalize_sku(f['sku'])}%")
    if f["name"]:
        # Все слова должны встретиться в названии, бренде, месте или примечании.
        for word in f["name"].split():
            where.append(f"{text_sql} LIKE ?")
            params.append(like(word))
    if f["unit"]:
        where.append("unit = ?")
        params.append(f["unit"])
    for key in RANGE_FIELDS:
        column = RANGE_COLUMNS[key]
        low, high = to_float(f[f"{key}_min"], None), to_float(f[f"{key}_max"], None)
        if low is not None:
            where.append(f"{column} >= ?")
            params.append(low)
        if high is not None:
            where.append(f"{column} <= ?")
            params.append(high)
    stock = f["stock"]
    if stock == "in":
        where.append("balance > 0")
    elif stock == "out":
        where.append("balance <= 0")
    elif stock == "low":
        where.append("min_quantity > 0 AND quantity <= min_quantity")
    elif stock == "moved":
        where.append("(incoming <> 0 OR outgoing <> 0)")
    elif stock == "idle":
        where.append("incoming = 0 AND outgoing = 0")

    direction = f["dir"].upper()
    order = (f"markup_percent IS NULL, markup_percent {direction}" if f["sort"] == "markup"
             else f"{SORTS[f['sort']]} {direction}")
    if f["group"]:
        order = f"category_position, category_name, {order}"
    sql = (f"SELECT * FROM ({inner}) t" + (f" WHERE {' AND '.join(where)}" if where else "")
           + f" ORDER BY {order}, name COLLATE NOCASE")
    return sql, params, f, start, end


@bp.route("/")
def index():
    """Обзор склада: итоги и отдельная карточка для каждого типа запчастей."""
    if request.args:
        # Старые ссылки вида /warehouse/?category=…&stock=low ведут на нужную страницу.
        args = request.args.to_dict()
        scope = args.pop("category", "") or "all"
        if args.pop("show", "") == "low":
            args["stock"] = "low"
        return redirect(url_for("warehouse.items", scope=scope, **args))
    stats_sql = """
        COUNT(p.id) AS positions,
        COALESCE(SUM(p.quantity * p.purchase_price), 0) AS cost,
        COALESCE(SUM(p.quantity * p.sale_price), 0) AS retail,
        COALESCE(SUM(CASE WHEN p.min_quantity > 0 AND p.quantity <= p.min_quantity
                     THEN 1 ELSE 0 END), 0) AS low,
        COALESCE(SUM(CASE WHEN p.quantity <= 0 THEN 1 ELSE 0 END), 0) AS empty
    """
    types = db.query(
        f"SELECT pc.id, pc.name, {stats_sql} FROM part_categories pc "
        f"LEFT JOIN parts p ON p.category_id = pc.id AND p.is_active = 1 "
        f"GROUP BY pc.id ORDER BY pc.position, pc.name COLLATE NOCASE"
    )
    untyped = db.query(f"SELECT {stats_sql} FROM parts p "
                       f"WHERE p.is_active = 1 AND p.category_id IS NULL", one=True)
    totals = db.query(f"SELECT {stats_sql} FROM parts p WHERE p.is_active = 1", one=True)
    recent = db.query(
        """
        SELECT m.*, p.name AS part_name, p.unit, o.number AS order_number, o.year AS order_year
        FROM stock_movements m JOIN parts p ON p.id = m.part_id
        LEFT JOIN orders o ON o.id = m.order_id
        ORDER BY m.id DESC LIMIT 8
        """
    )
    return render_template("warehouse/index.html", types=types, untyped=untyped, totals=totals,
                           recent=recent)


@bp.route("/list/<scope>")
def items(scope):
    """Позиции одного типа (или все) с колонками, фильтрами и сортировкой."""
    scope = get_scope(scope)
    sql, params, f, start, end = build_stock_query(request.args, scope)
    page = db.paginate(sql, params, to_int(request.args.get("page"), 1), per_page=100)
    totals = db.query(
        f"SELECT COUNT(*) AS positions, COALESCE(SUM(balance * purchase_price), 0) AS cost, "
        f"COALESCE(SUM(balance * sale_price), 0) AS retail, "
        f"SUM(CASE WHEN min_quantity > 0 AND quantity <= min_quantity THEN 1 ELSE 0 END) AS low "
        f"FROM ({sql})", params, one=True,
    )
    unit_sql = "SELECT DISTINCT unit FROM parts"
    unit_params = []
    if scope["id"]:
        unit_sql += " WHERE category_id = ?"
        unit_params.append(scope["id"])
    units = [r["unit"] for r in db.query(unit_sql + " ORDER BY unit", unit_params)]
    # Текущие параметры — чтобы сортировка и период не сбрасывали фильтр.
    keep = {k: v for k, v in f.items() if k in FILTER_KEYS and v}
    filtered = bool(keep)
    if scope["key"] == "all" and not f["group"]:
        keep["group"] = "0"
    if (f["sort"], f["dir"]) != ("name", "asc"):
        keep.update(sort=f["sort"], dir=f["dir"])
    advanced = any(f[f"{k}_{b}"] for k in RANGE_FIELDS for b in ("min", "max")) or bool(f["unit"])
    return render_template(
        "warehouse/items.html", scope=scope, page=page, f=f, totals=totals, units=units,
        keep=keep, stock_states=STOCK_STATES, presets=period_presets(), start=start, end=end,
        filtered=filtered, advanced=advanced,
    )


# ---------- Карточка позиции ----------

@bp.route("/new", methods=["GET", "POST"])
def create():
    if request.method == "POST":
        data = part_form_data()
        initial = max(0, form_float("initial_quantity"))
        if not data["name"]:
            flash("Укажите наименование.", "error")
            return render_form(data)
        part_id = db.execute(
            f"INSERT INTO parts ({', '.join(PART_COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(PART_COLUMNS))})",
            tuple(data[c] for c in PART_COLUMNS),
        )
        if initial > 0:
            move_stock(part_id, "adjust", initial, price=data["purchase_price"],
                       note="Начальный остаток")
        db.commit()
        flash("Позиция добавлена на склад.", "success")
        if request.form.get("add_more"):
            return redirect(url_for("warehouse.create", category_id=data["category_id"] or ""))
        return redirect(url_for("warehouse.view", part_id=part_id))
    return render_form({"is_active": 1, "unit": "шт",
                        "category_id": to_int(request.args.get("category_id"))})


@bp.route("/<int:part_id>")
def view(part_id):
    part = get_part(part_id)
    movements = db.query(
        """
        SELECT m.*, o.number AS order_number, o.year AS order_year
        FROM stock_movements m LEFT JOIN orders o ON o.id = m.order_id
        WHERE m.part_id = ? ORDER BY m.id DESC LIMIT 300
        """,
        (part_id,),
    )
    turnover = db.query(
        f"SELECT COALESCE(SUM({INCOMING_SQL}), 0) AS incoming, "
        f"COALESCE(SUM({OUTGOING_SQL}), 0) AS outgoing FROM stock_movements WHERE part_id = ?",
        (part_id,), one=True,
    )
    return render_template("warehouse/view.html", part=part, movements=movements,
                           turnover=turnover)


@bp.route("/<int:part_id>/edit", methods=["GET", "POST"])
def edit(part_id):
    part = get_part(part_id)
    if request.method == "POST":
        data = part_form_data()
        if not data["name"]:
            flash("Укажите наименование.", "error")
            return render_form({**dict(part), **data}, edit=True)
        db.execute(
            f"UPDATE parts SET {', '.join(c + ' = ?' for c in PART_COLUMNS)} WHERE id = ?",
            (*(data[c] for c in PART_COLUMNS), part_id),
        )
        db.commit()
        flash("Позиция сохранена.", "success")
        return redirect(url_for("warehouse.view", part_id=part_id))
    return render_form(part, edit=True)


@bp.route("/<int:part_id>/delete", methods=["POST"])
def delete(part_id):
    part = get_part(part_id)
    used = db.scalar("SELECT COUNT(*) FROM order_parts WHERE part_id = ?", (part_id,))
    if used or part["quantity"]:
        db.execute("UPDATE parts SET is_active = 0 WHERE id = ?", (part_id,))
        flash("Позиция есть в заказ-нарядах или имеет остаток, поэтому перенесена в архив.",
              "success")
    else:
        db.execute("DELETE FROM parts WHERE id = ?", (part_id,))
        flash("Позиция удалена.", "success")
    db.commit()
    return redirect(url_for("warehouse.items", scope=part["category_id"] or "none"))


# ---------- Типы запчастей ----------

@bp.route("/categories", methods=["GET", "POST"])
def manage_categories():
    if request.method == "POST":
        action = request.form.get("action")
        category_id = to_int(request.form.get("id"))
        name = form_str("name")
        rows = categories()
        try:
            if action == "add" and name:
                db.execute("INSERT INTO part_categories (name, position) VALUES (?, ?)",
                           (name, max([r["position"] for r in rows] or [0]) + 1))
                flash(f"Тип «{name}» добавлен.", "success")
            elif action == "rename" and category_id and name:
                db.execute("UPDATE part_categories SET name = ? WHERE id = ?", (name, category_id))
                flash("Название сохранено.", "success")
            elif action in ("up", "down") and category_id:
                ids = [r["id"] for r in rows]
                i = ids.index(category_id)
                j = i - 1 if action == "up" else i + 1
                if 0 <= j < len(ids):
                    ids[i], ids[j] = ids[j], ids[i]
                    for position, cid in enumerate(ids, start=1):
                        db.execute("UPDATE part_categories SET position = ? WHERE id = ?",
                                   (position, cid))
            elif action == "delete" and category_id:
                db.execute("DELETE FROM part_categories WHERE id = ?", (category_id,))
                flash("Тип удалён. Его позиции остались на складе без типа.", "success")
            db.commit()
        except sqlite3.IntegrityError:
            flash("Тип с таким названием уже есть.", "error")
        return redirect(url_for("warehouse.manage_categories"))
    rows = db.query(
        "SELECT pc.*, (SELECT COUNT(*) FROM parts p WHERE p.category_id = pc.id) AS parts "
        "FROM part_categories pc ORDER BY pc.position, pc.name COLLATE NOCASE"
    )
    without = db.scalar("SELECT COUNT(*) FROM parts WHERE category_id IS NULL")
    return render_template("warehouse/categories.html", rows=rows, without=without)


# ---------- Приход, списание, движение ----------

@bp.route("/receipt", methods=["GET", "POST"])
def receipt():
    """Приход товара от поставщика: несколько позиций одним документом."""
    parts = db.query(
        "SELECT p.*, pc.name AS category_name FROM parts p "
        "LEFT JOIN part_categories pc ON pc.id = p.category_id WHERE p.is_active = 1 "
        "ORDER BY COALESCE(pc.position, 9999), p.name COLLATE NOCASE"
    )
    if request.method == "POST":
        supplier = form_str("supplier")
        doc = form_str("document")
        receipt_date = parse_date(form_str("date")) or parse_date(today_str())
        record_expense = bool(request.form.get("record_expense"))
        method = request.form.get("method", "cash")
        if method not in ("cash", "card", "bank"):
            method = "cash"
        update_prices = bool(request.form.get("update_prices"))

        rows = []
        for part_id, qty, price in zip(request.form.getlist("part_id"),
                                       request.form.getlist("quantity"),
                                       request.form.getlist("price")):
            part_id = to_int(part_id)
            qty = to_float(qty)
            if not part_id or qty <= 0:
                continue
            rows.append((part_id, qty, money_round(max(0, to_float(price)))))
        if not rows:
            flash("Добавьте хотя бы одну позицию с количеством больше нуля.", "error")
            return render_template("warehouse/receipt.html", parts=parts)

        note = f"Документ: {doc}" if doc else None
        total = 0.0
        repriced = []
        for part_id, qty, price in rows:
            part = get_part(part_id)
            move_stock(part_id, "receipt", qty, price=price, supplier=supplier or None, note=note)
            total += qty * price
            if update_prices and price > 0:
                change = apply_receipt_price(part_id, price)
                if change:
                    repriced.append((part["name"], *change))
        total = money_round(total)
        if record_expense and total > 0:
            description = "Приход товара"
            if supplier:
                description += f" от {supplier}"
            if doc:
                description += f", {doc}"
            db.execute(
                "INSERT INTO transactions (date, kind, category, amount, method, description) "
                "VALUES (?, 'expense', ?, ?, ?, ?)",
                (receipt_date.isoformat(), CATEGORY_PURCHASE, total, method, description),
            )
        db.commit()
        flash(f"Приход оформлен: {len(rows)} поз. на сумму {fmt_money(total)}.", "success")
        if repriced:
            shown = "; ".join(f"{name}: {fmt_money(old)} → {fmt_money(new)}"
                              for name, old, new in repriced[:10])
            if len(repriced) > 10:
                shown += f" и ещё {len(repriced) - 10}"
            flash(f"Закупка подорожала — цена продажи повышена ({len(repriced)} поз.): {shown}.",
                  "warning")
        return redirect(url_for("warehouse.movements"))
    return render_template("warehouse/receipt.html", parts=parts,
                           preselect=to_int(request.args.get("part_id")))


@bp.route("/<int:part_id>/writeoff", methods=["GET", "POST"])
def writeoff(part_id):
    part = get_part(part_id)
    if request.method == "POST":
        mode = request.form.get("mode", "writeoff")
        note = form_str("note")
        try:
            if mode == "adjust":
                actual = form_float("actual", -1)
                if actual < 0:
                    raise StockError("Укажите фактический остаток.")
                diff = round(actual - part["quantity"], 3)
                if diff == 0:
                    flash("Остаток совпадает, изменений нет.", "success")
                    return redirect(url_for("warehouse.view", part_id=part_id))
                move_stock(part_id, "adjust", diff, note=note or "Инвентаризация",
                           allow_negative=True)
            else:
                qty = form_float("quantity")
                if qty <= 0:
                    raise StockError("Количество должно быть больше нуля.")
                move_stock(part_id, "writeoff", -qty, price=part["purchase_price"],
                           note=note or "Списание")
        except StockError as e:
            flash(str(e), "error")
            return render_template("warehouse/writeoff.html", part=part)
        db.commit()
        flash("Остаток обновлён.", "success")
        return redirect(url_for("warehouse.view", part_id=part_id))
    return render_template("warehouse/writeoff.html", part=part)


@bp.route("/movements")
def movements():
    kind = request.args.get("kind", "")
    sql = """
        SELECT m.*, p.name AS part_name, p.sku, p.unit,
               o.number AS order_number, o.year AS order_year
        FROM stock_movements m
        JOIN parts p ON p.id = m.part_id
        LEFT JOIN orders o ON o.id = m.order_id
    """
    params = []
    if kind:
        sql += " WHERE m.kind = ?"
        params.append(kind)
    sql += " ORDER BY m.id DESC"
    page = db.paginate(sql, params, to_int(request.args.get("page"), 1), per_page=100)
    return render_template("warehouse/movements.html", page=page, kind=kind)
