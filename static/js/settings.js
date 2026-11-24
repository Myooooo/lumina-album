/* ==========================================================================
   Lumina Album - Settings: dialogs, editors, folder browser and SD-card sync
   ========================================================================== */
"use strict";

/**
 * Clear every filter and the search box, without reloading.
 *
 * Kept separate from `resetFilters` so a folder change can clear the controls
 * before it decides which view to refresh - and so it can pass the status filter
 * it wants rather than the remembered default.
 */
function clearFilterControls({ status } = {}) {
  const statusValue = status || state.defaultStatusFilter || "analyzed";
  setSelectValue($("statusFilter"), statusValue);
  setSelectValue($("favoriteFilter"), "");
  setSelectValue($("minScoreFilter"), "");
  setSelectValue($("tagFilter"), "");
  setSelectValue($("yearFilter"), "");
  setSelectValue($("kindFilter"), "");
  setSelectValue($("searchMode"), "smart");
  setSelectValue($("sortFilter"), "score_desc");
  const input = $("searchInput");
  if (input) {
    input.value = "";
    input.placeholder = "例如：去年秋天的金黄落叶、西湖落日…";
  }
  updateFilterHighlights();
  updateActiveStat();
}

function resetFilters() {
  clearFilterControls();
  loadPhotos();
}

/**
 * Switch to another album: drop the filters, then refresh what is on screen.
 *
 * The order matters. `reloadCurrentView` reads the search box to decide whether to
 * call the model, so clearing first is what stops a folder change from spending a
 * semantic request on a query the reader has finished with.
 */
async function applyFolderChange(folder) {
  state.folder = folder || "";
  // The badge belongs to the album that is actually open, so it is painted
  // before anything is awaited.
  state.folderHidden = isHiddenFolder(state.folder);
  paintFolderHiddenBadge();
  // A new album's default status filter is derived from what it actually holds,
  // so the controls are cleared before that answer is known - then corrected.
  clearFilterControls({ status: "all" });
  await applyDefaultStatusFilter();
  clearFilterControls({ status: state.defaultStatusFilter || "analyzed" });
  await reloadCurrentView();
}

async function loadPromptPresets() {
  try {
    const data = await api("/api/prompt-presets");
    promptPresets = data.presets || {};
    customPromptPresets = Array.isArray(data.custom) ? data.custom : [];
    if (data.markers && typeof data.markers === "object") {
      promptMarkers = { ...promptMarkers, ...data.markers };
    }
  } catch (e) {
    console.warn("加载提示词预设失败", e);
  }
}

/**
 * Reduce a prompt to its editable part.
 *
 * A stored prompt may predate this version, or the reader may paste a whole
 * prompt (fixed blocks included). Only the style and criteria survive: the
 * anti-AI and output rules are not something a prompt can replace.
 */
function stripPromptFormat(text) {
  let value = String(text || "").trim();
  const preamble = promptMarkers.preamble || "";
  if (preamble && value.startsWith(preamble)) {
    value = value.slice(preamble.length).replace(/^\s+/, "");
  }
  for (const marker of [
    promptMarkers.anti_ai,
    promptMarkers.format,
    promptMarkers.legacy_format,
  ]) {
    if (!marker) continue;
    const index = value.indexOf(marker);
    if (index >= 0) value = value.slice(0, index).trim();
  }
  return value;
}

function activePresetFor(text) {
  const normalized = stripPromptFormat(text);
  return (
    Object.entries(promptPresets).find(
      ([, template]) => stripPromptFormat(template) === normalized
    )?.[0] || ""
  );
}

function activeCustomPresetFor(text) {
  const normalized = stripPromptFormat(text);
  return (
    customPromptPresets.find(
      (preset) => stripPromptFormat(preset.text) === normalized
    )?.id || ""
  );
}

/** Next free 「自定义N」 name, in order of creation. */
function nextCustomPromptName() {
  let highest = 0;
  for (const preset of customPromptPresets) {
    const match = /^自定义(\d+)$/.exec(String(preset.name || ""));
    if (match) highest = Math.max(highest, Number(match[1]));
  }
  return `自定义${highest + 1}`;
}

/** Light the built-in chip whose text is exactly in the editor.
 *
 * A selected saved prompt wins: its trigger is the active choice even when its
 * text happens to equal a built-in (for example, a saved copy of one).
 */
function paintBuiltinPresetHighlight(text) {
  const active = activeCustomPresetId ? "" : activePresetFor(text);
  document.querySelectorAll(".preset-btn[data-preset]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.preset === active);
  });
}

/** Reflect the active saved prompt on the trigger, menu and delete button. */
function paintCustomPromptSelection() {
  const current = customPromptPresets.find(
    (preset) => preset.id === activeCustomPresetId
  );
  const trigger = $("customPromptBtn");
  const label = $("customPromptLabel");
  if (trigger) {
    trigger.classList.toggle("active", Boolean(current));
    trigger.title = current ? current.name : "我保存过的提示词";
  }
  if (label) label.textContent = current ? current.name : "自定义提示词";
  const deleteBtn = $("deletePromptPresetBtn");
  if (deleteBtn) deleteBtn.classList.toggle("hidden", !current);
  document.querySelectorAll(".prompt-custom-option").forEach((option) => {
    const selected = option.dataset.customId === activeCustomPresetId;
    option.classList.toggle("active", selected);
    option.setAttribute("aria-selected", selected ? "true" : "false");
  });
}

/** Build the dropdown rows for every saved prompt. */
function renderCustomPromptMenu() {
  const menu = $("customPromptMenu");
  if (!menu) return;
  if (!customPromptPresets.length) {
    menu.innerHTML =
      '<div class="prompt-custom-empty">还没有保存自定义提示词</div>';
    paintCustomPromptSelection();
    return;
  }
  menu.innerHTML = customPromptPresets
    .map(
      (preset) => `
      <button type="button" class="prompt-custom-option" role="option"
              data-custom-id="${escapeHtml(preset.id)}"
              aria-selected="false"
              title="${escapeHtml(preset.name)}">
        ${ICONS.customPrompt || ""}<span>${escapeHtml(preset.name)}</span>
      </button>`
    )
    .join("");
  menu.querySelectorAll("[data-custom-id]").forEach((option) => {
    option.addEventListener("click", () => {
      applyCustomPrompt(option.dataset.customId);
      closeCustomPromptMenu();
    });
  });
  paintCustomPromptSelection();
}

function customPromptMenuOpen() {
  const menu = $("customPromptMenu");
  return Boolean(menu && !menu.classList.contains("hidden"));
}

function openCustomPromptMenu() {
  renderCustomPromptMenu();
  const menu = $("customPromptMenu");
  const trigger = $("customPromptBtn");
  if (!menu) return;
  menu.classList.remove("hidden");
  if (trigger) trigger.setAttribute("aria-expanded", "true");
}

function closeCustomPromptMenu() {
  const menu = $("customPromptMenu");
  const trigger = $("customPromptBtn");
  if (menu) menu.classList.add("hidden");
  if (trigger) trigger.setAttribute("aria-expanded", "false");
}

function toggleCustomPromptMenu() {
  if (customPromptMenuOpen()) closeCustomPromptMenu();
  else openCustomPromptMenu();
}

function applyPromptPreset(name) {
  const template = promptPresets[name];
  if (!template) {
    showToast("提示词预设不可用", "error");
    return;
  }
  const promptText = stripPromptFormat(template);
  $("setSystemPrompt").value = promptText;
  activeCustomPresetId = "";
  $("promptPresetName").value = "";
  paintBuiltinPresetHighlight(promptText);
  paintCustomPromptSelection();
  closeCustomPromptMenu();
}

function applyCustomPrompt(id) {
  const preset = customPromptPresets.find((item) => item.id === id);
  if (!preset) return;
  const promptText = stripPromptFormat(preset.text);
  $("setSystemPrompt").value = promptText;
  $("promptPresetName").value = preset.name;
  activeCustomPresetId = preset.id;
  paintBuiltinPresetHighlight(promptText);
  paintCustomPromptSelection();
}

/* Editing the text stops matching a built-in; a selected saved prompt stays
   the base being edited until the name field itself is touched. */
function onPromptTextInput() {
  paintBuiltinPresetHighlight($("setSystemPrompt").value);
  paintCustomPromptSelection();
}

/* Editing the name means "save this as a new prompt", not "update the old one". */
function onPromptNameInput() {
  activeCustomPresetId = "";
  // It may now match a built-in exactly; with the saved prompt released, that
  // chip is the right thing to highlight.
  paintBuiltinPresetHighlight($("setSystemPrompt").value);
  paintCustomPromptSelection();
}

/** Save the editor text; a blank name gets 自定义1, 自定义2, ... */
async function saveCustomPrompt() {
  const nameInput = $("promptPresetName");
  let name = (nameInput.value || "").trim();
  if (!name) {
    name = nextCustomPromptName();
    nameInput.value = name;
  }
  const text = stripPromptFormat($("setSystemPrompt").value);
  if (!text) {
    showToast("提示词内容不能为空", "error");
    return;
  }
  try {
    const data = await api("/api/prompt-presets", {
      method: "POST",
      body: JSON.stringify({ id: activeCustomPresetId || "", name, text }),
    });
    customPromptPresets = Array.isArray(data.custom) ? data.custom : [];
    const saved = data.preset || {};
    if (saved.id) activeCustomPresetId = saved.id;
    nameInput.value = saved.name || name;
    renderCustomPromptMenu();
    paintBuiltinPresetHighlight(text);
    paintCustomPromptSelection();
    showToast(
      `已保存自定义提示词「${saved.name || name}」，点「保存设置」后启用。`,
      "success"
    );
  } catch (e) {
    showToast("保存提示词失败：" + e.message, "error");
  }
}

