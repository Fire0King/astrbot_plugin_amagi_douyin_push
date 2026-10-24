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
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from astrbot.api import logger

HOME_URL = "https://www.douyin.com/"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

# 点开登录弹层的候选选择器（按实测优先级；2026-10-24 实测 header 里这个最稳）
_LOGIN_SELECTORS: Sequence[str] = (
    'header button:has-text("登录")',
    'button:has-text("登录")',
    '[data-e2e="login-button"]',
)

# 登录成功的最低要求：浏览器上下文里出现 sessionid
REQUIRED_COOKIE_KEYS: Tuple[str, ...] = ("sessionid",)

# 登录弹层里二维码所在容器。实测(2026-10-24):
#   容器内部没有 <img>/<canvas>, 二维码是整个"弹层区域"渲染出来的;
#   直接截这个容器截不到码, 要截它的**父级**(xpath=../..) 才有码
#   (父级截图解码得到 https://api.amemv.com/.../douyin_scan_code_login/...)
BOX_SELECTOR = "#douyin_login_comp_scan_code"
QR_CAPTURE_SELECTOR = f"{BOX_SELECTOR} >> xpath=../.."

# 读弹层文字(判断"已扫码/已失效")时向上找几层
_MODAL_TEXT_JS = """
(sel) => {
  let el = document.querySelector(sel);
  for (let i = 0; i < 4 && el && el.parentElement; i++) el = el.parentElement;
  return el ? (el.innerText || '') : '';
}
"""


