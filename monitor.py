#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
智學網作業多賬號自動監控提醒
============================
- 支持多個賬號（學生賬號 / 家長賬號均可），逐個檢查未完成作業
- 首次登入通過瀏覽器完成（可處理驗證碼），之後使用本機緩存的 cookie 自動續查
- 發現新作業 / 臨近截止 / 已過期時彈出 Windows 系統通知
- 生成 data/status.html 狀態總覽頁，雙擊即可查看所有賬號的作業情況

用法：
    python monitor.py                # 常駐監控（按 config 的間隔輪詢）
    python monitor.py --once         # 只檢查一次後退出
    python monitor.py --relogin      # 彈出瀏覽器為所有賬號重新登入
    python monitor.py --relogin 賬號1 # 只為指定 label 的賬號重新登入
    python monitor.py --test-notify  # 發送一條測試通知
    python monitor.py --demo         # 用模擬數據生成狀態頁預覽
    python monitor.py --open-status  # 打開狀態總覽頁
    python monitor.py --gen-config   # （重新）生成示例配置文件
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import logging.handlers
import os
import random
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import requests

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
COOKIE_DIR = DATA_DIR / "cookies"
LOG_DIR = DATA_DIR / "logs"
STATE_PATH = DATA_DIR / "state.json"
STATUS_PATH = DATA_DIR / "status.html"
CONFIG_PATH = BASE_DIR / "config.json"
TASK_NAME = "ZhixueHomeworkMonitor"

NOTIFY_TITLE = "📚 智學網作業提醒"

EXAMPLE_CONFIG = {
    "check_interval_minutes": 30,
    "homework_page_size": 50,
    "auto_relogin": False,
    "notify": {
        "mode": "new_only",             # new_only=只提醒新作業; every=每次檢查都匯報
        "remind_on_first_run": False,
        "deadline_warn_hours": 24,      # 截止前多久提醒；逾期一律不提醒
        "remind_schedule": [],          # 重複提醒節奏(分鐘序列,可從5/10/15/30/60/120/180/240/300中選多個)：
                                        #   例 [300,180,120,60,30,10]=提醒後5h再提醒、再3h、再2h、再1h、再30min、
                                        #   之後每10min重複，直到完成；[]=不重複提醒（每項只提醒一次）
        "verify_before_send": True,     # 重複提醒發送前先重新核實作業仍未完成，未核實成功不發送
        # ↓↓ 通知渠道：配幾個發幾個，都不配則 Windows 上退回系統彈窗 ↓↓
        "windows_toast": True,          # Windows 系統彈窗（服務器上自動忽略）
        "windows_toast_sound": True,
        "serverchan_sendkey": "",       # Server醬 https://sct.ftqq.com 微信推送
        "pushplus_token": "",           # PushPlus https://www.pushplus.plus 微信推送
        "wxpusher_app_token": "",       # WxPusher https://wxpusher.zjiecode.com
        "wxpusher_uids": [],            #   接收者UID（掃碼關注後獲得）
        "wxpusher_topic_ids": [],       #   或主題ID
        "wecom_webhook": "",            # 企業微信群機器人 Webhook 地址
        "wecom_app_corpid": "",         # 企業微信自建應用（個人微信可收，支持按人定向）
        "wecom_app_secret": "",
        "wecom_app_agentid": 0,
        "wecom_app_touser": "@all",     # 默認接收人（企業微信用戶ID）；微信機器人綁定後自動按人發送
        "dingtalk_webhook": "",         # 釘釘群機器人 Webhook 地址
        "dingtalk_secret": "",          #   加簽密鑰（沒有則留空；關鍵詞建議設「提醒」）
        "feishu_webhook": "",           # 飛書群機器人 Webhook 地址
        "email_smtp_host": "",          # 郵件：如 smtp.qq.com / smtp.163.com
        "email_smtp_port": 465,
        "email_ssl": True,
        "email_username": "",           # 發件郵箱
        "email_password": "",           # 郵箱 SMTP 授權碼（不是登錄密碼）
        "email_to": [],                 # 收件郵箱列表
    },
    "wecom_bot": {
        "enabled": False,
        "corpid": "",                   # 企業ID
        "secret": "",                   # 自建應用的 Secret
        "agentid": 0,                   # 自建應用的 AgentId
        "callback_token": "",           # 回調配置的 Token
        "encoding_aes_key": "",         # 回調配置的 EncodingAESKey（43位）
        "listen_port": 8080,            # 回調服務端口（反代/防火牆放行）
        "admins": []                    # 企業微信用戶ID列表，可管理所有賬號
    },
    "accounts": [
        {"label": "账号1", "username": "在此填寫賬號(學生號或手機號)", "password": "在此填寫密碼", "enabled": True},
        {"label": "账号2", "username": "", "password": "", "enabled": True},
    ],
}

# 舊版配置的鍵名 → 新鍵名（讀取舊 config.json 時自動遷移）
LEGACY_NOTIFY_KEYS = {"toast": "windows_toast", "sound": "windows_toast_sound"}

NEED_LOGIN_TOAST_INTERVAL = 12 * 3600  # 「需要重新登入」通知的節流間隔（秒）

PS_TOAST = r'''
$ErrorActionPreference = "Stop"
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

$title = $env:ZHIXUE_TOAST_TITLE
$body = $env:ZHIXUE_TOAST_BODY
$silent = $env:ZHIXUE_TOAST_SILENT -eq "1"

function Esc([string]$s) { [System.Security.SecurityElement]::Escape($s) }

if ($silent) { $audio = "<audio silent=`"true`"/>" }
else { $audio = "<audio src=`"ms-winsoundevent:Notification.Default`"/>" }

$xmlText = "<toast scenario=`"reminder`"><visual><binding template=`"ToastGeneric`"><text>$(Esc $title)</text><text>$(Esc $body)</text></binding></visual>$audio</toast>"

$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($xmlText)
$toast = New-Object Windows.UI.Notifications.ToastNotification($xml)
$appId = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
'''

log = logging.getLogger("zhixue")


# ---------------------------------------------------------------------------
# 基礎工具
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "monitor.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout is not None:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        log.addHandler(ch)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default
    except Exception as e:
        log.warning("讀取 %s 失敗（%s），使用默認值", path, e)
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_") or "account"


# ---------------------------------------------------------------------------
# 通知（Windows Toast）
# ---------------------------------------------------------------------------

def send_toast(title: str, body: str, sound: bool = True) -> bool:
    try:
        env = os.environ.copy()
        env["ZHIXUE_TOAST_TITLE"] = title
        env["ZHIXUE_TOAST_BODY"] = body
        env["ZHIXUE_TOAST_SILENT"] = "0" if sound else "1"
        encoded = base64.b64encode(PS_TOAST.encode("utf-16-le")).decode("ascii")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded],
            env=env, capture_output=True, timeout=40, creationflags=flags,
        )
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "replace").strip()[:300])
        return True
    except Exception as e:
        log.warning("Windows 通知發送失敗：%s", e)
        try:
            import winsound
            winsound.MessageBeep()
        except Exception:
            pass
        return False


