"""Заказ-наряды: приём техники, работы, запчасти со списанием со склада,
статусы, оплаты, печатная форма, фильтры и архив прошлых лет.

Номер заказ-наряда начинается с 1 каждый календарный год. Заказ-наряды прошлых
лет попадают в архив; незакрытые и неоплаченные остаются видны в общем списке."""

import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import db
from ..stock import StockError, move_stock
from ..utils import (
    ACTIVE_STATUSES, CATEGORY_ORDER_PAYMENT, COMPLETED_STATUSES, LOCKED_STATUSES,
    ORDER_STATUSES, current_year, fmt_money, fmt_order_no, form_float, form_int,
    form_str, like, money_round, now_str, order_label, parse_date, to_int, today_str,
)
from .clients import insert_vehicle, vehicle_form_data, vehicle_has_data

bp = Blueprint("orders", __name__, url_prefix="/orders")

# Основа всех списков заказ-нарядов: клиент, техника, ответственный и суммы.
ORDER_SELECT = """
    SELECT o.*, c.name AS client_name, c.phone AS client_phone,
           v.make, v.model, v.plate, r.full_name AS responsible_name,
           ot.total, ot.paid, ot.due
    FROM orders o
    JOIN order_totals ot ON ot.order_id = o.id
    JOIN clients c ON c.id = o.client_id
    LEFT JOIN vehicles v ON v.id = o.vehicle_id
    LEFT JOIN employees r ON r.id = o.responsible_id
"""

# Долг клиента: техника выдана, а заказ-наряд оплачен не полностью.
DEBT_SQL = "(o.status = 'issued' AND ot.due > 0.009)"

FILTER_KEYS = ("number", "client", "vehicle", "master", "payment", "year")


# ---------- Вспомогательные функции ----------

def get_order(order_id):
    order = db.query(
        """
        SELECT o.*, c.name AS client_name, c.phone AS client_phone, c.phone2 AS client_phone2,
               c.kind AS client_kind, c.inn AS client_inn, c.address AS client_address,
               c.email AS client_email,
               v.make, v.model, v.year AS vehicle_year, v.vin, v.plate, v.engine, v.fuel_system,
               r.full_name AS responsible_name, ot.total, ot.paid, ot.due
        FROM orders o
        JOIN order_totals ot ON ot.order_id = o.id
        JOIN clients c ON c.id = o.client_id
        LEFT JOIN vehicles v ON v.id = o.vehicle_id
        LEFT JOIN employees r ON r.id = o.responsible_id
        WHERE o.id = ?
        """,
        (order_id,), one=True,
    )
    if order is None:
        abort(404)
    return order


def label(order):
    """«Заказ-наряд № 15/2026» — для записей на складе и в финансах."""
    return f"Заказ-наряд № {order_label(order['number'], order['year'])}"


def next_order_number(year):
    """Следующий номер в году. Номер удалённого заказ-наряда повторно не выдаётся."""
    key = f"order_seq_{year}"
    last = max(
        to_int(db.scalar("SELECT value FROM settings WHERE key = ?", (key,)), 0),
        db.scalar("SELECT COALESCE(MAX(number), 0) FROM orders WHERE year = ?", (year,)),
    )
    db.set_setting(key, str(last + 1))
    return last + 1


def masters():
    return db.query(
        "SELECT id, full_name FROM employees WHERE is_active = 1 AND is_master = 1 "
        "ORDER BY full_name COLLATE NOCASE"
    )


def is_locked(order):
    return order["status"] in LOCKED_STATUSES


def redirect_to_order(order_id, anchor=""):
    return redirect(url_for("orders.view", order_id=order_id) + (f"#{anchor}" if anchor else ""))


def ensure_editable(order):
    if is_locked(order):
        flash(f"Заказ-наряд в статусе «{ORDER_STATUSES[order['status']]}» менять нельзя. "
              "Сначала верните его в работу.", "error")
        return False
    return True


