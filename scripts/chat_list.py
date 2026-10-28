# ============ 小工具：看一眼聊天页的会话列表（验证打招呼是否真的发出）============
# 打招呼成功后，去 /web/geek/chat 看会话列表 —— 刚打招呼的会排在最前面。
#
# 用法：
#   python scripts/chat_list.py            # 列出最新 12 个会话
#   python scripts/chat_list.py --n 20
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bossgreet.core.cdp import Cdp                                 # noqa: E402
from bossgreet.core import cdpinput as bg                          # noqa: E402
from bossgreet.pages.zhipin import ChatPage, set_input_mode        # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    args = ap.parse_args()

    set_input_mode("cdp")
    cdp = Cdp("http://127.0.0.1:9222")
    t = cdp.find_target("zhipin.com")
    if not t:
        print("[!] 没找到 BOSS 页面")
        return 1
    cdp.attach(t)
    print(f"[*] 视口环境：{bg.window_visibility(cdp)}")

    chat = ChatPage(cdp)
    if not chat.goto():
        print("[!] 聊天页没加载出会话列表")
        return 1
    time.sleep(1.5)

    convs = chat.conversations(limit=args.n)
    print(f"[*] 聊天页会话列表（最新 {len(convs)} 个）：\n")
    for c in convs:
        print(f"  #{c['index']:>2}  {c['text']}")

    if convs:
        print(f"\n[*] 点开最新会话，看最后几条消息：")
        if chat.open_conversation(index=0):
            for m in chat.last_messages(4):
                print(f"     · {m}")
        else:
            print("     (点不开)")

    cdp.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
