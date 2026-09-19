"""
消息渲染器 —— 支持图文渲染和纯文本两种模式。

图文模式（rai=true）：
  使用 HTML 模板 + AstrBot 内置 html_render 生成卡片图片。
纯文本模式（rai=false）：
  直接返回格式化的纯文本消息。

渲染结果一律经过校验：拿到路径不等于拿到图片 —— 上游 t2i 服务可能返回空内容
或错误页，这类"图片"发到协议端必然失败，因此这里校验 + 重试，失败则返回 None
由调用方降级。
"""

import asyncio
from pathlib import Path
from typing import Optional, Tuple

from astrbot.api import logger
from astrbot.api.all import Star

from ..core.models import LiveInfo, UserInfo, VideoInfo
from ..core.utils import build_live_url, build_video_url, first_url, format_number
from .card_renderer import CardRenderer

# 插件根目录
plugin_dir = Path(__file__).resolve().parent.parent

# ==================== 卡片渲染选项 ====================
#
# 为什么不用「PNG + 高分屏放大」：
#   推送图片时, 图片由 AstrBot 读成 base64 交给协议端(NapCat), 再由 QQ 上传到
#   腾讯富媒体服务器。图片体积过大时这一步会直接失败, 报
#   `rich media transfer failed` (retcode 1200), 整条推送(含链接)一起丢。
# 因此这里刻意选更小的输出:
#   - type=jpeg      : 无损 PNG 的体积通常是 JPEG 的数倍
#   - scale=css      : 不再按高分屏放大(device/ultra), 避免渲染出 2~3 倍分辨率的巨图
# 若仍偶发失败, 可把 quality 继续下调(如 50)进一步减小体积。
CARD_RENDER_OPTIONS = {
    "full_page": True,
    "type": "jpeg",
    "quality": 75,
    "scale": "css",
}

# ==================== 渲染结果校验 / 重试 ====================
CARD_MAX_ATTEMPTS = 3           # 最多尝试次数
CARD_RETRY_DELAY = 2            # 两次尝试之间的间隔(秒)
CARD_MIN_IMAGE_BYTES = 4096     # 小于此体积一律视为无效(空响应/错误页远小于它)

# ==================== 纯文本消息模板 ====================

VIDEO_TEXT_TEMPLATE = """📹 新视频发布
👤 {nickname}
📝 {desc}
❤️ {digg_count} 👍 {comment_count} 💬 {collect_count} ⭐
🔗 {url}"""

LIVE_START_TEXT = """🔴 {nickname} 开播啦！
📺 {title}
🔗 {url}"""

LIVE_END_TEXT = """⚫ {nickname} 已下播
📺 本次直播: {title}"""

# ==================== B 站纯文本模板 ====================

BILI_VIDEO_TEXT = """📺 {nickname} 发布了新视频
📝 {title}
▶️ {play}  💬 {danmaku}  ⏱ {duration}
🔗 {url}"""

BILI_LIVE_START_TEXT = """🔴 {nickname} 开播啦！
📺 {title}
🔗 {url}"""

BILI_LIVE_END_TEXT = """⚫ {nickname} 已下播
📺 本次直播: {title}
🔗 {url}"""


