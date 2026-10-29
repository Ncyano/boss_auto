#!/usr/bin/env bash
# macOS 安装脚本（离线）：使用包内 wheelhouse，无需联网
set -e
cd "$(dirname "$0")"

echo "=== BOSS 招聘助手 · macOS 安装（离线依赖）==="

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3。请先安装："
  echo "  xcode-select --install                      # 自带 python3"
  echo "  或 https://www.python.org/downloads/macos/"
  exit 1
fi
echo "python3: $(python3 -V 2>&1)"
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    print("需要 Python 3.10 以上（建议 3.11+）")
    raise SystemExit(1)
PY

ARCH="$(uname -m)"
case "$ARCH" in
  arm64) WH="wheelhouse/arm64" ;;
  x86_64) WH="wheelhouse/x86_64" ;;
  *) echo "未知架构 $ARCH"; exit 1 ;;
esac

if [ ! -d "$WH" ]; then
  echo "缺少离线依赖目录 $WH"
  exit 1
fi
echo "CPU 架构: $ARCH  →  $WH"
echo "wheel 数量: $(ls -1 "$WH"/*.whl 2>/dev/null | wc -l | tr -d ' ')"

if [ ! -d .venv ]; then
  echo "创建虚拟环境 .venv ..."
  python3 -m venv .venv
fi

echo "离线安装依赖 ..."
./.venv/bin/python -m pip install --no-index --find-links "$WH" --upgrade pip setuptools wheel >/dev/null 2>&1 || true
./.venv/bin/python -m pip install --no-index --find-links "$WH" \
    numpy pillow openpyxl opencv-python

echo
echo "自检 ..."
./.venv/bin/python - <<'PY'
import sys
mods = ["numpy", "cv2", "PIL", "openpyxl"]
bad = []
for m in mods:
    try:
        __import__(m)
        print(f"  ✓ {m}")
    except Exception as exc:
        bad.append((m, exc))
        print(f"  ✗ {m}: {exc}")
sys.path.insert(0, "boss_vision")
try:
    import cdp
    print("  ✓ cdp")
except Exception as exc:
    bad.append(("cdp", exc))
    print(f"  ✗ cdp: {exc}")
raise SystemExit(1 if bad else 0)
PY

echo
echo "=== 安装完成 ==="
echo "下一步："
echo "  1) cp config.example.json config.json  然后填入两个 API key"
echo "  2) ./run-browser-mac.command   启动浏览器并登录 BOSS"
echo "  3) ./run-mac.command           启动控制台"
echo
echo "注意：macOS 只支持【后台模式】（CDP），浏览器窗口不能最小化。"