def employee_id_or_none(value):
    emp_id = to_int(value)
    if emp_id and db.scalar("SELECT 1 FROM employees WHERE id = ?", (emp_id,)):
        return emp_id
    return None


# ---------- Фильтры ----------

def parse_number(text):
    """«15» → (15, None); «15/2025» или «15-2025» → (15, 2025)."""
    match = re.fullmatch(r"(\d+)\s*(?:[/\-.]\s*(\d{4}))?", text.replace("№", "").strip())
    if not match:
        return None, None
    return int(match.group(1)), (int(match.group(2)) if match.group(2) else None)


def build_filters(args, archive, with_status=True):
    """Собирает WHERE для списка заказ-нарядов по параметрам из адресной строки."""
    f = {k: (args.get(k) or "").strip() for k in FILTER_KEYS}
    status = args.get("status") or ("all" if archive else "active")
    where, params = [], []

    if f["number"]:
        number, year = parse_number(f["number"])
        if number is None:
            where.append("0")
        else:
            where.append("o.number = ?")
            params.append(number)
            if year:
                where.append("o.year = ?")
                params.append(year)
    if f["client"]:
        digits = re.sub(r"\D", "", f["client"])
        cond = "LOWER_RU(c.name) LIKE ? OR LOWER_RU(c.inn) LIKE ?"
        params += [like(f["client"]), like(f["client"])]
        if len(digits) >= 3:
            cond += " OR DIGITS(c.phone) LIKE ? OR DIGITS(c.phone2) LIKE ?"
            params += [f"%{digits}%", f"%{digits}%"]
        where.append(f"({cond})")
    if f["vehicle"]:
        where.append(
            "(LOWER_RU(COALESCE(v.make, '') || ' ' || COALESCE(v.model, '')) LIKE ? "
            "OR LOWER_RU(v.plate) LIKE ? OR LOWER_RU(v.vin) LIKE ? OR LOWER_RU(o.units) LIKE ?)"
        )
        params += [like(f["vehicle"])] * 4
    master_id = to_int(f["master"])
    if master_id:
        where.append("(o.responsible_id = ? OR o.id IN "
                     "(SELECT order_id FROM order_services WHERE master_id = ?))")
        params += [master_id, master_id]
    if f["payment"] == "debt":
        where.append(DEBT_SQL)
    elif f["payment"] == "unpaid":
        where.append("(o.status <> 'cancelled' AND ot.due > 0.009)")
    elif f["payment"] == "paid":
        where.append("(ot.total > 0 AND ot.due <= 0.009)")

    year_now = current_year()
    if archive:
        where.append("o.year < ?")
        params.append(year_now)
        if to_int(f["year"]):
            where.append("o.year = ?")
            params.append(to_int(f["year"]))
    elif f["payment"] not in ("debt", "unpaid"):
        # В общем списке — текущий год и всё незакрытое с прошлых лет.
        # Долги показываются за все годы: они не «сгорают» 1 января.
        where.append(f"(o.year = ? OR o.status IN ({','.join('?' * len(ACTIVE_STATUSES))}))")
        params += [year_now, *ACTIVE_STATUSES]

    if with_status:
        if status == "active":
            where.append(f"o.status IN ({','.join('?' * len(ACTIVE_STATUSES))})")
            params += ACTIVE_STATUSES
        elif status in ORDER_STATUSES:
            where.append("o.status = ?")
            params.append(status)

    f["status"] = status
    return " AND ".join(where) or "1", params, f


