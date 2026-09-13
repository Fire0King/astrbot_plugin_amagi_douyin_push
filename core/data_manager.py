import json
import os
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.star import StarTools

from .models import SubscriptionRecord


class DataManager:
    """数据管理器，负责订阅数据的持久化"""

    def __init__(self):
        # 使用 AstrBot 标准数据目录（防止更新/重装插件时数据被覆盖）
        data_dir = StarTools.get_data_dir(plugin_name="astrbot_plugin_amagi_douyin_push")
        os.makedirs(data_dir, exist_ok=True)
        self.subscriptions_file = os.path.join(data_dir, "subscriptions.json")
        self._subscriptions: Dict[str, List[SubscriptionRecord]] = {}
        self._load()

    def _load(self):
        """从文件加载订阅数据"""
        if os.path.exists(self.subscriptions_file):
            try:
                with open(self.subscriptions_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for key, records in data.items():
                        self._subscriptions[key] = [
                            SubscriptionRecord(**r) for r in records
                        ]
                logger.info(f"加载订阅数据成功: {len(self._subscriptions)} 个会话")
            except Exception as e:
                logger.error(f"加载订阅数据失败: {e}")

    def _save(self):
        """保存订阅数据到文件"""
        try:
            data = {}
            for key, records in self._subscriptions.items():
                data[key] = [r.__dict__ for r in records]
            with open(self.subscriptions_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存订阅数据失败: {e}")

    def get_subscriptions(self, sub_user: str) -> List[SubscriptionRecord]:
        """获取某个会话的所有订阅"""
        return self._subscriptions.get(sub_user, [])

    def get_all_subscriptions(self) -> Dict[str, List[SubscriptionRecord]]:
        """获取所有会话的订阅"""
        return self._subscriptions

    def add_subscription(self, sub_user: str, record: SubscriptionRecord) -> bool:
        """添加订阅"""
        if sub_user not in self._subscriptions:
            self._subscriptions[sub_user] = []
        # 检查是否已存在
        for r in self._subscriptions[sub_user]:
            if r.uid == record.uid and r.sub_type == record.sub_type:
                return False
        self._subscriptions[sub_user].append(record)
        self._save()
        return True

    def remove_subscription(self, sub_user: str, uid: str, sub_type: str) -> bool:
        """移除订阅"""
        if sub_user not in self._subscriptions:
            return False
        original_len = len(self._subscriptions[sub_user])
        self._subscriptions[sub_user] = [
            r for r in self._subscriptions[sub_user]
            if not (r.uid == uid and r.sub_type == sub_type)
        ]
        if len(self._subscriptions[sub_user]) != original_len:
            self._save()
            return True
        return False

    def update_subscription(self, sub_user: str, uid: str, sub_type: str, **kwargs):
        """更新订阅信息"""
        if sub_user not in self._subscriptions:
            return False
        for r in self._subscriptions[sub_user]:
            if r.uid == uid and r.sub_type == sub_type:
                for key, value in kwargs.items():
                    if hasattr(r, key):
                        setattr(r, key, value)
                self._save()
                return True
        return False

    def get_subscription(self, sub_user: str, uid: str, sub_type: str) -> Optional[SubscriptionRecord]:
        """获取单个订阅"""
        if sub_user not in self._subscriptions:
            return None
        for r in self._subscriptions[sub_user]:
            if r.uid == uid and r.sub_type == sub_type:
                return r
        return None