"""scheduler.py — 主循环 + 队列调度逻辑。

在独立线程中运行，与 Flask 主线程共享内存状态。
通过 threading.Lock 保护所有共享数据的读写。
"""

from __future__ import annotations

import heapq
import logging
import math
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import database as db
from config import get_config
from dreamina_cli import DreaminaCli, ExceedConcurrencyLimit, DreaminaError
from models import (
    CustomerCounters,
    ProviderState,
    QueuedTask,
    get_base_queue_type,
    is_lite_model,
    is_vip_model,
    map_to_dreamina_model,
)

logger = logging.getLogger(__name__)

# Dreamina can report a task as finished a few seconds before the account's
# submit slot is actually released. Treat official concurrency errors as
# backpressure, not as terminal task failures.
PROVIDER_RELEASE_GRACE_SECONDS = 15
CONCURRENCY_RETRY_SECONDS = 30

# ---------------------------------------------------------------------------
# 全局共享状态
# ---------------------------------------------------------------------------

lock = threading.Lock()

# Provider 运行时状态 {username: ProviderState}
provider_states: Dict[str, ProviderState] = {}

# Customer 运行时计数 {username: CustomerCounters}
customer_counters: Dict[str, CustomerCounters] = {}

# 三个队列
vip_queue: deque = deque()                # FIFO
sd2_queue: List[QueuedTask] = []          # heapq (min-heap)
sd2_fast_queue: List[QueuedTask] = []     # heapq (min-heap)

# 即梦处理中的任务 {task_id: dict(task row)}
dreamina_processing: Dict[str, dict] = {}

# Provider 轮询指针
_provider_rr_index: int = 0

# CLI 接口
cli: Optional[DreaminaCli] = None

# 控制主循环
_running = False
_thread: Optional[threading.Thread] = None


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------


def init_scheduler():
    """初始化调度器：创建 CLI 接口、从数据库恢复状态。"""
    global cli
    cfg = get_config()
    cli = DreaminaCli(
        stub_mode=cfg.dreamina.stub_mode,
        stub_completion_seconds=cfg.dreamina.stub_completion_seconds,
        cli_command=cfg.dreamina.cli_command,
    )
    _clear_runtime_state()
    _recover_state()


def _clear_runtime_state():
    """清空内存队列，避免同一进程重复初始化时重复恢复任务。"""
    global _provider_rr_index
    provider_states.clear()
    customer_counters.clear()
    vip_queue.clear()
    sd2_queue.clear()
    sd2_fast_queue.clear()
    dreamina_processing.clear()
    _provider_rr_index = 0


def _recover_state():
    """进程重启后，从数据库重建内存状态。"""
    global provider_states, customer_counters
    cfg = get_config()

    # 1. 重建 provider 状态
    providers = db.get_users_by_role("provider")
    for p in providers:
        if not p.get("cookie_path"):
            continue
        state = ProviderState(username=p["username"], cookie_path=p["cookie_path"])
        processing = db.get_tasks_by_provider_and_status(p["username"], "dreamina_processing")
        for task in processing:
            actual = map_to_dreamina_model(task["model_version"])
            if actual == "sd2":
                state.running_sd2 += 1
            elif actual == "sd2_fast":
                state.running_sd2_fast += 1
            dreamina_processing[task["task_id"]] = task
        provider_states[p["username"]] = state

    # 2. 重建 customer 计数（VIP 不计入）
    customers = db.get_users_by_role("customer")
    for cu in customers:
        counters = CustomerCounters()
        active = db.get_active_tasks_by_customer(cu["username"])
        for task in active:
            _increment_counter(counters, task["model_version"])
        customer_counters[cu["username"]] = counters

    # 3. 重建待处理队列
    queued = db.get_tasks_by_status("queued")
    for task in sorted(queued, key=lambda t: t["created_at"]):
        qt = _task_to_queued(task)
        queue_type = get_base_queue_type(task["model_version"])
        if queue_type == "vip":
            vip_queue.append(qt)
        elif queue_type == "sd2":
            heapq.heappush(sd2_queue, qt)
        else:
            heapq.heappush(sd2_fast_queue, qt)

    # 4. submitting → queued（中断恢复）
    submitting = db.get_tasks_by_status("submitting")
    for task in submitting:
        db.update_task(task["task_id"], status="queued", provider=None)
        task["status"] = "queued"
        task["provider"] = None
        qt = _task_to_queued(task)
        queue_type = get_base_queue_type(task["model_version"])
        if queue_type == "vip":
            vip_queue.append(qt)
        elif queue_type == "sd2":
            heapq.heappush(sd2_queue, qt)
        else:
            heapq.heappush(sd2_fast_queue, qt)

    logger.info("状态恢复完成: %d providers, %d customers, %d queued, %d processing",
                len(provider_states), len(customer_counters),
                len(sd2_queue) + len(sd2_fast_queue) + len(vip_queue),
                len(dreamina_processing))


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------


