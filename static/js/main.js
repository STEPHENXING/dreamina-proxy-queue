const referenceMedia = [];
let nextLocalId = 1;
let toastTimer = null;

function showToast(message, tone = "info", timeout = 3500) {
  let toast = document.querySelector("#app-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "app-toast";
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    document.body.appendChild(toast);
  }
  toast.className = `app-toast app-toast-${tone}`;
  toast.textContent = message;
  toast.hidden = false;
  window.clearTimeout(toastTimer);
  if (timeout > 0) {
    toastTimer = window.setTimeout(() => {
      toast.hidden = true;
    }, timeout);
  }
}

function isVipModelSelected() {
  const model = document.querySelector("#model");
  const selected = model?.selectedOptions?.[0];
  return selected?.dataset.isVip === "1" || String(model?.value || "").endsWith("_vip");
}

function isTextOnlySelected() {
  return document.querySelector("#text-only-check")?.checked || false;
}

function syncVipQueueOption() {
  const queueCheck = document.querySelector("#queue-check");
  const queueLabel = queueCheck?.closest("label");
  if (!queueCheck || !queueLabel) return;
  const isVip = isVipModelSelected();
  if (isVip) queueCheck.checked = false;
  queueCheck.disabled = isVip;
  queueLabel.hidden = isVip;
}

function syncTextOnlyOption() {
  const referencePanel = document.querySelector("#reference-panel");
  if (referencePanel) referencePanel.hidden = isTextOnlySelected();
}

function updateEstimate() {
  syncVipQueueOption();
  syncTextOnlyOption();
  const model = document.querySelector("#model");
  const duration = document.querySelector("#duration");
  const estimate = document.querySelector("#credit-estimate");
  if (!model || !duration || !estimate) return;
  const cps = Number(model.selectedOptions[0].dataset.cps || 0);
  const dur = Number(duration.value || 0);
  const queued = !isVipModelSelected() && document.querySelector("#queue-check")?.checked;
  const total = queued ? Math.floor(cps * 1.5 * dur) : cps * dur;
  estimate.textContent = String(total);
}

function updateCurrentCredits(credits) {
  if (!Number.isFinite(Number(credits))) return;
  const currentCredits = document.querySelector("#current-credits");
  const currentCreditCny = document.querySelector("#current-credit-cny");
  if (currentCredits) currentCredits.textContent = String(credits);
  if (currentCreditCny) {
    const rate = Number(currentCreditCny.dataset.rate || 0);
    currentCreditCny.textContent = (Number(credits) * rate).toFixed(2);
  }
}

function maxForKind(kind) {
  const picker = document.querySelector("#reference-picker");
  const key = kind === "image" ? "maxImages" : "maxAudios";
  return Number(picker?.dataset[key] || 0);
}

function countKind(kind) {
  return referenceMedia.filter((item) => item.kind === kind).length;
}

function renderMediaList() {
  const list = document.querySelector("#media-list");
  const hidden = document.querySelector("#media-ids");
  if (!list || !hidden) return;
  list.innerHTML = "";
  referenceMedia.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "media-item";
    row.dataset.key = item.key;

    const preview = item.kind === "image"
      ? `<img class="media-thumb" src="${escapeAttr(item.previewUrl || item.url)}" alt="${escapeAttr(item.name || "reference image")}">`
      : `<div class="media-thumb media-audio" aria-label="audio reference">Audio</div>`;
    const state = item.media_id ? "已就绪" : "待提交时上传";
    row.innerHTML = `
      ${preview}
      <div class="media-meta">
        <span class="media-name">${index + 1}. ${escapeHtml(item.name || item.media_id || item.key)}</span>
        <span>${escapeHtml(item.kind)} · ${state}</span>
      </div>
      <div class="media-actions">
        <button type="button" class="icon-button" data-action="up" title="上移">↑</button>
        <button type="button" class="icon-button" data-action="down" title="下移">↓</button>
        <button type="button" class="icon-button danger-button" data-action="remove" title="移除">×</button>
      </div>
    `;
    list.appendChild(row);
  });
  hidden.value = JSON.stringify(referenceMedia.filter((item) => item.media_id).map((item) => item.media_id));
}

