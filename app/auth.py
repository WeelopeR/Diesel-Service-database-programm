"""Вход в программу, роли и защита форм."""

import functools
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlparse

from flask import (
    Blueprint, abort, flash, g, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from . import db
from .utils import form_str

bp = Blueprint("auth", __name__)

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 5 * 60
_failed_logins = {}   # (ip, логин) -> (число попыток, время первой попытки)

PUBLIC_ENDPOINTS = {"auth.login", "auth.setup", "static"}


def hash_password(password):
    return generate_password_hash(password)


def validate_password(password):
    if len(password) < 6:
        return "Пароль должен быть не короче 6 символов."
    return None


def has_role(*roles):
    user = g.get("user")
    return bool(user) and user["role"] in roles


def role_required(*roles):
    def decorator(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            if not has_role(*roles):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def csrf_token():
    token = session.get("_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf"] = token
    return token


def safe_next(target):
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _client_key(username):
    return (request.remote_addr or "?", username.lower())


def _is_locked(key):
    entry = _failed_logins.get(key)
    if not entry:
        return False
    count, first = entry
    if time.time() - first > LOCKOUT_SECONDS:
        _failed_logins.pop(key, None)
        return False
    return count >= MAX_FAILED_ATTEMPTS


def _register_failure(key):
    count, first = _failed_logins.get(key, (0, time.time()))
    if time.time() - first > LOCKOUT_SECONDS:
        count, first = 0, time.time()
    _failed_logins[key] = (count + 1, first)


def _password_fingerprint(password_hash):
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def load_user():
    g.user = None
    user_id = session.get("user_id")
    if user_id is None:
        return
    user = db.query(
        "SELECT * FROM employees WHERE id = ? AND is_active = 1 AND password_hash IS NOT NULL",
        (user_id,), one=True,
    )
    # Смена пароля сбрасывает все старые сессии.
    if user and hmac.compare_digest(
        session.get("pw", ""), _password_fingerprint(user["password_hash"])
    ):
        g.user = user
    else:
        session.clear()


def users_exist():
    return bool(db.scalar(
        "SELECT COUNT(*) FROM employees WHERE password_hash IS NOT NULL AND role = 'admin'"
    ))


def before_request():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "Форма устарела. Обновите страницу и попробуйте ещё раз.")

    load_user()
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not users_exist():
        return redirect(url_for("auth.setup"))
    if g.user is None:
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
    return None


def _start_session(user):
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["pw"] = _password_fingerprint(user["password_hash"])


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not users_exist():
        return redirect(url_for("auth.setup"))
    if g.user:
        return redirect(url_for("dashboard.index"))

    error = None
    username = ""
    if request.method == "POST":
        username = form_str("username")
        password = request.form.get("password", "")
        key = _client_key(username)
        if _is_locked(key):
            error = "Слишком много неудачных попыток. Подождите 5 минут."
        else:
            user = db.query(
                "SELECT * FROM employees WHERE username = ? AND is_active = 1 "
                "AND password_hash IS NOT NULL",
                (username,), one=True,
            )
            if user and check_password_hash(user["password_hash"], password):
                _failed_logins.pop(key, None)
                _start_session(user)
                return redirect(safe_next(request.args.get("next")) or url_for("dashboard.index"))
            _register_failure(key)
            error = "Неверный логин или пароль."
    return render_template("auth/login.html", error=error, username=username)


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Первый запуск: создание учётной записи руководителя."""
    if users_exist():
        return redirect(url_for("auth.login"))

    errors = []
    if request.method == "POST":
        full_name = form_str("full_name")
        username = form_str("username")
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")
        company = form_str("company_name")
        if not full_name:
            errors.append("Укажите ФИО.")
        if not username:
            errors.append("Укажите логин.")
        problem = validate_password(password)
        if problem:
            errors.append(problem)
        if password != password2:
            errors.append("Пароли не совпадают.")
        if not errors:
            user_id = db.execute(
                "INSERT INTO employees (full_name, position, role, username, password_hash, is_master) "
                "VALUES (?, 'Руководитель', 'admin', ?, ?, 0)",
                (full_name, username, hash_password(password)),
            )
            if company:
                db.set_setting("company_name", company)
            db.commit()
            user = db.query("SELECT * FROM employees WHERE id = ?", (user_id,), one=True)
            _start_session(user)
            flash("Учётная запись руководителя создана. Добро пожаловать!", "success")
            return redirect(url_for("dashboard.index"))
    return render_template("auth/setup.html", errors=errors)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/profile/password", methods=["GET", "POST"])
def change_password():
    errors = []
    if request.method == "POST":
        current = request.form.get("current", "")
        new = request.form.get("password", "")
        new2 = request.form.get("password2", "")
        if not check_password_hash(g.user["password_hash"], current):
            errors.append("Текущий пароль указан неверно.")
        problem = validate_password(new)
        if problem:
            errors.append(problem)
        if new != new2:
            errors.append("Пароли не совпадают.")
        if not errors:
            db.execute(
                "UPDATE employees SET password_hash = ? WHERE id = ?",
                (hash_password(new), g.user["id"]),
            )
            db.commit()
            user = db.query("SELECT * FROM employees WHERE id = ?", (g.user["id"],), one=True)
            _start_session(user)
            flash("Пароль изменён.", "success")
            return redirect(url_for("dashboard.index"))
    return render_template("auth/password.html", errors=errors)
