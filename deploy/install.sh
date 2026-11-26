#!/usr/bin/env bash
# Установка «Дизель Сервис» на сервер Ubuntu 24.04 (или 22.04):
#   - основная копия (ветка main) и, по желанию, тестовая (ветка test);
#   - HTTPS через Caddy (сертификаты выпускаются и продлеваются сами);
#   - автообновление из GitHub каждые 2 минуты (перед обновлением — копия базы);
#   - ежедневные резервные копии базы (хранятся последние 30).
#
# Запуск (из папки со скачанной программой):
#   sudo bash deploy/install.sh --domain ваш-домен.ru --test-domain test.ваш-домен.ru \
#        --key /root/diesel_deploy_key
#
# Параметры:
#   --domain        адрес основной копии (обязательно)
#   --test-domain   адрес тестовой копии (если не указать — ставится только основная)
#   --key           ключ доступа к закрытому репозиторию GitHub (deploy key)
#   --test-branch   ветка для тестовой копии (по умолчанию test)
#
# Скрипт можно запускать повторно: уже сделанное он не ломает.
set -euo pipefail
export GIT_TERMINAL_PROMPT=0   # не ждать ввода логина GitHub, если доступа нет

DOMAIN=""
TEST_DOMAIN=""
KEY=""
TEST_BRANCH="test"

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --test-domain) TEST_DOMAIN="${2:-}"; shift 2 ;;
    --key) KEY="${2:-}"; shift 2 ;;
    --test-branch) TEST_BRANCH="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Неизвестный параметр: $1 (подсказка: --help)" >&2; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo: sudo bash deploy/install.sh ..." >&2
  exit 1
fi
if [ -z "$DOMAIN" ]; then
  echo "Укажите адрес основной копии: --domain ваш-домен.ru" >&2
  exit 1
fi
if [ -n "$KEY" ] && [ ! -f "$KEY" ]; then
  echo "Файл ключа не найден: $KEY" >&2
  exit 1
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_URL="$(git -C "$SRC_DIR" remote get-url origin)"
SERVICE_USER="diesel"
HOME_DIR="/var/lib/diesel"

step() { printf '\n==> %s\n' "$*"; }
as_user() { runuser -u "$SERVICE_USER" -- "$@"; }

step "Устанавливаю системные пакеты (git, Python, Caddy)"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv caddy >/dev/null

step "Создаю системного пользователя $SERVICE_USER"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$HOME_DIR"

if [ -n "$KEY" ]; then
  step "Настраиваю доступ к GitHub по ключу"
  install -d -m 700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$HOME_DIR/.ssh"
  install -m 600 -o "$SERVICE_USER" -g "$SERVICE_USER" "$KEY" "$HOME_DIR/.ssh/id_ed25519"
  cat > "$HOME_DIR/.ssh/config" <<'EOF'
Host github.com
  IdentityFile ~/.ssh/id_ed25519
  StrictHostKeyChecking accept-new
EOF
  chown "$SERVICE_USER:$SERVICE_USER" "$HOME_DIR/.ssh/config"
  chmod 600 "$HOME_DIR/.ssh/config"
fi

install -d -m 755 /opt/diesel
install -d -m 755 /etc/diesel

has_password() {
  # Пароль хранится в базе копии; база появляется при первом запуске.
  local db="$1/diesel_service.db"
  [ -f "$db" ] && python3 - "$db" <<'EOF'
import sqlite3, sys
row = sqlite3.connect(sys.argv[1]).execute(
    "SELECT value FROM settings WHERE key = 'admin_password_hash'").fetchone()
sys.exit(0 if row and row[0] else 1)
EOF
}

