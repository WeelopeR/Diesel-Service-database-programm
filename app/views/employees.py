"""Сотрудники и учётные записи для входа."""

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .. import db
from ..auth import hash_password, role_required, validate_password
from ..utils import ROLES, form_str

bp = Blueprint("employees", __name__, url_prefix="/employees")


def get_employee(employee_id):
    emp = db.query("SELECT * FROM employees WHERE id = ?", (employee_id,), one=True)
    if emp is None:
        abort(404)
    return emp


def form_data():
    role = request.form.get("role", "master")
    return {
        "full_name": form_str("full_name"),
        "position": form_str("position"),
        "phone": form_str("phone"),
        "role": role if role in ROLES else "master",
        "username": form_str("username") or None,
        "is_master": 1 if request.form.get("is_master") else 0,
        "is_active": 1 if request.form.get("is_active") else 0,
        "notes": form_str("notes"),
    }


def validate(data, employee_id=None, password="", require_password=False):
    errors = []
    if not data["full_name"]:
        errors.append("Укажите ФИО.")
    if data["username"]:
        clash = db.scalar(
            "SELECT id FROM employees WHERE username = ? AND id <> ?",
            (data["username"], employee_id or 0),
        )
        if clash:
            errors.append("Такой логин уже занят.")
    if password:
        problem = validate_password(password)
        if problem:
            errors.append(problem)
        if not data["username"]:
            errors.append("Чтобы задать пароль, укажите логин.")
    elif require_password and data["username"]:
        errors.append("Для нового входа в программу задайте пароль.")
    return errors


def admins_left(excluding_id):
    return db.scalar(
        "SELECT COUNT(*) FROM employees WHERE role = 'admin' AND is_active = 1 "
        "AND password_hash IS NOT NULL AND id <> ?",
        (excluding_id,),
    )


@bp.route("/")
@role_required("admin")
def index():
    rows = db.query("SELECT * FROM employees ORDER BY is_active DESC, full_name COLLATE NOCASE")
    return render_template("employees/index.html", rows=rows)


@bp.route("/new", methods=["GET", "POST"])
@role_required("admin")
def create():
    if request.method == "POST":
        data = form_data()
        password = request.form.get("password", "")
        errors = validate(data, password=password, require_password=True)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("employees/form.html", emp=data)
        db.execute(
            "INSERT INTO employees (full_name, position, phone, role, username, password_hash, "
            "is_master, is_active, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (data["full_name"], data["position"], data["phone"], data["role"], data["username"],
             hash_password(password) if password else None, data["is_master"],
             data["is_active"], data["notes"]),
        )
        db.commit()
        flash("Сотрудник добавлен.", "success")
        return redirect(url_for("employees.index"))
    return render_template("employees/form.html",
                           emp={"role": "master", "is_master": 1, "is_active": 1})


@bp.route("/<int:employee_id>/edit", methods=["GET", "POST"])
@role_required("admin")
def edit(employee_id):
    emp = get_employee(employee_id)
    if request.method == "POST":
        data = form_data()
        password = request.form.get("password", "")
        errors = validate(data, employee_id, password,
                          require_password=not emp["password_hash"])
        losing_admin = emp["role"] == "admin" and emp["password_hash"] and (
            data["role"] != "admin" or not data["is_active"] or not data["username"])
        if losing_admin and not admins_left(employee_id):
            errors.append("Это последний руководитель с доступом — его нельзя понизить или "
                          "отключить.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("employees/form.html", emp={**dict(emp), **data}, edit=True)
        password_hash = emp["password_hash"]
        if password:
            password_hash = hash_password(password)
        if not data["username"]:
            password_hash = None
        db.execute(
            "UPDATE employees SET full_name=?, position=?, phone=?, role=?, username=?, "
            "password_hash=?, is_master=?, is_active=?, notes=? WHERE id=?",
            (data["full_name"], data["position"], data["phone"], data["role"], data["username"],
             password_hash, data["is_master"], data["is_active"], data["notes"], employee_id),
        )
        db.commit()
        flash("Данные сотрудника сохранены.", "success")
        if employee_id == g.user["id"] and password:
            flash("Вы сменили свой пароль — войдите заново.", "success")
        return redirect(url_for("employees.index"))
    return render_template("employees/form.html", emp=emp, edit=True)
