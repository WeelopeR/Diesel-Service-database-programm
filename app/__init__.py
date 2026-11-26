"""«Дизель Сервис» — программа учёта: клиенты, услуги, склад, заказ-наряды,
финансы и зарплата мастеров."""

import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, g, render_template

from . import auth, db, utils

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_secret_key(data_dir):
    key_file = data_dir / "secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.write_text(key)
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


def create_app(test_config=None):
    app = Flask(__name__)

    data_dir = Path(os.environ.get("DIESEL_DATA_DIR", BASE_DIR / "data"))
    if test_config and "DATA_DIR" in test_config:
        data_dir = Path(test_config["DATA_DIR"])
    data_dir.mkdir(parents=True, exist_ok=True)

    app.config.update(
        DATA_DIR=str(data_dir),
        DATABASE=str(data_dir / "diesel_service.db"),
        SECRET_KEY=_load_secret_key(data_dir),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("DIESEL_HTTPS") == "1",
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    db.init_db(app.config["DATABASE"])
    app.teardown_appcontext(db.close_db)
    app.before_request(auth.before_request)

    from .views import (
        clients, dashboard, employees, finance, orders, salary, services, settings, warehouse,
    )

    app.register_blueprint(auth.bp)
    for module in (dashboard, clients, services, warehouse, orders, finance, salary,
                   employees, settings):
        app.register_blueprint(module.bp)

    app.jinja_env.filters.update(
        money=utils.fmt_money,
        qty=utils.fmt_qty,
        num=utils.fmt_num,
        date=utils.fmt_date,
        datetime=utils.fmt_datetime,
        order_no=utils.fmt_order_no,
        vehicle=utils.vehicle_title,
    )

    @app.context_processor
    def inject_globals():
        return {
            "csrf_token": auth.csrf_token,
            "logged_in": g.get("logged_in", False),
            "ORDER_STATUSES": utils.ORDER_STATUSES,
            "PAYMENT_METHODS": utils.PAYMENT_METHODS,
            "PAYMENT_FILTERS": utils.PAYMENT_FILTERS,
            "STOCK_KINDS": utils.STOCK_KINDS,
            "CLIENT_KINDS": utils.CLIENT_KINDS,
            "company_name": _company_name(),
            "today_iso": utils.today_str(),
            "current_year": utils.current_year(),
        }

    def _company_name():
        try:
            return db.scalar("SELECT value FROM settings WHERE key = 'company_name'") or ""
        except Exception:  # noqa: BLE001 — шапка не должна ронять страницу
            return ""

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template("error.html", code=403,
                               message="У вас нет доступа к этому разделу."), 403

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404, message="Страница не найдена."), 404

    @app.errorhandler(400)
    def bad_request(e):
        return render_template("error.html", code=400,
                               message=e.description or "Некорректный запрос."), 400

    return app