def start():
    """启动主循环线程。"""
    global _running, _thread
    if _thread and _thread.is_alive():
        logger.info("调度器线程已在运行，跳过重复启动")
        return
    _running = True
    _thread = threading.Thread(target=_main_loop, daemon=True, name="scheduler")
    _thread.start()
    logger.info("调度器主循环已启动")


def stop():
    """停止主循环线程。"""
    global _running
    _running = False
    if _thread:
        _thread.join(timeout=5)
    logger.info("调度器主循环已停止")


def _main_loop():
    cfg = get_config()
    interval = cfg.provider.main_loop_interval
    poll_interval = cfg.provider.poll_interval_seconds
    last_poll = 0.0

    while _running:
        now = time.time()
        try:
            with lock:
                _check_queue_timeouts(now)
                _process_pending_queues()

                if now - last_poll >= poll_interval:
                    _poll_dreamina_status()
                    last_poll = now
        except Exception:
            logger.exception("主循环异常")

        time.sleep(interval)


# ---------------------------------------------------------------------------
# 阶段 0：待处理队列超时检查
# ---------------------------------------------------------------------------


def _check_queue_timeouts(now: float):
    cfg = get_config()
    timeout = cfg.provider.task_timeout_seconds

    # sd2 / sd2_fast 队列
    for queue in [sd2_queue, sd2_fast_queue]:
        _timeout_priority_queue(queue, now, timeout)

    # VIP 队列
    timed_out = []
    remaining = deque()
    while vip_queue:
        qt = vip_queue.popleft()
        if now - qt.created_at_ts > timeout:
            timed_out.append(qt)
        else:
            remaining.append(qt)
    vip_queue.extend(remaining)

    for qt in timed_out:
        _cancel_queued_task(qt, f"排队超时（超过 {timeout} 秒）")


def _timeout_priority_queue(queue: list, now: float, timeout: float):
    timed_out = []
    remaining = []
    while queue:
        qt = heapq.heappop(queue)
        if now - qt.created_at_ts > timeout:
            timed_out.append(qt)
        else:
            remaining.append(qt)
    for qt in remaining:
        heapq.heappush(queue, qt)

    affected_customers = set()
    for qt in timed_out:
        _cancel_queued_task(qt, f"排队超时（超过 {timeout} 秒）")
        affected_customers.add(qt.customer)

    for cu in affected_customers:
        _recalculate_first_in_same_type(cu, queue)


# ---------------------------------------------------------------------------
# 阶段 1：处理待处理队列
# ---------------------------------------------------------------------------


def _process_pending_queues():
    # VIP 队列
    while vip_queue:
        qt = vip_queue[0]
        prov = _find_available_provider(lambda p: p.is_vip_available(
            get_config().provider.cooldown_seconds))
        if prov is None:
            break
        vip_queue.popleft()
        _submit_to_dreamina(qt, prov)

    # SD2 队列
    _process_priority_queue(sd2_queue, "sd2")

    # SD2 Fast 队列
    _process_priority_queue(sd2_fast_queue, "sd2_fast")


def _process_priority_queue(queue: list, queue_type: str):
    cooldown = get_config().provider.cooldown_seconds
    while queue:
        qt = queue[0]  # peek
        if queue_type == "sd2":
            prov = _find_available_provider(
                lambda p: p.is_sd2_available(cooldown))
        else:
            prov = _find_available_provider(
                lambda p: p.is_sd2_fast_available(cooldown))
        if prov is None:
            break
        heapq.heappop(queue)
        logger.info(
            "dispatch task=%s queue=%s model=%s provider=%s "
            "provider_running=(sd2:%d, sd2_fast:%d) queue_sizes=(sd2:%d, sd2_fast:%d, vip:%d)",
            qt.task_id, queue_type, qt.model_version, prov.username,
            prov.running_sd2, prov.running_sd2_fast,
            len(sd2_queue), len(sd2_fast_queue), len(vip_queue),
        )
        _submit_to_dreamina(qt, prov)