def make_notifier(ncfg: dict):
    """根據配置構建通知渠道列表；所有渠道依次發送，單個渠道失敗不影響其他"""
    senders = []
    if ncfg.get("windows_toast", True) and sys.platform == "win32":
        _s = bool(ncfg.get("windows_toast_sound", True))
        senders.append(("Windows彈窗", lambda t, b, _s=_s: send_toast(t, b, sound=_s)))
    if ncfg.get("serverchan_sendkey"):
        _k = ncfg["serverchan_sendkey"]
        senders.append(("Server醬(微信)", lambda t, b, _k=_k: _send_serverchan(_k, t, b)))
    if ncfg.get("pushplus_token"):
        _k = ncfg["pushplus_token"]
        senders.append(("PushPlus(微信)", lambda t, b, _k=_k: _send_pushplus(_k, t, b)))
    if ncfg.get("wxpusher_app_token") and (ncfg.get("wxpusher_uids") or ncfg.get("wxpusher_topic_ids")):
        _c = {k: ncfg.get(k) for k in ("wxpusher_app_token", "wxpusher_uids", "wxpusher_topic_ids")}
        senders.append(("WxPusher(微信)", lambda t, b, _c=_c: _send_wxpusher(_c, t, b)))
    if ncfg.get("wecom_webhook"):
        _u = ncfg["wecom_webhook"]
        senders.append(("企業微信機器人", lambda t, b, _u=_u: _send_wecom(_u, t, b)))
    if ncfg.get("wecom_app_corpid") and ncfg.get("wecom_app_secret") and ncfg.get("wecom_app_agentid"):
        _c = (ncfg["wecom_app_corpid"], ncfg["wecom_app_secret"],
              int(ncfg["wecom_app_agentid"]), ncfg.get("wecom_app_touser") or "@all")
        senders.append(("企業微信應用(微信)", lambda t, b, tu=None, _c=_c: _send_wecom_app(_c, t, b, tu)))
    if ncfg.get("dingtalk_webhook"):
        _u, _s = ncfg["dingtalk_webhook"], ncfg.get("dingtalk_secret") or ""
        senders.append(("釘釘機器人", lambda t, b, _u=_u, _s=_s: _send_dingtalk(_u, _s, t, b)))
    if ncfg.get("feishu_webhook"):
        _u = ncfg["feishu_webhook"]
        senders.append(("飛書機器人", lambda t, b, _u=_u: _send_feishu(_u, t, b)))
    if ncfg.get("email_smtp_host") and ncfg.get("email_to"):
        _c = {k: ncfg.get(k) for k in ("email_smtp_host", "email_smtp_port", "email_ssl",
                                       "email_username", "email_password", "email_to")}
        senders.append(("郵件", lambda t, b, _c=_c: _send_email(_c, t, b)))

    if not senders:
        # 一個渠道都沒配：Windows 上退回彈窗，其他平台僅記錄日誌
        if sys.platform == "win32":
            senders.append(("Windows彈窗", lambda t, b: send_toast(t, b)))

    def notify(title: str, body: str, touser: str | None = None) -> None:
        for name, fn in senders:
            try:
                ok = fn(title, body) if name != "企業微信應用(微信)" else fn(title, body, touser)
                log.info("[通知·%s%s] %s | %s", name, "：已發送" if ok else "：失敗",
                         title, body.replace("\n", " "))
            except Exception as e:
                log.warning("[通知·%s] 發送異常：%s", name, e)

    return notify


_wecom_client_cache: dict = {}


def _send_wecom_app(cfg: tuple, title: str, body: str, touser: str | None = None) -> bool:
    """企業微信自建應用推送（成員在個人微信裡通過「微信插件」接收）"""
    corpid, secret, agentid, default_touser = cfg
    from wecom_bot import WeComClient
    key = (corpid, secret)
    client = _wecom_client_cache.get(key)
    if client is None:
        client = _wecom_client_cache[key] = WeComClient(corpid, secret)
    return client.push_text(touser or default_touser, f"{title}\n{body}", agentid)


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _send_serverchan(sendkey: str, title: str, body: str) -> bool:
    r = requests.post(f"https://sctapi.ftqq.com/{sendkey}.send",
                      data={"title": _clip(title, 32), "desp": body}, timeout=20)
    return r.ok and r.json().get("code") == 0


def _send_pushplus(token: str, title: str, body: str) -> bool:
    r = requests.post("https://www.pushplus.plus/send",
                      json={"token": token, "title": _clip(title, 100),
                            "content": body, "template": "txt"}, timeout=20)
    return r.ok and r.json().get("code") == 200


def _send_wxpusher(cfgw: dict, title: str, body: str) -> bool:
    r = requests.post("https://wxpusher.zjiecode.com/api/send/message",
                      json={"appToken": cfgw.get("wxpusher_app_token"),
                            "summary": _clip(title, 99),
                            "content": f"{title}\n{body}",
                            "contentType": 1,
                            "uids": cfgw.get("wxpusher_uids") or [],
                            "topicIds": cfgw.get("wxpusher_topic_ids") or []},
                      timeout=20)
    return r.ok and r.json().get("code") == 1000


def _send_wecom(url: str, title: str, body: str) -> bool:
    r = requests.post(url, json={"msgtype": "text",
                                 "text": {"content": _clip(f"{title}\n{body}", 2000)}},
                      timeout=20)
    return r.ok and r.json().get("errcode") == 0


def _send_dingtalk(url: str, secret: str, title: str, body: str) -> bool:
    if secret:
        ts = str(round(time.time() * 1000))
        sign = base64.b64encode(hmac.new(secret.encode(),
                                         f"{ts}\n{secret}".encode(),
                                         hashlib.sha256).digest())
        url = url + f"&timestamp={ts}&sign={quote_plus(sign)}"
    r = requests.post(url, json={"msgtype": "text",
                                 "text": {"content": _clip(f"{title}\n{body}", 2000)}},
                      timeout=20)
    return r.ok and r.json().get("errcode") == 0


def _send_feishu(url: str, title: str, body: str) -> bool:
    r = requests.post(url, json={"msg_type": "text",
                                 "content": {"text": f"{title}\n{body}"}}, timeout=20)
    data = r.json()
    return r.ok and (data.get("code") == 0 or data.get("StatusCode") == 0)


def _send_email(cfgm: dict, title: str, body: str) -> bool:
    import smtplib
    from email.header import Header
    from email.mime.text import MIMEText
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = cfgm["email_username"]
    msg["To"] = ", ".join(cfgm["email_to"])
    port = int(cfgm.get("email_smtp_port") or 465)
    if cfgm.get("email_ssl", True):
        s = smtplib.SMTP_SSL(cfgm["email_smtp_host"], port, timeout=25)
    else:
        s = smtplib.SMTP(cfgm["email_smtp_host"], port, timeout=25)
        if port == 587:
            s.starttls()
    try:
        s.login(cfgm["email_username"], cfgm["email_password"])
        s.sendmail(cfgm["email_username"], cfgm["email_to"], msg.as_string())
    finally:
        s.quit()
    return True


def channels_configured(ncfg: dict) -> bool:
    """是否配置了至少一個跨設備通知渠道（Windows 彈窗以外）"""
    return bool(
        ncfg.get("serverchan_sendkey") or ncfg.get("pushplus_token")
        or (ncfg.get("wxpusher_app_token") and (ncfg.get("wxpusher_uids") or ncfg.get("wxpusher_topic_ids")))
        or ncfg.get("wecom_webhook") or ncfg.get("dingtalk_webhook") or ncfg.get("feishu_webhook")
        or (ncfg.get("wecom_app_corpid") and ncfg.get("wecom_app_secret") and ncfg.get("wecom_app_agentid"))
        or (ncfg.get("email_smtp_host") and ncfg.get("email_to"))
    )


# ---------------------------------------------------------------------------
# 智學網會話管理
# ---------------------------------------------------------------------------

