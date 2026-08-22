"""dreamina_cli.py — 即梦 CLI 封装层。

支持两种模式：
1. stub 模式（开发/测试）：不调用真实 CLI，模拟提交和完成。
2. 真实模式（生产）：调用即梦 CLI 可执行文件。

两种模式均通过 DREAMINA_HOME 环境变量切换 provider 账号。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class SubmitResult:
    """即梦提交任务返回结果。"""
    task_id: str  # 即梦任务 ID


@dataclass
class TaskStatus:
    """即梦任务状态查询结果。"""
    is_completed: bool = False
    is_failed: bool = False
    video_url: str = ""
    error_message: str = ""
    progress: str = ""
    progress_meta: str = ""  # JSON 字符串


class ExceedConcurrencyLimit(Exception):
    """即梦并发超限异常。"""
    pass


class DreaminaError(Exception):
    """即梦其他错误。"""
    pass


def _compact_text(value, limit: int = 1200) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "...（已截断）"


def _json_error_message(output: dict, fallback: str = "任务失败") -> str:
    parts = []
    for key in ("fail_reason", "error", "message", "ret", "code", "logid"):
        value = output.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    if parts:
        return _compact_text("; ".join(parts))
    return _compact_text(f"{fallback}: {json.dumps(output, ensure_ascii=False)}")


def _extract_cli_json(text: str) -> Optional[dict]:
    """Extract a JSON object from CLI output that may include noisy log lines."""
    raw = (text or "").strip()
    if not raw:
        return None

    try:
        output = json.loads(raw)
        return output if isinstance(output, dict) else None
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    candidates = []
    for match in re.finditer(r"\{", raw):
        try:
            value, _end = decoder.raw_decode(raw[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(value)

    for value in reversed(candidates):
        if any(key in value for key in ("submit_id", "gen_status", "status", "result_json")):
            return value
    return candidates[-1] if candidates else None


def _simple_submit_id(text: str) -> str:
    """Return a bare submit id only when stdout is exactly a small id-like value."""
    raw = (text or "").strip()
    if "\n" in raw or "\r" in raw or len(raw) > 128:
        return ""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}", raw):
        return raw
    return ""


# ---------------------------------------------------------------------------
# Stub 模式实现（开发测试用）
# ---------------------------------------------------------------------------

# stub 内存中的"任务"
_stub_tasks: Dict[str, dict] = {}


def _stub_submit(model: str, prompt: str, duration: int, ratio: str,
                 cookie_path: str, media_paths: list = None,
                 generation_mode: str = "multimodal") -> SubmitResult:
    """模拟提交任务。"""
    task_id = f"stub_{uuid.uuid4().hex[:12]}"
    _stub_tasks[task_id] = {
        "model": model,
        "generation_mode": generation_mode,
        "prompt": prompt,
        "submitted_at": time.time(),
        "status": "processing",
    }
    logger.info("[STUB] 提交任务 %s (model=%s, prompt=%s, cookie=%s)",
                task_id, model, prompt[:30], cookie_path)
    return SubmitResult(task_id=task_id)


def _stub_query(dreamina_task_id: str, cookie_path: str,
                completion_seconds: float = 10) -> TaskStatus:
    """模拟查询任务状态，经过 completion_seconds 秒后自动完成。"""
    task = _stub_tasks.get(dreamina_task_id)
    if task is None:
        return TaskStatus(is_failed=True, error_message="stub 任务不存在")

    elapsed = time.time() - task["submitted_at"]
    if elapsed >= completion_seconds:
        task["status"] = "completed"
        return TaskStatus(
            is_completed=True,
            video_url=f"https://stub.dreamina.example/video/{dreamina_task_id}.mp4",
            progress="100%",
        )
    else:
        pct = min(99, int(elapsed / completion_seconds * 100))
        remaining = int(completion_seconds - elapsed)
        meta = json.dumps({"remaining_seconds": remaining})
        return TaskStatus(
            progress=f"{pct}%",
            progress_meta=meta,
        )


# ---------------------------------------------------------------------------
# 真实 CLI 实现
# ---------------------------------------------------------------------------


def _real_submit(model: str, prompt: str, duration: int, ratio: str,
                 cookie_path: str, cli_command: str = "dreamina",
                 media_paths: list = None,
                 generation_mode: str = "multimodal") -> SubmitResult:
    """调用即梦 CLI 提交视频生成任务。"""
    env = os.environ.copy()
    env["DREAMINA_HOME"] = cookie_path
    env["HOME"] = cookie_path

    command = "text2video" if generation_mode == "text2video" else "multimodal2video"
    cmd = [
        cli_command, command,
        f"--prompt={prompt}",
        f"--duration={duration}",
        f"--ratio={ratio}",
    ]
    
    # 添加模型参数
    if model == "sd2_vip":
        cmd.append("--model_version=seedance2.0_vip")
    elif model == "sd2_fast_vip":
        cmd.append("--model_version=seedance2.0fast_vip")
    elif model == "sd2_mini":
        cmd.append("--model_version=seedance2.0mini")
    elif model in ("sd2_fast", "sd2_fast_lite"):
        cmd.append("--model_version=seedance2.0fast")
    else:
        cmd.append("--model_version=seedance2.0")
    cmd.append("--video_resolution=720p")

    # 逐个添加图片
    if generation_mode != "text2video" and media_paths:
        for path in media_paths:
            cmd.append(f"--image={path}")

    logger.info("[CLI] 执行: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=60, encoding='utf-8'
        )
    except subprocess.TimeoutExpired:
        raise DreaminaError("即梦 CLI 执行超时")

    if result.returncode != 0:
        err_msg = result.stderr.strip() or result.stdout.strip() or "CLI 未返回错误详情"
        err_msg = _compact_text(err_msg)
        if "ExceedConcurrencyLimit" in err_msg:
            raise ExceedConcurrencyLimit(err_msg)
        raise DreaminaError(f"即梦 CLI 失败 (code={result.returncode}): {err_msg}")

    output = _extract_cli_json(result.stdout)
    raw_stdout = result.stdout.strip()

    if output:
        # 处理可能的 JSON 报错 (returncode == 0 但内部含有 gen_status="fail")
        if output.get("gen_status") == "fail":
            fail_reason = _json_error_message(output, "即梦提交失败")
            if "ExceedConcurrencyLimit" in fail_reason:
                raise ExceedConcurrencyLimit(fail_reason)
            raise DreaminaError(f"即梦 CLI 提交失败: {fail_reason}")

        submit_id = output.get("submit_id") or ""
        if submit_id:
            return SubmitResult(task_id=submit_id)

    if "ExceedConcurrencyLimit" in raw_stdout:
        raise ExceedConcurrencyLimit(_compact_text(raw_stdout))

    submit_id = _simple_submit_id(raw_stdout)
    if submit_id:
        return SubmitResult(task_id=submit_id)

    raise DreaminaError(f"无法从 CLI 输出中找到有效 submit_id: {_compact_text(raw_stdout)}")


def _real_query(dreamina_task_id: str, cookie_path: str,
                cli_command: str = "dreamina") -> TaskStatus:
    """调用即梦 CLI 查询任务状态。"""
    env = os.environ.copy()
    env["DREAMINA_HOME"] = cookie_path
    env["HOME"] = cookie_path

    # multimodal2video 必须使用 query_result
    cmd = [cli_command, "query_result", f"--submit_id={dreamina_task_id}"]

    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=30, encoding='utf-8'
        )
    except subprocess.TimeoutExpired:
        logger.warning("[CLI] 查询超时: %s", dreamina_task_id)
        return TaskStatus(progress="查询超时")

    if result.returncode != 0:
        err_msg = result.stderr.strip() or result.stdout.strip() or "任务不存在或查询异常"
        err_msg = _compact_text(err_msg, 500)
        logger.warning("[CLI] 查询暂时失败 (code=%d): %s", result.returncode, err_msg)
        return TaskStatus(progress=f"查询暂时失败，稍后自动重试: {err_msg}")

    output = _extract_cli_json(result.stdout)
    if not output:
        return TaskStatus(progress=_compact_text(result.stdout, 300))

    gen_status = output.get("gen_status", "")
    video_url = _extract_video_url(output)
    
    # 兼容老接口的 status 和新接口的 gen_status
    if gen_status == "success" or output.get("status") == "completed":
        return TaskStatus(
            is_completed=True,
            video_url=video_url,
            progress="100%",
            progress_meta=json.dumps({
                k: v for k, v in output.items()
                if k not in ("status", "gen_status", "video_url", "url")
            }),
        )
    elif gen_status == "fail" or output.get("status") in ("failed", "error", "cancelled", "canceled", "aborted"):
        return TaskStatus(
            is_failed=True,
            error_message=_json_error_message(output, "任务生成失败"),
        )
    else:
        # 如果不是成功或失败，则认为还在排队/处理中
        queue_info = output.get("queue_info", {})
        progress = output.get("progress", gen_status or output.get("status", "processing"))
        if queue_info.get("queue_status") == "Running":
            progress = "生成中..."
        elif queue_info.get("queue_status") == "Queueing":
            idx = queue_info.get("queue_idx", "?")
            length = queue_info.get("queue_length", "?")
            priority = queue_info.get("priority", "?")
            progress = f"即梦官方排队中 (第 {idx} 位 / 共 {length} 人，优先级: {priority})"
            
            # 动态附加其他所有可能有用的排队字段（例如预计时间 est_wait_time 等）
            extra_parts = []
            for k, v in queue_info.items():
                if k not in ["queue_idx", "queue_length", "priority", "queue_status", "debug_info"]:
                    # 尝试将常见的英文键名翻译一下
                    key_name = k
                    if "time" in k.lower():
                        key_name = "预计时间"
                    extra_parts.append(f"{key_name}: {v}")
            if extra_parts:
                progress += f" [ {', '.join(extra_parts)} ]"
            
        meta = {k: v for k, v in output.items() if k not in ("status", "gen_status")}
        return TaskStatus(
            progress=str(progress),
            progress_meta=json.dumps(meta) if meta else "",
        )


def _extract_video_url(output: dict) -> str:
    """Return the generated video URL across known Dreamina response shapes."""
    for key in ("video_url", "url", "download_url", "play_url"):
        value = output.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value

    result_json = output.get("result_json")
    if isinstance(result_json, str):
        try:
            result_json = json.loads(result_json)
        except json.JSONDecodeError:
            result_json = None

    if isinstance(result_json, dict):
        videos = result_json.get("videos")
        if isinstance(videos, list):
            for video in videos:
                if not isinstance(video, dict):
                    continue
                for key in ("video_url", "url", "download_url", "play_url"):
                    value = video.get(key)
                    if isinstance(value, str) and value.startswith(("http://", "https://")):
                        return value

    return ""


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


class DreaminaCli:
    """即梦 CLI 统一接口。根据配置自动选择 stub 或真实模式。"""

    def __init__(self, stub_mode: bool = True, stub_completion_seconds: float = 10,
                 cli_command: str = "dreamina"):
        self.stub_mode = stub_mode
        self.stub_completion_seconds = stub_completion_seconds
        self.cli_command = cli_command

    def submit(self, model: str, prompt: str, duration: int, ratio: str,
               cookie_path: str, media_paths: list = None,
               generation_mode: str = "multimodal") -> SubmitResult:
        if self.stub_mode:
            return _stub_submit(model, prompt, duration, ratio, cookie_path, media_paths,
                                generation_mode)
        return _real_submit(model, prompt, duration, ratio, cookie_path,
                            self.cli_command, media_paths, generation_mode)

    def query(self, dreamina_task_id: str, cookie_path: str) -> TaskStatus:
        if self.stub_mode:
            return _stub_query(dreamina_task_id, cookie_path,
                               self.stub_completion_seconds)
        return _real_query(dreamina_task_id, cookie_path, self.cli_command)
