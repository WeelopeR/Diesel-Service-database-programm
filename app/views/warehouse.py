"""Склад: номенклатура, приход, списание, инвентаризация и история движения."""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import db
from ..auth import role_required
from ..stock import StockError, move_stock
from ..utils import (
    CATEGORY_PURCHASE, fmt_money, form_float, form_str, money_round, parse_date, to_float, to_int,
    today_str,
)

bp = Blueprint("warehouse", __name__, url_prefix="/warehouse")


def get_part(part_id):
    part = db.query("SELECT * FROM parts WHERE id = ?", (part_id,), one=True)
    if part is None:
        abort(404)
    return part


def part_form_data():
    return {
        "sku": form_str("sku"),
        "name": form_str("name"),
        "brand": form_str("brand"),
        "unit": form_str("unit") or "шт",
        "purchase_price": money_round(max(0, form_float("purchase_price"))),
        "sale_price": money_round(max(0, form_float("sale_price"))),
        "min_quantity": max(0, form_float("min_quantity")),
        "location": form_str("location"),
        "notes": form_str("notes"),
        "is_active": 1 if request.form.get("is_active") else 0,
    }


def active_parts():
    return db.query(
        "SELECT * FROM parts WHERE is_active = 1 ORDER BY name COLLATE NOCASE"
    )


@bp.route("/")
@role_required("admin", "manager")
def index():
    q = request.args.get("q", "").strip()
    show = request.args.get("show", "active")
    sql = "SELECT * FROM parts WHERE 1 = 1"
    params = []
    if show == "low":
        sql += " AND is_active = 1 AND quantity <= min_quantity AND min_quantity > 0"
    elif show == "archived":
        sql += " AND is_active = 0"
    elif show != "all":
        sql += " AND is_active = 1"
    if q:
        like = f"%{q}%"
        sql += " AND (name LIKE ? OR sku LIKE ? OR brand LIKE ? OR location LIKE ?)"
        params += [like] * 4
    sql += " ORDER BY name COLLATE NOCASE"
    page = db.paginate(sql, params, to_int(request.args.get("page"), 1), per_page=100)
    totals = db.query(
        "SELECT COUNT(*) AS positions, COALESCE(SUM(quantity * purchase_price), 0) AS cost, "
        "COALESCE(SUM(quantity * sale_price), 0) AS retail, "
        "SUM(CASE WHEN quantity <= min_quantity AND min_quantity > 0 THEN 1 ELSE 0 END) AS low "
        "FROM parts WHERE is_active = 1",
        one=True,
    )
    return render_template("warehouse/index.html", page=page, q=q, show=show, totals=totals)


@bp.route("/new", methods=["GET", "POST"])
@role_required("admin", "manager")
def create():
    if request.method == "POST":
        data = part_form_data()
        initial = max(0, form_float("initial_quantity"))
        if not data["name"]:
            flash("Укажите наименование.", "error")
            return render_template("warehouse/form.html", part=data)
        part_id = db.execute(
            "INSERT INTO parts (sku, name, brand, unit, purchase_price, sale_price, min_quantity, "
            "location, notes, is_active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (data["sku"], data["name"], data["brand"], data["unit"], data["purchase_price"],
             data["sale_price"], data["min_quantity"], data["location"], data["notes"],
             data["is_active"]),
        )
        if initial > 0:
            move_stock(part_id, "adjust", initial, price=data["purchase_price"],
                       note="Начальный остаток")
        db.commit()
        flash("Позиция добавлена на склад.", "success")
        if request.form.get("add_more"):
            return redirect(url_for("warehouse.create"))
        return redirect(url_for("warehouse.view", part_id=part_id))
    return render_template("warehouse/form.html", part={"is_active": 1, "unit": "шт"})


@bp.route("/<int:part_id>")
@role_required("admin", "manager")
def view(part_id):
    part = get_part(part_id)
    movements = db.query(
        """
        SELECT m.*, e.full_name AS employee_name
        FROM stock_movements m LEFT JOIN employees e ON e.id = m.employee_id
        WHERE m.part_id = ? ORDER BY m.id DESC LIMIT 300
        """,
        (part_id,),
    )
    return render_template("warehouse/view.html", part=part, movements=movements)


