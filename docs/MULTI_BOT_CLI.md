# 多機器人 CLI

輸入 `tgbot` 先開啟互動指令介面，包含啟動畫面與 `tgbot Ready>` 輸入框。在這個輸入框輸入 `tgbot config`，才會開啟深色控制面板。面板首頁列出所有機器人的狀態、PID、記憶體與主機記憶體使用率，每秒更新。上下方向鍵選取，Enter 直接進入該台設定；Esc 或 q 離開面板後回到指令輸入框，背景程序繼續執行。

設定畫面使用分區標題、左側欄位與右側值。Enter 編輯選取的欄位，再按 Enter 套用；Ctrl+S 驗證並儲存；Esc 取消編輯，或放棄尚未儲存的修改並返回。Token 全程遮蔽，編輯時留空會保留原值。下方提供背景啟動、停止、重啟、備份、日誌與刪除。

## 互動指令與即時名稱提示

在 `tgbot Ready>` 後輸入 `tgbot start`，再加一個空格，下方就會列出現有機器人名稱與狀態，不必先按 Tab。繼續輸入名稱會即時篩選；↑↓ 選取，Tab 或第一次 Enter 帶入，再按 Enter 執行。`stop`、`restart`、`config`、`status`、`logs`、`check`、`backup`、`restore`、`delete` 也有名稱提示。

可使用完整指令 `tgbot start school`，或簡寫成 `start school`、`/start school`。輸入 `tgbot` 後加空格或輸入 `/` 可查看指令提示。`help` 顯示說明，↑↓ 可在沒有提示選單時瀏覽本次工作階段的歷史。歷史只存於記憶體，不寫入磁碟；Token 請在設定面板中輸入。

`exit`、Ctrl+C、Ctrl+D 離開互動介面，背景機器人繼續執行。`--help`、錯誤指令及面板關閉後會返回輸入框。若在介面中確認 `tgbot app delete`，介面會結束，讓解除安裝程序完成。需要含空格的備份路徑時，使用雙引號，例如 `tgbot restore school "C:\my backups\snapshot.sqlite3"`。

這些即時提示屬於 `tgbot` 的互動輸入框；外層 CMD、PowerShell、bash 仍可直接執行一次性的 `tgbot start school` 等指令。非互動終端改用普通文字輸入，控制面板使用編號選單。

## 指令

- `tgbot add school`：新增並設定 school；省略名稱會詢問，`--no-config` 只建立草稿。
- `tgbot`：開啟互動指令介面。
- `tgbot config`：開啟多機器人控制面板。
- `tgbot config school`：直接設定指定機器人。
- `tgbot start`：背景啟動全部。
- `tgbot start school`：背景啟動指定機器人。
- `tgbot stop`：停止全部。
- `tgbot stop school`：停止指定機器人。
- `tgbot restart`：停止後重啟全部；加名稱可只重啟一台。
- `tgbot status`／`tgbot list`：列出狀態；`status school` 只看一台。
- `tgbot check`：檢查各台本機設定；加名稱可只檢查一台。
- `tgbot logs school`：最近 40 行日誌。
- `tgbot backup school`：立即備份這台資料庫。
- `tgbot restore school "備份.sqlite3"`：停止該台後還原，還原後投稿會暫停供人工核對。
- `tgbot delete school`：停止並永久刪除該台的設定、資料庫、備份及日誌，需輸入名稱確認。
- `tgbot app delete`：完整解除安裝 uv 套件版及管理目錄中的所有私人資料，需輸入 `DELETE ALL` 確認。獨立程序會在 CLI 結束後完成，畫面會先顯示結果日誌位置。
- `tgbot --version`／`tgbot --help`：版本與說明。

刪除指令可明確加上 `--yes`，供腳本確認操作。若只想移除套件並保留資料，先停止全部，再使用 `uv tool uninstall telegram-anonymous-board`。完整自動解除安裝適用 uv 工具安裝，不會遞迴刪除原始碼開發專案；共用的 Python 與 uv 也會保留。

名稱支援 1～40 個中英文字、數字、底線、連字號，不分大小寫檢查重複。`tgbot start --select`、`tgbot stop --select` 提供方向鍵選單；輸入部分名稱如 `tgbot config sch` 可篩選符合名稱。`tgbot names` 只列出名稱。非互動終端提供編號選單。

## 資料與隔離

管理目錄預設為 Windows `%LOCALAPPDATA%\TelegramAnonymousBoard`，Linux／macOS `$XDG_CONFIG_HOME/tgbot`，未設定 XDG 時為 `~/.config/tgbot`。可使用 `TGBOT_HOME` 指定獨立資料夾。

每台放在 `bots/名稱/`，包含 `.env`、`data/bot.sqlite3`、`data/backups/`、輪替日誌 `bot.log` 及執行紀錄。每台獨立背景程序，設定變更需重啟該台生效。修改設定前會保留上一版 `.env.bak`，既有 HMAC 秘密不會重新產生。設定、資料庫與備份都是私人資料，不應上傳 GitHub。

同一管理目錄禁止重複部署相同 Telegram Bot。資料庫與暫存必須位於該台目錄內。背景程序不繼承全域同名 Token 或目標設定，避免多台誤用相同設定。停止會核對 PID、建立時間與程序參數，先正常關閉，最多等待 15 秒，再終止無回應的程序。停止全部會先通知各台，再等候結束。

「啟動中」表示背景程序正在驗證；完成 Telegram Token 與群組權限檢查後才標記「執行中」。異常退出會顯示「異常停止」，可查日誌後重啟。日誌最多約 8 MB（目前檔加 3 份輪替），不顯示 Token。

關閉面板與 SSH 登出後仍繼續執行；VPS 重新開機後需要重新啟動，或使用下方 systemd 範例。背景模式不會在崩潰後無限重試。可部署數量取決於記憶體、CPU、製圖頻率與 Telegram 限制，請依面板的實際記憶體資訊評估。

刪除本機部署不會刪除群組歷史貼文，也不會刪除 BotFather 中的 Bot。

## VPS 開機啟動

可選用 `deploy/tgbot-manager.service`。預設專案位於 `/opt/telegram-anonymous-board`，使用該處 `.venv` 與服務帳號 telegrambot，管理目錄為 `/opt/telegram-anonymous-board/data/manager`。

先依 [VPS 部署步驟](VPS.md)完成 Python 環境、服務帳號、資料夾權限及 `fonts-noto-cjk` 字型安裝，再使用同一帳號及管理目錄設定：

```bash
cd /opt/telegram-anonymous-board
sudo -u telegrambot env TGBOT_HOME=/opt/telegram-anonymous-board/data/manager .venv/bin/python -m app.cli
sudo cp deploy/tgbot-manager.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tgbot-manager.service
```

開機服務會啟動全部機器人，停止服務會停止全部。請避免舊版單機器人服務與此服務同時啟動相同 Bot。這個 unit 管理程序群組，不會監控每台 Telegram 連線或自動重啟個別崩潰程序。

原始碼 systemd 部署需先停用並移除 unit，再備份及自行清理部署目錄；`app delete` 適用 uv 套件版。升級前先停止並備份，再重新安裝程式。

## 舊版相容

0.1 版單機器人的 `.env` 和資料不會自動搬移。舊版前景執行改用 `tgbot run --config "舊 .env 路徑"`；`config`、`check`、`backup`、`restore` 保留 `--config` 相容模式。改用多機器人之前先停止舊服務，保存 `.env`、`CONTENT_HASH_SECRET` 與資料庫備份。