def _import_zxw():
    try:
        from zhixuewang.account import get_account_role, session_to_account
        from zhixuewang.session import get_basic_session
        return get_basic_session, get_account_role, session_to_account
    except ImportError as e:
        log.error("缺少依賴庫（%s）。請先雙擊運行 setup.bat 安裝依賴。", e)
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# 瀏覽器登入（隱藏自動化特徵，避免被極驗/GeeTest 風控封禁）
# ---------------------------------------------------------------------------

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}, loadTimes: () => ({}), csi: () => ({})};
const _origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : _origQuery(p);
"""

LOGIN_URL = "https://www.zhixue.com/wap_login.html"


async def _pw_login_async(username: str, password: str, headless: bool = False,
                          wait_timeout: float | None = None, auto_submit: bool = False) -> dict:
    from playwright.async_api import async_playwright

    async def launch_browser(p):
        launch_kwargs = {
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        # 優先用系統真實瀏覽器（指紋真實，最不容易被風控攔截）
        for channel in ("chrome", "msedge"):
            try:
                browser = await p.chromium.launch(channel=channel, **launch_kwargs)
                log.info("使用系統%s瀏覽器登入", "Chrome" if channel == "chrome" else "Edge")
                return browser
            except Exception:
                continue
        log.info("未找到系統 Chrome/Edge，使用內置瀏覽器（如遇「極驗封禁」請點重試或手動輸入）")
        return await p.chromium.launch(**launch_kwargs)

    async with async_playwright() as p:
        browser = await launch_browser(p)
        try:
            context = await browser.new_context(
                locale="zh-CN", timezone_id="Asia/Shanghai",
                viewport={"width": 1280, "height": 820},
                # 無頭模式 UA 會帶 HeadlessChrome 字樣，換成正常 UA 避免被風控識別
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36") if headless else None,
            )
            await context.add_init_script(STEALTH_JS)
            page = await context.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            await page.wait_for_timeout(int(800 + random.random() * 800))
            # 模擬真人節奏填入賬號密碼；桌面模式由用戶完成驗證碼和點擊登錄
            try:
                await page.click("#txtUserName")
                await page.type("#txtUserName", username, delay=60 + random.random() * 80)
                await page.wait_for_timeout(int(300 + random.random() * 700))
                await page.click("#txtPassword")
                await page.type("#txtPassword", password, delay=60 + random.random() * 80)
                if auto_submit:
                    await page.wait_for_timeout(int(400 + random.random() * 600))
                    await page.click("#signup_button")
                    log.info("已自動填寫並提交登入（無人值守模式），等待結果…")
                else:
                    log.info("已自動填好賬號密碼。請在瀏覽器視窗中完成滑塊驗證，然後點擊「登錄」按鈕。")
            except Exception as e:
                if not auto_submit:
                    log.warning("自動填寫賬號失敗（%s），請直接在瀏覽器中手動輸入賬號密碼登入。", e.__class__.__name__)
            await page.wait_for_url("**/htm-vessel/**", timeout=wait_timeout or 0)
            await page.wait_for_timeout(1500)  # 等登入憑證 cookie 寫完
            cookies = {c["name"]: c["value"] for c in await context.cookies()}
        finally:
            await browser.close()
        if not cookies.get("loginUserName"):
            raise RuntimeError("登入後未取到有效憑證（loginUserName cookie 缺失）")
        return cookies


def playwright_login(username: str, password: str, headless: bool = False,
                     wait_timeout: float | None = None, auto_submit: bool = False) -> dict:
    """彈出瀏覽器登入智學網，返回 cookie 字典。
    headless=True 用於服務器無人值守（失敗會拋異常，由調用方兜底）；桌面模式等待用戶完成登入。"""
    try:
        return asyncio.run(_pw_login_async(username, password, headless=headless,
                                           wait_timeout=wait_timeout, auto_submit=auto_submit))
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        raise
    except Exception as e:
        raise RuntimeError(f"瀏覽器登入未完成：{e.__class__.__name__}: {e}") from e


def _cookie_file(label: str) -> Path:
    return COOKIE_DIR / f"{_safe_name(label)}.json"


def load_cookies(label: str):
    f = _cookie_file(label)
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data else None
    except Exception:
        return None


def save_cookies(label: str, session) -> None:
    COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    jar = requests.utils.dict_from_cookiejar(session.cookies)
    _cookie_file(label).write_text(json.dumps(jar, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("已保存「%s」的登入憑證", label)


def build_session(cookies: dict):
    get_basic_session, _, _ = _import_zxw()
    s = get_basic_session()
    s.cookies.update(cookies)
    if cookies.get("uname") is None and cookies.get("loginUserName"):
        s.cookies.set("uname", base64.b64encode(cookies["loginUserName"].encode()).decode())
    return s


def validate_session(session) -> tuple[str | None, str | None]:
    """返回 (role, err)；err 為 None 表示有效，'network' 表示網路問題，'expired' 表示登入已失效"""
    try:
        r = session.get("https://www.zhixue.com/container/getCurrentUser", timeout=20)
        data = r.json()
        role = (data.get("result") or {}).get("role")
        return (role or None), (None if role else "expired")
    except requests.RequestException as e:
        log.debug("getCurrentUser 網路異常：%s", e)
        return None, "network"
    except Exception:
        return None, "expired"


def _hw_to_dict(hw) -> dict:
    t = getattr(hw, "type", None)
    return {
        "id": str(getattr(hw, "id", "") or ""),
        "title": getattr(hw, "title", "") or "",
        "type": (getattr(t, "name", "") or ""),
        "begin_time": int(getattr(hw, "begin_time", 0) or 0),
        "end_time": int(getattr(hw, "end_time", 0) or 0),
        "create_time": int(getattr(hw, "create_time", 0) or 0),
    }


# ---------------------------------------------------------------------------
# 提醒判斷邏輯
# ---------------------------------------------------------------------------

def _fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    days, rem = divmod(s, 86400)
    hours, rem2 = divmod(rem, 3600)
    mins = rem2 // 60
    if days:
        return f"{days}天{hours}小時"
    if hours:
        return f"{hours}小時{mins}分"
    return f"{mins}分鐘"


def _fmt_hw(h: dict) -> str:
    line = f"「{h['title']}」"
    if h["end_time"] > 0:
        line += f"（{_fmt_time(h['end_time'])} 截止）"
    return line


def repeat_due_items(hws: list[dict], st: dict, ncfg: dict, now: float | None = None):
    """按用戶設置的提醒節奏序列，計算需要重複提醒的作業。
    remind_schedule 例 [300,180,120,60,30,10]：提醒後隔5h、再3h、再2h、再1h、再30min，
    之後每10min循環提醒，直到完成。返回 [(作業, 已提醒次數), ...]"""
    try:
        schedule = [int(x) for x in (ncfg.get("remind_schedule") or []) if float(x) > 0]
    except (TypeError, ValueError):
        schedule = []
    if not schedule:
        return []
    track = st.get("remind_track") or {}
    now = now if now is not None else time.time()
    due = []
    for h in hws:
        t = track.get(h["id"])
        if not t or not h["id"]:
            continue
        end = h["end_time"]
        if 0 < end <= now:  # 已過期不重複提醒
            continue
        idx = int(t.get("count", 0))
        gap = schedule[idx] if idx < len(schedule) else schedule[-1]
        if now - float(t.get("last", 0)) >= gap * 60:
            due.append((h, idx + 1))
    return due


def diff_and_notify(label: str, hws: list[dict], st: dict, ncfg: dict, notify) -> list[str]:
    """對比狀態判斷需要發送的提醒，更新狀態，返回發送的消息文本列表"""
    msgs: list[str] = []
    now = time.time()
    mode = ncfg.get("mode", "new_only")
    warn_hours = float(ncfg.get("deadline_warn_hours", 24) or 0)
    seen = set(st.get("known_unfinished", []))
    new = [h for h in hws if h["id"] and h["id"] not in seen]
    initialized = bool(st.get("initialized"))
    dw = st.setdefault("deadline_warned", {})
    track = st.setdefault("remind_track", {})  # 重複提醒追蹤 {作業id: {count, last}}

    def urgent_of(h: dict):
        """返回 ('soon', 剩餘秒數) 表示即將截止；逾期不提醒（僅狀態頁顯示）"""
        end = h["end_time"]
        if end <= 0 or not h["id"] or warn_hours <= 0:
            return None
        remain = end - now
        if remain <= 0:
            return None
        if remain <= warn_hours * 3600:
            return ("soon", remain)
        return None

    def inventory_msg(prefix: str, items: list[dict]) -> str:
        head = f"{prefix}：目前有 {len(items)} 項未完成作業"
        return head + "：" + "；".join(_fmt_hw(h) for h in items[:3]) + ("等" if len(items) > 3 else "")

    if not initialized:
        # 首次見到該賬號：記錄基線。存量作業不逐一彈窗，只發一條「即將截止」匯總
        st["initialized"] = True
        urgent = []
        for h in hws:
            u = urgent_of(h)
            if u:
                urgent.append(h)
                dw[h["id"]] = now
                track[h["id"]] = {"count": 0, "last": now}
        if ncfg.get("remind_on_first_run", False) and hws:
            msgs.append(inventory_msg(label, hws))
        elif urgent:
            parts = [f"「{h['title']}」將於 {_fmt_time(h['end_time'])} 截止" for h in urgent[:3]]
            msgs.append(f"⏰ {label}｜有 {len(urgent)} 項作業即將截止：" + "；".join(parts)
                        + ("等" if len(urgent) > 3 else ""))
    else:
        if mode == "every":
            if hws:
                msgs.append(inventory_msg(f"📋 {label}", hws))
        elif new:
            msgs.append(f"🆕 {label}｜有 {len(new)} 項新作業："
                        + "；".join(_fmt_hw(h) for h in new[:3]) + ("等" if len(new) > 3 else ""))
            for h in new:  # 新作業開始納入重複提醒追蹤
                track[h["id"]] = {"count": 0, "last": now}
        if warn_hours > 0:
            for h in hws:
                u = urgent_of(h)
                if u and h["id"] not in dw:
                    msgs.append(f"⏰ {label}｜作業即將截止：「{h['title']}」，"
                                f"將於 {_fmt_time(h['end_time'])} 截止，剩餘 {_fmt_dur(u[1])}")
                    dw[h["id"]] = now
                    track[h["id"]] = {"count": 0, "last": now}  # 緊急提醒重置節奏

    for k in [k for k, ts in dw.items() if now - ts > 7 * 86400]:
        dw.pop(k, None)

    st["known_unfinished"] = [h["id"] for h in hws if h["id"]][-300:]
    st.pop("notified_new", None)  # 舊版本字段，已改為僅用上一輪快照判斷新作業
    st.pop("overdue_warned", None)  # 已取消逾期提醒
    live_ids = {h["id"] for h in hws if h["id"]}
    for k in [k for k in track if k not in live_ids]:
        track.pop(k, None)  # 已完成/消失的作業停止重複提醒
    return msgs


# ---------------------------------------------------------------------------
# 單賬號檢查
# ---------------------------------------------------------------------------

def _is_configured(acc: dict) -> bool:
    """賬號密碼都已填寫（排除示例配置里的佔位文字）"""
    u = (acc.get("username") or "").strip()
    p = (acc.get("password") or "").strip()
    if not u or not p:
        return False
    return "填" not in u and "填" not in p


def check_account(acc: dict, cfg: dict, st: dict, notify) -> dict:
    label = (acc.get("label") or acc.get("username") or "未命名賬號").strip()
    username = (acc.get("username") or "").strip()
    password = (acc.get("password") or "").strip()
    res = {
        "label": label, "name": "", "role": "", "status": "ok", "error": "",
        "homeworks": [], "checked_at": time.time(),
    }
    if acc.get("enabled") is False:
        res["status"] = "disabled"
        return res
    if not _is_configured(acc):
        res["status"] = "unconfigured"
        return res

    _, get_account_role, session_to_account = _import_zxw()

    def finish_need_login(reason: str):
        res["status"] = "need_login"
        res["error"] = reason
        last = st.get("last_need_login_toast", 0)
        if time.time() - last > NEED_LOGIN_TOAST_INTERVAL:
            st["last_need_login_toast"] = time.time()
            if sys.platform == "win32":
                hint = "請雙擊 login_refresh.bat 重新登入（會打開瀏覽器）。"
            else:
                hint = ("請重新導入憑證：python3 monitor.py --import-cookie「%s」\"Cookie字串\""
                        "（獲取方法見 README）" % label)
            notify("🔑 智學網需要重新登入", f"賬號「{label}」{reason}。{hint}", touser=acc.get("owner"))

    cookies = load_cookies(label)
    session = None
    if cookies:
        session = build_session(cookies)
        role, err = validate_session(session)
        if err == "network":
            res["status"] = "network"
            res["error"] = "網路連線失敗（稍後自動重試）"
            return res
        if role is None:
            log.info("「%s」的登入憑證已失效，需要重新登入", label)
            session = None

    if session is None:
        if username and password and cfg.get("auto_relogin", False):
            if sys.platform != "win32":
                log.warning("「%s」憑證已失效：服務器上無法彈出瀏覽器登入，請用 --import-cookie 重新導入", label)
            else:
                log.info("「%s」正在自動彈出瀏覽器重新登入……", label)
                try:
                    cookies = playwright_login(username, password)
                    session = build_session(cookies)
                    save_cookies(label, session)
                except Exception as e:
                    log.error("「%s」自動登入失敗：%s", label, e)
        if session is None:
            finish_need_login("登入憑證已失效" if cookies else "還未登入過")
            return res

    try:
        role = get_account_role(session)
        account = session_to_account(session)
    except requests.RequestException as e:
        res["status"] = "network"
        res["error"] = f"網路請求失敗：{e.__class__.__name__}"
        return res
    except Exception as e:
        res["status"] = "error"
        res["error"] = f"獲取賬戶信息失敗：{e.__class__.__name__}: {e}"
        log.error("「%s」%s", label, res["error"])
        return res

    res["role"] = role
    res["name"] = getattr(account, "name", "") or ""

    try:
        size = int(cfg.get("homework_page_size", 50) or 50)
        raw = account.get_homeworks(size=size, is_complete=False)
        hws = [_hw_to_dict(h) for h in raw]
    except requests.RequestException as e:
        res["status"] = "network"
        res["error"] = f"獲取作業失敗（網路）：{e.__class__.__name__}"
        return res
    except Exception as e:
        res["status"] = "error"
        res["error"] = f"獲取作業失敗：{e.__class__.__name__}: {e}"
        log.error("「%s」%s", label, res["error"])
        return res

    hws.sort(key=lambda h: (h["end_time"] == 0, h["end_time"]))
    res["homeworks"] = hws
    log.info("「%s」%s（%s）：未完成作業 %d 項", label, res["name"] or "-", role, len(hws))

    msgs = diff_and_notify(label, hws, st, cfg.get("notify", {}), notify)
    for m in msgs:
        notify(NOTIFY_TITLE, m, touser=acc.get("owner"))

    # 重複提醒：未完成的作業按用戶設置的節奏反覆提醒；
    # 發送前先重新核實一遍（重新拉取作業列表），確認仍未完成才發，核實失敗則本輪不發
    ncfg_eff = cfg["notify"]
    if "remind_schedule" in acc and acc.get("remind_schedule") is not None:
        ncfg_eff = dict(ncfg_eff, remind_schedule=acc["remind_schedule"])  # 賬號級覆蓋
    rep = repeat_due_items(hws, st, ncfg_eff)
    if rep and ncfg_eff.get("verify_before_send", True):
        try:
            fresh = account.get_homeworks(size=size, is_complete=False)
            fresh_ids = {str(getattr(x, "id", "") or "") for x in fresh}
            before = len(rep)
            rep = [item for item in rep if item[0]["id"] in fresh_ids]
            if len(rep) < before:
                log.info("「%s」發送前核實：%d 項已完成/消失，取消提醒", label, before - len(rep))
        except Exception as e:
            log.warning("「%s」發送前核實失敗，本輪重複提醒不發送：%s", label, e.__class__.__name__)
            rep = []
    if rep:
        now = time.time()
        lines = [f"🔁 {label}｜以下作業仍未完成，記得提醒孩子完成："]
        for h, times in rep[:6]:
            ddl = f"（{_fmt_time(h['end_time'])} 截止）" if h["end_time"] else ""
            lines.append(f"· {h['title']}{ddl}｜已提醒 {times} 次")
        if len(rep) > 6:
            lines.append(f"…等共 {len(rep)} 項")
        notify(NOTIFY_TITLE, "\n".join(lines), touser=acc.get("owner"))
        track = st.setdefault("remind_track", {})
        for h, times in rep:
            track[h["id"]] = {"count": times, "last": now}
    return res


# ---------------------------------------------------------------------------
# 狀態總覽頁
# ---------------------------------------------------------------------------

STATUS_TMPL = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="300">
<title>智學網作業監控 · 狀態總覽</title>
<style>
  :root { --bg:#f4f6fa; --card:#fff; --line:#e5e9f0; --text:#24292f; --muted:#6a737d;
          --red:#d73a49; --orange:#e36209; --green:#22863a; --blue:#0366d6; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px; background:var(--bg); color:var(--text);
         font-family:"Microsoft YaHei","PingFang TC",system-ui,sans-serif; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:var(--muted); font-size:13px; margin-bottom:20px; }
  .summary { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:20px; }
  .chip { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:10px 16px; font-size:14px; }
  .chip b { font-size:20px; margin-left:6px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:16px 20px; margin-bottom:16px; }
  .card h2 { font-size:17px; margin:0 0 2px; }
  .meta { color:var(--muted); font-size:12px; margin-bottom:10px; }
  .status-ok { color:var(--green); } .status-warn { color:var(--orange); }
  .status-err { color:var(--red); }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th, td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; font-size:12px; }
  tr:last-child td { border-bottom:none; }
  .overdue { color:var(--red); font-weight:600; }
  .soon { color:var(--orange); font-weight:600; }
  .ok { color:var(--green); }
  .empty { color:var(--green); padding:10px 0; }
  .badge { display:inline-block; border-radius:6px; font-size:12px; padding:1px 8px;
           background:#eef2f7; color:var(--muted); margin-left:8px; vertical-align:2px; }
</style>
</head>
<body>
<h1>智學網作業監控 · 狀態總覽</h1>
<div class="sub">生成時間：__GEN_TIME__ ｜ 頁面每 5 分鐘自動刷新；監控程式每次檢查後也會更新此頁</div>
<div class="summary">__SUMMARY__</div>
__CARDS__
</body>
</html>
"""

