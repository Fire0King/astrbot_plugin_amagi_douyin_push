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

from astrbot.api import logger
from astrbot.api.star import StarTools
from PIL import Image, ImageDraw, ImageFont

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
OFFLINE_GRAY = (0x99, 0x99, 0x99)
COVER_BG = (0xF0, 0xF0, 0xF0)
AVATAR_BG = (0xE6, 0xE6, 0xEA)

COVER_BOX_H = 420                      # 封面展示区高度(contain 适配)

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


class CardRenderer:
    """用 Pillow 画视频/直播卡片"""

    def __init__(self, font_path: str = "", quality: int = 80):
        self.quality = max(50, min(95, int(quality or 80)))
        self.font_path = find_cjk_font(font_path or "")
        self._font_cache: Dict[Tuple[int, bool], ImageFont.FreeTypeFont] = {}
        if self.font_path:
            logger.info(f"卡片渲染使用字体: {self.font_path}")
        else:
            logger.warning("未找到可用的中文字体, 卡片中文可能显示为方框(可用 font_path 指定)")

        data_dir = StarTools.get_data_dir(plugin_name="astrbot_plugin_amagi_douyin_push")
        self.out_dir = Path(data_dir) / "cards"
        self.out_dir.mkdir(parents=True, exist_ok=True)

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

    @staticmethod
    def _load_image(url: str) -> Optional[Image.Image]:
        """下载图片; 支持 http(s)、file:// 与本地路径(便于离线测试)"""
        url = (url or "").strip()
        if not url:
            return None
        try:
            if url.startswith("file://"):
                with open(url[7:], "rb") as f:
                    return Image.open(io.BytesIO(f.read()))
            if not url.lower().startswith(("http://", "https://")):
                if Path(url).exists():
                    with open(url, "rb") as f:
                        return Image.open(io.BytesIO(f.read()))
                return None
            import requests
            resp = requests.get(
                url, timeout=10,
                headers={
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"),
                    "Referer": "https://www.douyin.com/",
                },
            )
            resp.raise_for_status()
            return Image.open(io.BytesIO(resp.content))
        except Exception as e:  # noqa: BLE001
            logger.debug(f"卡片素材下载失败({url[:60]}): {e}")
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
        nickname = str(data.get("nickname") or "抖音用户")
        title = str(data.get("title") or "")
        url = str(data.get("url") or "")
        stats = [
            (_draw_heart, str(data.get("digg_count") or "0")),
            (_draw_comment, str(data.get("comment_count") or "0")),
            (_draw_star, str(data.get("collect_count") or "0")),
            (_draw_share, str(data.get("share_count") or "0")),
        ]

        f_nick = self.font(16, bold=True)
        f_tag = self.font(12, bold=True)
        f_title = self.font(15)
        f_stat = self.font(12)
        f_url = self.font(11)
        f_avatar = self.font(18, bold=True)

        avatar = self._load_image(str(data.get("avatar") or ""))
        cover = self._load_image(str(data.get("cover") or ""))

        inner_w = CARD_WIDTH - 32           # 卡片左右各 16px 内边距

        # --- 预估高度 ---
        header_h = 68
        cover_h = 0
        cover_img = None
        if cover is not None:
            cover_img = _fit_contain(cover.convert("RGB"), CARD_WIDTH, COVER_BOX_H)
            cover_h = cover_img.height

        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        title_lines = _wrap_text(probe, title, f_title, inner_w, max_lines=2)
        title_h = len(title_lines) * 21
        stats_h = 20
        url_lines = _wrap_text(probe, url, f_url, inner_w, max_lines=1)
        url_h = 16 if url_lines else 0
        body_h = 12 + title_h + (10 if title_lines else 0) + stats_h + (8 if url_h else 0) + url_h + 14

        card_h = header_h + cover_h + body_h
        canvas = Image.new("RGB", (CARD_WIDTH + PAGE_PAD * 2, card_h + PAGE_PAD * 2), BG_COLOR)
        draw = ImageDraw.Draw(canvas)

        # --- 卡片底 ---
        card_x, card_y = PAGE_PAD, PAGE_PAD
        draw.rounded_rectangle(
            (card_x, card_y, card_x + CARD_WIDTH, card_y + card_h),
            radius=CARD_RADIUS, fill=CARD_COLOR,
        )

        # --- 头部: 头像 + 昵称 + 标签 ---
        avatar_img = _circle_avatar(avatar, 40, nickname, f_avatar)
        canvas.paste(avatar_img, (card_x + 16, card_y + 14), avatar_img)
        draw = ImageDraw.Draw(canvas)
        # 头像描边(抖音红)
        draw.ellipse((card_x + 16, card_y + 14, card_x + 56, card_y + 54), outline=ACCENT, width=2)

        draw.text((card_x + 66, card_y + 18), _ellipsize(probe, nickname, f_nick, inner_w - 60),
                  font=f_nick, fill=TEXT_MAIN)
        _draw_play(draw, card_x + 66, card_y + 42, 9, ACCENT)
        draw.text((card_x + 80, card_y + 39), "新视频", font=f_tag, fill=ACCENT)

        y = card_y + header_h

        # --- 封面 ---
        if cover_img is not None:
            draw.rectangle((card_x, y, card_x + CARD_WIDTH, y + cover_h), fill=COVER_BG)
            # contain 缩放后宽度可能不足卡片宽, 必须水平居中(等价 CSS 的 text-align/居中)
            canvas.paste(cover_img, (card_x + (CARD_WIDTH - cover_img.width) // 2, y))
            draw = ImageDraw.Draw(canvas)
            y += cover_h

        # --- 正文 ---
        y += 12
        for line in title_lines:
            draw.text((card_x + 16, y), line, font=f_title, fill=TEXT_BODY)
            y += 21
        if title_lines:
            y += 10

        x = card_x + 16
        for icon_fn, value in stats:
            icon_fn(draw, x, y + 2, 13, TEXT_WEAK)
            x += 17
            draw.text((x, y), value, font=f_stat, fill=(0x55, 0x55, 0x55))
            x += int(draw.textlength(value, font=f_stat)) + 14
            if x > card_x + CARD_WIDTH - 30:
                break

        if url_lines:
            y += stats_h + 8
            draw.text((card_x + 16, y), url_lines[0], font=f_url, fill=TEXT_FAINT)

        return self._save(canvas, "v")

    # ---------------- 直播卡片 ----------------

    def render_live_card(self, data: dict) -> Optional[str]:
        nickname = str(data.get("nickname") or "抖音主播")
        title = str(data.get("title") or "无标题")
        url = str(data.get("url") or "")
        is_live = bool(data.get("is_live"))
        badge_text = "直播中" if is_live else "已下播"
        badge_color = ACCENT if is_live else OFFLINE_GRAY

        f_badge = self.font(12, bold=True)
        f_nick = self.font(16, bold=True)
        f_title = self.font(15)
        f_url = self.font(11)
        f_avatar = self.font(22, bold=True)

        avatar = self._load_image(str(data.get("avatar") or ""))

        inner_w = CARD_WIDTH - 32
        probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        title_lines = _wrap_text(probe, title, f_title, inner_w, max_lines=3)
        url_lines = _wrap_text(probe, url, f_url, inner_w, max_lines=1)

        header_h = 84
        body_h = 12 + len(title_lines) * 21 + (8 + 16 if url_lines else 0) + 14
        card_h = header_h + body_h

        canvas = Image.new("RGB", (CARD_WIDTH + PAGE_PAD * 2, card_h + PAGE_PAD * 2), BG_COLOR)
        draw = ImageDraw.Draw(canvas)
        card_x, card_y = PAGE_PAD, PAGE_PAD
        draw.rounded_rectangle((card_x, card_y, card_x + CARD_WIDTH, card_y + card_h),
                               radius=CARD_RADIUS, fill=CARD_COLOR)

        avatar_img = _circle_avatar(avatar, 56, nickname, f_avatar)
        canvas.paste(avatar_img, (card_x + 16, card_y + 14), avatar_img)
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((card_x + 16, card_y + 14, card_x + 72, card_y + 70),
                     outline=badge_color, width=2)

        draw.text((card_x + 84, card_y + 16), _ellipsize(probe, nickname, f_nick, inner_w - 80),
                  font=f_nick, fill=TEXT_MAIN)

        # 徽标胶囊
        bw = int(draw.textlength(badge_text, font=f_badge)) + 30
        if is_live:
            _draw_dot(draw, card_x + 88, card_y + 49, 8, badge_color)
        else:
            draw.ellipse((card_x + 88, card_y + 49, card_x + 96, card_y + 57),
                         outline=badge_color, width=2)
        draw.text((card_x + 102, card_y + 46), badge_text, font=f_badge, fill=badge_color)

        y = card_y + header_h
        for line in title_lines:
            draw.text((card_x + 16, y), line, font=f_title, fill=TEXT_BODY)
            y += 21
        if url_lines:
            y += 8
            draw.text((card_x + 16, y), url_lines[0], font=f_url, fill=TEXT_FAINT)

        return self._save(canvas, "l")

    # ---------------- 线程池入口 ----------------

    async def arender_video_card(self, data: dict) -> Optional[str]:
        """PIL 绘制与图片下载都是阻塞操作, 放到线程里避免卡住事件循环"""
        return await asyncio.to_thread(self.render_video_card, data)

    async def arender_live_card(self, data: dict) -> Optional[str]:
        return await asyncio.to_thread(self.render_live_card, data)


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
               max_width: int) -> str:
    """单行省略"""
    text = (text or "").strip()
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"