@bp.route("/<int:part_id>/edit", methods=["GET", "POST"])
@role_required("admin", "manager")
def edit(part_id):
    part = get_part(part_id)
    if request.method == "POST":
        data = part_form_data()
        if not data["name"]:
            flash("Укажите наименование.", "error")
            return render_template("warehouse/form.html", part=data, edit=True)
        db.execute(
            "UPDATE parts SET sku=?, name=?, brand=?, unit=?, purchase_price=?, sale_price=?, "
            "min_quantity=?, location=?, notes=?, is_active=? WHERE id=?",
            (data["sku"], data["name"], data["brand"], data["unit"], data["purchase_price"],
             data["sale_price"], data["min_quantity"], data["location"], data["notes"],
             data["is_active"], part_id),
        )
        db.commit()
        flash("Позиция сохранена.", "success")
        return redirect(url_for("warehouse.view", part_id=part_id))
    return render_template("warehouse/form.html", part=part, edit=True)


@bp.route("/<int:part_id>/delete", methods=["POST"])
@role_required("admin")
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
    return redirect(url_for("warehouse.index"))


@bp.route("/receipt", methods=["GET", "POST"])
@role_required("admin", "manager")
def receipt():
    """Приход товара от поставщика: несколько позиций одним документом."""
    parts = active_parts()
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
        for part_id, qty, price, sale in zip(
            request.form.getlist("part_id"), request.form.getlist("quantity"),
            request.form.getlist("price"), request.form.getlist("sale_price"),
        ):
            part_id = to_int(part_id)
            qty = to_float(qty)
            if not part_id or qty <= 0:
                continue
            rows.append((part_id, qty, money_round(max(0, to_float(price))),
                         money_round(max(0, to_float(sale)))))
        if not rows:
            flash("Добавьте хотя бы одну позицию с количеством больше нуля.", "error")
            return render_template("warehouse/receipt.html", parts=parts)

        note = f"Документ: {doc}" if doc else None
        total = 0.0
        for part_id, qty, price, sale in rows:
            get_part(part_id)
            move_stock(part_id, "receipt", qty, price=price, supplier=supplier or None, note=note)
            total += qty * price
            if update_prices:
                if price > 0:
                    db.execute("UPDATE parts SET purchase_price = ? WHERE id = ?", (price, part_id))
                if sale > 0:
                    db.execute("UPDATE parts SET sale_price = ? WHERE id = ?", (sale, part_id))
        total = money_round(total)
        if record_expense and total > 0:
            description = "Приход товара"
            if supplier:
                description += f" от {supplier}"
            if doc:
                description += f", {doc}"
            db.execute(
                "INSERT INTO transactions (date, kind, category, amount, method, description, "
                "created_by) VALUES (?, 'expense', ?, ?, ?, ?, ?)",
                (receipt_date.isoformat(), CATEGORY_PURCHASE, total, method, description,
                 g.user["id"]),
            )
        db.commit()
        flash(f"Приход оформлен: {len(rows)} поз. на сумму {fmt_money(total)}.", "success")
        return redirect(url_for("warehouse.movements"))
    return render_template("warehouse/receipt.html", parts=parts,
                           preselect=to_int(request.args.get("part_id")))


@bp.route("/<int:part_id>/writeoff", methods=["GET", "POST"])
@role_required("admin", "manager")
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
@role_required("admin", "manager")
def movements():
    kind = request.args.get("kind", "")
    sql = """
        SELECT m.*, p.name AS part_name, p.sku, p.unit, e.full_name AS employee_name
        FROM stock_movements m
        JOIN parts p ON p.id = m.part_id
        LEFT JOIN employees e ON e.id = m.employee_id
    """
    params = []
    if kind:
        sql += " WHERE m.kind = ?"
        params.append(kind)
    sql += " ORDER BY m.id DESC"
    page = db.paginate(sql, params, to_int(request.args.get("page"), 1), per_page=100)
    return render_template("warehouse/movements.html", page=page, kind=kind)
