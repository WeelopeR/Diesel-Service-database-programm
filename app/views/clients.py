"""Клиенты и их техника."""

import re

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import db
from ..utils import CLIENT_KINDS, form_str, like, to_int

bp = Blueprint("clients", __name__, url_prefix="/clients")

CLIENT_FIELDS = ("kind", "name", "phone", "phone2", "email", "inn", "address", "notes")
VEHICLE_FIELDS = ("make", "model", "year", "vin", "plate", "engine", "fuel_system", "notes")


def get_client(client_id):
    client = db.query("SELECT * FROM clients WHERE id = ?", (client_id,), one=True)
    if client is None:
        abort(404)
    return client


def get_vehicle(vehicle_id):
    vehicle = db.query("SELECT * FROM vehicles WHERE id = ?", (vehicle_id,), one=True)
    if vehicle is None:
        abort(404)
    return vehicle


def client_form_data():
    data = {f: form_str(f) for f in CLIENT_FIELDS}
    if data["kind"] not in ("person", "company"):
        data["kind"] = "person"
    return data


def vehicle_form_data(prefix=""):
    data = {f: form_str(prefix + f) for f in VEHICLE_FIELDS}
    data["year"] = to_int(data["year"])
    data["vin"] = data["vin"].upper()
    data["plate"] = data["plate"].upper()
    return data


def vehicle_has_data(data):
    return any(data[f] for f in ("make", "model", "vin", "plate", "engine", "fuel_system"))


def insert_vehicle(client_id, data):
    return db.execute(
        "INSERT INTO vehicles (client_id, make, model, year, vin, plate, engine, fuel_system, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (client_id, data["make"], data["model"], data["year"], data["vin"], data["plate"],
         data["engine"], data["fuel_system"], data["notes"]),
    )


CLIENT_SORTS = {
    "name": ("По имени", "c.name COLLATE NOCASE"),
    "recent": ("Недавние визиты", "last_visit IS NULL, last_visit DESC"),
    "debt": ("По сумме долга", "debt DESC, c.name COLLATE NOCASE"),
    "orders": ("По числу заказов", "orders_count DESC, c.name COLLATE NOCASE"),
}


@bp.route("/")
def index():
    f = {k: (request.args.get(k) or "").strip() for k in ("q", "vehicle", "kind", "debt", "sort")}
    if f["sort"] not in CLIENT_SORTS:
        f["sort"] = "name"
    where, params = [], []
    if f["q"]:
        digits = re.sub(r"\D", "", f["q"])
        cond = "LOWER_RU(c.name) LIKE ? OR LOWER_RU(c.inn) LIKE ? OR LOWER_RU(c.email) LIKE ?"
        params += [like(f["q"])] * 3
        if len(digits) >= 3:
            cond += " OR DIGITS(c.phone) LIKE ? OR DIGITS(c.phone2) LIKE ?"
            params += [f"%{digits}%"] * 2
        where.append(f"({cond})")
    if f["vehicle"]:
        where.append(
            "c.id IN (SELECT client_id FROM vehicles WHERE "
            "LOWER_RU(COALESCE(make, '') || ' ' || COALESCE(model, '')) LIKE ? "
            "OR LOWER_RU(plate) LIKE ? OR LOWER_RU(vin) LIKE ?)"
        )
        params += [like(f["vehicle"])] * 3
    if f["kind"] in CLIENT_KINDS:
        where.append("c.kind = ?")
        params.append(f["kind"])

    # Долг — выданные, но не полностью оплаченные заказ-наряды.
    sql = f"""
        SELECT c.*,
               COUNT(o.id) AS orders_count,
               MAX(o.created_at) AS last_visit,
               COALESCE(SUM(CASE WHEN o.status = 'issued' AND ot.due > 0.009
                                 THEN ot.due END), 0) AS debt,
               (SELECT GROUP_CONCAT(TRIM(COALESCE(v.make, '') || ' ' || COALESCE(v.model, ''))
                    || CASE WHEN COALESCE(v.plate, '') <> '' THEN ' · ' || v.plate ELSE '' END,
                    ', ')
                FROM vehicles v WHERE v.client_id = c.id) AS vehicles
        FROM clients c
        LEFT JOIN orders o ON o.client_id = c.id
        LEFT JOIN order_totals ot ON ot.order_id = o.id
        {"WHERE " + " AND ".join(where) if where else ""}
        GROUP BY c.id
        {"HAVING debt > 0.009" if f["debt"] else ""}
        ORDER BY {CLIENT_SORTS[f["sort"]][1]}
    """
    page = db.paginate(sql, params, to_int(request.args.get("page"), 1))
    totals = db.query(
        f"SELECT COUNT(*) AS n, COALESCE(SUM(debt), 0) AS debt, "
        f"SUM(CASE WHEN debt > 0.009 THEN 1 ELSE 0 END) AS debtors FROM ({sql})",
        params, one=True,
    )
    keep = {k: v for k, v in f.items() if v and k != "sort"}
    return render_template("clients/index.html", page=page, f=f, totals=totals,
                           sorts=CLIENT_SORTS, filtered=bool(keep))


