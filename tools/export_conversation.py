"""导出本会话（或指定会话）的对话记录为 Markdown。

会话原始记录在 `~/.dsh/sessions/<项目目录>/<session-id>/session.v4.jsonl.zstd`，
是 zstd 压缩的 JSONL，事件类型包括 user/message、assistant/message、tool/call 等。

用法：
    python tools\\export_conversation.py --last 10
    python tools\\export_conversation.py --last 10 --session <session-id>
    python tools\\export_conversation.py --list          # 列出所有会话
    python tools\\export_conversation.py --last 5 --with-tools   # 连工具调用也导出

输出：工作区根目录 `对话记录-最近<N>条-<时间>.md`
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSIONS = Path.home() / ".dsh" / "sessions"


def find_session_files() -> list[Path]:
    return sorted(SESSIONS.rglob("session.v4.jsonl.zstd"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def read_events(path: Path) -> list[dict]:
    import zstandard

    with path.open("rb") as fh:
        reader = zstandard.ZstdDecompressor().stream_reader(fh)
        raw = reader.read().decode("utf-8", "replace")
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


def _text_of(content: list[dict], with_images: bool = True) -> str:
    out: list[str] = []
    for part in content or []:
        kind = part.get("type")
        if kind == "text":
            out.append(part.get("text", "").strip())
        elif kind == "image" and with_images:
            out.append("[图片/截图]")
    return "\n\n".join(t for t in out if t)


def build_rounds(events: list[dict], with_tools: bool) -> list[dict]:
    """把事件流整理成「用户一轮 + 助手若干条」的轮次列表。"""
    rounds: list[dict] = []
    current: dict | None = None

    for ev in events:
        kind = ev.get("type")
        data = ev.get("data") or {}
        ts = ev.get("time")

        if kind == "user/message":
            content = data.get("content") or []
            text = _text_of(content)
            if not text:
                continue
            # 运行环境注入的提示不算用户发言
            if text.startswith("Current runtime context") or text.startswith("<system-reminder>"):
                continue
            only_image = all(p.get("type") == "image" for p in content)
            # 截图常单独发一条：并到上一轮，避免出现"只贴了图"的空轮次
            if only_image and rounds and current is not None:
                current["user"] = (current["user"] + "\n\n" + text).strip()
                continue
            current = {"time": ts, "user": text, "assistant": [], "tools": []}
            rounds.append(current)

        elif kind == "assistant/message" and current is not None:
            msg = data.get("message") or {}
            text = _text_of(msg.get("content") or [], with_images=False)
            # 只保留讲给用户的话，跳过 reasoning 与重复内容
            chunks = [p.get("text", "").strip() for p in (msg.get("content") or [])
                      if p.get("type") == "text" and p.get("text", "").strip()]
            for chunk in chunks:
                if not current["assistant"] or current["assistant"][-1] != chunk:
                    current["assistant"].append(chunk)

        elif kind == "tool/call" and current is not None and with_tools:
            name = data.get("name") or data.get("tool") or "tool"
            args = data.get("arguments") or data.get("args") or {}
            brief = json.dumps(args, ensure_ascii=False)
            current["tools"].append({"name": name, "brief": brief[:300]})

    return rounds


def fmt_time(ms: int | None) -> str:
    if not ms:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=10, help="导出最近 N 轮")
    ap.add_argument("--session", default=None, help="会话 id（默认取最近修改的那个）")
    ap.add_argument("--list", action="store_true", help="列出可用会话")
    ap.add_argument("--with-tools", action="store_true", help="是否包含工具调用")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    files = find_session_files()
    if args.list:
        for f in files:
            print(f"{f.parent.name}  ({time.strftime('%Y-%m-%d %H:%M', time.localtime(f.stat().st_mtime))})")
        return 0
    if not files:
        print("没找到会话记录")
        return 2

    target = files[0]
    if args.session:
        target = next((f for f in files if args.session in str(f)), None) or target

    events = read_events(target)
    rounds = build_rounds(events, args.with_tools)
    picked = rounds[-args.last:] if args.last else rounds

    stamp = time.strftime("%Y%m%d-%H%M")
    out = Path(args.out) if args.out else ROOT / f"对话记录-最近{len(picked)}条-{stamp}.md"

    lines = [
        f"# 对话记录（最近 {len(picked)} 轮）",
        "",
        f"- 会话：`{target.parent.name}`",
        f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 全会话共 {len(rounds)} 轮，本次导出最后 {len(picked)} 轮",
        "- 助手内容为会话记录原文（含中间说明）；`[图片/截图]` 表示该轮附了截图",
        "",
        "---",
        "",
    ]
    for i, r in enumerate(picked, 1):
        lines.append(f"## 第 {i} 轮 · {fmt_time(r['time'])}")
        lines.append("")
        lines.append("### 用户")
        lines.append("")
        lines.append(r["user"])
        lines.append("")
        lines.append("### 助手")
        lines.append("")
        lines.append("\n\n".join(r["assistant"]) if r["assistant"] else "（本轮没有文字回复）")
        lines.append("")
        if r["tools"]:
            lines.append("<details><summary>工具调用（{} 次）</summary>".format(len(r["tools"])))
            lines.append("")
            for t in r["tools"]:
                lines.append(f"- `{t['name']}` {t['brief']}")
            lines.append("")
            lines.append("</details>")
            lines.append("")
        lines.append("---")
        lines.append("")

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"已导出 {len(picked)} 轮 -> {out}（{out.stat().st_size / 1024:.1f} KB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