/** Delete the saved prompt the editor is based on. */
async function deleteCustomPrompt() {
  if (!activeCustomPresetId) return;
  const preset = customPromptPresets.find(
    (item) => item.id === activeCustomPresetId
  );
  const ok = await confirmDialog(
    `删除自定义提示词「${preset ? preset.name : ""}」吗？\n删除后不能恢复，但编辑框里的文字会保留。`,
    { title: "删除提示词", confirmText: "删除", danger: true }
  );
  if (!ok) return;
  try {
    const data = await api(
      `/api/prompt-presets/${encodeURIComponent(activeCustomPresetId)}`,
      { method: "DELETE" }
    );
    customPromptPresets = Array.isArray(data.custom) ? data.custom : [];
    activeCustomPresetId = "";
    $("promptPresetName").value = "";
    renderCustomPromptMenu();
    paintBuiltinPresetHighlight($("setSystemPrompt").value);
    paintCustomPromptSelection();
    showToast("已删除这套自定义提示词。", "success");
  } catch (e) {
    showToast("删除提示词失败：" + e.message, "error");
  }
}

/* ==========================================================================
   Settings dialog: tabs
   ========================================================================== */

const SETTINGS_TABS = ["model", "image", "video", "people", "geo", "data"];

/** Slide the "ink" behind the active tab and show its pane. */
function showSettingsTab(name, options = {}) {
  const target = SETTINGS_TABS.includes(name) ? name : SETTINGS_TABS[0];
  const tabs = [...document.querySelectorAll("#settingsModal .settings-tab")];
  const panes = [...document.querySelectorAll("#settingsModal .settings-pane")];
  if (!tabs.length) return;

  tabs.forEach((tab) => {
    const active = tab.dataset.tab === target;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
    tab.tabIndex = active ? 0 : -1;
  });
  panes.forEach((pane) => {
    const active = pane.dataset.pane === target;
    // ``hidden`` is what actually takes the pane out of the layout; the CSS
    // animation then replays from the start each time a tab is opened.
    pane.hidden = !active;
    pane.classList.toggle("active", active);
  });

  moveSettingsTabInk();
  if (options.focus) {
    const activeTab = tabs.find((tab) => tab.dataset.tab === target);
    if (activeTab) activeTab.focus();
  }
}

/** Position the sliding highlight under the active tab. */
function moveSettingsTabInk() {
  const ink = $("settingsTabInk");
  const container = document.querySelector("#settingsModal .settings-tabs");
  const active = container ? container.querySelector(".settings-tab.active") : null;
  if (!ink || !container || !active) return;
  const rect = active.getBoundingClientRect();
  if (!rect.width) {
    // The dialog is still hidden, so there is nothing to measure yet.
    ink.style.opacity = "0";
    return;
  }
  // ``left: 0`` on the ink is the padding edge, while getBoundingClientRect is
  // the border edge; subtracting the border width lines the two up.
  const base = container.getBoundingClientRect();
  const border = container.clientLeft || 0;
  ink.style.opacity = "1";
  ink.style.width = `${rect.width}px`;
  ink.style.transform = `translateX(${rect.left - base.left - border}px)`;
}

/** Arrow-key navigation between tabs, as expected of a real tablist. */
function onSettingsTabKeydown(e) {
  const keys = { ArrowRight: 1, ArrowLeft: -1, Home: "first", End: "last" };
  if (!(e.key in keys)) return;
  e.preventDefault();
  const tabs = [...document.querySelectorAll("#settingsModal .settings-tab")];
  const current = tabs.findIndex((tab) => tab.classList.contains("active"));
  let next;
  if (keys[e.key] === "first") next = 0;
  else if (keys[e.key] === "last") next = tabs.length - 1;
  else next = (current + keys[e.key] + tabs.length) % tabs.length;
  showSettingsTab(tabs[next].dataset.tab, { focus: true });
}

/**
 * Show whether ffmpeg was found.
 *
 * It cannot be configured from here (the path belongs to the machine, not the
 * album), so the row is a status line: the user needs to know why their videos
 * have no preview before they start hunting through the logs.
 */
async function paintFfmpegStatus() {
  const el = $("setFfmpegStatus");
  if (!el) return;
  el.classList.remove("ok", "bad");
  el.textContent = "正在检测 ffmpeg…";
  try {
    const data = await api("/api/video/status");
    state.ffmpegAvailable = !!data.available;
    if (data.available) {
      el.classList.add("ok");
      el.textContent = `已检测到 ffmpeg：${data.version || "版本未知"}`;
    } else {
      el.classList.add("bad");
      el.textContent = "未检测到 ffmpeg。视频仍然会被索引，但无法生成预览或播放。请安装 ffmpeg 或设置 PHOTO_FFMPEG_PATH。";
    }
  } catch (e) {
    el.textContent = "无法检测 ffmpeg： " + e.message;
  }
}

let modelDownloadPollTimer = null;
let lastPeopleEngineStatus = null;

function formatDownloadSpeed(bps) {
  const v = Number(bps) || 0;
  if (v <= 0) return "";
  if (v >= 1024 * 1024) return `${(v / (1024 * 1024)).toFixed(1)} MB/s`;
  if (v >= 1024) return `${(v / 1024).toFixed(0)} KB/s`;
  return `${Math.round(v)} B/s`;
}

async function paintPeopleEngineStatus() {
  const el = $("setPeopleEngineStatus");
  const pillsEl = $("setPeopleStatusPills");
  const pathEl = $("setFaceModelsResolvedPath");
  const listEl = $("setFaceManualList");
  if (!el) return;
  el.classList.remove("ok", "bad");
  el.textContent = "正在检测本地识别引擎与硬件加速…";
  try {
    const data = await api("/api/people/status");
    lastPeopleEngineStatus = data;
    const accel = data.accelerator || "CPU";
    const model = data.model || "buffalo_l";
    const deps = data.deps || {};
    const modelsInstalled = data.models_installed || {};
    const ready = Boolean(data.ready || data.engine_ready);
    const missingPkgs = Array.isArray(data.missing_packages)
      ? data.missing_packages
      : Object.entries(deps).filter(([, ok]) => !ok).map(([k]) => k);
    const modelReady = Boolean(modelsInstalled[model]);

    if (ready) {
      el.classList.add("ok");
      el.textContent = `本地识别引擎已就绪 · 当前模型 ${model} · 硬件加速：${accel}`;
    } else {
      el.classList.add("bad");
      const reasons = [];
      if (missingPkgs.length) reasons.push(`缺少 Python 依赖（${missingPkgs.join(", ")}）`);
      if (!modelReady) reasons.push(`未检测到 ${model} 本地模型文件`);
      el.textContent = `${reasons.join("；") || "尚未就绪"} · 硬件加速：${accel}`;
    }

    if (pillsEl) {
      const pill = (label, ok) =>
        `<span class="engine-pill ${ok ? "is-ok" : "is-miss"}">${ok ? "✓" : "!"} ${escapeHtml(label)}</span>`;
      pillsEl.innerHTML = [
        pill(`加速：${accel}`, Boolean(deps.onnxruntime)),
        pill(`buffalo_l`, Boolean(modelsInstalled.buffalo_l)),
        pill(`buffalo_s`, Boolean(modelsInstalled.buffalo_s)),
        pill(`宠物模型`, Boolean(modelsInstalled.pets)),
        pill(`ultralytics`, Boolean(deps.ultralytics)),
        pill(`insightface`, Boolean(deps.insightface)),
        pill(`onnxruntime`, Boolean(deps.onnxruntime)),
      ].join("");
    }

    if (pathEl && data.models_dir) {
      pathEl.textContent = data.models_dir;
      pathEl.title = data.models_dir;
    }
    if (listEl && data.manual_paths && data.manual_urls) {
      listEl.innerHTML = `
        <li><strong>buffalo_l（默认高精度）</strong>：下载 <a href="${escapeHtml(data.manual_urls.buffalo_l)}" target="_blank" rel="noopener noreferrer">buffalo_l.zip</a> 并解压 <code>.onnx</code> 到 <code>${escapeHtml(data.manual_paths.buffalo_l)}</code></li>
        <li><strong>buffalo_s（轻量版）</strong>：下载 <a href="${escapeHtml(data.manual_urls.buffalo_s)}" target="_blank" rel="noopener noreferrer">buffalo_s.zip</a> 并解压 <code>.onnx</code> 到 <code>${escapeHtml(data.manual_paths.buffalo_s)}</code></li>
        <li><strong>宠物猫狗（可选）</strong>：将 <code>yolov8n.onnx</code> 放至 <code>${escapeHtml(data.manual_paths.pets)}</code></li>
      `;
    }
  } catch (e) {
    el.classList.add("bad");
    el.textContent = "无法检测本地人物识别引擎：" + e.message;
  }
}