# ---------------------------------------------------------------------------
# 提交任务到即梦
# ---------------------------------------------------------------------------


def _submit_to_dreamina(qt: QueuedTask, prov: ProviderState):
    task_id = qt.task_id
    db.update_task(task_id, status="submitting", provider=prov.username)

    actual_model = map_to_dreamina_model(qt.model_version)

    # 获取素材物理路径
    media_paths = []
    for mid in qt.media_ids:
        m = db.get_media(mid)
        if m:
            # 获取绝对路径传给即梦 CLI 进行上传
            import os
            abs_path = os.path.abspath(m["file_path"])
            media_paths.append(abs_path)

    try:
        logger.info(
            "submit_attempt task=%s provider=%s model=%s actual=%s "
            "mode=%s provider_running_before=(sd2:%d, sd2_fast:%d) media_count=%d",
            task_id, prov.username, qt.model_version, actual_model, qt.generation_mode,
            prov.running_sd2, prov.running_sd2_fast, len(media_paths),
        )
        result = cli.submit(
            model=qt.model_version,
            prompt=qt.prompt,
            duration=qt.duration,
            ratio=qt.ratio,
            cookie_path=prov.cookie_path,
            media_paths=media_paths,
            generation_mode=qt.generation_mode,
        )

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        db.update_task(task_id,
                       status="dreamina_processing",
                       dreamina_task_id=result.task_id,
                       submitted_at=now_str)

        # 更新 provider 计数
        if actual_model == "sd2":
            prov.running_sd2 += 1
        elif actual_model == "sd2_fast":
            prov.running_sd2_fast += 1
        prov.last_submit_time = time.time()

        # 加入即梦处理中队列
        task_row = db.get_task(task_id)
        dreamina_processing[task_id] = task_row

        logger.info("任务 %s 已提交到即梦 (provider=%s, dreamina_id=%s)",
                    task_id, prov.username, result.task_id)

    except ExceedConcurrencyLimit as e:
        # 不应该发生 — 视为 bug
        retry_delay = max(
            CONCURRENCY_RETRY_SECONDS,
            get_config().provider.cooldown_seconds + PROVIDER_RELEASE_GRACE_SECONDS,
        )
        prov.defer(retry_delay)
        _requeue_task(qt, "即梦官方并发占用，稍后自动重试")
        logger.warning(
            "official_concurrency_busy task=%s provider=%s model=%s actual=%s "
            "provider_running=(sd2:%d, sd2_fast:%d) retry_delay=%ss error=%s",
            task_id, prov.username, qt.model_version, actual_model,
            prov.running_sd2, prov.running_sd2_fast, retry_delay, e,
        )
        return
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        db.update_task(task_id,
                       status="failed",
                       error="内部错误：即梦并发超限（不应发生，请联系管理员）",
                       failed_at=now_str,
                       provider=prov.username)
        _refund_credits(task_id, qt.customer, qt.credits)
        _decrement_counter_by_model(qt.customer, qt.model_version)
        # 标记 prov 实际有任务
        if actual_model == "sd2":
            prov.running_sd2 = 1
        elif actual_model == "sd2_fast":
            prov.running_sd2_fast = 1

    except DreaminaError as e:
        logger.error("提交失败 %s: %s", task_id, e)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        db.update_task(task_id,
                       status="failed",
                       error=str(e),
                       failed_at=now_str)
        _refund_credits(task_id, qt.customer, qt.credits)
        _decrement_counter_by_model(qt.customer, qt.model_version)

    except Exception as e:
        logger.exception("未知系统错误 %s: %s", task_id, e)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        db.update_task(task_id,
                       status="failed",
                       error=f"内部执行错误: {repr(e)}",
                       failed_at=now_str)
        _refund_credits(task_id, qt.customer, qt.credits)
        _decrement_counter_by_model(qt.customer, qt.model_version)