def order_list(archive):
    where, params, filters = build_filters(request.args, archive)
    sql = f"{ORDER_SELECT} WHERE {where}"
    page = db.paginate(sql + " ORDER BY o.year DESC, o.number DESC", params,
                       to_int(request.args.get("page"), 1))
    summary = db.query(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS total, COALESCE(SUM(paid), 0) AS paid, "
        f"COALESCE(SUM(CASE WHEN status = 'issued' AND due > 0.009 THEN due END), 0) AS debt "
        f"FROM ({sql})", params, one=True,
    )
    count_where, count_params, _ = build_filters(request.args, archive, with_status=False)
    counts = {r["status"]: r["n"] for r in db.query(
        f"SELECT o.status, COUNT(*) AS n FROM orders o "
        f"JOIN order_totals ot ON ot.order_id = o.id JOIN clients c ON c.id = o.client_id "
        f"LEFT JOIN vehicles v ON v.id = o.vehicle_id WHERE {count_where} GROUP BY o.status",
        count_params,
    )}
    # Параметры фильтров без статуса — чтобы переключение статуса их не сбрасывало.
    keep = {k: v for k, v in filters.items() if v and k != "status"}
    years = [r["year"] for r in db.query(
        "SELECT DISTINCT year FROM orders WHERE year < ? ORDER BY year DESC", (current_year(),)
    )]
    return render_template(
        "orders/index.html", archive=archive, page=page, filters=filters, keep=keep,
        summary=summary, counts=counts, masters=masters(), years=years,
        filtered=any(keep.values()),
    )


# ---------- Список, архив и создание ----------

@bp.route("/")
def index():
    return order_list(archive=False)


@bp.route("/archive")
def archive():
    return order_list(archive=True)


@bp.route("/new", methods=["GET", "POST"])
def create():
    client_id = to_int(request.values.get("client_id"))
    if not client_id:
        q = request.args.get("q", "").strip()
        sql = """
            SELECT c.*, (SELECT GROUP_CONCAT(COALESCE(NULLIF(plate, ''), model), ', ')
                         FROM vehicles WHERE client_id = c.id) AS vehicles
            FROM clients c
        """
        if q:
            digits = re.sub(r"\D", "", q)
            clients = db.query(
                sql + """
                WHERE LOWER_RU(c.name) LIKE ? OR LOWER_RU(c.inn) LIKE ?
                   OR (? <> '' AND (DIGITS(c.phone) LIKE ? OR DIGITS(c.phone2) LIKE ?))
                   OR c.id IN (SELECT client_id FROM vehicles WHERE LOWER_RU(plate) LIKE ?
                               OR LOWER_RU(vin) LIKE ? OR LOWER_RU(model) LIKE ?)
                ORDER BY c.name COLLATE NOCASE LIMIT 30
                """,
                [like(q), like(q), digits, f"%{digits}%", f"%{digits}%",
                 like(q), like(q), like(q)],
            )
        else:
            clients = db.query(
                sql + "ORDER BY (SELECT MAX(id) FROM orders WHERE client_id = c.id) DESC, "
                      "c.id DESC LIMIT 15"
            )
        return render_template("orders/choose_client.html", clients=clients, q=q)

    client = db.query("SELECT * FROM clients WHERE id = ?", (client_id,), one=True)
    if client is None:
        abort(404)
    vehicles = db.query("SELECT * FROM vehicles WHERE client_id = ? ORDER BY id DESC",
                        (client_id,))

    if request.method == "POST":
        vehicle_choice = request.form.get("vehicle_id", "")
        vehicle_id = None
        if vehicle_choice == "new":
            vdata = vehicle_form_data("v_")
            if vehicle_has_data(vdata):
                vehicle_id = insert_vehicle(client_id, vdata)
        else:
            vehicle_id = to_int(vehicle_choice)
            if vehicle_id and not any(v["id"] == vehicle_id for v in vehicles):
                vehicle_id = None
        planned = parse_date(form_str("planned_date"))
        year = current_year()
        number = next_order_number(year)
        order_id = db.execute(
            "INSERT INTO orders (year, number, client_id, vehicle_id, mileage, complaint, units, "
            "notes, responsible_id, planned_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (year, number, client_id, vehicle_id, form_int("mileage"), form_str("complaint"),
             form_str("units"), form_str("notes"),
             employee_id_or_none(request.form.get("responsible_id")),
             planned.isoformat() if planned else None),
        )
        db.commit()
        flash(f"Заказ-наряд № {number} создан. Добавьте работы и запчасти.", "success")
        return redirect_to_order(order_id)

    return render_template("orders/form.html", client=client, vehicles=vehicles, order={},
                           masters=masters(),
                           preselect_vehicle=to_int(request.args.get("vehicle_id")))


