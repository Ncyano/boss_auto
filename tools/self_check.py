"""全局自检 v2：语法 / 重复 / 危险模式 / 密钥 / 导入 / JSON / 编码 / 工具脚本。

用法：
    .venv\\Scripts\\python.exe tools/self_check.py
    .venv\\Scripts\\python.exe tools/self_check.py --verbose
"""
from __future__ import annotations

import ast
import collections
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(".").resolve()
SKIP_DIRS = {".venv", "data", "dist", "build", "__pycache__", ".git", "node_modules",
             "runs", "_test_runs", ".workbuddy"}
VERBOSE = "--verbose" in sys.argv

PROBLEMS: list[str] = []


def add(msg: str) -> None:
    PROBLEMS.append(msg)
    print(f"   ⚠ {msg}")


def iter_py():
    for p in ROOT.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        yield p


py_files = sorted(iter_py())
print(f"=== 1. 语法检查（{len(py_files)} 个文件）===")
bad = []
for f in py_files:
    try:
        ast.parse(f.read_text(encoding="utf-8"))
    except Exception as exc:
        bad.append((f, exc))
        add(f"语法错误 {f.relative_to(ROOT)}: {exc}")
print(f"  失败 {len(bad)}")

print("\n=== 2. 编码检查（BOM / 非 UTF-8）===")
enc_bad = 0
for f in py_files:
    raw = f.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        add(f"含 BOM: {f.relative_to(ROOT)}")
        enc_bad += 1
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        add(f"非 UTF-8: {f.relative_to(ROOT)} ({exc})")
        enc_bad += 1
print(f"  问题 {enc_bad}")

print("\n=== 3. 重复定义 ===")
dup_n = 0
for f in py_files:
    try:
        tree = ast.parse(f.read_text(encoding="utf-8"))
    except Exception:
        continue
    top = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    for n, c in collections.Counter(top).items():
        if c > 1:
            add(f"重复顶层定义 {f.relative_to(ROOT)}: {n}×{c}")
            dup_n += 1
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            names = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
            for n, c in collections.Counter(names).items():
                if c > 1:
                    add(f"重复方法 {f.relative_to(ROOT)}::{node.name}.{n}×{c}")
                    dup_n += 1
print(f"  重复 {dup_n}")

print("\n=== 4. 危险模式（排除自检脚本自身）===")
PATTERNS = {
    "sys.stdout = ": "全局替换 stdout",
    "sys.stderr = ": "全局替换 stderr",
    "os._exit": "强制退出",
    "shell=True": "shell 注入",
    "verify=False": "关闭 TLS 校验",
    "except Exception: pass": "静默吞异常（单行写法）",
}
hits = 0
for f in py_files:
    if f.name == "self_check.py":
        continue
    text = f.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        for pat, why in PATTERNS.items():
            if pat in line:
                hits += 1
                if VERBOSE:
                    print(f"   {f.relative_to(ROOT)}:{i} {pat.strip()} → {why}")
print(f"  命中 {hits}（--verbose 看明细）")

print("\n=== 5. 密钥扫描（排除 .git / data / dist）===")
KEY_RE = re.compile(r"(sk-[A-Za-z0-9._\-]{14,}|AKID[A-Za-z0-9]{12,})")
leaks = 0
for p in ROOT.rglob("*"):
    if not p.is_file() or p.suffix.lower() not in {".py", ".json", ".md", ".cmd", ".txt", ".yml", ".ini"}:
        continue
    if any(part in SKIP_DIRS for part in p.parts):
        continue
    try:
        text = p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        continue
    for i, line in enumerate(text.splitlines(), 1):
        if KEY_RE.search(line) and "sk-xxx" not in line.lower() and "sk-..." not in line:
            tracked = "config.json" == p.name
            tag = "(gitignore，未入库)" if tracked else ""
            add(f"疑似密钥 {p.relative_to(ROOT)}:{i} {tag}")
            leaks += 1
print(f"  命中 {leaks}")

print("\n=== 6. JSON 配置可解析 ===")
json_bad = 0
for name in ("config.json", "config.example.json", "boss_vision/kb_positions.json",
             "pyrightconfig.json", ".vscode/settings.json"):
    p = ROOT / name
    if not p.exists():
        continue
    try:
        json.loads(p.read_text(encoding="utf-8-sig"))
        if VERBOSE:
            print(f"   ✓ {name}")
    except Exception as exc:
        add(f"JSON 解析失败 {name}: {exc}")
        json_bad += 1
print(f"  失败 {json_bad}")

print("\n=== 7. 核心模块导入 ===")
sys.path.insert(0, str(ROOT / "boss_vision"))
core = ["settings", "kb", "screening", "judge", "judge_inline", "vlm", "vision",
        "cdp", "winenv", "driver", "resume_reader", "export_csv", "textfix"]
ok = 0
for m in core:
    try:
        __import__(m)
        ok += 1
    except Exception as exc:
        add(f"导入失败 {m}: {type(exc).__name__}: {exc}")
print(f"  成功 {ok}/{len(core)}")

print("\n=== 8. 工具脚本导入（tools/*.py 顶层可编译）===")
tool_bad = 0
for f in sorted((ROOT / "tools").glob("*.py")):
    try:
        compile(f.read_text(encoding="utf-8"), str(f), "exec")
    except Exception as exc:
        add(f"工具脚本编译失败 {f.name}: {exc}")
        tool_bad += 1
print(f"  失败 {tool_bad}")

print("\n=== 9. 运行配置一致性 ===")
try:
    import judge
    import vlm
    v = vlm.load_config()
    c = judge.struct_config()
    j = judge.load_config()
    print(f"  pipeline={v.get('pipeline')}  ocr={v.get('ocr_model')}")
    print(f"  struct={c.get('model')}  judge={j.get('model')}")
    print(f"  vision key: {'已配置' if vlm.api_key(v) else '缺失'}")
    print(f"  judge  key: {'已配置' if judge.available() else '缺失'}")
except Exception as exc:
    add(f"配置检查失败: {exc}")

print("\n=== 10. 岗位就绪状态 ===")
try:
    import kb
    for n in kb.position_names():
        print(f"  {n}: ready={kb.is_ready(n)}")
    print(f"  当前 {kb.current_position()}  锁定 {kb.position_locked()}")
except Exception as exc:
    add(f"岗位检查失败: {exc}")

print("\n" + "=" * 46)
if PROBLEMS:
    print(f"自检完成：{len(PROBLEMS)} 项待关注")
    for i, p in enumerate(PROBLEMS[:20], 1):
        print(f"  {i}. {p}")
else:
    print("自检完成：未发现问题")
