#!/usr/bin/env bash
# macOS 安装脚本：建虚拟环境 + 装依赖
set -e
cd "$(dirname "$0")"

echo "=== BOSS 招聘助手 · macOS 安装 ==="

if ! command -v python3 >/dev/null 2>&1; then
  echo "未找到 python3。请先安装：https://www.python.org/downloads/macos/"
  echo "或执行：xcode-select --install"
  exit 1
fi

PYV=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
echo "python3 版本: $PYV"
python3 - <<'PY'
import sys
if sys.version_info < (3, 10):
    print("需要 Python 3.10 以上")
    raise SystemExit(1)
PY

if [ ! -d .venv ]; then
  echo "创建虚拟环境 .venv ..."
  python3 -m venv .venv
fi

echo "安装依赖（清华镜像）..."
./.venv/bin/python -m pip install --upgrade pip -q -i https://pypi.tuna.tsinghua.edu.cn/simple
./.venv/bin/python -m pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple \
    numpy opencv-python pillow openpyxl

echo
echo "自检..."
./.venv/bin/python -c "import numpy, cv2, PIL, openpyxl; print('依赖 OK')"
./.venv/bin/python -c "import sys; sys.path.insert(0,'boss_vision'); import cdp; print('cdp OK')"

echo
echo "=== 安装完成 ==="
echo "下一步："
echo "  1) 编辑 app 目录下的 config.json（由 config.example.json 复制）填两个 key"
echo "  2) 双击 run-mac.command 启动控制台"
echo
echo "注意：macOS 只支持【后台模式】（CDP）。浏览器必须用 run-browser-mac.command 启动。"
