"""Клиенты и их техника."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import db
from ..auth import role_required
from ..utils import form_str, to_int

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


@bp.route("/")
@role_required("admin", "manager")
def index():
    q = request.args.get("q", "").strip()
    sql = """
        SELECT c.*,
               (SELECT COUNT(*) FROM vehicles v WHERE v.client_id = c.id) AS vehicles_count,
               (SELECT COUNT(*) FROM orders o WHERE o.client_id = c.id) AS orders_count,
               (SELECT MAX(o.created_at) FROM orders o WHERE o.client_id = c.id) AS last_visit
        FROM clients c
    """
    params = []
    if q:
        like = f"%{q}%"
        sql += """
            WHERE c.name LIKE ? OR c.phone LIKE ? OR c.phone2 LIKE ? OR c.inn LIKE ?
               OR c.email LIKE ?
               OR c.id IN (SELECT client_id FROM vehicles
                           WHERE plate LIKE ? OR vin LIKE ? OR model LIKE ? OR make LIKE ?)
        """
        params = [like] * 9
    sql += " ORDER BY c.name COLLATE NOCASE"
    page = db.paginate(sql, params, to_int(request.args.get("page"), 1))
    return render_template("clients/index.html", page=page, q=q)


@bp.route("/new", methods=["GET", "POST"])
@role_required("admin", "manager")
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
@role_required("admin", "manager")
def view(client_id):
    client = get_client(client_id)
    vehicles = db.query(
        "SELECT * FROM vehicles WHERE client_id = ? ORDER BY id", (client_id,)
    )
    orders = db.query(
        """
        SELECT o.*, v.make, v.model, v.plate,
               COALESCE((SELECT SUM(quantity * price) FROM order_services WHERE order_id = o.id), 0)
             + COALESCE((SELECT SUM(quantity * price) FROM order_parts WHERE order_id = o.id), 0)
               AS total
        FROM orders o LEFT JOIN vehicles v ON v.id = o.vehicle_id
        WHERE o.client_id = ? ORDER BY o.id DESC
        """,
        (client_id,),
    )
    paid = db.scalar(
        "SELECT COALESCE(SUM(t.amount), 0) FROM transactions t JOIN orders o ON o.id = t.order_id "
        "WHERE o.client_id = ? AND t.kind = 'income'",
        (client_id,),
    )
    billed = sum(o["total"] for o in orders if o["status"] != "cancelled")
    return render_template("clients/view.html", client=client, vehicles=vehicles,
                           orders=orders, paid=paid, billed=billed)


@bp.route("/<int:client_id>/edit", methods=["GET", "POST"])
@role_required("admin", "manager")
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
@role_required("admin")
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
@role_required("admin", "manager")
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
@role_required("admin", "manager")
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
@role_required("admin", "manager")
def vehicle_delete(vehicle_id):
    vehicle = get_vehicle(vehicle_id)
    if db.scalar("SELECT COUNT(*) FROM orders WHERE vehicle_id = ?", (vehicle_id,)):
        flash("Эта техника есть в заказ-нарядах, удалить её нельзя.", "error")
    else:
        db.execute("DELETE FROM vehicles WHERE id = ?", (vehicle_id,))
        db.commit()
        flash("Техника удалена.", "success")
    return redirect(url_for("clients.view", client_id=vehicle["client_id"]))
