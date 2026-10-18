# 舊版單機器人部署與還原

本文件保留既有單機器人 `.env` 與 systemd 的操作方式。新安裝請先閱讀 [README](../README.md) 與 [多機器人 VPS 部署](VPS.md)。兩種方式不要同時啟動相同 Token。

## 舊版單機器人設定相容模式

以下編號選單適用 `tgbot config --config "舊 .env 路徑"` 與非互動終端的相容模式；新版面板使用方向鍵與 Ctrl+S。`--check` 檢查舊版單一 `.env`，多機器人請使用 `check`。

在專案資料夾的 CMD 輸入 `tgbot` 即可開啟；PowerShell 使用 `.\tgbot.cmd`。啟動檔會優先使用 PATH 中已安裝的 `tgbot.exe`，尚未安裝時才使用專案 `.venv`，避免專案內的同名檔案蓋過套件版。要明確操作原始碼版，使用 `.venv\Scripts\python.exe -m app.cli`。若要保留舊版原始碼捷徑安裝方式，可執行：

```powershell
.\tgbot.cmd --install
```

此方式適用原始碼版，不需要管理員權限，將啟動指令放到 `%LOCALAPPDATA%\TelegramAnonymousBoard\bin` 並加入目前使用者的 PATH。請完全關閉終端程式後再開啟；若要讓目前的 CMD 立即使用，可先執行 `set "PATH=%LOCALAPPDATA%\TelegramAnonymousBoard\bin;%PATH%"`。套件版請使用前面的 uv 安裝方式。之後即可輸入：

```cmd
tgbot
tgbot --check
tgbot --help
```

選單提供 Token、群組／頻道 ID、管理員 ID、一般／粗體字型、時區及 Logo 設定。Token 輸入與畫面都會隱藏，無法隱藏輸入的終端會拒絕輸入。選「8」才會寫入畫面指定的 `.env`；離開或 Ctrl+C 不會儲存。既有註解及其他設定會保留，儲存前將上一版原樣備份為 `.env.bak`，只保留上一次設定。兩個檔案都包含秘密，請另外安全保存，勿提交或分享。

「7 檢查設定」及 `--check` 只檢查格式、時區及檔案存在，不會發送訊息。真正的 Token 有效性及目標權限會在啟動 Bot 時驗證。修改後需要重新啟動 Bot，設定不會即時套用。選單檢查的是 `.env` 本身；啟動環境若另外設定同名環境變數，環境變數仍優先。

未安裝套件時，啟動檔使用專案的 `.venv`，不需要先啟用虛擬環境。原始碼捷徑在專案搬移時，須先移除 `%LOCALAPPDATA%\TelegramAnonymousBoard\bin\tgbot.cmd`，再在新位置執行 `--install` 更新；移除舊捷徑時刪除該 bin 資料夾，並從 Windows 使用者 PATH 移除該路徑。套件版則不依賴原始碼資料夾的位置。

Linux 可從專案目錄執行 `.venv/bin/python -m app.cli`；VPS 請使用 `sudo -u telegrambot .venv/bin/python -m app.cli`，讓設定檔由服務帳號保存。也可繼續手動複製 `.env.example` 後編輯設定。繁中字型可放在 `assets/fonts/`，例如 `NotoSansTC-Regular.otf`、`NotoSansTC-Bold.otf`；`TELEGRAM_LOGO_PATH` 可留空以使用開發 fallback，正式環境建議自行提供有權使用的透明 PNG。

