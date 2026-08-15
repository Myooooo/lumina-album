/* Local Photo Review System - frontend logic */
"use strict";

const state = {
  folder: localStorage.getItem("photoFolder") || "",
  folderHistory: (() => {
    try {
      const saved = JSON.parse(localStorage.getItem("photoFolderHistory") || "[]");
      return Array.isArray(saved) ? saved.filter((x) => typeof x === "string") : [];
    } catch (e) {
      return [];
    }
  })(),
  photos: [],
  selected: new Set(),
  currentDirPath: "",
  currentPreviewId: null,
  previewNavToken: 0,
  pollingTimer: null,
  pollTick: 0,
  photoSignature: "",
  currentJobId: null,
};

const ICONS = window.LuminaIcons;

const $ = (id) => document.getElementById(id);
const { enhanceSelect, setSelectValue, showToast, showLoading, hideLoading, confirmDialog } = window.LuminaUI;

async function api(url, options = {}) {
  const resp = await fetch(url, {
    ...options,
    headers: options.body
      ? { "Content-Type": "application/json", ...(options.headers || {}) }
      : options.headers,
  });
  if (!resp.ok) {
    let detail = "";
    try {
      const data = await resp.json();
      detail = data.error || JSON.stringify(data);
    } catch (e) {
      detail = resp.statusText;
    }
    throw new Error(detail || `HTTP ${resp.status}`);
  }
  return resp.json();
}

function folderPathKey(path) {
  return String(path || "").replace(/[\/]+$/, "").toLowerCase();
}

function rememberFolder(folder) {
  const clean = String(folder || "").trim();
  if (!clean) return;
  state.folderHistory = [
    clean,
    ...state.folderHistory.filter((item) => folderPathKey(item) !== folderPathKey(clean)),
  ].slice(0, 20);
  try {
    localStorage.setItem("photoFolderHistory", JSON.stringify(state.folderHistory));
  } catch (e) {
    // localStorage may be unavailable; memory history still works.
  }
}

async function selectBestFolder() {
  let folders = [];
  try {
    const data = await api("/api/folders");
    folders = data.folders || [];
  } catch (e) {
    folders = [];
  }
  const candidates = [state.folder, ...state.folderHistory].filter(Boolean);
  const chosen =
    candidates.find((candidate) =>
      folders.some((folder) => folderPathKey(folder) === folderPathKey(candidate))
    ) ||
    folders[0] ||
    "";
  if (chosen) rememberFolder(chosen);
  return chosen;
}

async function loadInitialFolder() {
  state.folder = await selectBestFolder();
  localStorage.setItem("photoFolder", state.folder);
  $("folderInput").value = state.folder;
  if (state.folder) {
    await loadPhotos();
  } else {
    state.photos = [];
    renderGallery();
    updateSelectionBar();
    loadStats();
    loadTags();
    loadYears();
  }
}

function updateActiveStat() {
  const current = $("statusFilter").value;
  document.querySelectorAll(".clickable-stat").forEach((el) => {
    el.classList.toggle("active", el.dataset.filter === current);
  });
  updateTrashActions();
}

function updateTrashActions() {
  $("trashActions").classList.toggle("hidden", $("statusFilter").value !== "deleted");
}

function applyStatFilter(filter) {
  setSelectValue($("statusFilter"), filter);
  updateActiveStat();
  loadPhotos();
}

const FILTER_DEFAULTS = {
  min_score: "",
  sort: "score_desc",
  tag: "",
  year: "",
  search_mode: "smart",
  search: "",
};

function currentFilterValue(key) {
  switch (key) {
    case "min_score": return $("minScoreFilter").value;
    case "sort": return $("sortFilter").value;
    case "tag": return $("tagFilter").value;
    case "year": return $("yearFilter").value;
    case "search_mode": return $("searchMode").value;
    case "search": return $("searchInput").value.trim();
    default: return "";
  }
}

function updateFilterHighlights() {
  document.querySelectorAll("[data-filter-key]").forEach((group) => {
    const key = group.dataset.filterKey;
    const value = currentFilterValue(key);
    group.classList.toggle("active-filter", value !== FILTER_DEFAULTS[key]);
  });
}

function fmtSize(bytes) {
  if (!bytes && bytes !== 0) return "-";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / 1024 / 1024).toFixed(1) + " MB";
}

function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function scoreText(score) {
  return score === null || score === undefined ? "-" : Number(score).toFixed(1);
}

const DIMENSION_NAMES = {
  technical: "技术",
  composition: "构图",
  memory: "回忆",
  uniqueness: "独特",
};
const DIMENSION_COLORS = {
  technical: "#5b8dd9",
  composition: "#d9a05b",
  memory: "#d96b7b",
  uniqueness: "#6f9e7a",
};

function dimensionRows(dims) {
  const d = dims || {};
  return Object.keys(DIMENSION_NAMES).map((key) => {
    const val = d[key] ?? 0;
    const pct = Math.max(0, Math.min(100, Number(val) * 10));
    const color = DIMENSION_COLORS[key] || "#b3815a";
    return `
      <div class="dimension-row">
        <span class="dimension-name">${DIMENSION_NAMES[key]}</span>
        <span class="dimension-bar"><span class="dimension-fill" style="width:${pct}%;background:${color}"></span></span>
        <span class="dimension-value">${Number(val).toFixed(1)}</span>
      </div>
    `;
  }).join("");
}

