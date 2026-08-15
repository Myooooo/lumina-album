/* ==========================================================================
   Lumina Album (拾光相册) - Frontend Core Controller
   ========================================================================== */
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
  fullscreen: {
    scale: 1,
    panX: 0,
    panY: 0,
    isDragging: false,
    startX: 0,
    startY: 0,
  },
};

const ICONS = window.LuminaIcons;
const $ = (id) => document.getElementById(id);
const {
  renderAllIcons,
  enhanceSelect,
  setSelectValue,
  showToast,
  showLoading,
  hideLoading,
  confirmDialog,
} = window.LuminaUI;

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
    // localStorage may be unavailable in some environments
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

function formatFnumber(val) {
  if (val === null || val === undefined || val === "") return "";
  const num = Number(val);
  if (!Number.isFinite(num) || num <= 0) return String(val);
  return num >= 10 ? num.toFixed(0) : num.toFixed(1).replace(/\.0$/, "");
}

function formatExposure(val) {
  if (val === null || val === undefined || val === "") return "";
  const s = String(val).trim();
  if (s.includes("/")) return s + "s";
  const num = Number(s);
  if (!Number.isFinite(num) || num <= 0) return s;
  if (num < 1) {
    const denom = Math.round(1 / num);
    return `1/${denom}s`;
  }
  return `${num.toFixed(1).replace(/\.0$/, "")}s`;
}

function formatIso(val) {
  if (val === null || val === undefined || val === "") return "";
  return String(val).replace(/^ISO\s*/i, "");
}

function formatFocalLength(val) {
  if (val === null || val === undefined || val === "") return "";
  const num = parseFloat(String(val).replace(/mm$/i, "").trim());
  if (!Number.isFinite(num)) return String(val);
  return `${num.toFixed(0)}mm`;
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
    const rawVal = d[key];
    const val = (rawVal === null || rawVal === undefined) ? 0 : Number(rawVal);
    const pct = Math.max(4, Math.min(100, Math.round(val * 10)));
    const color = DIMENSION_COLORS[key] || "#b3815a";
    return `
      <div class="dimension-row">
        <span class="dimension-name">${DIMENSION_NAMES[key]}</span>
        <div class="dimension-bar">
          <div class="dimension-fill" style="width:${pct}%;background-color:${color}!important;"></div>
        </div>
        <span class="dimension-value" style="color:${color}">${val.toFixed(1)}</span>
      </div>
    `;
  }).join("");
}

