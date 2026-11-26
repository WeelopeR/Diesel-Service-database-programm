"""Вход в программу по паролю руководителя и защита форм.

Программой пользуется один человек — руководитель, поэтому логина нет:
пароль (в виде хэша) хранится в таблице settings под ключом admin_password_hash."""

import hashlib
import hmac
import secrets
import time
from urllib.parse import urlparse

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db

bp = Blueprint("auth", __name__)

PASSWORD_KEY = "admin_password_hash"
MIN_PASSWORD_LENGTH = 8
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 5 * 60
_failed_logins = {}   # IP -> (число попыток, время первой попытки)

PUBLIC_ENDPOINTS = {"auth.login", "auth.setup", "static"}


def validate_password(password, password2=None):
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Пароль должен быть не короче {MIN_PASSWORD_LENGTH} символов."
    if password2 is not None and password != password2:
        return "Пароли не совпадают."
    return None


def get_password_hash():
    return db.scalar("SELECT value FROM settings WHERE key = ?", (PASSWORD_KEY,))


def set_password(password):
    """Сохраняет новый пароль. Все открытые сессии после этого разлогиниваются."""
    db.set_setting(PASSWORD_KEY, generate_password_hash(password))
    db.commit()


def password_is_set():
    return bool(get_password_hash())


def _fingerprint(password_hash):
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


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


def is_local_request():
    """Запрос с того же компьютера, где запущена программа."""
    return (request.remote_addr or "") in ("127.0.0.1", "::1")


def _start_session(password_hash):
    session.clear()
    session.permanent = True
    session["auth"] = _fingerprint(password_hash)


def before_request():
    if request.method == "POST":
        sent = request.form.get("_csrf", "")
        if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "Форма устарела. Обновите страницу и попробуйте ещё раз.")

    password_hash = get_password_hash()
    # Смена пароля делает недействительными все старые сессии.
    g.logged_in = bool(password_hash and hmac.compare_digest(
        session.get("auth", ""), _fingerprint(password_hash)))

    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not password_hash:
        return redirect(url_for("auth.setup"))
    if not g.logged_in:
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
    return None


@bp.route("/login", methods=["GET", "POST"])
def login():
    if not password_is_set():
        return redirect(url_for("auth.setup"))
    if g.logged_in:
        return redirect(url_for("dashboard.index"))

    error = None
    if request.method == "POST":
        key = request.remote_addr or "?"
        if _is_locked(key):
            error = "Слишком много неудачных попыток. Подождите 5 минут."
        else:
            password_hash = get_password_hash()
            if check_password_hash(password_hash, request.form.get("password", "")):
                _failed_logins.pop(key, None)
                _start_session(password_hash)
                return redirect(safe_next(request.args.get("next")) or url_for("dashboard.index"))
            _register_failure(key)
            error = "Неверный пароль."
    return render_template("auth/login.html", error=error)


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """Первый запуск: руководитель придумывает пароль для входа."""
    if password_is_set():
        return redirect(url_for("auth.login"))
    # Первый пароль задаётся только с компьютера, где запущена программа: иначе на
    # сервере в интернете его успел бы задать посторонний.
    if not is_local_request():
        if request.method == "POST":
            abort(403)
        return render_template("auth/setup.html", remote=True)
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        error = validate_password(password, request.form.get("password2", ""))
        if not error:
            set_password(password)
            _start_session(get_password_hash())
            flash("Пароль сохранён. Добро пожаловать!", "success")
            return redirect(url_for("dashboard.index"))
    return render_template("auth/setup.html", error=error)


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/password", methods=["GET", "POST"])
def change_password():
    error = None
    if request.method == "POST":
        if not check_password_hash(get_password_hash(), request.form.get("current", "")):
            error = "Текущий пароль указан неверно."
        else:
            password = request.form.get("password", "")
            error = validate_password(password, request.form.get("password2", ""))
        if not error:
            set_password(password)
            _start_session(get_password_hash())
            flash("Пароль изменён. На других устройствах нужно будет войти заново.", "success")
            return redirect(url_for("dashboard.index"))
    return render_template("auth/password.html", error=error)