function revokeLocalPreviews(items = referenceMedia) {
  items.forEach((item) => {
    if (item.previewUrl?.startsWith("blob:")) URL.revokeObjectURL(item.previewUrl);
  });
}

function setReferenceMedia(items) {
  revokeLocalPreviews();
  referenceMedia.splice(0, referenceMedia.length, ...items);
  renderMediaList();
}

function addLocalFiles(kind, files) {
  const msg = document.querySelector("#task-message");
  const max = maxForKind(kind);
  const available = Math.max(0, max - countKind(kind));
  const accepted = Array.from(files).slice(0, available);
  accepted.forEach((file) => {
    referenceMedia.push({
      key: `local_${nextLocalId++}`,
      kind,
      file,
      name: file.name,
      previewUrl: kind === "image" ? URL.createObjectURL(file) : "",
      media_id: null,
      url: "",
    });
  });
  renderMediaList();
  if (msg) {
    const skipped = files.length - accepted.length;
    msg.textContent = skipped > 0 ? `已加入 ${accepted.length} 个资源，超过上限的 ${skipped} 个已忽略` : `已加入 ${accepted.length} 个资源，提交任务时会自动上传`;
  }
}

async function uploadPendingMedia() {
  const msg = document.querySelector("#task-message");
  for (let index = 0; index < referenceMedia.length; index += 1) {
    const item = referenceMedia[index];
    if (item.media_id) continue;
    if (!item.file) throw new Error(`${item.name || "参考资源"} 缺少本地文件`);
    if (msg) msg.textContent = `正在上传参考资源 ${index + 1}/${referenceMedia.length}：${item.name || item.key}`;
    const data = new FormData();
    data.append("kind", item.kind);
    data.append("file", item.file, item.name || item.file.name);
    const response = await fetch("/api/media/upload", { method: "POST", body: data });
    const body = await response.json();
    if (!body.ok) throw new Error(body.error || "参考资源上传失败");
    item.media_id = body.media_id;
    item.url = body.url;
    item.previewUrl = body.url + "?thumb=1";
    item.file = null;
    renderMediaList();
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value).replaceAll("`", "&#096;");
}

function parseProgressMeta(row) {
  const raw = row?.dataset.progressMeta;
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch (_error) {
    return {};
  }
}