function dimensionValues(dims) {
  const d = dims || {};
  return Object.keys(DIMENSION_NAMES).map((key) => {
    const val = d[key] ?? 0;
    const color = DIMENSION_COLORS[key] || "#b3815a";
    return `<span class="score-pill" style="color:${color};background-color:${color}1a">${DIMENSION_NAMES[key]} ${Number(val).toFixed(1)}</span>`;
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
      const label = wrapper ? wrapper.querySelector(".custom-select-label") : null;
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
        <button class="btn small restore-btn" title="恢复"><span class="btn-icon">${ICONS.restore}</span>恢复</button>
        <button class="btn small danger permanent-btn" title="彻底移除"><span class="btn-icon">${ICONS.close}</span></button>
      `;
    } else {
      deleteActions = `
        <button class="btn small danger delete-btn" title="暂时收起"><span class="btn-icon">${ICONS.trash}</span></button>
      `;
    }

    card.innerHTML = `
      <div class="polaroid-inner">
        <div class="polaroid-front">
          <div class="tape"></div>
          <button class="favorite-btn ${photo.favorite ? "active" : ""}" title="${photo.favorite ? "取消珍藏" : "珍藏"}">${photo.favorite ? ICONS.heartFilled : ICONS.heart}</button>
          <img class="polaroid-photo" src="/api/thumbnail/${photo.id}" alt="${escapeHtml(photo.filename)}" loading="lazy" />
          <div class="polaroid-body">
            ${photo.location ? `<div class="photo-location">${ICONS.pin} <span>${escapeHtml(photo.location)}</span></div>` : ""}
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
                <button class="btn small reanalyze-btn" title="重新解读"><span class="btn-icon">${ICONS.refresh}</span></button>
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
  const force = $("forceScan").checked;
  try {
    const data = await api("/api/rebuild-index", {
      method: "POST",
      body: JSON.stringify({ folder, force }),
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
      const phaseText = job.phase === "analyze" ? "正在解读" : "整理照片";
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
  showLoading("重新解读这张照片…");
  try {
    const updated = await api("/api/reanalyze", {
      method: "POST",
      body: JSON.stringify({ id }),
    });

    // Patch the current card immediately so the latest title/score/tags are
    // visible even before the next list refresh completes.
    const idx = state.photos.findIndex((p) => p.id === id);
    if (idx >= 0 && updated) {
      state.photos[idx] = updated;
      renderGallery({ animate: false });
    }

    await loadPhotos();
    const stillVisible = state.photos.some((p) => p.id === id);
    if (state.currentPreviewId === id) {
      if (stillVisible) {
        updatePreview({ skipImage: true });
      } else {
        closeModal("previewModal");
      }
    }
    if ($("statusFilter").value === "pending" && !stillVisible) {
      showToast("重新解读完成，照片已移入已收录。", "success");
    }
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
      panel.innerHTML = `<div class="folder-history-empty">${ICONS.history || ""} 暂无整理过的文件夹</div>`;
    } else {
      panel.innerHTML = data.folders.map((f) => `<div class="folder-history-item" data-path="${escapeHtml(f)}">${ICONS.folder || ""}<span>${escapeHtml(f)}</span></div>`).join("");
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

function applyFsTransform() {
  const img = $("fsImage");
  if (!img) return;
  const { scale, panX, panY } = state.fullscreen;
  img.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
  const pctEl = $("fsZoomPct");
  if (pctEl) pctEl.textContent = `${Math.round(scale * 100)}%`;
}

function openFullscreenViewer() {
  if (!state.currentPreviewId) return;
  const photo = state.photos.find((p) => p.id === state.currentPreviewId);
  if (!photo) return;
  
  const fsModal = $("fullscreenModal");
  const fsImg = $("fsImage");
  if (!fsModal || !fsImg) return;

  state.fullscreen.scale = 1;
  state.fullscreen.panX = 0;
  state.fullscreen.panY = 0;
  state.fullscreen.isDragging = false;

  fsImg.src = `/api/original/${photo.id}`;
  applyFsTransform();
  fsModal.classList.remove("hidden");
}

function closeFullscreenViewer() {
  const fsModal = $("fullscreenModal");
  if (fsModal) fsModal.classList.add("hidden");
  state.fullscreen.scale = 1;
  state.fullscreen.panX = 0;
  state.fullscreen.panY = 0;
  state.fullscreen.isDragging = false;
}

function zoomFullscreen(factor, clientX, clientY) {
  const oldScale = state.fullscreen.scale;
  let newScale = oldScale * factor;
  newScale = Math.max(0.5, Math.min(6.0, newScale));
  
  if (clientX !== undefined && clientY !== undefined) {
    const rect = $("fsImageContainer").getBoundingClientRect();
    const offsetX = clientX - (rect.left + rect.width / 2);
    const offsetY = clientY - (rect.top + rect.height / 2);
    state.fullscreen.panX -= (offsetX - state.fullscreen.panX) * (newScale / oldScale - 1);
    state.fullscreen.panY -= (offsetY - state.fullscreen.panY) * (newScale / oldScale - 1);
  }
  
  state.fullscreen.scale = newScale;
  applyFsTransform();
}

function resetFullscreenZoom() {
  state.fullscreen.scale = 1;
  state.fullscreen.panX = 0;
  state.fullscreen.panY = 0;
  applyFsTransform();
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

/**
 * Update and Render Lightbox Preview Details with Rich Iconography
 */
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
  favBtn.innerHTML = `${photo.favorite ? ICONS.heartFilled : ICONS.heart}<span>${photo.favorite ? "已珍藏" : "珍藏"}</span>`;
  favBtn.title = photo.favorite ? "取消珍藏 (快捷键 F)" : "珍藏 (快捷键 F)";

  const exif = photo.exif || {};
  const cameraText = [exif.make, exif.model].filter(Boolean).join(" ");
  
  // Optical Specs Grid - Compact row with icons only
  const specItems = [];
  if (cameraText) {
    specItems.push(`
      <div class="spec-badge camera-badge" title="拍摄机身/镜头：${escapeHtml(cameraText)}">
        ${ICONS.camera}
        <span class="spec-val">${escapeHtml(cameraText)}</span>
      </div>
    `);
  }
  if (exif.fnumber) {
    specItems.push(`
      <div class="spec-badge" title="光圈：f/${formatFnumber(exif.fnumber)}">
        ${ICONS.aperture}
        <span class="spec-val">f/${formatFnumber(exif.fnumber)}</span>
      </div>
    `);
  }
  if (exif.exposure) {
    specItems.push(`
      <div class="spec-badge" title="快门速度：${formatExposure(exif.exposure)}">
        ${ICONS.shutter}
        <span class="spec-val">${formatExposure(exif.exposure)}</span>
      </div>
    `);
  }
  if (exif.iso) {
    specItems.push(`
      <div class="spec-badge" title="感光度：ISO ${formatIso(exif.iso)}">
        ${ICONS.iso}
        <span class="spec-val">ISO ${formatIso(exif.iso)}</span>
      </div>
    `);
  }
  if (exif.focal_length) {
    specItems.push(`
      <div class="spec-badge" title="焦距：${formatFocalLength(exif.focal_length)}">
        ${ICONS.focal}
        <span class="spec-val">${formatFocalLength(exif.focal_length)}</span>
      </div>
    `);
  }

  const captureTime = formatCaptureTime(exif.datetime_original);

  $("previewInfo").innerHTML = `
    <div class="preview-header-block">
      <h3>${escapeHtml(photo.title || photo.filename)}</h3>
    </div>

    ${photo.reason ? `
      <div class="preview-quote-card">
        ${ICONS.quote} “${escapeHtml(photo.reason)}”
      </div>
    ` : ""}

    ${specItems.length ? `
      <div class="specs-badge-grid">
        ${specItems.join("")}
      </div>
    ` : ""}

    <div class="meta-list-block" style="display:flex;flex-direction:column;gap:8px;margin:4px 0;">
      ${captureTime ? `
        <div class="meta-item-row" title="拍摄时间">
          ${ICONS.calendar}
          <div class="meta-content">${escapeHtml(captureTime)}</div>
        </div>
      ` : ""}
      ${photo.location ? `
        <div class="meta-item-row" title="拍摄地点">
          ${ICONS.pin}
          <div class="meta-content">${escapeHtml(photo.location)}</div>
        </div>
      ` : ""}
      <div class="meta-item-row" title="尺寸分辨率">
        ${ICONS.imageSize}
        <div class="meta-content">${photo.width ? photo.width + " × " + photo.height : "-"}</div>
      </div>
      <div class="meta-item-row" title="文件大小">
        ${ICONS.fileSize}
        <div class="meta-content">${fmtSize(photo.size)}</div>
      </div>
      <div class="meta-item-row" title="物理路径">
        ${ICONS.path}
        <div class="meta-content">${escapeHtml(photo.path || "")}</div>
      </div>
    </div>

    <div class="dimension-section">
      <div class="dimension-section-title">
        <span style="display:inline-flex;align-items:center;gap:6px;">${ICONS.score} 回忆评分与维度解读</span>
        <span class="badge score">总分 ${scoreText(photo.score)}</span>
      </div>
      <div class="dimension-list">
        ${dimensionRows(photo.dimensions || {})}
      </div>
    </div>

    ${photo.tags && photo.tags.length ? `
      <div class="meta-item-row" title="情感与场景标签">
        ${ICONS.tag}
        <div class="meta-content">${photo.tags.map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join(" ")}</div>
      </div>
    ` : ""}

    ${photo.error ? `
      <div class="meta-item-row" style="color:var(--danger)" title="解析错误">
        ${ICONS.error}
        <div class="meta-content">${escapeHtml(photo.error)}</div>
      </div>
    ` : ""}
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
    setSelectValue($("setGeocodingProvider"), cfg.geocoding_provider || "nominatim");
    $("setGeocodingApiKey").value = cfg.geocoding_api_key || "";
    $("setGeocodingInterval").value = cfg.geocoding_interval || 1.0;
    $("setGeocodingRetries").value = cfg.geocoding_retries ?? 3;
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
    geocoding_retries: parseInt($("setGeocodingRetries").value, 10) || 3,
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
    `确定清空${scope}的回收站吗？\n回收站中的照片原文件将被彻底删除，此操作无法恢复。`,
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
}

function init() {
  // Render embedded icons
  renderAllIcons();

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
  $("previewFavoriteBtn").addEventListener("click", toggleCurrentPreviewFavorite);
  $("previewImage").addEventListener("click", openFullscreenViewer);

  // Fullscreen Viewer Controls
  const fsCloseBtn = $("fsCloseBtn");
  if (fsCloseBtn) fsCloseBtn.addEventListener("click", closeFullscreenViewer);
  const fsZoomInBtn = $("fsZoomInBtn");
  if (fsZoomInBtn) fsZoomInBtn.addEventListener("click", () => zoomFullscreen(1.25));
  const fsZoomOutBtn = $("fsZoomOutBtn");
  if (fsZoomOutBtn) fsZoomOutBtn.addEventListener("click", () => zoomFullscreen(0.8));
  const fsResetBtn = $("fsResetBtn");
  if (fsResetBtn) fsResetBtn.addEventListener("click", resetFullscreenZoom);

  const fsContainer = $("fsImageContainer");
  if (fsContainer) {
    fsContainer.addEventListener("wheel", (e) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.15 : 0.85;
      zoomFullscreen(factor, e.clientX, e.clientY);
    }, { passive: false });

    fsContainer.addEventListener("mousedown", (e) => {
      if (e.target.closest("button")) return;
      state.fullscreen.isDragging = true;
      state.fullscreen.startX = e.clientX - state.fullscreen.panX;
      state.fullscreen.startY = e.clientY - state.fullscreen.panY;
      fsContainer.classList.add("is-dragging");
    });

    window.addEventListener("mousemove", (e) => {
      if (!state.fullscreen.isDragging) return;
      state.fullscreen.panX = e.clientX - state.fullscreen.startX;
      state.fullscreen.panY = e.clientY - state.fullscreen.startY;
      applyFsTransform();
    });

    window.addEventListener("mouseup", () => {
      if (state.fullscreen.isDragging) {
        state.fullscreen.isDragging = false;
        fsContainer.classList.remove("is-dragging");
      }
    });

    fsContainer.addEventListener("dblclick", (e) => {
      if (state.fullscreen.scale > 1.1) {
        resetFullscreenZoom();
      } else {
        zoomFullscreen(2.0, e.clientX, e.clientY);
      }
    });
  }

  $("dirGoBtn").addEventListener("click", () => loadDirList($("dirPathInput").value.trim()));
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

  // Keyboard Shortcuts: ← / → / Esc / F / E
  document.addEventListener("keydown", (e) => {
    const fsModal = $("fullscreenModal");
    if (fsModal && !fsModal.classList.contains("hidden")) {
      if (e.key === "Escape") {
        closeFullscreenViewer();
      } else if (e.key === "+" || e.key === "=") {
        zoomFullscreen(1.25);
      } else if (e.key === "-") {
        zoomFullscreen(0.8);
      } else if (e.key === "0") {
        resetFullscreenZoom();
      }
      return;
    }

    if ($("previewModal").classList.contains("hidden")) return;
    if (["input", "textarea", "select"].includes((e.target.tagName || "").toLowerCase())) return;

    if (e.key === "ArrowLeft") {
      e.preventDefault();
      navigatePreview(-1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      navigatePreview(1);
    } else if (e.key === "Escape") {
      closeModal("previewModal");
    } else if (e.key === "f" || e.key === "F") {
      e.preventDefault();
      toggleCurrentPreviewFavorite();
    } else if (e.key === "e" || e.key === "E") {
      e.preventDefault();
      openEditPreview();
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
