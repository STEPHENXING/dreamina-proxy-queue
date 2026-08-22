"""models.py — 运行时数据模型（内存中维护，不持久化）。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Provider 运行时状态
# ---------------------------------------------------------------------------

@dataclass
class ProviderState:
    username: str
    cookie_path: str  # DREAMINA_HOME 路径
    running_sd2: int = 0
    running_sd2_fast: int = 0
    last_submit_time: float = 0.0
    defer_until: float = 0.0

    def is_cooled_down(self, cooldown: float) -> bool:
        now = time.time()
        return now - self.last_submit_time >= cooldown and now >= self.defer_until

    def defer(self, seconds: float):
        self.defer_until = max(self.defer_until, time.time() + seconds)

    def is_sd2_available(self, cooldown: float) -> bool:
        return self.running_sd2 == 0 and self.is_cooled_down(cooldown)

    def is_sd2_fast_available(self, cooldown: float) -> bool:
        return self.running_sd2_fast == 0 and self.is_cooled_down(cooldown)

    def is_vip_available(self, cooldown: float) -> bool:
        return self.is_cooled_down(cooldown)


# ---------------------------------------------------------------------------
# Customer 运行时计数
# ---------------------------------------------------------------------------

@dataclass
class CustomerCounters:
    running_sd2: int = 0
    running_sd2_fast: int = 0
    running_sd2_lite: int = 0
    running_sd2_fast_lite: int = 0


# ---------------------------------------------------------------------------
# 优先级队列中的任务条目
# ---------------------------------------------------------------------------

@dataclass(order=False)
class QueuedTask:
    """待处理队列中的任务条目。实现 __lt__ 用于 heapq 排序。"""
    task_id: str
    customer: str
    model_version: str
    is_first_in_same_type: bool = True
    created_at_ts: float = field(default_factory=time.time)  # 提交时间戳
    # 以下字段在提交时不参与排序
    prompt: str = ""
    generation_mode: str = "multimodal"
    duration: int = 5
    ratio: str = "16:9"
    credits: float = 0.0
    media_ids: list = field(default_factory=list)

    def __lt__(self, other: QueuedTask) -> bool:
        """
        排序规则（用于 heapq，数值小的优先级高）：
        1. isFirstInSameType = True 的优先（True → 0, False → 1）
        2. 提交时间越早越优先
        """
        self_first = 0 if self.is_first_in_same_type else 1
        other_first = 0 if other.is_first_in_same_type else 1
        if self_first != other_first:
            return self_first < other_first
        return self.created_at_ts < other.created_at_ts

    def __eq__(self, other):
        if isinstance(other, QueuedTask):
            return self.task_id == other.task_id
        return NotImplemented

    def __hash__(self):
        return hash(self.task_id)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

# 模型到即梦实际类型的映射
_DREAMINA_MODEL_MAP = {
    "sd2": "sd2",
    "sd2_fast": "sd2_fast",
    "sd2_vip": "sd2_vip",
    "sd2_fast_vip": "sd2_fast_vip",
    "sd2_mini": "seedance2.0mini",
    "sd2_lite": "sd2",
    "sd2_fast_lite": "sd2_fast",
}


def map_to_dreamina_model(model_version: str) -> str:
    """将我们平台的模型名映射为即梦实际使用的模型名。"""
    return _DREAMINA_MODEL_MAP.get(model_version, model_version)


def get_base_queue_type(model_version: str) -> str:
    """获取队列类型：'sd2' 或 'sd2_fast' 或 'vip'。"""
    if model_version in ("sd2_vip", "sd2_fast_vip", "sd2_mini"):
        return "vip"
    if model_version in ("sd2_fast", "sd2_fast_lite"):
        return "sd2_fast"
    return "sd2"


def is_lite_model(model_version: str) -> bool:
    return model_version in ("sd2_lite", "sd2_fast_lite")


def is_vip_model(model_version: str) -> bool:
    return model_version in ("sd2_vip", "sd2_fast_vip", "sd2_mini")
