#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
企業微信自建應用機器人
======================
讓家長在**個人微信**裡完成一切操作（通過企業微信的「微信插件」，消息直達微信）：
    綁定 賬號 密碼 [備注]   → 登記賬號並嘗試後台自動登入
    cookie 賬號標籤 <字符串> → 手動導入憑證（自動登入被驗證碼攔截時的兜底）
    我的                    → 查看自己賬號的作業狀態
    間隔 60 [賬號標籤]       → 設置檢測間隔（分鐘）
    檢查 [賬號標籤]          → 立即檢查一次
    解綁 賬號標籤            → 停用賬號
    幫助                    → 指令列表

本模塊只負責：微信回調加解密、指令解析、主動推送。
賬號登記/登入/檢查等業務邏輯通過 ctx 由 monitor.py 注入。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import struct
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

log_bot = None  # 由 monitor 注入 logger


def set_logger(logger):
    global log_bot
    log_bot = logger


# ---------------------------------------------------------------------------
# 企業微信回調加解密（WXBizMsgCrypt 兼容實現）
# ---------------------------------------------------------------------------

class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, receive_id: str):
        self.token = token
        self.receive_id = receive_id
        self.key = base64.b64decode(encoding_aes_key + "=")
        if len(self.key) != 32:
            raise ValueError("EncodingAESKey 無效（應為43位字符串）")

    def signature(self, timestamp: str, nonce: str, encrypt: str) -> str:
        raw = "".join(sorted([self.token, timestamp, nonce, encrypt]))
        return hashlib.sha1(raw.encode()).hexdigest()

    def encrypt(self, plain: bytes) -> str:
        from Crypto.Cipher import AES
        data = os.urandom(16) + struct.pack(">I", len(plain)) + plain + self.receive_id.encode()
        pad = 32 - len(data) % 32
        data += bytes([pad]) * pad
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        return base64.b64encode(cipher.encrypt(data)).decode()

    def decrypt(self, encrypt_b64: str) -> tuple[str, str]:
        """返回 (明文, receiveid)"""
        from Crypto.Cipher import AES
        cipher = AES.new(self.key, AES.MODE_CBC, self.key[:16])
        data = cipher.decrypt(base64.b64decode(encrypt_b64))
        pad = data[-1]
        data = data[:-pad]
        msg_len = struct.unpack(">I", data[16:20])[0]
        return data[20:20 + msg_len].decode(), data[20 + msg_len:].decode()


# ---------------------------------------------------------------------------
# 企業微信 API 客戶端（主動推送）
# ---------------------------------------------------------------------------

class WeComClient:
    def __init__(self, corpid: str, secret: str):
        self.corpid = corpid
        self.secret = secret
        self._token = ""
        self._token_expires = 0.0
        self._lock = threading.Lock()

    def access_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expires - 120:
                return self._token
            r = requests.get("https://qyapi.weixin.qq.com/cgi-bin/gettoken",
                             params={"corpid": self.corpid, "corpsecret": self.secret},
                             timeout=15)
            data = r.json()
            if data.get("errcode") != 0:
                raise RuntimeError(f"獲取 access_token 失敗：{data}")
            self._token = data["access_token"]
            self._token_expires = time.time() + float(data.get("expires_in", 7200))
            return self._token

    def push_text(self, touser: str, content: str, agentid: int) -> bool:
        content = _clip_bytes(content, 2000)
        payload = {"touser": touser or "@all", "msgtype": "text",
                   "agentid": agentid, "text": {"content": content}}
        for attempt in (1, 2):  # token 過期自動重取一次
            r = requests.post("https://qyapi.weixin.qq.com/cgi-bin/message/send",
                              params={"access_token": self.access_token()},
                              json=payload, timeout=15)
            data = r.json()
            if data.get("errcode") in (40014, 42001):  # token 失效
                with self._lock:
                    self._token_expires = 0
                continue
            if data.get("errcode") != 0:
                raise RuntimeError(f"推送失敗：{data}")
            return True
        return False


