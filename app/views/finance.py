"""Доходы и расходы."""

import calendar
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from .. import db
from ..utils import (
    EXPENSE_CATEGORIES, INCOME_CATEGORIES, MONTHS, form_float, form_str, money_round,
    parse_date, period_from_request, period_presets, to_int, today_str,
)

bp = Blueprint("finance", __name__, url_prefix="/finance")


def get_tx(tx_id):
    tx = db.query(
        "SELECT t.*, o.number AS order_number, o.year AS order_year FROM transactions t "
        "LEFT JOIN orders o ON o.id = t.order_id WHERE t.id = ?", (tx_id,), one=True)
    if tx is None:
        abort(404)
    return tx


def all_categories(kind):
    base = INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES
    used = [r["category"] for r in db.query(
        "SELECT DISTINCT category FROM transactions WHERE kind = ? ORDER BY category", (kind,)
    )]
    return base + [c for c in used if c not in base]


def tx_form_data():
    kind = request.form.get("kind")
    method = request.form.get("method", "cash")
    return {
        "kind": kind if kind in ("income", "expense") else "expense",
        "date": (parse_date(form_str("date")) or date.today()).isoformat(),
        "category": form_str("category"),
        "amount": money_round(form_float("amount")),
        "method": method if method in ("cash", "card", "bank") else "cash",
        "description": form_str("description"),
        "employee_id": to_int(request.form.get("employee_id")),
    }


def validate(data):
    if not data["category"]:
        return "Укажите статью (категорию)."
    if data["amount"] <= 0:
        return "Сумма должна быть больше нуля."
    return None


def render_form(tx, edit=False):
    return render_template(
        "finance/form.html", tx=tx, edit=edit, employees=employees(),
        categories=all_categories(tx["kind"]),
        income_categories=all_categories("income"),
        expense_categories=all_categories("expense"),
    )


def employees():
    return db.query("SELECT id, full_name FROM employees WHERE is_active = 1 "
                    "ORDER BY full_name COLLATE NOCASE")


@bp.route("/")
def index():
    start, end = period_from_request()
    kind = request.args.get("kind", "")
    category = request.args.get("category", "")
    method = request.args.get("method", "")

    where = "WHERE t.date BETWEEN ? AND ?"
    params = [start.isoformat(), end.isoformat()]
    if kind in ("income", "expense"):
        where += " AND t.kind = ?"
        params.append(kind)
    if category:
        where += " AND t.category = ?"
        params.append(category)
    if method in ("cash", "card", "bank"):
        where += " AND t.method = ?"
        params.append(method)

    rows = db.query(
        f"""
        SELECT t.*, e.full_name AS employee_name, o.number AS order_number, o.year AS order_year
        FROM transactions t
        LEFT JOIN employees e ON e.id = t.employee_id
        LEFT JOIN orders o ON o.id = t.order_id
        {where} ORDER BY t.date DESC, t.id DESC
        """,
        params,
    )
    period = [start.isoformat(), end.isoformat()]
    summary = db.query(
        "SELECT kind, category, SUM(amount) AS total, COUNT(*) AS n FROM transactions "
        "WHERE date BETWEEN ? AND ? GROUP BY kind, category ORDER BY total DESC",
        period,
    )
    income = sum(r["total"] for r in summary if r["kind"] == "income")
    expense = sum(r["total"] for r in summary if r["kind"] == "expense")
    by_method = db.query(
        "SELECT method, SUM(CASE WHEN kind = 'income' THEN amount ELSE -amount END) AS balance, "
        "SUM(CASE WHEN kind = 'income' THEN amount ELSE 0 END) AS income, "
        "SUM(CASE WHEN kind = 'expense' THEN amount ELSE 0 END) AS expense "
        "FROM transactions WHERE date BETWEEN ? AND ? GROUP BY method",
        period,
    )
    categories = sorted({r["category"] for r in db.query(
        "SELECT DISTINCT category FROM transactions")})
    return render_template(
        "finance/index.html", rows=rows, start=start, end=end, kind=kind, category=category,
        method=method, summary=summary, income=income, expense=expense,
        profit=income - expense, by_method={r["method"]: r for r in by_method},
        categories=categories, presets=period_presets(),
    )


@bp.route("/report")
def report():
    """Помесячный отчёт за год."""
    year = to_int(request.args.get("year"), date.today().year)
    rows = db.query(
        "SELECT CAST(strftime('%m', date) AS INTEGER) AS month, kind, SUM(amount) AS total "
        "FROM transactions WHERE strftime('%Y', date) = ? GROUP BY month, kind",
        (str(year),),
    )
    months = {m: {"income": 0.0, "expense": 0.0} for m in range(1, 13)}
    for r in rows:
        months[r["month"]][r["kind"]] = r["total"]
    table = [
        {"month": m, "name": MONTHS[m], "income": v["income"], "expense": v["expense"],
         "profit": v["income"] - v["expense"],
         "start": date(year, m, 1).isoformat(),
         "end": date(year, m, calendar.monthrange(year, m)[1]).isoformat()}
        for m, v in months.items()
    ]
    categories = db.query(
        "SELECT kind, category, SUM(amount) AS total FROM transactions "
        "WHERE strftime('%Y', date) = ? GROUP BY kind, category ORDER BY kind DESC, total DESC",
        (str(year),),
    )
    years = [r["y"] for r in db.query(
        "SELECT DISTINCT CAST(strftime('%Y', date) AS INTEGER) AS y FROM transactions ORDER BY y"
    )] or [year]
    if year not in years:
        years.append(year)
    return render_template(
        "finance/report.html", year=year, years=sorted(years), table=table,
        categories=categories,
        total_income=sum(r["income"] for r in table),
        total_expense=sum(r["expense"] for r in table),
    )


@bp.route("/new", methods=["GET", "POST"])
def create():
    kind = request.values.get("kind", "expense")
    if request.method == "POST":
        data = tx_form_data()
        problem = validate(data)
        if problem:
            flash(problem, "error")
            return render_form(data)
        db.execute(
            "INSERT INTO transactions (date, kind, category, amount, method, description, "
            "employee_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (data["date"], data["kind"], data["category"], data["amount"], data["method"],
             data["description"], data["employee_id"]),
        )
        db.commit()
        flash("Запись добавлена.", "success")
        return redirect(url_for("finance.index"))
    tx = {"kind": kind if kind in ("income", "expense") else "expense", "date": today_str(),
          "method": "cash"}
    return render_form(tx)


@bp.route("/<int:tx_id>/edit", methods=["GET", "POST"])
def edit(tx_id):
    tx = get_tx(tx_id)
    if request.method == "POST":
        data = tx_form_data()
        if tx["order_id"]:
            data["kind"] = "income"   # оплата заказ-наряда остаётся доходом
        problem = validate(data)
        if problem:
            flash(problem, "error")
            return render_form({**dict(tx), **data}, edit=True)
        db.execute(
            "UPDATE transactions SET date=?, kind=?, category=?, amount=?, method=?, "
            "description=?, employee_id=? WHERE id=?",
            (data["date"], data["kind"], data["category"], data["amount"], data["method"],
             data["description"], data["employee_id"], tx_id),
        )
        db.commit()
        flash("Запись сохранена.", "success")
        return redirect(url_for("finance.index"))
    return render_form(tx, edit=True)


@bp.route("/<int:tx_id>/delete", methods=["POST"])
def delete(tx_id):
    get_tx(tx_id)
    db.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    db.commit()
    flash("Запись удалена.", "success")
    return redirect(url_for("finance.index"))
