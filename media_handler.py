"""media_handler.py — 素材上传、缩略图生成。

处理客户上传的参考图片和音频文件。
图片会生成 128×128 的缩略图以便前端快速展示。
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Pillow 是可选依赖，没有的时候跳过缩略图生成
try:
    from PIL import Image
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False
    logger.warning("Pillow 未安装，将跳过缩略图生成")


THUMB_SIZE = (128, 128)
ALLOWED_EXTENSIONS = {
    "image": {".jpg", ".jpeg", ".png", ".webp"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg"},
}


def save_upload(file_storage, customer: str, kind: str,
                data_dir: str) -> Tuple[str, str, str, Optional[str]]:
    """
    保存上传文件，返回 (media_id, file_path, thumb_path, original_name)。

    参数:
        file_storage: Flask 的 FileStorage 对象
        customer: 上传者用户名
        kind: 'image' | 'audio'
        data_dir: 数据目录根路径

    返回:
        (media_id, file_path, thumb_path, original_name)
        thumb_path 仅在 kind='image' 且 Pillow 可用时有值。
    """
    media_id = uuid.uuid4().hex
    original_name = file_storage.filename or f"upload_{media_id}"
    if kind not in ALLOWED_EXTENSIONS:
        raise ValueError("不支持的素材类型")

    # 保存原始文件
    upload_dir = os.path.join(data_dir, "uploads", customer)
    os.makedirs(upload_dir, exist_ok=True)

    _, ext = os.path.splitext(original_name)
    ext = ext.lower()
    if ext not in ALLOWED_EXTENSIONS[kind]:
        raise ValueError("不支持的文件格式")

    safe_name = f"{media_id}{ext}"
    file_path = os.path.join(upload_dir, safe_name)
    file_storage.save(file_path)

    # 生成缩略图
    thumb_path = None
    if kind == "image" and _HAS_PIL:
        try:
            thumb_dir = os.path.join(data_dir, "thumbs", customer)
            os.makedirs(thumb_dir, exist_ok=True)
            thumb_path = os.path.join(thumb_dir, f"{media_id}_thumb.jpg")
            img = Image.open(file_path)
            img.thumbnail(THUMB_SIZE)
            # 转为 RGB（处理 RGBA/P 模式）
            if img.mode not in ("RGB",):
                img = img.convert("RGB")
            img.save(thumb_path, "JPEG", quality=80)
        except Exception as e:
            logger.warning("缩略图生成失败 (%s): %s", original_name, e)
            thumb_path = None

    return media_id, file_path, thumb_path, original_name