def _clip_bytes(s: str, n: int) -> str:
    b = s.encode("utf-8")
    if len(b) <= n:
        return s
    return b[:n].decode("utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# 機器人主體
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📖 指令列表\n"
    "1️⃣ 綁定賬號：發送\n"
    "    綁定 賬號 密碼 [備注]\n"
    "   例：綁定 student001 abc123 小明\n"
    "   （若自動登入被驗證碼攔截，會引導你發 Cookie，一步即可完成）\n"
    "2️⃣ 手動導入憑證：發送\n"
    "    cookie 賬號標籤 Cookie字符串\n"
    "3️⃣ 我的 → 查看自己賬號的作業狀態\n"
    "4️⃣ 間隔 60 [賬號標籤] → 設置檢測間隔（分鐘，5~1440）\n"
    "5️⃣ 檢查 [賬號標籤] → 立即檢查一次\n"
    "6️⃣ 提醒 [賬號標籤] 節奏 → 未完成作業的重複提醒\n"
    "    例：提醒 5h,3h,2h,1h,30m,10m\n"
    "    　　提醒 小明 1h,30m\n"
    "    　　提醒 关（關閉重複提醒）\n"
    "    可選間隔：5m~5h（h=小時 m=分鐘），可組合多個遞進\n"
    "7️⃣ 解綁 賬號標籤\n"
    "💡 新作業/臨近截止會立即提醒；重複提醒發送前會自動核實仍未完成"
)

_CMD_SPLIT = re.compile(r"\s+")


