"""Зарплата мастеров: процент от стоимости выполненных работ.

Начисление по работе = количество × цена × процент мастера / 100.
Работа попадает в зарплату, когда заказ-наряд переведён в статус «Готов»
или «Выдан» (по дате выполнения). Выплаты — это расходы в финансах,
привязанные к сотруднику."""

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import db
from ..auth import safe_next
from ..utils import (
    CATEGORY_SALARY, COMPLETED_STATUSES, fmt_date, fmt_money, form_float, form_str,
    money_round, parse_date, period_from_request, period_presets, today_str,
)

bp = Blueprint("salary", __name__, url_prefix="/salary")

_STATUS_FILTER = f"o.status IN ({','.join(repr(s) for s in COMPLETED_STATUSES)})"

ACCRUAL_SQL = f"""
    SELECT os.master_id,
           COUNT(os.id) AS works,
           COUNT(DISTINCT os.order_id) AS orders,
           COALESCE(SUM(os.quantity * os.price), 0) AS revenue,
           COALESCE(SUM(os.quantity * os.price * os.master_percent / 100.0), 0) AS accrued
    FROM order_services os JOIN orders o ON o.id = os.order_id
    WHERE {_STATUS_FILTER} AND os.master_id IS NOT NULL
"""


def accruals(start=None, end=None):
    sql, params = ACCRUAL_SQL, []
    if start and end:
        sql += " AND date(o.completed_at) BETWEEN ? AND ?"
        params = [start.isoformat(), end.isoformat()]
    sql += " GROUP BY os.master_id"
    return {r["master_id"]: r for r in db.query(sql, params)}


def payouts(start=None, end=None):
    sql = ("SELECT employee_id, SUM(amount) AS paid FROM transactions "
           "WHERE kind = 'expense' AND employee_id IS NOT NULL")
    params = []
    if start and end:
        sql += " AND date BETWEEN ? AND ?"
        params = [start.isoformat(), end.isoformat()]
    sql += " GROUP BY employee_id"
    return {r["employee_id"]: r["paid"] for r in db.query(sql, params)}


def employee_summary(employee_id, start, end):
    period = accruals(start, end).get(employee_id)
    total = accruals().get(employee_id)
    paid_period = payouts(start, end).get(employee_id, 0)
    paid_total = payouts().get(employee_id, 0)
    accrued_total = total["accrued"] if total else 0
    return {
        "works": period["works"] if period else 0,
        "orders": period["orders"] if period else 0,
        "revenue": period["revenue"] if period else 0,
        "accrued": period["accrued"] if period else 0,
        "paid": paid_period,
        "balance": money_round(accrued_total - paid_total),
    }


@bp.route("/")
def index():
    start, end = period_from_request()
    period_acc = accruals(start, end)
    total_acc = accruals()
    period_paid = payouts(start, end)
    total_paid = payouts()
    rows = []
    for emp in db.query("SELECT * FROM employees WHERE is_master = 1 "
                        "ORDER BY is_active DESC, full_name COLLATE NOCASE"):
        acc = period_acc.get(emp["id"])
        balance = money_round((total_acc[emp["id"]]["accrued"] if emp["id"] in total_acc else 0)
                              - total_paid.get(emp["id"], 0))
        if not emp["is_active"] and not acc and not balance and not period_paid.get(emp["id"]):
            continue
        rows.append({
            "employee": emp,
            "works": acc["works"] if acc else 0,
            "orders": acc["orders"] if acc else 0,
            "revenue": acc["revenue"] if acc else 0,
            "accrued": acc["accrued"] if acc else 0,
            "paid": period_paid.get(emp["id"], 0),
            "balance": balance,
        })
    totals = {k: sum(r[k] for r in rows) for k in ("works", "revenue", "accrued", "paid", "balance")}
    return render_template("salary/index.html", rows=rows, totals=totals, start=start, end=end,
                           presets=period_presets())


@bp.route("/<int:employee_id>")
def detail(employee_id):
    employee = db.query("SELECT * FROM employees WHERE id = ?", (employee_id,), one=True)
    if employee is None:
        abort(404)
    start, end = period_from_request()
    lines = db.query(
        f"""
        SELECT os.*, o.id AS order_id, o.number, o.year, o.completed_at, o.status,
               c.name AS client_name,
               v.make, v.model, v.plate,
               os.quantity * os.price AS amount,
               os.quantity * os.price * os.master_percent / 100.0 AS earned
        FROM order_services os
        JOIN orders o ON o.id = os.order_id
        JOIN clients c ON c.id = o.client_id
        LEFT JOIN vehicles v ON v.id = o.vehicle_id
        WHERE {_STATUS_FILTER} AND os.master_id = ? AND date(o.completed_at) BETWEEN ? AND ?
        ORDER BY o.completed_at, o.id, os.id
        """,
        (employee_id, start.isoformat(), end.isoformat()),
    )
    in_progress = db.query(
        f"""
        SELECT os.*, o.id AS order_id, o.number, o.year, o.status,
               os.quantity * os.price * os.master_percent / 100.0 AS earned
        FROM order_services os JOIN orders o ON o.id = os.order_id
        WHERE NOT {_STATUS_FILTER} AND o.status <> 'cancelled' AND os.master_id = ?
        ORDER BY o.id
        """,
        (employee_id,),
    )
    paid_rows = db.query(
        "SELECT * FROM transactions WHERE kind = 'expense' AND employee_id = ? "
        "AND date BETWEEN ? AND ? ORDER BY date, id",
        (employee_id, start.isoformat(), end.isoformat()),
    )
    summary = employee_summary(employee_id, start, end)
    return render_template(
        "salary/detail.html", employee=employee, lines=lines, in_progress=in_progress,
        paid_rows=paid_rows, summary=summary, start=start, end=end, presets=period_presets(),
        today=today_str(),
    )


@bp.route("/<int:employee_id>/pay", methods=["POST"])
def pay(employee_id):
    employee = db.query("SELECT * FROM employees WHERE id = ?", (employee_id,), one=True)
    if employee is None:
        abort(404)
    amount = money_round(form_float("amount"))
    if amount <= 0:
        flash("Сумма выплаты должна быть больше нуля.", "error")
    else:
        method = request.form.get("method", "cash")
        if method not in ("cash", "card", "bank"):
            method = "cash"
        pay_date = parse_date(form_str("date")) or parse_date(today_str())
        note = form_str("note")
        db.execute(
            "INSERT INTO transactions (date, kind, category, amount, method, description, "
            "employee_id) VALUES (?, 'expense', ?, ?, ?, ?, ?)",
            (pay_date.isoformat(), CATEGORY_SALARY, amount, method,
             f"Выплата: {employee['full_name']}" + (f" — {note}" if note else ""),
             employee_id),
        )
        db.commit()
        flash(f"Выплата {fmt_money(amount)} от {fmt_date(pay_date)} записана в расходы.",
              "success")
    back = safe_next(request.form.get("back"))
    return redirect(back or url_for("salary.detail", employee_id=employee_id))
