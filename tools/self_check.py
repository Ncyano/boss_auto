"""全仓库静态自检：语法、导入、重复定义、危险模式、密钥泄露、异常吞没。"""
from __future__ import annotations

import ast
import collections
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(".")
SKIP_DIRS = {".venv", "data", "dist", "build", "__pycache__", ".git", "node_modules", "runs"}
PY_FILES = [p for p in ROOT.rglob("*.py")
            if not any(part in SKIP_DIRS for part in p.parts)]

print(f"=== 1. 语法检查（{len(PY_FILES)} 个文件）===")
bad = []
for f in PY_FILES:
    try:
        ast.parse(f.read_text(encoding="utf-8"))
    except Exception as exc:
        bad.append((f, exc))
print(f"  失败 {len(bad)}")
for f, e in bad:
    print(f"   {f}: {e}")

print("\n=== 2. 重复定义 ===")
for f in PY_FILES:
    try:
        tree = ast.parse(f.read_text(encoding="utf-8"))
    except Exception:
        continue
    top = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    dup = [n for n, c in collections.Counter(top).items() if c > 1]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
            d2 = [n for n, c in collections.Counter(names).items() if c > 1]
            if d2:
                print(f"   {f}:{node.name} 重复方法 {d2}")
    if dup:
        print(f"   {f} 重复顶层 {dup}")

print("\n=== 3. 危险模式 ===")
PATTERNS = {
    "sys.stdout = ": "全局替换 stdout（会吞掉别处输出）",
    "sys.stderr = ": "全局替换 stderr",
    "os._exit": "强制退出，跳过清理",
    "eval(": "eval 执行字符串",
    "exec(": "exec 执行字符串",
    "verify=False": "关闭 TLS 校验",
    "shell=True": "shell 注入风险",
}
for f in PY_FILES:
    text = f.read_text(encoding="utf-8")
    for pat, why in PATTERNS.items():
        for i, line in enumerate(text.splitlines(), 1):
            if pat in line and not line.strip().startswith("#"):
                print(f"   {f}:{i} {pat.strip()}  → {why}")

print("\n=== 4. 密钥泄露扫描 ===")
KEY_RE = re.compile(r"(sk-[A-Za-z0-9._\-]{12,}|AKID[A-Za-z0-9]{10,})")
hits = 0
scan_ext = {".py", ".json", ".md", ".cmd", ".txt", ".yml", ".yaml", ".ini"}
for p in ROOT.rglob("*"):
    if not p.is_file() or p.suffix.lower() not in scan_ext:
        continue
    if any(part in SKIP_DIRS for part in p.parts):
        continue
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue
    for i, line in enumerate(text.splitlines(), 1):
        if KEY_RE.search(line) and "sk-xxx" not in line.lower():
            print(f"   {p}:{i} 疑似密钥: {line.strip()[:70]}")
            hits += 1
print(f"  命中 {hits} 处")

print("\n=== 5. 吞异常统计 ===")
swallow = collections.Counter()
for f in PY_FILES:
    text = f.read_text(encoding="utf-8")
    n = text.count("except Exception:\n        pass") + text.count("except Exception:\n            pass")
    if n:
        swallow[f] = n
for f, n in swallow.most_common(10):
    print(f"   {f}: {n} 处 except→pass")

print("\n=== 6. 关键模块导入 ===")
sys.path.insert(0, str(ROOT / "boss_vision"))
mods = ["settings", "kb", "screening", "judge", "judge_inline", "vlm", "vision",
        "cdp", "winenv", "driver", "resume_reader", "export_csv", "textfix", "gui"]
ok, fail = 0, []
for m in mods:
    try:
        __import__(m)
        ok += 1
    except Exception as exc:
        fail.append((m, f"{type(exc).__name__}: {exc}"))
print(f"  成功 {ok}/{len(mods)}")
for m, e in fail:
    print(f"   {m}: {e}")

print("\n=== 7. 配置一致性 ===")
try:
    sys.path.insert(0, str(ROOT / "boss_vision"))
    import vlm
    import judge
    v = vlm.load_config()
    j = judge.load_config()
    c = judge.struct_config()
    print(f"  vision.pipeline={v.get('pipeline')} ocr={v.get('ocr_model')}")
    print(f"  struct.model={c.get('model')} base={c.get('base_url')}")
    print(f"  judge.model={j.get('model')} base={j.get('base_url')}")
    print(f"  vision key 已配置: {bool(vlm.api_key(v))}")
except Exception as exc:
    print(f"  配置检查失败: {exc}")

print("\n=== 8. position 配置 ===")
try:
    import kb
    for n in kb.position_names():
        print(f"  {n}: ready={kb.is_ready(n)}")
    print(f"  当前: {kb.current_position()}  锁定: {kb.position_locked()}")
except Exception as exc:
    print(f"  失败: {exc}")

print("\n=== 自检结束 ===")
