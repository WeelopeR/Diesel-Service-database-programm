#!/usr/bin/env bash
# Задать новый пароль для копии «Дизель Сервис» на сервере:
#   sudo diesel-password prod      (или test)
set -euo pipefail

NAME="${1:?Укажите копию: prod или test}"
ENV_FILE="/etc/diesel/$NAME.env"
[ -f "$ENV_FILE" ] || { echo "Нет копии «$NAME» (файл $ENV_FILE не найден)" >&2; exit 1; }
# shellcheck source=/dev/null
source "$ENV_FILE"

runuser -u diesel -- env DIESEL_DATA_DIR="$DIESEL_DATA_DIR" \
  "$APP_DIR/.venv/bin/python" "$APP_DIR/run.py" --set-password