function formatSeconds(seconds) {
  const safe = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(safe / 3600);
  const minutes = Math.floor((safe % 3600) / 60);
  const rest = safe % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`;
  }
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

function formatDateTime(value) {
  if (!value) return "";
  const raw = String(value).trim();
  const normalized = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(raw)
    ? `${raw.replace(" ", "T")}Z`
    : raw;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function formatExistingTimes() {
  document.querySelectorAll("time[datetime]").forEach((node) => {
    node.textContent = formatDateTime(node.getAttribute("datetime"));
  });
}

function progressSummary(row) {
  const status = row.dataset.status || "";
  const base = row.dataset.progressText || "";
  const meta = parseProgressMeta(row);
  const parts = [];
  if (base) parts.push(base);
  if (meta.queue_status) parts.push(`队列：${meta.queue_status}`);
  if (meta.queue_idx !== undefined) parts.push(`排位：${meta.queue_idx}`);
  if (meta.queue_length !== undefined) parts.push(`队列长度：${meta.queue_length}`);
  if (meta.credit_count !== undefined) parts.push(`即梦积分：${meta.credit_count}`);

  const eta = meta.remaining_seconds ?? meta.countdown_seconds ?? meta.eta_seconds;
  if (eta !== undefined) {
    const updatedAt = Number(row.dataset.progressMetaUpdatedAt || Date.now());
    const elapsed = Math.floor((Date.now() - updatedAt) / 1000);
    parts.push(`即梦倒计时：${formatSeconds(Number(eta) - elapsed)}`);
  }

  if (!["completed", "failed"].includes(status)) {
    const createdAt = Date.parse(row.dataset.createdAt || "");
    const timeout = Number(document.querySelector("#task-table-body")?.dataset.timeoutSeconds || 0);
    if (createdAt && timeout > 0) {
      const remaining = timeout - Math.floor((Date.now() - createdAt) / 1000);
      parts.push(`平台兜底超时：${formatSeconds(remaining)}`);
    }
  }
  return parts.join(" · ");
}

function refreshProgressCells() {
  document.querySelectorAll("#task-table-body tr").forEach((row) => {
    const cell = row.querySelector(".progress-cell");
    if (cell) cell.textContent = progressSummary(row);
  });
}

function displayStateHtml(displayState, fallbackStatus = "pending", isQueued = false) {
  const state = displayState || {
    label: fallbackStatus,
    detail: "",
    tone: "waiting",
  };
  const queueBadge = isQueued ? `<span class="badge badge-queue">排队模式</span>` : "";
  return `
    <span class="status status-${escapeAttr(state.tone || "waiting")}">${escapeHtml(state.label || fallbackStatus)}</span>
    <span class="status-detail">${escapeHtml(state.detail || "")}</span>
    ${queueBadge}
  `;
}

function resultCellHtml(task) {
  if (task.status !== "completed") return "";
  const taskId = encodeURIComponent(task.task_id);
  return `
    <a class="download-link link-button" href="/api/download/${taskId}">⬇️ 下载</a>
    <a class="link-button" href="/api/view/${taskId}" target="_blank" rel="noopener">▶️ 播放</a>
  `;
}

function initPromptToggle(scope = document) {
  scope.querySelectorAll(".task-card-prompt").forEach((prompt) => {
    const button = prompt.nextElementSibling?.matches?.(".prompt-toggle")
      ? prompt.nextElementSibling
      : null;
    if (!button) return;
    prompt.classList.remove("is-expanded");
    button.textContent = "展开全部";
    button.setAttribute("aria-expanded", "false");
    button.hidden = prompt.scrollHeight <= prompt.clientHeight + 2;
  });
}
function taskFinishedAt(task) {
  return task.completed_at || task.failed_at || "";
}

function canCancelTask(status) {
  return status === "queued";
}

function timeCellHtml(task) {
  const createdAt = task.created_at || "";
  const submittedAt = task.submitted_at || "";
  const finishedAt = taskFinishedAt(task);
  const submitted = submittedAt
    ? `<time datetime="${escapeAttr(submittedAt)}">${escapeHtml(formatDateTime(submittedAt))}</time>`
    : '<span class="muted">等待提交</span>';
  const returned = finishedAt
    ? `<time datetime="${escapeAttr(finishedAt)}">${escapeHtml(formatDateTime(finishedAt))}</time>`
    : '<span class="muted">处理中</span>';
  return `
    <span>发起：<time datetime="${escapeAttr(createdAt)}">${escapeHtml(formatDateTime(createdAt))}</time></span>
    <span>提交即梦：${submitted}</span>
    <span>返回：${returned}</span>
  `;
}

function updateTaskRow(row, task) {
  if (!row || !task) return;
  const displayState = task.display_state || {};
  row.dataset.status = task.status || row.dataset.status || "pending";
  row.dataset.displayCode = displayState.code || row.dataset.displayCode || "";
  row.dataset.createdAt = task.created_at || row.dataset.createdAt || "";
  row.dataset.submittedAt = task.submitted_at || row.dataset.submittedAt || "";
  row.dataset.finishedAt = taskFinishedAt(task);
  row.dataset.progressText = task.progress || "";
  row.dataset.progressMeta = task.progress_meta || "";
  row.dataset.progressMetaUpdatedAt = String(Date.now());
  const isQueued = row.dataset.isQueued === "1" || task.is_queued;

  const stateCell = row.querySelector(".state-cell");
  if (stateCell) stateCell.innerHTML = displayStateHtml(displayState, row.dataset.status, isQueued);

  const resultCell = row.querySelector(".result-cell");
  if (resultCell) resultCell.innerHTML = resultCellHtml(task);

  const timeCell = row.querySelector(".time-cell");
  if (timeCell) timeCell.innerHTML = timeCellHtml(task);

  const errorCell = row.querySelector(".error-cell");
  if (errorCell) errorCell.textContent = task.error || "";

  const cancelBtn = row.querySelector('button[data-action="cancel-task"]');
  if (!canCancelTask(task.status) && cancelBtn) {
    if (cancelBtn) cancelBtn.remove();
  } else if (canCancelTask(task.status) && !cancelBtn) {
    const actions = row.querySelector('.task-card-actions');
    if (actions) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'danger-button';
      btn.dataset.action = 'cancel-task';
      btn.dataset.taskId = row.dataset.taskId;
      btn.textContent = '❌ 取消并退款';
      actions.insertBefore(btn, actions.querySelector('.result-cell'));
    }
  }

  // Show delete button when task reaches terminal state
  if (["completed", "failed", "cancelled", "rejected"].includes(task.status) && !row.querySelector('button[data-action="delete-task"]')) {
    const actions = row.querySelector('.task-card-actions');
    if (actions) {
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'secondary-button';
      del.dataset.action = 'delete-task';
      del.dataset.taskId = row.dataset.taskId;
      del.textContent = '🗑️ 删除';
      actions.insertBefore(del, actions.querySelector('.result-cell'));
    }
  }

  refreshProgressCells();
}

function appendTaskRow(task) {
  const grid = document.querySelector("#task-grid");
  if (!grid || !task) return;
  const displayState = task.display_state || {
    code: "waiting_provider",
    label: task.status || "pending",
    detail: "任务已提交",
    tone: "waiting",
  };
  const isQueued = !!task.is_queued;
  const card = document.createElement("article");
  card.className = "task-card";
  card.dataset.taskId = task.task_id;
  card.dataset.createdAt = task.created_at || new Date().toISOString();
  card.dataset.submittedAt = task.submitted_at || "";
  card.dataset.finishedAt = taskFinishedAt(task);
  card.dataset.status = task.status || "pending";
  card.dataset.displayCode = displayState.code || "";
  card.dataset.progressText = task.progress || "";
  card.dataset.progressMeta = task.progress_meta || "";
  card.dataset.progressMetaUpdatedAt = String(Date.now());
  card.dataset.isQueued = isQueued ? "1" : "0";

  let mediaHtml = "";
  if (task.media && task.media.length > 0) {
    const mediaItems = task.media.map(m => {
      if (m.kind === 'image') return `<img src="${escapeAttr(m.url)}?thumb=1" alt="参考图" title="${escapeAttr(m.original_name)}" loading="lazy">`;
      return `<div class="audio-thumb" title="${escapeAttr(m.original_name)}">🎵 ${escapeHtml(m.original_name)}</div>`;
    }).join("");
    mediaHtml = `<div class="task-card-media">${mediaItems}</div>`;
  }

  const cancelBtn = canCancelTask(task.status)
    ? `<button type="button" class="danger-button" data-action="cancel-task" data-task-id="${escapeAttr(task.task_id)}">❌ 取消并退款</button>`
    : "";

  const deleteBtn = ["completed", "failed", "cancelled", "rejected"].includes(task.status)
    ? `<button type="button" class="secondary-button" data-action="delete-task" data-task-id="${escapeAttr(task.task_id)}">🗑️ 删除</button>`
    : "";

  card.innerHTML = `
    <div class="task-card-header">
      <div class="state-cell">${displayStateHtml(displayState, task.status || "pending", isQueued)}</div>
      <div class="meta-badges">
        <span class="badge">${escapeHtml(task.model_version)}</span>
        <span class="badge">${escapeHtml(task.credits)} 积分</span>
      </div>
    </div>
    <div class="task-card-body">
      <div class="task-card-prompt">${escapeHtml(task.prompt)}</div>
      <button type="button" class="prompt-toggle" data-action="toggle-prompt" aria-expanded="false" hidden>展开全部</button>
      ${mediaHtml}
    </div>
    <details class="task-card-details">
      <summary>详情信息 (进度 / 时间 / ID)</summary>
      <div class="details-content">
        <div class="time-cell">${timeCellHtml(task)}</div>
        <div class="progress-cell"></div>
        <div class="error-cell">${escapeHtml(task.error || "")}</div>
        <div class="task-id-cell muted">ID: ${escapeHtml(task.task_id)}</div>
      </div>
    </details>
    <div class="task-card-actions">
      <button type="button" class="primary-button" data-action="reuse-task" data-task-id="${escapeAttr(task.task_id)}">🔁 复用此任务</button>
      ${cancelBtn}
      ${deleteBtn}
      <div class="result-cell">${resultCellHtml(task)}</div>
    </div>
  `;
  grid.prepend(card);
  initPromptToggle(card);
  refreshProgressCells();
}

function fillTaskForm(task) {
  const form = document.querySelector("#task-form");
  if (!form || !task) return;
  const modelSelect = form.querySelector('[name="model_version"]');
  if (modelSelect) modelSelect.value = task.model_version;
  form.querySelector('[name="duration"]').value = String(task.duration);
  form.querySelector('[name="ratio"]').value = task.ratio;
  form.querySelector('[name="prompt"]').value = task.prompt || "";
  const queueCheck = document.querySelector("#queue-check");
  if (queueCheck) queueCheck.checked = !!task.is_queued;
  const textOnlyCheck = document.querySelector("#text-only-check");
  if (textOnlyCheck) textOnlyCheck.checked = task.generation_mode === "text2video";
  syncVipQueueOption();
  syncTextOnlyOption();
  setReferenceMedia((task.media || []).map((item) => ({
    key: `media_${item.media_id}`,
    media_id: item.media_id,
    kind: item.kind,
    url: item.url,
    previewUrl: item.url + "?thumb=1",
    name: item.name || item.original_name || item.media_id,
    file: null,
  })));
  updateEstimate();
}

function filenameFromDisposition(header, fallback) {
  const match = String(header || "").match(/filename\*?=(?:UTF-8''|")?([^";]+)/i);
  if (!match) return fallback;
  try {
    return decodeURIComponent(match[1].replaceAll('"', ""));
  } catch (_error) {
    return match[1].replaceAll('"', "") || fallback;
  }
}

async function downloadVideoFromLink(link) {
  const msg = document.querySelector("#task-message");
  if (link.dataset.downloading === "1") {
    showToast("视频正在准备下载，请稍等。", "info", 2200);
    return;
  }
  link.dataset.downloading = "1";
  link.classList.add("is-loading");
  if (msg) msg.textContent = "正在准备视频下载";
  showToast("已开始准备视频下载，文件较大时请稍等。", "info", 0);
  try {
    const response = await fetch(link.href, { credentials: "same-origin" });
    if (!response.ok) throw new Error(`下载失败：${response.status}`);
    const blob = await response.blob();
    const filename = filenameFromDisposition(
      response.headers.get("content-disposition"),
      link.href.split("/").pop() + ".mp4",
    );
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
    if (msg) msg.textContent = `视频下载已开始：${filename}`;
    showToast(`浏览器已开始下载：${filename}`, "success", 5000);
  } finally {
    link.dataset.downloading = "0";
    link.classList.remove("is-loading");
  }
}

let taskPollInFlight = false;

async function pollTaskStatuses() {
  if (taskPollInFlight) return;
  const rows = Array.from(document.querySelectorAll("#task-grid .task-card")).filter(
    (row) => row.dataset.taskId && !["completed", "failed"].includes(row.dataset.status || ""),
  );
  if (!rows.length) return;
  taskPollInFlight = true;
  try {
    await Promise.all(rows.map(async (row) => {
      const response = await fetch(`/api/task_status/${encodeURIComponent(row.dataset.taskId)}`);
      if (!response.ok) return;
      const body = await response.json();
      if (body.ok && body.task) {
        updateTaskRow(row, body.task);
        updateCurrentCredits(body.user_credits);
      }
    }));
  } catch (_error) {
    // 下一轮轮询会继续尝试；这里不打扰正在填写表单的用户。
  } finally {
    taskPollInFlight = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  formatExistingTimes();
  document.querySelectorAll("#task-grid .task-card").forEach((row) => {
    row.dataset.progressText = row.querySelector(".progress-cell")?.textContent.trim() || "";
    row.dataset.progressMetaUpdatedAt = String(Date.now());
  });
  initPromptToggle();
  refreshProgressCells();
  setInterval(refreshProgressCells, 1000);
  setTimeout(pollTaskStatuses, 800);
  setInterval(pollTaskStatuses, 5000);

  updateEstimate();
  document.querySelector("#model")?.addEventListener("change", updateEstimate);
  document.querySelector("#duration")?.addEventListener("change", updateEstimate);
  document.querySelector("#queue-check")?.addEventListener("change", updateEstimate);
  document.querySelector("#text-only-check")?.addEventListener("change", updateEstimate);

  document.querySelector("#reference-images")?.addEventListener("change", (event) => {
    addLocalFiles("image", event.currentTarget.files || []);
    event.currentTarget.value = "";
  });
  document.querySelector("#reference-audios")?.addEventListener("change", (event) => {
    addLocalFiles("audio", event.currentTarget.files || []);
    event.currentTarget.value = "";
  });

  document.querySelector("#clear-media")?.addEventListener("click", () => {
    setReferenceMedia([]);
    const msg = document.querySelector("#task-message");
    if (msg) msg.textContent = "已清空当前任务参考资源";
  });

  document.querySelector("#media-list")?.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const item = button.closest(".media-item");
    const key = item?.dataset.key;
    const index = referenceMedia.findIndex((media) => media.key === key);
    if (index < 0) return;
    const action = button.dataset.action;
    if (action === "remove") {
      revokeLocalPreviews([referenceMedia[index]]);
      referenceMedia.splice(index, 1);
    } else if (action === "up" && index > 0) {
      [referenceMedia[index - 1], referenceMedia[index]] = [referenceMedia[index], referenceMedia[index - 1]];
    } else if (action === "down" && index < referenceMedia.length - 1) {
      [referenceMedia[index + 1], referenceMedia[index]] = [referenceMedia[index], referenceMedia[index + 1]];
    }
    renderMediaList();
  });

  document.querySelector("#task-grid")?.addEventListener("click", async (event) => {
    const promptToggle = event.target.closest('button[data-action="toggle-prompt"]');
    if (promptToggle) {
      const prompt = promptToggle.previousElementSibling;
      if (!prompt?.classList?.contains("task-card-prompt")) return;
      const expanded = prompt.classList.toggle("is-expanded");
      promptToggle.textContent = expanded ? "收起" : "展开全部";
      promptToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      return;
    }
    const downloadLink = event.target.closest("a.download-link");
    if (downloadLink) {
      if (downloadLink.origin !== window.location.origin) return;
      event.preventDefault();
      const msg = document.querySelector("#task-message");
      try {
        await downloadVideoFromLink(downloadLink);
      } catch (error) {
        const message = error.message || "下载失败";
        if (msg) msg.textContent = message;
        showToast(message, "danger", 5000);
      }
      return;
    }

    const cancelBtn = event.target.closest('button[data-action="cancel-task"]');
    if (cancelBtn) {
      if (!confirm("确定要取消此排队任务并退还积分吗？")) return;
      const msg = document.querySelector("#task-message");
      cancelBtn.disabled = true;
      try {
        const response = await fetch(`/api/cancel_task/${encodeURIComponent(cancelBtn.dataset.taskId)}`, { method: "POST" });
        const body = await response.json();
        if (!body.ok) {
          if (msg) msg.textContent = body.error;
          cancelBtn.disabled = false;
          return;
        }
        if (msg) msg.textContent = "取消成功，积分已退还";
        const card = cancelBtn.closest('.task-card');
        if (card) updateTaskRow(card, body.task);
        if (body.user_credits !== undefined) updateCurrentCredits(body.user_credits);
      } catch (error) {
        if (msg) msg.textContent = "取消请求失败";
        cancelBtn.disabled = false;
      }
      return;
    }

    const deleteBtn = event.target.closest('button[data-action="delete-task"]');
    if (deleteBtn) {
      const card = deleteBtn.closest('.task-card');
      const isCompleted = card && card.dataset.status === 'completed';
      if (isCompleted && !confirm("该任务已成功生成视频，确定要删除吗？")) return;
      const msg = document.querySelector("#task-message");
      deleteBtn.disabled = true;
      try {
        const response = await fetch(`/api/delete_task/${encodeURIComponent(deleteBtn.dataset.taskId)}`, { method: "POST" });
        const body = await response.json();
        if (!body.ok) {
          if (msg) msg.textContent = body.error;
          deleteBtn.disabled = false;
          return;
        }
        if (msg) msg.textContent = "任务已删除";
        const card = deleteBtn.closest('.task-card');
        if (card) card.remove();
      } catch (error) {
        if (msg) msg.textContent = "删除请求失败";
        deleteBtn.disabled = false;
      }
      return;
    }

    const button = event.target.closest('button[data-action="reuse-task"]');
    if (!button) return;
    const msg = document.querySelector("#task-message");
    const response = await fetch(`/api/task_reuse/${encodeURIComponent(button.dataset.taskId)}`);
    const body = await response.json();
    if (!body.ok) {
      if (msg) msg.textContent = body.error;
      return;
    }
    fillTaskForm(body.task);
    if (msg) {
      const missing = body.task.missing_media_count ? `，${body.task.missing_media_count} 个历史资源已清理` : "";
      msg.textContent = `已复用 ${body.task.task_id} 的提示词和参考资源${missing}`;
    }
    document.querySelector("#task-form")?.scrollIntoView({ behavior: "smooth", block: "start" });
  });

  document.querySelector("#task-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submitButton = form.querySelector('button[type="submit"]');
    const msg = document.querySelector("#task-message");
    const formData = new FormData(form);
    const promptText = String(formData.get("prompt") || "");
    const imageCount = countKind("image");
    const textOnly = isTextOnlySelected();

    if (!textOnly && imageCount > 0) {
      const missing = [];
      for (let i = 1; i <= imageCount; i++) {
        if (!promptText.includes(`@img${i}`)) missing.push(`@img${i}`);
      }
      let hasExtra = false;
      const imgRegex = /@img(\d+)/g;
      let match;
      while ((match = imgRegex.exec(promptText)) !== null) {
        const idx = Number(match[1]);
        if (idx < 1 || idx > imageCount) hasExtra = true;
      }
      if (missing.length > 0 || hasExtra) {
        let example = "例如：“";
        if (imageCount === 1) example += "男主@img1在奔跑”";
        else if (imageCount === 2) example += "男主@img1 和 女主@img2 在散步”";
        else if (imageCount === 3) example += "男主@img1 和 女主@img2 带着宠物@img3”";
        else if (imageCount === 4) example += "男主@img1 和 女主@img2 带着@img3 在场景@img4 蹦迪”";
        else {
          example += "男主@img1 和 女主@img2 带着@img3 在场景@img4 蹦迪";
          for (let i = 5; i <= imageCount; i++) {
            example += `，并使用道具@img${i}`;
          }
          example += "”";
        }

        const alertMsg = `您当前上传了 ${imageCount} 张参考图，提示词中必须且只能包含 @img1 到 @img${imageCount} 的引用。\n\n${example}\n\n检测到您的引用可能漏写或多写，是否忽略警告强制提交？`;
        if (!confirm(alertMsg)) {
          return;
        }
      }
    } else if (!textOnly && referenceMedia.length === 0) {
      alert("请至少上传 1 张参考图片。");
      return;
    }

    submitButton.disabled = true;
    try {
      if (!textOnly) await uploadPendingMedia();
      const payload = {
        prompt: formData.get("prompt"),
        duration: Number(formData.get("duration")),
        ratio: formData.get("ratio"),
        model_version: formData.get("model_version"),
        media_ids: textOnly ? [] : referenceMedia.map((item) => item.media_id).filter(Boolean),
        queue: !isVipModelSelected() && (document.querySelector("#queue-check")?.checked || false),
        text_only: textOnly,
      };
      if (msg) msg.textContent = "参考资源已准备好，正在提交任务";
      const response = await fetch("/api/submit_task", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json();
      if (!body.ok) {
        if (msg) {
          msg.textContent = body.error;
          msg.style.color = "red";
          msg.style.fontWeight = "bold";
        }
        alert(body.error);
        return;
      }
      if (msg) {
        msg.style.color = "";
        msg.style.fontWeight = "";
        msg.textContent = `任务已提交：${body.task_id}，扣除 ${body.credits} 积分；当前表单已保留，可继续微调后再提交`;
      }
      updateCurrentCredits(body.remaining_credits);
      appendTaskRow(body.task);
    } catch (error) {
      if (msg) msg.textContent = error.message || "提交失败";
    } finally {
      submitButton.disabled = false;
      renderMediaList();
    }
  });
});
