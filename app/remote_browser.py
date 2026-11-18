#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""远程浏览器：把服务端的 Chromium **画面与输入**搬到面板里（CDP screencast）。

## 为什么不是 noVNC

noVNC 的链路是 `Xvfb → 窗口管理器 → 有头 Chrome → x11vnc → websockify → 前端`，
要额外背一套 X11 桌面和 700MB 级依赖，而且"抓到登录态"还得再想办法读 cookie。

这里用 Chrome DevTools Protocol：

    Page.startScreencast  → JPEG 帧（WebSocket 二进制）
    Input.dispatch*       → 鼠标/键盘原样回放

同一套 Playwright 浏览器，不加任何系统包，还能顺手 `context.cookies()` 读 HttpOnly 的
sessionid —— 也就是**既能操作，又能把登录态收进池子**。noVNC 做不到后半段。

## 用途

服务器上没有图形界面时，手机号+验证码、滑块验证码、扫码这些**必须人看屏幕**的步骤，
都能在面板里直接完成。安全边界：单会话、10 分钟闲置自动关、WebSocket 用一次性 ticket。
"""
from __future__ import annotations

import base64
import queue
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field

TIMEOUT = 600          # 单会话最长 10 分钟
DETACH_GRACE = 90      # 页面关闭后保留 90 秒，方便刷新/重连
VIEWPORT = {"width": 1280, "height": 800}
HOME = "https://jimeng.jianying.com/ai-tool/home"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")


class RemoteUnavailable(RuntimeError):
    pass


def _launch(p):
    errors: list[str] = []
    for channel in ("chrome", "chromium"):
        try:
            return p.chromium.launch(
                channel=channel, headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{channel}: {type(e).__name__}")
    raise RemoteUnavailable(
        "无法启动浏览器（" + "; ".join(errors) + "）。请安装 "
        "uv pip install -r requirements-login.txt && playwright install chromium")


@dataclass
class RemoteSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    state: str = "starting"       # starting|live|closed|error
    error: str = ""
    account: dict | None = None
    grab: dict | None = None
    attached: int = 0
    last_detach: float = 0.0
    created_at: float = time.time()
    frames: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=2))
    cmds: queue.Queue = field(default_factory=queue.Queue)
    events: queue.Queue = field(default_factory=queue.Queue)
    stop: threading.Event = field(default_factory=threading.Event)
    meta: dict = field(default_factory=lambda: dict(VIEWPORT))
    thread: threading.Thread | None = None

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        if self.thread:
            return
        self.thread = threading.Thread(target=self._run, name="jimeng-remote",
                                       daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.cmds.put({"t": "stop"})

    def send(self, cmd: dict) -> None:
        try:
            self.cmds.put_nowait(cmd)
        except queue.Full:
            pass

    def pop_frame(self) -> bytes | None:
        try:
            return self.frames.get_nowait()
        except queue.Empty:
            return None

    def pop_event(self) -> dict | None:
        try:
            return self.events.get_nowait()
        except queue.Empty:
            return None

    # ------------------------------------------------------------------ 主循环
    def _run(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.state = "error"
            self.error = ("远程浏览器未安装：uv pip install -r requirements-login.txt "
                          "&& playwright install chromium")
            self.events.put({"type": "error", "message": self.error})
            return

        try:
            with sync_playwright() as p:
                browser = _launch(p)
                try:
                    ctx = browser.new_context(
                        viewport=dict(VIEWPORT), locale="zh-CN", user_agent=UA)
                    page = ctx.new_page()
                    page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
                    cdp = ctx.new_cdp_session(page)
                    cdp.on("Page.screencastFrame", self._on_frame)
                    cdp.send("Page.startScreencast", {
                        # 登录页不是视频：降质量 + 隔帧，WAN 下带宽从 ~20Mbps 降到 ~5Mbps
                        "format": "jpeg", "quality": 55,
                        "maxWidth": VIEWPORT["width"],
                        "maxHeight": VIEWPORT["height"],
                        "everyNthFrame": 2,
                    })
                    self.state = "live"
                    self.events.put({"type": "state", "state": "live",
                                     "meta": dict(self.meta)})
                    self._loop(page, ctx, cdp)
                finally:
                    browser.close()
                    self.state = "closed"
        except RemoteUnavailable as e:
            self.state = "error"
            self.error = str(e)
            self.events.put({"type": "error", "message": str(e)})
        except Exception as e:  # noqa: BLE001
            self.state = "error"
            self.error = f"{type(e).__name__}: {e}"
            self.events.put({"type": "error", "message": self.error})

    def _on_frame(self, frame: dict) -> None:
        """CDP 事件线程：只做拷贝和入队，绝不在这里调 send（可能死锁）。"""
        try:
            self.cmds.put_nowait({"ack": frame.get("sessionId")})
            jpeg = base64.b64decode(frame.get("data") or "")
            meta = frame.get("metadata") or {}
            w, h = meta.get("deviceWidth"), meta.get("deviceHeight")
            if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                self.meta = {"width": w, "height": h}
            while True:
                try:
                    self.frames.get_nowait()
                except queue.Empty:
                    break
            self.frames.put_nowait(jpeg)
        except Exception:  # noqa: BLE001
            pass

    def _loop(self, page, ctx, cdp) -> None:
        last_check = 0.0
        while not self.stop.is_set():
            if time.time() - self.created_at > TIMEOUT:
                self.events.put({"type": "error", "message": "远程会话超时（10 分钟）"})
                return
            if self.attached <= 0 and self.last_detach and \
                    time.time() - self.last_detach > DETACH_GRACE:
                return

            # 命令队列（批量消费）
            while True:
                try:
                    cmd = self.cmds.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(cmd, page, ctx, cdp)
                except Exception as e:  # noqa: BLE001
                    self.events.put({"type": "error",
                                     "message": f"操作失败：{type(e).__name__}: {e}"})

            # 每秒检查一次登录态（读 HttpOnly cookie 是这条链路的核心价值）
            if time.time() - last_check > 1.0:
                last_check = time.time()
                sid, expires = self._sid(ctx)
                if sid and not self.account:
                    from .login_assistant import register_session
                    res = register_session(sid, expires, "远程浏览器")
                    self.account = {"account_id": res["account_id"],
                                    "credits": res["credits"],
                                    "message": res["message"]}
                    self.events.put({"type": "login", **self.account})
            time.sleep(0.04)

    @staticmethod
    def _sid(ctx) -> tuple[str, int | None]:
        jar = {c["name"]: c["value"] for c in ctx.cookies()}
        sid = jar.get("sessionid") or ""
        expires = None
        bits = (jar.get("sid_guard") or "").split("|")
        if len(bits) >= 3 and bits[1].isdigit() and bits[2].isdigit():
            expires = int(bits[1]) + int(bits[2])
        return sid, expires

    # ------------------------------------------------------------------ 输入
    def _handle(self, cmd: dict, page, ctx, cdp) -> None:
        if "ack" in cmd:
            cdp.send("Page.screencastFrameAck", {"sessionId": cmd["ack"]})
            return
        t = cmd.get("t")
        if t == "mouse":
            cdp.send("Input.dispatchMouseEvent", {
                "type": cmd.get("type", "mouseMoved"),
                "x": float(cmd.get("x", 0)), "y": float(cmd.get("y", 0)),
                "button": cmd.get("button", "none"),
                "buttons": int(cmd.get("buttons", 0)),
                "clickCount": int(cmd.get("clickCount", 0)),
            })
        elif t == "wheel":
            cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseWheel", "x": float(cmd.get("x", 0)),
                "y": float(cmd.get("y", 0)), "deltaX": 0,
                "deltaY": float(cmd.get("deltaY", 0)),
            })
        elif t == "text":
            text = str(cmd.get("text") or "")
            if text:
                cdp.send("Input.insertText", {"text": text})
        elif t == "key":
            cdp.send("Input.dispatchKeyEvent", {
                "type": cmd.get("type", "keyDown"),
                "key": cmd.get("key", ""),
                "code": cmd.get("code", ""),
                "windowsVirtualKeyCode": int(cmd.get("keyCode", 0)),
                "nativeVirtualKeyCode": int(cmd.get("keyCode", 0)),
            })
        elif t == "back":
            page.go_back()
        elif t == "reload":
            page.reload()
        elif t == "home":
            page.goto(HOME, wait_until="domcontentloaded")
        elif t == "grab":
            sid, expires = self._sid(ctx)
            if not sid:
                self.grab = {"ok": False, "message": "还没有登录（没有 sessionid）"}
            elif self.account:
                self.grab = {"ok": True, **self.account}
            else:
                from .login_assistant import register_session
                res = register_session(sid, expires, "远程浏览器")
                self.account = {"account_id": res["account_id"],
                                "credits": res["credits"],
                                "message": res["message"]}
                self.grab = {"ok": True, **self.account}
            self.events.put({"type": "grab", **self.grab})
        elif t == "stop":
            self.stop.set()


# ---------------------------------------------------------------- 单例与 ticket
_SESSION: RemoteSession | None = None
_LOCK = threading.Lock()
_TICKETS: dict[str, float] = {}
_TICKET_TTL = 60.0


def issue_ticket() -> str:
    now = time.time()
    for k, exp in list(_TICKETS.items()):
        if exp < now:
            _TICKETS.pop(k, None)
    t = secrets.token_urlsafe(24)
    _TICKETS[t] = now + _TICKET_TTL
    return t


def verify_ticket(ticket: str) -> bool:
    exp = _TICKETS.pop(ticket or "", 0)
    return bool(exp) and exp >= time.time()


def attach() -> RemoteSession:
    global _SESSION
    with _LOCK:
        if _SESSION is None or _SESSION.state in ("closed", "error"):
            _SESSION = RemoteSession()
            _SESSION.start()
        _SESSION.attached += 1
        _SESSION.last_detach = 0.0
        return _SESSION


def detach(session: RemoteSession) -> None:
    with _LOCK:
        session.attached = max(0, session.attached - 1)
        session.last_detach = time.time()


def stop() -> None:
    global _SESSION
    with _LOCK:
        if _SESSION:
            _SESSION.close()
            _SESSION = None


def available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True
