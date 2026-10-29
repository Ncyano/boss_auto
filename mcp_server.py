"""MCP 服务端：把「简历判定」能力暴露成标准 MCP 工具。

传输：stdio（每行一条 JSON-RPC 2.0 消息），纯标准库实现，无需安装 mcp SDK。

暴露的工具：
    screen_resume      传简历文本 → 判定 合适/不合适 + 原因
    screen_records     批量判定（文本数组）
    list_rules         查看知识库当前生效的规则
    check_balance      查通义 / DeepSeek 余额与连通性

启动（供 MCP 客户端调用）：
    .venv\\Scripts\\python.exe mcp_server.py

手动测试：
    .venv\\Scripts\\python.exe mcp_server.py --selftest
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "boss_vision"))

SERVER_INFO = {"name": "boss-screening", "version": "1.0.0"}
PROTOCOL = "2024-11-05"


def _log(msg: str) -> None:
    """日志必须写 stderr，stdout 只走协议消息。"""
    print(f"[mcp] {msg}", file=sys.stderr, flush=True)


# ---------------- 工具实现 ----------------

def tool_screen_resume(args: dict) -> str:
    import judge
    import vlm

    text = str(args.get("text") or "").strip()
    if not text:
        return "缺少参数 text（简历正文）"
    if len(text) < 30:
        return "简历文本过短，无法判定（至少 30 字）"

    st = judge.structure_text(text, judge.struct_config()) or {}
    parts = vlm.normalize((st or {}).get("data", {}).get("sections"))
    work = parts.get("work") or []
    edu = parts.get("education") or []

    rec = {
        "name": str(args.get("name") or ""),
        "work": work,
        "education": edu,
        "basic": (st.get("data") or {}).get("basic") or {},
        "complete": bool(work and edu),
        "reached_education": bool(edu),
        "missing": [] if (work and edu) else ["工作经历" if not work else "", "教育经历" if not edu else ""],
    }
    rec["missing"] = [m for m in rec["missing"] if m]

    import judge_inline
    import screening

    row = judge_inline.record_to_row(rec)
    out = screening.screen_row(row, screening.load_rules())

    lines = [
        f"结论：{out.get('最终结论')}",
        f"处理状态：{out.get('处理状态')}",
        f"原因：{out.get('原因')}",
        f"工作经历 {len(work)} 条 / 教育经历 {len(edu)} 条",
    ]
    if work:
        lines.append("首段经历：" + str(work[0].get("company") or "")[:20]
                     + " / " + str(work[0].get("title") or "")[:20])
    if edu:
        lines.append("学历：" + str(edu[0].get("school") or "")[:20]
                     + " / " + str(edu[0].get("degree") or ""))
    return "\n".join(lines)


def tool_screen_records(args: dict) -> str:
    items = args.get("items") or []
    if not isinstance(items, list):
        return "参数 items 必须是数组"
    out = []
    for i, it in enumerate(items[:50], 1):
        text = str(it.get("text") if isinstance(it, dict) else it or "")
        one = tool_screen_resume({"text": text, "name": (it or {}).get("name", "") if isinstance(it, dict) else ""})
        first = one.splitlines()[0] if one else ""
        out.append(f"{i}. {first}")
    return "\n".join(out) if out else "没有可判定的条目"


def tool_list_rules(args: dict) -> str:
    import kb
    data = kb.load()
    rules = data.get("rules") if isinstance(data, dict) else None
    if not rules:
        return json.dumps(data, ensure_ascii=False)[:1500]
    out = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        out.append(f"{r.get('id')}\t{r.get('kind') or ''}\t{r.get('desc') or r.get('name') or ''}")
    return "\n".join(out)


def tool_check_balance(args: dict) -> str:
    sys.path.insert(0, str(REPO / "tools"))
    import importlib
    mod = importlib.import_module("check_balance")
    import settings
    cfg = settings.load()
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        mod.check_qwen(cfg.get("vision") or {})
        mod.check_deepseek(cfg.get("judge") or {})
    return buf.getvalue().strip()


RUN = {"thread": None, "started": 0.0, "params": {}, "last_error": ""}


def tool_start_batch(args: dict) -> str:
    """启动采集（后台线程）。参数：limit/min_gap/max_gap/live/url/allow_refresh。"""
    import threading
    import time as _t
    import driver

    if RUN["thread"] and RUN["thread"].is_alive():
        return "已有一个任务在运行，请先调用 stop_batch"

    limit = int(args.get("limit") or 20)
    min_gap = float(args.get("min_gap") or 3.0)
    max_gap = float(args.get("max_gap") or 5.0)
    live = bool(args.get("live", True))
    url = str(args.get("url") or "")
    allow_refresh = bool(args.get("allow_refresh", True))
    capture = str(args.get("capture") or "cdp")

    def _run() -> None:
        # 关键：driver.run 全程使用 print()，而 MCP 的 stdout 是 JSON-RPC 通道。
        # 在采集线程里把 stdout/stderr 全部改到 stderr，避免污染协议输出。
        import contextlib
        import sys as _sys
        try:
            with contextlib.redirect_stdout(_sys.stderr):
                driver.run(live=live, limit=limit, max_scrolls=None, read="scroll",
                           anchor=(1250, 600), max_refresh=None,
                           min_gap=min_gap, max_gap=max_gap, icon_check=True,
                           count_valid=True, allow_refresh=allow_refresh,
                           block_text_scan=False, capture=capture, url=url)
        except Exception as exc:
            RUN["last_error"] = f"{type(exc).__name__}: {exc}"

    driver.reset_control()
    th = threading.Thread(target=_run, daemon=True)
    th.start()
    RUN.update({"thread": th, "started": _t.time(),
                "params": {"limit": limit, "min_gap": min_gap, "max_gap": max_gap,
                           "live": live, "capture": capture}})
    return (f"已启动：目标 {limit} 个打招呼，间隔 {min_gap}~{max_gap} 秒，"
            f"模式={'实盘' if live else '试运行'}，抓图={capture}")


def tool_status(args: dict) -> str:
    import time as _t
    import driver
    p = dict(driver.PROGRESS)
    alive = bool(RUN["thread"] and RUN["thread"].is_alive())
    elapsed = int(_t.time() - RUN["started"]) if RUN["started"] else 0
    lines = [
        f"状态：{'运行中' if alive else '空闲'}",
        f"已打招呼：{p.get('valid', 0)}/{p.get('target', 0)}",
        f"尝试次数：{p.get('handled', 0)}",
        f"已运行：{elapsed // 60}分{elapsed % 60}秒",
    ]
    if RUN["last_error"]:
        lines.append(f"最近错误：{RUN['last_error'][:160]}")
    if not alive and RUN["started"]:
        lines.append("任务已结束")
    return "\n".join(lines)


def tool_stop_batch(args: dict) -> str:
    import driver
    driver.request_stop()
    return "已发送停止指令，当前动作结束后退出"



LAUNCHER_NAMES = {
    "edge": ["run-edge-debug.cmd", "启动调试浏览器.cmd"],
    "gui": ["run-gui.cmd", "启动控制台.cmd"],
}


def _find_launcher(kind: str) -> Path | None:
    """在项目目录 / 包目录里找启动脚本。"""
    names = LAUNCHER_NAMES[kind]
    roots = [REPO, REPO.parent, REPO / "app".replace("app", ""), REPO / "app"]
    for root in roots:
        for n in names:
            cand = root / n
            if cand.exists():
                return cand
    # 包结构：<包>\app\boss_vision\..\..\启动调试浏览器.cmd
    for up in (REPO.parent.parent, REPO.parent.parent.parent):
        for n in names:
            cand = up / n
            if cand.exists():
                return cand
    return None


def _run_launcher(kind: str) -> str:
    """启动一个 .cmd（独立控制台窗口，不阻塞 MCP）。"""
    import subprocess
    cmd = _find_launcher(kind)
    if cmd is None:
        return f"未找到启动脚本（{LAUNCHER_NAMES[kind][0]}）"
    subprocess.Popen(["cmd", "/c", "start", "", str(cmd)],
                     cwd=str(cmd.parent), close_fds=True)
    return f"已启动 {cmd.name}"


PROFILE_HINT = "boss-cdp-profile"


def _kill_debug_edge() -> str:
    """只结束「调试专用实例」（命令行含 boss-cdp-profile），不影响日常 Edge。"""
    import subprocess

    try:
        out = subprocess.run(
            ["wmic", "process", "where",
             f"CommandLine like '%{PROFILE_HINT}%'", "get", "ProcessId"],
            capture_output=True, text=True, timeout=20).stdout
    except Exception as exc:
        return f"查询进程失败: {exc}"

    pids = [ln.strip() for ln in out.splitlines()
            if ln.strip().isdigit()]
    if not pids:
        return "没有残留的调试浏览器实例"
    killed = 0
    for pid in pids:
        r = subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            killed += 1
    return f"已结束 {killed} 个调试浏览器进程"


def tool_launch_browser(args: dict) -> str:
    """启动带调试端口的浏览器，并**确保**停在该网址（默认推荐牛人页）。"""
    import json as _json
    import time as _t
    import urllib.request

    import cdp
    import winenv

    target_url = str(args.get("url") or "https://www.zhipin.com/web/chat/recommend")
    wait = float(args.get("wait") or 25)
    lines: list[str] = []

    if not cdp.available():
        # 先清掉「调试专用」的残留实例，避免 start 被已有进程接管
        lines.append(_kill_debug_edge())
        _t.sleep(1.5)
        msg = _run_launcher("edge")
        lines.append(msg)

        # 第一次尝试：等 12 秒
        def _wait_port(secs: float) -> bool:
            t0 = _t.time()
            while _t.time() - t0 < secs:
                if cdp.available():
                    return True
                _t.sleep(1.0)
            return False

        if not _wait_port(min(12.0, wait)):
            # 说明被已有 Edge 实例吞掉了：只能关掉全部 Edge 后重来
            lines.append("被已有 Edge 实例接管，改为结束全部 Edge 后重启")
            import subprocess
            subprocess.run(["taskkill", "/F", "/IM", "msedge.exe"],
                           capture_output=True, text=True, timeout=30)
            _t.sleep(2.0)
            lines.append(_run_launcher("edge"))
            _wait_port(max(5.0, wait - 14.0))
        t0 = _t.time()
        if not cdp.available():
            lines.append(f"等待 {int(wait)} 秒后端口仍不可用")
            lines.append("常见原因：已有普通 Edge 在运行，新启动被它接管；"
                         "请先完全退出所有 Edge 后重试。")
            return "\n".join(lines)
        lines.append("调试端口已就绪")
    else:
        lines.append("调试端口已在运行")

    def _pages() -> list[dict]:
        _no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with _no_proxy.open("http://127.0.0.1:9222/json", timeout=5) as r:
            ts = _json.loads(r.read().decode())
        return [t for t in ts if t.get("type") == "page"]

    pages = _pages()
    hit = next((t for t in pages if target_url in (t.get("url") or "")), None)

    if hit is None:
        # 没有目标页：优先复用 BOSS 页签导航过去，否则新建
        boss = next((t for t in pages if "zhipin.com" in (t.get("url") or "")), None)
        if boss:
            cdp._PINNED_PAGE[0] = boss["webSocketDebuggerUrl"]
            c = cdp.CDP()
            lines.append(f"把现有页签导航到目标页（原 {boss.get('url','')[:50]}）")
            c.navigate(target_url, wait=5.0)
            c.close()
        else:
            cdp._PINNED_PAGE[0] = pages[0]["webSocketDebuggerUrl"] if pages else None
            c = cdp.CDP()
            c._call("Target.createTarget", {"url": target_url})
            lines.append("已新建页签打开目标页")
            _t.sleep(4.0)
            c.close()
        cdp._PINNED_PAGE[0] = None
        pages = _pages()
        hit = next((t for t in pages if target_url in (t.get("url") or "")), None)

    if hit is None:
        lines.append("⚠ 仍未找到目标页，请手动打开：" + target_url)
        return "\n".join(lines)

    # 固定到该页签，并核对 location.href
    cdp._PINNED_PAGE[0] = hit["webSocketDebuggerUrl"]
    c = cdp.CDP()
    href = ((c._call("Runtime.evaluate", {"expression": "location.href",
                                          "returnByValue": True})
             .get("result") or {}).get("value")) or ""
    if target_url not in href:
        lines.append(f"页签地址不符（{href[:60]}），重新导航")
        c.navigate(target_url, wait=5.0)
        href = ((c._call("Runtime.evaluate", {"expression": "location.href",
                                              "returnByValue": True})
                 .get("result") or {}).get("value")) or ""
    # 进入页面后切到「最新」标签（BOSS 推荐页有两个 tab：推荐 / 最新）
    tab = str(args.get("click_tab") or "最新")
    tab_ok = False
    if tab:
        try:
            tab_ok = bool(c.click_element(tab, exact=True))
            if not tab_ok:
                tab_ok = bool(c.click_element(tab, exact=False))
            if tab_ok:
                _t.sleep(1.5)
        except Exception:
            tab_ok = False

    logged = ((c._call("Runtime.evaluate",
                       {"expression": "document.cookie.indexOf('zp_at')>=0 || !!localStorage.getItem('__zp_stoken__')",
                        "returnByValue": True})
               .get("result") or {}).get("value"))
    c.close()

    lines.append(f"当前页签: {href[:80]}")
    lines.append("与目标一致: " + ("是" if target_url in href else "否"))
    if tab:
        lines.append(f"切换到「{tab}」: " + ("成功" if tab_ok else "未找到该标签"))
    lines.append("已登录迹象: " + ("有" if logged else "未检测到（请确认已登录 BOSS）"))
    if bool(args.get("start_gui", False)):
        lines.append(_run_launcher("gui"))
    lines.append("可以直接调用 start_batch 开始采集")
    return "\n".join(lines)


def tool_get_progress_detail(args: dict) -> str:
    """返回每个人的判定结果（读最近一次 results.json，并叠加实时进度）。"""
    import json as _json
    import time as _t
    import driver

    runs = REPO / "boss_vision" / "runs"
    files = sorted(runs.glob("*-results.json"), key=lambda p: p.stat().st_mtime)
    lines = []
    prog = dict(driver.PROGRESS)
    alive = bool(RUN["thread"] and RUN["thread"].is_alive())
    lines.append(f"状态：{'运行中' if alive else '空闲'}｜"
                 f"已打招呼 {prog.get('valid', 0)}/{prog.get('target', 0)}｜"
                 f"尝试 {prog.get('handled', 0)}")
    if not files:
        lines.append("还没有结果文件")
        return "\n".join(lines)

    latest = files[-1]
    lines.append(f"结果文件：{latest.name}")
    try:
        rows = _json.loads(latest.read_text(encoding="utf-8"))
    except Exception as exc:
        lines.append(f"读取失败：{exc}")
        return "\n".join(lines)

    limit = int(args.get("limit") or 30)
    only = str(args.get("only") or "").strip()   # passed / rejected
    shown = 0
    for i, r in enumerate(rows, 1):
        j = r.get("judgment") or {}
        verdict = j.get("verdict") or "?"
        if only == "passed" and verdict != "合适":
            continue
        if only == "rejected" and verdict != "不合适":
            continue
        if shown >= limit:
            break
        shown += 1
        greeted = "已打招呼" if r.get("greeted") else ""
        lines.append(
            f"{i}. {str(r.get('name') or '(无名)')[:12]}\t{verdict}\t"
            f"{(j.get('stage') or '')[:6]}\t{greeted}\t"
            f"{(j.get('reason') or '')[:70]}")
    if shown == 0:
        lines.append("没有符合过滤条件的结果")
    return "\n".join(lines)


TOOLS = [
    {
        "name": "screen_resume",
        "description": "按知识库规则判断一份简历是否合适（输入简历正文文本）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "简历正文（OCR 或人工粘贴的纯文本）"},
                "name": {"type": "string", "description": "候选人姓名（可选）"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "screen_records",
        "description": "批量判断多份简历（最多 50 条），返回每条的结论行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "text": {"type": "string"}, "name": {"type": "string"}}},
                }
            },
            "required": ["items"],
        },
    },
    {
        "name": "list_rules",
        "description": "列出知识库中生效的判定规则（编号 / 类型 / 说明）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "start_batch",
        "description": "启动自动采集（后台运行）。可自定义打招呼数量与候选人间隔。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "要打招呼的人数（默认 20）"},
                "min_gap": {"type": "number", "description": "最小间隔秒数（默认 3）"},
                "max_gap": {"type": "number", "description": "最大间隔秒数（默认 5）"},
                "live": {"type": "boolean", "description": "true=真实点击，false=试运行"},
                "capture": {"type": "string", "description": "cdp（后台）或 screen"},
                "allow_refresh": {"type": "boolean", "description": "列表到底时是否刷新"},
                "url": {"type": "string", "description": "可选，开始前先打开的网址"},
            },
        },
    },
    {
        "name": "status",
        "description": "查看当前任务的进度（已打招呼数 / 尝试次数 / 耗时）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "stop_batch",
        "description": "停止正在运行的任务。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "launch_browser",
        "description": "启动带调试端口的浏览器（并可选打开网址），供随后 start_batch 使用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "启动后打开的网址"},
                "click_tab": {"type": "string",
                              "description": "进入页面后点击的标签，默认「最新」，传空串则不点"},
                "wait": {"type": "number", "description": "等待端口就绪的秒数（默认 25）"},
                "start_gui": {"type": "boolean",
                              "description": "是否同时启动图形控制台（run-gui.cmd）"},
            },
        },
    },
    {
        "name": "get_progress_detail",
        "description": "返回每个人的判定结果（姓名 / 结论 / 环节 / 是否已打招呼 / 原因）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回多少条（默认 30）"},
                "only": {"type": "string", "description": "可选 passed / rejected 过滤"},
            },
        },
    },
    {
        "name": "check_balance",
        "description": "检查通义千问与 DeepSeek 的连通性和余额。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

DISPATCH = {
    "screen_resume": tool_screen_resume,
    "screen_records": tool_screen_records,
    "list_rules": tool_list_rules,
    "check_balance": tool_check_balance,
    "start_batch": tool_start_batch,
    "status": tool_status,
    "stop_batch": tool_stop_batch,
    "launch_browser": tool_launch_browser,
    "get_progress_detail": tool_get_progress_detail,
}


# ---------------- 协议 ----------------

def reply(msg_id, result=None, error=None) -> str:
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    return json.dumps(out, ensure_ascii=False)


def handle(msg: dict) -> str | None:
    method = msg.get("method") or ""
    mid = msg.get("id")

    if method == "initialize":
        return reply(mid, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return reply(mid, {})
    if method == "tools/list":
        return reply(mid, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name")
        fn = DISPATCH.get(name)
        if not fn:
            return reply(mid, error={"code": -32601, "message": f"未知工具: {name}"})
        try:
            text = fn(params.get("arguments") or {})
            return reply(mid, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:
            _log(f"工具 {name} 异常: {exc}")
            return reply(mid, {"content": [{"type": "text", "text": f"执行失败: {exc}"}],
                               "isError": True})
    if mid is None:
        return None
    return reply(mid, error={"code": -32601, "message": f"不支持的方法: {method}"})


def serve() -> int:
    _log("MCP 服务已启动（stdio）")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            print(reply(None, error={"code": -32700, "message": "JSON 解析失败"}), flush=True)
            continue
        out = handle(msg)
        if out:
            print(out, flush=True)
    return 0


def selftest() -> int:
    cases = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "list_rules", "arguments": {}}},
    ]
    for c in cases:
        out = handle(c)
        label = c.get("method")
        print(f"  -> {label}: {str(out)[:180]}")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    raise SystemExit(serve())