STATUS_LABELS = {
    "ok": ('<span class="status-ok">✔ 正常</span>', "status-ok"),
    "need_login": ('<span class="status-warn">⚠ 需要重新登入（請雙擊 login_refresh.bat）</span>', "status-warn"),
    "network": ('<span class="status-warn">⚠ 網路異常，稍後自動重試</span>', "status-warn"),
    "error": ('<span class="status-err">✖ {error}</span>', "status-err"),
    "unconfigured": ('<span class="status-warn">⚠ 尚未配置賬號或密碼</span>', "status-warn"),
    "disabled": ('<span class="status-err">已停用</span>', "status-warn"),
}

ROLE_LABELS = {"student": "學生賬號", "parent": "家長賬號", "teacher": "教師賬號"}


def render_status(results: list[dict]) -> str:
    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_unfinished = sum(len(r["homeworks"]) for r in results if r["status"] == "ok")
    accounts_ok = sum(1 for r in results if r["status"] == "ok")
    summary = (
        f'<div class="chip">賬號<b>{len(results)}</b></div>'
        f'<div class="chip">正常<b>{accounts_ok}</b></div>'
        f'<div class="chip">未完成作業合計<b>{total_unfinished}</b></div>'
    )
    cards = []
    for r in results:
        role_txt = ROLE_LABELS.get(r.get("role"), r.get("role") or "")
        who = r["label"] + (f"（{r['name']}）" if r.get("name") and r["name"] != r["label"] else "")
        checked = datetime.fromtimestamp(r.get("checked_at", time.time())).strftime("%H:%M:%S")
        st_tmpl = STATUS_LABELS.get(r["status"], STATUS_LABELS["error"])[0]
        st_html = st_tmpl.format(error=html.escape(r.get("error", "")))
        head = (f'<div class="card"><h2>{html.escape(who)}'
                f'<span class="badge">{role_txt or "未知的賬號類型"}</span></h2>'
                f'<div class="meta">上次檢查：{checked} ｜ 狀態：{st_html}</div>')
        if r["status"] != "ok":
            cards.append(head + "</div>")
            continue
        if not r["homeworks"]:
            cards.append(head + '<div class="empty">🎉 太棒了，目前沒有未完成的作業！</div></div>')
            continue
        rows = []
        for h in r["homeworks"]:
            end = h["end_time"]
            if end <= 0:
                remain, cls = "無截止時間", ""
            else:
                d = end - time.time()
                if d <= 0:
                    remain, cls = f"已逾期 {_fmt_dur(-d)}", "overdue"
                elif d <= 86400:
                    remain, cls = f"剩餘 {_fmt_dur(d)}", "soon"
                else:
                    remain, cls = f"剩餘 {_fmt_dur(d)}", "ok"
            rows.append(
                "<tr><td>{title}</td><td>{type}</td><td>{create}</td><td>{end}</td><td class=\"{cls}\">{remain}</td></tr>".format(
                    title=html.escape(h["title"] or "（無標題）"),
                    type=html.escape(h["type"] or "—"),
                    create=_fmt_time(h["create_time"]) if h["create_time"] else "—",
                    end=_fmt_time(end) if end else "—",
                    cls=cls, remain=remain,
                ))
        cards.append(
            head + "<table><tr><th>作業標題</th><th>類型</th><th>佈置時間</th><th>截止時間</th><th>狀態</th></tr>"
            + "".join(rows) + "</table></div>")
    return (STATUS_TMPL
            .replace("__GEN_TIME__", gen_time)
            .replace("__SUMMARY__", summary)
            .replace("__CARDS__", "".join(cards)))