setup_instance() {
  local name="$1" branch="$2" port="$3" domain="$4"
  local app="/opt/diesel/$name" data="$HOME_DIR/$name" backups="/var/backups/diesel/$name"

  step "Копия «$name»: $domain (ветка $branch)"
  install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$data"
  install -d -m 750 -o "$SERVICE_USER" -g "$SERVICE_USER" /var/backups/diesel "$backups"

  if [ ! -d "$app/.git" ]; then
    local checkout="$branch"
    if ! as_user git ls-remote --exit-code --heads "$REPO_URL" "$branch" >/dev/null 2>&1; then
      echo "Ветки «$branch» на GitHub пока нет — ставлю версию из main."
      echo "Как только ветка появится, копия обновится на неё сама."
      checkout="main"
    fi
    install -d -m 755 -o "$SERVICE_USER" -g "$SERVICE_USER" "$app"
    as_user git clone --quiet --branch "$checkout" "$REPO_URL" "$app"
  fi

  echo "Устанавливаю компоненты Python…"
  [ -x "$app/.venv/bin/python" ] || as_user python3 -m venv "$app/.venv"
  as_user "$app/.venv/bin/pip" install --quiet --upgrade pip
  as_user "$app/.venv/bin/pip" install --quiet -r "$app/requirements.txt"

  cat > "/etc/diesel/$name.env" <<EOF
# Настройки копии «$name» программы «Дизель Сервис»
APP_DIR=$app
BRANCH=$branch
PORT=$port
DIESEL_DATA_DIR=$data
BACKUP_DIR=$backups
DIESEL_HTTPS=1
DIESEL_PROXY=1
EOF
  chmod 644 "/etc/diesel/$name.env"

  if has_password "$data"; then
    echo "Пароль для входа уже задан."
  elif [ -t 0 ]; then
    echo
    echo "Задайте пароль для входа в копию «$name» ($domain)."
    as_user env DIESEL_DATA_DIR="$data" "$app/.venv/bin/python" "$app/run.py" --set-password
  else
    echo "ВНИМАНИЕ: пароль для «$name» не задан. Задайте его командой:"
    echo "  sudo diesel-password $name"
  fi
}

setup_instance prod main 8001 "$DOMAIN"
if [ -n "$TEST_DOMAIN" ]; then
  setup_instance test "$TEST_BRANCH" 8002 "$TEST_DOMAIN"
fi

step "Устанавливаю службы, автообновление и резервные копии"
install -m 755 "$SRC_DIR/deploy/update.sh" /usr/local/sbin/diesel-update
install -m 755 "$SRC_DIR/deploy/password.sh" /usr/local/sbin/diesel-password
install -m 644 "$SRC_DIR"/deploy/systemd/*.service "$SRC_DIR"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload

instances="prod"
[ -n "$TEST_DOMAIN" ] && instances="prod test"
for name in $instances; do
  systemctl enable --quiet "diesel@$name" "diesel-update@$name.timer" "diesel-backup@$name.timer"
  systemctl restart "diesel@$name"
  systemctl start "diesel-update@$name.timer" "diesel-backup@$name.timer"
done

step "Настраиваю HTTPS (Caddy)"
{
  echo "# Создано установщиком «Дизель Сервис». Сертификаты HTTPS Caddy получает сам."
  echo "$DOMAIN {"
  echo "	encode gzip"
  echo "	reverse_proxy 127.0.0.1:8001"
  echo "}"
  if [ -n "$TEST_DOMAIN" ]; then
    echo "$TEST_DOMAIN {"
    echo "	encode gzip"
    echo "	reverse_proxy 127.0.0.1:8002"
    echo "}"
  fi
} > /etc/caddy/Caddyfile
if ! check="$(caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile 2>&1)"; then
  echo "$check" >&2
  echo "Ошибка в настройке Caddy (/etc/caddy/Caddyfile) — проверьте адреса сайтов." >&2
  exit 1
fi
systemctl enable --quiet caddy
systemctl reload caddy 2>/dev/null || systemctl restart caddy

step "Готово"
echo "Основная копия:  https://$DOMAIN"
[ -n "$TEST_DOMAIN" ] && echo "Тестовая копия:  https://$TEST_DOMAIN"
echo
echo "Первое открытие может занять до минуты — Caddy получает сертификат HTTPS."
echo "Состояние:        sudo systemctl status diesel@prod"
echo "Журнал:           sudo journalctl -u diesel@prod -u diesel-update@prod -n 50"
echo "Резервные копии:  /var/backups/diesel/"