function dimensionValues(dims) {
  const d = dims || {};
  return Object.keys(DIMENSION_NAMES).map((key) => {
    const val = d[key] ?? 0;
    const color = DIMENSION_COLORS[key] || "#b3815a";
    return `<span class="score-pill" style="color:${color};background:${color}1a">${DIMENSION_NAMES[key]} ${Number(val).toFixed(1)}</span>`;
  }).join("");
}

async function loadStats() {
  try {
    const folder = state.folder;
    const q = folder ? `?folder=${encodeURIComponent(folder)}` : "";
    const stats = await api(`/api/stats${q}`);
    $("statTotal").textContent = stats.total || 0;
    $("statAnalyzed").textContent = stats.analyzed || 0;
    $("statFavorite").textContent = stats.favorite || 0;
    $("statPending").textContent = stats.pending || 0;
    $("statDeleted").textContent = stats.deleted || 0;
    updateActiveStat();
  } catch (e) {
    console.warn("loadStats failed", e);
  }
}

function getFilterParams() {
  const params = new URLSearchParams();
  if (state.folder) params.set("folder", state.folder);
  const status = $("statusFilter").value;
  if (status !== "all") params.set("status", status);
  const min = $("minScoreFilter").value;
  if (min !== "") params.set("min_score", min);
  const tag = $("tagFilter").value.trim();
  if (tag) params.set("tag", tag);
  const fav = $("favoriteFilter").value;
  if (fav) params.set("favorite", fav);
  const year = $("yearFilter").value;
  if (year) params.set("year", year);
  const sort = $("sortFilter").value;
  params.set("sort", sort);
  const search = $("searchInput").value.trim();
  if (search) params.set("search", search);
  return params;
}

function renderSkeleton() {
  const gallery = $("gallery");
  $("emptyState").classList.add("hidden");
  gallery.innerHTML = Array.from({ length: 8 }).map((_, idx) => `
    <div class="polaroid-card skeleton-card" style="animation-delay:${(idx % 6) * 0.05}s">
      <div class="polaroid-inner">
        <div class="skeleton-photo"></div>
        <div class="skeleton-line"></div>
        <div class="skeleton-line short"></div>
      </div>
    </div>
  `).join("");
}

async function loadPhotos(options = {}) {
  const quiet = !!options.quiet;
  if (!quiet) updateFilterHighlights();
  const shouldSkeleton = !quiet && !state.photos.length;
  if (shouldSkeleton) renderSkeleton();

  const params = getFilterParams();
  const mode = $("searchMode").value;
  const query = $("searchInput").value.trim();
  const scrollY = quiet ? window.scrollY : 0;
  let data;
  try {
    if ((mode === "semantic" || mode === "smart") && query) {
      const statusVal = $("statusFilter").value;
      const body = {
        folder: state.folder || undefined,
        query,
        mode,
        status: statusVal === "all" ? undefined : statusVal,
        min_score: $("minScoreFilter").value || undefined,
        tag: $("tagFilter").value || undefined,
        favorite: $("favoriteFilter").value || undefined,
        year: $("yearFilter").value || undefined,
        sort: $("sortFilter").value,
      };
      if (!quiet) showLoading("正在用大模型寻找相关回忆…");
      try {
        data = await api("/api/search", {
          method: "POST",
          body: JSON.stringify(body),
        });
      } finally {
        if (!quiet) hideLoading();
      }
    } else {
      data = await api(`/api/photos?${params.toString()}`);
    }

    if (data.warning && !quiet) showToast(data.warning, "warning");
    const nextPhotos = data.photos || [];
    const nextSignature = nextPhotos
      .map((p) => `${p.id}:${p.status}:${p.score}:${p.favorite ? 1 : 0}`)
      .join("|");
    if (quiet && nextSignature === state.photoSignature) {
      state.photos = nextPhotos;
      return;
    }
    state.photoSignature = nextSignature;
    state.photos = nextPhotos;
    const ids = new Set(state.photos.map((p) => p.id));
    for (const id of [...state.selected]) {
      if (!ids.has(id)) state.selected.delete(id);
    }
    renderGallery({ animate: !quiet });
    if (quiet) window.scrollTo({ top: scrollY, left: 0, behavior: "instant" });
    updateSelectionBar();
    if (!quiet) {
      loadStats();
      loadTags();
      loadYears();
    }
  } catch (e) {
    if (quiet) {
      console.warn("后台刷新照片失败", e);
      return;
    }
    if (shouldSkeleton) {
      state.photos = [];
      renderGallery();
    }
    showToast("读取照片失败：" + e.message, "error");
  }
}

