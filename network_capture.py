"""
浏览器网络请求录制工具（Chrome + Selenium）。

用法：
  python network_capture.py

1. 点击「启动 Chrome」打开浏览器
2. 配置筛选（类似 DevTools Network 面板），或点击「启用登录分析模式」一键过滤
3. 点击「开始监听」记录符合条件的网络请求和 WebSocket 收发帧
4. 在浏览器中手动操作
5. 点击「结束监听」停止记录；数据保存在 network_capture_output/ 下
6. 可从未脱敏的会话导出已录制的 JS/CSS 资源

多标签页：每个标签页独立 CDP 连接，轮询发现新标签，不会切换标签页。
"""

from __future__ import annotations

import base64
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import getproxies

from privacy import redact_record, redact_url
from assets import export_assets

import requests
from websocket import WebSocketApp

ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT = Path(os.environ.get("NETWORK_RECORDER_OUTPUT", str(Path.home() / "BrowserNetworkRecorder")))
MAX_BODY_CHARS = 500_000
USER_AGENT = ""

# 与 Chrome DevTools Network 面板类似的类型标签
INCLUDE_FILTER_OPTIONS: list[tuple[str, str]] = [
    ("all", "全部"),
    ("fetch_xhr", "Fetch / XHR"),
    ("doc", "文档 (Doc)"),
    ("js", "JS"),
    ("css", "CSS"),
    ("img", "图片 (Img)"),
    ("media", "媒体 (Media)"),
    ("font", "字体 (Font)"),
    ("ws", "WebSocket (WS)"),
    ("other", "其他"),
]

INCLUDE_FILTER_LABELS = {k: v for k, v in INCLUDE_FILTER_OPTIONS}

# 登录分析模式：跳过静态资源、预检、埋点及登录后的业务噪音
LOGIN_ANALYSIS_SKIP_CATEGORIES = frozenset({"js", "css", "img", "font", "media", "ws"})
LOGIN_ANALYSIS_SKIP_METHODS = frozenset({"OPTIONS"})
LOGIN_ANALYSIS_SKIP_URL_PARTS = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".woff",
    ".woff2",
    ".svg",
    ".ico",
    ".map",
    "googletagmanager",
    "google-analytics",
    "googleapis.com",
    "gstatic.com",
    "datadoghq",
    "sentry.io",
    "hm.baidu.com",
    "cnzz.com",
    "arms-retcode.aliyuncs.com",
    "sockjs",
    "stomp",

)
LOGIN_ANALYSIS_KEEP_URL_PARTS = (
    "login",
    "auth",
    "oauth",
    "cas",
    "sso",
    "callback",
    "token",
    "captcha",
    "geetest",
    "verify",
    "validate",
    "password",
    "code=",
    "profile",
    "member",
    "caslogin",
    "switchdealer",
    "serviceinfo",
    "whoami",
    "user-info",
    "msso",
    "keypair",
    "refresh",
    "sign",
    "org_list",
    "session",
)


