#!/usr/bin/env bash
# 启动图形控制台（macOS）
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "还没安装依赖，先运行："
  echo "  ./install-mac.sh"
  read -r -p "按回车退出..." _
  exit 1
fi

export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

echo "检查调试端口 9222 …"
if curl -s --max-time 3 http://127.0.0.1:9222/json/version >/dev/null; then
  echo "✓ 端口可用（后台模式就绪）"
else
  echo "✗ 端口不可用：请先运行 run-browser-mac.command 启动浏览器"
fi
echo

exec ./.venv/bin/python boss_vision/gui.py