async function loadTags() {
  try {
    const q = state.folder ? `?folder=${encodeURIComponent(state.folder)}` : "";
    const data = await api(`/api/tags${q}`);
    const select = $("tagFilter");
    const current = select.value;
    select.innerHTML = `<option value="">全部标签</option>` + (data.tags || []).map((t) => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join("");
    if (current && (data.tags || []).includes(current)) select.value = current;
    if (select.dataset.enhanced) {
      const wrapper = select.parentElement;
      const label = wrapper.querySelector(".custom-select-label");
      const opt = select.options[select.selectedIndex];
      if (label && opt) label.textContent = opt.text;
    }
  } catch (e) {
    console.warn("loadTags failed", e);
  }
}

async function loadYears() {
  try {
    const q = state.folder ? `?folder=${encodeURIComponent(state.folder)}` : "";
    const data = await api(`/api/years${q}`);
    const select = $("yearFilter");
    const current = select.value;
    const years = (data.years || []).map(String);
    select.innerHTML = `<option value="">全部年份</option>` + years.map((y) => `<option value="${escapeHtml(y)}">${escapeHtml(y)} 年</option>`).join("");
    setSelectValue(select, years.includes(current) ? current : "");
  } catch (e) {
    console.warn("loadYears failed", e);
  }
}

function renderGallery(options = {}) {
  const gallery = $("gallery");
  const empty = $("emptyState");
  gallery.innerHTML = "";
  if (!state.photos.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");

  state.photos.forEach((photo, idx) => {
    const card = document.createElement("div");
    card.className = "polaroid-card";
    if (options.animate === false) {
      card.style.animation = "none";
    } else {
      card.style.animationDelay = `${(idx % 16) * 0.025}s`;
    }
    card.dataset.id = photo.id;

    const isDeleted = photo.status === "deleted";
    const dims = photo.dimensions || {};
    const tagsHtml = (photo.tags || []).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("");

    let deleteActions = "";
    if (isDeleted) {
      deleteActions = `
        <button class="btn small restore-btn" title="恢复">${ICONS.restore}</button>
        <button class="btn small danger permanent-btn" title="彻底移除">${ICONS.close}</button>
      `;
    } else {
      deleteActions = `
        <button class="btn small danger delete-btn" title="暂时收起">${ICONS.trash}</button>
      `;
    }

    card.innerHTML = `
      <div class="polaroid-inner">
        <div class="polaroid-front">
          <div class="tape"></div>
          <button class="favorite-btn ${photo.favorite ? "active" : ""}" title="${photo.favorite ? "取消珍藏" : "珍藏"}">${photo.favorite ? ICONS.heartFilled : ICONS.heart}</button>
          <img class="polaroid-photo" src="/api/thumbnail/${photo.id}" alt="${escapeHtml(photo.filename)}" loading="lazy" />
          <div class="polaroid-body">
            ${photo.location ? `<div class="photo-location">${ICONS.pin} ${escapeHtml(photo.location)}</div>` : ""}
            ${photo.reason ? `<div class="photo-comment">“${escapeHtml(photo.reason)}”</div>` : ""}
            <div class="back-tags">${tagsHtml || ""}</div>
            <div class="score-pills">
              <span class="score-pill total">总分 ${scoreText(photo.score)}</span>
              ${dimensionValues(dims)}
            </div>
            <div class="card-actions">
              <label class="card-check" title="选择">
                <input type="checkbox" />
              </label>
              <span class="card-btn-group">
                <button class="btn small reanalyze-btn" title="重新解读">${ICONS.refresh}</button>
                ${deleteActions}
              </span>
            </div>
          </div>
        </div>
      </div>
    `;
    gallery.appendChild(card);

    const photoImg = card.querySelector(".polaroid-photo");
    photoImg.addEventListener("load", () => photoImg.classList.add("is-loaded"));
    photoImg.addEventListener("error", () => photoImg.classList.add("is-loaded"));

    card.addEventListener("click", (e) => {
      if (e.target.closest("button") || e.target.closest("input") || e.target.closest("a")) return;
      openPreview(photo.id);
    });

    const favBtn = card.querySelector(".favorite-btn");
    favBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const newVal = !photo.favorite;
      try {
        await api("/api/favorite", {
          method: "POST",
          body: JSON.stringify({ id: photo.id, favorite: newVal }),
        });
        photo.favorite = newVal;
        favBtn.classList.toggle("active", newVal);
        favBtn.title = newVal ? "取消珍藏" : "珍藏";
        favBtn.innerHTML = newVal ? ICONS.heartFilled : ICONS.heart;
        loadStats();
        if ($("favoriteFilter").value === "1" && !newVal) {
          await loadPhotos();
        }
      } catch (err) {
        showToast("珍藏操作失败：" + err.message, "error");
      }
    });

    const checkbox = card.querySelector(".card-check input");
    checkbox.checked = state.selected.has(photo.id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selected.add(photo.id);
      else state.selected.delete(photo.id);
      updateSelectionBar();
    });

    card.querySelector(".reanalyze-btn").addEventListener("click", async (e) => {
      e.stopPropagation();
      await reanalyze(photo.id);
    });

    if (isDeleted) {
      card.querySelector(".restore-btn").addEventListener("click", async (e) => {
        e.stopPropagation();
        await restorePhotos([photo.id]);
      });
      card.querySelector(".permanent-btn").addEventListener("click", async (e) => {
        e.stopPropagation();
        await permanentDeletePhotos([photo.id]);
      });
    } else {
      card.querySelector(".delete-btn").addEventListener("click", async (e) => {
        e.stopPropagation();
        if (await confirmDialog(`确定把“${photo.filename}”暂时收起吗？`, { title: "暂时收起", confirmText: "暂时收起", danger: true })) {
          await deletePhotos([photo.id]);
        }
      });
    }
  });
}

function updateCardFavorite(id) {
  const photo = state.photos.find((p) => p.id === id);
  const card = document.querySelector(`.polaroid-card[data-id="${id}"]`);
  if (!photo || !card) return;
  const btn = card.querySelector(".favorite-btn");
  btn.classList.toggle("active", !!photo.favorite);
  btn.title = photo.favorite ? "取消珍藏" : "珍藏";
  btn.innerHTML = photo.favorite ? ICONS.heartFilled : ICONS.heart;
}