# ---------------------------------------------------------------------------
# 阶段 2：轮询即梦任务状态
# ---------------------------------------------------------------------------


def _poll_dreamina_status():
    for task_id in list(dreamina_processing.keys()):
        task = dreamina_processing[task_id]
        prov_name = task.get("provider")
        prov = provider_states.get(prov_name)
        if not prov:
            logger.warning("任务 %s 的 provider %s 不在状态中", task_id, prov_name)
            continue

        try:
            status = cli.query(task["dreamina_task_id"], prov.cookie_path)
        except Exception as e:
            logger.warning("查询任务 %s 状态失败: %s", task_id, e)
            continue

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if status.is_completed:
            db.update_task(task_id,
                           status="completed",
                           result_url=status.video_url,
                           completed_at=now_str,
                           progress=status.progress,
                           progress_meta=status.progress_meta)
            del dreamina_processing[task_id]
            _decrement_provider_counter(task, prov)
            _decrement_counter_by_model(task["customer"], task["model_version"])
            _reward_provider(prov, task)
            prov.defer(PROVIDER_RELEASE_GRACE_SECONDS)
            logger.info("任务 %s 完成 ✓", task_id)

        elif status.is_failed:
            db.update_task(task_id,
                           status="failed",
                           error=status.error_message,
                           failed_at=now_str)
            del dreamina_processing[task_id]
            _decrement_provider_counter(task, prov)
            _decrement_counter_by_model(task["customer"], task["model_version"])
            _refund_credits(task_id, task["customer"], task["credits"])
            prov.defer(PROVIDER_RELEASE_GRACE_SECONDS)
            logger.warning("任务 %s 失败: %s", task_id, status.error_message)

        else:
            db.update_task(task_id,
                           progress=status.progress,
                           progress_meta=status.progress_meta)
            # 更新内存中的 task 信息
            dreamina_processing[task_id] = db.get_task(task_id)


# ---------------------------------------------------------------------------
# 公开 API（被 app.py 调用，需在 lock 内执行）
# ---------------------------------------------------------------------------


def submit_task(customer: str, prompt: str, model_version: str,
                duration: int, ratio: str, media_ids: list,
                is_queued: bool, generation_mode: str = "multimodal"
                ) -> Tuple[bool, str, Optional[dict]]:
    """
    处理用户提交任务。

    返回: (success, message, task_dict)
    """
    cfg = get_config()
    models = cfg.video.models

    # 只允许配置中的真实模型，以及平台内部生成的两个 lite 排队模型。
    lite_base_models = {
        "sd2_lite": "sd2",
        "sd2_fast_lite": "sd2_fast",
    }
    base_model = lite_base_models.get(model_version, model_version)
    if model_version not in models and model_version not in lite_base_models:
        return False, f"不支持的模型: {model_version}", None

    # 计算积分
    base_cps = models[base_model].credits_per_second
    if is_lite_model(model_version):
        credits = math.floor(base_cps * 1.5 * duration)
    else:
        credits = base_cps * duration

    # 检查积分
    user = db.get_user(customer)
    if not user:
        return False, "用户不存在", None
    if user["credits"] < credits:
        return False, f"积分不足，需要 {credits}，当前 {user['credits']}", None

    # 获取或创建计数器
    counters = customer_counters.setdefault(customer, CustomerCounters())

    # VIP 类型 — 不限并发
    if is_vip_model(model_version):
        task_id = uuid.uuid4().hex
        # 扣积分
        db.adjust_credits(customer, -credits)
        db.add_transaction(customer, "deduct", -credits, task_id,
                           f"提交任务 {model_version}")
        # 创建任务
        db.create_task(task_id, customer, prompt, model_version,
                       duration, ratio, int(is_queued), credits, "queued",
                       generation_mode=generation_mode)
        if media_ids:
            db.link_task_media(task_id, media_ids)

        # 创建排队条目
        qt = QueuedTask(task_id=task_id, customer=customer,
                        model_version=model_version,
                        prompt=prompt, generation_mode=generation_mode,
                        duration=duration, ratio=ratio,
                        credits=credits, media_ids=media_ids)
        vip_queue.append(qt)

        task = db.get_task(task_id)
        return True, "ok", task

    # SD2 / SD2 Fast 普通类型 — 限制并发
    if model_version == "sd2":
        if counters.running_sd2 > 0:
            return False, "您超出了并发限制！\n\n当前已经有 SD2 任务正在处理中。请稍作等待，或者勾选下方的「由系统帮我排队」选项，将任务交给后台自动为您排队处理。", None
    elif model_version == "sd2_fast":
        if counters.running_sd2_fast > 0:
            return False, "您超出了并发限制！\n\n当前已经有 SD2 Fast 任务正在处理中。请稍作等待，或者勾选下方的「由系统帮我排队」选项，将任务交给后台自动为您排队处理。", None

    # 通过检查，创建任务
    task_id = uuid.uuid4().hex
    db.adjust_credits(customer, -credits)
    db.add_transaction(customer, "deduct", -credits, task_id,
                       f"提交任务 {model_version}")
    db.create_task(task_id, customer, prompt, model_version,
                   duration, ratio, int(is_queued), credits, "queued",
                   generation_mode=generation_mode)
    if media_ids:
        db.link_task_media(task_id, media_ids)

    # 更新 customer 计数
    _increment_counter(counters, model_version)

    # 计算 isFirstInSameType
    if model_version == "sd2_lite":
        is_first = (counters.running_sd2_lite == 1)
    elif model_version == "sd2_fast_lite":
        is_first = (counters.running_sd2_fast_lite == 1)
    else:
        is_first = True  # 普通类型经过并发检查，一定是第一个

    qt = QueuedTask(task_id=task_id, customer=customer,
                    model_version=model_version,
                    is_first_in_same_type=is_first,
                    prompt=prompt, generation_mode=generation_mode,
                    duration=duration, ratio=ratio,
                    credits=credits, media_ids=media_ids)

    # 尝试直接提交
    queue_type = get_base_queue_type(model_version)
    # 不再尝试直接同步提交，统一放入队列由后台异步线程消费
    if queue_type == "sd2":
        heapq.heappush(sd2_queue, qt)
    else:
        heapq.heappush(sd2_fast_queue, qt)

    task = db.get_task(task_id)
    return True, "ok", task


