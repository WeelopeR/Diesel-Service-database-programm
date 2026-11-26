"""Запуск программы «Дизель Сервис».

    python run.py                  — запуск на порту 8000 для всей локальной сети
    python run.py --port 8080      — другой порт
    python run.py --host 127.0.0.1 — только с этого компьютера
    python run.py --set-password   — задать новый пароль (если забыли старый)
    python run.py --backup ПАПКА   — сохранить копию базы в папку (хранятся последние --keep)
"""

import argparse
import getpass
import os
import socket
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from waitress import serve

from app import auth, create_app, default_data_dir


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def set_password(app):
    print("Новый пароль для входа в программу (символы при вводе не отображаются).")
    while True:
        password = getpass.getpass("Пароль: ")
        problem = auth.validate_password(password, getpass.getpass("Ещё раз: "))
        if not problem:
            break
        print(problem)
    with app.app_context():
        auth.set_password(password)
    print("Пароль сохранён. Все, кто был в программе, должны войти заново.")
    sys.exit(0)


def server_options(host, port):
    """Настройки веб-сервера Waitress."""
    options = {"host": host, "port": port, "threads": 8}
    if os.environ.get("DIESEL_PROXY") == "1":
        # На сервере программа стоит за Caddy на этом же компьютере. Адрес посетителя
        # и протокол (https) берутся из заголовков, но только от 127.0.0.1 — снаружи
        # их подделать нельзя. Без этого блокировка после неверных паролей видела бы
        # всех посетителей как одного.
        options.update(
            trusted_proxy="127.0.0.1",
            trusted_proxy_count=1,
            trusted_proxy_headers="x-forwarded-for x-forwarded-proto x-forwarded-host",
            clear_untrusted_proxy_headers=True,
        )
    return options


def backup(folder, keep):
    """Копия базы средствами SQLite — можно во время работы программы. Базу не
    открывает через приложение, поэтому копия делается до её обновления."""
    source = default_data_dir() / "diesel_service.db"
    if not source.exists():
        print(f"Базы ещё нет ({source}) — копировать нечего.")
        return
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"diesel_service_{datetime.now():%Y-%m-%d_%H-%M-%S}.db"
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    copies = sorted(folder.glob("diesel_service_*.db"))
    for old in copies[:-max(1, keep)]:
        old.unlink()
    print(f"Копия сохранена: {target} (всего копий: {min(len(copies), max(1, keep))})")


def main():
    parser = argparse.ArgumentParser(description="Дизель Сервис — программа учёта")
    parser.add_argument("--host", default="0.0.0.0", help="адрес (по умолчанию все сетевые)")
    parser.add_argument("--port", type=int, default=8000, help="порт (по умолчанию 8000)")
    parser.add_argument("--set-password", action="store_true",
                        help="задать новый пароль для входа и выйти")
    parser.add_argument("--backup", metavar="ПАПКА", help="сохранить копию базы в папку и выйти")
    parser.add_argument("--keep", type=int, default=30,
                        help="сколько последних копий хранить (вместе с --backup, по умолчанию 30)")
    args = parser.parse_args()

    if args.backup:
        backup(args.backup, args.keep)
        return
    app = create_app()
    if args.set_password:
        set_password(app)
        return
    print("«Дизель Сервис» запущен.")
    print(f"  На этом компьютере:     http://127.0.0.1:{args.port}")
    if args.host == "0.0.0.0":
        print(f"  С других устройств сети: http://{local_ip()}:{args.port}")
    print(f"  База данных: {app.config['DATABASE']}")
    print("Чтобы остановить — закройте окно или нажмите Ctrl+C.")
    if os.name == "nt":
        print("Забыли пароль? Закройте окно и запустите: start.bat --set-password")
    else:
        print("Забыли пароль? python run.py --set-password (на сервере: sudo diesel-password prod)")
    serve(app, **server_options(args.host, args.port))


if __name__ == "__main__":
    main()
