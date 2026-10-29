#!/usr/bin/env bash
# 启动带调试端口的 Chrome/Edge（独立 profile），并打开推荐牛人页
set -e
cd "$(dirname "$0")"

URL="https://www.zhipin.com/web/chat/recommend"
PROFILE="$HOME/Library/Application Support/boss-cdp-profile"

BROWSER=""
for p in \
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  "/Applications/Chromium.app/Contents/MacOS/Chromium"
do
  if [ -x "$p" ]; then BROWSER="$p"; break; fi
done

if [ -z "$BROWSER" ]; then
  echo "没找到 Chrome / Edge / Chromium。"
  echo "请先安装 Google Chrome：https://www.google.com/chrome/"
  read -r -p "按回车退出..." _
  exit 1
fi

echo "使用浏览器: $BROWSER"
echo "配置目录:   $PROFILE"
echo "调试端口:   9222"
echo
echo "正在启动…（首次需要在这个浏览器里登录 BOSS）"

"$BROWSER" \
  --remote-debugging-port=9222 \
  --user-data-dir="$PROFILE" \
  --no-first-run \
  --no-default-browser-check \
  --disable-backgrounding-occluded-windows \
  --disable-renderer-backgrounding \
  --disable-features=CalculateNativeWinOcclusion \
  "$URL" &

sleep 4
echo
echo "检查端口…"
if curl -s --max-time 3 http://127.0.0.1:9222/json/version >/dev/null; then
  echo "✓ 调试端口可用"
else
  echo "✗ 调试端口不可用。请完全退出 Chrome 后重试。"
fi
echo
read -r -p "按回车关闭本窗口（浏览器会继续运行）..." _