## 舊版單機器人 Ubuntu VPS 部署

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv fonts-noto-cjk
sudo useradd --system --home /opt/telegram-anonymous-board --shell /usr/sbin/nologin telegrambot
sudo mkdir -p /opt/telegram-anonymous-board
sudo chown -R telegrambot:telegrambot /opt/telegram-anonymous-board
```

把專案放到 `/opt/telegram-anonymous-board` 後，以服務帳號建立環境：

```bash
cd /opt/telegram-anonymous-board
sudo -u telegrambot python3.12 -m venv .venv
sudo -u telegrambot .venv/bin/pip install -r requirements.txt
sudo -u telegrambot cp .env.example .env
sudo -u telegrambot mkdir -p data/tmp
```

接著編輯 `.env`，將字型路徑設為 VPS 上實際存在的繁中字型，例如：

```dotenv
FONT_REGULAR_PATH=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
FONT_BOLD_PATH=/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc
```

將 `.env` 權限限制為服務帳號，安裝並啟動 systemd：

```bash
sudo chmod 600 .env
sudo chown telegrambot:telegrambot .env
sudo cp deploy/telegram-anonymous-board.service /etc/systemd/system/
sudo cp deploy/telegram-anonymous-board-backup.service deploy/telegram-anonymous-board-backup.timer /etc/systemd/system/
sudo chmod 700 data data/tmp
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-anonymous-board.service
sudo systemctl enable --now telegram-anonymous-board-backup.timer
sudo systemctl status telegram-anonymous-board.service
journalctl -u telegram-anonymous-board.service -f
```

Bot 必須先加入目標並取得傳送圖片與刪除訊息所需權限；頻道須有發文管理權限。啟動時會檢查聊天類型、加入狀態及圖片發送的明確權限限制。`CHANNEL_ID` 仍沿用原設定名稱，可填群組或頻道的負數 ID，超級群組／頻道通常為 `-100...`；`ADMIN_USER_ID` 是你自己的 Telegram User ID。

也可執行 `sudo bash deploy/setup-vps.sh` 安裝服務與備份 timer。已有 `.env` 時選擇保留原設定；升級不要重新產生 `CONTENT_HASH_SECRET`，否則舊內容 HMAC 無法繼續比對。

## 使用者與管理員操作

使用者私訊傳送文字後按確認發布。`/start` 會顯示「我的投稿」「檢舉方式」「匿名說明」按鈕，也可使用：

```text
/myposts
/report XG_ABCDEFGH 冒用他人身分
/privacy
/cancel
```

「我的投稿」每頁顯示 5 篇，只能查看及操作自己的文章。開啟貼文需要原群組／頻道的存取權；基本群組沒有可建立的 `t.me/c` 連結時，仍可使用文章 ID 申請刪文。已確認並正在處理的投稿無法用 `/cancel` 中途撤回，發布完成後再透過刪文流程處理。

管理員私訊 `/admin` 開啟面板。「暫停／恢復投稿」內提供明確的開關按鈕，重複按暫停不會誤恢復。暫停會擋住新稿及尚未送出的排隊稿件，檢舉與刪文仍可使用；已送往 Telegram 的請求仍可能完成。「直接發話／公告」可選單一或全部已設定目標，輸入純文字並選擇是否置頂，詳見 [README](../README.md#管理員直接發話與置頂)。

「待處理事項」分為發布／刪文與檢舉。可查詢文章、強制刪文、封鎖作者或結案檢舉。收到檢舉、發文結果不明或首次刪文失敗時，Bot 會盡力通知指定管理員；通知失敗時，待處理紀錄仍保存。管理員須先私訊啟用 Bot 才能收到通知。

發文結果不明時，先在目標群組／頻道搜尋文章 hashtag。若文章存在，先人工刪除全部圖片；確認目標內已無此文後，才使用「人工核對結案」並二次確認。這會解除未知發文的重投限制；曾成功發布的文章，即使刪除，原本 24 小時的重複限制與額度仍保留。

## 異常恢復與升級

只允許一個 Bot 程序使用同一份資料庫，第二個程序會拒絕啟動。發送節流在此程序中維護，重啟後仍會依 Telegram 回覆處理限速。

資料庫會在升級舊 schema 前自動建立一份備份，再加入新欄位與資料表，保留舊文章、作者、封鎖名單與時間。舊版本留下的未完成發文會保守保留供人工核對。重啟後尚未送出的稿件需要作者再次確認；結果不明的發文不會自動重送。

升級步驟：

1. 停止 `telegram-anonymous-board.service` 及備份 timer／service。
2. 將新版程式覆蓋至原專案目錄，保留 `.env`、`data/` 與正式字型。
3. 以服務帳號執行 `.venv/bin/python -m app.backup create`，先備份目前資料。
4. 執行 `sudo bash deploy/setup-vps.sh`，選擇保留原設定，安裝新版 unit、重啟 Bot 並啟用每日備份。
5. 查看服務日誌與管理面板的待處理事項。

部署壓縮包不包含 `.env`、資料庫、備份或虛擬環境。自動升級只備份資料庫，Bot Token 與 `CONTENT_HASH_SECRET` 所在的 `.env` 需要另外安全保存。

## 備份與還原

使用 SQLite Online Backup API 取得包含 WAL 已提交資料的一致備份，不直接複製正在使用的 `.sqlite3` 檔。備份存於資料庫旁的 `backups/`，保留最近 7 **份**，手動備份與還原前的安全備份也算一份。Linux 檔案使用 0600，備份目錄使用 0700；不透過 Telegram 傳送資料庫。

```bash
cd /opt/telegram-anonymous-board
sudo -u telegrambot .venv/bin/python -m app.backup create
systemctl list-timers telegram-anonymous-board-backup.timer --all
journalctl -u telegram-anonymous-board-backup.service -n 30 --no-pager
```

備份包含作者 ID、檢舉原因及備份當時的待確認原文，應視為私人資料。這是同台 VPS 的本機備份；需要防止整台主機或硬碟遺失時，應另行安全保存副本。

還原必須先停止服務。以下檔名請換成要還原的實際備份：

```bash
sudo systemctl stop telegram-anonymous-board.service telegram-anonymous-board-backup.timer telegram-anonymous-board-backup.service
cd /opt/telegram-anonymous-board
sudo -u telegrambot .venv/bin/python -m app.backup restore data/backups/實際備份檔名.sqlite3
sudo systemctl start telegram-anonymous-board.service telegram-anonymous-board-backup.timer
```

還原會驗證資料庫完整性與必要資料表，先建立現有資料的安全備份，再替換資料庫。Bot 仍在運行、另一個備份／還原工作未結束或驗證失敗時會拒絕覆寫。還原失敗請先查看錯誤訊息，勿刪除原資料或安全備份。

還原後會暫停投稿並清空待確認稿件與操作狀態，避免舊按鈕重送；作者需要重新投稿。Telegram 不會跟著回到備份時點，備份之後已發布的文章可能失去資料庫對應紀錄。管理員應先人工核對這段期間的貼文，再於面板恢復投稿。

