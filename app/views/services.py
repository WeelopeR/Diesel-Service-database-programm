"""Прайс-лист услуг с процентом мастеру."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import db
from ..utils import form_float, form_str, money_round

bp = Blueprint("services", __name__, url_prefix="/services")


def get_service(service_id):
    service = db.query("SELECT * FROM services WHERE id = ?", (service_id,), one=True)
    if service is None:
        abort(404)
    return service


def service_form_data():
    percent = form_float("master_percent")
    return {
        "name": form_str("name"),
        "category": form_str("category"),
        "price": money_round(max(0, form_float("price"))),
        "master_percent": min(100, max(0, percent)),
        "notes": form_str("notes"),
        "is_active": 1 if request.form.get("is_active") else 0,
    }


def categories():
    return [r["category"] for r in db.query(
        "SELECT DISTINCT category FROM services WHERE category <> '' ORDER BY category"
    )]


@bp.route("/")
def index():
    q = request.args.get("q", "").strip()
    show = request.args.get("show", "active")
    sql = "SELECT * FROM services WHERE 1 = 1"
    params = []
    if show == "active":
        sql += " AND is_active = 1"
    elif show == "archived":
        sql += " AND is_active = 0"
    if q:
        sql += " AND (name LIKE ? OR category LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY COALESCE(NULLIF(category, ''), 'яяя'), name COLLATE NOCASE"
    rows = db.query(sql, params)
    groups = {}
    for row in rows:
        groups.setdefault(row["category"] or "Без категории", []).append(row)
    return render_template("services/index.html", groups=groups, q=q, show=show,
                           total=len(rows))


@bp.route("/new", methods=["GET", "POST"])
def create():
    if request.method == "POST":
        data = service_form_data()
        if not data["name"]:
            flash("Укажите название услуги.", "error")
            return render_template("services/form.html", service=data, categories=categories())
        db.execute(
            "INSERT INTO services (name, category, price, master_percent, notes, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (data["name"], data["category"], data["price"], data["master_percent"],
             data["notes"], data["is_active"]),
        )
        db.commit()
        flash("Услуга добавлена.", "success")
        if request.form.get("add_more"):
            return redirect(url_for("services.create"))
        return redirect(url_for("services.index"))
    return render_template("services/form.html", service={"is_active": 1},
                           categories=categories())


@bp.route("/<int:service_id>/edit", methods=["GET", "POST"])
def edit(service_id):
    service = get_service(service_id)
    if request.method == "POST":
        data = service_form_data()
        if not data["name"]:
            flash("Укажите название услуги.", "error")
            return render_template("services/form.html", service=data, edit=True,
                                   categories=categories())
        db.execute(
            "UPDATE services SET name=?, category=?, price=?, master_percent=?, notes=?, "
            "is_active=? WHERE id=?",
            (data["name"], data["category"], data["price"], data["master_percent"],
             data["notes"], data["is_active"], service_id),
        )
        db.commit()
        flash("Услуга сохранена. В уже созданных заказ-нарядах цены не меняются.", "success")
        return redirect(url_for("services.index"))
    return render_template("services/form.html", service=service, edit=True,
                           categories=categories())


@bp.route("/<int:service_id>/delete", methods=["POST"])
def delete(service_id):
    get_service(service_id)
    used = db.scalar("SELECT COUNT(*) FROM order_services WHERE service_id = ?", (service_id,))
    if used:
        db.execute("UPDATE services SET is_active = 0 WHERE id = ?", (service_id,))
        flash("Услуга используется в заказ-нарядах, поэтому перенесена в архив.", "success")
    else:
        db.execute("DELETE FROM services WHERE id = ?", (service_id,))
        flash("Услуга удалена.", "success")
    db.commit()
    return redirect(url_for("services.index"))
