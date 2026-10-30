#!/usr/bin/env bash
# macOS 安装脚本：优先使用包内自带的 Python 运行时（无需系统 Python、无需管理员）
set -e
cd "$(dirname "$0")"

echo "=== BOSS 招聘助手 · macOS 安装（全离线）==="
echo

ARCH="$(uname -m)"
case "$ARCH" in
  arm64)  PYDIR="python/arm64" ; WH="wheelhouse/arm64" ;;
  x86_64) PYDIR="python/x86_64"; WH="wheelhouse/x86_64" ;;
  *) echo "未知架构：$ARCH"; exit 1 ;;
esac
echo "CPU 架构 : $ARCH"
echo "运行时   : $PYDIR"
echo "离线依赖 : $WH"
echo

# ---------- 1. 准备 Python ----------
PY=""
if [ -x "python/bin/python3" ]; then
  PY="python/bin/python3"
  echo "✓ 使用包内 Python 运行时"
elif [ -f "$PYDIR/python.tar.gz" ]; then
  echo "解压内置 Python 运行时 ..."
  TMPD="$(mktemp -d)"
  tar -xzf "$PYDIR/python.tar.gz" -C "$TMPD"
  rm -rf python
  mv "$TMPD/python" python
  rmdir "$TMPD" 2>/dev/null || true
  PY="python/bin/python3"
  echo "✓ 已解压到 ./python"
elif command -v python3.13 >/dev/null 2>&1; then
  PY="$(command -v python3.13)"
  echo "✓ 使用系统 python3.13：$PY"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
  echo "✓ 使用系统 python3：$PY"
else
  echo "✗ 包里没有 Python 运行时，系统也没有 python3。"
  echo "  请重新解压完整包，或安装 Python 3.13："
  echo "    https://www.python.org/downloads/macos/"
  exit 1
fi

echo -n "Python 版本："
"$PY" -V
"$PY" - <<'PYCHK'
import sys
if sys.version_info[:2] < (3, 11):
    print("需要 Python 3.11+（离线 wheel 是 3.13 编的，版本不符请用包内运行时）")
    raise SystemExit(1)
PYCHK

# ---------- 2. 建虚拟环境 ----------
if [ ! -x .venv/bin/python ]; then
  echo "创建虚拟环境 .venv ..."
  "$PY" -m venv .venv
fi
VENVPY="./.venv/bin/python"
"$VENVPY" -V

# ---------- 3. 离线安装依赖 ----------
if [ ! -d "$WH" ]; then
  echo "✗ 缺少离线依赖目录 $WH"
  exit 1
fi
echo "离线安装依赖（wheel 数量：$(ls -1 "$WH"/*.whl 2>/dev/null | wc -l | tr -d ' ')）..."
"$VENVPY" -m pip install --no-index --find-links "$WH" --upgrade pip >/dev/null 2>&1 || true
"$VENVPY" -m pip install --no-index --find-links "$WH" \
    numpy pillow openpyxl opencv-python

# ---------- 4. 自检 ----------
echo
echo "自检 ..."
"$VENVPY" - <<'PYCHK'
import sys
bad = []
for m in ("numpy", "cv2", "PIL", "openpyxl"):
    try:
        __import__(m)
        print(f"  ✓ {m}")
    except Exception as exc:
        bad.append(m)
        print(f"  ✗ {m}: {exc}")
sys.path.insert(0, "boss_vision")
for m in ("cdp", "kb", "screening", "judge", "winenv", "driver"):
    try:
        __import__(m)
        print(f"  ✓ {m}")
    except Exception as exc:
        bad.append(m)
        print(f"  ✗ {m}: {exc}")
raise SystemExit(1 if bad else 0)
PYCHK

# ---------- 5. 配置文件 ----------
if [ ! -f config.json ] && [ -f config.example.json ]; then
  cp config.example.json config.json
  echo
  echo "已生成 config.json —— 请填入两个 API key："
  echo "  vision.api_key  通义千问（OCR）"
  echo "  judge.api_key   DeepSeek（结构化 + 判定）"
fi

echo
echo "=== 安装完成 ==="
echo "下一步："
echo "  1) 编辑 config.json 填入 API key（可用 open -e config.json）"
echo "  2) ./run-browser-mac.command   启动浏览器并登录 BOSS"
echo "  3) ./run-mac.command           启动控制台"
echo
echo "提示：macOS 只能用【后台模式】（CDP），浏览器窗口不要最小化。"
