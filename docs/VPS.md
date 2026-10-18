# VPS 多機器人部署

需要手動背景執行時，直接使用 [README 的 Linux 安裝方式](../README.md#linux--ubuntu--debian)與 `tgbot start`。本文件則提供 **Ubuntu 24.04 + systemd 開機啟動**：使用獨立服務帳號、固定資料目錄與專案內 Python 環境。

以下適用首次安裝，部署目錄尚不存在的主機。既有部署請看下方更新說明。Bot 使用 long polling，需能對外連線 Telegram HTTPS，不需要網域或額外開放 Web 連接埠。

## 1. 安裝程式與繁中字型

```bash
sudo apt update
sudo apt install -y git python3.12 python3.12-venv fonts-noto-cjk ca-certificates
sudo git clone https://github.com/zephyr-154/telegram-bot-console.git /opt/telegram-anonymous-board
sudo python3.12 -m venv /opt/telegram-anonymous-board/.venv
sudo /opt/telegram-anonymous-board/.venv/bin/python -m pip install /opt/telegram-anonymous-board
```

Ubuntu 22.04 預設沒有 Python 3.12，不能直接套用上述 Python 套件指令；可先使用 README 的 uv 安裝方式，或自行準備符合版本的服務用 Python。systemd 範例的 Python 必須位於服務帳號可讀取、且 `ProtectHome=true` 不會封鎖的位置。

## 2. 建立服務帳號與資料目錄

```bash
sudo useradd --system --home-dir /opt/telegram-anonymous-board --shell /usr/sbin/nologin telegrambot
sudo install -d -m 0700 -o telegrambot -g telegrambot /opt/telegram-anonymous-board/data
```

如果已經有 `telegrambot` 帳號，略過 `useradd`。程式與 Python 環境由 root 管理，機器人以 `telegrambot` 寫入 `data/`。

## 3. 開啟面板並新增機器人

在目前 bash 終端定義操作捷徑：

```bash
alias tgbot-vps='sudo -u telegrambot env TGBOT_HOME=/opt/telegram-anonymous-board/data/manager /opt/telegram-anonymous-board/.venv/bin/tgbot'
tgbot-vps config
```

這個 alias 僅供目前終端使用。新增各台並填入 Token、群組／頻道 ID、管理員 ID，按 Ctrl+S 儲存。Ubuntu 安裝的 Noto CJK 字型會自動找到。

之後也可以直接使用完整指令，不必依賴 alias：

```bash
sudo -u telegrambot env TGBOT_HOME=/opt/telegram-anonymous-board/data/manager /opt/telegram-anonymous-board/.venv/bin/tgbot status
```

所有面板、備份與手動啟停都必須使用相同服務帳號及 `TGBOT_HOME`。自己的使用者帳號執行另一份 `tgbot`，預設會看到另一個管理目錄。

## 4. 安裝開機服務

先完成每台設定，再執行：

```bash
tgbot-vps check
sudo cp /opt/telegram-anonymous-board/deploy/tgbot-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tgbot-manager.service
tgbot-vps status
```

登入面板仍可查看與管理全部機器人。關閉 SSH 後會繼續執行，VPS 重開機後 systemd 會啟動全部已設定的機器人。

```bash
sudo systemctl status tgbot-manager.service --no-pager
sudo journalctl -u tgbot-manager.service -n 50 --no-pager
tgbot-vps logs school
```

`school` 請換成實際名稱。這個 unit 是 `oneshot`，`active (exited)` 表示啟動指令已完成，不代表每台 Bot 都成功連線；請以 `tgbot-vps status` 與各台日誌確認。個別設定錯誤不會中止其他正常機器人的啟動。

## 5. 日常管理

```bash
tgbot-vps add school
tgbot-vps config school
tgbot-vps start school
tgbot-vps restart school
tgbot-vps backup school
tgbot-vps status
```

整體停止與啟動：

```bash
sudo systemctl stop tgbot-manager.service
sudo systemctl start tgbot-manager.service
```

不要同時使用舊版 `telegram-anonymous-board.service` 啟動相同 Token。此 unit 會管理整組程序，但不會自動重啟個別崩潰的機器人；發生錯誤時，先查看日誌再重啟。可容納數量取決於實際記憶體、CPU、製圖量及 Telegram 限流，面板可用來觀察用量。

## 6. 更新與備份

先停止服務，為每台建立備份，並另外安全保存 `.env` 與 `.env.bak`：

```bash
sudo systemctl stop tgbot-manager.service
tgbot-vps backup school
tgbot-vps backup work
sudo git -C /opt/telegram-anonymous-board pull --ff-only
sudo /opt/telegram-anonymous-board/.venv/bin/python -m pip install --upgrade /opt/telegram-anonymous-board
tgbot-vps check
sudo systemctl start tgbot-manager.service
tgbot-vps status
```

名稱請替換為自己的機器人。若更新包含 unit 調整，先重新複製 `deploy/tgbot-manager.service` 並執行 `sudo systemctl daemon-reload`，再啟動服務。不要覆寫 `data/`、重建 `.env` 或變更既有 `CONTENT_HASH_SECRET`。

多機器人備份目前由 `backup 名稱` 產生，存於各台的 `data/backups/` 並保留最近 7 份；舊版單機器人備份 timer 不會自動備份這些資料。若加入排程，請使用相同服務帳號、管理目錄及完整指令路徑。

## 7. 移除服務

若只想停止開機啟動並保留資料：

```bash
sudo systemctl disable --now tgbot-manager.service
```

需要移除 unit 時，在確認停止後執行：

```bash
sudo rm /etc/systemd/system/tgbot-manager.service
sudo systemctl daemon-reload
```

此方式屬於原始碼與 venv 部署；`tgbot app delete` 適用 uv 工具安裝。部署目錄請在備份確認後自行清理。刪除單台仍可使用 `tgbot-vps delete 名稱`，需輸入名稱確認。