class Renderer:
    """消息渲染器"""

    def __init__(self, star: Star, rai: bool = False, engine: str = "local",
                 font_path: str = "", card_quality: int = 88,
                 card_scale: float = 0.0):
        self.star = star
        self.rai = rai
        # 图片卡片的渲染引擎: local = Pillow 本地自绘(默认, 不依赖外部服务)
        #                     html  = AstrBot html_render(远程 t2i, 带校验与重试)
        self.engine = (engine or "local").strip().lower()
        if self.engine not in ("local", "html"):
            logger.warning(f"未知的 card_engine={engine}, 回退为 local")
            self.engine = "local"
        self._templates = {}
        self.cards: Optional[CardRenderer] = None
        if rai:
            try:
                self.cards = CardRenderer(font_path=font_path, quality=card_quality,
                                          scale=card_scale)
            except Exception as e:  # noqa: BLE001
                logger.error(f"本地卡片渲染器初始化失败, 将改用 html 渲染: {e}")
                self.cards = None
                self.engine = "html"

    def _load_template(self, name: str) -> Optional[str]:
        """加载 HTML 模板"""
        if name in self._templates:
            return self._templates[name]
        tmpl_path = plugin_dir / "assets" / "templates" / name
        if not tmpl_path.exists():
            return None
        try:
            with open(tmpl_path, 'r', encoding='utf-8') as f:
                content = f.read()
                self._templates[name] = content
                return content
        except Exception as e:
            logger.error(f"加载模板 {name} 失败: {e}")
            return None

    async def _render_card(self, tmpl_name: Optional[str], data: dict,
                           local_kind: str = "video") -> Optional[str]:
        """
        渲染卡片图片(按 card_engine 分派)。

        local: Pillow 本地自绘 —— 内存 ~30MB、单张 ~0.2s、不依赖任何外部服务,
               小内存机器上的默认选择。
        html : AstrBot 内置 html_render(远程 t2i 服务), 带「校验 + 重试」。

        tmpl_name 为 None 表示该卡片只有本地实现(B 站卡片就是这种), 此时即使
        card_engine=html 也走本地渲染; 若本地渲染器不可用则返回 None, 由调用方降级为纯文本。
        """
        if not self.rai:
            return None
        if self.cards is not None and (self.engine == "local" or not tmpl_name):
            return await self._render_card_local(local_kind, data)
        if tmpl_name:
            return await self._render_card_html(tmpl_name, data)
        logger.warning("本地卡片渲染器不可用, 且该卡片没有 HTML 模板, 本次降级为纯文本")
        return None

    async def _render_card_local(self, kind: str, data: dict) -> Optional[str]:
        """本地 Pillow 自绘(阻塞操作已在线程池里执行)"""
        try:
            if kind == "live":
                img_path = await self.cards.arender_live_card(data)
            elif kind == "bili_video":
                img_path = await self.cards.arender_bili_video_card(data)
            elif kind == "bili_live":
                img_path = await self.cards.arender_bili_live_card(data)
            else:
                img_path = await self.cards.arender_video_card(data)
            if img_path:
                logger.info(f"卡片渲染成功(本地): {img_path}{self._size_hint(img_path)}")
            return img_path
        except Exception as e:  # noqa: BLE001
            logger.error(f"本地卡片渲染失败, 本次降级: {e}")
            return None

    async def _render_card_html(self, tmpl_name: str, data: dict) -> Optional[str]:
        """
        使用 AstrBot 内置 html_render 渲染卡片图片。

        渲染服务可能偶发返回空内容或错误页, 只判断"有没有拿到路径"是不够的,
        因此这里做「校验 + 重试」:
          - 文件必须存在, 且体积大于 CARD_MIN_IMAGE_BYTES
          - 必须能被 PIL 完整解码(HTML/JSON 错误页、被截断的图在这里被拒)
          - 最多尝试 CARD_MAX_ATTEMPTS 次, 间隔 CARD_RETRY_DELAY 秒

        全部尝试失败返回 None, 由调用方降级为「纯文本 + 封面图」。
        """
        tmpl_str = self._load_template(tmpl_name)
        if not tmpl_str:
            return None

        for attempt in range(1, CARD_MAX_ATTEMPTS + 1):
            try:
                img_path = await self.star.html_render(
                    tmpl=tmpl_str,
                    data=data,
                    return_url=False,
                    options=CARD_RENDER_OPTIONS,
                )
                if (
                    img_path
                    and Path(img_path).exists()
                    and Path(img_path).stat().st_size > CARD_MIN_IMAGE_BYTES
                    and self._validate_image(img_path)
                ):
                    logger.info(f"卡片渲染成功: {img_path}{self._size_hint(img_path)}")
                    return img_path
                logger.warning(
                    f"卡片渲染结果无效 (尝试 {attempt}/{CARD_MAX_ATTEMPTS}): "
                    f"{img_path}{self._size_hint(img_path) or ' (文件不存在)'}"
                )
            except Exception as e:
                logger.error(f"渲染图片失败 (尝试 {attempt}/{CARD_MAX_ATTEMPTS}): {e}")

            if attempt < CARD_MAX_ATTEMPTS:
                await asyncio.sleep(CARD_RETRY_DELAY)

        logger.warning(f"渲染图片失败: 已尝试 {CARD_MAX_ATTEMPTS} 次, 本次降级")
        return None

    @staticmethod
    def _validate_image(img_path: str) -> bool:
        """验证图片文件是否有效可读(PIL 能完整解码)"""
        try:
            from PIL import Image

            with Image.open(img_path) as img:
                img.verify()
            return True
        except Exception as e:
            logger.warning(f"图片验证失败 {img_path}: {e}")
            return False

    @staticmethod
    def _size_hint(img_path: str) -> str:
        """渲染结果的文件大小提示"""
        try:
            size_kb = Path(img_path).stat().st_size / 1024
        except OSError:
            return ""
        return f" ({size_kb:.0f} KB)"

    async def render_video(self, work: dict, nickname: str = "") -> Tuple[str, Optional[str]]:
        """
        渲染视频消息。返回 (消息文本, 可选的图片路径)
        """
        author = work.get('author', {})
        nickname = nickname or author.get('nickname', '未知')
        aweme_id = str(work.get('aweme_id', ''))
        desc = work.get('desc', '无标题')
        statistics = work.get('statistics', {})
        cover = first_url(work.get('video', {}).get('cover'))
        avatar = first_url(author.get('avatar_thumb'))
        digg = format_number(statistics.get('digg_count', 0))
        comment = format_number(statistics.get('comment_count', 0))
        collect = format_number(statistics.get('collect_count', 0))
        share = format_number(statistics.get('share_count', 0))
        url = build_video_url(aweme_id)

        text = VIDEO_TEXT_TEMPLATE.format(
            nickname=nickname, desc=desc[:100],
            digg_count=digg, comment_count=comment,
            collect_count=collect, url=url,
        )

        img_path = await self._render_card("video_card.html", {
            "nickname": nickname,
            "avatar": avatar,
            "cover": cover,
            "title": desc[:100],
            "digg_count": digg,
            "comment_count": comment,
            "collect_count": collect,
            "share_count": share,
            "url": url,
        }, local_kind="video")

        return text, img_path

    async def render_live(self, record, is_live: bool, title: str = "",
                          avatar: str = "", work: Optional[dict] = None) -> Tuple[str, Optional[str]]:
        """
        渲染直播消息。返回 (消息文本, 可选的图片路径)

        新版直播订阅锚定用户(sec_uid), 无房间号时链接指向用户主页。
        """
        nickname = record.nickname or getattr(record, 'sec_uid', None) or record.uid
        title = title or "无标题"

        # 链接: 有房间号(旧数据/直播间直链)用直播间, 否则用用户主页
        room_id = getattr(record, 'room_id', None) or None
        if room_id:
            url = build_live_url(room_id)
        elif getattr(record, 'sec_uid', None):
            url = build_user_url(record.sec_uid)
        else:
            url = ""

        text = (LIVE_START_TEXT if is_live else LIVE_END_TEXT).format(
            nickname=nickname, title=title, url=url,
        )

        if not avatar and work:
            avatar = first_url(work.get('author', {}).get('avatar_thumb'))

        img_path = await self._render_card("live_card.html", {
            "badge_class": "live-badge" if is_live else "offline-badge",
            "badge_text": "🔴 直播中" if is_live else "⭕ 已下播",
            "nickname": nickname,
            "avatar": avatar,
            "title": title,
            "url": url,
            "is_live": is_live,
        }, local_kind="live")

        return text, img_path

    # ---------------- B 站消息 ----------------

    async def render_bili_video(self, video: dict) -> Tuple[str, Optional[str]]:
        """
        渲染 B 站投稿视频消息。返回 (消息文本, 可选的图片路径)

        video 为 core.bilibili.extract_video() 归一化后的结构。
        """
        author = video.get("author") or {}
        nickname = author.get("name") or "B站UP主"
        title = video.get("title") or "无标题"
        desc = str(video.get("desc") or "").strip()
        url = video.get("url") or ""
        play = str(video.get("play") or "0")
        danmaku = str(video.get("danmaku") or "0")
        duration = str(video.get("duration") or "")

        text = BILI_VIDEO_TEXT.format(
            nickname=nickname, title=title, play=play,
            danmaku=danmaku, duration=duration or "-", url=url,
        )
        img_path = await self._render_card(None, {
            "nickname": nickname,
            "avatar": author.get("face") or "",
            "cover": video.get("cover") or "",
            "title": title,
            "desc": desc[:60],
            "play": play,
            "danmaku": danmaku,
            "duration": duration,
            "url": url,
        }, local_kind="bili_video")
        return text, img_path

    async def render_bili_live(self, record, is_live: bool, title: str = "",
                               cover: str = "", avatar: str = "") -> Tuple[str, Optional[str]]:
        """渲染 B 站开播/下播消息。返回 (消息文本, 可选的图片路径)"""
        nickname = record.nickname or record.uid
        room_id = getattr(record, "room_id", "") or ""
        url = (f"https://live.bilibili.com/{room_id}" if room_id
               else f"https://space.bilibili.com/{record.uid}")

        text = (BILI_LIVE_START_TEXT if is_live else BILI_LIVE_END_TEXT).format(
            nickname=nickname, title=title or "无标题", url=url,
        )
        img_path = await self._render_card(None, {
            "nickname": nickname,
            "title": title or "无标题",
            "cover": cover or "",
            "avatar": avatar or "",
            "url": url,
            "is_live": is_live,
        }, local_kind="bili_live")
        return text, img_path

    def render_bili_user_info(self, card: dict) -> str:
        """B 站用户信息（仅纯文本）"""
        fans = card.get("fans")
        sign = str(card.get("sign") or "").replace("\n", " ")[:50]
        return (
            f"👤 {card.get('name') or '未知'}\n"
            f"📝 {sign or '这个人很懒，什么都没写'}\n"
            f"👥 粉丝: {format_number(fans) if fans is not None else '未知'}\n"
            f"🔗 https://space.bilibili.com/{card.get('mid') or ''}"
        )

    def render_user_info(self, user: UserInfo) -> str:
        """渲染用户信息（仅纯文本）"""
        user_url = user.user_url or build_user_url(user.sec_uid)
        return (
            f"👤 {user.nickname}\n"
            f"📝 {user.signature[:50] if user.signature else '这个人很懒，什么都没写'}\n"
            f"👥 粉丝: {format_number(user.follower_count)}  ·  关注: {format_number(user.following_count)}\n"
            f"❤️ 获赞: {format_number(user.total_favorited)}  ·  作品: {user.aweme_count}\n"
            f"🔗 {user_url}"
        )


def build_user_url(sec_uid: str) -> str:
    """构建抖音用户主页 URL"""
    return f"https://www.douyin.com/user/{sec_uid}"