function updateSelectionBar() {
  const bar = $("selectionBar");
  const count = state.selected.size;
  $("selectedCount").textContent = count;
  const hasDeleted = state.photos.some((p) => state.selected.has(p.id) && p.status === "deleted");
  const hasActive = state.photos.some((p) => state.selected.has(p.id) && p.status !== "deleted");
  $("restoreSelectedBtn").classList.toggle("hidden", !hasDeleted);
  $("permanentDeleteSelectedBtn").classList.toggle("hidden", !hasDeleted);
  $("deleteSelectedBtn").classList.toggle("hidden", !hasActive);
  $("reanalyzeSelectedBtn").classList.toggle("hidden", !hasActive);
  if (count > 0) bar.classList.remove("hidden");
  else bar.classList.add("hidden");
}

async function startScan() {
  const folder = $("folderInput").value.trim();
  if (!folder) {
    showToast("先告诉我们照片文件夹在哪里吧", "error");
    return;
  }
  state.folder = folder;
  rememberFolder(folder);
  localStorage.setItem("photoFolder", folder);
  const force = $("forceScan").checked;
  try {
    const data = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({ folder, force }),
    });
    state.currentJobId = data.job_id;
    showProgress("开始整理…");
    pollScan();
  } catch (e) {
    showToast("开始整理失败：" + e.message, "error");
  }
}

async function startRebuildIndex() {
  const folder = $("folderInput").value.trim() || state.folder;
  if (!folder) {
    showToast("先选择一份回忆文件夹吧", "error");
    return;
  }
  state.folder = folder;
  rememberFolder(folder);
  localStorage.setItem("photoFolder", folder);
  try {
    const data = await api("/api/rebuild-index", {
      method: "POST",
      body: JSON.stringify({ folder }),
    });
    state.currentJobId = data.job_id;
    showProgress("正在同步相册…");
    pollScan();
  } catch (e) {
    showToast("同步相册失败：" + e.message, "error");
  }
}

function showProgress(text) {
  $("scanProgress").classList.remove("hidden");
  $("progressText").textContent = text;
  $("progressFill").style.width = "0%";
  state.pollTick = 0;
}

function hideProgress() {
  $("scanProgress").classList.add("hidden");
  state.currentJobId = null;
  if (state.pollingTimer) {
    clearTimeout(state.pollingTimer);
    state.pollingTimer = null;
  }
}

function pollScan() {
  if (!state.currentJobId) return;
  api(`/api/scan/status/${state.currentJobId}`)
    .then((job) => {
      if (job.status === "completed" || job.status === "cancelled" || job.status === "error") {
        const pct = job.total ? Math.round((job.processed / job.total) * 100) : 100;
        $("progressFill").style.width = pct + "%";
        $("progressText").textContent =
          job.status === "completed"
            ? `整理完成：${job.processed}/${job.total}`
            : job.status === "cancelled"
            ? "整理已取消"
            : "整理出错：" + (job.error || "");
        setTimeout(hideProgress, 1500);
        state.currentJobId = null;
        loadPhotos();
        return;
      }
      const pct = job.total ? Math.round((job.processed / job.total) * 100) : 0;
      $("progressFill").style.width = pct + "%";
      const phaseText = job.phase === "analyze" ? "正在聆听" : "整理照片";
      $("progressText").textContent = `${phaseText} ${job.processed}/${job.total}：${job.current || ""}`;
      state.pollTick += 1;
      loadStats();
      if (state.pollTick % 4 === 0 && !$("searchInput").value.trim()) {
        loadPhotos({ quiet: true });
      }
      state.pollingTimer = setTimeout(pollScan, 1000);
    })
    .catch((e) => {
      $("progressText").textContent = "读取进度失败：" + e.message;
      setTimeout(hideProgress, 2000);
    });
}

async function deletePhotos(ids) {
  ids = ids.filter((id) => {
    const p = state.photos.find((x) => x.id === id);
    return p && p.status !== "deleted";
  });
  if (!ids.length) return;
  if (!await confirmDialog(`确定把选中的 ${ids.length} 张照片暂时收起吗？`, { title: "暂时收起", confirmText: "暂时收起", danger: true })) return;
  try {
    const data = await api("/api/delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    if (data.errors && data.errors.length) {
      showToast("部分照片暂时收起失败：" + data.errors.map((e) => e.error).join("; "), "error");
    }
    state.selected.clear();
    await loadPhotos();
    if (state.currentPreviewId) {
      const p = state.photos.find((x) => x.id === state.currentPreviewId);
      if (!p) closeModal("previewModal");
      else updatePreview();
    }
  } catch (e) {
    showToast("暂时收起失败：" + e.message, "error");
  }
}