@bp.route("/new", methods=["GET", "POST"])
def create():
    next_url = request.values.get("next", "")
    if request.method == "POST":
        data = client_form_data()
        vehicle = vehicle_form_data("v_")
        if not data["name"]:
            flash("Укажите имя клиента или название организации.", "error")
            return render_template("clients/form.html", client=data, vehicle=vehicle,
                                   next_url=next_url)
        client_id = db.execute(
            "INSERT INTO clients (kind, name, phone, phone2, email, inn, address, notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(data[f] for f in CLIENT_FIELDS),
        )
        if vehicle_has_data(vehicle):
            insert_vehicle(client_id, vehicle)
        db.commit()
        flash("Клиент добавлен.", "success")
        if next_url == "order":
            return redirect(url_for("orders.create", client_id=client_id))
        return redirect(url_for("clients.view", client_id=client_id))
    return render_template("clients/form.html", client={"kind": "person"}, vehicle={},
                           next_url=next_url)


@bp.route("/<int:client_id>")
def view(client_id):
    client = get_client(client_id)
    vehicles = db.query(
        "SELECT * FROM vehicles WHERE client_id = ? ORDER BY id", (client_id,)
    )
    orders = db.query(
        """
        SELECT o.*, v.make, v.model, v.plate, ot.total, ot.paid, ot.due
        FROM orders o
        JOIN order_totals ot ON ot.order_id = o.id
        LEFT JOIN vehicles v ON v.id = o.vehicle_id
        WHERE o.client_id = ? ORDER BY o.year DESC, o.number DESC
        """,
        (client_id,),
    )
    valid = [o for o in orders if o["status"] != "cancelled"]
    stats = {
        "billed": sum(o["total"] for o in valid),
        "paid": sum(o["paid"] for o in valid),
        "debt": sum(o["due"] for o in valid if o["status"] == "issued" and o["due"] > 0.009),
        "open_due": sum(o["due"] for o in valid if o["status"] != "issued" and o["due"] > 0.009),
    }
    return render_template("clients/view.html", client=client, vehicles=vehicles,
                           orders=orders, stats=stats)


@bp.route("/<int:client_id>/edit", methods=["GET", "POST"])
def edit(client_id):
    client = get_client(client_id)
    if request.method == "POST":
        data = client_form_data()
        if not data["name"]:
            flash("Укажите имя клиента или название организации.", "error")
            return render_template("clients/form.html", client=data, edit=True)
        db.execute(
            "UPDATE clients SET kind=?, name=?, phone=?, phone2=?, email=?, inn=?, address=?, "
            "notes=? WHERE id=?",
            (*(data[f] for f in CLIENT_FIELDS), client_id),
        )
        db.commit()
        flash("Данные клиента сохранены.", "success")
        return redirect(url_for("clients.view", client_id=client_id))
    return render_template("clients/form.html", client=client, edit=True)


@bp.route("/<int:client_id>/delete", methods=["POST"])
def delete(client_id):
    get_client(client_id)
    if db.scalar("SELECT COUNT(*) FROM orders WHERE client_id = ?", (client_id,)):
        flash("Нельзя удалить клиента, у которого есть заказ-наряды.", "error")
        return redirect(url_for("clients.view", client_id=client_id))
    db.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    db.commit()
    flash("Клиент удалён.", "success")
    return redirect(url_for("clients.index"))


@bp.route("/<int:client_id>/vehicles/new", methods=["GET", "POST"])
def vehicle_create(client_id):
    client = get_client(client_id)
    if request.method == "POST":
        data = vehicle_form_data()
        if not vehicle_has_data(data):
            flash("Заполните хотя бы марку, модель, госномер или VIN.", "error")
            return render_template("clients/vehicle_form.html", client=client, vehicle=data)
        insert_vehicle(client_id, data)
        db.commit()
        flash("Техника добавлена.", "success")
        if request.form.get("next") == "order":
            return redirect(url_for("orders.create", client_id=client_id))
        return redirect(url_for("clients.view", client_id=client_id))
    return render_template("clients/vehicle_form.html", client=client, vehicle={},
                           next_url=request.args.get("next", ""))


@bp.route("/vehicles/<int:vehicle_id>/edit", methods=["GET", "POST"])
def vehicle_edit(vehicle_id):
    vehicle = get_vehicle(vehicle_id)
    client = get_client(vehicle["client_id"])
    if request.method == "POST":
        data = vehicle_form_data()
        if not vehicle_has_data(data):
            flash("Заполните хотя бы марку, модель, госномер или VIN.", "error")
            return render_template("clients/vehicle_form.html", client=client, vehicle=data,
                                   edit=True)
        db.execute(
            "UPDATE vehicles SET make=?, model=?, year=?, vin=?, plate=?, engine=?, fuel_system=?, "
            "notes=? WHERE id=?",
            (*(data[f] for f in VEHICLE_FIELDS), vehicle_id),
        )
        db.commit()
        flash("Данные техники сохранены.", "success")
        return redirect(url_for("clients.view", client_id=client["id"]))
    return render_template("clients/vehicle_form.html", client=client, vehicle=vehicle, edit=True)


@bp.route("/vehicles/<int:vehicle_id>/delete", methods=["POST"])
def vehicle_delete(vehicle_id):
    vehicle = get_vehicle(vehicle_id)
    if db.scalar("SELECT COUNT(*) FROM orders WHERE vehicle_id = ?", (vehicle_id,)):
        flash("Эта техника есть в заказ-нарядах, удалить её нельзя.", "error")
    else:
        db.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        db.commit()
        flash("Техника удалена.", "success")
    return redirect(url_for("clients.view", client_id=vehicle["client_id"]))