def cancel_task(customer: str, task_id: str) -> Tuple[bool, str, Optional[dict]]:
    """取消排队中的任务。返回 (success, message, task_dict)。"""
    task = db.get_task(task_id)
    if not task:
        return False, "任务不存在", None
    if task["customer"] != customer:
        return False, "无权操作", None
    if task["status"] != "queued":
        return False, "只能取消排队中的任务", None

    # 从队列中移除
    model = task["model_version"]
    queue_type = get_base_queue_type(model)
    qt_removed = None

    if queue_type == "vip":
        for i, qt in enumerate(vip_queue):
            if qt.task_id == task_id:
                del vip_queue[i]
                qt_removed = qt
                break
    elif queue_type == "sd2":
        qt_removed = _remove_from_heap(sd2_queue, task_id)
    else:
        qt_removed = _remove_from_heap(sd2_fast_queue, task_id)

    # 更新数据库
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.update_task(task_id, status="cancelled", failed_at=now_str)
    _refund_credits(task_id, customer, task["credits"])
    _decrement_counter_by_model(customer, model)

    # 重新计算 isFirstInSameType
    if queue_type == "sd2":
        _recalculate_first_in_same_type(customer, sd2_queue)
    elif queue_type == "sd2_fast":
        _recalculate_first_in_same_type(customer, sd2_fast_queue)

    updated = db.get_task(task_id)
    return True, "ok", updated


def delete_task(customer: str, task_id: str) -> Tuple[bool, str]:
    """删除已完成/失败的任务。"""
    task = db.get_task(task_id)
    if not task:
        return False, "任务不存在"
    if task["customer"] != customer:
        return False, "无权操作"
    if task["status"] not in ("completed", "failed", "rejected", "cancelled"):
        return False, "只能删除已结束的任务"
    db.delete_task(task_id)
    return True, "ok"


