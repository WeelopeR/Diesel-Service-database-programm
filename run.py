"""Запуск программы «Дизель Сервис».

    python run.py                  — запуск на порту 8000 для всей локальной сети
    python run.py --port 8080      — другой порт
    python run.py --host 127.0.0.1 — только с этого компьютера
    python run.py --set-password   — задать новый пароль (если забыли старый)
"""

import argparse
import getpass
import socket
import sys

from waitress import serve

from app import auth, create_app


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


def main():
    parser = argparse.ArgumentParser(description="Дизель Сервис — программа учёта")
    parser.add_argument("--host", default="0.0.0.0", help="адрес (по умолчанию все сетевые)")
    parser.add_argument("--port", type=int, default=8000, help="порт (по умолчанию 8000)")
    parser.add_argument("--set-password", action="store_true",
                        help="задать новый пароль для входа и выйти")
    args = parser.parse_args()

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
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