async function permanentDeletePhotos(ids) {
  ids = ids.filter((id) => {
    const p = state.photos.find((x) => x.id === id);
    return p && p.status === "deleted";
  });
  if (!ids.length) return;
  if (!await confirmDialog(`确定彻底移除 ${ids.length} 张照片？\n文件将从磁盘移除，且不可恢复。`, { title: "彻底移除", confirmText: "彻底移除", danger: true })) return;
  try {
    const data = await api("/api/delete/permanent", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    if (data.errors && data.errors.length) {
      showToast("部分照片彻底移除失败：" + data.errors.map((e) => e.error).join("; "), "error");
    }
    state.selected.clear();
    await loadPhotos();
    if (state.currentPreviewId) {
      const p = state.photos.find((x) => x.id === state.currentPreviewId);
      if (!p) closeModal("previewModal");
      else updatePreview();
    }
  } catch (e) {
    showToast("彻底移除失败：" + e.message, "error");
  }
}

async function restorePhotos(ids) {
  ids = ids.filter((id) => {
    const p = state.photos.find((x) => x.id === id);
    return p && p.status === "deleted";
  });
  if (!ids.length) return;
  try {
    const data = await api("/api/restore", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    if (data.errors && data.errors.length) {
      showToast("部分照片恢复失败：" + data.errors.map((e) => e.error).join("; "), "error");
    }
    state.selected.clear();
    await loadPhotos();
    if (state.currentPreviewId) {
      const p = state.photos.find((x) => x.id === state.currentPreviewId);
      if (!p) closeModal("previewModal");
      else updatePreview();
    }
  } catch (e) {
    showToast("恢复失败：" + e.message, "error");
  }
}

async function reanalyze(id) {
  showLoading("重新欣赏这张照片…");
  try {
    await api("/api/reanalyze", {
      method: "POST",
      body: JSON.stringify({ id }),
    });
    await loadPhotos();
    if (state.currentPreviewId === id) updatePreview();
  } catch (e) {
    showToast("重新解读失败：" + e.message, "error");
  } finally {
    hideLoading();
  }
}

function preloadImage(url) {
  return new Promise((resolve) => {
    const probe = new Image();
    let settled = false;
    let timer = 0;
    const done = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };
    probe.onload = done;
    probe.onerror = done;
    probe.src = url;
    timer = setTimeout(done, 10000);
  });
}

function visiblePhotoOrder() {
  return [...document.querySelectorAll(".polaroid-card")]
    .map((card) => Number(card.dataset.id))
    .filter((id) => Number.isFinite(id));
}

async function navigatePreview(delta) {
  // Use the currently rendered gallery order so prev/next always follows the
  // active filters and sort, even if the list was refreshed behind the modal.
  const order = visiblePhotoOrder();
  if (!order.length) return;
  const idx = order.indexOf(state.currentPreviewId);
  if (idx < 0) return;
  const nextId = order[(idx + delta + order.length) % order.length];
  const token = ++state.previewNavToken;
  const url = `/api/original/${nextId}`;
  await preloadImage(url);
  if (token !== state.previewNavToken) return;
  state.currentPreviewId = nextId;
  const img = $("previewImage");
  const animClass = delta < 0 ? "preview-switch-left" : "preview-switch-right";
  img.classList.remove("preview-switch-left", "preview-switch-right");
  img.src = url;
  void img.offsetWidth;
  img.classList.add(animClass);
  updatePreview({ skipImage: true });
}

async function toggleFolderHistory() {
  const panel = $("folderHistory");
  if (!panel.classList.contains("hidden")) {
    panel.classList.add("hidden");
    return;
  }
  try {
    const data = await api("/api/folders");
    if (!data.folders || !data.folders.length) {
      panel.innerHTML = `<div class="folder-history-empty">暂无整理过的文件夹</div>`;
    } else {
      panel.innerHTML = data.folders.map((f) => `<div class="folder-history-item" data-path="${escapeHtml(f)}">${escapeHtml(f)}</div>`).join("");
      panel.querySelectorAll(".folder-history-item").forEach((el) => {
        el.addEventListener("click", () => {
          state.folder = el.dataset.path;
          rememberFolder(state.folder);
          localStorage.setItem("photoFolder", state.folder);
          $("folderInput").value = state.folder;
          panel.classList.add("hidden");
          loadPhotos();
        });
      });
    }
    panel.classList.remove("hidden");
  } catch (e) {
    showToast("读取文件夹列表失败：" + e.message, "error");
  }
}

async function openFolder() {
  const folder = $("folderInput").value.trim() || state.folder;
  if (!folder) {
    showToast("先选择一份回忆文件夹吧", "error");
    return;
  }
  try {
    await api("/api/open-folder", {
      method: "POST",
      body: JSON.stringify({ path: folder }),
    });
  } catch (e) {
    showToast("无法打开文件夹：" + e.message, "error");
  }
}

async function reanalyzeSelected(ids) {
  ids = ids.filter((id) => {
    const p = state.photos.find((x) => x.id === id);
    return p && p.status !== "deleted";
  });
  if (!ids.length) return;
  if (!await confirmDialog(`确定重新解读选中的 ${ids.length} 张照片吗？`, { title: "重新解读", confirmText: "重新解读" })) return;
  showLoading(`正在重新解读选中的 ${ids.length} 张照片…`);
  let ok = 0;
  let fail = 0;
  try {
    for (let i = 0; i < ids.length; i++) {
      $("loadingText").textContent = `正在重新解读照片 ${i + 1}/${ids.length}…`;
      try {
        await api("/api/reanalyze", { method: "POST", body: JSON.stringify({ id: ids[i] }) });
        ok++;
      } catch (e) {
        fail++;
      }
    }
    state.selected.clear();
    await loadPhotos();
    showToast(`重新解读完成：成功 ${ok} 张，失败 ${fail} 张`, fail ? "error" : "success");
  } finally {
    hideLoading();
  }
}

function openPreview(id) {
  state.previewNavToken += 1;
  state.currentPreviewId = id;
  $("previewModal").classList.remove("hidden");
  updatePreview();
}

function closeModal(id) {
  $(id).classList.add("hidden");
  if (id === "previewModal") {
    state.currentPreviewId = null;
    state.previewNavToken += 1;
    $("previewImage").classList.remove("preview-switch-left", "preview-switch-right");
  }
}

function formatCaptureTime(value) {
  if (value === null || value === undefined || value === "") return "";
  const text = String(value).trim().replace(/\x00/g, "").trim();
  if (!text) return "";
  const m = text.match(/^(\d{4})[-\/:](\d{1,2})[-\/:](\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?/);
  if (!m) return text;
  const [, y, mo, d, hh, mm, ss] = m;
  if (!hh) return `${y}年${mo}月${d}日`;
  return `${y}年${mo}月${d}日 ${hh}:${mm}${ss ? ":" + ss : ""}`;
}

function parseExifNumber(value) {
  const text = String(value ?? "").trim();
  if (!text) return Number.NaN;
  const tuple = text.match(/^\(?\s*([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)\s*\)?$/);
  if (tuple) {
    const num = Number(tuple[1]);
    const den = Number(tuple[2]);
    return den ? num / den : Number.NaN;
  }
  const fraction = text.match(/^([+-]?\d+(?:\.\d+)?)\s*\/\s*([+-]?\d+(?:\.\d+)?)$/);
  if (fraction) {
    const num = Number(fraction[1]);
    const den = Number(fraction[2]);
    return den ? num / den : Number.NaN;
  }
  const numeric = Number(text);
  return Number.isFinite(numeric) ? numeric : Number.NaN;
}

function formatFnumber(value) {
  const n = parseExifNumber(value);
  if (Number.isFinite(n)) return n.toFixed(1).replace(/\.0$/, "");
  return String(value).trim();
}

function formatExposure(value) {
  const text = String(value ?? "").trim();
  if (!text) return "";
  const n = parseExifNumber(text);
  if (Number.isFinite(n)) return text.toLowerCase().endsWith("s") ? text : `${n}s`;
  return text.toLowerCase().endsWith("s") ? text : `${text}s`;
}

function formatIso(value) {
  const n = parseExifNumber(value);
  if (Number.isFinite(n)) return String(Math.round(n));
  return String(value ?? "").trim();
}

function formatFocalLength(value) {
  const text = String(value ?? "").trim().replace(/mm$/i, "");
  const n = parseExifNumber(text);
  if (Number.isFinite(n)) return `${Number(n.toFixed(2))}mm`;
  return text ? `${text}mm` : "";
}

function updatePreview(options = {}) {
  const id = state.currentPreviewId;
  const photo = state.photos.find((p) => p.id === id);
  if (!photo) return;
  const hasNav = state.photos.length > 1;
  $("previewPrevBtn").classList.toggle("hidden", !hasNav);
  $("previewNextBtn").classList.toggle("hidden", !hasNav);
  const img = $("previewImage");
  if (!options.skipImage) {
    img.classList.remove("preview-switch-left", "preview-switch-right");
    img.src = `/api/original/${id}`;
  }
  const isDeleted = photo.status === "deleted";
  $("previewDeleteBtn").classList.toggle("hidden", isDeleted);
  $("previewRestoreBtn").classList.toggle("hidden", !isDeleted);
  $("previewPermanentDeleteBtn").classList.toggle("hidden", !isDeleted);
  const favBtn = $("previewFavoriteBtn");
  favBtn.classList.toggle("active", !!photo.favorite);
  favBtn.innerHTML = photo.favorite ? ICONS.heartFilled : ICONS.heart;
  favBtn.title = photo.favorite ? "取消珍藏" : "珍藏";
  const exif = photo.exif || {};
  const cameraText = [exif.make, exif.model].filter(Boolean).join(" ");
  const shotParts = [];
  if (exif.fnumber) shotParts.push(`f/${formatFnumber(exif.fnumber)}`);
  if (exif.exposure) shotParts.push(formatExposure(exif.exposure));
  if (exif.iso) shotParts.push(`ISO ${formatIso(exif.iso)}`);
  if (exif.focal_length) shotParts.push(formatFocalLength(exif.focal_length));
  const cameraHtml = (cameraText || shotParts.length) ? `
    ${cameraText ? `<div class="camera-info">${ICONS.camera} ${escapeHtml(cameraText)}</div>` : ""}
    ${shotParts.length ? `<div class="shot-info">${shotParts.map((p) => `<span class="tag">${escapeHtml(p)}</span>`).join("")}</div>` : ""}
  ` : "";
  const captureTime = formatCaptureTime(exif.datetime_original);
  $("previewInfo").innerHTML = `
    <h3>${escapeHtml(photo.title || photo.filename)}</h3>
    ${cameraHtml}
    ${captureTime ? `<p>${ICONS.clock} ${escapeHtml(captureTime)}</p>` : ""}
    ${photo.location ? `<p>${ICONS.pin} ${escapeHtml(photo.location)}</p>` : ""}
    <p><strong>路径：</strong>${escapeHtml(photo.path || "")}</p>
    <p><strong>尺寸：</strong>${photo.width ? photo.width + " × " + photo.height : "-"}，
       <strong>大小：</strong>${fmtSize(photo.size)}</p>
    <p><strong>评分：</strong><span class="badge score">${scoreText(photo.score)}</span></p>
    <div class="dimension-list" style="margin-top:8px">${dimensionRows(photo.dimensions || {})}</div>
    <p><strong>标签：</strong>${(photo.tags || []).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join(" ") || "-"}</p>
    <p><strong>简评：</strong>${escapeHtml(photo.reason || "")}</p>
    ${photo.error ? `<p><strong>错误：</strong>${escapeHtml(photo.error)}</p>` : ""}
  `;
}

function resetFilters() {
  setSelectValue($("statusFilter"), "analyzed");
  setSelectValue($("favoriteFilter"), "");
  setSelectValue($("minScoreFilter"), "");
  setSelectValue($("tagFilter"), "");
  setSelectValue($("yearFilter"), "");
  setSelectValue($("searchMode"), "smart");
  setSelectValue($("sortFilter"), "score_desc");
  $("searchInput").value = "";
  updateActiveStat();
  loadPhotos();
}

async function openSettings() {
  try {
    const cfg = await api("/api/config");
    $("setApiBase").value = cfg.api_base_url || "";
    $("setApiKey").value = cfg.api_key || "";
    $("setModel").value = cfg.model || "";
    $("setSystemPrompt").value = cfg.system_prompt || "";
    $("setMaxEdge").value = cfg.proxy_max_edge || 1024;
    $("setTimeout").value = cfg.request_timeout || 120;
    $("setIndexThreads").value = cfg.index_concurrency || 4;
    $("setGeocodingProvider").value = cfg.geocoding_provider || "nominatim";
    $("setGeocodingApiKey").value = cfg.geocoding_api_key || "";
    $("setGeocodingInterval").value = cfg.geocoding_interval || 1.0;
    $("settingsModal").classList.remove("hidden");
  } catch (e) {
    showToast("读取设置失败：" + e.message, "error");
  }
}

async function saveSettings() {
  const payload = {
    api_base_url: $("setApiBase").value.trim(),
    api_key: $("setApiKey").value,
    model: $("setModel").value.trim(),
    system_prompt: $("setSystemPrompt").value,
    proxy_max_edge: parseInt($("setMaxEdge").value, 10) || 1024,
    request_timeout: parseInt($("setTimeout").value, 10) || 120,
    index_concurrency: parseInt($("setIndexThreads").value, 10) || 4,
    geocoding_provider: $("setGeocodingProvider").value,
    geocoding_api_key: $("setGeocodingApiKey").value,
    geocoding_interval: parseFloat($("setGeocodingInterval").value) || 1.0,
  };
  try {
    await api("/api/config", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    closeModal("settingsModal");
    showToast("设置已保存，重启后仍会保留。", "success");
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

function openEditPreview() {
  const photo = state.photos.find((p) => p.id === state.currentPreviewId);
  if (!photo) return;
  const dims = photo.dimensions || {};
  $("editTitle").value = photo.title || "";
  $("editScore").value = scoreText(photo.score);
  $("editTechnical").value = dims.technical ?? 0;
  $("editComposition").value = dims.composition ?? 0;
  $("editMemory").value = dims.memory ?? 0;
  $("editUniqueness").value = dims.uniqueness ?? 0;
  $("editLocation").value = photo.location || "";
  $("editTags").value = (photo.tags || []).join("，");
  $("editReason").value = photo.reason || "";
  $("editModal").classList.remove("hidden");
}

async function saveEditedPhoto() {
  const id = state.currentPreviewId;
  const photo = state.photos.find((p) => p.id === id);
  if (!photo) return;
  const score = parseFloat($("editScore").value);
  if (Number.isNaN(score) || score < 0 || score > 10) {
    showToast("总分需要在 0 到 10 之间", "error");
    return;
  }
  const dimensions = {};
  for (const key of ["technical", "composition", "memory", "uniqueness"]) {
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
    await api(`/api/photo/${id}/edit`, {
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
    await loadPhotos();
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
    "确定从拾光相册中移除当前目录吗？" + String.fromCharCode(10) + 
    "只会清除数据库记录和该目录的代理缓存，不会改动任何照片原图。",
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
    state.photos = [];
    state.selected.clear();
    state.photoSignature = "";
    localStorage.removeItem("photoFolder");
    state.folder = await selectBestFolder();
    localStorage.setItem("photoFolder", state.folder);
    $("folderInput").value = state.folder;
    await loadPhotos();
    updateTrashActions();
    const fallback = state.folder
      ? `已自动回到上一个有效目录：${state.folder}`
      : "当前没有其他已索引目录，已留空。";
    showToast(`已移除 ${data.removed || 0} 条照片记录和缓存，原图未做任何改动。${fallback}`, "success");
  } catch (e) {
    showToast("移除当前目录失败：" + e.message, "error");
  }
}

async function emptyTrash() {
  const scope = state.folder ? "当前目录" : "全部目录";
  const ok = await confirmDialog(
    `确定清空${scope}的回收站吗？` + String.fromCharCode(10) + 
    "回收站中的照片原文件将被彻底删除，此操作无法恢复。",
    { title: "清空回收站", confirmText: "彻底清空", danger: true }
  );
  if (!ok) return;
  try {
    const data = await api("/api/trash/empty", {
      method: "POST",
      body: JSON.stringify({ folder: state.folder || undefined }),
    });
    state.selected.clear();
    await loadPhotos();
    if (state.currentPreviewId) {
      const still = state.photos.some((p) => p.id === state.currentPreviewId);
      if (!still) closeModal("previewModal");
      else updatePreview({ skipImage: true });
    }
    showToast(`回收站已清空，共移除 ${data.deleted || 0} 张照片。`, "success");
  } catch (e) {
    showToast("清空回收站失败：" + e.message, "error");
  }
}

async function loadPromptTemplate() {
  try {
    const data = await api("/api/prompt-template");
    $("setSystemPrompt").value = data.template || "";
  } catch (e) {
    showToast("加载提示词模板失败：" + e.message, "error");
  }
}

function clearPrompt() {
  $("setSystemPrompt").value = "";
}


async function openDirBrowser() {
  const path = state.folder || "";
  $("dirModal").classList.remove("hidden");
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
      up.textContent = "返回上一级";
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
      const li = document.createElement("li");
      li.textContent = dir.name;
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

function init() {
  // The default view is always "已收录" on page load.
  setSelectValue($("statusFilter"), "analyzed");
  $("folderInput").value = state.folder;
  $("folderInput").addEventListener("change", () => {
    const val = $("folderInput").value.trim();
    if (val) {
      state.folder = val;
      rememberFolder(val);
      localStorage.setItem("photoFolder", val);
      loadPhotos();
    }
  });
  $("scanBtn").addEventListener("click", startScan);
  $("rebuildIndexBtn").addEventListener("click", startRebuildIndex);
  $("browseBtn").addEventListener("click", openDirBrowser);
  $("openFolderBtn").addEventListener("click", openFolder);
  $("historyBtn").addEventListener("click", toggleFolderHistory);
  $("settingsBtn").addEventListener("click", openSettings);
  $("searchBtn").addEventListener("click", loadPhotos);
  $("resetFiltersBtn").addEventListener("click", resetFilters);
  $("cancelScanBtn").addEventListener("click", async () => {
    if (state.currentJobId) {
      try { await api(`/api/scan/cancel/${state.currentJobId}`, { method: "POST" }); } catch (e) {}
    }
  });

  $("statusFilter").addEventListener("change", loadPhotos);
  $("minScoreFilter").addEventListener("change", loadPhotos);
  $("sortFilter").addEventListener("change", loadPhotos);
  $("searchInput").addEventListener("change", loadPhotos);
  $("searchInput").addEventListener("keydown", (e) => { if (e.key === "Enter") loadPhotos(); });
  $("tagFilter").addEventListener("change", loadPhotos);
  $("favoriteFilter").addEventListener("change", loadPhotos);
  $("yearFilter").addEventListener("change", loadPhotos);
  $("searchMode").addEventListener("change", loadPhotos);

  $("deleteSelectedBtn").addEventListener("click", () => deletePhotos([...state.selected]));
  $("restoreSelectedBtn").addEventListener("click", () => restorePhotos([...state.selected]));
  $("reanalyzeSelectedBtn").addEventListener("click", () => reanalyzeSelected([...state.selected]));
  $("permanentDeleteSelectedBtn").addEventListener("click", () => permanentDeletePhotos([...state.selected]));
  $("clearSelectionBtn").addEventListener("click", () => {
    state.selected.clear();
    updateSelectionBar();
    renderGallery();
  });

  $("previewPrevBtn").addEventListener("click", () => navigatePreview(-1));
  $("previewNextBtn").addEventListener("click", () => navigatePreview(1));
  $("previewEditBtn").addEventListener("click", openEditPreview);
  $("previewDeleteBtn").addEventListener("click", () => deletePhotos([state.currentPreviewId]));
  $("previewRestoreBtn").addEventListener("click", () => restorePhotos([state.currentPreviewId]));
  $("previewPermanentDeleteBtn").addEventListener("click", () => permanentDeletePhotos([state.currentPreviewId]));
  $("previewReanalyzeBtn").addEventListener("click", () => reanalyze(state.currentPreviewId));
  $("previewFavoriteBtn").addEventListener("click", async () => {
    const photo = state.photos.find((p) => p.id === state.currentPreviewId);
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
  });

  $("dirGoBtn").addEventListener("click", () => loadDirList($("dirPathInput").value.trim()));
  $("dirUpBtn").addEventListener("click", () => loadDirList(state.currentDirPath ? state.currentDirPath.replace(/[\/][^\/]*$/, "") : ""));
  $("dirChooseBtn").addEventListener("click", () => {
    if (state.currentDirPath) {
      state.folder = state.currentDirPath;
      rememberFolder(state.folder);
      localStorage.setItem("photoFolder", state.folder);
      $("folderInput").value = state.folder;
      closeModal("dirModal");
      loadPhotos();
    }
  });

  $("saveSettingsBtn").addEventListener("click", saveSettings);
  $("cleanCacheBtn").addEventListener("click", cleanCache);
  $("removeFolderBtn").addEventListener("click", removeCurrentFolder);
  $("editSaveBtn").addEventListener("click", saveEditedPhoto);
  $("editCancelBtn").addEventListener("click", () => closeModal("editModal"));
  $("emptyTrashBtn").addEventListener("click", emptyTrash);
  $("usePromptTemplateBtn").addEventListener("click", loadPromptTemplate);
  $("clearPromptBtn").addEventListener("click", clearPrompt);

  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => closeModal(btn.dataset.close));
  });
  document.querySelectorAll(".modal").forEach((modal) => {
    modal.addEventListener("click", (e) => {
      if (e.target === modal) closeModal(modal.id);
    });
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest("#historyBtn") && !e.target.closest("#folderHistory")) {
      $("folderHistory").classList.add("hidden");
    }
  });
  document.addEventListener("keydown", (e) => {
    if ($("previewModal").classList.contains("hidden")) return;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      navigatePreview(-1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      navigatePreview(1);
    } else if (e.key === "Escape") {
      closeModal("previewModal");
    }
  });

  document.querySelectorAll("select").forEach(enhanceSelect);
  updateFilterHighlights();
  updateTrashActions();
  document.querySelectorAll(".clickable-stat").forEach((el) => {
    el.addEventListener("click", () => applyStatFilter(el.dataset.filter));
  });

  loadInitialFolder();
}

document.addEventListener("DOMContentLoaded", init);
