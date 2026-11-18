#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫码登录助手：**无头浏览器 + 抖音 OAuth 二维码**，把登录过程搬进面板。

为什么需要它：即梦的 `sessionid` 是 **HttpOnly** cookie（`document.cookie` 里读不到，
浏览器扩展按策略也不暴露 cookie 值），所以"从已登录的浏览器里把 sessionid 抠出来"
在工具层面走不通。服务器上更没有图形界面去开 DevTools。

这里换一条**正路**：服务端自己起一个浏览器，走站点真实的登录流程
（登录 → 勾选协议 → 抖音登录 → OAuth 二维码），用户在面板里用抖音 App 扫一下；
扫码成功后 `BrowserContext.cookies()` 能拿到 HttpOnly 的 sessionid，直接入池。

安全边界：
* 浏览器只访问 `jimeng.jianying.com` / `open.douyin.com`；
* sessionid 只写进本地 DB，**不打印、不返回给前端**（面板只看到尾号）；
* 一次只允许一个登录会话（每个浏览器约 200MB，且没有并发的必要）。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field

from . import db, repo

HOME = "https://jimeng.jianying.com/ai-tool/home"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")
TIMEOUT = 300         # 二维码有效期按 5 分钟算
QR_REFRESH = 8.0      # 每 8 秒重截一次二维码（二维码会轮换）

#: 登录方式 → (弹窗里的按钮 aria-label, 提示里用的 App 名)
PROVIDERS: dict[str, tuple[str, str]] = {
    "douyin": ("抖音登录", "抖音"),
    "feishu": ("飞书登录", "飞书"),
}

_LOCK = threading.Lock()
_SESSIONS: dict[str, "LoginSession"] = {}


class LoginUnavailable(RuntimeError):
    """Playwright 未安装/浏览器不可用 —— 面板据此显示安装指引。"""


@dataclass
class LoginSession:
    id: str
    remark: str = ""
    provider: str = "douyin"
    state: str = "starting"   # starting|waiting|success|error|expired|cancelled
    message: str = "正在打开登录页…"
    qr: bytes = b""
    qr_version: int = 0
    account_id: int | None = None
    credits: int | None = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    stop: threading.Event = field(default_factory=threading.Event)

    def snapshot(self) -> dict:
        return {
            "session_id": self.id, "state": self.state,
            "provider": self.provider,
            "message": self.message, "qr_version": self.qr_version,
            "account_id": self.account_id, "credits": self.credits,
            "error": self.error, "age": int(time.time() - self.created_at),
            "has_qr": bool(self.qr),
        }


