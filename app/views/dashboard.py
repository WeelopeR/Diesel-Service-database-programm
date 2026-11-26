"""Главная страница: сводка по работе сервиса."""

from pathlib import Path

from flask import Blueprint, current_app, render_template

from .. import db
from ..auth import LOCKOUT_SECONDS, MAX_FAILED_ATTEMPTS, MIN_PASSWORD_LENGTH
from ..utils import ACTIVE_STATUSES, PRICE_STEP, current_year, month_bounds, today_str
from .orders import DEBT_SQL, ORDER_SELECT

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    start, end = month_bounds()
    period = (start.isoformat(), end.isoformat())
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))

    status_counts = {r["status"]: r["n"] for r in db.query(
        f"SELECT status, COUNT(*) AS n FROM orders WHERE status IN ({placeholders}) "
        "GROUP BY status", ACTIVE_STATUSES)}
    active_orders = db.query(
        f"{ORDER_SELECT} WHERE o.status IN ({placeholders}) "
        "ORDER BY o.year DESC, o.number DESC LIMIT 15",
        ACTIVE_STATUSES,
    )
    overdue = db.scalar(
        f"SELECT COUNT(*) FROM orders WHERE status IN ({placeholders}) AND status <> 'done' "
        "AND planned_date IS NOT NULL AND planned_date < ?",
        (*ACTIVE_STATUSES, today_str()),
    )
    low_stock = db.query(
        "SELECT * FROM parts WHERE is_active = 1 AND min_quantity > 0 "
        "AND quantity <= min_quantity ORDER BY name LIMIT 10"
    )
    debts = db.query(f"{ORDER_SELECT} WHERE {DEBT_SQL} ORDER BY o.year DESC, o.number DESC")

    money = {r["kind"]: r["total"] for r in db.query(
        "SELECT kind, SUM(amount) AS total FROM transactions WHERE date BETWEEN ? AND ? "
        "GROUP BY kind", period)}
    income, expense = money.get("income", 0), money.get("expense", 0)
    completed_revenue = db.scalar(
        "SELECT COALESCE(SUM(ot.total), 0) FROM orders o "
        "JOIN order_totals ot ON ot.order_id = o.id "
        "WHERE o.status IN ('done', 'issued') AND date(o.completed_at) BETWEEN ? AND ?",
        period,
    )
    orders_this_year = db.scalar("SELECT COUNT(*) FROM orders WHERE year = ?", (current_year(),))

    return render_template(
        "dashboard.html", status_counts=status_counts, active_total=sum(status_counts.values()),
        active_orders=active_orders, overdue=overdue, today=today_str(), month_start=start,
        low_stock=low_stock, debts=debts, debts_total=sum(d["due"] for d in debts),
        income=income, expense=expense, profit=income - expense,
        completed_revenue=completed_revenue, orders_this_year=orders_this_year,
    )


@bp.route("/help")
def help_page():
    """Инструкция для руководителя. Доступна только после входа, поэтому здесь —
    всё, что не стоит держать в открытом README: пароль, доступ, где лежат данные."""
    return render_template(
        "help.html", db_path=Path(current_app.config["DATABASE"]),
        data_dir=Path(current_app.config["DATA_DIR"]), price_step=PRICE_STEP,
        min_password=MIN_PASSWORD_LENGTH, max_attempts=MAX_FAILED_ATTEMPTS,
        lockout_minutes=LOCKOUT_SECONDS // 60,
    )
