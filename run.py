"""Запуск программы учёта дизельного сервиса.

    python run.py                 — запуск на порту 8000 для всей локальной сети
    python run.py --port 8080     — другой порт
    python run.py --host 127.0.0.1 — только с этого компьютера
"""

import argparse
import socket

from waitress import serve

from app import create_app


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="Дизельный сервис — программа учёта")
    parser.add_argument("--host", default="0.0.0.0", help="адрес (по умолчанию все сетевые)")
    parser.add_argument("--port", type=int, default=8000, help="порт (по умолчанию 8000)")
    args = parser.parse_args()

    app = create_app()
    print("Программа учёта запущена.")
    print(f"  На этом компьютере:     http://127.0.0.1:{args.port}")
    if args.host == "0.0.0.0":
        print(f"  С других устройств сети: http://{local_ip()}:{args.port}")
    print(f"  База данных: {app.config['DATABASE']}")
    print("Чтобы остановить — закройте окно или нажмите Ctrl+C.")
    serve(app, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