function stopModelDownloadPoll() {
  if (modelDownloadPollTimer) {
    clearInterval(modelDownloadPollTimer);
    modelDownloadPollTimer = null;
  }
}

function paintModelDownloadJob(job) {
  const msgEl = $("modelDownloadMessage");
  const fillEl = $("modelDownloadFill");
  const pctEl = $("modelDownloadPct");
  const metaEl = $("modelDownloadMeta");
  const warnEl = $("modelDownloadWarnings");
  const cancelBtn = $("modelDownloadCancelBtn");
  const closeBtn = $("modelDownloadCloseBtn");
  if (!job) return;

  const running = job.status === "running";
  const pct = Math.max(0, Math.min(100, Number(job.percent) || 0));
  if (msgEl) msgEl.textContent = job.message || "正在下载模型…";
  if (fillEl) {
    fillEl.style.width = `${pct}%`;
    fillEl.classList.toggle("is-indeterminate", running && pct <= 1 && !(job.total_bytes > 0));
  }
  if (pctEl) pctEl.textContent = `${Math.round(pct)}%`;
  if (metaEl) {
    if (job.total_bytes > 0) {
      const speed = formatDownloadSpeed(job.speed_bps);
      metaEl.textContent = `${formatBytes(job.downloaded_bytes)} / ${formatBytes(job.total_bytes)}${speed ? ` · ${speed}` : ""}`;
    } else if (job.downloaded_bytes > 0) {
      const speed = formatDownloadSpeed(job.speed_bps);
      metaEl.textContent = `已下载 ${formatBytes(job.downloaded_bytes)}${speed ? ` · ${speed}` : ""}`;
    } else if (job.current_item) {
      metaEl.textContent = job.current_item;
    } else {
      metaEl.textContent = running ? "正在处理…" : "";
    }
  }
  if (warnEl) {
    const notices = [];
    if (job.error) notices.push(job.error);
    if (Array.isArray(job.warnings)) notices.push(...job.warnings);
    if (notices.length) {
      warnEl.innerHTML = notices.map((n) => `<p>${escapeHtml(n)}</p>`).join("");
      warnEl.classList.remove("hidden");
    } else {
      warnEl.classList.add("hidden");
      warnEl.innerHTML = "";
    }
  }
  if (cancelBtn) cancelBtn.classList.toggle("hidden", !running);
  if (closeBtn) closeBtn.classList.toggle("hidden", running);
}

function startModelDownloadPoll() {
  stopModelDownloadPoll();
  modelDownloadPollTimer = setInterval(async () => {
    try {
      const data = await api("/api/people/models/download/status");
      const job = data.job || {};
      paintModelDownloadJob(job);
      if (job.status !== "running") {
        stopModelDownloadPoll();
        await paintPeopleEngineStatus();
        if (job.status === "completed") {
          showToast(job.message || "本地模型准备完成", "success");
        } else if (job.status === "error") {
          showToast("自动下载未完成，可按设置页指引手动下载放置", "warning");
        }
      }
    } catch (e) {
      stopModelDownloadPoll();
    }
  }, 500);
}

