"""抖音插件工具函数"""

import re
from typing import Optional


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