# ---------- Карточка заказ-наряда ----------

@bp.route("/<int:order_id>")
def view(order_id):
    order = get_order(order_id)
    service_lines = db.query(
        """
        SELECT os.*, e.full_name AS master_name
        FROM order_services os LEFT JOIN employees e ON e.id = os.master_id
        WHERE os.order_id = ? ORDER BY os.id
        """,
        (order_id,),
    )
    part_lines = db.query(
        "SELECT op.*, p.quantity AS stock_quantity FROM order_parts op "
        "JOIN parts p ON p.id = op.part_id WHERE op.order_id = ? ORDER BY op.id",
        (order_id,),
    )
    payments = db.query(
        "SELECT * FROM transactions WHERE order_id = ? ORDER BY date, id", (order_id,)
    )
    catalog_services = db.query(
        "SELECT * FROM services WHERE is_active = 1 "
        "ORDER BY COALESCE(NULLIF(category, ''), 'яяя'), name COLLATE NOCASE"
    )
    catalog_parts = db.query(
        "SELECT p.*, pc.name AS category_name FROM parts p "
        "LEFT JOIN part_categories pc ON pc.id = p.category_id WHERE p.is_active = 1 "
        "ORDER BY COALESCE(pc.position, 9999), p.name COLLATE NOCASE"
    )
    all_masters = masters()
    default_master = order["responsible_id"] if any(
        m["id"] == order["responsible_id"] for m in all_masters) else None
    return render_template(
        "orders/view.html", order=order, service_lines=service_lines, part_lines=part_lines,
        payments=payments,
        services_total=sum(line["quantity"] * line["price"] for line in service_lines),
        parts_total=sum(line["quantity"] * line["price"] for line in part_lines),
        catalog_services=catalog_services, catalog_parts=catalog_parts, masters=all_masters,
        default_master=default_master, locked=is_locked(order), today=today_str(),
        due=money_round(order["due"]),
    )


@bp.route("/<int:order_id>/edit", methods=["GET", "POST"])
def edit(order_id):
    order = get_order(order_id)
    client = db.query("SELECT * FROM clients WHERE id = ?", (order["client_id"],), one=True)
    vehicles = db.query("SELECT * FROM vehicles WHERE client_id = ? ORDER BY id DESC",
                        (order["client_id"],))
    if request.method == "POST":
        vehicle_choice = request.form.get("vehicle_id", "")
        if vehicle_choice == "new":
            vdata = vehicle_form_data("v_")
            vehicle_id = insert_vehicle(order["client_id"], vdata) if vehicle_has_data(vdata) \
                else None
        else:
            vehicle_id = to_int(vehicle_choice)
            if vehicle_id and not any(v["id"] == vehicle_id for v in vehicles):
                vehicle_id = order["vehicle_id"]
        planned = parse_date(form_str("planned_date"))
        db.execute(
            "UPDATE orders SET vehicle_id=?, mileage=?, complaint=?, units=?, diagnosis=?, "
            "recommendations=?, notes=?, responsible_id=?, planned_date=? WHERE id=?",
            (vehicle_id, form_int("mileage"), form_str("complaint"), form_str("units"),
             form_str("diagnosis"), form_str("recommendations"), form_str("notes"),
             employee_id_or_none(request.form.get("responsible_id")),
             planned.isoformat() if planned else None, order_id),
        )
        db.commit()
        flash("Заказ-наряд сохранён.", "success")
        return redirect_to_order(order_id)
    return render_template("orders/form.html", client=client, vehicles=vehicles, order=order,
                           masters=masters(), edit=True, preselect_vehicle=order["vehicle_id"])


