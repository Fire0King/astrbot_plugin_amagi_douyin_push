"""
本地卡片渲染 (Pillow 自绘)。

为什么需要它:
  AstrBot 的 `html_render` 走的是**远程文转图(t2i)服务**。该服务由第三方托管,
  会抖动(实测返回 502/503 页面)或变慢(部署在国外, 单次渲染 2~5 秒), 而且
  小内存机器(1.7G)自己也跑不动 Chromium 自部署方案。
  这里用 Pillow 直接画卡片: 内存 ~30MB、单张 ~30ms、**不依赖任何外部服务**。

字体: 自动探测系统里的中文字体(容器内已有 NotoSansCJK), 可用 font_path 覆盖。
     探测时会用"私用区字符"做对照, 避免把 notdef 方框误判成有字形。
"""

import asyncio
import io
import math
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.api.star import StarTools
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ==================== 版式常量 (与 assets/templates/*.html 对齐) ====================
BG_COLOR = (0x1A, 0x1A, 0x2E)          # 页面背景(深蓝黑)
CARD_COLOR = (0xFF, 0xFF, 0xFF)        # 卡片白底
CARD_RADIUS = 12
CARD_WIDTH = 380
PAGE_PAD = 12

TEXT_MAIN = (0x1A, 0x1A, 0x2E)
TEXT_BODY = (0x33, 0x33, 0x33)
TEXT_WEAK = (0x88, 0x88, 0x88)
TEXT_FAINT = (0xAA, 0xAA, 0xAA)
ACCENT = (0xFE, 0x2C, 0x55)            # 抖音红
BILI_ACCENT = (0xFB, 0x72, 0x99)       # B 站粉
OFFLINE_GRAY = (0x99, 0x99, 0x99)
COVER_BG = (0xF0, 0xF0, 0xF0)
AVATAR_BG = (0xE6, 0xE6, 0xEA)

COVER_BOX_H = 420                      # 抖音竖版封面展示区高度(contain 适配)
BILI_COVER_H = 214                     # B 站横版封面(16:9 → 380x214, 正好铺满)

# ==================== 输出倍率 ====================
# 版式按"设计单位"排版(卡片宽 380, 与 assets/templates/*.html 一致), 绘制时整体乘以该倍率。
# 为什么需要: B 站插件走 html 渲染, 输出是 800 CSS px × device_scale_factor 1.8 ≈ 1440px 宽,
# 而本地 1:1 绘制只有 404px —— 在 QQ 里看着明显偏小、也不够清晰。
# 默认 4.0 → 404 × 4 ≈ 1616px, 与 B 站插件同级(它约 1440px);
# 文字是按倍率用矢量重绘的(不是位图放大), 所以放大后依然锐利。
DEFAULT_SCALE = 4.0
MAX_SCALE = 6.0

# 中文字体候选(按优先级); 前面的更好看
_FONT_CANDIDATES: Sequence[str] = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    # Windows / macOS (便于本地开发调试)
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
)

# 用于"字形存在性"对照的私用区字符: 正常字体没有它的字形, 会画成 notdef 方框
_PROBE_CHAR = "测"
_MISSING_CHAR = "\ue000"


def _font_ink(font: ImageFont.FreeTypeFont, ch: str) -> bytes:
    """把单个字符画到小图上, 返回像素字节(用于比较字形是否存在)"""
    canvas = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(canvas).text((2, 2), ch, font=font, fill=255)
    return canvas.tobytes()


def _can_render_cjk(path: str) -> bool:
    """判断字体是否真的含有中文字形(而不是画成方框)"""
    try:
        font = ImageFont.truetype(path, 32)
    except Exception:  # noqa: BLE001
        return False
    try:
        return _font_ink(font, _PROBE_CHAR) != _font_ink(font, _MISSING_CHAR)
    except Exception:  # noqa: BLE001
        return False


def find_cjk_font(preferred: str = "") -> Optional[str]:
    """返回一个能画中文的字体文件路径; 找不到返回 None"""
    if preferred:
        if _can_render_cjk(preferred):
            return preferred
        logger.warning(f"font_path 指定的字体无法渲染中文, 将自动探测: {preferred}")
    for path in _FONT_CANDIDATES:
        if Path(path).exists() and _can_render_cjk(path):
            return path
    return None


# ==================== 小工具 ====================

def _fit_contain(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """等比缩放并居中裁剪到不超过 box 的尺寸(等价 CSS object-fit: contain)"""
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    ratio = min(box_w / w, box_h / h)
    new_size = (max(1, int(w * ratio)), max(1, int(h * ratio)))
    return img.resize(new_size, Image.LANCZOS)


def _fit_cover(img: Image.Image, box_w: int, box_h: int) -> Image.Image:
    """等比缩放填满并居中裁剪(等价 CSS object-fit: cover), 用于头像方图"""
    w, h = img.size
    if w <= 0 or h <= 0:
        return img
    ratio = max(box_w / w, box_h / h)
    resized = img.resize((max(1, int(w * ratio)), max(1, int(h * ratio))), Image.LANCZOS)
    left = max(0, (resized.width - box_w) // 2)
    top = max(0, (resized.height - box_h) // 2)
    return resized.crop((left, top, left + box_w, top + box_h))


def _circle_avatar(img: Optional[Image.Image], size: int, fallback_text: str,
                   font: ImageFont.FreeTypeFont) -> Image.Image:
    """生成圆形头像; 没有头像时画一个带首字的灰色圆"""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    square = _fit_cover(img.convert("RGB"), size, size) if img is not None \
        else Image.new("RGB", (size, size), AVATAR_BG)

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    canvas.paste(square, (0, 0), mask)

    if img is None and fallback_text:
        draw = ImageDraw.Draw(canvas)
        ch = fallback_text.strip()[:1]
        if ch:
            bbox = draw.textbbox((0, 0), ch, font=font)
            draw.text(((size - (bbox[2] - bbox[0])) / 2 - bbox[0],
                       (size - (bbox[3] - bbox[1])) / 2 - bbox[1]),
                      ch, font=font, fill=(0x77, 0x77, 0x88))
    return canvas


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
               max_width: int, max_lines: int = 0) -> List[str]:
    """
    按像素宽度换行。

    中文没有空格分词, 因此逐字符测量; 超过行数上限时在末行加省略号。
    max_lines=0 表示不限制行数。
    """
    text = (text or "").replace("\r", "").strip()
    if not text:
        return []

    lines: List[str] = []
    cur = ""
    truncated = False
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and draw.textlength(cur + ch, font=font) > max_width:
            lines.append(cur)
            cur = ch
            if max_lines and len(lines) >= max_lines:
                truncated = True
                break
        else:
            cur += ch

    if cur:
        if max_lines and len(lines) >= max_lines:
            truncated = True          # 还有内容没放下
        else:
            lines.append(cur)

    if max_lines:
        lines = lines[:max_lines]

    if truncated and lines:
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


# ==================== 矢量小图标(不依赖 emoji 字体) ====================

def _draw_heart(d: ImageDraw.ImageDraw, x: float, y: float, size: int, color):
    r = size * 0.26
    d.ellipse((x, y, x + 2 * r, y + 2 * r), fill=color)
    d.ellipse((x + size - 2 * r, y, x + size, y + 2 * r), fill=color)
    d.polygon([(x, y + 1.25 * r), (x + size / 2, y + size), (x + size, y + 1.25 * r)], fill=color)


def _draw_comment(d: ImageDraw.ImageDraw, x: float, y: float, size: int, color):
    d.rounded_rectangle((x, y, x + size, y + size * 0.72), radius=size * 0.18, fill=color)
    d.polygon([(x + size * 0.24, y + size * 0.68),
               (x + size * 0.44, y + size * 0.68),
               (x + size * 0.26, y + size)], fill=color)


def _draw_star(d: ImageDraw.ImageDraw, x: float, y: float, size: int, color):
    cx, cy, r1, r2 = x + size / 2, y + size / 2, size / 2, size * 0.21
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rr = r1 if i % 2 == 0 else r2
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    d.polygon(pts, fill=color)


def _draw_share(d: ImageDraw.ImageDraw, x: float, y: float, size: int, color):
    w = max(2, int(size * 0.14))
    d.line((x + size * 0.1, y + size * 0.72, x + size * 0.62, y + size * 0.2), fill=color, width=w)
    d.polygon([(x + size * 0.52, y + size * 0.1), (x + size * 0.98, y + size * 0.02),
               (x + size * 0.9, y + size * 0.48)], fill=color)


def _draw_play(d: ImageDraw.ImageDraw, x: float, y: float, size: int, color):
    d.polygon([(x, y), (x + size, y + size / 2), (x, y + size)], fill=color)


def _draw_dot(d: ImageDraw.ImageDraw, x: float, y: float, size: int, color):
    d.ellipse((x, y, x + size, y + size), fill=color)


def _draw_clock(d: ImageDraw.ImageDraw, x: float, y: float, size: int, color):
    """时长用的表盘图标"""
    w = max(1, int(size * 0.13))
    d.ellipse((x, y, x + size, y + size), outline=color, width=w)
    cx, cy = x + size / 2, y + size / 2
    d.line((cx, cy, cx, y + size * 0.22), fill=color, width=w)
    d.line((cx, cy, x + size * 0.76, cy), fill=color, width=w)


class CardRenderer:
    """用 Pillow 画视频/直播卡片"""

    def __init__(self, font_path: str = "", quality: int = 88,
                 scale: float = DEFAULT_SCALE):
        self.quality = max(50, min(95, int(quality or 88)))
        try:
            self.scale = max(1.0, min(MAX_SCALE, float(scale or DEFAULT_SCALE)))
        except (TypeError, ValueError):
            self.scale = DEFAULT_SCALE
        self.font_path = find_cjk_font(font_path or "")
        self._font_cache: Dict[Tuple[int, bool], ImageFont.FreeTypeFont] = {}
        if self.font_path:
            logger.info(
                f"卡片渲染使用字体: {self.font_path} "
                f"(输出倍率 {self.scale:g}x, 成品宽度约 {int((CARD_WIDTH + 2 * PAGE_PAD) * self.scale)}px)"
            )
        else:
            logger.warning("未找到可用的中文字体, 卡片中文可能显示为方框(可用 font_path 指定)")

        data_dir = StarTools.get_data_dir(plugin_name="astrbot_plugin_amagi_douyin_push")
        self.out_dir = Path(data_dir) / "cards"
        self.out_dir.mkdir(parents=True, exist_ok=True)

    def _u(self, value: float) -> int:
        """设计单位 → 输出像素"""
        return int(round(value * self.scale))

    # ---------------- 字体 ----------------

    def font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        key = (size, bold)
        if key not in self._font_cache:
            path = self.font_path
            if bold and path:
                bold_path = path.replace("-Regular", "-Bold")
                if bold_path != path and Path(bold_path).exists():
                    path = bold_path
            try:
                self._font_cache[key] = ImageFont.truetype(path, size) if path \
                    else ImageFont.load_default()
            except Exception:  # noqa: BLE001
                self._font_cache[key] = ImageFont.load_default()
        return self._font_cache[key]

    # ---------------- 图片下载 ----------------

    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36")

    @staticmethod
    def _image_headers(url: str) -> List[Dict[str, str]]:
        """
        按图片域名给出 Referer 尝试顺序 (逐个试, 前一个失败就用下一个)。

        实测坑: B 站图床 (i0.hdslb.com) 拿到抖音 Referer 会**直接 403**,
        卡片于是只剩灰色占位(整张卡明显变小变空)。所以必须按平台挑 Referer,
        最后再兜一次「不带 Referer」(多数图床此时反而放行)。
        """
        host = (urlparse(url).netloc or "").lower()
        if "hdslb" in host or "bilibili" in host:
            referers = ["https://www.bilibili.com/", ""]
        elif "douyin" in host or "byteimg" in host or "bytedance" in host:
            referers = ["https://www.douyin.com/", ""]
        else:
            referers = ["", "https://www.douyin.com/"]
        headers: List[Dict[str, str]] = []
        for ref in referers:
            item = {"User-Agent": CardRenderer._UA}
            if ref:
                item["Referer"] = ref
            headers.append(item)
        return headers

    @classmethod
    def _load_image(cls, url: str) -> Optional[Image.Image]:
        """下载图片; 支持 http(s)、file:// 与本地路径(便于离线测试)"""
        url = (url or "").strip()
        if not url:
            return None
        if url.startswith("file://"):
            try:
                with open(url[7:], "rb") as f:
                    return Image.open(io.BytesIO(f.read()))
            except Exception as e:  # noqa: BLE001
                logger.debug(f"卡片素材读取失败({url[:60]}): {e}")
                return None
        if not url.lower().startswith(("http://", "https://")):
            try:
                if Path(url).exists():
                    with open(url, "rb") as f:
                        return Image.open(io.BytesIO(f.read()))
            except Exception as e:  # noqa: BLE001
                logger.debug(f"卡片素材读取失败({url[:60]}): {e}")
            return None

        try:
            import requests
        except Exception as e:  # noqa: BLE001
            logger.debug(f"卡片素材下载不可用(缺少 requests): {e}")
            return None

        last_err: Optional[Exception] = None
        for headers in cls._image_headers(url):
            try:
                resp = requests.get(url, timeout=10, headers=headers)
                resp.raise_for_status()
                return Image.open(io.BytesIO(resp.content))
            except Exception as e:  # noqa: BLE001
                last_err = e
        logger.debug(f"卡片素材下载失败({url[:60]}): {last_err}")
        return None

    # ---------------- 输出 ----------------

    def _save(self, img: Image.Image, tag: str) -> Optional[str]:
        try:
            path = self.out_dir / f"card_{tag}_{int(time.time())}_{uuid.uuid4().hex[:6]}.jpg"
            img.convert("RGB").save(path, format="JPEG", quality=self.quality, optimize=True)
            self._prune()
            return str(path)
        except Exception as e:  # noqa: BLE001
            logger.error(f"卡片保存失败: {e}")
            return None

    def _prune(self, max_age_secs: int = 3600) -> None:
        """清理过期的旧卡片, 避免长期占盘"""
        try:
            now = time.time()
            for f in self.out_dir.glob("card_*.jpg"):
                if now - f.stat().st_mtime > max_age_secs:
                    f.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 视频卡片 ----------------

    def render_video_card(self, data: dict) -> Optional[str]:
        u = self._u
        nickname = str(data.get("nickname") or "抖音用户")
        title = str(data.get("title") or "")
        url = str(data.get("url") or "")
        stats = [
            (_draw_heart, str(data.get("digg_count") or "0")),
            (_draw_comment, str(data.get("comment_count") or "0")),
            (_draw_star, str(data.get("collect_count") or "0")),
            (_draw_share, str(data.get("share_count") or "0")),
        ]

        f_nick = self.font(u(16), bold=True)
        f_tag = self.font(u(12), bold=True)
        f_title = self.font(u(15))
        f_stat = self.font(u(12))
        f_url = self.font(u(11))
        f_avatar = self.font(u(18), bold=True)

        avatar = self._load_image(str(data.get("avatar") or ""))
        cover = self._load_image(str(data.get("cover") or ""))

        card_w = u(CARD_WIDTH)
        inner_w = card_w - u(32)            # 卡片左右各 16 设计单位内边距
        pad = u(PAGE_PAD)

        # --- 预估高度(全部为输出像素) ---
        header_h = u(68)
        cover_h = 0
        cover_img = None
        if cover is not None:
            cover_img = _fit_contain(cover.convert("RGB"), card_w, u(COVER_BOX_H))
            cover_h = cover_img.height

        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        title_lines = _wrap_text(probe, title, f_title, inner_w, max_lines=2)
        line_h = u(21)
        title_h = len(title_lines) * line_h
        stats_h = u(20)
        url_lines = _wrap_text(probe, url, f_url, inner_w, max_lines=1)
        url_h = u(16) if url_lines else 0
        body_h = (u(12) + title_h + (u(10) if title_lines else 0) + stats_h
                  + (u(8) if url_h else 0) + url_h + u(14))

        card_h = header_h + cover_h + body_h
        canvas = Image.new("RGB", (card_w + pad * 2, card_h + pad * 2), BG_COLOR)
        draw = ImageDraw.Draw(canvas)

        # --- 卡片底 ---
        card_x, card_y = pad, pad
        draw.rounded_rectangle(
            (card_x, card_y, card_x + card_w, card_y + card_h),
            radius=u(CARD_RADIUS), fill=CARD_COLOR,
        )

        # --- 头部: 头像 + 昵称 + 标签 ---
        avatar_size = u(40)
        avatar_img = _circle_avatar(avatar, avatar_size, nickname, f_avatar)
        avatar_xy = (card_x + u(16), card_y + u(14))
        canvas.paste(avatar_img, avatar_xy, avatar_img)
        draw = ImageDraw.Draw(canvas)
        # 头像描边(抖音红)
        draw.ellipse((avatar_xy[0], avatar_xy[1],
                      avatar_xy[0] + avatar_size, avatar_xy[1] + avatar_size),
                     outline=ACCENT, width=max(2, u(2)))

        draw.text((card_x + u(66), card_y + u(18)),
                  _ellipsize(probe, nickname, f_nick, inner_w - u(60)),
                  font=f_nick, fill=TEXT_MAIN)
        _draw_play(draw, card_x + u(66), card_y + u(42), u(9), ACCENT)
        draw.text((card_x + u(80), card_y + u(39)), "新视频", font=f_tag, fill=ACCENT)

        y = card_y + header_h

        # --- 封面 ---
        if cover_img is not None:
            # 竖版封面 contain 后两侧必然留白: 用封面自身的模糊放大版铺底,
            # 比纯色留白好看得多(尺寸与版式不变, 只是换了底色)
            backdrop = _fit_cover(cover.convert("RGB"), card_w, cover_h)
            backdrop = backdrop.filter(ImageFilter.GaussianBlur(u(18)))
            canvas.paste(backdrop, (card_x, y))
            # contain 缩放后宽度可能不足卡片宽, 必须水平居中(等价 CSS 的居中)
            canvas.paste(cover_img, (card_x + (card_w - cover_img.width) // 2, y))
            draw = ImageDraw.Draw(canvas)
            y += cover_h

        # --- 正文 ---
        y += u(12)
        for line in title_lines:
            draw.text((card_x + u(16), y), line, font=f_title, fill=TEXT_BODY)
            y += line_h
        if title_lines:
            y += u(10)

        x = card_x + u(16)
        for icon_fn, value in stats:
            icon_fn(draw, x, y + u(2), u(13), TEXT_WEAK)
            x += u(17)
            draw.text((x, y), value, font=f_stat, fill=(0x55, 0x55, 0x55))
            x += int(draw.textlength(value, font=f_stat)) + u(14)
            if x > card_x + card_w - u(30):
                break

        if url_lines:
            y += stats_h + u(8)
            draw.text((card_x + u(16), y), url_lines[0], font=f_url, fill=TEXT_FAINT)

        return self._save(canvas, "v")

    # ---------------- 直播卡片 ----------------

    def render_live_card(self, data: dict) -> Optional[str]:
        u = self._u
        nickname = str(data.get("nickname") or "抖音主播")
        title = str(data.get("title") or "无标题")
        url = str(data.get("url") or "")
        is_live = bool(data.get("is_live"))
        badge_text = "直播中" if is_live else "已下播"
        badge_color = ACCENT if is_live else OFFLINE_GRAY

        f_badge = self.font(u(12), bold=True)
        f_nick = self.font(u(16), bold=True)
        f_title = self.font(u(15))
        f_url = self.font(u(11))
        f_avatar = self.font(u(22), bold=True)

        avatar = self._load_image(str(data.get("avatar") or ""))

        card_w = u(CARD_WIDTH)
        inner_w = card_w - u(32)
        pad = u(PAGE_PAD)

        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        title_lines = _wrap_text(probe, title, f_title, inner_w, max_lines=3)
        url_lines = _wrap_text(probe, url, f_url, inner_w, max_lines=1)

        header_h = u(84)
        line_h = u(21)
        body_h = (u(12) + len(title_lines) * line_h
                  + (u(8) + u(16) if url_lines else 0) + u(14))
        card_h = header_h + body_h

        canvas = Image.new("RGB", (card_w + pad * 2, card_h + pad * 2), BG_COLOR)
        draw = ImageDraw.Draw(canvas)
        card_x, card_y = pad, pad
        draw.rounded_rectangle((card_x, card_y, card_x + card_w, card_y + card_h),
                               radius=u(CARD_RADIUS), fill=CARD_COLOR)

        avatar_size = u(56)
        avatar_img = _circle_avatar(avatar, avatar_size, nickname, f_avatar)
        avatar_xy = (card_x + u(16), card_y + u(14))
        canvas.paste(avatar_img, avatar_xy, avatar_img)
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((avatar_xy[0], avatar_xy[1],
                      avatar_xy[0] + avatar_size, avatar_xy[1] + avatar_size),
                     outline=badge_color, width=max(2, u(2)))

        draw.text((card_x + u(84), card_y + u(16)),
                  _ellipsize(probe, nickname, f_nick, inner_w - u(80)),
                  font=f_nick, fill=TEXT_MAIN)

        # 徽标: 直播中 = 实心圆点, 已下播 = 空心圈
        dot_y = card_y + u(49)
        if is_live:
            _draw_dot(draw, card_x + u(88), dot_y, u(8), badge_color)
        else:
            draw.ellipse((card_x + u(88), dot_y, card_x + u(96), dot_y + u(8)),
                         outline=badge_color, width=max(2, u(2)))
        draw.text((card_x + u(102), card_y + u(46)), badge_text,
                  font=f_badge, fill=badge_color)

        y = card_y + header_h
        for line in title_lines:
            draw.text((card_x + u(16), y), line, font=f_title, fill=TEXT_BODY)
            y += line_h
        if url_lines:
            y += u(8)
            draw.text((card_x + u(16), y), url_lines[0], font=f_url, fill=TEXT_FAINT)

        return self._save(canvas, "l")

    # ---------------- B 站视频卡片 ----------------

    def render_bili_video_card(self, data: dict) -> Optional[str]:
        """
        B 站投稿视频卡片。

        与抖音卡片的差别: B 站封面是 16:9 横版, 正好铺满卡宽(无留白);
        统计项为 播放/弹幕, 并在封面右下角压一个时长胶囊。
        """
        u = self._u
        nickname = str(data.get("nickname") or "B站UP主")
        title = str(data.get("title") or "无标题")
        desc = str(data.get("desc") or "")
        url = str(data.get("url") or "")
        play = str(data.get("play") or "0")
        danmaku = str(data.get("danmaku") or "0")
        duration = str(data.get("duration") or "")

        f_nick = self.font(u(16), bold=True)
        f_tag = self.font(u(12), bold=True)
        f_title = self.font(u(15))
        f_stat = self.font(u(12))
        f_url = self.font(u(11))
        f_avatar = self.font(u(18), bold=True)
        f_dur = self.font(u(12), bold=True)

        avatar = self._load_image(str(data.get("avatar") or ""))
        cover = self._load_image(str(data.get("cover") or ""))

        card_w = u(CARD_WIDTH)
        inner_w = card_w - u(32)
        pad = u(PAGE_PAD)

        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        title_lines = _wrap_text(probe, title, f_title, inner_w, max_lines=2)
        desc_lines = _wrap_text(probe, desc, f_stat, inner_w, max_lines=1) if desc else []
        line_h = u(21)
        url_lines = _wrap_text(probe, url, f_url, inner_w, max_lines=1)

        header_h = u(68)
        cover_img = None
        cover_h = 0
        if cover is not None:
            cover_img = _fit_contain(cover.convert("RGB"), card_w, u(BILI_COVER_H))
            cover_h = cover_img.height
        body_h = (u(12) + len(title_lines) * line_h
                  + (u(6) + u(18) if desc_lines else 0)
                  + u(10) + u(20) + (u(8) + u(16) if url_lines else 0) + u(14))
        card_h = header_h + cover_h + body_h

        canvas = Image.new("RGB", (card_w + pad * 2, card_h + pad * 2), BG_COLOR)
        draw = ImageDraw.Draw(canvas)
        card_x, card_y = pad, pad
        draw.rounded_rectangle((card_x, card_y, card_x + card_w, card_y + card_h),
                               radius=u(CARD_RADIUS), fill=CARD_COLOR)

        # 头部
        avatar_size = u(40)
        avatar_img = _circle_avatar(avatar, avatar_size, nickname, f_avatar)
        avatar_xy = (card_x + u(16), card_y + u(14))
        canvas.paste(avatar_img, avatar_xy, avatar_img)
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((avatar_xy[0], avatar_xy[1],
                      avatar_xy[0] + avatar_size, avatar_xy[1] + avatar_size),
                     outline=BILI_ACCENT, width=max(2, u(2)))
        draw.text((card_x + u(66), card_y + u(18)),
                  _ellipsize(probe, nickname, f_nick, inner_w - u(60)),
                  font=f_nick, fill=TEXT_MAIN)
        _draw_play(draw, card_x + u(66), card_y + u(42), u(9), BILI_ACCENT)
        draw.text((card_x + u(80), card_y + u(39)), "新视频", font=f_tag, fill=BILI_ACCENT)

        y = card_y + header_h

        # 封面(16:9 横版, 正好铺满; 非 16:9 时用封面模糊图铺底)
        if cover_img is not None:
            if cover_img.width < card_w:
                backdrop = _fit_cover(cover.convert("RGB"), card_w, cover_h)
                canvas.paste(backdrop.filter(ImageFilter.GaussianBlur(u(18))), (card_x, y))
            canvas.paste(cover_img, (card_x + (card_w - cover_img.width) // 2, y))
            draw = ImageDraw.Draw(canvas)
            if duration:
                # 右下角时长胶囊
                tw = int(draw.textlength(duration, font=f_dur))
                pill_w, pill_h = tw + u(14), u(20)
                px, py = card_x + card_w - pill_w - u(8), y + cover_h - pill_h - u(8)
                draw.rounded_rectangle((px, py, px + pill_w, py + pill_h),
                                       radius=u(4), fill=(0x20, 0x20, 0x20))
                draw.text((px + u(7), py + u(2)), duration, font=f_dur, fill=(0xFF, 0xFF, 0xFF))
            y += cover_h

        # 正文
        y += u(12)
        for line in title_lines:
            draw.text((card_x + u(16), y), line, font=f_title, fill=TEXT_BODY)
            y += line_h
        if desc_lines:
            y += u(6)
            draw.text((card_x + u(16), y), desc_lines[0], font=f_stat, fill=TEXT_WEAK)
            y += u(18)
        y += u(10)

        x = card_x + u(16)
        for icon_fn, value in ((_draw_play, play), (_draw_comment, danmaku)):
            icon_fn(draw, x, y + u(2), u(13), TEXT_WEAK)
            x += u(17)
            draw.text((x, y), value, font=f_stat, fill=(0x55, 0x55, 0x55))
            x += int(draw.textlength(value, font=f_stat)) + u(14)
        if duration:
            _draw_clock(draw, x, y + u(2), u(13), TEXT_WEAK)
            x += u(17)
            draw.text((x, y), duration, font=f_stat, fill=(0x55, 0x55, 0x55))

        if url_lines:
            y += u(20) + u(8)
            draw.text((card_x + u(16), y), url_lines[0], font=f_url, fill=TEXT_FAINT)

        return self._save(canvas, "bv")

    # ---------------- B 站直播卡片 ----------------

    def render_bili_live_card(self, data: dict) -> Optional[str]:
        u = self._u
        nickname = str(data.get("nickname") or "B站UP主")
        title = str(data.get("title") or "无标题")
        url = str(data.get("url") or "")
        is_live = bool(data.get("is_live"))
        badge_text = "直播中" if is_live else "已下播"
        badge_color = BILI_ACCENT if is_live else OFFLINE_GRAY

        f_badge = self.font(u(12), bold=True)
        f_nick = self.font(u(16), bold=True)
        f_title = self.font(u(15))
        f_url = self.font(u(11))
        f_avatar = self.font(u(22), bold=True)

        avatar = self._load_image(str(data.get("avatar") or ""))
        cover = self._load_image(str(data.get("cover") or ""))

        card_w = u(CARD_WIDTH)
        inner_w = card_w - u(32)
        pad = u(PAGE_PAD)

        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        title_lines = _wrap_text(probe, title, f_title, inner_w, max_lines=2)
        url_lines = _wrap_text(probe, url, f_url, inner_w, max_lines=1)

        header_h = u(84)
        cover_img = None
        cover_h = 0
        if cover is not None:
            cover_img = _fit_contain(cover.convert("RGB"), card_w, u(BILI_COVER_H))
            cover_h = cover_img.height
        line_h = u(21)
        body_h = (u(12) + len(title_lines) * line_h
                  + (u(8) + u(16) if url_lines else 0) + u(14))
        card_h = header_h + cover_h + body_h

        canvas = Image.new("RGB", (card_w + pad * 2, card_h + pad * 2), BG_COLOR)
        draw = ImageDraw.Draw(canvas)
        card_x, card_y = pad, pad
        draw.rounded_rectangle((card_x, card_y, card_x + card_w, card_y + card_h),
                               radius=u(CARD_RADIUS), fill=CARD_COLOR)

        avatar_size = u(56)
        avatar_img = _circle_avatar(avatar, avatar_size, nickname, f_avatar)
        avatar_xy = (card_x + u(16), card_y + u(14))
        canvas.paste(avatar_img, avatar_xy, avatar_img)
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((avatar_xy[0], avatar_xy[1],
                      avatar_xy[0] + avatar_size, avatar_xy[1] + avatar_size),
                     outline=badge_color, width=max(2, u(2)))
        draw.text((card_x + u(84), card_y + u(16)),
                  _ellipsize(probe, nickname, f_nick, inner_w - u(80)),
                  font=f_nick, fill=TEXT_MAIN)

        dot_y = card_y + u(49)
        if is_live:
            _draw_dot(draw, card_x + u(88), dot_y, u(8), badge_color)
        else:
            draw.ellipse((card_x + u(88), dot_y, card_x + u(96), dot_y + u(8)),
                         outline=badge_color, width=max(2, u(2)))
        draw.text((card_x + u(102), card_y + u(46)), badge_text,
                  font=f_badge, fill=badge_color)

        y = card_y + header_h
        if cover_img is not None:
            if cover_img.width < card_w:
                backdrop = _fit_cover(cover.convert("RGB"), card_w, cover_h)
                canvas.paste(backdrop.filter(ImageFilter.GaussianBlur(u(18))), (card_x, y))
            canvas.paste(cover_img, (card_x + (card_w - cover_img.width) // 2, y))
            draw = ImageDraw.Draw(canvas)
            y += cover_h

        y += u(12)
        for line in title_lines:
            draw.text((card_x + u(16), y), line, font=f_title, fill=TEXT_BODY)
            y += line_h
        if url_lines:
            y += u(8)
            draw.text((card_x + u(16), y), url_lines[0], font=f_url, fill=TEXT_FAINT)

        return self._save(canvas, "bl")

    # ---------------- 线程池入口 ----------------

    async def arender_video_card(self, data: dict) -> Optional[str]:
        """PIL 绘制与图片下载都是阻塞操作, 放到线程里避免卡住事件循环"""
        return await asyncio.to_thread(self.render_video_card, data)

    async def arender_live_card(self, data: dict) -> Optional[str]:
        return await asyncio.to_thread(self.render_live_card, data)

    async def arender_bili_video_card(self, data: dict) -> Optional[str]:
        return await asyncio.to_thread(self.render_bili_video_card, data)

    async def arender_bili_live_card(self, data: dict) -> Optional[str]:
        return await asyncio.to_thread(self.render_bili_live_card, data)


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
               max_width: int) -> str:
    """单行省略"""
    text = (text or "").strip()
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"