class WeComBot:
    AGREEMENT = (
        "👋 歡迎使用智學管家！首次使用請先閱讀並同意以下約定：\n\n"
        "1️⃣ 你發送的智學網賬號密碼僅保存在部署者的服務器本地，僅用於登入智學網查詢作業狀態\n"
        "2️⃣ 系統只讀取作業/成績信息用於提醒，不會提交或修改任何數據\n"
        "3️⃣ 催提醒消息僅發送給你本人，請勿將收到的憑證信息轉發他人\n"
        "4️⃣ 如不再使用，可隨時發送「解綁」清除賬號密碼\n\n"
        "回覆「同意」開始使用"
    )

    def __init__(self, bot_cfg: dict, ctx: dict):
        self.cfg = bot_cfg
        self.ctx = ctx
        self.corpid = bot_cfg["corpid"]
        self.agentid = int(bot_cfg.get("agentid") or 0)
        self.crypto = WeComCrypto(bot_cfg["callback_token"],
                                  bot_cfg["encoding_aes_key"], self.corpid)
        self.client = WeComClient(self.corpid, bot_cfg["secret"])
        self.admins = set(bot_cfg.get("admins") or [])
        self._seen_msgids: list[str] = []
        self._lock = threading.Lock()
        base_dir = ctx.get("base_dir")
        self.users_path = (Path(base_dir) / "data" / "wecom_users.json") if base_dir \
            else Path("data") / "wecom_users.json"
        self._agreed = self._load_users()

    # ---------- 用戶協議狀態（持久化） ----------

    def _load_users(self) -> dict:
        try:
            data = json.loads(self.users_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            self._log("warning", "讀取用戶文件失敗：%s", e)
            return {}

    def _save_users(self) -> None:
        try:
            self.users_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.users_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._agreed, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.users_path)
        except Exception as e:
            self._log("warning", "保存用戶文件失敗：%s", e)

    def _has_agreed(self, user: str) -> bool:
        return user in self._agreed or user in self.admins

    # ---------- 微信回調：驗證 & 消息 ----------

    def handle_get(self, qs: dict) -> tuple[int, str]:
        try:
            msg_sig = qs["msg_signature"][0]
            ts = qs["timestamp"][0]
            nonce = qs["nonce"][0]
            echostr = qs["echostr"][0]
        except (KeyError, IndexError):
            return 400, "bad request"
        if self.crypto.signature(ts, nonce, echostr) != msg_sig:
            return 403, "signature mismatch"
        plain, receiveid = self.crypto.decrypt(echostr)
        if receiveid != self.corpid:
            self._log("warning", "回調驗證 receiveid 不匹配：%s", receiveid)
        return 200, plain

    def handle_post(self, qs: dict, body: bytes) -> tuple[int, str]:
        try:
            msg_sig = qs["msg_signature"][0]
            ts = qs["timestamp"][0]
            nonce = qs["nonce"][0]
            encrypt = ET.fromstring(body).findtext("Encrypt") or ""
        except (KeyError, IndexError, ET.ParseError) as e:
            self._log("warning", "回調解析失敗：%s", e)
            return 400, "bad request"
        if self.crypto.signature(ts, nonce, encrypt) != msg_sig:
            return 403, "signature mismatch"
        try:
            plain, _ = self.crypto.decrypt(encrypt)
            msg = ET.fromstring(plain)
        except Exception as e:
            self._log("warning", "消息解密失敗：%s", e)
            return 200, "success"

        from_user = msg.findtext("FromUserName") or ""
        content = (msg.findtext("Content") or "").strip()
        msg_id = msg.findtext("MsgId") or ""
        if not self._remember_msg(msg_id):  # 重複推送直接忽略
            return 200, "success"
        self._log("info", "收到 %s 的消息：%s", from_user, content[:50])

        reply = "✅ 收到，正在處理…"
        try:
            reply = self.dispatch(from_user, content)
        except Exception:
            self._log("error", "處理消息出錯：%s", traceback.format_exc())
            reply = "❌ 處理出錯，請稍後重試或聯繫管理員"
        return 200, self._reply_xml(from_user, nonce, reply)

    def _remember_msg(self, msg_id: str) -> bool:
        if not msg_id:
            return True
        with self._lock:
            if msg_id in self._seen_msgids:
                return False
            self._seen_msgids.append(msg_id)
            del self._seen_msgids[:-500]
            return True

    def _reply_xml(self, from_user: str, nonce: str, content: str) -> str:
        now = str(int(time.time()))
        resp = (
            "<xml><ToUserName><![CDATA[%s]]></ToUserName>"
            "<FromUserName><![CDATA[%s]]></FromUserName>"
            "<CreateTime>%s</CreateTime><MsgType><![CDATA[text]]></MsgType>"
            "<Content><![CDATA[%s]]></Content></xml>"
        ) % (from_user, self.corpid, now, content)
        enc = self.crypto.encrypt(resp.encode())
        sig = self.crypto.signature(now, nonce, enc)
        return (
            "<xml><Encrypt><![CDATA[%s]]></Encrypt>"
            "<MsgSignature><![CDATA[%s]]></MsgSignature>"
            "<TimeStamp>%s</TimeStamp><Nonce><![CDATA[%s]]></Nonce></xml>"
        ) % (enc, sig, now, nonce)

    # ---------- 指令分發 ----------

    def dispatch(self, user: str, text: str) -> str:
        parts = _CMD_SPLIT.split(text.strip())
        if not parts or not parts[0]:
            return HELP_TEXT
        # 首次使用的用戶需先同意使用約定，之後才能執行指令
        if not self._has_agreed(user):
            if parts[0] in ("同意", "同意協議", "同意协议", "agree"):
                self._agreed[user] = {"agreed_at": time.time()}
                self._save_users()
                self._log("info", "用戶 %s 已同意使用約定", user)
                return "✅ 已同意，開始使用吧！\n\n" + HELP_TEXT
            return self.AGREEMENT
        cmd = parts[0].lower()
        args = parts[1:]
        is_admin = user in self.admins
        if cmd in ("幫助", "帮助", "help", "menu"):
            return HELP_TEXT
        if cmd in ("綁定", "绑定"):
            return self.cmd_bind(user, args, is_admin)
        if cmd in ("cookie", "Cookie", "COOKIE", "憑證", "凭证"):
            return self.cmd_cookie(user, args, is_admin)
        if cmd in ("我的", "状态", "狀態", "status"):
            return self.ctx["summary_for"](user)
        if cmd in ("間隔", "间隔"):
            return self.cmd_interval(user, args, is_admin)
        if cmd in ("提醒",):
            return self.cmd_remind(user, args, is_admin)
        if cmd in ("檢查", "检查"):
            return self.cmd_check(user, args, is_admin)
        if cmd in ("解綁", "解绑"):
            return self.cmd_unbind(user, args, is_admin)
        if cmd in ("測試", "测试", "test"):
            self.push(user, "✅ 這是一條測試消息，通道正常！")
            return "已發送測試消息，請查收"
        return "🤔 未识别的指令。\n\n" + HELP_TEXT

    def _thread(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def cmd_bind(self, user: str, args: list, is_admin: bool) -> str:
        if len(args) < 2:
            return "格式：綁定 賬號 密碼 [備注]\n例：綁定 student001 abc123 小明"
        username, password = args[0], args[1]
        label = args[2] if len(args) >= 3 else ""
        ok, msg = self.ctx["register"](user, label, username, password, is_admin)
        if not ok:
            return msg
        label = msg  # register 成功時返回最終 label
        self._thread(self._bind_worker, user, label, username, password)
        return (f"✅「{label}」已登記！正在後台登入智學網…\n"
                f"· 成功 → 稍後自動推送該賬號當前作業\n"
                f"· 被驗證碼攔截 → 我會發你一步 Cookie 導入指引")

    def _bind_worker(self, user: str, label: str, username: str, password: str):
        try:
            ok, msg = self.ctx["login_and_save"](label, username, password)
            if ok:
                self.push(user, f"🎉「{label}」{msg}\n已開始監控，之後有新作業/臨近截止會自動提醒你。")
                self._thread(self._check_worker, user, label)
            else:
                self.push(user, (
                    f"⚠️「{label}」{msg}\n\n"
                    f"請改用 Cookie 方式（約1分鐘）：\n"
                    f"1. 手機或電腦瀏覽器打開 zhixue.com 並登入\n"
                    f"2. 登入後按 F12 → 網絡/Network → 刷新 → 點第一條請求\n"
                    f"3. 在「請求標頭」裡複製 Cookie: 後面的整串內容\n"
                    f"4. 回覆我：cookie {label} 粘貼內容"))
        except Exception:
            self._log("error", "綁定後台登入出錯：%s", traceback.format_exc())
            self.push(user, f"❌「{label}」後台登入異常，請稍後重試或用 cookie 指令導入")

    def cmd_cookie(self, user: str, args: list, is_admin: bool) -> str:
        if len(args) < 2:
            return "格式：cookie 賬號標籤 Cookie字符串\n（在已登入 zhixue.com 的瀏覽器 F12 → 網絡 → 第一條請求 → 請求標頭 → 複製 Cookie 後的整串）"
        label, cookie_str = args[0], " ".join(args[1:])
        ok, msg = self.ctx["import_cookie"](user, label, cookie_str, is_admin)
        if not ok:
            return msg
        self._thread(self._check_worker, user, label)
        return f"✅ {msg}\n正在檢查該賬號的作業，稍後推送…"

    def cmd_interval(self, user: str, args: list, is_admin: bool) -> str:
        if not args or not args[0].isdigit():
            return "格式：間隔 分鐘數 [賬號標籤]\n例：間隔 60　或　間隔 60 小明（分鐘範圍 5~1440）"
        minutes = max(5, min(1440, int(args[0])))
        label = args[1] if len(args) >= 2 else ""
        return self.ctx["set_interval"](user, minutes, label, is_admin)

    def cmd_remind(self, user: str, args: list, is_admin: bool) -> str:
        if not args:
            return ("格式：提醒 [賬號標籤] 間隔序列\n"
                    "例：提醒 5h,3h,2h,1h,30m,10m\n"
                    "　　提醒 小明 1h,30m\n"
                    "　　提醒 关（關閉重複提醒）\n"
                    "單位：h=小時，m/min=分鐘；可選 5min~5h，可組合多個遞進")
        if args[0] in ("关", "關", "off", "0", "无", "無"):
            label, spec = "", args[0]
        elif args[0][:1].isdigit():
            label, spec = "", " ".join(args)
        else:
            label, spec = args[0], " ".join(args[1:])
            if not spec:
                return ("格式：提醒 賬號標籤 間隔序列\n"
                        "例：提醒 小明 1h,30m（也可省略標籤：提醒 1h,30m；提醒 关 關閉重複提醒）\n"
                        "可選間隔：5m~5h（h=小時，m/min=分鐘）")
        return self.ctx["set_remind"](user, label, spec, is_admin)

    def cmd_check(self, user: str, args: list, is_admin: bool) -> str:
        label = args[0] if args else ""
        ok, msg = self.ctx["check_access"](user, label, is_admin)
        if not ok:
            return msg
        self._thread(self._check_worker, user, msg)  # msg=實際label
        return f"正在檢查「{msg}」…結果稍後推送"

    def _check_worker(self, user: str, label: str):
        try:
            self.push(user, self.ctx["check_now"](user, label))
        except Exception:
            self._log("error", "立即檢查出錯：%s", traceback.format_exc())
            self.push(user, "❌ 檢查異常，請稍後重試")

    def cmd_unbind(self, user: str, args: list, is_admin: bool) -> str:
        if not args:
            return "格式：解綁 賬號標籤"
        return self.ctx["unbind"](user, args[0], is_admin)

    # ---------- 主動推送 ----------

    def push(self, touser: str, content: str) -> None:
        try:
            self.client.push_text(touser, content, self.agentid)
            self._log("info", "已推送給 %s：%s", touser, content.replace("\n", " ")[:60])
        except Exception as e:
            self._log("warning", "推送給 %s 失敗：%s", touser, e)

    def _log(self, level, fmt, *args):
        if log_bot:
            getattr(log_bot, level)("[微信機器人] " + fmt, *args)
        else:
            print(("[微信機器人] ") + (fmt % args))


# ---------------------------------------------------------------------------
# HTTP 服務
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    bot: WeComBot = None  # type: ignore

    def do_GET(self):
        status, body = self.bot.handle_get(parse_qs(urlparse(self.path).query))
        self._respond(status, body, content_type="text/plain; charset=utf-8")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        status, xml_out = self.bot.handle_post(parse_qs(urlparse(self.path).query), body)
        self._respond(status, xml_out, content_type="application/xml; charset=utf-8")

    def _respond(self, status, body, content_type):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 靜默默認訪問日誌
        pass


def start_bot(bot_cfg: dict, ctx: dict, logger=None) -> None:
    """在校驗配置後，後台線程啟動回調 HTTP 服務"""
    set_logger(logger)
    required = ("corpid", "secret", "agentid", "callback_token", "encoding_aes_key")
    missing = [k for k in required if not bot_cfg.get(k)]
    if missing:
        raise SystemExit(f"wecom_bot 配置缺少字段：{', '.join(missing)}（或把 enabled 設為 false）")
    try:
        bot = WeComBot(bot_cfg, ctx)
        if bot_cfg.get("secret"):
            bot.client.access_token()  # 啟動時先驗證 secret 可用
    except Exception as e:
        raise SystemExit(f"微信機器人初始化失敗：{e}\n請檢查 wecom_bot 配置（corpid/secret/agentid/token/aes_key）")

    _Handler.bot = bot
    port = int(bot_cfg.get("listen_port") or 8080)
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="wecom-bot").start()
    bot._log("info", "微信機器人已啟動，回調端口 %d（請在企業微信後台配置回調URL）", port)
