# 智學管家 ZhixueButler

> 智學網（zhixue.com）作業自動監控與提醒系統 · Multi-account homework monitor for Zhixue.com with WeChat push & bot

自動監控**多個賬號**（學生賬號 / 家長賬號）的作業情況，提醒發到**微信**等常用通訊工具。
支持 Windows 電腦和 Linux 服務器（Ubuntu 24.04 已適配），還可以在**微信裡直接綁定賬號**：

- 🕐 每 30 分鐘（可按賬號單獨調整）自動檢查未完成作業
- 🆕 新作業立即推送；距截止 ≤24 小時再提醒一次；**已過期不打擾**（僅狀態頁標紅）
- 🔁 **重複提醒可自選節奏**：未完成的作業按你選的間隔序列反覆提醒
  （如 5h→3h→2h→1h→30min→之後每10min，直到完成），**發送前會自動重新核實仍未完成**，
  已提交的作業絕不誤提醒
- 📱 推送渠道：**微信**（Server酱 / PushPlus / WxPusher / 企業微信自建應用）、
  企業微信群機器人、釘釘、飛書、郵件、Windows 彈窗，配幾個發幾個
- 💬 **微信機器人**：家長在微信裡發「綁定 賬號 密碼」即可自助接入，
  並可查詢狀態、修改檢測間隔，提醒精準發到每個人自己的微信
- 🌐 網頁狀態總覽 `data/status.html`，一屏看清所有賬號

## 工作原理

