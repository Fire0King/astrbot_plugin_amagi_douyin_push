"""抖音插件工具函数"""

import os
import re
from typing import Optional

from astrbot.api import logger
from PIL import Image as PILImage

# 图片文件体积上限: 超过则不再作为图片发送, 改为退化成文件发送
MAX_IMAGE_FILE_BYTES = 10 * 1024 * 1024

# 通用平台图片高度上限
MAX_IMAGE_HEIGHT = 25000

# Telegram 特殊限制: (宽 + 高) 与长宽比的硬上限
TELEGRAM_MAX_SIDE_SUM = 10000
TELEGRAM_MAX_ASPECT_RATIO = 20


def parse_sec_uid(text: str) -> Optional[str]:
    """
    从文本中提取抖音用户的 sec_uid。

    支持格式：
    - https://www.douyin.com/user/MS4wLjABAAAA...
    - MS4wLjABAAAA... （纯 sec_uid）
    """
    text = text.strip()
    # 匹配完整 URL
    url_match = re.search(r'douyin\.com/user/([a-zA-Z0-9_-]+)', text)
    if url_match:
        return url_match.group(1)
    # 匹配纯 sec_uid（通常以 MS4wLjAB 开头）
    if re.match(r'^[a-zA-Z0-9_-]{20,}$', text):
        return text
    return None


def parse_live_room_id(text: str) -> Optional[str]:
    """
    从文本中提取抖音直播间 ID。

    支持格式：
    - https://live.douyin.com/852953608964
    - 852953608964 （纯数字ID）
    """
    text = text.strip()
    # 匹配直播 URL
    url_match = re.search(r'live\.douyin\.com/(\d+)', text)
    if url_match:
        return url_match.group(1)
    # 匹配纯数字 ID（抖音直播间ID通常是10-19位数字）
    if text.isdigit() and 5 < len(text) < 20:
        return text
    return None


def first_url(media) -> str:
    """
    从 {"url_list": [...]} 结构中安全取第一个 URL。

    注意: 不要写成 `media.get("url_list", [None])[0]` —— 默认值只在键**缺失**时生效,
    若 url_list 存在但为空列表(抖音返回空封面/空头像时很常见)会抛 IndexError,
    导致整条推送失败。
    """
    if not isinstance(media, dict):
        return ""
    url_list = media.get("url_list")
    if not isinstance(url_list, (list, tuple)) or not url_list:
        return ""
    return str(url_list[0] or "")


def format_number(num) -> str:
    """格式化数字（万、亿）"""
    try:
        num = int(num)
    except (ValueError, TypeError):
        return str(num)
    if num >= 100000000:
        return f"{num / 100000000:.1f}亿"
    elif num >= 10000:
        return f"{num / 10000:.1f}万"
    return str(num)


def build_user_url(sec_uid: str) -> str:
    """构建抖音用户主页 URL"""
    return f"https://www.douyin.com/user/{sec_uid}"


def build_video_url(aweme_id: str) -> str:
    """构建抖音视频 URL"""
    return f"https://www.douyin.com/video/{aweme_id}"


def build_live_url(room_id: str) -> str:
    """构建抖音直播 URL"""
    return f"https://live.douyin.com/{room_id}"


def is_height_valid(img_path: str, platform_name: str = "",
                    max_height: int = MAX_IMAGE_HEIGHT) -> bool:
    """
    检查图片能否作为**图片**消息发送 (而不是退化成文件)。

    各平台对图片的尺寸与体积限制不同, 超限时平台会直接拒绝, 因此调用方应当在
    返回 False 时改用 File 组件发送, 否则图片会发不出去。

    - 体积 > 10MB: 一律不允许
    - Telegram: (宽 + 高) <= 10000 且 长边/短边 <= 20
    - 其他平台: 高度 <= 25000 (如 QQ 的长图限制)
    """
    try:
        if os.path.getsize(img_path) > MAX_IMAGE_FILE_BYTES:
            return False

        with PILImage.open(img_path) as img:
            width, height = img.size

        if platform_name == "telegram":
            if (width + height) > TELEGRAM_MAX_SIDE_SUM:
                return False
            longer, shorter = max(width, height), min(width, height)
            if shorter > 0 and (longer / shorter) > TELEGRAM_MAX_ASPECT_RATIO:
                return False
            return True

        return height <= max_height
    except Exception as e:
        logger.error(f"无法打开图片 {img_path} 进行尺寸检查: {e}")
        return False