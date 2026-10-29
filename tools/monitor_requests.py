"""监控浏览器页面的网络请求（用 CDP 的 Network 域）。

用法：
    .venv\\Scripts\\python.exe tools\\monitor_requests.py --seconds 30

流程：
    1. 先让浏览器停在你关心的页面
    2. 运行本脚本（会开始录制）
    3. 在浏览器里正常操作（翻页、点筛选、打开简历…）
    4. 到时自动停止，输出汇总 + 保存 JSON

输出：
    控制台：按 host 统计、可疑接口列表
    文件：  boss_vision\\runs\\requests-<时间>.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "boss_vision"))

import cdp  # noqa: E402

INTERESTING = ("zhipin.com", "api", "job", "resume", "geek", "chat", "recommend", "list")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--filter", default="zhipin.com", help="只看包含该串的 URL")
    args = ap.parse_args()

    if not cdp.available():
        print("调试端口没开：先运行 启动调试浏览器.cmd")
        return 1

    c = cdp.CDP()
    print(f"  当前页签: {getattr(c, 'href', '')[:70]}")
    print(f"  开始录制 {args.seconds} 秒，请在浏览器里正常操作…\n")

    # 打开 Network 并清空缓冲
    c._send("Network.enable")
    try:
        c.ws.sock.settimeout(0.5)
    except Exception:
        pass

    c.ws.send(json.dumps({"id": 9001, "method": "Network.enable", "params": {}}))

    reqs: dict[str, dict] = {}
    finished: list[dict] = []
    deadline = time.time() + args.seconds

    while time.time() < deadline:
        try:
            raw = c.ws.recv()
        except Exception:
            continue                 # 超时没消息，继续等
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        method = msg.get("method") or ""
        p = msg.get("params") or {}

        if method == "Network.requestWillBeSent":
            rid = p.get("requestId")
            r = p.get("request") or {}
            reqs[rid] = {
                "method": r.get("method"),
                "url": r.get("url"),
                "type": p.get("type"),
                "ts": time.time(),
            }
        elif method == "Network.responseReceived":
            rid = p.get("response").get("url"), p.get("requestId")
            rid = p.get("requestId")
            info = reqs.get(rid)
            if info is not None:
                resp = p.get("response") or {}
                info["status"] = resp.get("status")
                info["mime"] = resp.get("mimeType")
                finished.append(info)

    try:
        c.ws.sock.settimeout(10.0)
    except Exception:
        pass
    c.close()

    rows = [v for v in reqs.values() if args.filter in (v.get("url") or "")]
    hosts = Counter(urlparse(v["url"]).netloc for v in rows)
    apis = [v for v in rows if (v.get("type") in ("XHR", "Fetch")
                                or "/api/" in (v.get("url") or ""))]

    print(f"  共捕获 {len(reqs)} 个请求，其中匹配「{args.filter}」的 {len(rows)} 个")
    print("\n  按域名统计：")
    for h, n in hosts.most_common(10):
        print(f"    {n:4d}  {h}")

    print(f"\n  接口类请求（XHR/Fetch/api）共 {len(apis)} 个：")
    for v in apis[:40]:
        url = v["url"].split("?")[0]
        print(f"    {v.get('method','?'):4} {v.get('status','?'):>4}  {url[:100]}")

    out = REPO / "boss_vision" / "runs"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"requests-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(sorted(rows, key=lambda x: x.get("ts", 0)),
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  明细已保存: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
