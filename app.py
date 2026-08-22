"""app.py — Flask 入口，路由定义。

功能：
- 登录/登出
- Customer 面板（提交任务、查看任务、上传素材）
- Admin 面板（管理用户、积分、查看任务、上传 provider cookie）
- API 路由（JSON 接口供前端 JS 调用）
- 静态资源服务（素材缩略图）
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import sqlite3
import secrets
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import random
import string
from io import BytesIO
from logging.handlers import RotatingFileHandler
from captcha.image import ImageCaptcha

from flask import (
    Flask,
    abort,
    flash,
    get_flashed_messages,
    jsonify,
    redirect,
    render_template,
    Response,
    request,
    send_file,
    session,
    stream_with_context,
    url_for,
)
from werkzeug.security import generate_password_hash, check_password_hash

import database as db
import scheduler
from config import get_config, load_config
from media_handler import save_upload

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
runtime_log_handler = RotatingFileHandler(
    "runtime_debug.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
)
runtime_log_handler.setLevel(logging.INFO)
runtime_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
))
logging.getLogger().addHandler(runtime_log_handler)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------

app = Flask(__name__)


# ---------------------------------------------------------------------------
# 认证装饰器
# ---------------------------------------------------------------------------


def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        user = db.get_user(session["username"])
        if not user or user["role"] != "admin":
            abort(403)
        return f(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# 访问频率限制 & 验证码
# ---------------------------------------------------------------------------

RATE_LIMIT_STORE = {}

def check_rate_limit(req, action="auth", limit_seconds=4):
    ip = req.headers.get("X-Forwarded-For", req.remote_addr)
    if ip and ',' in ip:
        ip = ip.split(',')[0].strip()
    if not ip:
        ip = "unknown"

    now = time.time()
    key = f"{action}_{ip}"
    last_time = RATE_LIMIT_STORE.get(key, 0)

    if now - last_time < limit_seconds:
        return False

    RATE_LIMIT_STORE[key] = now
    return True

@app.route("/captcha")
def get_captcha():
    # 为了方便用户识别，只使用数字
    code = "".join(random.choices(string.digits, k=4))
    session["captcha"] = code
    image = ImageCaptcha(width=160, height=60)
    data = image.generate(code)
    return Response(data.getvalue(), mimetype="image/png")

# ---------------------------------------------------------------------------
# 页面路由
# ---------------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error=None, flashes=get_flashed_messages())

    if not check_rate_limit(request, action="login", limit_seconds=4):
        time.sleep(1)
        return render_template("login.html", error="操作太频繁，请 4 秒后再试", flashes=get_flashed_messages())

    captcha_input = request.form.get("captcha", "").strip()
    if not captcha_input or captcha_input.lower() != session.get("captcha", "").lower():
        time.sleep(1)
        return render_template("login.html", error="验证码错误", flashes=get_flashed_messages())

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = db.get_user(username)

    if not user or not check_password_hash(user["password"], password):
        time.sleep(1)  # 防暴力破解延迟
        return render_template("login.html", error="用户名或密码错误", flashes=get_flashed_messages())

    session["username"] = username
    session["role"] = user["role"]

    if user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("customer_dashboard"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", error=None)

    if not check_rate_limit(request, action="register", limit_seconds=4):
        time.sleep(1)
        return render_template("register.html", error="操作太频繁，请 4 秒后再试")

    captcha_input = request.form.get("captcha", "").strip()
    if not captcha_input or captcha_input.lower() != session.get("captcha", "").lower():
        time.sleep(1)
        return render_template("register.html", error="验证码错误")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not username or not password:
        return render_template("register.html", error="用户名和密码不能为空")
    if password != confirm_password:
        return render_template("register.html", error="两次输入的密码不一致")
    if len(password) < 6:
        return render_template("register.html", error="密码至少需要 6 位")

    if db.get_user(username):
        return render_template("register.html", error="用户名已存在")

    try:
        db.create_user(username, generate_password_hash(password), "customer", 0)
    except sqlite3.IntegrityError:
        return render_template("register.html", error="用户名已存在")

    flash("注册成功，请登录。你后面可以再由管理员手动加分。")
    return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    role = session.get("role", "customer")
    if role == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("customer_dashboard"))


@app.route("/customer")
@login_required
def customer_dashboard():
    cfg = get_config()
    username = session["username"]
    user = db.get_user(username)
    if not user:
        return redirect(url_for("login"))

    tasks = db.get_tasks_by_customer(username)
    for t in tasks:
        scheduler.enrich_task(t)
        t["result_url"] = _task_result_url(t)

    transactions = db.get_transactions(user=username, limit=100)

    return render_template(
        "customer_dashboard.html",
        user=user,
        cfg=cfg,
        tasks=tasks,
        transactions=transactions,
    )


@app.route("/admin")
@admin_required
def admin_dashboard():
    cfg = get_config()
    csrf_token = _get_csrf_token()
    users = db.get_all_users()
    tasks = db.get_all_tasks(limit=200)
    for t in tasks:
        scheduler.enrich_task(t)
        t["result_url"] = _task_result_url(t)

    user_filter = request.args.get("user", "")
    if user_filter:
        transactions = db.get_transactions(user=user_filter, limit=200)
    else:
        transactions = db.get_transactions(limit=200)

    # Provider 状态和队列 Dump
    with scheduler.lock:
        prov_states = []
        for pname, pstate in scheduler.provider_states.items():
            prov_states.append({
                "username": pname,
                "cookie_path": pstate.cookie_path,
                "running_sd2": pstate.running_sd2,
                "running_sd2_fast": pstate.running_sd2_fast,
                "is_cooled": pstate.is_cooled_down(cfg.provider.cooldown_seconds),
            })
        queues_dump = scheduler.get_queues_dump()

    return render_template(
        "admin_dashboard.html",
        cfg=cfg,
        csrf_token=csrf_token,
        users=users,
        tasks=tasks,
        transactions=transactions,
        user_filter=user_filter,
        provider_states=prov_states,
        queues_dump=queues_dump,
    )


# ---------------------------------------------------------------------------
# Admin 操作路由
# ---------------------------------------------------------------------------


@app.route("/admin/create_user", methods=["POST"])
@admin_required
def create_user():
    _require_csrf_token()
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "customer")
    credits = float(request.form.get("credits", 0))
    note = request.form.get("note", "")

    if not username or not password:
        flash("用户名和密码不能为空")
        return redirect(url_for("admin_dashboard"))

    existing = db.get_user(username)
    if existing:
        flash(f"用户 {username} 已存在")
        return redirect(url_for("admin_dashboard"))

    db.create_user(username, generate_password_hash(password), role, credits)

    if credits != 0:
        db.add_transaction(username, "admin_set", credits, note=note or "初始积分")

    flash(f"用户 {username} 创建成功")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete_user", methods=["POST"])
@admin_required
def delete_user():
    _require_csrf_token()
    username = request.form.get("username", "").strip()

    if not username:
        flash("用户名不能为空")
        return redirect(url_for("admin_dashboard"))

    if username == "admin":
        flash("不能删除 admin 账号")
        return redirect(url_for("admin_dashboard"))

    user = db.get_user(username)
    if not user:
        flash(f"用户 {username} 不存在")
        return redirect(url_for("admin_dashboard"))

    db.delete_user(username)

    # 刷新 provider 内存状态，如果被删的是 provider
    if user["role"] == "provider":
        with scheduler.lock:
            if username in scheduler.provider_states:
                del scheduler.provider_states[username]

    flash(f"用户 {username} 已被删除")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/adjust_credits", methods=["POST"])
@admin_required
def adjust_credits():
    _require_csrf_token()
    username = request.form.get("username", "").strip()
    mode = request.form.get("mode", "delta")
    credits = float(request.form.get("credits", 0))
    note = request.form.get("note", "")

    user = db.get_user(username)
    if not user:
        flash(f"用户 {username} 不存在")
        return redirect(url_for("admin_dashboard"))

    if mode == "set":
        old = user["credits"]
        db.set_credits(username, credits)
        db.add_transaction(username, "admin_set", credits - old, note=note or f"设置积分为 {credits}")
    else:
        db.adjust_credits(username, credits)
        db.add_transaction(username, "admin_adjust", credits, note=note or "积分调整")

    flash(f"积分已更新")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/api/provider/oauth_start", methods=["POST"])
@admin_required
def oauth_start():
    _require_csrf_token()
    username = request.form.get("username", "").strip()
    if not username:
        return jsonify(ok=False, error="缺少 provider 用户名")

    user = db.get_user(username)
    if not user or user["role"] != "provider":
        return jsonify(ok=False, error="非 provider 用户")

    cfg = get_config()
    save_dir = os.path.join(cfg.data_dir, "provider_cookies", username)
    os.makedirs(save_dir, exist_ok=True)

    # 强制让 CLI 将 HOME 认作我们的独立目录，防止覆盖 Linux Keyring
    env = os.environ.copy()
    env["DREAMINA_HOME"] = save_dir
    env["HOME"] = save_dir

    cmd = [cfg.dreamina.cli_command, "login", "--headless"]
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=10)
    except Exception as e:
        return jsonify(ok=False, error=f"执行命令失败: {e}")

    # 如果已有本地登录态复用，它会打印 "已复用当前本地 OAuth 登录态"
    if "已复用" in result.stdout:
        # 更新数据库并重载
        db.update_user_cookie_path(username, save_dir)
        with scheduler.lock:
            scheduler.reload_provider(username)
        return jsonify(ok=True, message="本地已存在有效登录态，直接复用成功", already_logged_in=True)

    # 提取链接和 device code
    lines = result.stdout.strip().split("\n")
    uri = ""
    device_code = ""
    for line in lines:
        if line.startswith("verification_uri:"):
            uri = line.split(":", 1)[1].strip()
        elif line.startswith("device_code:"):
            device_code = line.split(":", 1)[1].strip()

    if not uri or not device_code:
        return jsonify(ok=False, error=f"无法解析 CLI 输出:\\n{result.stdout}\\n{result.stderr}")

    return jsonify(ok=True, uri=uri, device_code=device_code, save_dir=save_dir)


@app.route("/admin/api/provider/oauth_check", methods=["POST"])
@admin_required
def oauth_check():
    _require_csrf_token()
    username = request.form.get("username", "").strip()
    device_code = request.form.get("device_code", "").strip()

    if not username or not device_code:
        return jsonify(ok=False, error="缺少参数")

    cfg = get_config()
    save_dir = os.path.join(cfg.data_dir, "provider_cookies", username)
    env = os.environ.copy()
    env["DREAMINA_HOME"] = save_dir
    env["HOME"] = save_dir

    # 尝试轮询获取 token
    cmd = [cfg.dreamina.cli_command, "login", "checklogin", f"--device_code={device_code}", "--poll=5"]
    try:
        result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=15)
    except Exception as e:
        return jsonify(ok=False, error=f"检查失败: {e}")

    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip()
        return jsonify(ok=False, error=f"授权未完成或失败: {err}")

    # 登录成功，更新数据库
    db.update_user_cookie_path(username, save_dir)
    with scheduler.lock:
        scheduler.reload_provider(username)

    return jsonify(ok=True, message="授权成功并已重载 Provider！")


# ---------------------------------------------------------------------------
# API 路由（JSON）
# ---------------------------------------------------------------------------


@app.route("/api/submit_task", methods=["POST"])
@login_required
def api_submit_task():
    data = request.get_json(force=True)
    username = session["username"]

    prompt = data.get("prompt", "").strip()
    if not prompt:
        return jsonify(ok=False, error="提示词不能为空")

    duration = int(data.get("duration", 5))
    ratio = data.get("ratio", "16:9")
    model_version = data.get("model_version", "sd2")
    media_ids = data.get("media_ids", [])
    text_only = bool(data.get("text_only", False))
    generation_mode = "text2video" if text_only else "multimodal"
    if text_only:
        media_ids = []
    cfg = get_config()
    model_cfg = cfg.video.models.get(model_version)
    queue = data.get("queue", False)
    if model_cfg and model_cfg.get("is_vip"):
        queue = False

    # 时长验证 (修复负数时长刷分漏洞)
    if duration < cfg.video.duration_min or duration > cfg.video.duration_max:
        return jsonify(ok=False, error=f"视频时长必须在 {cfg.video.duration_min} 到 {cfg.video.duration_max} 秒之间")

    # 比例验证
    if ratio not in cfg.video.supported_ratios:
        return jsonify(ok=False, error=f"不支持的视频比例: {ratio}")

    if not isinstance(media_ids, list):
        return jsonify(ok=False, error="素材参数格式错误")
    media_counts = {"image": 0, "audio": 0}
    for media_id in media_ids:
        media = db.get_media(str(media_id))
        if not media or media["customer"] != username:
            return jsonify(ok=False, error="素材不存在或无权使用")
        if media["kind"] not in media_counts:
            return jsonify(ok=False, error="不支持的素材类型")
        media_counts[media["kind"]] += 1
    if media_counts["image"] > cfg.video.max_reference_images:
        return jsonify(ok=False, error=f"参考图片最多 {cfg.video.max_reference_images} 张")
    if media_counts["audio"] > cfg.video.max_reference_audios:
        return jsonify(ok=False, error=f"参考音频最多 {cfg.video.max_reference_audios} 个")
    if generation_mode != "text2video" and media_counts["image"] < 1:
        return jsonify(ok=False, error="请至少上传 1 张参考图片")

    # 如果勾选了排队，映射模型为 lite 版
    if queue:
        if model_version == "sd2":
            model_version = "sd2_lite"
        elif model_version == "sd2_fast":
            model_version = "sd2_fast_lite"

    with scheduler.lock:
        success, message, task = scheduler.submit_task(
            customer=username,
            prompt=prompt,
            model_version=model_version,
            duration=duration,
            ratio=ratio,
            media_ids=media_ids,
            is_queued=queue,
            generation_mode=generation_mode,
        )

    if not success:
        return jsonify(ok=False, error=message)

    user = db.get_user(username)
    scheduler.enrich_task(task)

    return jsonify(
        ok=True,
        task_id=task["task_id"],
        credits=task["credits"],
        remaining_credits=user["credits"],
        task=task,
    )


@app.route("/api/task_status/<task_id>")
@login_required
def api_task_status(task_id):
    task = db.get_task(task_id)
    if not task:
        return jsonify(ok=False, error="任务不存在")
    if task["customer"] != session["username"]:
        return jsonify(ok=False, error="无权查看")

    scheduler.enrich_task(task)
    task["result_url"] = _task_result_url(task)
    user = db.get_user(session["username"])

    return jsonify(ok=True, task=task, user_credits=user["credits"])


@app.route("/api/task_reuse/<task_id>")
@login_required
def api_task_reuse(task_id):
    task = db.get_task(task_id)
    if not task:
        return jsonify(ok=False, error="任务不存在")
    if task["customer"] != session["username"]:
        return jsonify(ok=False, error="无权操作")

    # 获取 media
    media = db.get_task_media(task_id)
    # 检查哪些 media 还存在
    valid_media = []
    missing_count = 0
    for m in media:
        if os.path.exists(m["file_path"]):
            valid_media.append(m)
        else:
            missing_count += 1

    task["media"] = valid_media
    task["missing_media_count"] = missing_count

    # 映射回原始 model_version（如果是 lite，前端需要 base model + queue=true）
    if task["model_version"] in ("sd2_lite", "sd2_fast_lite"):
        task["is_queued"] = True
        if task["model_version"] == "sd2_lite":
            task["model_version"] = "sd2"
        else:
            task["model_version"] = "sd2_fast"
    else:
        task["is_queued"] = bool(task.get("is_queued"))

    return jsonify(ok=True, task=task)


@app.route("/api/cancel_task/<task_id>", methods=["POST"])
@login_required
def api_cancel_task(task_id):
    username = session["username"]

    with scheduler.lock:
        success, message, task = scheduler.cancel_task(username, task_id)

    if not success:
        return jsonify(ok=False, error=message)

    scheduler.enrich_task(task)
    user = db.get_user(username)
    return jsonify(ok=True, task=task, user_credits=user["credits"])


@app.route("/api/delete_task/<task_id>", methods=["POST"])
@login_required
def api_delete_task(task_id):
    username = session["username"]

    with scheduler.lock:
        success, message = scheduler.delete_task(username, task_id)

    if not success:
        return jsonify(ok=False, error=message)

    return jsonify(ok=True)


@app.route("/api/media/upload", methods=["POST"])
@login_required
def api_media_upload():
    username = session["username"]
    kind = request.form.get("kind", "image")
    file = request.files.get("file")

    if not file:
        return jsonify(ok=False, error="没有文件")

    cfg = get_config()
    try:
        media_id, file_path, thumb_path, original_name = save_upload(
            file, username, kind, cfg.data_dir
        )
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc))

    # 构建公网 URL
    url = f"/api/media/{media_id}"

    db.create_media(media_id, username, kind, original_name,
                    file_path, thumb_path or "", url)

    return jsonify(ok=True, media_id=media_id, url=url)


@app.route("/api/media/<media_id>")
@login_required
def api_media_serve(media_id):
    """提供素材文件（含缩略图参数）。"""
    media = db.get_media(media_id)
    if not media:
        abort(404)
    if media["customer"] != session["username"] and session.get("role") != "admin":
        abort(403)

    thumb = request.args.get("thumb")
    if thumb and media.get("thumb_path") and os.path.exists(media["thumb_path"]):
        response = send_file(media["thumb_path"], mimetype="image/jpeg")
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    if not os.path.exists(media["file_path"]):
        abort(404)
    response = send_file(media["file_path"])
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.route("/api/download/<task_id>")
@login_required
def download_result(task_id):
    """下载视频结果 — 重定向到即梦链接。"""
    task = db.get_task(task_id)
    if not task:
        abort(404)
    if task["customer"] != session["username"] and session.get("role") != "admin":
        abort(403)
    return _proxy_result_video(task, as_attachment=True)


@app.route("/api/view/<task_id>")
@login_required
def view_result(task_id):
    """播放视频结果 — 重定向到即梦链接。"""
    task = db.get_task(task_id)
    if not task:
        abort(404)
    if task["customer"] != session["username"] and session.get("role") != "admin":
        abort(403)
    return _proxy_result_video(task, as_attachment=False)


def _proxy_result_video(task: dict, as_attachment: bool):
    result_url = _refresh_task_result_url(task) or _task_result_url(task)
    if not result_url:
        abort(404)

    try:
        upstream = _open_result_url(result_url)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            result_url = _refresh_task_result_url(task)
            if not result_url:
                abort(404)
            try:
                upstream = _open_result_url(result_url)
            except urllib.error.HTTPError as retry_exc:
                logger.warning("video_proxy_fetch_failed task=%s status=%s", task.get("task_id"), retry_exc.code)
                abort(502)
        else:
            logger.warning("video_proxy_fetch_failed task=%s status=%s", task.get("task_id"), exc.code)
            abort(502)
    except urllib.error.URLError as exc:
        logger.warning("video_proxy_fetch_failed task=%s error=%s", task.get("task_id"), exc)
        abort(502)

    headers = {}
    for key in ("Content-Length", "Content-Range", "Accept-Ranges"):
        value = upstream.headers.get(key)
        if value:
            headers[key] = value
    filename = f"{task.get('task_id', 'dreamina-video')}.mp4"
    disposition = "attachment" if as_attachment else "inline"
    headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    headers["Cache-Control"] = "private, max-age=0, no-store"
    mimetype = upstream.headers.get("Content-Type") or "video/mp4"

    def generate():
        try:
            while True:
                chunk = upstream.read(1024 * 256)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(generate()),
        status=getattr(upstream, "status", 200),
        headers=headers,
        mimetype=mimetype,
        direct_passthrough=True,
    )


def _open_result_url(result_url: str):
    headers = {"User-Agent": "Mozilla/5.0"}
    range_header = request.headers.get("Range")
    if range_header:
        headers["Range"] = range_header
    req = urllib.request.Request(result_url, headers=headers)
    return urllib.request.urlopen(req, timeout=30)


def _refresh_task_result_url(task: dict) -> str:
    dreamina_task_id = task.get("dreamina_task_id")
    provider_name = task.get("provider")
    if not dreamina_task_id or not provider_name:
        return ""

    provider = db.get_user(provider_name)
    if not provider or not provider.get("cookie_path"):
        return ""

    try:
        status = scheduler.cli.query(dreamina_task_id, provider["cookie_path"])
    except Exception as exc:
        logger.warning("video_url_refresh_failed task=%s error=%s", task.get("task_id"), exc)
        return ""

    if not status.video_url:
        return ""

    db.update_task(
        task["task_id"],
        result_url=status.video_url,
        progress=status.progress or task.get("progress"),
        progress_meta=status.progress_meta or task.get("progress_meta"),
    )
    task["result_url"] = status.video_url
    logger.info("video_url_refreshed task=%s provider=%s", task.get("task_id"), provider_name)
    return status.video_url


def _task_result_url(task: dict) -> str:
    """Return the stored video URL, including legacy rows where it is only in progress_meta."""
    result_url = task.get("result_url") or ""
    if result_url:
        return result_url

    raw_meta = task.get("progress_meta") or ""
    if not raw_meta:
        return ""

    try:
        meta = json.loads(raw_meta)
    except (TypeError, json.JSONDecodeError):
        return ""

    result_json = meta.get("result_json")
    if isinstance(result_json, str):
        try:
            result_json = json.loads(result_json)
        except json.JSONDecodeError:
            result_json = None

    if not isinstance(result_json, dict):
        return ""

    videos = result_json.get("videos")
    if not isinstance(videos, list):
        return ""

    for video in videos:
        if not isinstance(video, dict):
            continue
        for key in ("video_url", "url", "download_url", "play_url"):
            value = video.get(key)
            if isinstance(value, str) and value.startswith(("http://", "https://")):
                return value
    return ""


# ---------------------------------------------------------------------------
# CSRF 简单实现
# ---------------------------------------------------------------------------


def _get_csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(16)
    return session["csrf_token"]


def _require_csrf_token():
    expected = session.get("csrf_token")
    actual = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not actual or not secrets.compare_digest(expected, actual):
        abort(400)


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------


def create_app():
    """工厂函数：加载配置、初始化数据库、启动调度器。"""
    cfg = load_config()

    # 动态覆盖默认的弱 secret_key
    if cfg.server.secret_key == "change-me-in-production-to-a-random-string":
        app.secret_key = secrets.token_hex(32)
        logger.warning("检测到使用了默认的安全密钥，已在内存中随机生成临时密钥以保证安全。")
    else:
        app.secret_key = cfg.server.secret_key

    # 初始化数据库
    db.init_db(cfg.data_dir, cfg.database.filename)

    # 确保 admin 账户存在
    admin = db.get_user("admin")
    if not admin:
        random_pwd = secrets.token_urlsafe(12)
        db.create_user("admin", generate_password_hash(random_pwd), "admin", 0)
        logger.warning("="*60)
        logger.warning("检测到系统首次启动，已创建默认的 admin 账号！")
        logger.warning(f"  初始密码为: {random_pwd}")
        logger.warning("请务必保存并及时登录修改密码！")
        logger.warning("="*60)

    # 初始化并启动调度器
    scheduler.init_scheduler()
    scheduler.start()

    return app


# ---------------------------------------------------------------------------
# 直接运行
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    application = create_app()
    cfg = get_config()
    application.run(
        host=cfg.server.host,
        port=cfg.server.port,
        debug=cfg.server.debug,
        use_reloader=False,  # 避免重启时调度器线程冲突
    )