@bp.route("/<int:order_id>/diagnosis", methods=["POST"])
def update_diagnosis(order_id):
    get_order(order_id)
    db.execute(
        "UPDATE orders SET diagnosis = ?, recommendations = ? WHERE id = ?",
        (form_str("diagnosis"), form_str("recommendations"), order_id),
    )
    db.commit()
    flash("Диагностика сохранена.", "success")
    return redirect_to_order(order_id, "diagnosis")


@bp.route("/<int:order_id>/status", methods=["POST"])
def change_status(order_id):
    order = get_order(order_id)
    new = request.form.get("status")
    if new not in ORDER_STATUSES or new == order["status"]:
        return redirect_to_order(order_id)

    if new == "cancelled":
        if order["paid"] > 0:
            flash(f"По заказ-наряду есть оплаты на {fmt_money(order['paid'])}. "
                  "Удалите их или оформите возврат расходом, затем отменяйте.", "error")
            return redirect_to_order(order_id)
        for line in db.query("SELECT * FROM order_parts WHERE order_id = ?", (order_id,)):
            move_stock(line["part_id"], "return", line["quantity"], order_id=order_id,
                       note=f"Отмена: {label(order)}")
        db.execute("DELETE FROM order_parts WHERE order_id = ?", (order_id,))

    completed_at = order["completed_at"]
    if new in COMPLETED_STATUSES:
        completed_at = completed_at or now_str()
    else:
        completed_at = None
    issued_at = (order["issued_at"] or now_str()) if new == "issued" else None

    db.execute("UPDATE orders SET status = ?, completed_at = ?, issued_at = ? WHERE id = ?",
               (new, completed_at, issued_at, order_id))
    db.commit()

    message = f"Статус изменён: «{ORDER_STATUSES[new]}»."
    if new == "cancelled":
        message += " Запчасти возвращены на склад."
    due = money_round(order["due"])
    if new == "issued" and due > 0:
        flash(message + f" Внимание: не оплачено {fmt_money(due)}.", "warning")
    else:
        flash(message, "success")
    return redirect_to_order(order_id)


@bp.route("/<int:order_id>/delete", methods=["POST"])
def delete(order_id):
    order = get_order(order_id)
    if order["paid"] > 0:
        flash("Нельзя удалить заказ-наряд с оплатами. Сначала удалите оплаты.", "error")
        return redirect_to_order(order_id)
    for line in db.query("SELECT * FROM order_parts WHERE order_id = ?", (order_id,)):
        move_stock(line["part_id"], "return", line["quantity"],
                   note=f"Удалён {label(order).lower()}")
    db.execute("DELETE FROM orders WHERE id = ?", (order_id,))
    db.commit()
    flash(f"Заказ-наряд № {fmt_order_no(order)} удалён.", "success")
    return redirect(url_for("orders.index"))


# ---------- Работы ----------

@bp.route("/<int:order_id>/services/add", methods=["POST"])
def add_service(order_id):
    order = get_order(order_id)
    if not ensure_editable(order):
        return redirect_to_order(order_id, "services")
    service = None
    service_id = to_int(request.form.get("service_id"))
    if service_id:
        service = db.query("SELECT * FROM services WHERE id = ?", (service_id,), one=True)
    name = form_str("name") or (service["name"] if service else "")
    if not name:
        flash("Выберите услугу из прайса или впишите название работы.", "error")
        return redirect_to_order(order_id, "services")

    price = form_float("price", None)
    if price is None:
        price = service["price"] if service else 0
    percent = form_float("master_percent", None)
    if percent is None:
        percent = service["master_percent"] if service else 0
    quantity = form_float("quantity", 1) or 1

    db.execute(
        "INSERT INTO order_services (order_id, service_id, name, quantity, price, master_id, "
        "master_percent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (order_id, service["id"] if service else None, name, max(quantity, 0.001),
         money_round(max(price, 0)), employee_id_or_none(request.form.get("master_id")),
         min(100, max(0, percent))),
    )
    db.commit()
    return redirect_to_order(order_id, "services")


