"""
抖音扫码登录（与 B 站插件的 `/bili_login` 对齐的体验）。

为什么必须是"真浏览器":
  `sso.douyin.com` 的登录接口对非浏览器请求一律返回 **JS 反爬挑战页**（约 10KB HTML，
  里面是 `__ac_nonce`/`__ac_signature` 那套 SDK）；老接口 `/passport/web/get_qrcode/`
  直接回 `{"error_code":22,"description":"非法应用"}`。
  也就是说：没有 JS 执行环境，拿不到登录票据。让无头 Chrome 真加载页面，反爬是自动过的。

流程:
  start() -> 打开抖音首页 → 点「登录」→ 从登录弹层里取 `data:image` 二维码 → 存成 PNG
  poll()  -> 读浏览器上下文里的 Cookie，出现 sessionid 即视为扫码成功
  close() -> 关浏览器、删二维码临时文件

实测（容器内 Chrome for Testing 151 + playwright 1.62，2026-10-23）:
  - 出码约 10s，二维码为 512x512 PNG；
  - 解码内容形如 `https://v.douyin.com/xxxx/?hide_nav_bar=1`（抖音 App 扫的登录票据）。
"""

from __future__ import annotations

import asyncio
import base64
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from astrbot.api import logger

HOME_URL = "https://www.douyin.com/"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# 点开登录弹层的候选选择器（按实测优先级）
_LOGIN_SELECTORS: Sequence[str] = (
    'header button:has-text("登录")',
    'button:has-text("登录")',
    '[data-e2e="login-button"]',
)

# 登录成功的最低要求：浏览器上下文里出现 sessionid
REQUIRED_COOKIE_KEYS: Tuple[str, ...] = ("sessionid",)

# 抓二维码：优先登录弹层容器内的 data:image，再退化到全页方形 img/canvas
_QR_JS = """
() => {
  const ok = (el) => {
    const r = el.getBoundingClientRect();
    return r.width >= 110 && Math.abs(r.width - r.height) < 30;
  };
  const fromImg = (root) => {
    for (const el of root.querySelectorAll('img')) {
      const src = el.src || '';
      if (src.startsWith('data:image')) return src;
    }
    return '';
  };
  const fromCanvas = (root) => {
    for (const c of root.querySelectorAll('canvas')) {
      if (!ok(c)) continue;
      try { return c.toDataURL('image/png'); } catch (e) {}
    }
    return '';
  };
  const box = document.querySelector('#douyin_login_comp_scan_code');
  if (box) {
    const got = fromImg(box) || fromCanvas(box);
    if (got) return got;
  }
  for (const el of document.querySelectorAll('img')) {
    if (ok(el) && (el.src || '').startsWith('data:image')) return el.src;
  }
  return fromCanvas(document);
}
"""


class DouyinLoginError(Exception):
    """抖音扫码登录失败。"""


@dataclass(frozen=True)
class QrCodeInfo:
    image_path: str
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class LoginPollResult:
    state: str                     # waiting / confirmed / expired / error
    message: str = ""
    cookie: str = ""

    @property
    def is_done(self) -> bool:
        return self.state == "confirmed" and bool(self.cookie)

    @property
    def is_expired(self) -> bool:
        return self.state == "expired"


# ============================================================================
# 纯函数（便于单测）
# ============================================================================

def filter_douyin_cookies(raw_cookies: Optional[Sequence[Dict[str, Any]]]) -> Dict[str, str]:
    """从 playwright 的 cookies() 结果里挑出 douyin.com 域、且值非空的 cookie。"""
    out: Dict[str, str] = {}
    for item in raw_cookies or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        domain = str(item.get("domain") or "")
        if not name or value is None or str(value).strip() == "":
            continue
        if domain and "douyin.com" not in domain:
            continue
        out[name] = str(value)
    return out


def cookies_have_login(cookies: Dict[str, str]) -> bool:
    return all(bool(cookies.get(k)) for k in REQUIRED_COOKIE_KEYS)


