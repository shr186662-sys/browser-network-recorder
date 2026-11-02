# Browser Network Recorder

轻量浏览器网络录制工具，支持 **macOS（Apple Silicon / Intel）和 Windows**。打开独立 Chrome，手动登录、操作网页，将多个标签页的 HTTP 请求、响应和 WebSocket 帧录制为 JSONL，供接口排查、脚本处理和 AI 辅助分析使用。

A standalone desktop recorder for Chrome on macOS and Windows. Manually browse a site and capture multi-tab HTTP requests, response bodies, and WebSocket frames as local JSONL files. Built with Python, Tkinter, Selenium and Chrome DevTools Protocol.

[![Tests](https://github.com/shr186662-sys/browser-network-recorder/actions/workflows/test.yml/badge.svg)](https://github.com/shr186662-sys/browser-network-recorder/actions/workflows/test.yml)

## 功能

- 图形界面：启动浏览器、开始/结束监听、打开输出目录。
- 每个标签页独立 CDP 连接，自动发现新标签页，不切换当前标签页。
- 记录请求方法、URL、请求头、正文、响应状态、响应头和可获取的响应正文。
- 记录重定向、失败请求，以及监听期间建立的 WebSocket 的文本/二进制收发帧；原始模式下二进制帧以 Base64 保存。
- 按 Fetch/XHR、文档、JS、CSS、图片等类型筛选；提供通用登录分析筛选。
- 默认脱敏；可在开始前取消勾选，录制原始数据。
- 从当前已结束的原始会话导出已录制 JS/CSS，不重新请求网站，不内置任何业务域名或验证码服务。
- Mac 和 Windows 使用同一核心，各有启动入口。全部项目文件均在本仓库，无需其他业务项目、数据库、服务器或账户配置。

## 下载桌面版

在 [Releases](https://github.com/shr186662-sys/browser-network-recorder/releases) 下载对应 ZIP 并**完整解压**：

| 设备 | 文件 | 启动 |
| --- | --- | --- |
| Apple Silicon Mac | `BrowserNetworkRecorder-mac-arm64.zip` | `BrowserNetworkRecorder.app` |
| Intel Mac | `BrowserNetworkRecorder-mac-intel.zip` | `BrowserNetworkRecorder.app` |
| Windows x64 | `BrowserNetworkRecorder-windows-x64.zip` | 文件夹内 `BrowserNetworkRecorder.exe` |

桌面包包含 Python 运行时与第三方依赖，仍需安装 Google Chrome，首次启动驱动需要联网。桌面构建未购买签名证书、未做 Apple 公证，系统可能阻止直接打开；不要全局关闭系统安全保护。如果无法通过系统允许的方式打开，可以使用下面的源码方式。桌面包由 GitHub Actions 按平台构建，具体构建结果以 Releases 和 Actions 为准。

## 源码启动

准备 Google Chrome 和 **Python 3.11 或以上（含 Tkinter，推荐 python.org 安装包）**。下载本仓库 ZIP 后先解压到可写目录。源码启动第一次会联网从 PyPI 安装 `requirements.txt` 到本目录 `.venv`。

### macOS

双击 `start_mac.command`；或者终端执行：

```bash
cd browser-network-recorder
bash start_mac.command
```

支持 Intel 和 Apple Silicon。Chrome 放在系统或个人「应用程序」目录。若 Python 不含 Tkinter，安装含 Tcl/Tk 的 Python，或为现有 Python 单独安装匹配的 Tkinter。

### Windows

安装 Python 时勾选 **Add python.exe to PATH**，保留 Tcl/Tk 组件。双击 `start_windows.bat`。支持 `py -3` 和 `python`。

### 手动安装（两平台通用）

```bash
python -m venv .venv
# macOS
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python network_capture.py
# Windows
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe network_capture.py
```

macOS 的系统命令可能是 `python3`。依赖均为公开 Python 包：Selenium、Requests、websocket-client；驱动由 Selenium 自带的 Selenium Manager 管理。

## 使用

1. 点击「启动 Chrome」，等待驱动准备完成。
2. 设置筛选和脱敏选项，点击「开始监听」。**监听开始之后**再进行待录制操作。
3. 在工具新开的 Chrome 中手动登录并操作网页；允许打开新标签页。
4. 等待请求完成，点击「结束监听」，再打开输出目录。
5. 需要导出 JS/CSS 时，在原始模式下用「全部」或对应类型录制，然后结束监听并点击「导出 JS/CSS」。

筛选和脱敏设置在每次开始时固定，运行中修改筛选将在下一次录制生效。停止时会等待已接收事件处理完毕，大量请求时可能需要等待。

录制浏览器使用独立临时配置，**不会复用日常 Chrome 的登录状态**。程序只监听自己启动的浏览器，不监听手机、其他桌面应用或整个系统。

## 输出

默认保存在用户主目录 `BrowserNetworkRecorder/时间戳/`，不会写入 Git 仓库：

```text
session.json       开始时间、起始 URL、筛选和脱敏状态
requests.jsonl     每行一条 HTTP 请求或 WebSocket 事件
summary.json       结束时间、记录总数、WebSocket 帧数和跳过数量
assets/            手动导出的 JS/CSS 和 manifest.json
```

`summary.json` 的 `request_count` 是全部已保存事件数，包含 WebSocket 帧，不仅是 HTTP 请求数。`targetId` 与 `requestId` 共同定位来源标签页的 HTTP 请求。

示例（人工构造）：

```json
{"timestamp":"2026-01-01T12:00:00.000","url":"https://example.test/api/demo","method":"GET","status":200,"response_body":"{\"ok\":true}","redacted":true}
```

## 隐私与边界

- 默认将 URL 查询值、片段、用户信息，以及非常用请求/响应头隐藏；JSON 正文递归隐藏常见凭据和个人信息字段；表单隐藏全部值。
- 默认省略非 JSON 正文和 WebSocket 载荷。需要完整正文、JS/CSS 或无损二进制时，在开始前取消脱敏。原始录制可能包含密码、Cookie、Token、客户资料等，应只保存在受控环境。
- **脱敏是尽力处理，不是匿名化保证。** URL 路径、自定义 JSON 字段及自由文本仍可能包含敏感信息。分享给他人、AI 服务或提交 Issue 前，必须人工检查；不要上传真实登录会话。
- 本工具不主动上传录制内容，也不向作者发送录制数据。浏览器会访问你打开的网站；pip 和 Selenium Manager 会访问包/驱动分发服务。程序默认关闭 Selenium Manager 使用统计；其他依赖和 Chrome 行为由相应软件控制。
- 只在有权访问和调试的环境使用；工具不提供验证码破解、自动登录或访问权限绕过。

## 已知限制

- 新标签页约每 0.5 秒发现一次；接入前的最早请求可能漏录，需等待接入日志后刷新或重做操作。
- 无法恢复开始监听前的请求/帧。已存在 WebSocket 可能录到帧但没有 URL；不覆盖 Worker、Service Worker 或子框架独立目标的全部流量。
- HTTP 正文最多保存 500,000 字符，超出会标记截断；响应正文可能因缓存、标签页关闭或 Chrome 回收而读取失败；重定向中间响应不保证有正文。
- 目前 JSONL 导出，不提供 HAR、请求回放、瀑布图或协议层完整抓包。二进制 HTTP 正文按文本尽力解码；只有原始 WebSocket 二进制载荷保证按 CDP Base64 保存。
- 登录分析依赖关键词和请求类型，可能漏录；完整排查请用「全部」。JS/CSS 导出只包含已录制且可读取的正文，可能被截断，并不保证资源可独立运行。
- 自动化测试不等于每个系统版本的人工验收；请在自己设备上先用测试页面验证。

## 网络与自定义路径

初次驱动下载和 Chrome 大版本更新需要访问官方驱动分发服务。下载时会读取系统 HTTP/HTTPS 代理；此设置只用于驱动准备，不强行设置 Chrome 代理。CDP 的本机 HTTP 连接不经过环境代理。

可选环境变量：

| 变量 | 用途 |
| --- | --- |
| `NETWORK_RECORDER_OUTPUT` | 录制输出目录 |
| `NETWORK_RECORDER_CHROME` | 自定义 Chrome 可执行文件路径 |
| `NETWORK_RECORDER_DRIVER` | 已下载且与 Chrome 匹配的 chromedriver 路径，用于离线环境 |
| `NETWORK_RECORDER_PYTHON` | 源码启动器使用的 Python 可执行文件路径 |

不要将驱动的调试端口开放给其他机器。

## 开发、测试与构建

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m pip install pyinstaller
python build.py
```

构建结果位于 `dist/`。GitHub Actions 在 Mac、Windows、Linux 上执行单元测试；推送 `v*` 标签分别构建 Apple Silicon、Intel Mac 和 Windows x64 桌面包，全部成功后发布 Release。

真实 Chrome 集成测试需要额外安装 `websockets`，并设置 `RECORDER_INTEGRATION=1` 后运行测试。测试只使用本机人工构造的 HTTP/WebSocket 服务，不登录业务网站。

欢迎提交包含复现步骤、系统/Python/Chrome 版本的 Issue，请使用测试域名和人工数据。

## License

MIT。第三方依赖遵循各自许可证。
