#!/usr/bin/env bash
# 双击即用：首次自动解压运行时，然后直接启动控制台（不跑 pip、不建 venv）
set -e
cd "$(dirname "$0")"

ARCH="$(uname -m)"
case "$ARCH" in
  arm64)  PYTGZ="python/arm64/python.tar.gz" ; LIBS="libs/arm64" ;;
  x86_64) PYTGZ="python/x86_64/python.tar.gz"; LIBS="libs/x86_64" ;;
  *) echo "未知架构：$ARCH"; read -r -p "回车退出..." _; exit 1 ;;
esac

# 1) 首次运行：解压包内 Python 运行时
if [ ! -x "python/bin/python3" ]; then
  if [ -f "$PYTGZ" ]; then
    echo "首次运行：解压内置 Python 运行时（约 2 秒）..."
    TMPD="$(mktemp -d)"
    tar -xzf "$PYTGZ" -C "$TMPD"
    rm -rf python
    mv "$TMPD/python" python
    rmdir "$TMPD" 2>/dev/null || true
    # 去掉 Gatekeeper 隔离标记（否则首次执行可能被拦）
    xattr -dr com.apple.quarantine python 2>/dev/null || true
    chmod +x python/bin/* 2>/dev/null || true
  else
    echo "✗ 缺少内置运行时：$PYTGZ"
    echo "  请重新解压完整安装包。"
    read -r -p "回车退出..." _
    exit 1
  fi
fi

chmod +x python/bin/python3 2>/dev/null || true

# 2) 依赖已经在 libs/ 里（预解压），用 PYTHONPATH 加载，无需 pip
export PYTHONPATH="$PWD/$LIBS"
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

# 3) 首次运行生成配置文件
if [ ! -f config.json ] && [ -f config.example.json ]; then
  cp config.example.json config.json
fi

# 4) 提示端口状态（不阻塞）
if curl -s --max-time 2 http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
  echo "✓ 调试端口 9222 可用"
else
  echo "⚠ 调试端口不可用：请先双击 run-browser-mac.command 启动浏览器"
  echo "  （程序仍会打开，但点开始会提示找不到页面）"
fi
echo

exec python/bin/python3 boss_vision/gui.py
