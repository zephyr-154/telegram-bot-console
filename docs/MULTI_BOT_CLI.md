# 多機器人 CLI

輸入 `tgbot` 先開啟互動指令介面，包含啟動畫面與 `tgbot Ready>` 輸入框。在這個輸入框輸入 `tgbot config`，才會開啟深色控制面板。面板首頁列出所有機器人的狀態、PID、記憶體與主機記憶體使用率，每秒更新。上下方向鍵選取，Enter 直接進入該台設定；Esc 或 q 離開面板後回到指令輸入框，背景程序繼續執行。

設定畫面使用分區標題、左側欄位與右側值。Enter 編輯選取的欄位，再按 Enter 套用；Ctrl+S 驗證並儲存；Esc 取消編輯，或放棄尚未儲存的修改並返回。Token 全程遮蔽，編輯時留空會保留原值。下方提供背景啟動、停止、重啟、備份、日誌與刪除。

## 即時群組／頻道偵測

1. 在 **Bot Token** 按 Enter 輸入，再按 Enter 套用。可直接使用畫面中尚未儲存的 Token，不必先填目標 ID、管理員或字型。
2. 移到 Token 下方 **偵測群組／頻道**，按 Enter 開始。顯示「等待邀請 @你的機器人」後，再到 Telegram 邀請它加入群組或頻道。
3. 收到加入通知後，「已記錄」數量會自動更新。移到 **目標群組／頻道 ID** 按 Enter，可看到名稱、ID、群組／頻道類型。
4. ↓ 選取清單、Enter 單選帶入；需要多個目標時使用 Space 勾選／取消，再 Enter 套用。也能直接在下方輸入框填逗號分隔的 ID，或輸入名稱／ID 的一部分篩選。
5. 選擇 **拉取群組管理員**，再於 **管理員 User ID** 選取候選。邀請人也會出現在清單中，Space 支援多選；候選不會自動取得權限。
6. 完成其他欄位後 Ctrl+S 儲存，選擇「背景啟動」。設定面板會先結束暫時偵測並釋放連線，再啟動正式程序。

偵測期間再次在該列按 Enter 可停止；離開設定面板也會停止暫時偵測。已記錄的清單會保留。正式背景程序執行中時，由它持續登記群組；設定面板每秒讀取結果，不會再啟動另一個接收更新的程序。從舊版更新後，請先重啟原本的背景程序。

舊群組尚未出現時，在偵測期間於群組傳送 `/start@你的機器人帳號`，頻道則發一則新貼文。這是用新通知登記群組，不會把群組文字當成匿名投稿。開始偵測時會核對已知群組的目前名稱與機器人加入狀態；被移除的群組不再提供選取。更換成另一台 Bot 的 Token 會切換到它自己的清單，同一 Bot 重設 Token 可沿用原清單。

Telegram 沒有列出 Bot 所有既有群組的 API，待接收通知最多保留 24 小時，因此舊通知已過期且從未登記的群組，需要上述操作或手動填入 ID。單獨偵測會保留待處理投稿，不會確認或清空更新；若累積到 Telegram 單次上限 100 筆，畫面會提示新通知可能排在後面，此時需先啟動正式程序處理，或先手動填入目標 ID。背景執行模式會照原有投稿流程處理更新，不受此暫時偵測限制。[Telegram 更新機制](https://core.telegram.org/bots/api#getting-updates)

如果同一 Bot 正在其他部署接收更新，請先停止原部署；若設定了 Webhook，請先在原部署停用。面板會顯示原因，不會自動解除既有 Webhook。非互動終端的編號選單仍使用手動 ID；即時偵測與方向鍵清單適用互動式 CMD、PowerShell 及終端機。

## 多位管理員與多個投稿目標

管理員與目標欄位都支援方向鍵瀏覽、名稱或帳號搜尋、Space 勾選多個、Enter 套用；未使用 Space 時，Enter 可直接單選目前項目。也能手動輸入 `123456789,987654321` 或 `-1001234567890,-1009876543210`。清單含有已設定但尚未取得名稱的 ID，仍可取消勾選。重複 ID 會合併。

邀請人來自 Bot 被加入那次的 Telegram 通知，後來調整管理員權限的操作者不會覆蓋原邀請人。若加入通知未收到或已過期，無法補查原邀請人；可拉取目前群組管理員，或手動填入 ID。拉取需有效 Token 與已選目標，不必先儲存其他欄位，也不必先有管理員 ID。取得不到的目標會顯示讀取失敗數量，其他成功取得的候選仍可使用；已記錄的資料可能保留，請依需要重新拉取。

同一台 Bot 的指定管理員都能使用完整管理面板，各有獨立的私訊操作狀態。所有指定管理員都會收到通知；每位需先私訊 Bot 按 `/start`，未開啟私訊或封鎖 Bot 的管理員收不到通知，不影響其他人。這些權限作用於整台 Bot 的所有投稿目標。

每篇投稿使用同一文章 ID、只製圖一次，依序發到所有所選群組／頻道。確認畫面顯示目標數量，待確認稿會保存當時的目標；若設定改變，需要重新投稿確認。每份副本的群組與訊息 ID 都會保存，因此變更目前設定後，刪文仍會處理原始副本。「我的投稿」與管理員文章詳情會提供各目標的連結。

發送到部分目標後若遇到拒絕、暫停或到期，會嘗試撤回已知副本。若回覆遺失、程序中斷或撤回失敗，保留原始目標與可取得的訊息紀錄，阻擋重複投稿，通知管理員逐一核對；不會把「已刪除已知副本」當成所有副本都已刪除。結果不明時須在所有原始目標搜尋文章 ID，手動刪除殘留副本後，再從待處理事項人工結案。

舊版單一 ID 設定不需搬移。首次開啟舊資料庫時，會先在該台的備份目錄備份，再新增目標欄位；文章、私訊待確認原文及既有訊息紀錄會保留。沒有目標快照的舊稿若要改成多群組發布，需重新確認。

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