def _launch(p):
    """优先用本机 Chrome（免下载）；服务器上没有 Chrome 就退回 Playwright 自带 chromium。"""
    errors: list[str] = []
    for channel in ("chrome", "chromium"):
        try:
            return p.chromium.launch(
                channel=channel, headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
        except Exception as e:  # noqa: BLE001 - 逐个候选尝试
            errors.append(f"{channel}: {type(e).__name__}")
    raise LoginUnavailable(
        "无法启动浏览器（" + "; ".join(errors) + "）。"
        "请安装：uv pip install -r requirements-login.txt && "
        "playwright install chromium")


def _qr_png(popup) -> bytes:
    """截二维码：在**所有 frame** 里找最大的正方形 img/canvas，退化为整页截图。

    抖音的码是 `<img>`，飞书的码是 `<canvas>`（2026-11-19 实测，184×184）——
    只找 img 会把飞书那张整页截图塞进面板，二维码小到扫不出来。
    """
    best = None
    best_area = 0
    frames = [popup] + [f for f in popup.frames if f != popup.main_frame]
    for frame in frames:
        try:
            elems = frame.locator("img, canvas")
            for i in range(min(elems.count(), 20)):
                box = elems.nth(i).bounding_box()
                if not box:
                    continue
                w, h = box["width"], box["height"]
                if 140 <= w <= 480 and abs(w - h) <= 24 and w * h > best_area:
                    best, best_area = elems.nth(i), w * h
        except Exception:  # noqa: BLE001
            continue
    if best is not None:
        try:
            return best.screenshot(type="png")
        except Exception:  # noqa: BLE001
            pass
    return popup.screenshot(type="png", full_page=False)


def _sid_of(ctx) -> tuple[str, int | None]:
    jar = {c["name"]: c["value"] for c in ctx.cookies()}
    sid = jar.get("sessionid") or ""
    expires = None
    guard = jar.get("sid_guard") or ""
    bits = guard.split("|")
    if len(bits) >= 3 and bits[1].isdigit() and bits[2].isdigit():
        expires = int(bits[1]) + int(bits[2])
    return sid, expires


def register_session(sid: str, expires: int | None, remark: str = "") -> dict:
    """把一个已登录的 sessionid 落库并探活。扫码助手和远程浏览器共用。"""
    row = db.one("SELECT id FROM accounts WHERE sessionid = ?", (sid,))
    if row:
        return {"account_id": int(row["id"]), "credits": None,
                "message": "该账号已在池中，无需重复添加"}
    account_id = repo.add_account(sid, remark or "登录助手")
    if expires:
        repo.update_account(account_id, expires_at=expires)
    out = {"account_id": account_id, "credits": None,
           "message": "登录成功，已加入账号池"}
    # 探活只是为了把昵称/积分填上；失败不影响入池
    try:
        from .pool import pool
        res = pool.probe(account_id)
        out["credits"] = res.get("credits")
        out["message"] = (f"登录成功：{res.get('name') or '未命名'} · "
                          f"积分 {res.get('credits')}")
    except Exception as e:  # noqa: BLE001
        out["message"] = f"登录成功，但探活失败：{e}"
    return out


def _finish_success(s: LoginSession, sid: str, expires: int | None) -> None:
    res = register_session(sid, expires, s.remark or "扫码登录")
    s.account_id = res["account_id"]
    s.credits = res["credits"]
    s.state = "success"
    s.message = res["message"]


def _run(s: LoginSession) -> None:
    try:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover - 环境相关
            raise LoginUnavailable(
                "登录助手未安装：uv pip install -r requirements-login.txt && "
                "playwright install chromium") from e

        with sync_playwright() as p:
            browser = _launch(p)
            try:
                ctx = browser.new_context(
                    locale="zh-CN", viewport={"width": 1280, "height": 900},
                    user_agent=UA)
                page = ctx.new_page()
                s.message = "正在打开即梦登录页…"
                page.goto(HOME, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)

                # 已经登录（复用了浏览器 profile 之类）：直接收
                sid, expires = _sid_of(ctx)
                if sid:
                    _finish_success(s, sid, expires)
                    return

                label, app_name = PROVIDERS.get(s.provider,
                                                PROVIDERS["douyin"])
                # 打开登录弹窗 → 勾协议 → 选中的第三方登录（弹出 OAuth 二维码页）
                page.get_by_role("button", name="登录", exact=True).first.click()
                s.message = "已打开登录弹窗，正在拉起第三方登录…"
                page.wait_for_timeout(1200)
                page.evaluate(
                    "() => {const c=document.querySelector('input[type=checkbox]');"
                    " if(c && !c.checked) c.click();}")
                with ctx.expect_page(timeout=20000) as newp:
                    page.evaluate(
                        "(label) => {const b=[...document.querySelectorAll('button')]"
                        ".find(x=>x.getAttribute('aria-label')===label);"
                        " if(b) b.click();}", label)
                popup = newp.value
                popup.wait_for_load_state("domcontentloaded")
                popup.wait_for_timeout(2500)

                s.state = "waiting"
                s.message = f"用{app_name} App 扫码，并在手机上确认"
                s.qr = _qr_png(popup)
                s.qr_version += 1
                last_shot = time.time()
                deadline = time.time() + TIMEOUT
                closed_at: float | None = None

                while time.time() < deadline:
                    if s.stop.is_set():
                        s.state = "cancelled"
                        s.message = "已取消扫码"
                        return
                    sid, expires = _sid_of(ctx)
                    if sid:
                        _finish_success(s, sid, expires)
                        return
                    if popup.is_closed():
                        # 扫码确认后弹窗会自己关，cookie 可能晚 1~2 秒才写进来
                        closed_at = closed_at or time.time()
                        if time.time() - closed_at > 5:
                            s.state = "error"
                            s.error = "扫码窗口已关闭，请重新发起"
                            s.message = s.error
                            return
                    elif closed_at:
                        closed_at = None
                    if time.time() - last_shot > QR_REFRESH:
                        try:
                            shot = _qr_png(popup)
                            if shot and shot != s.qr:  # 图没变就不加版本，别让前端白刷
                                s.qr = shot
                                s.qr_version += 1
                            last_shot = time.time()
                        except Exception:  # noqa: BLE001 - 截图失败不影响主流程
                            pass
                    time.sleep(1.0)

                s.state = "expired"
                s.message = "二维码已过期，请重新发起扫码"
            finally:
                browser.close()
    except LoginUnavailable as e:
        s.state = "error"
        s.error = str(e)
        s.message = str(e)
    except Exception as e:  # noqa: BLE001 - 任何异常都要变成面板可读的状态
        s.state = "error"
        s.error = f"{type(e).__name__}: {e}"
        s.message = "扫码登录失败：" + s.error
    finally:
        if s.state in ("success", "error", "expired", "cancelled"):
            s.stop.set()


def start(remark: str = "", provider: str = "douyin") -> LoginSession:
    """发起一个扫码会话；已有进行中的会话时直接复用它。"""
    with _LOCK:
        for s in _SESSIONS.values():
            if s.state in ("starting", "waiting"):
                return s
        # 清理 10 分钟前的终态会话，避免内存堆积
        for k, v in list(_SESSIONS.items()):
            if v.state not in ("starting", "waiting") and time.time() - v.created_at > 600:
                _SESSIONS.pop(k, None)
        provider = provider if provider in PROVIDERS else "douyin"
        s = LoginSession(id=uuid.uuid4().hex[:12], remark=remark,
                         provider=provider)
        _SESSIONS[s.id] = s
        threading.Thread(target=_run, args=(s,), name="jimeng-login", daemon=True).start()
        return s


def get(session_id: str) -> LoginSession | None:
    return _SESSIONS.get(session_id)


def cancel(session_id: str) -> bool:
    s = _SESSIONS.get(session_id)
    if not s:
        return False
    s.stop.set()
    return True


def available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True