def write_status(results: list[dict]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(render_status(results), encoding="utf-8")


# ---------------------------------------------------------------------------
# 配置與狀態
# ---------------------------------------------------------------------------

def reload_config_quiet() -> dict | None:
    """讀取並規範化 config.json；失敗返回 None（不清空上一份好配置）。
    accounts 允許為空列表（服務器純微信機器人模式，用戶之後通過微信綁定）"""
    cfg = load_json(CONFIG_PATH, None)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("accounts"), list):
        return None
    raw_notify = cfg.get("notify") or {}
    ncfg = dict(EXAMPLE_CONFIG["notify"])  # 預置全部新鍵的默認值
    ncfg.update(raw_notify)
    for old, new in LEGACY_NOTIFY_KEYS.items():  # 舊版鍵名自動遷移
        if old in ncfg and new not in raw_notify:
            ncfg[new] = ncfg.pop(old)
    cfg["notify"] = ncfg
    cfg.setdefault("check_interval_minutes", 30)
    cfg.setdefault("homework_page_size", 50)
    cfg.setdefault("auto_relogin", False)
    cfg.setdefault("wecom_bot", {})
    return cfg


def load_or_create_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(
            json.dumps(EXAMPLE_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        msg = (f"已生成示例配置文件：{CONFIG_PATH}\n"
               "請用記事本打開，在 accounts 中填入智學網的賬號和密碼後保存，然後重新運行。")
        log.info(msg)
        try:
            os.startfile(CONFIG_PATH)
        except Exception:
            pass
        raise SystemExit(0)
    cfg = reload_config_quiet()
    if cfg is None:
        log.error("config.json 內容無效或沒有 accounts，請檢查：%s", CONFIG_PATH)
        raise SystemExit(1)
    return cfg


def load_state() -> dict:
    st = load_json(STATE_PATH, {})
    return st if isinstance(st, dict) else {}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def account_due(acc: dict, cfg: dict, st: dict, now: float | None = None) -> bool:
    """該賬號是否到了該檢查的時間（賬號可單獨設 check_interval_minutes）"""
    if acc.get("enabled") is False:
        return False
    iv = float(acc.get("check_interval_minutes") or cfg.get("check_interval_minutes") or 30)
    iv = max(iv, 1.0)
    last = st.get("last_check", 0)
    return (now if now is not None else time.time()) - last >= iv * 60


def check_all(cfg: dict, state: dict, only_due: bool = False,
              labels: list[str] | None = None) -> list[dict]:
    notify = make_notifier(cfg["notify"])
    now = time.time()
    results = []
    for acc in cfg["accounts"]:
        label = (acc.get("label") or acc.get("username") or "未命名賬號").strip()
        if labels and label not in labels:
            continue
        st = state.setdefault("accounts", {}).setdefault(label, {})
        if only_due and not account_due(acc, cfg, st, now):
            continue
        try:
            r = check_account(acc, cfg, st, notify)
        except Exception as e:
            log.exception("檢查「%s」時發生未預期的錯誤", label)
            r = {"label": label, "name": "", "role": "", "status": "error",
                 "error": f"未預期錯誤：{e}", "homeworks": [], "checked_at": time.time()}
        st["last_check"] = r["checked_at"]
        st["last_status"] = r["status"]
        st["last_error"] = r["error"]
        st["name"] = r["name"]
        st["role"] = r["role"]
        st["last_homeworks"] = r["homeworks"]
        results.append(r)
    state.setdefault("accounts", {})
    save_json(STATE_PATH, state)
    if results:
        write_status(state_to_results(state, results) if only_due else results)
    log.info("本輪檢查完成（%d 個賬號），狀態頁已更新", len(results))
    return results


def state_to_results(state: dict, results: list[dict]) -> list[dict]:
    """only_due 模式下用舊狀態補齊未檢查的賬號，讓狀態頁始終顯示全部賬號"""
    merged = {r["label"]: r for r in results}
    out = []
    for label, st in state.get("accounts", {}).items():
        if label in merged:
            out.append(merged[label])
            continue
        out.append({"label": label, "name": st.get("name", ""), "role": st.get("role", ""),
                    "status": st.get("last_status", "error"), "error": st.get("last_error", ""),
                    "homeworks": st.get("last_homeworks") or [],
                    "checked_at": st.get("last_check", 0)})
    return out


def run_loop(cfg: dict, override_interval: float | None) -> None:
    interval_min = float(override_interval or cfg.get("check_interval_minutes", 30))
    if interval_min < 1:
        interval_min = 1
    bot_enabled = bool((cfg.get("wecom_bot") or {}).get("enabled"))
    if bot_enabled:
        log.info("微信機器人已啟用：每分鐘喚醒，按各賬號的間隔到期檢查（配置可熱更新）")
    else:
        log.info("開始常駐監控，每 %.0f 分鐘檢查一次（Ctrl+C 退出）", interval_min)
    while True:
        t0 = time.time()
        try:
            cur = (reload_config_quiet() or cfg) if bot_enabled else cfg
            check_all(cur, load_state(), only_due=bot_enabled)
        except SystemExit:
            raise
        except Exception:
            log.exception("本輪檢查出錯，將在下一輪重試")
        if bot_enabled:
            wait = 60.0
        else:
            wait = max(60.0, interval_min * 60 - (time.time() - t0))
        next_t = datetime.fromtimestamp(time.time() + wait).strftime("%H:%M:%S")
        log.info("下次檢查：%s", next_t)
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            log.info("已手動停止監控")
            return


def do_relogin(cfg: dict, only_label: str | None) -> None:
    targets = []
    for acc in cfg["accounts"]:
        label = (acc.get("label") or acc.get("username") or "未命名賬號").strip()
        if only_label and only_label not in ("all", label):
            continue
        targets.append((acc, label))
    if not targets:
        log.error("沒有匹配的賬號：%s", only_label)
        return
    state = load_state()
    ok_labels = []
    for acc, label in targets:
        if not _is_configured(acc):
            log.warning("「%s」未配置賬號或密碼，跳過。請先編輯 config.json", label)
            continue
        log.info("正在為「%s」打開瀏覽器登入視窗……（若出現「極驗封禁」請點擊「請點擊重試」，"
                 "仍失敗就關掉視窗，改用 README 裡的手動導入 Cookie 方法）", label)
        try:
            cookies = playwright_login(acc["username"].strip(), acc["password"].strip())
            session = build_session(cookies)
            save_cookies(label, session)
            ok_labels.append(label)
            state.setdefault("accounts", {}).setdefault(label, {}).pop("last_need_login_toast", None)
            log.info("✅「%s」登入成功，憑證已保存", label)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.error("❌「%s」登入失敗：%s", label, e)
    save_json(STATE_PATH, state)
    if ok_labels:
        log.info("重新登入流程結束，正在執行一次完整檢查……")
        check_all(cfg, state)


def run_demo() -> None:
    now = time.time()
    results = [
        {"label": "示例-小明", "name": "小明", "role": "student", "status": "ok", "error": "",
         "checked_at": now, "homeworks": [
             {"id": "d1", "title": "數學《一元二次方程》練習卷", "type": "自由出題",
              "begin_time": now - 86400 * 2, "end_time": now + 3600 * 5, "create_time": now - 86400 * 2},
             {"id": "d2", "title": "語文第3課背誦打卡", "type": "朗讀作業",
              "begin_time": now - 86400, "end_time": now + 86400 * 2, "create_time": now - 86400},
             {"id": "d3", "title": "英語 Unit2 單詞默寫", "type": "自由出題",
              "begin_time": now - 86400 * 5, "end_time": now - 3600 * 6, "create_time": now - 86400 * 5},
         ]},
        {"label": "示例-小紅", "name": "小紅", "role": "parent", "status": "ok", "error": "",
         "checked_at": now, "homeworks": []},
        {"label": "示例-未登入", "name": "", "role": "", "status": "need_login",
         "error": "登入憑證已失效", "checked_at": now, "homeworks": []},
    ]
    write_status(results)
    log.info("已生成演示狀態頁：%s", STATUS_PATH)
    webbrowser.open(STATUS_PATH.as_uri())


# ---------------------------------------------------------------------------
# 微信機器人業務邏輯（供 wecom_bot.py 通過 ctx 調用）
# ---------------------------------------------------------------------------

_CFG_LOCK = threading.Lock()


def _parse_cookie_string(cookie_str: str) -> dict:
    cookies = {}
    for part in cookie_str.replace("\n", " ").split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()
    return cookies


def _label_of(acc: dict) -> str:
    return (acc.get("label") or acc.get("username") or "未命名賬號").strip()


def make_bot_context() -> dict:
    """把賬號登記/登入/檢查等業務函數打包給微信機器人"""

    def find_account(label: str):
        cfg = reload_config_quiet()
        if not cfg:
            return None, None
        for acc in cfg["accounts"]:
            if _label_of(acc) == label:
                return cfg, acc
        return cfg, None

    def is_admin(user: str) -> bool:
        return user in ((reload_config_quiet() or {}).get("wecom_bot") or {}).get("admins", [])

    def register(user: str, label: str, username: str, password: str, admin: bool):
        if not label:
            label = f"賬號{username[-4:]}" if len(username) >= 4 else username
        with _CFG_LOCK:
            cfg = reload_config_quiet()
            if not cfg:
                return False, "❌ 配置文件讀取失敗，請聯繫管理員"
            for acc in cfg["accounts"]:
                if _label_of(acc) == label:
                    if acc.get("owner") not in (None, "", user) and not admin:
                        return False, f"❌ 賬號標籤「{label}」已被其他人綁定，請換一個備注"
                    acc.update({"label": label, "username": username, "password": password,
                                "enabled": True, "owner": user})
                    break
            else:
                cfg["accounts"].append({"label": label, "username": username, "password": password,
                                        "enabled": True, "owner": user})
            save_json(CONFIG_PATH, cfg)
        return True, label

    def login_and_save(label: str, username: str, password: str):
        try:
            cookies = playwright_login(username, password, headless=True,
                                       auto_submit=True, wait_timeout=75)
        except Exception as e:
            reason = str(e)
            if "playwright" in reason.lower() and "executable" in reason.lower():
                return False, "服務器還沒安裝登入瀏覽器（playwright chromium）"
            return False, f"自動登入未成功（{reason[:80]}）"
        session = build_session(cookies)
        role, err = validate_session(session)
        if err == "network":
            return False, "網路異常"
        if role is None:
            return False, "登入未生效（大概率被智學網風控/驗證碼攔截）"
        save_cookies(label, session)
        state = load_state()
        state.setdefault("accounts", {}).setdefault(label, {}).pop("last_need_login_toast", None)
        save_json(STATE_PATH, state)
        return True, f"登入成功（{ROLE_LABELS.get(role, role)}）"

    def import_cookie(user: str, label: str, cookie_str: str, admin: bool):
        _, acc = find_account(label)
        if acc is None:
            return False, f"❌ 找不到賬號「{label}」，先發「綁定」指令登記，或檢查標籤寫法"
        if acc.get("owner") not in (None, "", user) and not admin:
            return False, "❌ 這不是你綁定的賬號"
        cookies = _parse_cookie_string(cookie_str)
        if not cookies:
            return False, "❌ Cookie 內容解析失敗，請複製完整的「k=v; k=v; …」字符串"
        session = build_session(cookies)
        role, err = validate_session(session)
        if err == "network":
            return False, "❌ 網路異常，請稍後重試"
        if role is None:
            return False, "❌ Cookie 無效或已過期，請確認是「已登入智學網」的瀏覽器裡複製的完整內容"
        save_cookies(label, session)
        state = load_state()
        state.setdefault("accounts", {}).setdefault(label, {}).pop("last_need_login_toast", None)
        save_json(STATE_PATH, state)
        return True, f"「{label}」憑證導入成功（{ROLE_LABELS.get(role, role)}）"

    def set_interval(user: str, minutes: int, label: str, admin: bool):
        with _CFG_LOCK:
            cfg = reload_config_quiet()
            if not cfg:
                return "❌ 配置文件讀取失敗"
            changed = []
            for acc in cfg["accounts"]:
                lbl = _label_of(acc)
                if label and lbl != label:
                    continue
                if acc.get("owner") not in (None, "", user) and not admin:
                    continue
                acc["check_interval_minutes"] = minutes
                changed.append(lbl)
            if not changed:
                return f"❌ 沒有找到你可管理的賬號「{label}」" if label else "❌ 你還沒綁定任何賬號"
            save_json(CONFIG_PATH, cfg)
        return f"✅ 已把「{'、'.join(changed)}」的檢測間隔設為 {minutes} 分鐘"

    def set_remind(user: str, label: str, spec: str, admin: bool):
        """設置重複提醒節奏；spec 形如「5h,3h,2h,1h,30m,10m」，關/关/off 為關閉"""
        def fmt_schedule(schedule):
            return "、".join((f"{m // 60}h" if m % 60 == 0 else f"{m}min") for m in schedule)

        if spec.strip().lower() in ("關", "关", "off", "0", "無", "无"):
            schedule = []
        else:
            out = []
            for s in [x for x in re.split(r"[,，、\s]+", spec.strip()) if x]:
                m = re.fullmatch(r"(\d+(?:\.\d+)?)(h|小时|小時|m|min|分钟|分鐘|分)?", s.lower())
                if not m:
                    return f"❌ 看不懂「{s}」，請用 5h、30m、10 這樣的寫法（h=小時，m=分鐘）"
                minutes = int(float(m.group(1)) * 60) if m.group(2) == "h" else int(float(m.group(1)))
                if minutes < 5:
                    return "❌ 最小間隔為 5 分鐘"
                out.append(minutes)
            if not out or len(out) > 10:
                return "❌ 請提供 1~10 個間隔"
            schedule = out
        with _CFG_LOCK:
            cfg = reload_config_quiet()
            if not cfg:
                return "❌ 配置文件讀取失敗"
            changed = []
            for acc in cfg["accounts"]:
                lbl = _label_of(acc)
                if label and lbl != label:
                    continue
                if acc.get("owner") not in (None, "", user) and not admin:
                    continue
                acc["remind_schedule"] = schedule
                changed.append(lbl)
            if not changed:
                return f"❌ 沒有找到你可管理的賬號「{label}」" if label else "❌ 你還沒綁定任何賬號"
            save_json(CONFIG_PATH, cfg)
        rhythm = fmt_schedule(schedule) if schedule else "已關閉（每項作業只提醒一次）"
        return (f"✅「{'、'.join(changed)}」重複提醒節奏：{rhythm}\n"
                f"（發送前會自動重新核實作業仍未完成，未核實成功不發送）")

    def unbind(user: str, label: str, admin: bool):
        with _CFG_LOCK:
            cfg = reload_config_quiet()
            if not cfg:
                return "❌ 配置文件讀取失敗"
            for acc in cfg["accounts"]:
                if _label_of(acc) == label:
                    if acc.get("owner") not in (None, "", user) and not admin:
                        return "❌ 這不是你綁定的賬號"
                    acc["enabled"] = False
                    acc.pop("password", None)
                    save_json(CONFIG_PATH, cfg)
                    return f"✅ 已解綁「{label}」（賬號已停用並清除密碼）"
        return f"❌ 找不到賬號「{label}」"

    def check_access(user: str, label: str, admin: bool):
        """返回 (ok, label)；label 為空代表默認取該用戶的第一個賬號"""
        _, acc = find_account(label) if label else (None, None)
        if acc is None and not label:
            for a in (reload_config_quiet() or {}).get("accounts", []):
                if a.get("owner") in (None, "", user):
                    acc = a
                    break
        if acc is None:
            return False, f"❌ 找不到你可用的賬號「{label}」" if label else "❌ 你還沒綁定任何賬號"
        if acc.get("owner") not in (None, "", user) and not admin:
            return False, "❌ 這不是你綁定的賬號"
        return True, _label_of(acc)

    def check_now(user: str, label: str) -> str:
        cfg = reload_config_quiet()
        if not cfg:
            return "❌ 配置文件讀取失敗"
        acc = next((a for a in cfg["accounts"] if _label_of(a) == label), None)
        if acc is None:
            return f"❌ 找不到賬號「{label}」"
        state = load_state()
        st = state.setdefault("accounts", {}).setdefault(label, {})
        r = check_account(acc, cfg, st, lambda t, b, touser=None: None)  # 靜默檢查，避免打擾其他渠道
        save_json(STATE_PATH, state)
        if r["status"] == "ok":
            if not r["homeworks"]:
                return f"🎉「{label}」（{r['name'] or label}）目前沒有未完成的作業！"
            lines = [f"📋「{label}」（{r['name'] or label}）未完成作業 {len(r['homeworks'])} 項："]
            for h in r["homeworks"][:5]:
                ddl = f"（{_fmt_time(h['end_time'])} 截止）" if h["end_time"] else ""
                lines.append(f"· {h['title']}{ddl}")
            if len(r["homeworks"]) > 5:
                lines.append(f"…等共 {len(r['homeworks'])} 項（詳見狀態頁）")
            return "\n".join(lines)
        return f"⚠️「{label}」檢查未成功：{r['error'] or r['status']}"

    def summary_for(user: str) -> str:
        cfg = reload_config_quiet()
        state = load_state().get("accounts", {})
        mine = [a for a in (cfg or {}).get("accounts", [])
                if a.get("owner") in (None, "", user) and a.get("enabled", True)]
        if not mine:
            return "你還沒有綁定賬號。發送：綁定 賬號 密碼 [備注]"
        lines = ["📋 你綁定的賬號："]
        for acc in mine:
            label = _label_of(acc)
            st = state.get(label, {})
            n = len(st.get("last_homeworks") or [])
            status = {"ok": "✔ 正常", "need_login": "⚠ 需重新登入",
                      "network": "⚠ 網路異常"}.get(st.get("last_status"), "尚未檢查")
            iv = acc.get("check_interval_minutes") or (cfg or {}).get("check_interval_minutes", 30)
            lines.append(f"\n· {label}（{st.get('name') or '未知姓名'}）\n"
                         f"  狀態：{status}｜未完成：{n} 項｜間隔：{iv} 分鐘")
        return "\n".join(lines)

    return {"register": register, "login_and_save": login_and_save,
            "import_cookie": import_cookie, "set_interval": set_interval,
            "set_remind": set_remind,
            "unbind": unbind, "check_access": check_access,
            "check_now": check_now, "summary_for": summary_for}


def notify_test_all(cfg: dict) -> None:
    """向所有已配置渠道發送測試消息"""
    notify = make_notifier(cfg["notify"])
    notify("🧪 智學網監控測試", "這是一條測試消息。看到即說明該渠道配置成功 ✅")
    log.info("已向所有已配置渠道發送測試消息，請檢查各渠道是否收到（結果見上方日誌）")


def main() -> None:
    parser = argparse.ArgumentParser(description="智學網作業多賬號自動監控提醒")
    parser.add_argument("--once", action="store_true", help="只檢查一次後退出")
    parser.add_argument("--interval", type=float, default=None, metavar="分鐘",
                        help="臨時覆蓋檢查間隔（分鐘）")
    parser.add_argument("--relogin", nargs="?", const="all", metavar="LABEL",
                        help="彈出瀏覽器重新登入（可指定賬號 label）")
    parser.add_argument("--import-cookie", nargs=2, metavar=("LABEL", "COOKIE"),
                        help="手動導入登入憑證：把瀏覽器裡 zhixue.com 的 Cookie 字串導入指定賬號")
    parser.add_argument("--test-notify", action="store_true", help="發送桌面彈窗測試")
    parser.add_argument("--notify-test", action="store_true", help="向所有已配置的推送渠道發送測試消息")
    parser.add_argument("--demo", action="store_true", help="用模擬數據生成狀態頁預覽")
    parser.add_argument("--open-status", action="store_true", help="打開狀態總覽頁")
    parser.add_argument("--gen-config", action="store_true", help="生成示例配置文件")
    args = parser.parse_args()

    setup_logging()

    if args.test_notify:
        ok = send_toast("🧪 Windows 彈窗測試",
                        "這是一條桌面彈窗測試。想測試微信等渠道，請用 --notify-test", sound=True)
        log.info("桌面彈窗%s", "發送成功" if ok else "發送失敗（詳見日誌）")
        return
    if args.demo:
        run_demo()
        return
    if args.import_cookie:
        label, cookie_str = args.import_cookie
        cookies = {}
        for part in cookie_str.split(";"):
            if "=" in part:
                k, _, v = part.partition("=")
                cookies[k.strip()] = v.strip()
        if not cookies:
            log.error("Cookie 字串解析失敗，格式應類似「k1=v1; k2=v2」")
            return
        session = build_session(cookies)
        role, err = validate_session(session)
        if err == "network":
            log.error("❌ 網路異常，暫時無法驗證 Cookie，請稍後重試")
            return
        if role is None:
            log.error("❌ Cookie 無效或已過期。請在「已登入智學網」的瀏覽器裡重新複製完整的 Cookie 字串")
            return
        save_cookies(label, session)
        state = load_state()
        state.setdefault("accounts", {}).setdefault(label, {}).pop("last_need_login_toast", None)
        save_json(STATE_PATH, state)
        log.info("✅「%s」憑證導入成功（%s），現在可以正常監控了", label, ROLE_LABELS.get(role, role))
        return
    if args.gen_config:
        CONFIG_PATH.write_text(
            json.dumps(EXAMPLE_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("已生成示例配置文件：%s", CONFIG_PATH)
        return
    if args.open_status:
        if STATUS_PATH.exists():
            webbrowser.open(STATUS_PATH.as_uri())
        else:
            log.info("狀態頁還不存在，請先運行一次檢查（monitor.py --once）")
        return

    cfg = load_or_create_config()
    if args.notify_test:
        notify_test_all(cfg)
        return
    bot_on = bool((cfg.get("wecom_bot") or {}).get("enabled"))
    if not any(a.get("enabled", True) and _is_configured(a) for a in cfg["accounts"]) and not bot_on:
        # 微信機器人模式下允許空賬號啟動（用戶之後在微信裡綁定）
        log.warning("config.json 中還沒有填寫任何賬號，請先編輯 %s", CONFIG_PATH)
        try:
            os.startfile(CONFIG_PATH)
        except Exception:
            pass
        raise SystemExit(1)

    if args.relogin:
        do_relogin(cfg, args.relogin)
        return

    if sys.platform != "win32" and not channels_configured(cfg["notify"]):
        log.warning("⚠️ 當前在非 Windows 環境且未配置任何通知渠道（微信/企業微信/釘釘/飛書/郵件），"
                    "提醒將只寫入日誌。請在 config.json 的 notify 中至少配置一個渠道")

    if (cfg.get("wecom_bot") or {}).get("enabled"):
        import wecom_bot
        try:
            wecom_bot.set_logger(log)
            wecom_bot.start_bot(cfg["wecom_bot"], make_bot_context(), logger=log)
        except SystemExit as e:
            log.error("%s", e)

    if args.once:
        check_all(cfg, load_state())
        return
    run_loop(cfg, args.interval)


if __name__ == "__main__":
    main()
