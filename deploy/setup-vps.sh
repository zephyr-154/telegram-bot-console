#!/usr/bin/env bash
set -eu

PROJECT_DIR=/opt/telegram-anonymous-board
SERVICE_NAME=telegram-anonymous-board.service
BACKUP_TIMER=telegram-anonymous-board-backup.timer

if [ "$(id -u)" -ne 0 ]; then
    printf '請以 root 執行此設定程式。\n' >&2
    exit 1
fi

if [ ! -d "$PROJECT_DIR" ]; then
    printf '找不到專案目錄：%s\n' "$PROJECT_DIR" >&2
    exit 1
fi

if ! id telegrambot >/dev/null 2>&1; then
    useradd --system --home-dir "$PROJECT_DIR" --shell /usr/sbin/nologin telegrambot
fi

install -d -m 0700 -o telegrambot -g telegrambot "$PROJECT_DIR/data" "$PROJECT_DIR/data/tmp" "$PROJECT_DIR/data/backups"
install -m 0644 "$PROJECT_DIR/deploy/telegram-anonymous-board.service" \
    "/etc/systemd/system/$SERVICE_NAME"
install -m 0644 "$PROJECT_DIR/deploy/telegram-anonymous-board-backup.service" \
    /etc/systemd/system/telegram-anonymous-board-backup.service
install -m 0644 "$PROJECT_DIR/deploy/telegram-anonymous-board-backup.timer" \
    "/etc/systemd/system/$BACKUP_TIMER"
chown -R telegrambot:telegrambot "$PROJECT_DIR/data"
find "$PROJECT_DIR/data" -type f -exec chmod 0600 {} +

if [ -f "$PROJECT_DIR/.env" ]; then
    printf '伺服器上已經有 .env，是否要重新輸入設定？[y/N] '
    read -r overwrite
    case "$overwrite" in
        y|Y|yes|YES)
            ;;
        *)
            systemctl daemon-reload
            systemctl enable "$SERVICE_NAME"
            systemctl restart "$SERVICE_NAME"
            systemctl enable --now "$BACKUP_TIMER"
            printf '已保留原有設定並重啟服務，且啟用每日備份。\n'
            exit 0
            ;;
    esac
fi

while :; do
    printf '請輸入 Telegram Bot Token（輸入時不會顯示）：'
    read -r -s BOT_TOKEN
    printf '\n'
    if [ -n "$BOT_TOKEN" ]; then
        break
    fi
    printf 'Bot Token 不可為空，請重新輸入。\n'
done

while :; do
    printf '請輸入目標頻道 ID（通常是 -100 開頭）：'
    read -r CHANNEL_ID
    case "$CHANNEL_ID" in
        -[0-9]*|[0-9]*)
            if [ "$CHANNEL_ID" != "0" ]; then
                break
            fi
            ;;
    esac
    printf '頻道 ID 格式不正確，請重新輸入。\n'
done

while :; do
    printf '請輸入你的 Telegram User ID：'
    read -r ADMIN_USER_ID
    case "$ADMIN_USER_ID" in
        [1-9][0-9]*)
            break
            ;;
    esac
    printf 'User ID 必須是正整數，請重新輸入。\n'
done

if command -v openssl >/dev/null 2>&1; then
    CONTENT_HASH_SECRET="$(openssl rand -hex 32)"
else
    CONTENT_HASH_SECRET="$("$PROJECT_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))')"
fi

umask 077
{
    printf 'BOT_TOKEN=%s\n' "$BOT_TOKEN"
    printf 'CHANNEL_ID=%s\n' "$CHANNEL_ID"
    printf 'ADMIN_USER_ID=%s\n' "$ADMIN_USER_ID"
    printf 'DATABASE_PATH=%s\n' "$PROJECT_DIR/data/bot.sqlite3"
    printf 'TEMP_DIR=%s\n' "$PROJECT_DIR/data/tmp"
    printf 'FONT_REGULAR_PATH=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc\n'
    printf 'FONT_BOLD_PATH=/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc\n'
    printf 'TELEGRAM_LOGO_PATH=\n'
    printf 'CONTENT_HASH_SECRET=%s\n' "$CONTENT_HASH_SECRET"
    printf 'TIMEZONE=Asia/Taipei\n'
} > "$PROJECT_DIR/.env"

unset BOT_TOKEN CHANNEL_ID ADMIN_USER_ID CONTENT_HASH_SECRET
chmod 600 "$PROJECT_DIR/.env"
chown telegrambot:telegrambot "$PROJECT_DIR/.env"
chown -R telegrambot:telegrambot "$PROJECT_DIR/app" "$PROJECT_DIR/assets" "$PROJECT_DIR/data"

if [ ! -f /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc ] || \
   [ ! -f /usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc ]; then
    printf '找不到 Noto 繁中字型，請先安裝 fonts-noto-cjk。\n' >&2
    exit 1
fi

"$PROJECT_DIR/.venv/bin/python" -m compileall -q "$PROJECT_DIR/app"
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
systemctl enable --now "$BACKUP_TIMER"

if systemctl is-active --quiet "$SERVICE_NAME"; then
    printf '\n設定完成，機器人服務已啟動。\n'
    printf '查看狀態：systemctl status %s --no-pager\n' "$SERVICE_NAME"
    printf '查看日誌：journalctl -u %s -f\n' "$SERVICE_NAME"
else
    printf '\n機器人服務啟動失敗，最近日誌如下：\n' >&2
    journalctl -u "$SERVICE_NAME" -n 50 --no-pager >&2 || true
    exit 1
fi
