"""Мастера: назначаются на работы в заказ-нарядах, по ним считается зарплата.
В программу они не входят — ей пользуется только руководитель."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import db
from ..utils import form_str

bp = Blueprint("employees", __name__, url_prefix="/masters")


def get_employee(employee_id):
    emp = db.query("SELECT * FROM employees WHERE id = ? AND is_master = 1", (employee_id,),
                   one=True)
    if emp is None:
        abort(404)
    return emp


def form_data():
    return {
        "full_name": form_str("full_name"),
        "position": form_str("position"),
        "phone": form_str("phone"),
        "is_active": 1 if request.form.get("is_active") else 0,
        "notes": form_str("notes"),
    }


@bp.route("/")
def index():
    rows = db.query(
        """
        SELECT e.*,
               (SELECT COUNT(*) FROM order_services os JOIN orders o ON o.id = os.order_id
                WHERE os.master_id = e.id AND o.status IN ('done', 'issued')) AS works
        FROM employees e WHERE e.is_master = 1
        ORDER BY e.is_active DESC, e.full_name COLLATE NOCASE
        """
    )
    return render_template("employees/index.html", rows=rows)


@bp.route("/new", methods=["GET", "POST"])
def create():
    if request.method == "POST":
        data = form_data()
        if not data["full_name"]:
            flash("Укажите ФИО.", "error")
            return render_template("employees/form.html", emp=data)
        db.execute(
            "INSERT INTO employees (full_name, position, phone, is_active, notes, is_master) "
            "VALUES (?, ?, ?, ?, ?, 1)",
            (data["full_name"], data["position"], data["phone"], data["is_active"],
             data["notes"]),
        )
        db.commit()
        flash("Мастер добавлен.", "success")
        return redirect(url_for("employees.index"))
    return render_template("employees/form.html", emp={"is_active": 1})


@bp.route("/<int:employee_id>/edit", methods=["GET", "POST"])
def edit(employee_id):
    emp = get_employee(employee_id)
    if request.method == "POST":
        data = form_data()
        if not data["full_name"]:
            flash("Укажите ФИО.", "error")
            return render_template("employees/form.html", emp={**dict(emp), **data}, edit=True)
        db.execute(
            "UPDATE employees SET full_name=?, position=?, phone=?, is_active=?, notes=? "
            "WHERE id=?",
            (data["full_name"], data["position"], data["phone"], data["is_active"],
             data["notes"], employee_id),
        )
        db.commit()
        flash("Данные мастера сохранены.", "success")
        return redirect(url_for("employees.index"))
    return render_template("employees/form.html", emp=emp, edit=True)