def get_task_display_state(task: dict) -> dict:
    """为任务生成前端展示状态。"""
    status = task["status"]
    states = {
        "queued": {"code": "waiting_provider", "label": "排队中",
                   "detail": "安心去睡觉，我会为你守候", "tone": "waiting"},
        "submitting": {"code": "submitting", "label": "提交中",
                       "detail": "正在提交到即梦", "tone": "active"},
        "dreamina_processing": {"code": "processing", "label": "生成中",
                                "detail": task.get("progress") or "即梦正在生成视频", "tone": "active"},
        "completed": {"code": "completed", "label": "已完成",
                      "detail": "视频生成成功", "tone": "success"},
        "failed": {"code": "failed", "label": "失败",
                   "detail": task.get("error") or "任务失败", "tone": "danger"},
        "rejected": {"code": "rejected", "label": "已拒绝",
                     "detail": task.get("error") or "并发超限", "tone": "danger"},
        "cancelled": {"code": "cancelled", "label": "已取消",
                      "detail": task.get("error") or "任务已取消", "tone": "muted"},
    }
    return states.get(status, {"code": status, "label": status,
                               "detail": "", "tone": "muted"})


def enrich_task(task: dict) -> dict:
    """为任务添加 display_state 和 media 信息。"""
    task["display_state"] = get_task_display_state(task)
    task["media"] = db.get_task_media(task["task_id"])
    return task


def reload_provider(username: str):
    """Admin 上传 cookie 后刷新 provider 状态。"""
    user = db.get_user(username)
    if not user or user["role"] != "provider" or not user.get("cookie_path"):
        return
    if username in provider_states:
        provider_states[username].cookie_path = user["cookie_path"]
    else:
        provider_states[username] = ProviderState(
            username=username, cookie_path=user["cookie_path"])


def get_queues_dump() -> dict:
    """获取各个队列的当前快照。"""
    def _dump_qt(qt: QueuedTask) -> dict:
        return {
            "task_id": qt.task_id,
            "customer": qt.customer,
            "model_version": qt.model_version,
            "is_first": qt.is_first_in_same_type,
            "prompt": qt.prompt,
        }

    return {
        "vip_queue": [_dump_qt(qt) for qt in vip_queue],
        "sd2_queue": [_dump_qt(qt) for qt in sorted(sd2_queue)],
        "sd2_fast_queue": [_dump_qt(qt) for qt in sorted(sd2_fast_queue)],
        "dreamina_processing": [
            {
                "task_id": t["task_id"],
                "customer": t["customer"],
                "provider": t["provider"],
                "model_version": t["model_version"]
            }
            for t in dreamina_processing.values()
        ]
    }


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------


def _find_available_provider(check_fn) -> Optional[ProviderState]:
    """Round Robin 寻找可用 provider。"""
    global _provider_rr_index
    providers = list(provider_states.values())
    n = len(providers)
    if n == 0:
        return None
    for i in range(n):
        idx = (_provider_rr_index + i) % n
        prov = providers[idx]
        if check_fn(prov):
            _provider_rr_index = (idx + 1) % n
            return prov
    return None


def _increment_counter(counters: CustomerCounters, model: str):
    if model == "sd2":
        counters.running_sd2 += 1
    elif model == "sd2_fast":
        counters.running_sd2_fast += 1
    elif model == "sd2_lite":
        counters.running_sd2_lite += 1
    elif model == "sd2_fast_lite":
        counters.running_sd2_fast_lite += 1
    # VIP 不计入


def _decrement_counter_by_model(customer: str, model: str):
    counters = customer_counters.get(customer)
    if not counters:
        return
    if model == "sd2":
        counters.running_sd2 = max(0, counters.running_sd2 - 1)
    elif model == "sd2_fast":
        counters.running_sd2_fast = max(0, counters.running_sd2_fast - 1)
    elif model == "sd2_lite":
        counters.running_sd2_lite = max(0, counters.running_sd2_lite - 1)
    elif model == "sd2_fast_lite":
        counters.running_sd2_fast_lite = max(0, counters.running_sd2_fast_lite - 1)


def _decrement_provider_counter(task: dict, prov: ProviderState):
    actual = map_to_dreamina_model(task["model_version"])
    if actual == "sd2":
        prov.running_sd2 = max(0, prov.running_sd2 - 1)
    elif actual == "sd2_fast":
        prov.running_sd2_fast = max(0, prov.running_sd2_fast - 1)