async function downloadFaceModels() {
  const btn = $("downloadFaceModelsBtn");
  if (btn) btn.disabled = true;
  try {
    const customDir = $("setFaceModelsDir") ? $("setFaceModelsDir").value.trim() : "";
    const model = $("setFaceModel") ? $("setFaceModel").value : "buffalo_l";
    const includePets = $("setFaceDetectPets") ? $("setFaceDetectPets").checked : true;
    await api("/api/config", {
      method: "POST",
      body: JSON.stringify({ face_model: model, face_detect_pets: includePets, face_models_dir: customDir }),
    });
    const data = await api("/api/people/models/download", {
      method: "POST",
      body: JSON.stringify({ model, include_pets: includePets }),
    });
    const modal = $("modelDownloadModal");
    paintModelDownloadJob(data.job || { status: "running", percent: 0, message: `正在下载 ${model}…` });
    if (modal) {
      showModal(modal);
      pushModal(modal);
    }
    startModelDownloadPoll();
  } catch (e) {
    showToast("启动下载失败：" + e.message, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function cancelModelDownload() {
  try {
    await api("/api/people/models/download/cancel", { method: "POST" });
    showToast("正在取消模型下载…", "info");
  } catch (e) {
    showToast("取消失败：" + e.message, "error");
  }
}

function closeModelDownloadModal() {
  stopModelDownloadPoll();
  closeModal("modelDownloadModal");
  paintPeopleEngineStatus();
}

async function copyFaceModelsPath() {
  const path = (lastPeopleEngineStatus && lastPeopleEngineStatus.models_dir) || ($("setFaceModelsResolvedPath") && $("setFaceModelsResolvedPath").textContent) || "";
  if (!path || path === "检测中…") return;
  const ok = await copyText(path);
  showToast(ok ? `已复制模型目录路径：${path}` : "复制失败，请手动复制", ok ? "success" : "error");
}

async function openFaceModelsDir() {
  const path = (lastPeopleEngineStatus && lastPeopleEngineStatus.models_dir) || "";
  if (!path) {
    showToast("尚未获取到模型目录路径", "warning");
    return;
  }
  await openFolderByPath(path);
}

async function reclusterPeopleFromSettings() {
  const ok = await confirmDialog(
    "重新聚类未命名的人物与宠物吗？\n已手动命名的分组不会丢失，未命名照片会按最新阈值重新整理。",
    { title: "重新聚类", confirmText: "开始聚类" }
  );
  if (!ok) return;
  try {
    const data = await api("/api/people/recluster", { method: "POST" });
    showToast(
      `聚类完成：自动归类 ${data.auto_matched || 0} 处，待确认 ${data.pending_queued || 0} 处，新建分组 ${data.clusters_created || 0} 个。`,
      "success"
    );
    if (state.view === "people" && typeof loadPeopleGrid === "function") {
      loadPeopleGrid();
    }
  } catch (e) {
    showToast("重新聚类失败：" + e.message, "error");
  }
}

async function openSettings() {
  try {
    await loadPromptPresets();
    renderCustomPromptMenu();
    closeCustomPromptMenu();
    const cfg = await api("/api/config");
    $("setApiBase").value = cfg.api_base_url || "";
    $("setApiKey").value = cfg.api_key || "";
    $("setModel").value = cfg.model || "";
    const promptText = stripPromptFormat(cfg.system_prompt || promptPresets.playful || "");
    $("setSystemPrompt").value = promptText;
    activeCustomPresetId = activeCustomPresetFor(promptText);
    const currentCustom = customPromptPresets.find(
      (preset) => preset.id === activeCustomPresetId
    );
    $("promptPresetName").value = currentCustom ? currentCustom.name : "";
    paintBuiltinPresetHighlight(promptText);
    paintCustomPromptSelection();
    $("setMaxEdge").value = cfg.proxy_max_edge || 1280;
    $("setProxyQuality").value = cfg.proxy_quality ?? 80;
    $("setGalleryThumbSize").value = cfg.gallery_thumb_size || 640;
    $("setThumbQuality").value = cfg.thumb_quality ?? 50;
    $("setModelConcurrency").value = cfg.scan_concurrency || 3;
    $("setModelRetries").value = cfg.model_retries ?? 3;
    $("setTimeout").value = cfg.request_timeout || 120;
    $("setIndexThreads").value = cfg.index_concurrency || 4;
    setSelectValue($("setGeocodingProvider"), cfg.geocoding_provider || "nominatim");
    $("setGeocodingApiKey").value = cfg.geocoding_api_key || "";
    $("setGeocodingFallback").checked = !!cfg.geocoding_fallback;
    $("setGeocodingInterval").value = cfg.geocoding_interval || 1.0;
    $("setGeocodingRetries").value = cfg.geocoding_retries ?? 3;
    if ($("setFootprintDefaultView")) setSelectValue($("setFootprintDefaultView"), cfg.footprint_default_view || "grid");
    if ($("setMapTileProvider")) setSelectValue($("setMapTileProvider"), cfg.map_tile_provider || "carto");
    if ($("setMapTileUrl")) $("setMapTileUrl").value = cfg.map_tile_url || "";
    if ($("setMapApiKey")) $("setMapApiKey").value = cfg.map_api_key || "";
    $("setVideoClipSeconds").value = cfg.video_clip_seconds ?? 3;
    $("setVideoClipMaxEdge").value = cfg.video_clip_max_edge || 960;
    $("setVideoClipCrf").value = cfg.video_clip_crf ?? 28;
    $("setVideoPosterRatio").value = cfg.video_poster_ratio ?? 0.1;
    $("setVideoClipAudio").checked = cfg.video_clip_audio !== false;
    $("setVideoPregenerate").checked = !!cfg.video_pregenerate;
    $("setMergeLivePhotos").checked = cfg.merge_live_photos !== false;
    setSelectValue($("setMemoryWindow"), String(cfg.memory_window_days ?? 0));
    setSelectValue($("setVideoPreviewMode"), cfg.video_preview_mode || "once");
    setSelectValue($("setVideoProcessing"), cfg.video_processing || "ffmpeg");
    setVolume("video", cfg.video_preview_volume ?? 50);
    setVolume("live", cfg.live_volume ?? 0);
    if ($("setFaceEnabled")) $("setFaceEnabled").checked = cfg.face_recognition_enabled !== false;
    if ($("setFaceAutoScan")) $("setFaceAutoScan").checked = !!cfg.face_auto_scan;
    if ($("setFaceDetectPets")) $("setFaceDetectPets").checked = cfg.face_detect_pets !== false;
    if ($("setFaceModel")) setSelectValue($("setFaceModel"), cfg.face_model || "buffalo_l");
    if ($("setFaceMatchThreshold")) $("setFaceMatchThreshold").value = cfg.face_match_threshold ?? 0.4;
    if ($("setFaceMinCluster")) $("setFaceMinCluster").value = cfg.face_min_cluster_size ?? 2;
    if ($("setFaceModelsDir")) $("setFaceModelsDir").value = cfg.face_models_dir || "";
    state.videoClipSeconds = Number(cfg.video_clip_seconds) || 3;
    state.videoPreviewMode = cfg.video_preview_mode || "once";
    state.videoPreviewVolume = Number(cfg.video_preview_volume ?? 50);
    state.liveVolume = Number(cfg.live_volume ?? 0);
    state.faceRecognitionEnabled = cfg.face_recognition_enabled !== false;
    state.faceAutoScan = !!cfg.face_auto_scan;
    state.geocodingProvider = cfg.geocoding_provider || "nominatim";
    state.footprintDefaultView = cfg.footprint_default_view || "grid";
    state.mapTileProvider = cfg.map_tile_provider || "carto";
    state.mapTileUrl = cfg.map_tile_url || "";
    state.mapApiKey = cfg.map_api_key || "";
    paintFfmpegStatus();
    paintPeopleEngineStatus();
    // Always open on the model tab: that is what most visits are for.
    showSettingsTab("model");
    showModal($("settingsModal"));
    pushModal($("settingsModal"));
    // The tab widths are only measurable once the dialog is on screen.
    requestAnimationFrame(moveSettingsTabInk);
  } catch (e) {
    showToast("读取设置失败：" + e.message, "error");
  }
}

async function saveSettings() {
  const payload = {
    api_base_url: $("setApiBase").value.trim(),
    api_key: $("setApiKey").value,
    model: $("setModel").value.trim(),
    system_prompt: stripPromptFormat($("setSystemPrompt").value),
    proxy_max_edge: parseInt($("setMaxEdge").value, 10) || 1280,
    proxy_quality: parseInt($("setProxyQuality").value, 10) || 80,
    gallery_thumb_size: parseInt($("setGalleryThumbSize").value, 10) || 640,
    thumb_quality: parseInt($("setThumbQuality").value, 10) || 50,
    scan_concurrency: parseInt($("setModelConcurrency").value, 10) || 3,
    model_retries: parseInt($("setModelRetries").value, 10) || 3,
    request_timeout: parseInt($("setTimeout").value, 10) || 120,
    index_concurrency: parseInt($("setIndexThreads").value, 10) || 4,
    geocoding_provider: $("setGeocodingProvider").value,
    geocoding_api_key: $("setGeocodingApiKey").value,
    geocoding_fallback: $("setGeocodingFallback").checked,
    geocoding_interval: parseFloat($("setGeocodingInterval").value) || 1.0,
    geocoding_retries: parseInt($("setGeocodingRetries").value, 10) || 3,
    footprint_default_view: $("setFootprintDefaultView") ? $("setFootprintDefaultView").value : "grid",
    map_tile_provider: $("setMapTileProvider") ? $("setMapTileProvider").value : "carto",
    map_tile_url: $("setMapTileUrl") ? $("setMapTileUrl").value.trim() : "",
    map_api_key: $("setMapApiKey") ? $("setMapApiKey").value.trim() : "",
    video_clip_seconds: parseFloat($("setVideoClipSeconds").value) || 3,
    video_clip_max_edge: parseInt($("setVideoClipMaxEdge").value, 10) || 960,
    video_clip_crf: parseInt($("setVideoClipCrf").value, 10) || 28,
    video_poster_ratio: parseFloat($("setVideoPosterRatio").value) || 0.1,
    video_clip_audio: $("setVideoClipAudio").checked,
    video_pregenerate: $("setVideoPregenerate").checked,
    merge_live_photos: $("setMergeLivePhotos").checked,
    memory_window_days: parseInt($("setMemoryWindow").value, 10) || 0,
    video_preview_mode: $("setVideoPreviewMode").value,
    video_processing: $("setVideoProcessing").value,
    video_preview_volume: clampVolume($("setVideoPreviewVolume").value),
    live_volume: clampVolume($("setLiveVolume").value),
    face_recognition_enabled: $("setFaceEnabled") ? $("setFaceEnabled").checked : true,
    face_auto_scan: $("setFaceAutoScan") ? $("setFaceAutoScan").checked : false,
    face_detect_pets: $("setFaceDetectPets") ? $("setFaceDetectPets").checked : true,
    face_model: $("setFaceModel") ? $("setFaceModel").value : "buffalo_l",
    face_match_threshold: parseFloat($("setFaceMatchThreshold") ? $("setFaceMatchThreshold").value : "0.4") || 0.4,
    face_min_cluster_size: parseInt($("setFaceMinCluster") ? $("setFaceMinCluster").value : "2", 10) || 2,
    face_models_dir: $("setFaceModelsDir") ? $("setFaceModelsDir").value.trim() : "",
  };
  try {
    await api("/api/config", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    state.faceRecognitionEnabled = !!payload.face_recognition_enabled;
    state.faceAutoScan = !!payload.face_auto_scan;
    state.geocodingProvider = payload.geocoding_provider || "nominatim";
    state.footprintDefaultView = payload.footprint_default_view || "grid";
    state.mapTileProvider = payload.map_tile_provider || "carto";
    state.mapTileUrl = payload.map_tile_url || "";
    state.mapApiKey = payload.map_api_key || "";
    if (typeof refreshFootprintsMapLayer === "function") {
      refreshFootprintsMapLayer();
    }
    closeModal("settingsModal");
    showToast("设置已保存。", "success");
  } catch (e) {
    showToast("保存设置失败：" + e.message, "error");
  }
}

async function cleanCache() {
  if (!await confirmDialog("确定清理所有缓存吗？\n这会移除无引用的代理图片和旧缓存文件，不影响原始照片。", { title: "清理缓存", confirmText: "清理", danger: true })) return;
  try {
    const data = await api("/api/cache/clean", { method: "POST" });
    showToast(`缓存清理完成：释放 ${data.freed} 个缓存文件，${(data.freed_size / 1024 / 1024).toFixed(2)} MB，涉及 ${data.photos_affected || 0} 张照片`, "success");
  } catch (e) {
    showToast("清理缓存失败：" + e.message, "error");
  }
}

const SCORE_EDITORS = [
  { key: "score", numberId: "editScore", rangeId: "editScoreRange" },
  { key: "technical", numberId: "editTechnical", rangeId: "editTechnicalRange" },
  { key: "memory", numberId: "editMemory", rangeId: "editMemoryRange" },
  { key: "uniqueness", numberId: "editUniqueness", rangeId: "editUniquenessRange" },
];

/**
 * Volume rows in the settings dialog.
 *
 * Same shape as the score editors: a label, a themed slider and a number box
 * that stay in step, so a value can be dragged for feel or typed for precision.
 */
const VOLUME_EDITORS = [
  { key: "video", rangeId: "setVideoPreviewVolume", numberId: "setVideoPreviewVolumeNumber" },
  { key: "live", rangeId: "setLiveVolume", numberId: "setLiveVolumeNumber" },
];

function clampVolume(value, fallback = 0) {
  const num = Number(value);
  if (!Number.isFinite(num)) return fallback;
  return Math.max(0, Math.min(100, Math.round(num)));
}

/** Paint the slider's filled portion. */
function paintVolumeRange(rangeEl, value) {
  if (!rangeEl) return;
  rangeEl.style.setProperty("--volume-pct", `${clampVolume(value)}%`);
}

/** Write one volume to both inputs of its row. */
function setVolumeValue(editor, value, source) {
  const rounded = clampVolume(value);
  const rangeEl = $(editor.rangeId);
  const numberEl = $(editor.numberId);
  if (rangeEl && source !== "range") rangeEl.value = String(rounded);
  if (numberEl && source !== "number") numberEl.value = String(rounded);
  paintVolumeRange(rangeEl, rounded);
}

function initVolumeEditors() {
  for (const editor of VOLUME_EDITORS) {
    const rangeEl = $(editor.rangeId);
    const numberEl = $(editor.numberId);
    if (!rangeEl || !numberEl) continue;
    rangeEl.addEventListener("input", () => {
      setVolumeValue(editor, rangeEl.value, "range");
    });
    numberEl.addEventListener("input", () => {
      // Keep typing usable: only mirror once the value parses.
      if (numberEl.value === "" || numberEl.value === "-") return;
      const parsed = parseFloat(numberEl.value);
      if (!Number.isFinite(parsed)) return;
      paintVolumeRange(rangeEl, parsed);
      if (clampVolume(parsed) === parsed) rangeEl.value = String(clampVolume(parsed));
    });
    numberEl.addEventListener("blur", () => {
      // Typing "250" is clamped on the way out rather than under the cursor.
      setVolumeValue(editor, numberEl.value);
    });
  }
}

function clampScore(value, fallback = 0) {
  const num = Number(value);
  if (!Number.isFinite(num)) return fallback;
  return Math.max(0, Math.min(10, Math.round(num * 10) / 10));
}

/** Paint the slider's filled portion so it reads like the detail-page bar. */
function paintScoreRange(rangeEl, value) {
  if (!rangeEl) return;
  rangeEl.style.setProperty("--score-pct", `${clampScore(value) * 10}%`);
}

/** Write one score to both inputs of its row. */
function setScoreValue(editor, value, source) {
  const rounded = clampScore(value);
  const rangeEl = $(editor.rangeId);
  const numberEl = $(editor.numberId);
  if (rangeEl && source !== "range") rangeEl.value = String(rounded);
  if (numberEl && source !== "number") numberEl.value = rounded.toFixed(1);
  paintScoreRange(rangeEl, rounded);
}

function initScoreEditors() {
  for (const editor of SCORE_EDITORS) {
    const rangeEl = $(editor.rangeId);
    const numberEl = $(editor.numberId);
    if (!rangeEl || !numberEl) continue;
    rangeEl.addEventListener("input", () => {
      setScoreValue(editor, rangeEl.value, "range");
    });
    numberEl.addEventListener("input", () => {
      // Keep typing usable: only mirror once the value parses.
      if (numberEl.value === "" || numberEl.value === "-") return;
      const parsed = parseFloat(numberEl.value);
      if (!Number.isFinite(parsed)) return;
      const clamped = clampScore(parsed);
      const rangeElNow = $(editor.rangeId);
      if (rangeElNow) rangeElNow.value = String(clamped);
      paintScoreRange(rangeElNow, clamped);
    });
    numberEl.addEventListener("blur", () => {
      // Normalise on blur: clamp out-of-range input and re-format to one
      // decimal, writing both the box and the slider.
      setScoreValue(editor, numberEl.value);
    });
    setScoreValue(editor, numberEl.value || 0);
  }
}

function openEditPreview() {
  const photo = photoById(state.currentPreviewId);
  if (!photo) return;
  const dims = photo.dimensions || {};
  $("editTitle").value = photo.title || "";
  const values = {
    score: photo.score ?? 0,
    technical: dims.technical ?? 0,
    memory: dims.memory ?? 0,
    uniqueness: dims.uniqueness ?? 0,
  };
  for (const editor of SCORE_EDITORS) {
    setScoreValue(editor, values[editor.key]);
  }
  $("editLocation").value = photo.location || "";
  $("editTags").value = (photo.tags || []).join("，");
  $("editReason").value = photo.reason || "";
  showModal($("editModal"));
  pushModal($("editModal"));
}

async function saveEditedPhoto() {
  const id = state.currentPreviewId;
  const photo = photoById(id);
  if (!photo) return;
  const score = parseFloat($("editScore").value);
  if (Number.isNaN(score) || score < 0 || score > 10) {
    showToast("总分需要在 0 到 10 之间", "error");
    return;
  }
  const dimensions = {};
  for (const key of ["technical", "memory", "uniqueness"]) {
    const el = $("edit" + key[0].toUpperCase() + key.slice(1));
    const value = parseFloat(el.value);
    if (Number.isNaN(value) || value < 0 || value > 10) {
      showToast("各项评分需要在 0 到 10 之间", "error");
      return;
    }
    dimensions[key] = value;
  }
  const tags = $("editTags").value
    .replace(/，/g, ",")
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  try {
    const updated = await api(`/api/photo/${id}/edit`, {
      method: "POST",
      body: JSON.stringify({
        score,
        dimensions,
        tags,
        title: $("editTitle").value.trim(),
        reason: $("editReason").value.trim(),
        location: $("editLocation").value.trim(),
      }),
    });
    closeModal("editModal");
    const idx = photoIndexById(id);
    if (idx >= 0 && updated) {
      // A hand-edit rewrites one row; repaint that card, not the whole grid.
      Object.assign(state.photos[idx], updated);
      invalidatePhotoDetail(id);
      refreshCard(id);
    }
    if (state.currentPreviewId === id) updatePreview({ skipImage: true });
    showToast("这段回忆已经更新。", "success");
  } catch (e) {
    showToast("保存失败：" + e.message, "error");
  }
}

async function removeCurrentFolder() {
  const folder = state.folder || $("folderInput").value.trim();
  if (!folder) {
    showToast("请先选择一个照片文件夹", "error");
    return;
  }
  const ok = await confirmDialog(
    "确定从拾光相册中移除当前目录吗？\n只会清除数据库记录和该目录的代理缓存，不会改动任何照片原图。",
    { title: "移除当前目录", confirmText: "移除目录", danger: true }
  );
  if (!ok) return;
  try {
    const data = await api("/api/folder/remove", {
      method: "POST",
      body: JSON.stringify({ folder }),
    });
    closeModal("settingsModal");
    if (!$("previewModal").classList.contains("hidden")) closeModal("previewModal");
    state.folder = "";
    indexPhotos([]);
    state.selected.clear();
    state.details.clear();
    state.detailRequested.clear();
    state.folder = await selectBestFolder();
    saveUiState({ folder: state.folder });
    $("folderInput").value = state.folder;
    await reloadCurrentView();
    updateTrashActions();
    const fallback = state.folder
      ? `已自动回到上一个有效目录：${state.folder}`
      : "当前没有其他已索引目录，已留空。";
    showToast(`已移除 ${data.removed || 0} 条照片记录和缓存，原图未做任何改动。${fallback}`, "success");
  } catch (e) {
    showToast("移除当前目录失败：" + e.message, "error");
  }
}

/** Hide the open album from every main-page path selector. */
async function hideCurrentFolder() {
  const folder = state.folder || $("folderInput").value.trim();
  if (!folder) {
    showToast("请先选择一个照片文件夹", "error");
    return;
  }
  if (isHiddenFolder(folder)) {
    showToast("这个目录已经隐藏了。", "info");
    return;
  }
  const ok = await confirmDialog(
    `隐藏「${folder}」吗？\n隐藏后它不会出现在主页的路径选择里，只能从「设置 → 系统 → 查看隐藏的目录」进入。照片、评分与缓存都会保留。`,
    { title: "隐藏当前目录", confirmText: "隐藏" }
  );
  if (!ok) return;
  try {
    const data = await api("/api/folders/hide", {
      method: "POST",
      body: JSON.stringify({ folder }),
    });
    state.hiddenFolders = Array.isArray(data.folders) ? data.folders : [];
    state.folderHistory = state.folderHistory.filter(
      (item) => !isHiddenFolder(item)
    );
    // The album that was open just left the visible world: move to the last
    // album that is still visible, and remember *that* one for the next boot.
    const next = await selectBestFolder();
    if (!next) {
      // No visible album left. Show the empty state rather than letting the
      // gallery fall back to "every album", which would put the photos that
      // were just hidden back on screen.
      state.folder = "";
      state.folderHidden = false;
      $("folderInput").value = "";
      saveUiState({ folder: "" });
      indexPhotos([]);
      renderGallery();
      updateSelectionBar();
      loadStats();
      loadTags();
      loadYears();
      paintFolderHiddenBadge();
      showToast("已隐藏这个目录，现在没有其他可见目录。", "success");
      return;
    }
    state.folder = next;
    $("folderInput").value = next;
    saveUiState({ folder: next });
    await applyFolderChange(next);
    showToast("已隐藏这个目录，可以从设置面板重新进入。", "success");
  } catch (e) {
    showToast("隐藏目录失败：" + e.message, "error");
  }
}

/** Build the rows of the hidden-album dialog. */
function renderHiddenFolders() {
  const list = $("hiddenFoldersList");
  if (!list) return;
  if (!state.hiddenFolders.length) {
    list.innerHTML = '<div class="hidden-folders-empty">还没有隐藏的目录。</div>';
    return;
  }
  list.innerHTML = state.hiddenFolders
    .map(
      (folder) => `
      <div class="hidden-folder-row">
        <button type="button" class="hidden-folder-open" data-enter="${escapeHtml(folder)}" title="${escapeHtml(folder)}">
          ${ICONS.folder || ""}<span>${escapeHtml(folder)}</span>
        </button>
        <button type="button" class="btn small hidden-folder-unhide" data-unhide="${escapeHtml(folder)}">取消隐藏</button>
      </div>`
    )
    .join("");
  list.querySelectorAll("[data-enter]").forEach((btn) => {
    btn.addEventListener("click", () => enterHiddenFolder(btn.dataset.enter));
  });
  list.querySelectorAll("[data-unhide]").forEach((btn) => {
    btn.addEventListener("click", () => unhideFolder(btn.dataset.unhide));
  });
}

/** Open the hidden-album list from the settings panel. */
async function openHiddenFolders() {
  await loadHiddenFolders();
  renderHiddenFolders();
  showModal($("hiddenFoldersModal"));
  pushModal($("hiddenFoldersModal"));
}

/** Leave the settings panel and open a hidden album, marker included. */
function enterHiddenFolder(folder) {
  closeModal("hiddenFoldersModal");
  closeModal("settingsModal");
  showView("album");
  openAlbumFolder(folder);
  showToast(`已进入隐藏目录：${folder}`, "info");
}

/** Bring an album back to the normal path selectors. */
async function unhideFolder(folder) {
  try {
    const data = await api("/api/folders/unhide", {
      method: "POST",
      body: JSON.stringify({ folder }),
    });
    state.hiddenFolders = Array.isArray(data.folders) ? data.folders : [];
    renderHiddenFolders();
    if (state.folder && folderPathKey(state.folder) === folderPathKey(folder)) {
      state.folderHidden = false;
      paintFolderHiddenBadge();
      rememberFolder(state.folder);
      saveUiState({ folder: state.folder });
    }
    showToast("已取消隐藏，它会重新出现在路径选择里。", "success");
  } catch (e) {
    showToast("取消隐藏失败：" + e.message, "error");
  }
}

async function emptyTrash() {
  const scope = state.folder ? "当前目录" : "全部目录";
  const answer = await confirmDialog(
    `确定清空${scope}的回收站吗？\n回收站中的照片原文件将被彻底删除，此操作无法恢复。`,
    {
      title: "清空回收站",
      confirmText: "彻底清空",
      danger: true,
      checkbox: { label: "同时删除 RAW", checked: true },
    }
  );
  const confirmed = typeof answer === "object" ? answer.confirmed : answer;
  if (!confirmed) return;
  const deleteRaw = typeof answer === "object" ? answer.checked : true;
  try {
    // The sweep runs as a tracked job so it gets the same progress bar as a
    // scan; a full bin can hold thousands of files and used to freeze the UI.
    const data = await api("/api/trash/empty", {
      method: "POST",
      body: JSON.stringify({
        folder: state.folder || undefined,
        delete_raw: deleteRaw,
        background: true,
      }),
    });
    state.selected.clear();
    state.currentJobId = data.job_id;
    showProgress("正在清空回收站…");
    pollScan();
  } catch (e) {
    showToast("清空回收站失败：" + e.message, "error");
  }
}

function handlePromptPresetClick(name) {
  applyPromptPreset(name);
}

async function openDirBrowser() {
  const path = state.folder || "";
  showModal($("dirModal"));
  pushModal($("dirModal"));
  await loadDirList(path || "");
}

async function loadDirList(path) {
  if (!path) path = "";
  state.currentDirPath = path;
  $("dirPathInput").value = path;
  try {
    const q = path ? `?path=${encodeURIComponent(path)}` : "";
    const data = await api(`/api/dirs${q}`);
    state.currentDirPath = data.path;
    $("dirPathInput").value = data.path;
    const list = $("dirList");
    list.innerHTML = "";
    if (data.parent) {
      const up = document.createElement("li");
      up.innerHTML = `${ICONS.arrowUp || ""} <span>返回上一级</span>`;
      up.addEventListener("click", () => loadDirList(data.parent));
      list.appendChild(up);
    }
    if (!data.directories.length) {
      const li = document.createElement("li");
      li.textContent = "（没有子目录）";
      li.style.color = "var(--muted)";
      list.appendChild(li);
    }
    for (const dir of data.directories) {
      // Hidden albums are not offered as a path anywhere on the main page; the
      // settings panel's hidden list is the way back in.
      if (isHiddenFolder(dir.path)) continue;
      const li = document.createElement("li");
      li.innerHTML = `${ICONS.folder || ""} <span>${escapeHtml(dir.name)}</span>`;
      li.dataset.path = dir.path;
      li.addEventListener("click", () => {
        $("dirPathInput").value = dir.path;
        state.currentDirPath = dir.path;
        $("dirChooseBtn").disabled = false;
        document.querySelectorAll("#dirList li").forEach((x) => x.classList.remove("active"));
        li.classList.add("active");
      });
      li.addEventListener("dblclick", () => loadDirList(dir.path));
      list.appendChild(li);
    }
    $("dirChooseBtn").disabled = !state.currentDirPath;
  } catch (e) {
    showToast("读取目录失败：" + e.message, "error");
  }
}

async function toggleCurrentPreviewFavorite() {
  const photo = photoById(state.currentPreviewId);
  if (!photo) return;
  const newVal = !photo.favorite;
  try {
    await api("/api/favorite", {
      method: "POST",
      body: JSON.stringify({ id: photo.id, favorite: newVal }),
    });
    photo.favorite = newVal;
    updatePreview({ skipImage: true });
    updateCardFavorite(photo.id);
    loadStats();
    if ($("favoriteFilter").value === "1" && !newVal) {
      await loadPhotos();
      if (state.currentPreviewId && !state.photos.some((p) => p.id === state.currentPreviewId)) {
        closeModal("previewModal");
      }
    }
  } catch (e) {
    showToast("珍藏操作失败：" + e.message, "error");
  }
}

/* ==========================================================================
   SD card import
   ========================================================================== */

function importOptions() {
  return {
    source: $("importSource").value.trim(),
    target: state.folder || "",
    mode: $("importMode").value,
    raw_policy: $("importRawPolicy").value,
    after: $("importAfter").value,
    reset_analysis: $("importResetAnalysis").checked,
  };
}

function setImportPreview(html) {
  const box = $("importPreview");
  if (!html) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.innerHTML = html;
  box.classList.remove("hidden");
}

function formatBytes(bytes) {
  const value = Number(bytes) || 0;
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function renderImportPlan(plan) {
  const warnings = (plan.warnings || [])
    .map((text) => `<p class="import-warning">${escapeHtml(text)}</p>`)
    .join("");
  const items = (plan.items || [])
    .map((item) => {
      const tags = [
        `<span class="import-tag ${item.action}">${item.action === "add" ? "新增" : "覆盖"}</span>`,
      ];
      if (item.is_raw) tags.push(`<span class="import-tag raw">RAW</span>`);
      return `<li>${tags.join("")}<span class="import-path" title="${escapeHtml(item.relative_path)}">${escapeHtml(item.relative_path)}</span><span>${formatBytes(item.size)}</span></li>`;
    })
    .join("");
  const truncated = plan.items_truncated
    ? `<li><span class="import-path">…… 另有 ${plan.items_truncated} 个文件未列出</span></li>`
    : "";
  const list = items ? `<ul class="import-list">${items}${truncated}</ul>` : "";
  return `
    <div class="import-summary">
      <span>新增 <strong>${plan.new_count}</strong></span>
      <span>覆盖 <strong>${plan.overwrite_count}</strong></span>
      <span>已存在跳过 <strong>${plan.skip_count}</strong></span>
      <span>本地独有（不动） <strong>${plan.local_only_count}</strong></span>
      <span>待拷贝 <strong>${formatBytes(plan.total_bytes)}</strong></span>
    </div>
    ${warnings}
    ${list}
    ${plan.new_count + plan.overwrite_count === 0 ? `<p class="import-note">本地已经是最新的，没有需要拷贝的文件。</p>` : ""}
  `;
}

async function planImport(options = {}) {
  const payload = importOptions();
  if (!payload.source) {
    showToast("请先选择 SD 卡或来源文件夹", "error");
    return null;
  }
  if (!payload.target) {
    showToast("请先在上方选择一个本地回忆文件夹", "error");
    return null;
  }
  if (!options.silent) showLoading("正在比对 SD 卡与本地文件夹…");
  try {
    const data = await api("/api/import/plan", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setImportPreview(renderImportPlan(data.plan));
    return data.plan;
  } catch (e) {
    setImportPreview("");
    showToast("预演失败：" + e.message, "error");
    return null;
  } finally {
    if (!options.silent) hideLoading();
  }
}

async function runImport() {
  const payload = importOptions();
  if (!payload.source) {
    showToast("请先选择 SD 卡或来源文件夹", "error");
    return;
  }
  if (!payload.target) {
    showToast("请先在上方选择一个本地回忆文件夹", "error");
    return;
  }
  const plan = await planImport({ silent: true });
  if (!plan) return;
  const changes = plan.new_count + plan.overwrite_count;
  if (!changes) {
    showToast("没有需要拷贝的文件。", "info");
    return;
  }
  const modeText = payload.mode === "full" ? "全量导入" : "增量导入";
  const confirmed = await confirmDialog(
    `${modeText}：将新增 ${plan.new_count} 个、覆盖 ${plan.overwrite_count} 个文件，共 ${formatBytes(plan.total_bytes)}。\n本地独有、SD 卡上没有的 ${plan.local_only_count} 个文件不会被改动。`,
    { title: "开始导入", confirmText: "开始导入" }
  );
  if (!confirmed) return;

  try {
    const data = await api("/api/import/run", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    closeModal("importModal");
    state.currentJobId = data.job_id;
    showProgress("正在从 SD 卡导入…");
    pollScan();
  } catch (e) {
    showToast("导入失败：" + e.message, "error");
  }
}

async function openImportDialog() {
  if (!state.folder) {
    showToast("请先在上方选择一个本地回忆文件夹", "error");
    return;
  }
  $("importTarget").value = state.folder;
  await prefillImportSource();
  setImportPreview("");
  showModal($("importModal"));
  pushModal($("importModal"));
  // Opening the dialog is the moment the card is expected to be in the reader,
  // so any deletion queued while it was away is offered right here.
  const source = $("importSource").value.trim();
  if (source) await offerCardSync(source);
}

/**
 * Fill the source field with the folder this album was last imported from.
 *
 * The remembered card path is the fallback, but a camera album's own pairing is
 * the better answer: it is stored per album, survives a browser change, and is
 * exactly what the user means by "the SD card I use for these photos". The
 * field is only filled when it is empty, so a path typed in this session is
 * never overwritten.
 */
async function prefillImportSource() {
  const field = $("importSource");
  if (field.value.trim()) return;
  let source = state.importSource || "";
  if (state.folder) {
    try {
      const query = `?folder=${encodeURIComponent(state.folder)}`;
      const data = await api(`/api/card/status${query}`);
      const current = data.current;
      if (current && current.source) source = current.source;
    } catch (e) {
      console.warn("读取相机相册配对失败", e);
    }
  }
  if (source) field.value = source;
}

async function pickImportSource() {
  // Reuse the folder browser. The pending flag makes the browser hand its
  // selection back to the import dialog instead of switching the album folder.
  state.importPickPending = true;
  hideModal($("importModal"));
  await openDirBrowser();
}

function rememberImportSource(path) {
  state.importSource = String(path || "").trim();
  saveUiState({ import_source: state.importSource });
}

/** Hand an arbitrary folder to the OS file manager. */
async function openFolderByPath(path) {
  if (!path) {
    showToast("没有可打开的路径", "error");
    return;
  }
  try {
    await api("/api/open-folder", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
  } catch (e) {
    showToast("无法打开文件夹：" + e.message, "error");
  }
}

/**
 * Toolbar badge click: sync when there is queued work, explain when there is
 * not. The two cases are one click apart on purpose, so the badge never opens
 * an empty dialog.
 */
async function onCardStatusClick() {
  const card = await refreshCardStatus();
  if (!card) return;
  if (!card.pending.total) {
    const why = card.usable
      ? "卡片已连接，删除照片时可以选择同时从卡片移除。"
      : (card.warnings && card.warnings[0]) || "卡片当前不可用。";
    showToast(`${card.source}\n${why}`, "info");
    return;
  }
  await openCardSyncDialog({ source: card.source });
}

const CARD_POLL_MS = 15000;

function startCardPolling() {
  if (state.cardTimer) clearInterval(state.cardTimer);
  state.cardTimer = setInterval(() => {
    // Hidden tabs do not need to stat an SD card every 15 seconds.
    if (document.hidden) return;
    refreshCardStatus();
  }, CARD_POLL_MS);
}

/**
 * Offer the queued card work when a card that has some shows up.
 *
 * Called after an import finishes and when the import dialog is opened with a
 * remembered source, which is exactly the moment the user just plugged the card
 * in and expects to be told about last time's deletions.
 */
async function offerCardSync(source) {
  const card = await refreshCardStatus();
  if (source) {
    const match = (state.cardAll || []).find(
      (c) => folderPathKey(c.source) === folderPathKey(source)
    );
    if (match) state.card = match;
  }
  const target = state.card;
  if (!target || !target.pending.total || !target.usable) return false;
  await openCardSyncDialog({ source: target.source });
  return true;
}

/* ==========================================================================
   Camera album / SD card
   ========================================================================== */

/** Turn one API card report into a toast the user can act on. */
function reportCardResult(card, action) {
  if (!card) return;
  const parts = [];
  if (card.synced) parts.push(`已从 SD 卡${action} ${card.synced} 个文件`);
  if (card.restored) parts.push(`已把 ${card.restored} 个文件放回 SD 卡`);
  if (card.queued) parts.push(`已记录 ${card.queued} 项待同步`);
  if (card.errors && card.errors.length) {
    showToast(`SD 卡${action}部分失败：` + card.errors.map((e) => e.error).join("; "), "error");
  }
  if (parts.length) showToast(parts.join("，") + "。", "success");
}

/** Poll the card state of the current album and repaint the toolbar badge. */
async function refreshCardStatus() {
  let data;
  try {
    const query = state.folder ? `?folder=${encodeURIComponent(state.folder)}` : "";
    data = await api(`/api/card/status${query}`);
  } catch (e) {
    state.card = null;
    paintCardStatus();
    return null;
  }
  state.card = data.current || null;
  state.cardAll = data.cards || [];
  paintCardStatus();
  return state.card;
}

const CARD_STATE_TEXT = {
  connected: "SD 卡已连接",
  disconnected: "SD 卡未连接",
  read_only: "SD 卡只读",
  volume_mismatch: "路径已被其他磁盘占用",
};

const CARD_STATE_ICON = {
  connected: "sdCard",
  disconnected: "sdCardOff",
  read_only: "sdCardWarning",
  volume_mismatch: "sdCardWarning",
};

function paintCardStatus() {
  const btn = $("cardStatusBtn");
  if (!btn) return;
  const card = state.card;
  if (!card) {
    btn.classList.add("hidden");
    hideStrip($("cardBanner"));
    return;
  }
  // Same box metrics as the toolbar buttons next to it; only the state class
  // differs, so the badge reads as one of them rather than as a pasted-on chip.
  btn.className = `btn card-status state-${card.state}`;
  btn.classList.remove("hidden");
  btn.title = card.source + (card.warnings && card.warnings.length ? `\n${card.warnings.join("\n")}` : "");
  $("cardStatusText").textContent = CARD_STATE_TEXT[card.state] || "相机相册";
  $("cardStatusIcon").innerHTML = ICONS[CARD_STATE_ICON[card.state]] || ICONS.sdCard || "";

  const pending = card.pending || {};
  const dot = $("cardPendingDot");
  if (pending.total) {
    dot.textContent = String(pending.total);
    dot.classList.remove("hidden");
  } else {
    dot.classList.add("hidden");
  }

  // Dismissing the strip lasts until more work piles up: a dismissal is "I know
  // about these", not "never tell me again".
  if (pending.total > (state.cardPendingSeen || 0)) state.cardBannerDismissed = false;
  if (!pending.total) state.cardBannerDismissed = false;
  state.cardPendingSeen = pending.total;

  // The strip only appears once there is something waiting and the card can
  // actually take it: queueing silently is fine, nagging is not.
  const banner = $("cardBanner");
  if (pending.total && card.usable && !state.cardBannerDismissed) {
    const bits = [];
    if (pending.delete_count) bits.push(`${pending.delete_count} 项删除`);
    if (pending.restore_count) bits.push(`${pending.restore_count} 项恢复`);
    if (pending.erase_count) bits.push(`${pending.erase_count} 项彻底清理`);
    $("cardBannerText").textContent = `${card.source} 上有 ${bits.join("、")}待同步。`;
    showStrip(banner);
  } else {
    hideStrip(banner);
  }
}

function renderCardPreview(preview) {
  const summary = preview.summary || {};
  const rows = (preview.items || [])
    .map((item) => {
      const kindLabel = { delete: "删除", restore: "恢复", erase: "彻底清理" }[item.kind] || item.kind;
      const tags = [`<span class="import-tag ${item.kind}">${kindLabel}</span>`];
      if (item.is_raw) tags.push('<span class="import-tag raw">RAW</span>');
      const missing = item.on_card === false
        ? '<span class="import-missing-note">卡上已找不到</span>'
        : "";
      const cls = item.on_card === false ? "import-path missing" : "import-path";
      return `<li>${tags.join("")}<span class="${cls}" title="${escapeHtml(item.relative_path)}">${escapeHtml(item.relative_path)}</span>${missing}<span>${formatBytes(item.size)}</span></li>`;
    })
    .join("");
  const truncated = preview.items_truncated
    ? `<li><span class="import-path">…… 另有 ${preview.items_truncated} 个文件未列出</span></li>`
    : "";
  const list = rows ? `<ul class="import-list">${rows}${truncated}</ul>` : "";
  const target = preview.target || {};
  const targetNote = target.usable
    ? ""
    : `<p class="import-warning">${escapeHtml(
        (target.warnings && target.warnings[0]) || "SD 卡当前不可用，暂时无法同步。"
      )}</p>`;
  return `
    <div class="import-summary">
      <span>待删除 <strong>${summary.delete_count || 0}</strong></span>
      <span>待恢复 <strong>${summary.restore_count || 0}</strong></span>
      <span>彻底清理 <strong>${summary.erase_count || 0}</strong></span>
      <span>含 RAW <strong>${summary.raw_count || 0}</strong></span>
      <span>合计 <strong>${formatBytes(summary.total_bytes || 0)}</strong></span>
    </div>
    ${targetNote}
    ${
      summary.missing_count
        ? `<p class="import-warning">有 ${summary.missing_count} 个文件在卡上已经找不到了，同步时会跳过。</p>`
        : ""
    }
    ${list}
    ${
      summary.total
        ? ""
        : `<p class="import-note">没有需要同步的内容，卡片和本地已经一致。</p>`
    }
  `;
}

async function openCardSyncDialog(options = {}) {
  let preview;
  try {
    preview = (await api("/api/card/pending", {
      method: "POST",
      body: JSON.stringify({ source: options.source || (state.card ? state.card.source : "") }),
    })).preview;
  } catch (e) {
    showToast("读取待同步内容失败：" + e.message, "error");
    return;
  }
  state.cardPreview = preview;
  $("cardSyncSource").textContent = preview.source || "—";
  $("cardSyncPreview").innerHTML = renderCardPreview(preview);
  $("cardSyncPreview").classList.remove("hidden");
  const hasRaw = (preview.summary && preview.summary.raw_count) > 0;
  $("cardSyncRawRow").classList.toggle("hidden", !hasRaw);
  $("cardSyncRunBtn").disabled = !preview.ready || !preview.summary.total;
  $("cardSyncRunBtn").title = preview.ready ? "" : "SD 卡当前不可用";
  showModal($("cardSyncModal"));
  pushModal($("cardSyncModal"));
}

async function reloadCardPreview() {
  await refreshCardStatus();
  const source = state.cardPreview ? state.cardPreview.source : "";
  await openCardSyncDialog({ source });
}

async function runCardSync() {
  const preview = state.cardPreview;
  if (!preview) return;
  const summary = preview.summary || {};
  if (summary.missing_count) {
    const ok = await confirmDialog(
      `有 ${summary.missing_count} 个文件在卡上已经找不到，它们会被跳过。\n其余 ${summary.total - summary.missing_count} 项将立即执行。`,
      { title: "部分文件缺失", confirmText: "继续同步", danger: true }
    );
    if (!ok) return;
  }
  const includeRaw = $("cardSyncIncludeRaw").checked;
  showLoading("正在同步到 SD 卡…");
  try {
    const result = await api("/api/card/sync", {
      method: "POST",
      body: JSON.stringify({ source: preview.source, include_raw: includeRaw }),
    });
    hideLoading();
    const parts = [];
    if (result.applied) parts.push(`删除 ${result.applied}`);
    if (result.restored) parts.push(`恢复 ${result.restored}`);
    if (result.erased) parts.push(`彻底清理 ${result.erased}`);
    if (result.missing) parts.push(`跳过缺失 ${result.missing}`);
    if (result.skipped) parts.push(`保留 RAW ${result.skipped}`);
    const tail = result.remaining ? `，仍有 ${result.remaining} 项待处理` : "";
    showToast(`SD 卡同步完成：${parts.join("，") || "无变化"}${tail}。`, "success");
    if (result.failed && result.failed.length) {
      showToast("部分项目未能同步：" + result.failed.map((f) => f.error).join("; "), "error");
    }
    await refreshCardStatus();
    if (result.remaining && preview.ready) {
      await openCardSyncDialog({ source: preview.source });
    } else {
      closeModal("cardSyncModal");
    }
  } catch (e) {
    hideLoading();
    showToast("同步到 SD 卡失败：" + e.message, "error");
  }
}

/** Forget the pairing for the current folder (never touches card files). */
async function forgetCurrentCard() {
  const card = state.card || state.cardPreview;
  if (!card) return;
  const album = card.album || state.folder;
  if (!album) return;
  const total = (card.pending && card.pending.total) || 0;
  const ok = await confirmDialog(
    `确定不再把“${album}”与 ${card.source} 关联吗？\n会清掉尚未同步的 ${total} 项记录，但不会删除本地或 SD 卡上的任何文件。`,
    { title: "解除关联", confirmText: "解除关联", danger: true }
  );
  if (!ok) return;
  try {
    await api("/api/card/forget", {
      method: "POST",
      body: JSON.stringify({ folder: album, source: card.source }),
    });
    closeModal("cardSyncModal");
    showToast("已解除相机相册关联，照片与卡片文件都未改动。", "success");
    await refreshCardStatus();
  } catch (e) {
    showToast("解除关联失败：" + e.message, "error");
  }
}

/** Copy text, preferring the async clipboard API with a legacy fallback. */
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext !== false) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) {
    // fall through to the textarea fallback
  }
  try {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    area.remove();
    return ok;
  } catch (e) {
    return false;
  }
}

/**
 * Handle clicks inside the detail panel's metadata block.
 *
 * A path copies itself; the button beside it asks the server to open the file
 * with the OS default application (which is how a RAW can be handed to the
 * camera vendor's editor).
 */
async function handlePreviewInfoClick(event) {
  const rejectBtn = event.target.closest(".preview-person-reject");
  if (rejectBtn) {
    event.stopPropagation();
    const faceId = Number(rejectBtn.dataset.faceId);
    if (!Number.isFinite(faceId)) return;
    try {
      await api(`/api/faces/${faceId}`, {
        method: "PATCH",
        body: JSON.stringify({ action: "reject" }),
      });
      const photoId = state.currentPreviewId;
      if (photoId) {
        invalidatePhotoDetail(photoId);
        await ensurePhotoDetail(photoId);
      }
      showToast("已移出该识别标注。", "info");
    } catch (e) {
      showToast("操作失败：" + e.message, "error");
    }
    return;
  }

  const renameBtn = event.target.closest(".preview-person-rename");
  if (renameBtn) {
    event.stopPropagation();
    const pill = renameBtn.closest(".preview-person-pill");
    const faceId = Number(renameBtn.dataset.faceId);
    const currentName = pill ? pill.dataset.personName || "" : "";
    const name = window.prompt("输入人物或宠物的名字（如与已有同名将自动归入）：", currentName);
    if (name === null) return;
    const clean = name.trim();
    if (!clean) return;
    try {
      await api(`/api/faces/${faceId}`, {
        method: "PATCH",
        body: JSON.stringify({ action: "assign", name: clean }),
      });
      const photoId = state.currentPreviewId;
      if (photoId) {
        invalidatePhotoDetail(photoId);
        await ensurePhotoDetail(photoId);
      }
      showToast(`已标注为「${clean}」，后续相似照片将自动归入。`, "success");
    } catch (e) {
      showToast("标注失败：" + e.message, "error");
    }
    return;
  }

  const personMain = event.target.closest(".preview-person-main");
  if (personMain) {
    event.stopPropagation();
    const pill = personMain.closest(".preview-person-pill");
    const faceId = pill ? Number(pill.dataset.faceId) : NaN;
    const personId = pill ? Number(pill.dataset.personId) : NaN;
    const personName = pill ? pill.dataset.personName || "" : "";
    if (Number.isFinite(personId) && personId > 0 && personName) {
      closeModal("previewModal");
      if (typeof openPeople === "function") openPeople(personId);
      return;
    }
    if (Number.isFinite(faceId)) {
      const name = window.prompt("给这位人物或宠物起个名字（如与已有同名将自动归入）：", "");
      if (name === null) return;
      const clean = name.trim();
      if (!clean) return;
      try {
        await api(`/api/faces/${faceId}`, {
          method: "PATCH",
          body: JSON.stringify({ action: "assign", name: clean }),
        });
        const photoId = state.currentPreviewId;
        if (photoId) {
          invalidatePhotoDetail(photoId);
          await ensurePhotoDetail(photoId);
        }
        showToast(`已标注为「${clean}」。`, "success");
      } catch (e) {
        showToast("标注失败：" + e.message, "error");
      }
    }
    return;
  }

  const openBtn = event.target.closest(".path-open-btn");
  if (openBtn) {
    event.stopPropagation();
    const path = openBtn.dataset.openPath || "";
    const photoId = Number(openBtn.dataset.photoId);
    if (!path) return;
    openBtn.classList.add("busy");
    try {
      await api("/api/open-file", {
        method: "POST",
        body: JSON.stringify({ path, photo_id: Number.isFinite(photoId) ? photoId : undefined }),
      });
      showToast("已交给系统默认程序打开。", "success");
    } catch (e) {
      showToast("无法打开文件：" + e.message, "error");
    } finally {
      openBtn.classList.remove("busy");
    }
    return;
  }

  const link = event.target.closest(".path-link");
  if (!link) return;
  event.stopPropagation();
  const path = link.dataset.path || "";
  if (!path) return;
  const copied = await copyText(path);
  link.classList.toggle("copied", copied);
  setTimeout(() => link.classList.remove("copied"), 1200);
  if (copied) showToast("路径已复制到剪贴板。", "success");
  else showToast("复制失败，请手动选择路径文本。", "error");
}

/**
 * Fetch the handful of settings the playback path needs.
 *
 * The settings dialog loads the whole config, but that only happens when it is
 * opened - a preview must already know its mode and volume on the first click,
 * so this runs once at startup and stays quiet if it fails.
 */
async function loadPlaybackConfig() {
  try {
    const cfg = await api("/api/config");
    state.videoClipSeconds = Number(cfg.video_clip_seconds) || 3;
    state.videoPreviewMode = cfg.video_preview_mode || "once";
    state.videoPreviewVolume = Number(cfg.video_preview_volume ?? 50);
    state.liveVolume = Number(cfg.live_volume ?? 0);
    state.faceRecognitionEnabled = cfg.face_recognition_enabled !== false;
    state.faceAutoScan = !!cfg.face_auto_scan;
    state.geocodingProvider = cfg.geocoding_provider || "nominatim";
    state.footprintDefaultView = cfg.footprint_default_view || "grid";
    state.mapTileProvider = cfg.map_tile_provider || "carto";
    state.mapTileUrl = cfg.map_tile_url || "";
    state.mapApiKey = cfg.map_api_key || "";
  } catch (e) {
    // Defaults are sensible, so a failure here changes nothing visible.
    console.warn("读取播放设置失败", e);
  }
  try {
    // A full redo has to warn before it starts, which means the answer has to be
    // known before the settings dialog is ever opened.
    const status = await api("/api/video/status");
    state.ffmpegAvailable = !!status.available;
  } catch (e) {
    // Unknown is treated as available: refusing to scan because a status call
    // failed would be worse than the warning appearing late.
    state.ffmpegAvailable = true;
  }
}