def cookies_to_string(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def build_login_result(cookies: Dict[str, str], elapsed_secs: float,
                       timeout_secs: int) -> LoginPollResult:
    if cookies_have_login(cookies):
        return LoginPollResult(state="confirmed", message="扫码登录成功",
                               cookie=cookies_to_string(cookies))
    if elapsed_secs >= timeout_secs:
        return LoginPollResult(state="expired", message="二维码已过期")
    return LoginPollResult(state="waiting", message="等待扫码")


# ============================================================================
# 客户端
# ============================================================================

BrowserFactory = Callable[[], Awaitable[Tuple[Any, Any, Any, Any]]]


class DouyinQrLoginClient:
    """扫码登录客户端。

    browser_factory 仅供测试注入: async () -> (playwright, browser, context, page)。
    """

    def __init__(self, timeout_secs: int = 180, headless: bool = True,
                 browser_factory: Optional[BrowserFactory] = None):
        try:
            timeout_secs = int(timeout_secs)
        except (TypeError, ValueError):
            timeout_secs = 180
        self.timeout_secs = max(30, timeout_secs)
        self.headless = headless
        self._browser_factory = browser_factory
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self.started_at: float = 0.0
        self.qr_path: str = ""

    # ---------------- 生命周期 ----------------

    @property
    def is_running(self) -> bool:
        return self._context is not None

    async def start(self) -> QrCodeInfo:
        """打开抖音登录弹层并返回二维码图片路径。失败会自行清理。"""
        if self._context is not None:
            raise DouyinLoginError("已有一个登录会话在进行中")

        self._pw, self._browser, self._context, self._page = await self._open_browser()
        page = self._page
        try:
            try:
                await page.goto(HOME_URL, wait_until="commit", timeout=60000)
            except Exception as e:  # noqa: BLE001 — 首页超时也可能已经可用, 继续等
                logger.debug(f"打开抖音首页异常(继续): {e}")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(2)

            await self._click_login(page)
            data_url = await self._wait_qr(page)
            if not data_url:
                raise DouyinLoginError("没等到登录二维码（抖音页面结构可能已变化）")

            self.qr_path = self._save_qr(data_url)
        except BaseException:
            await self.close()
            raise

        self.started_at = time.time()
        logger.info(f"抖音扫码登录: 二维码已生成 ({self.qr_path})")
        return QrCodeInfo(image_path=self.qr_path, created_at=self.started_at)

    async def poll(self) -> LoginPollResult:
        """轮询扫码状态；出现 sessionid 即成功。"""
        if self._context is None:
            raise DouyinLoginError("尚未开始登录")
        try:
            raw = await self._context.cookies()
        except Exception as e:  # noqa: BLE001
            return LoginPollResult(state="error", message=f"读取浏览器 Cookie 失败: {e}")
        cookies = filter_douyin_cookies(raw)
        return build_login_result(cookies, time.time() - self.started_at, self.timeout_secs)

    async def close(self) -> None:
        """关闭浏览器并清理二维码临时文件（幂等）。"""
        for attr in ("_context", "_browser"):
            obj = getattr(self, attr)
            if obj is not None:
                try:
                    await obj.close()
                except Exception:  # noqa: BLE001
                    pass
                setattr(self, attr, None)
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
        self._page = None
        if self.qr_path:
            try:
                os.unlink(self.qr_path)
            except OSError:
                pass
            self.qr_path = ""

    # ---------------- 内部 ----------------

    async def _open_browser(self) -> Tuple[Any, Any, Any, Any]:
        if self._browser_factory is not None:
            try:
                return await self._browser_factory()
            except DouyinLoginError:
                raise
            except Exception as e:  # noqa: BLE001
                raise DouyinLoginError(f"启动浏览器失败: {e}") from e

        try:
            from playwright.async_api import async_playwright  # type: ignore
        except Exception as e:  # noqa: BLE001
            raise DouyinLoginError(
                "未安装 playwright: 需要 `pip install playwright` 且执行 "
                "`python -m playwright install chromium`"
            ) from e

        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-gpu",
                ],
            )
            context = await browser.new_context(
                locale="zh-CN",
                user_agent=_USER_AGENT,
                viewport={"width": 1366, "height": 900},
            )
            page = await context.new_page()
        except Exception as e:  # noqa: BLE001
            try:
                await pw.stop()
            except Exception:  # noqa: BLE001
                pass
            raise DouyinLoginError(
                f"启动无头浏览器失败: {e}（容器内需先装 chromium: "
                f"`python -m playwright install chromium`）"
            ) from e
        return pw, browser, context, page

    @staticmethod
    async def _click_login(page: Any) -> None:
        for sel in _LOGIN_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count():
                    await loc.click(timeout=4000)
                    logger.debug(f"已点击登录入口: {sel}")
                    return
            except Exception:  # noqa: BLE001
                continue
        logger.warning("没找到登录按钮，直接尝试读取二维码")

    @staticmethod
    async def _wait_qr(page: Any, tries: int = 24) -> str:
        for _ in range(max(1, tries)):
            try:
                data_url = await page.evaluate(_QR_JS)
            except Exception:  # noqa: BLE001
                data_url = ""
            if data_url:
                return str(data_url)
            await asyncio.sleep(0.5)
        return ""

    @staticmethod
    def _save_qr(data_url: str) -> str:
        try:
            _, b64 = str(data_url).split(",", 1)
            raw = base64.b64decode(b64)
        except Exception as e:  # noqa: BLE001
            raise DouyinLoginError(f"二维码数据解析失败: {e}") from e
        fd, path = tempfile.mkstemp(prefix="dy_login_qr_", suffix=".png")
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        return path