def _refund_credits(task_id: str, customer: str, credits: float):
    if credits <= 0:
        return
    db.adjust_credits(customer, credits)
    db.add_transaction(customer, "refund", credits, task_id, "任务失败/取消退款")


def _reward_provider(prov: ProviderState, task: dict):
    """Reward provider after a completed task."""
    customer_credits = float(task.get("credits") or 0)
    model_version = task.get("model_version") or ""
    duration = int(task.get("duration") or 0)

    if is_lite_model(model_version):
        base_model = model_version.replace("_lite", "")
        model_cfg = get_config().video.models.get(base_model)
        if not model_cfg:
            logger.warning("provider_reward_skipped task=%s unknown_base_model=%s",
                           task.get("task_id"), base_model)
            return
        provider_credits = model_cfg.credits_per_second * duration
        platform_credits = customer_credits - provider_credits
        note = (
            f"provider base reward {provider_credits:g}; "
            f"platform spread {platform_credits:g}"
        )
    else:
        provider_credits = customer_credits
        note = "provider reward"

    if provider_credits > 0:
        db.adjust_credits(prov.username, provider_credits)
        db.add_transaction(prov.username, "provider_reward", provider_credits,
                           task["task_id"], note)


def _cancel_queued_task(qt: QueuedTask, reason: str):
    """取消排队中的任务（超时或用户取消）。"""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.update_task(qt.task_id, status="cancelled", error=reason, failed_at=now_str)
    _refund_credits(qt.task_id, qt.customer, qt.credits)
    _decrement_counter_by_model(qt.customer, qt.model_version)

def _requeue_task(qt: QueuedTask, reason: str):
    """Put a popped task back when Dreamina says the account is temporarily busy."""
    db.update_task(qt.task_id, status="queued", provider=None, error=None, progress=reason)
    queue_type = get_base_queue_type(qt.model_version)
    if queue_type == "vip":
        vip_queue.appendleft(qt)
    elif queue_type == "sd2":
        heapq.heappush(sd2_queue, qt)
    else:
        heapq.heappush(sd2_fast_queue, qt)


def _remove_from_heap(heap: list, task_id: str) -> Optional[QueuedTask]:
    """从 heapq 中移除指定 task_id 的条目并重建堆。"""
    removed = None
    remaining = []
    for qt in heap:
        if qt.task_id == task_id:
            removed = qt
        else:
            remaining.append(qt)
    heap.clear()
    for qt in remaining:
        heapq.heappush(heap, qt)
    return removed


def _recalculate_first_in_same_type(customer: str, queue: list):
    """取消/超时后重新计算该用户在队列中的 isFirstInSameType。"""
    # 提取该用户的所有任务
    user_tasks = []
    others = []
    for qt in queue:
        if qt.customer == customer:
            user_tasks.append(qt)
        else:
            others.append(qt)

    if not user_tasks:
        return

    # 按创建时间排序
    user_tasks.sort(key=lambda t: t.created_at_ts)

    # 按模型分组，每组第一个 isFirst=True
    seen_models = set()
    for qt in user_tasks:
        base = get_base_queue_type(qt.model_version)
        # 这里用 model_version 的 lite/非lite 区分
        key = qt.model_version
        if key not in seen_models:
            qt.is_first_in_same_type = True
            seen_models.add(key)
        else:
            qt.is_first_in_same_type = False

    # 重建堆
    queue.clear()
    for qt in others + user_tasks:
        heapq.heappush(queue, qt)


def _task_to_queued(task: dict) -> QueuedTask:
    """将数据库 task dict 转为 QueuedTask。"""
    # 解析创建时间
    created_at = task.get("created_at", "")
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
        ts = dt.replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        ts = time.time()

    # 获取 media ids
    media = db.get_task_media(task["task_id"])
    media_ids = [m["media_id"] for m in media]

    return QueuedTask(
        task_id=task["task_id"],
        customer=task["customer"],
        model_version=task["model_version"],
        is_first_in_same_type=True,  # 恢复时默认 true，后续重排
        created_at_ts=ts,
        prompt=task.get("prompt", ""),
        generation_mode=task.get("generation_mode") or "multimodal",
        duration=task.get("duration", 5),
        ratio=task.get("ratio", "16:9"),
        credits=task.get("credits", 0),
        media_ids=media_ids,
    )
