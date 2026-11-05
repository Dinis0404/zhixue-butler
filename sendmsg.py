#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手動給已綁定微信的用戶發送消息（走 WxPusher 通道）

用法：
    python sendmsg.py "消息內容"                  # 發給 config.json 裡所有綁定了微信的用戶（去重）
    python sendmsg.py UID_xxxx "消息內容"         # 發給指定 UID
    python sendmsg.py 小明 "消息內容"             # 發給指定賬號(label)綁定的微信
    python sendmsg.py --list                     # 列出所有已綁定的用戶

需要：config.json 的 notify.wxpusher_app_token 已配置
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import urllib.request

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
API = "https://wxpusher.zjiecode.com/api/send/message"


def main() -> None:
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    token = (cfg.get("notify") or {}).get("wxpusher_app_token")
    if not token:
        print("❌ config.json 裡還沒配置 notify.wxpusher_app_token")
        raise SystemExit(1)

    # 建立 UID 路由表：UID → [賬號label]
    uid_accounts: dict[str, list[str]] = {}
    for acc in cfg.get("accounts", []):
        label = acc.get("label") or acc.get("username") or "?"
        uids = ((acc.get("notify") or {}).get("wxpusher_uids")) or []
        if acc.get("owner"):  # 微信機器人綁定的 owner 也納入
            uids = uids or [acc["owner"]]
        for u in uids:
            uid_accounts.setdefault(u, []).append(label)

    if args[0] == "--list":
        print(f"共 {len(uid_accounts)} 個已綁定的微信用戶：")
        for u, labels in uid_accounts.items():
            print(f"  {u}  ←  {'、'.join(labels)}")
        if not uid_accounts:
            print("  （家長掃碼關注後，把收到的 UID 填進賬號的 notify.wxpusher_uids）")
        return

    # 解析目標
    first = args[0]
    if first.startswith("UID_"):
        if len(args) < 2:
            print("用法：python sendmsg.py UID_xxxx \"消息內容\"")
            return
        targets = {first: []}
        content = " ".join(args[1:])
    elif first in uid_accounts or any(
        first in labels for labels in uid_accounts.values()
    ):
        if len(args) < 2:
            print("用法：python sendmsg.py 賬號標籤 \"消息內容\"")
            return
        content = " ".join(args[1:])
        targets = {}
        for u, labels in uid_accounts.items():
            if first in labels or first == u:
                targets[u] = labels
    else:
        # 第一個參數不是 UID/賬號 → 視為消息內容，群發給所有綁定用戶
        content = " ".join(args)
        targets = uid_accounts

    if not targets:
        print("❌ 沒有找到可發送的用戶（還沒有人綁定）")
        raise SystemExit(1)

    print(f"發送對象：{sum((v for v in targets.values()), []) or list(targets)}")
    print(f"消息內容：{content[:80]}{'…' if len(content) > 80 else ''}\n")

    ok = fail = 0
    for uid in targets:
        payload = {
            "appToken": token,
            "content": content,
            "contentType": 1,
            "uids": [uid],
            "summary": content[:99],
        }
        req = urllib.request.Request(
            API, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            data = json.loads(urllib.request.urlopen(req, timeout=15).read())
            ok_flag = data.get("code") == 1000
            ok += ok_flag
            fail += not ok_flag
            who = "、".join(uid_accounts.get(uid, [])) or uid
            print(f"{'✅' if ok_flag else '❌'} {who} ({uid})")
        except Exception as e:
            fail += 1
            print(f"❌ {uid} 發送異常：{e}")

    print(f"\n完成：成功 {ok}，失敗 {fail}")


if __name__ == "__main__":
    main()