def _qr_signature(path: str, size: int = 16) -> Optional[Tuple[int, ...]]:
    """把图缩成 16x16 灰度指纹，用于判断"二维码有没有换"（避免按字节比较被抗锯齿差异骗到）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            small = im.convert("L").resize((size, size))
            return tuple(small.getdata())
    except Exception:  # noqa: BLE001
        return None


def _signatures_differ(a: Optional[Tuple[int, ...]], b: Optional[Tuple[int, ...]],
                       threshold: float = 6.0) -> bool:
    """两张图的平均灰度差 >= threshold 视为"换了"（实测换码时差异远大于此）。"""
    if not a or not b or len(a) != len(b):
        return True
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a) >= threshold


def _looks_like_qr(path: str) -> bool:
    """粗判一张图是不是"画完的二维码"（不引入解码依赖，只用 Pillow）。

    实测同一个弹层:
      未渲染完(占位) : 黑 2% / 白 96% / 横向黑白跳变密度 ~1.4 次每行
      画完的二维码   : 黑 17% / 白 79% / 横向黑白跳变密度 ~10.8 次每行
    所以三个条件就能挡住"截到白板/截到半成品":
      尺寸 >= 200、黑色占比 >= 8%、每行平均跳变 >= 4。
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            gray = im.convert("L")
            w, h = gray.size
            if min(w, h) < 200:
                return False
            px = gray.load()
            black = total = trans = rows = 0
            for y in range(0, h, 2):
                prev = None
                rows += 1
                for x in range(0, w, 2):
                    v = px[x, y]
                    total += 1
                    if v < 60:
                        black += 1
                    cur = v < 128
                    if prev is not None and cur != prev:
                        trans += 1
                    prev = cur
            if not total:
                return False
            return (black / total) >= 0.08 and (trans / max(1, rows)) >= 4
    except Exception:  # noqa: BLE001
        return False


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

    # 打开登录弹层的重试参数（单测里可调小，避免慢）
    MODAL_CLICK_ATTEMPTS: int = 3
    MODAL_WAIT_SECS: int = 15

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

            await self._open_login_modal(page)
            self.qr_path = await self._capture_qr(page)
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
        # 先删二维码临时文件: 即使后面 close 过程被取消/卡住, 也不留下票据残留
        if self.qr_path:
            try:
                os.unlink(self.qr_path)
            except OSError:
                pass
            self.qr_path = ""
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
                device_scale_factor=2,      # 截图放大 2 倍, 二维码在手机上更好扫
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

    # ---------------- 打开弹层 / 抓二维码 ----------------

    @staticmethod
    async def _modal_present(page: Any) -> bool:
        try:
            return bool(await page.evaluate("(sel) => !!document.querySelector(sel)", BOX_SELECTOR))
        except Exception:  # noqa: BLE001
            return False

    async def _open_login_modal(self, page: Any, attempts: Optional[int] = None) -> None:
        """把登录弹层点开。打不开就报错——绝不退化成"页面上随便找张二维码"。"""
        attempts = self.MODAL_CLICK_ATTEMPTS if attempts is None else attempts
        wait_secs = max(1, self.MODAL_WAIT_SECS)
        # 等页面水合(header 里的登录按钮出现), 实测约 3s
        try:
            await page.wait_for_selector(_LOGIN_SELECTORS[0], timeout=25000)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"等待登录按钮超时: {e}")
        await asyncio.sleep(1)

        for i in range(max(1, attempts)):
            if await self._modal_present(page):
                logger.debug("登录弹层已打开")
                return
            try:
                await page.locator(_LOGIN_SELECTORS[0]).first.click(timeout=6000)
                logger.debug(f"已点击登录入口 (第 {i + 1} 次)")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"点击登录入口失败 (第 {i + 1} 次): {e}")
            # 弹层出现实测需要几秒; 0.3s 轮询, 最多等 wait_secs 秒
            deadline = time.time() + wait_secs
            while time.time() < deadline:
                if await self._modal_present(page):
                    logger.debug(f"登录弹层已打开 (第 {i + 1} 次点击后)")
                    return
                await asyncio.sleep(0.3)

        raise DouyinLoginError("没能打开抖音登录弹层（页面结构可能变化），请稍后再试")

    async def _capture_qr(self, page: Any, tries: int = 16) -> str:
        """截登录弹层区域作为二维码图片（容器本身截不到码, 要截父级）。

        弹层刚出现时二维码还没画完(实测 +0s 的截图解码失败), 所以这里反复截图,
        直到 _looks_like_qr 通过为止。
        """
        fd, path = tempfile.mkstemp(prefix="dy_login_qr_", suffix=".png")
        os.close(fd)
        last_err = ""
        for i in range(max(1, tries)):
            # 第一次稍等(约 0.8s 后二维码才画完), 之后小步重试
            await asyncio.sleep(0.8 if i == 0 else 0.45)
            try:
                loc = page.locator(QR_CAPTURE_SELECTOR).first
                if await loc.count():
                    await loc.screenshot(path=path)
                    if _looks_like_qr(path):
                        return path
                    last_err = "截图不像完整二维码"
            except Exception as e:  # noqa: BLE001
                last_err = str(e)[:120]
        try:
            os.unlink(path)
        except OSError:
            pass
        raise DouyinLoginError(f"没截到登录二维码（{last_err or '弹层未渲染'}）")

    # ---------------- 扫码进度 ----------------

    async def page_state(self) -> str:
        """读登录弹层的文字，判断现在到了哪一步。

        返回: "scanned"（已扫码待确认）/ "expired"（抖音侧二维码失效）/
              "confirmed"（页面提示登录成功）/ "unknown"
        """
        if self._page is None:
            return "unknown"
        try:
            text = await self._page.evaluate(_MODAL_TEXT_JS, BOX_SELECTOR)
        except Exception:  # noqa: BLE001
            text = ""
        text = str(text or "")
        if not text:
            # 弹层已经消失/还没渲染: 退化为整页文字(登录成功后整页常出现"登录成功")
            try:
                text = await self._page.inner_text("body")
            except Exception:  # noqa: BLE001
                text = ""
        if any(k in text for k in ("登录成功", "已登录")):
            return "confirmed"
        if any(k in text for k in ("扫描成功", "请在手机上", "请在手机")) and "扫一扫" not in text[:60]:
            return "scanned"
        if any(k in text for k in ("二维码已失效", "二维码已过期", "已失效", "点击刷新")):
            return "expired"
        return "unknown"

    async def refresh_qr(self) -> Optional[str]:
        """抖音侧二维码约 40 秒自动换一张；这里重新截图，**按图像相似度**判断是否真的换了。"""
        if self._page is None:
            return None
        old_path = self.qr_path
        fd, new_path = tempfile.mkstemp(prefix="dy_login_qr_", suffix=".png")
        os.close(fd)
        try:
            loc = self._page.locator(QR_CAPTURE_SELECTOR).first
            if not await loc.count():
                os.unlink(new_path)
                return None
            await loc.screenshot(path=new_path)
            if not _looks_like_qr(new_path):
                os.unlink(new_path)
                return None
            if old_path and os.path.exists(old_path) and not _signatures_differ(
                    _qr_signature(old_path), _qr_signature(new_path)):
                os.unlink(new_path)
                return None      # 还是同一张码（截图有轻微抗锯齿差异，不算换）
        except Exception as e:  # noqa: BLE001
            logger.debug(f"刷新二维码失败: {e}")
            try:
                os.unlink(new_path)
            except OSError:
                pass
            return None

        self.qr_path = new_path
        if old_path and old_path != new_path:
            try:
                os.unlink(old_path)
            except OSError:
                pass
        return new_path
