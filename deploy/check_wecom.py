"""Can this machine reach the WeCom smart-robot long connection? (design/deployment.md §9 step 1)

    python3 deploy/check_wecom.py          (needs the `websockets` package: run it with the project's venv)

Subscribes with dummy credentials. WeCom answering 853000 ("invalid bot_id or secret") means the
endpoint is reachable and speaks the protocol. No real credentials are used or needed.
"""

import asyncio
import json
import sys

import websockets

URL = "wss://openws.work.weixin.qq.com"


async def main() -> int:
    frame = {"cmd": "aibot_subscribe", "headers": {"req_id": "check-1"},
             "body": {"bot_id": "check", "secret": "check"}}
    try:
        async with websockets.connect(URL, open_timeout=10, ping_interval=None, proxy=None) as ws:
            await ws.send(json.dumps(frame))
            answer = json.loads(await asyncio.wait_for(ws.recv(), 10))
    except Exception as exc:  # noqa: BLE001 - report any failure
        print(f"NOT reachable: {type(exc).__name__}: {exc}")
        return 1
    print(f"reachable: errcode={answer.get('errcode')} errmsg={str(answer.get('errmsg'))[:60]!r}")
    return 0 if answer.get("errcode") == 853000 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
