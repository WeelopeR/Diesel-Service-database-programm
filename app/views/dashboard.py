"""Главная страница: сводка по работе сервиса."""

from flask import Blueprint, g, render_template

from .. import db
from ..utils import ACTIVE_STATUSES, month_bounds, today_str
from .orders import TOTALS_SQL
from .salary import employee_summary

bp = Blueprint("dashboard", __name__)


@bp.route("/")
def index():
    start, end = month_bounds()
    period = (start.isoformat(), end.isoformat())
    placeholders = ",".join("?" * len(ACTIVE_STATUSES))

    status_counts = {r["status"]: r["n"] for r in db.query(
        f"SELECT status, COUNT(*) AS n FROM orders WHERE status IN ({placeholders}) "
        "GROUP BY status", ACTIVE_STATUSES)}

    active_sql = f"""
        SELECT o.*, c.name AS client_name, c.phone AS client_phone,
               v.make, v.model, v.plate, r.full_name AS responsible_name, {TOTALS_SQL}
        FROM orders o
        JOIN clients c ON c.id = o.client_id
        LEFT JOIN vehicles v ON v.id = o.vehicle_id
        LEFT JOIN employees r ON r.id = o.responsible_id
        WHERE o.status IN ({placeholders})
    """
    params = list(ACTIVE_STATUSES)
    if g.user["role"] == "master":
        active_sql += """ AND (o.responsible_id = ? OR o.id IN
                          (SELECT order_id FROM order_services WHERE master_id = ?))"""
        params += [g.user["id"], g.user["id"]]
    active_orders = db.query(active_sql + " ORDER BY o.id DESC LIMIT 15", params)

    overdue = db.scalar(
        f"SELECT COUNT(*) FROM orders WHERE status IN ({placeholders}) AND status <> 'done' "
        "AND planned_date IS NOT NULL AND planned_date < ?",
        (*ACTIVE_STATUSES, today_str()),
    )

    context = {
        "status_counts": status_counts,
        "active_total": sum(status_counts.values()),
        "active_orders": active_orders,
        "overdue": overdue,
        "today": today_str(),
        "month_start": start,
        "month_end": end,
    }

    if g.user["role"] in ("admin", "manager"):
        context["low_stock"] = db.query(
            "SELECT * FROM parts WHERE is_active = 1 AND min_quantity > 0 "
            "AND quantity <= min_quantity ORDER BY name LIMIT 10"
        )
        debts = db.query(
            f"SELECT * FROM (SELECT o.id, c.name AS client_name, {TOTALS_SQL} FROM orders o "
            "JOIN clients c ON c.id = o.client_id WHERE o.status = 'issued') "
            "WHERE total - paid > 0.009 ORDER BY id DESC"
        )
        context["debts"] = debts
        context["debts_total"] = sum(d["total"] - d["paid"] for d in debts)
        context["orders_this_month"] = db.scalar(
            "SELECT COUNT(*) FROM orders WHERE date(created_at) BETWEEN ? AND ?", period)

    if g.user["role"] == "admin":
        money = db.query(
            "SELECT kind, SUM(amount) AS total FROM transactions WHERE date BETWEEN ? AND ? "
            "GROUP BY kind", period)
        totals = {r["kind"]: r["total"] for r in money}
        context["income"] = totals.get("income", 0)
        context["expense"] = totals.get("expense", 0)
        context["profit"] = context["income"] - context["expense"]
        context["completed_revenue"] = db.scalar(
            f"SELECT COALESCE(SUM(total), 0) FROM (SELECT {TOTALS_SQL} FROM orders o "
            "WHERE o.status IN ('done', 'issued') AND date(o.completed_at) BETWEEN ? AND ?)",
            period,
        )

    if g.user["is_master"]:
        context["my_salary"] = employee_summary(g.user["id"], start, end)

    return render_template("dashboard.html", **context)