本工具通過智學網官方網頁接口（基於開源庫
[zhixuewang-python](https://github.com/anwenhu/zhixuewang-python)）查詢作業，
與智學網桌面 App 完全無關。

```
登入一次（瀏覽器 / 微信綁定 / 導入Cookie）
        │  憑證保存到 data/cookies/
        ▼
┌──────────────────────────────┐
│  monitor.py 常駐後台          │
│  按各賬號的間隔到期即檢查：     │
│   ├─ 有新作業      → 推送微信 │
│   ├─ 臨近截止      → 推送微信 │
│   └─ 憑證過期      → 推送提醒 │
│  （可選）微信機器人回調服務     │
└──────────────────────────────┘
```

## 通知渠道：微信優先

在 `config.json` 的 `notify` 裡配置（配幾個發幾個，全不配則退回桌面彈窗）：

| 渠道 | 特點 | 填哪裡 |
|---|---|---|
| **企業微信自建應用** ⭐推薦 | 免費無條數限制、直達**個人微信**、支持**按人定向**（配合微信機器人每人的提醒發給每個人） | `wecom_app_corpid` / `wecom_app_secret` / `wecom_app_agentid` / `wecom_app_touser` |
| **Server酱** | 最簡單，微信掃碼拿 SendKey 即用（免費每天限 5 條） | `serverchan_sendkey` |
| **PushPlus / WxPusher** | 微信公眾號推送，免費 | `pushplus_token` / `wxpusher_*` |
| **企業微信群機器人** | 一條通知全家可見 | `wecom_webhook` |
| **釘釘 / 飛書群機器人** | 班級群場景 | `dingtalk_*` / `feishu_webhook` |
| **郵件** | 通用兜底 | `email_*` |
| Windows 桌面彈窗 | 本機提醒 | `windows_toast` |

配置後運行 `python monitor.py --notify-test`，所有已配置渠道會同時收到測試消息。

### 多個孩子，各種提醒發到各個家長的微信

兩種方式（可混用）：

**方式一：企業微信機器人（推薦，全自動對應）**
每位家長用自己微信掃「微信插件」二維碼後，各自發 `綁定 賬號 密碼`，
系統自動把賬號綁定到發消息的人，之後提醒只發給綁定者本人。

**方式二：每人一個推送 Key（免註冊企業微信）**
每位家長用自己的微信掃碼拿各自的 [Server酱](https://sct.ftqq.com) SendKey
（或 WxPusher UID），填在**對應賬號的 `notify` 覆蓋**裡：

```json
"accounts": [
    {"label": "小明", "username": "賬號A", "password": "···", "enabled": true,
     "notify": {"serverchan_sendkey": "媽媽的SendKey"}},
    {"label": "小紅", "username": "賬號B", "password": "···", "enabled": true,
     "notify": {"wxpusher_app_token": "共同的token", "wxpusher_uids": ["爸爸的UID"]}}
]
```

規則：賬號裡的 `notify` 會**覆蓋全局同名配置**（可只覆蓋部分鍵），
沒寫 `notify` 的賬號沿用全局渠道。檢測間隔、重複提醒節奏等同理支持賬號級設置
（`check_interval_minutes`、`remind_schedule`）。

## 微信機器人：在微信裡完成一切（進階，推薦服務器部署使用）

> 原理：借助「企業微信自建應用 + 微信插件」。**家長全程只用個人微信**——
> 關注一個二維碼後，和機器人的對話出現在微信裡。

家長能做的事（全部在微信對話裡完成）：

```
綁定 賬號 密碼 [備注]   → 登記賬號，服務器後台自動嘗試登入並開始監控
cookie 賬號標籤 <字符串> → 自動登入被驗證碼攔截時，按機器人引導導入憑證（約1分鐘）
我的                    → 查看自己賬號的作業狀態
間隔 60 [賬號標籤]       → 設置檢測間隔（分鐘）
提醒 [賬號標籤] 節奏     → 設置未完成作業的重複提醒節奏
                          例：提醒 5h,3h,2h,1h,30m,10m（可選 5m~5h，可多選遞進）
                          提醒 关 = 關閉重複提醒
檢查 [賬號標籤]          → 立即檢查一次並推送結果
解綁 賬號標籤            → 停用並清除密碼
```

每個賬號只能被綁定者（或管理員）管理；作業提醒**定向發給綁定者本人的微信**。

### 家长的完整流程（全程只用個人微信，零配置）

```
1. 掃碼關注「微信插件」二維碼（部署者提供）
2. 在微信裡給機器人發任意消息 → 收到使用約定 → 回覆「同意」
3. 發送：綁定 賬號 密碼 [備注]
   → 服務器後台自動嘗試登入，成功即配置完成
   → 被驗證碼攔截時按機器人指引回覆 Cookie（約1分鐘）
4. 之後：新作業/臨近截止自動提醒到這個微信；
   隨時可用 我的 / 間隔 / 提醒 / 檢查 指令
```

### 開通步驟（部署者一次性，約10分鐘）

1. 註冊企業微信：[work.weixin.qq.com](https://work.weixin.qq.com)（個人即可註冊，200人內免費，無需企業認證）
2. 管理後台 → 應用管理 → 創建應用（自建）→ 記下 `AgentId`、`Secret`，加上企業的 `CorpID`（我的企業→企業信息）
3. 成員如何用個人微信收發：管理後台 → 我的企业 → **微信插件** → 讓家長掃碼關注
4. 應用詳情 → 接收消息 → 設置 API 接收：
   - URL 填 `http://你的服務器IP:8080/wecom`（端口對應 `wecom_bot.listen_port`，需公網可訪問）
   - 隨機生成 Token 和 EncodingAESKey，**同時**填到服務器的 `config.json` → `wecom_bot` 裡
5. `config.json` 設置：`wecom_bot.enabled: true` + 上面四項 + `notify.wecom_app_*`（用同一套 corpid/secret/agentid）
6. 啟動監控後，回到企業微信後台點「保存」完成回調驗證，然後在微信裡發「幫助」測試

> 服務器上想讓「綁定」指令的自動登入生效，需安裝無頭瀏覽器（可選）：
> `.venv/bin/playwright install --with-deps chromium`

### 關於「自動通過人機驗證」的說明

智學網登入頁的極驗(GeeTest)風控**無法也不應被程序全自動繞過**（違反平台風控且隨時失效）。
本系統的策略是合規且順滑的兩段式：

1. 「綁定」時服務器用**隱藏了自動化特徵的無頭瀏覽器**模擬真人登入，風控寬鬆時直接成功，**全程無感**；
2. 被攔截時機器人自動回覆圖文指引：家長在自己手機瀏覽器正常登入一次（人過人機驗證），
   把 Cookie 發回微信即完成綁定，之後全自動。憑證一般幾天到幾週有效，過期機器人會提醒重發。

## 快速開始（Windows 電腦）

1. **填賬號**：克隆本倉庫後，把 `config.example.json` 複製為 `config.json`
   （`config.json` 已被 .gitignore 排除，**不會也不應該**上傳到 Git），
   用記事本打開 → `accounts`：
   ```json
   "accounts": [
       {"label": "小明", "username": "學生號或手機號", "password": "密碼", "enabled": true},
       {"label": "小紅", "username": "第二個賬號", "password": "密碼", "enabled": true,
        "check_interval_minutes": 60}
   ]
   ```
   （`check_interval_minutes` 可選，為該賬號單獨設檢測間隔）
2. **配渠道**：`notify` 裡至少配一個（見上表），跑 `python monitor.py --notify-test` 驗證
3. **首次登入**：雙擊 `login_refresh.bat`，在打開的 Chrome/Edge 裡完成驗證碼並點「登錄」
   （被極驗封禁時點「請點擊重試」，或用 `import_cookie.bat` 導入 Cookie，見 FAQ）
4. **啟動**：雙擊 `start_monitor.bat`；開機自啟雙擊 `install_autostart.bat`

## 部署到服務器（Ubuntu 24.04 LTS）

Ubuntu 24.04 完全支持（系統源自帶 Python 3.12 + systemd）：

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip
sudo mkdir -p /opt/zhixue-monitor && sudo chown $USER /opt/zhixue-monitor
# 上傳項目文件到 /opt/zhixue-monitor 後：
cd /opt/zhixue-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 可選：無頭瀏覽器（微信機器人「綁定」自動登入用）：
.venv/bin/playwright install --with-deps chromium

# 編輯 config.json（賬號 + 微信渠道/機器人），然後導入憑證：
.venv/bin/python monitor.py --import-cookie 账号1 "Cookie字串"

sed 's/YOUR_USER/'$USER'/; s|/usr/bin/env python3|/opt/zhixue-monitor/.venv/bin/python|' \
    deploy/zhixue-monitor.service | sudo tee /etc/systemd/system/zhixue-monitor.service >/dev/null
sudo systemctl daemon-reload && sudo systemctl enable --now zhixue-monitor
journalctl -u zhixue-monitor -f
```

> - 服務已設 `TZ=Asia/Shanghai`；`requirements.txt` 里的 playwright 只在自動登入時用到，
>   不裝瀏覽器也不影響定時檢查和推送
> - 微信機器人需要回調端口公網可達（防火牆/安全組放行 `listen_port`，或用 nginx 反代到 443/80）
> - 無 root：`nohup .venv/bin/python monitor.py >/dev/null 2>&1 &`

## 提醒規則

| 事件 | 行為 |
|---|---|
| 有新作業 | 🆕 立即推送（和上一輪對比新增的） |
| 距截止 ≤ `deadline_warn_hours` | ⏰ 提醒一次 |
| 未完成作業的重複提醒 | 🔁 按 `remind_schedule` 節奏反覆提醒，直到完成 |
| 作業已過期 | **不提醒**（僅狀態頁標紅） |
| 首次啟動 | 只記錄現狀；發現 24 小時內截止的作業時發一條匯總 |
| 登入憑證過期 | 🔑 每 12 小時提醒一次；有微信機器人時發給綁定者本人 |

**重複提醒節奏** `remind_schedule`（分鐘序列，可多選遞進，
可選值：5 / 10 / 15 / 30 / 60 / 120 / 180 / 240 / 300）：

```json
"remind_schedule": [300, 180, 120, 60, 30, 10]
```

含義：作業提醒後仍未完成 → 隔 5 小時再提醒 → 再隔 3 小時 → 再 2 小時 →
再 1 小時 → 再 30 分鐘 → **之後每 10 分鐘重複，直到完成**。
設 `[]` 為關閉重複提醒（每項只提醒一次）。

**發送前核驗** `verify_before_send: true`（默認開啟）：重複提醒發出前，
程序會**重新拉取一次作業列表**，確認作業仍未完成才發送；核實失敗（網路異常等）
寧可不發也不誤發——孩子剛提交的作業絕不會再被催。

其他開關：`mode` 改成 `every` 可每次檢查都匯報完整未完成列表；
`remind_on_first_run: true` 讓啟動時把現有未完成作業也匯報一遍。
微信機器人裡可隨時用「提醒」指令按賬號調整節奏。

## 文件說明

```
zhixue-butler/
├── monitor.py                 # 主程序：賬號輪詢、提醒判斷、通知分發、狀態頁
├── wecom_bot.py               # 微信機器人：回調加解密、指令解析、主動推送
├── config.json                # 運行配置（含隱私，已被 .gitignore 排除）
├── config.example.json        # 配置模板：克隆後複製為 config.json 再填
├── requirements.txt           # Python 依賴
├── start_monitor.bat          # Win：啟動常駐監控
├── start_wechat_test.bat      # Win：一鍵啟動監控+公網隧道（微信機器聯調）
├── login_refresh.bat          # Win：瀏覽器登入/續期所有賬號
├── import_cookie.bat          # Win：手動導入 Cookie
├── check_once.bat             # Win：立即檢查一次
├── open_status.bat            # Win：打開狀態總覽頁
├── test_notify.bat            # Win：測試桌面彈窗
├── install_autostart.bat      # Win：開機自啟（uninstall_ 為取消）
├── setup.bat                  # Win：安裝依賴
├── deploy/
│   └── zhixue-monitor.service # Linux systemd 服務文件（Ubuntu 24.04 適配）
└── data/                      # 運行產物（已被 .gitignore 排除）
    ├── status.html            # 作業狀態總覽頁
    ├── logs/monitor.log       # 運行日誌
    ├── cookies/               # 各賬號登入憑證
    └── wecom_users.json       # 微信用戶協議狀態
```

| 文件 | 作用 |
|---|---|
| `monitor.py` | 主程序 |
| `wecom_bot.py` | 微信機器人（回調加解密、指令解析、主動推送） |
| `config.json` | **配置：賬號 + 渠道 + 機器人 + 提醒規則** |
| `config.example.json` | 配置模板（新用戶複製為 config.json 後填寫） |
| `login_refresh.bat` / `import_cookie.bat` | 桌面登入 / 手動導入 Cookie |
| `start_monitor.bat` / `check_once.bat` | 常駐監控 / 手動檢查一次 |
| `start_wechat_test.bat` | 本機聯調微信機器人（監控+公網隧道一鍵啟動） |
| `open_status.bat` / `test_notify.bat` | 打開狀態頁 / 測試桌面彈窗 |
| `install_autostart.bat` / `uninstall_autostart.bat` | Windows 開機自啟 |
| `setup.bat` | 重裝 Python 依賴 |
| `deploy/zhixue-monitor.service` | Ubuntu 24.04 systemd 服務文件 |
| `data/status.html` / `data/logs/` / `data/cookies/` | 狀態頁 / 日誌 / 登入憑證 |

## 常見問題

**Q：登入彈「極驗封禁 / Error 60500」？**
風控臨時攔截。點「請點擊重試」一般可過；程序已用真實 Chrome/Edge + 隱藏自動化特徵 + 真人節奏。
反覆被攔就用 `import_cookie.bat` 手動導入（微信機器人場景機器人會自動發指引）。

**Q：手動導入 Cookie**
自己瀏覽器登入 zhixue.com → F12 → 網絡 → 刷新 → 點第一條請求 →
請求標頭 → 複製 `Cookie:` 後的整串 → 雙擊 `import_cookie.bat` 粘貼
（或微信發 `cookie 賬號標籤 粘貼內容`；服務器上 `--import-cookie 賬號1 "整串"`）。

**Q：收不到通知？**
跑 `python monitor.py --notify-test` 並看日誌裡 `[通知·渠道名]` 的結果。
釘釘關鍵詞請設「提醒」；Server酱免費每天限 5 條，量大用企業微信自建應用或 WxPusher。

**Q：查不到作業 / 接口失效？**
先瀏覽器手動登入確認賬號可見作業；看 `data/logs/monitor.log`；
更新底層庫 `.venv/bin/pip install -U zhixuewang`。

**Q：安全嗎？**
賬號密碼明文存本機 `config.json`、憑證在 `data/cookies/`，勿外傳/上傳。
兩者均已被 `.gitignore` 排除，不會被意外提交到 Git。
所有請求直連智學網官方與你配置的推送渠道。服務器上建議 `chmod -R o-rwx /opt/zhixue-monitor`。

## 依賴與致謝

- [zhixuewang-python](https://github.com/anwenhu/zhixuewang-python)（MIT）—— 智學網網頁接口封裝
- [playwright](https://playwright.dev/python/) —— 僅登入環節使用
- [pycryptodome](https://www.pycryptodome.com) —— 微信機器人回調加解密

## License

[MIT](LICENSE)