@bp.route("/<int:order_id>/services/<int:line_id>/update", methods=["POST"])
def update_service(order_id, line_id):
    order = get_order(order_id)
    line = db.query("SELECT * FROM order_services WHERE id = ? AND order_id = ?",
                    (line_id, order_id), one=True)
    if line is None:
        abort(404)
    if not ensure_editable(order):
        return redirect_to_order(order_id, "services")
    db.execute(
        "UPDATE order_services SET name=?, quantity=?, price=?, master_id=?, master_percent=? "
        "WHERE id=?",
        (form_str("name") or line["name"], max(form_float("quantity", 1), 0.001),
         money_round(max(form_float("price"), 0)),
         employee_id_or_none(request.form.get("master_id")),
         min(100, max(0, form_float("master_percent", line["master_percent"]))), line_id),
    )
    db.commit()
    return redirect_to_order(order_id, "services")


@bp.route("/<int:order_id>/services/<int:line_id>/delete", methods=["POST"])
def delete_service(order_id, line_id):
    order = get_order(order_id)
    if ensure_editable(order):
        db.execute("DELETE FROM order_services WHERE id = ? AND order_id = ?", (line_id, order_id))
        db.commit()
    return redirect_to_order(order_id, "services")


# ---------- Запчасти (списываются со склада сразу) ----------

@bp.route("/<int:order_id>/parts/add", methods=["POST"])
def add_part(order_id):
    order = get_order(order_id)
    if not ensure_editable(order):
        return redirect_to_order(order_id, "parts")
    part = db.query("SELECT * FROM parts WHERE id = ?", (to_int(request.form.get("part_id")),),
                    one=True)
    quantity = form_float("quantity", 1)
    if part is None or quantity <= 0:
        flash("Выберите запчасть и укажите количество.", "error")
        return redirect_to_order(order_id, "parts")
    price = form_float("price", None)
    if price is None:
        price = part["sale_price"]
    try:
        move_stock(part["id"], "order", -quantity, price=part["purchase_price"], order_id=order_id,
                   note=label(order))
    except StockError as e:
        flash(str(e), "error")
        return redirect_to_order(order_id, "parts")
    existing = db.query(
        "SELECT * FROM order_parts WHERE order_id = ? AND part_id = ? AND price = ?",
        (order_id, part["id"], money_round(price)), one=True,
    )
    if existing:
        db.execute("UPDATE order_parts SET quantity = ? WHERE id = ?",
                   (round(existing["quantity"] + quantity, 3), existing["id"]))
    else:
        db.execute(
            "INSERT INTO order_parts (order_id, part_id, name, sku, unit, quantity, price, "
            "purchase_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (order_id, part["id"], part["name"], part["sku"], part["unit"], quantity,
             money_round(max(price, 0)), part["purchase_price"]),
        )
    db.commit()
    return redirect_to_order(order_id, "parts")


@bp.route("/<int:order_id>/parts/<int:line_id>/update", methods=["POST"])
def update_part(order_id, line_id):
    order = get_order(order_id)
    line = db.query("SELECT * FROM order_parts WHERE id = ? AND order_id = ?",
                    (line_id, order_id), one=True)
    if line is None:
        abort(404)
    if not ensure_editable(order):
        return redirect_to_order(order_id, "parts")
    quantity = form_float("quantity", line["quantity"])
    if quantity <= 0:
        flash("Количество должно быть больше нуля. Чтобы убрать запчасть, удалите строку.", "error")
        return redirect_to_order(order_id, "parts")
    diff = round(quantity - line["quantity"], 3)
    try:
        if diff:
            move_stock(line["part_id"], "order" if diff > 0 else "return", -diff,
                       order_id=order_id, note=label(order))
    except StockError as e:
        flash(str(e), "error")
        return redirect_to_order(order_id, "parts")
    db.execute("UPDATE order_parts SET quantity = ?, price = ? WHERE id = ?",
               (quantity, money_round(max(form_float("price", line["price"]), 0)), line_id))
    db.commit()
    return redirect_to_order(order_id, "parts")


