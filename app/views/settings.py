"""Реквизиты сервиса для печатных форм и резервное копирование."""

import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from flask import Blueprint, after_this_request, current_app, flash, redirect, render_template, \
    request, send_file, url_for

from .. import db

bp = Blueprint("settings", __name__, url_prefix="/settings")

FIELDS = ("company_name", "company_address", "company_phone", "company_requisites",
          "order_footer")


@bp.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        for key in FIELDS:
            db.set_setting(key, request.form.get(key, "").strip())
        db.commit()
        flash("Настройки сохранены.", "success")
        return redirect(url_for("settings.index"))
    stats = {
        "clients": db.scalar("SELECT COUNT(*) FROM clients"),
        "orders": db.scalar("SELECT COUNT(*) FROM orders"),
        "parts": db.scalar("SELECT COUNT(*) FROM parts"),
        "transactions": db.scalar("SELECT COUNT(*) FROM transactions"),
    }
    db_path = Path(current_app.config["DATABASE"])
    return render_template("settings/index.html", settings=db.get_settings(), stats=stats,
                           db_path=db_path, db_size=db_path.stat().st_size)


@bp.route("/backup")
def backup():
    """Скачать полную копию базы. Копия делается штатным механизмом SQLite,
    поэтому её можно снимать прямо во время работы программы."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    target = sqlite3.connect(tmp.name)
    try:
        db.get_db().backup(target)
    finally:
        target.close()

    @after_this_request
    def _cleanup(response):
        response.call_on_close(lambda: Path(tmp.name).unlink(missing_ok=True))
        return response

    name = f"diesel_service_backup_{datetime.now():%Y-%m-%d_%H-%M}.db"
    return send_file(tmp.name, as_attachment=True, download_name=name,
                     mimetype="application/octet-stream")
