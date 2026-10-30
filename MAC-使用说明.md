# BOSS 招聘助手 · macOS 版说明

> **重要**：macOS 与 Windows 版差异很大，请先读第 1 节。

## 1. 与 Windows 版的差别

| 项目 | Windows | macOS |
|---|---|---|
| 抓图方式 | 屏幕抓图 或 CDP | **只能用 CDP（后台模式）** |
| 鼠标注入 | `SendInput` | 不支持（没有 Win32 API） |
| 窗口定位 | 按窗口标题枚举 | 整个浏览器视口即坐标系 |
| 浏览器 | Edge / Chrome | **Chrome / Edge / Chromium 均可** |
| 打包形式 | 自带 Python 运行时 | **源码包，需本机安装 Python 3.10+** |

因为 macOS 没有 Win32 的窗口枚举和鼠标注入，所以：

- **必须勾选「后台模式」**（下拉框默认已选，抓图=CDP）
- 点击通过 CDP 的 `Input.dispatchMouseEvent` 派发，**不会移动你的真实鼠标**
- 浏览器窗口**不能被最小化**（最小化后 Chromium 停止渲染，截图会超时）

## 2. 安装（一次性，全离线，**不需要系统 Python**）

```bash
cd BOSS招聘助手-macOS源码-<时间>
chmod +x install-mac.sh run-mac.command run-browser-mac.command
./install-mac.sh
```

包里自带 **Python 3.13 运行时**（python-build-standalone），解压到 `./python`
即可使用，**不需要系统装 Python，也不需要管理员权限**。

安装脚本的处理顺序：

1. 若 `./python/bin/python3` 已存在 → 直接使用
2. 否则解压 `python/<arch>/python.tar.gz` 到 `./python`
3. 若包里没有运行时 → 回退系统 `python3.13` / `python3`
4. 用该 Python 建 `.venv`，再用 `wheelhouse/<arch>` **离线**装依赖
5. 自检 numpy / cv2 / PIL / openpyxl / cdp / kb / screening / judge / winenv / driver

包内结构：

```
python/
├── arm64/python.tar.gz      Apple Silicon 用的 Python 3.13（24 MB）
└── x86_64/python.tar.gz     Intel Mac 用的 Python 3.13（24 MB）

wheelhouse/
├── arm64/    numpy / opencv / pillow / openpyxl / et_xmlfile
└── x86_64/   同上
```

> 首次运行 `./python/bin/python3` 时，macOS 可能提示「无法验证开发者」。
> 解决办法（任选其一）：
> ```bash
> xattr -dr com.apple.quarantine ./python
> ```
> 或 系统设置 → 隐私与安全性 → 仍要打开。

## 3. 配置

```bash
cp config.example.json config.json
open -e config.json
```

填两个 key：

```json
"vision": { "api_key": "sk-...（通义千问）" },
"judge":  { "api_key": "sk-...（DeepSeek）" }
```

`struct` 段会自动复用 `judge` 的 key（默认已是 deepseek-flash）。

## 4. 每次运行（两步）

**第一步：启动浏览器**

```bash
./run-browser-mac.command
```

- 用独立配置目录 `~/Library/Application Support/boss-cdp-profile`
- 带 `--remote-debugging-port=9222`
- 自动打开「推荐牛人」页
- **首次需要在这个浏览器里扫码登录 BOSS**

**第二步：启动控制台**

```bash
./run-mac.command
```

会先检查 9222 端口，然后打开 GUI。

## 5. 使用要点

```
✓ 勾选「后台模式」（默认已勾）
✓ 招聘岗位选「运营助理」
✓ 目标人数、间隔按需填
✗ 不要最小化浏览器窗口（可以被别的窗口遮挡）
✗ 不要用同一个账号在别的浏览器/手机登录（会顶号）
```

## 6. 常见问题

**Q: 提示「调试端口不可用」**
A: 完全退出 Chrome（`Cmd+Q`），再运行 `run-browser-mac.command`。Chrome 已经在运行时，新启动会被它接管，不会开新端口。

**Q: 提示「当前 CDP 页签不是 BOSS 页面」**
A: 浏览器里有多个标签页时，程序会挑 BOSS 的页签。请确认至少有一个标签停在 `zhipin.com`。

**Q: 点击没反应**
A: mac 版点击走 CDP，不移动真实鼠标。如果页面元素被遮挡（比如弹了遮罩），先按 Esc 或手动关掉弹窗。

**Q: 能不能不开浏览器直接跑？**
A: 不能。所有操作都通过浏览器调试协议完成。

**Q: 打包成 .app 双击运行？**
A: 需要在 Mac 上用 PyInstaller 构建（Windows 上无法交叉编译）。可以：

```bash
./.venv/bin/pip install pyinstaller
./.venv/bin/pyinstaller --windowed --name "BOSS招聘助手" \
  --paths boss_vision \
  --add-data "boss_vision:boss_vision" \
  boss_vision/gui.py
```

产物在 `dist/BOSS招聘助手.app`。

## 7. 目录说明

```
install-mac.sh            安装脚本
run-mac.command           启动控制台
run-browser-mac.command   启动带调试端口的浏览器
config.example.json       配置模板（复制为 config.json）
boss_vision/
├── winenv.py             平台分发层（自动选 winenv_win / winenv_mac）
├── winenv_mac.py         macOS 实现（纯 CDP）
├── winenv_win.py         Windows 实现
├── gui.py                图形界面
├── driver.py             采集主循环
└── data/                 学校名单等
mcp_server.py             MCP 服务端（可被 Claude 等客户端调用）
```