class TabCdpSession:
    """单个标签页的 CDP WebSocket 会话。"""

    def __init__(
        self,
        ws_url: str,
        target_id: str,
        target_title: str,
        event_queue: queue.Queue[dict[str, Any]],
    ) -> None:
        self.ws_url = ws_url
        self.target_id = target_id
        self.target_title = target_title
        self.event_queue = event_queue
        self._msg_id = 0
        self._io_lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}
        self._waiters: dict[int, threading.Event] = {}
        self._ws: WebSocketApp | None = None
        self._ws_thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._running = False

    def start(self, timeout: float = 10.0) -> None:
        if self._running:
            return
        self._running = True
        self._ws = WebSocketApp(
            self.ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws_thread = threading.Thread(
            target=lambda: self._ws.run_forever(suppress_origin=True),
            name=f"cdp-tab-{self.target_id[:8]}",
            daemon=True,
        )
        self._ws_thread.start()
        if not self._ready.wait(timeout):
            self.stop()
            raise TimeoutError(f"标签页 CDP 连接超时：{self.target_title}")

    def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._ws_thread is not None:
            self._ws_thread.join(timeout=2.0)
        self._ws = None
        self._ws_thread = None

    def get_response_body(self, request_id: str, *, timeout: float = 10.0) -> dict[str, Any]:
        result = self._send("Network.getResponseBody", {"requestId": request_id}, timeout=timeout)
        if "error" in result:
            raise RuntimeError(str(result["error"]))
        return result.get("result") or {}

    def _on_open(self, _ws) -> None:
        threading.Thread(target=self._enable_network, name=f"cdp-init-{self.target_id[:8]}", daemon=True).start()

    def _enable_network(self) -> None:
        try:
            self._send("Network.enable", {})
            self._ready.set()
        except Exception:
            self._running = False

    def _on_error(self, _ws, _error) -> None:
        self._running = False

    def _on_close(self, _ws, _status, _msg) -> None:
        self._running = False

    def _on_message(self, _ws, message: str) -> None:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            return

        msg_id = data.get("id")
        if msg_id is not None:
            waiter = self._waiters.get(int(msg_id))
            if waiter:
                self._pending[int(msg_id)] = data
                waiter.set()
            return

        method = data.get("method") or ""
        if self._running and method.startswith("Network."):
            data["_target_id"] = self.target_id
            self.event_queue.put(data)

    def _send(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 10.0) -> dict[str, Any]:
        if self._ws is None:
            raise RuntimeError("CDP WebSocket 未连接")
        with self._io_lock:
            self._msg_id += 1
            msg_id = self._msg_id
        payload: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        waiter = threading.Event()
        self._waiters[msg_id] = waiter
        try:
            self._ws.send(json.dumps(payload))
            if not waiter.wait(timeout):
                raise TimeoutError(f"CDP 命令超时：{method}")
            return self._pending.pop(msg_id, {})
        finally:
            self._waiters.pop(msg_id, None)


class MultiTabNetworkMonitor:
    """轮询 /json/list，为每个标签页建立独立 CDP 连接，无需切换标签。"""

    def __init__(self, debugger_address: str, *, on_log: Callable[[str], None] | None = None) -> None:
        self.debugger_address = debugger_address
        self._on_log = on_log
        self.event_queue: queue.Queue[dict[str, Any]] = queue.Queue()
        self._sessions: dict[str, TabCdpSession] = {}
        self._lock = threading.Lock()
        self._running = threading.Event()
        self._poll_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running.is_set():
            return
        self._running.set()
        self._refresh_targets()
        self._poll_thread = threading.Thread(target=self._poll_loop, name="cdp-tab-poll", daemon=True)
        self._poll_thread.start()

    def pause(self) -> None:
        self._running.clear()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=12)
        with self._lock:
            for session in self._sessions.values():
                session._running = False

    def stop(self) -> None:
        self._running.clear()
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.stop()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
        self._poll_thread = None

    def get_response_body(self, target_id: str, request_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(target_id)
        if session is None:
            raise RuntimeError("标签页 CDP 会话不存在")
        return session.get_response_body(request_id)

    def _log(self, message: str) -> None:
        if self._on_log:
            self._on_log(message)

    def _poll_loop(self) -> None:
        while self._running.is_set():
            try:
                self._refresh_targets()
            except Exception as exc:
                self._log(f"刷新标签页列表失败：{exc}")
            time.sleep(0.5)

    def _refresh_targets(self) -> None:
        local = requests.Session()
        local.trust_env = False
        with local:
            resp = local.get(f"http://{self.debugger_address}/json/list", timeout=5)
        resp.raise_for_status()
        active_ids: set[str] = set()
        for info in resp.json():
            if info.get("type") != "page":
                continue
            target_id = str(info.get("id") or "")
            ws_url = str(info.get("webSocketDebuggerUrl") or "")
            if not target_id or not ws_url:
                continue
            active_ids.add(target_id)
            with self._lock:
                existing = self._sessions.get(target_id)
                if existing is not None and existing._running:
                    continue
                if existing is not None:
                    self._sessions.pop(target_id)
            if existing is not None:
                existing.stop()
            title = str(info.get("title") or info.get("url") or target_id)
            session = TabCdpSession(ws_url, target_id, title, self.event_queue)
            session.start()
            with self._lock:
                self._sessions[target_id] = session
            self._log(f"标签页已接入监听：{title}")

        with self._lock:
            closed_ids = [tid for tid in self._sessions if tid not in active_ids]
        for target_id in closed_ids:
            with self._lock:
                session = self._sessions.pop(target_id, None)
            if session is not None:
                session.stop()


def resolve_debugger_address(driver) -> str:
    caps = driver.capabilities or {}
    chrome_opts = caps.get("goog:chromeOptions") or {}
    debugger_address = chrome_opts.get("debuggerAddress")
    if not debugger_address:
        raise RuntimeError("无法获取 Chrome debuggerAddress")
    return str(debugger_address)


def classify_request(record: dict[str, Any]) -> str:
    """将请求归类为 DevTools 风格的资源类型。"""
    cdp_type = str(record.get("resource_type") or "").strip().lower()
    mime = str(record.get("mimeType") or "").strip().lower()
    url = str(record.get("url") or "").strip()
    path = urlparse(url).path.lower()

    if cdp_type in ("fetch", "xhr", "preflight"):
        return "fetch_xhr"
    if cdp_type == "websocket":
        return "ws"
    if cdp_type == "document":
        return "doc"
    if cdp_type == "script":
        return "js"
    if cdp_type == "stylesheet":
        return "css"
    if cdp_type == "image":
        return "img"
    if cdp_type == "media":
        return "media"
    if cdp_type == "font":
        return "font"

    if "javascript" in mime or mime.endswith("/ecmascript"):
        return "js"
    if "text/css" in mime:
        return "css"
    if mime.startswith("image/"):
        return "img"
    if mime.startswith("audio/") or mime.startswith("video/"):
        return "media"
    if "font" in mime or mime.endswith("/woff") or mime.endswith("/woff2"):
        return "font"
    if mime.startswith("text/html") or mime.startswith("application/xhtml"):
        return "doc"

    if path.endswith(".js") or ".js?" in path:
        return "js"
    if path.endswith(".css") or ".css?" in path:
        return "css"
    if any(path.endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico")):
        return "img"
    if any(path.endswith(ext) for ext in (".woff", ".woff2", ".ttf", ".otf", ".eot")):
        return "font"

    http_method = str(record.get("method") or "").upper()
    if http_method in ("POST", "PUT", "PATCH", "DELETE") and not path.endswith((".js", ".css", ".html")):
        if "json" in mime or "xml" in mime or mime.startswith("application/"):
            return "fetch_xhr"

    return "other"


def is_login_analysis_relevant(record: dict[str, Any]) -> bool:
    """登录分析模式：仅保留 OAuth/鉴权/API 相关请求。"""
    category = classify_request(record)
    record["resource_category"] = category

    method = str(record.get("method") or "").upper()
    if method in LOGIN_ANALYSIS_SKIP_METHODS:
        return False
    if category in LOGIN_ANALYSIS_SKIP_CATEGORIES:
        return False

    url = str(record.get("url") or "").lower()
    if any(part in url for part in LOGIN_ANALYSIS_SKIP_URL_PARTS):
        return False

    if category == "fetch_xhr":
        return True
    if any(part in url for part in LOGIN_ANALYSIS_KEEP_URL_PARTS):
        return True
    if category == "doc" and "code=" in url:
        return True
    return False


def should_record_request(
    record: dict[str, Any],
    *,
    include_filter: str,
    exclude_js: bool = False,
    exclude_css: bool = False,
    exclude_fetch_xhr: bool = False,
    exclude_static: bool = False,
    login_analysis_mode: bool = False,
) -> bool:
    if login_analysis_mode:
        return is_login_analysis_relevant(record)

    category = classify_request(record)
    record["resource_category"] = category

    if include_filter != "all":
        return category == include_filter

    if exclude_js and category == "js":
        return False
    if exclude_css and category == "css":
        return False
    if exclude_fetch_xhr and category == "fetch_xhr":
        return False
    if exclude_static and category in ("js", "css", "img", "font", "media"):
        return False
    return True


def configure_macos_tk() -> None:
    """Locate bundled Tcl/Tk data in relocatable Python installations (e.g. uv)."""
    if sys.platform != "darwin":
        return
    library = Path(sys.base_prefix) / "lib"
    for variable, pattern, marker in (
        ("TCL_LIBRARY", f"tcl{tk.TclVersion}*", "init.tcl"),
        ("TK_LIBRARY", f"tk{tk.TkVersion}*", "tk.tcl"),
    ):
        for candidate in sorted(library.glob(pattern)):
            if (candidate / marker).is_file():
                os.environ.setdefault(variable, str(candidate))
                break


class NetworkCaptureApp:
    def __init__(self) -> None:
        configure_macos_tk()
        self.root = tk.Tk()
        self.root.title("Browser Network Recorder · 浏览器网络录制")
        self.root.geometry("1000x700")
        self.root.minsize(680, 520)

        self._driver = None
        self._browser_starting = False
        self._user_agent = USER_AGENT
        self._recording = threading.Event()
        self._record_thread: threading.Thread | None = None
        self._session_dir: Path | None = None
        self._jsonl_path: Path | None = None
        self._capture_count = 0
        self._skipped_count = 0
        self._websocket_frame_count = 0
        self._websocket_urls: dict[str, str] = {}
        self._lock = threading.Lock()
        self._debugger_address: str | None = None
        self._cdp_monitor: MultiTabNetworkMonitor | None = None

        self._ui_queue = queue.Queue()
        self._filter_settings = {}
        self._redact = True
        self._build_ui()
        self.root.after(50, self._drain_ui)

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self.root, padding=8)
        toolbar.pack(fill=tk.X)

        ttk.Button(toolbar, text="启动 Chrome", command=self._start_browser).pack(side=tk.LEFT, padx=4)
        self.start_btn = ttk.Button(toolbar, text="开始监听", command=self._start_recording, state=tk.DISABLED)
        self.start_btn.pack(side=tk.LEFT, padx=4)
        self.stop_btn = ttk.Button(toolbar, text="结束监听", command=self._stop_recording, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="打开输出目录", command=self._open_output_dir).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="导出 JS/CSS", command=self._export_assets).pack(side=tk.LEFT, padx=4)
        ttk.Button(toolbar, text="关闭浏览器", command=self._close_browser).pack(side=tk.LEFT, padx=4)

        url_row = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        url_row.pack(fill=tk.X)
        ttk.Label(url_row, text="起始 URL（可选）").pack(side=tk.LEFT, padx=(0, 8))
        self.url_var = tk.StringVar(value="about:blank")
        ttk.Entry(url_row, textvariable=self.url_var, width=60).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(url_row, text="打开", command=self._navigate).pack(side=tk.LEFT, padx=(8, 0))

        filter_frame = ttk.LabelFrame(self.root, text="请求筛选（类似 DevTools Network）", padding=8)
        filter_frame.pack(fill=tk.X, padx=8, pady=(0, 8))

        include_row = ttk.Frame(filter_frame)
        include_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(include_row, text="仅记录").pack(side=tk.LEFT, padx=(0, 8))
        self.include_filter_var = tk.StringVar(value="全部")
        include_combo = ttk.Combobox(
            include_row,
            textvariable=self.include_filter_var,
            values=[label for _, label in INCLUDE_FILTER_OPTIONS],
            width=18,
            state="readonly",
        )
        include_combo.pack(side=tk.LEFT)
        include_combo.bind("<<ComboboxSelected>>", self._on_include_filter_changed)
        ttk.Label(
            include_row,
            text="选「全部」时可配合下方排除项；选具体类型则只保存该类请求。",
            foreground="#666666",
        ).pack(side=tk.LEFT, padx=(12, 0))

        exclude_row = ttk.Frame(filter_frame)
        exclude_row.pack(fill=tk.X)
        ttk.Label(exclude_row, text="排除（仅「全部」时生效）").pack(side=tk.LEFT, padx=(0, 8))

        self.exclude_js_var = tk.BooleanVar(value=False)
        self.exclude_css_var = tk.BooleanVar(value=False)
        self.exclude_fetch_xhr_var = tk.BooleanVar(value=False)
        self.exclude_static_var = tk.BooleanVar(value=False)

        self._exclude_checks: list[tuple[ttk.Checkbutton, tk.BooleanVar]] = []
        for text, var in (
            ("JS", self.exclude_js_var),
            ("CSS", self.exclude_css_var),
            ("Fetch/XHR", self.exclude_fetch_xhr_var),
            ("静态资源 (JS/CSS/Img/Font/Media)", self.exclude_static_var),
        ):
            cb = ttk.Checkbutton(exclude_row, text=text, variable=var)
            cb.pack(side=tk.LEFT, padx=(0, 12))
            self._exclude_checks.append((cb, var))

        login_row = ttk.Frame(filter_frame)
        login_row.pack(fill=tk.X, pady=(8, 0))
        self.login_analysis_mode_var = tk.BooleanVar(value=False)
        self.login_mode_btn = ttk.Button(
            login_row,
            text="启用登录分析模式",
            command=self._toggle_login_analysis_mode,
            width=20,
        )
        self.login_mode_btn.pack(side=tk.LEFT)
        ttk.Label(
            login_row,
            text="过滤静态资源和常见埋点，启发式保留登录/API 请求（可能漏录）",
            foreground="#666666",
        ).pack(side=tk.LEFT, padx=(12, 0))

        self.redact_var = tk.BooleanVar(value=True)
        self.redact_check = ttk.Checkbutton(filter_frame, text="脱敏保存（隐藏凭据字段；非 JSON/表单正文及 WS 载荷省略）", variable=self.redact_var)
        self.redact_check.pack(anchor=tk.W, pady=(8, 0))
        self._include_combo = include_combo

        status_frame = ttk.LabelFrame(self.root, text="状态", padding=8)
        status_frame.pack(fill=tk.X, padx=8, pady=(0, 8))
        self.status_var = tk.StringVar(value="请先启动 Chrome")
        ttk.Label(status_frame, textvariable=self.status_var).pack(anchor=tk.W)
        self.session_var = tk.StringVar(value="输出目录：—")
        ttk.Label(status_frame, textvariable=self.session_var, foreground="#666666").pack(anchor=tk.W, pady=(4, 0))
        self.filter_summary_var = tk.StringVar(value="当前筛选：全部")
        ttk.Label(status_frame, textvariable=self.filter_summary_var, foreground="#666666").pack(
            anchor=tk.W, pady=(4, 0)
        )

        log_frame = ttk.LabelFrame(self.root, text="录制日志", padding=8)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=16, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        self._update_filter_summary()

    def _include_filter_key(self) -> str:
        label = self.include_filter_var.get().strip()
        for key, text in INCLUDE_FILTER_OPTIONS:
            if text == label:
                return key
        return "all"

    def _set_include_filter_key(self, key: str) -> None:
        self.include_filter_var.set(INCLUDE_FILTER_LABELS.get(key, "全部"))

    def _on_include_filter_changed(self, _event=None) -> None:
        if self.login_analysis_mode_var.get():
            self._update_filter_summary()
            return
        only_all = self._include_filter_key() == "all"
        for cb, _var in self._exclude_checks:
            cb.config(state=tk.NORMAL if only_all else tk.DISABLED)
        self._update_filter_summary()

    def _toggle_login_analysis_mode(self) -> None:
        enabled = not self.login_analysis_mode_var.get()
        self.login_analysis_mode_var.set(enabled)
        self._apply_login_analysis_mode_ui(enabled)
        self._update_filter_summary()
        if enabled:
            self._log("已启用登录分析模式：将跳过静态资源、埋点及登录后业务 API")
        else:
            self._log("已关闭登录分析模式，恢复手动筛选")

    def _apply_login_analysis_mode_ui(self, enabled: bool) -> None:
        if enabled:
            self.login_mode_btn.config(text="关闭登录分析模式")
            self._include_combo.config(state=tk.DISABLED)
            for cb, _var in self._exclude_checks:
                cb.config(state=tk.DISABLED)
            return
        self.login_mode_btn.config(text="启用登录分析模式")
        self._include_combo.config(state="readonly")
        self._on_include_filter_changed()

    def _current_filter_settings(self) -> dict[str, Any]:
        return {
            "include_filter": self._include_filter_key(),
            "exclude_js": bool(self.exclude_js_var.get()),
            "exclude_css": bool(self.exclude_css_var.get()),
            "exclude_fetch_xhr": bool(self.exclude_fetch_xhr_var.get()),
            "exclude_static": bool(self.exclude_static_var.get()),
            "login_analysis_mode": bool(self.login_analysis_mode_var.get()),
        }

    def _update_filter_summary(self) -> None:
        settings = self._current_filter_settings()
        if settings["login_analysis_mode"]:
            self.filter_summary_var.set(
                "当前筛选：登录分析模式（OAuth/鉴权/Token/API，已排除静态资源与业务噪音）"
            )
            return
        include_label = INCLUDE_FILTER_LABELS.get(settings["include_filter"], "全部")
        parts = [f"仅记录：{include_label}"]
        if settings["include_filter"] == "all":
            excludes: list[str] = []
            if settings["exclude_js"]:
                excludes.append("JS")
            if settings["exclude_css"]:
                excludes.append("CSS")
            if settings["exclude_fetch_xhr"]:
                excludes.append("Fetch/XHR")
            if settings["exclude_static"]:
                excludes.append("静态资源")
            if excludes:
                parts.append("排除 " + "、".join(excludes))
        self.filter_summary_var.set("当前筛选：" + "；".join(parts))

    def _drain_ui(self) -> None:
        for _ in range(500):
            try:
                kind, value = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status_var.set(value)
            else:
                self.log_text.config(state=tk.NORMAL)
                self.log_text.insert(tk.END, value)
                self.log_text.see(tk.END)
                self.log_text.config(state=tk.DISABLED)
        self.root.after(50, self._drain_ui)

    def _log(self, message: str) -> None:
        self._ui_queue.put(("log", f"[{datetime.now():%H:%M:%S}] {message}\n"))

    def _set_status(self, text: str) -> None:
        self._ui_queue.put(("status", text))

    def _start_browser(self) -> None:
        if self._browser_starting:
            return
        if self._driver is not None:
            messagebox.showinfo("提示", "浏览器已在运行")
            return

        url = self.url_var.get().strip() or "about:blank"
        self._browser_starting = True

        def work() -> None:
            driver = None
            try:
                self._set_status("正在启动 Chrome…")
                driver = self._create_driver()
                driver.get(url)
                self._driver = driver
                self._set_status("Chrome 已启动，可点击「开始监听」")
                self.root.after(0, lambda: self.start_btn.config(state=tk.NORMAL))
                self._log(f"Chrome 已启动，当前页：{url}")
            except Exception as exc:
                self._set_status(f"启动失败：{exc}")
                self._log(f"启动失败：{exc}")
                if driver is not None:
                    driver.quit()
                self.root.after(0, lambda error=str(exc): messagebox.showerror("错误", error))
            finally:
                self._browser_starting = False

        threading.Thread(target=work, name="capture-start-browser", daemon=True).start()

    def _create_driver(self):
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.selenium_manager import SeleniumManager

        os.environ.setdefault("SE_AVOID_STATS", "true")
        options = Options()
        options.add_argument("--window-size=1400,900")
        chrome_override = os.environ.get("NETWORK_RECORDER_CHROME")
        if chrome_override:
            options.binary_location = chrome_override
        elif sys.platform == "darwin":
            candidates = [
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            ]
            chrome = next((path for path in candidates if path.is_file()), None)
            if chrome is None:
                raise RuntimeError("未找到 Chrome，请安装 Google Chrome 后重试。")
            options.binary_location = str(chrome)
        self._log("正在匹配 Chrome 驱动，首次启动需要联网，请稍候…")
        driver_override = os.environ.get("NETWORK_RECORDER_DRIVER")
        if driver_override:
            driver = webdriver.Chrome(service=Service(executable_path=driver_override), options=options)
        else:
            proxy = getproxies().get("https") or getproxies().get("http")
            if proxy:
                args = ["--browser", "chrome", "--proxy", proxy]
                if options.binary_location:
                    args += ["--browser-path", options.binary_location]
                paths = SeleniumManager().binary_paths(args)
                driver = webdriver.Chrome(service=Service(executable_path=paths["driver_path"]), options=options)
            else:
                driver = webdriver.Chrome(options=options)
        try:
            driver.set_page_load_timeout(120)
            self._debugger_address = resolve_debugger_address(driver)
            self._user_agent = driver.execute_script("return navigator.userAgent")
            return driver
        except Exception:
            driver.quit()
            raise

    def _navigate(self) -> None:
        if not self._driver:
            messagebox.showwarning("提示", "请先启动 Chrome")
            return
        url = self.url_var.get().strip()
        if not url:
            return
        try:
            self._driver.get(url)
            self._log(f"已打开：{redact_url(url)}")
        except Exception as exc:
            messagebox.showerror("错误", str(exc))

    def _start_recording(self) -> None:
        if not self._driver:
            messagebox.showwarning("提示", "请先启动 Chrome")
            return
        if self._recording.is_set():
            return

        self._update_filter_summary()
        filter_settings = self._current_filter_settings()

        self._filter_settings = filter_settings
        self._redact = bool(self.redact_var.get())
        self.redact_check.config(state=tk.DISABLED)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._session_dir = DEFAULT_OUTPUT / stamp
        self._session_dir.mkdir(parents=True, exist_ok=True)
        self._jsonl_path = self._session_dir / "requests.jsonl"
        meta = {
            "started_at": datetime.now().isoformat(),
            "start_url": redact_url(self.url_var.get().strip()) if self._redact else self.url_var.get().strip(),
            "redacted": self._redact,
            "user_agent": self._user_agent,
            "filter": filter_settings,
        }
        (self._session_dir / "session.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        self._capture_count = 0
        self._skipped_count = 0
        self._websocket_frame_count = 0
        self._websocket_urls.clear()
        if not self._debugger_address:
            try:
                self._debugger_address = resolve_debugger_address(self._driver)
            except Exception as exc:
                messagebox.showerror("错误", f"无法获取 CDP 调试地址：{exc}")
                self.redact_check.config(state=tk.NORMAL)
                return

        try:
            self._cdp_monitor = MultiTabNetworkMonitor(self._debugger_address, on_log=self._log)
            self._cdp_monitor.start()
        except Exception as exc:
            messagebox.showerror("错误", f"启动 CDP 监听失败：{exc}")
            if self._cdp_monitor is not None:
                self._cdp_monitor.stop()
            self._cdp_monitor = None
            self.redact_check.config(state=tk.NORMAL)
            return

        self._recording.set()
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.session_var.set(f"输出目录：{self._session_dir}")
        self._set_status("正在监听网络请求…")
        self._log(f"开始监听，筛选：{self.filter_summary_var.get()}（本次会话固定，修改下次生效）")
        self._log(f"输出：{self._jsonl_path}")
        self._log("多标签页监听已启用；原始模式以 Base64 保存 WebSocket 二进制帧，脱敏模式省略载荷")

        self._record_thread = threading.Thread(target=self._record_loop, name="network-record", daemon=True)
        self._record_thread.start()

    def _stop_recording(self) -> None:
        if not self._recording.is_set():
            return
        monitor = self._cdp_monitor
        if monitor is not None:
            monitor.pause()
        self._recording.clear()
        if self._record_thread is not None:
            self._record_thread.join()
            self._record_thread = None
        if monitor is not None:
            monitor.stop()
            self._cdp_monitor = None
        self.redact_check.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.start_btn.config(state=tk.NORMAL)
        self._set_status(
            f"已停止监听，保存 {self._capture_count} 条，跳过 {self._skipped_count} 条"
        )
        self._log(
            f"结束监听，保存 {self._capture_count} 条，跳过 {self._skipped_count} 条 → {self._session_dir}"
        )

        if self._session_dir:
            summary = {
                "stopped_at": datetime.now().isoformat(),
                "request_count": self._capture_count,
                "websocket_frame_count": self._websocket_frame_count,
                "skipped_count": self._skipped_count,
                "filter": self._filter_settings,
                "jsonl": str(self._jsonl_path),
            }
            (self._session_dir / "summary.json").write_text(
                json.dumps(summary, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _record_loop(self) -> None:
        pending: dict[str, dict[str, Any]] = {}
        monitor = self._cdp_monitor
        if monitor is None:
            return
        while self._recording.is_set() or not monitor.event_queue.empty():
            try:
                data = monitor.event_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._handle_cdp_event(data, pending)
            except Exception as exc:
                self._log(f"录制事件处理失败：{type(exc).__name__}")

    @staticmethod
    def _pending_key(target_id: str, request_id: str) -> str:
        return f"{target_id or 'root'}:{request_id}"

    def _handle_cdp_event(self, data: dict[str, Any], pending: dict[str, dict[str, Any]]) -> None:
        method = data.get("method") or ""
        params = data.get("params") or {}
        target_id = str(data.get("_target_id") or "")

        if method == "Network.webSocketCreated":
            rid = str(params.get("requestId") or "")
            if rid:
                self._websocket_urls[self._pending_key(target_id, rid)] = str(params.get("url") or "")
            return

        if method in ("Network.webSocketFrameSent", "Network.webSocketFrameReceived"):
            rid = str(params.get("requestId") or "")
            if not rid:
                return
            response = params.get("response") or {}
            opcode = int(response.get("opcode") or 0)
            payload = str(response.get("payloadData") or "")
            direction = "sent" if method.endswith("FrameSent") else "received"
            record = {
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "event_type": "websocket_frame",
                "targetId": target_id,
                "requestId": rid,
                "url": self._websocket_urls.get(self._pending_key(target_id, rid), ""),
                "method": "WS_SEND" if direction == "sent" else "WS_RECEIVE",
                "resource_type": "WebSocket",
                "direction": direction,
                "opcode": opcode,
                "mask": bool(response.get("mask")),
            }
            if opcode == 2:
                # CDP supplies binary WebSocket payloadData as Base64. Preserve it
                # verbatim so Protobuf frames can be decoded byte-for-byte later.
                record["payload_encoding"] = "base64"
                record["payload_base64"] = payload
                try:
                    raw = base64.b64decode(payload, validate=False)
                    record["payload_byte_length"] = len(raw)
                    record["payload_utf8_preview"] = self._truncate(raw.decode("utf-8", errors="replace"))
                except Exception as exc:
                    record["payload_decode_error"] = str(exc)
            else:
                record["payload_encoding"] = "utf-8"
                record["payload_text"] = payload
                record["payload_byte_length"] = len(payload.encode("utf-8"))
            if self._passes_filter(record):
                self._write_record(record)
            else:
                with self._lock:
                    self._skipped_count += 1
            return

        if method in ("Network.webSocketClosed", "Network.webSocketFrameError"):
            rid = str(params.get("requestId") or "")
            key = self._pending_key(target_id, rid)
            if method.endswith("Closed"):
                self._websocket_urls.pop(key, None)
            if method.endswith("FrameError"):
                record = {
                    "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                    "event_type": "websocket_error", "requestId": rid,
                    "url": self._websocket_urls.get(key, ""), "method": "WS_ERROR",
                    "resource_type": "WebSocket", "error_message": params.get("errorMessage") or "",
                }
                if self._passes_filter(record):
                    self._write_record(record)
            return

        if method == "Network.requestWillBeSent":
            rid = params.get("requestId")
            if not rid:
                return
            key = self._pending_key(target_id, str(rid))
            if params.get("redirectResponse") and key in pending:
                previous = pending.pop(key)
                redirect = params["redirectResponse"]
                previous.update(status=redirect.get("status"), response_headers=redirect.get("headers", {}), redirect=True)
                if self._passes_filter(previous):
                    self._write_record(previous)
            req = params.get("request") or {}
            pending[key] = {
                "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                "requestId": rid,
                "targetId": target_id,
                "url": req.get("url", ""),
                "method": req.get("method", ""),
                "resource_type": params.get("type") or "",
                "request_headers": req.get("headers") or {},
                "request_body": req.get("postData") or "",
            }
            return

        if method == "Network.responseReceived":
            rid = params.get("requestId")
            if not rid:
                return
            key = self._pending_key(target_id, str(rid))
            record = pending.setdefault(
                key,
                {
                    "requestId": rid,
                    "targetId": target_id,
                    "timestamp": datetime.now().isoformat(timespec="milliseconds"),
                },
            )
            resp = params.get("response") or {}
            record["status"] = resp.get("status")
            record["response_headers"] = resp.get("headers") or {}
            record["mimeType"] = resp.get("mimeType") or ""
            if not record.get("resource_type"):
                record["resource_type"] = params.get("type") or resp.get("resourceType") or ""
            return

        if method == "Network.loadingFailed":
            key = self._pending_key(target_id, str(params.get("requestId") or ""))
            record = pending.pop(key, None)
            if record is not None:
                record["event_type"] = "request_failed"
                record["error_message"] = params.get("errorText") or "Request failed"
                if self._passes_filter(record):
                    self._write_record(record)
            return

        if method == "Network.loadingFinished":
            rid = params.get("requestId")
            if not rid:
                return
            key = self._pending_key(target_id, str(rid))
            record = pending.pop(key, None)
            if not record:
                return
            if not self._passes_filter(record):
                with self._lock:
                    self._skipped_count += 1
                return
            self._attach_response_body(record)
            self._write_record(record)

    def _passes_filter(self, record: dict[str, Any]) -> bool:
        return should_record_request(record, **self._filter_settings)

    def _attach_response_body(self, record: dict[str, Any]) -> None:
        monitor = self._cdp_monitor
        if monitor is None:
            return
        request_id = str(record.get("requestId") or "")
        target_id = str(record.get("targetId") or "")
        if not request_id or not target_id:
            return
        try:
            body_result = monitor.get_response_body(target_id, request_id)
            raw = body_result.get("body") or ""
            if body_result.get("base64Encoded"):
                try:
                    decoded = base64.b64decode(raw)
                    text = decoded.decode("utf-8", errors="replace")
                except Exception:
                    text = f"[base64, {len(raw)} chars]"
                record["response_body_encoding"] = "base64-decoded"
            else:
                text = raw
            record["response_body"] = self._truncate(text)
        except Exception as exc:
            record["response_body_error"] = str(exc)

    @staticmethod
    def _truncate(text: str) -> str:
        if len(text) <= MAX_BODY_CHARS:
            return text
        return text[:MAX_BODY_CHARS] + f"\n…[截断，原长 {len(text)} 字符]"

    def _write_record(self, record: dict[str, Any]) -> None:
        if not self._jsonl_path:
            return
        if self._redact:
            record = redact_record(record)
        with self._lock:
            with self._jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._capture_count += 1
            if record.get("event_type") == "websocket_frame":
                self._websocket_frame_count += 1
            count = self._capture_count
        category = record.get("resource_category") or classify_request(record)
        url = redact_url(str(record.get("url") or ""))[:100]
        self._log(
            f"#{count} [{category}] {record.get('method', '?')} "
            f"{record.get('status', '?')} {url}"
        )

    def _open_output_dir(self) -> None:
        path = self._session_dir or DEFAULT_OUTPUT
        path.mkdir(parents=True, exist_ok=True)
        try:
            import os
            import subprocess
            import sys

            if sys.platform == "win32":
                os.startfile(str(path))
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except Exception:
            messagebox.showinfo("输出目录", str(path))

    def _export_assets(self) -> None:
        if self._recording.is_set():
            messagebox.showinfo("提示", "请先结束监听再导出资源。")
            return
        if self._session_dir is None:
            messagebox.showinfo("提示", "请先录制一个会话。导出 JS/CSS 需要关闭脱敏后录制。")
            return
        try:
            count = export_assets(self._session_dir)
            messagebox.showinfo("资源导出", f"已导出 {count} 个已录制 JS/CSS 文件到会话 assets 目录。\n脱敏模式不保存这类正文；关闭脱敏后重新录制才能导出。")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))

    def _close_browser(self) -> None:
        if self._recording.is_set():
            self._stop_recording()
        if self._cdp_monitor is not None:
            self._cdp_monitor.stop()
            self._cdp_monitor = None
        if self._driver:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None
        self._debugger_address = None
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.DISABLED)
        self._set_status("浏览器已关闭")
        self._log("浏览器已关闭")

    def run(self) -> None:
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._on_include_filter_changed()
        self.root.mainloop()

    def _on_close(self) -> None:
        if self._recording.is_set():
            if not messagebox.askyesno("确认", "正在监听中，确定退出？"):
                return
            self._stop_recording()
        self._close_browser()
        self.root.destroy()


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--self-check":
        # Build smoke test: verify Tcl/Tk data and the bundled native driver manager.
        import subprocess
        from selenium.webdriver.common.selenium_manager import SeleniumManager
        configure_macos_tk()
        root = tk.Tk()
        root.withdraw()
        root.update()
        root.destroy()
        manager = SeleniumManager()._get_binary()
        subprocess.run([str(manager), "--version"], check=True, capture_output=True)
        Path(sys.argv[2]).write_text("ok", encoding="utf-8")
        return
    DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
    NetworkCaptureApp().run()


if __name__ == "__main__":
    main()
