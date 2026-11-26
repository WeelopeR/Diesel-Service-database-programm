#!/usr/bin/env bash
# Автообновление копии «Дизель Сервис» из GitHub. Запускается таймером
# diesel-update@<копия>.timer каждые 2 минуты; вручную: sudo diesel-update prod
#
# Если в ветке появились новые изменения: копия базы → новая версия кода →
# компоненты Python → перезапуск программы (база обновляется при запуске сама).
set -euo pipefail
export GIT_TERMINAL_PROMPT=0   # не ждать ввода логина GitHub, если доступа нет

NAME="${1:?Укажите копию: prod или test}"
ENV_FILE="/etc/diesel/$NAME.env"
[ -f "$ENV_FILE" ] || { echo "Нет файла настроек $ENV_FILE" >&2; exit 1; }
# shellcheck source=/dev/null
source "$ENV_FILE"

as_user() { runuser -u diesel -- "$@"; }
cd "$APP_DIR"

# Ветки может ещё не быть (например, test) — это не ошибка, просто ждём.
set +e
as_user git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1
status=$?
set -e
if [ "$status" -eq 2 ]; then
  exit 0
elif [ "$status" -ne 0 ]; then
  echo "Не удалось связаться с GitHub (код $status) — попробую в следующий раз." >&2
  exit 0
fi

as_user git fetch --quiet origin "$BRANCH"
current="$(as_user git rev-parse HEAD)"
latest="$(as_user git rev-parse FETCH_HEAD)"
[ "$current" = "$latest" ] && exit 0

echo "Обновление «$NAME»: ${current:0:7} → ${latest:0:7}"
as_user env DIESEL_DATA_DIR="$DIESEL_DATA_DIR" .venv/bin/python run.py \
  --backup "$BACKUP_DIR/before-update" --keep 10
as_user git checkout --quiet -B "$BRANCH" FETCH_HEAD
as_user git reset --quiet --hard FETCH_HEAD
as_user .venv/bin/pip install --quiet -r requirements.txt
systemctl restart "diesel@$NAME"
echo "Готово: $(as_user git log -1 --format='%h %s')"