@bp.route("/<int:order_id>/parts/<int:line_id>/delete", methods=["POST"])
def delete_part(order_id, line_id):
    order = get_order(order_id)
    line = db.query("SELECT * FROM order_parts WHERE id = ? AND order_id = ?",
                    (line_id, order_id), one=True)
    if line is None:
        abort(404)
    if ensure_editable(order):
        move_stock(line["part_id"], "return", line["quantity"], order_id=order_id,
                   note=f"Убрано из: {label(order).lower()}")
        db.execute("DELETE FROM order_parts WHERE id = ?", (line_id,))
        db.commit()
        flash(f"«{line['name']}» возвращено на склад.", "success")
    return redirect_to_order(order_id, "parts")


# ---------- Оплаты ----------

@bp.route("/<int:order_id>/payments/add", methods=["POST"])
def add_payment(order_id):
    order = get_order(order_id)
    if order["status"] == "cancelled":
        flash("Отменённый заказ-наряд оплатить нельзя.", "error")
        return redirect_to_order(order_id, "payments")
    amount = money_round(form_float("amount"))
    if amount <= 0:
        flash("Сумма оплаты должна быть больше нуля.", "error")
        return redirect_to_order(order_id, "payments")
    method = request.form.get("method", "cash")
    if method not in ("cash", "card", "bank"):
        method = "cash"
    pay_date = parse_date(form_str("date")) or parse_date(today_str())
    note = form_str("note")
    db.execute(
        "INSERT INTO transactions (date, kind, category, amount, method, description, order_id) "
        "VALUES (?, 'income', ?, ?, ?, ?, ?)",
        (pay_date.isoformat(), CATEGORY_ORDER_PAYMENT, amount, method,
         f"{label(order)}, {order['client_name']}" + (f" — {note}" if note else ""), order_id),
    )
    db.commit()
    flash(f"Оплата {fmt_money(amount)} принята и записана в доходы.", "success")
    return redirect_to_order(order_id, "payments")


@bp.route("/<int:order_id>/payments/<int:tx_id>/delete", methods=["POST"])
def delete_payment(order_id, tx_id):
    get_order(order_id)
    db.execute("DELETE FROM transactions WHERE id = ? AND order_id = ?", (tx_id, order_id))
    db.commit()
    flash("Оплата удалена.", "success")
    return redirect_to_order(order_id, "payments")


# ---------- Печать ----------

@bp.route("/<int:order_id>/warranty")
def warranty(order_id):
    """Гарантия по заказ-наряду. Шаблон гарантийного документа пока не оформлен —
    страница-заготовка, чтобы кнопка уже была на месте."""
    return render_template("orders/warranty.html", order=get_order(order_id))


@bp.route("/<int:order_id>/print")
def print_order(order_id):
    order = get_order(order_id)
    service_lines = db.query(
        "SELECT os.*, e.full_name AS master_name FROM order_services os "
        "LEFT JOIN employees e ON e.id = os.master_id WHERE os.order_id = ? ORDER BY os.id",
        (order_id,),
    )
    part_lines = db.query("SELECT * FROM order_parts WHERE order_id = ? ORDER BY id", (order_id,))
    return render_template(
        "orders/print.html", order=order, service_lines=service_lines, part_lines=part_lines,
        services_total=sum(x["quantity"] * x["price"] for x in service_lines),
        parts_total=sum(x["quantity"] * x["price"] for x in part_lines),
        settings=db.get_settings(), due=money_round(order["due"]),
    )

