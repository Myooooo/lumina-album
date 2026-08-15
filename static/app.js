/* Local Photo Review System - frontend logic */
"use strict";

const state = {
  folder: localStorage.getItem("photoFolder") || "",
  photos: [],
  selected: new Set(),
  flipped: new Set(),
  currentDirPath: "",
  currentPreviewId: null,
  previewNavToken: 0,
  pollingTimer: null,
  currentJobId: null,
};

const ICONS = {
  heart: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>',
  heartFilled: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/></svg>',
  pin: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>',
  arrowUp: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>',
  folder: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
  folderOpen: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><path d="M6 15h12l2-5H4z"/></svg>',
  refresh: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>',
  trash: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>',
  restore: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>',
  close: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>',
  search: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
  settings: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>',
  eye: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
  sparkle: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"/></svg>',
  camera: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>',
  clock: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
};
const $ = (id) => document.getElementById(id);

async function api(url, options = {}) {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
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

function enhanceSelect(select) {
  if (select.dataset.enhanced || select.dataset.noCustom) return;
  select.dataset.enhanced = "1";

  const wrapper = document.createElement("div");
  wrapper.className = "custom-select";
  select.parentNode.insertBefore(wrapper, select);
  wrapper.appendChild(select);

  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "custom-select-trigger";
  trigger.innerHTML = '<span class="custom-select-label"></span><span class="custom-select-arrow"></span>';
  wrapper.appendChild(trigger);

  const menu = document.createElement("div");
  menu.className = "custom-select-menu";
  wrapper.appendChild(menu);

  function updateLabel() {
    const opt = select.options[select.selectedIndex];
    trigger.querySelector(".custom-select-label").textContent = opt ? opt.text : "";
  }

  function buildMenu() {
    menu.innerHTML = "";
    [...select.options].forEach((opt) => {
      const item = document.createElement("div");
      item.className = "custom-select-option" + (opt.selected ? " selected" : "");
      item.textContent = opt.text;
      item.addEventListener("click", () => {
        select.value = opt.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        updateLabel();
        close();
      });
      menu.appendChild(item);
    });
  }

  function open() {
    buildMenu();
    menu.classList.add("open");
    trigger.classList.add("open");
  }
  function close() {
    menu.classList.remove("open");
    trigger.classList.remove("open");
  }

  trigger.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menu.classList.contains("open")) close();
    else open();
  });
  document.addEventListener("click", (e) => {
    if (!wrapper.contains(e.target)) close();
  });

  select.classList.add("native-hidden");
  updateLabel();
}

function setSelectValue(select, value) {
  select.value = value;
  if (select.dataset.enhanced) {
    const wrapper = select.parentElement;
    const label = wrapper.querySelector(".custom-select-label");
    const opt = select.options[select.selectedIndex];
    if (label && opt) label.textContent = opt.text;
  }
}

function updateActiveStat() {
  const current = $("statusFilter").value;
  document.querySelectorAll(".clickable-stat").forEach((el) => {
    el.classList.toggle("active", el.dataset.filter === current);
  });
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

function showToast(message, type = "info") {
  const container = $("toastContainer");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add("removing");
    setTimeout(() => el.remove(), 400);
  }, 3200);
}

function showLoading(text) {
  $("loadingText").textContent = text || "正在处理…";
  $("loadingModal").classList.remove("hidden");
}

function hideLoading() {
  $("loadingModal").classList.add("hidden");
}

function confirmDialog(message, options = {}) {
  return new Promise((resolve) => {
    const modal = $("confirmModal");
    $("confirmTitle").textContent = options.title || "确认操作";
    $("confirmMessage").textContent = message;
    const okBtn = $("confirmOkBtn");
    okBtn.textContent = options.confirmText || "确定";
    okBtn.className = "btn primary" + (options.danger ? " danger" : "");
    const cancelBtn = $("confirmCancelBtn");
    const closeBtn = modal.querySelector(".modal-close");

    function cleanup() {
      modal.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      closeBtn.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onOverlay);
    }
    function onOk() { cleanup(); resolve(true); }
    function onCancel() { cleanup(); resolve(false); }
    function onOverlay(e) { if (e.target === modal) onCancel(); }

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    closeBtn.addEventListener("click", onCancel);
    modal.addEventListener("click", onOverlay);
    modal.classList.remove("hidden");
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

async function loadPhotos() {
  updateFilterHighlights();
  const params = getFilterParams();
  const mode = $("searchMode").value;
  const query = $("searchInput").value.trim();
  let data;
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
    showLoading("正在用大模型寻找相关回忆…");
    try {
      data = await api("/api/search", {
        method: "POST",
        body: JSON.stringify(body),
      });
    } finally {
      hideLoading();
    }
  } else {
    data = await api(`/api/photos?${params.toString()}`);
  }
  state.photos = data.photos || [];
  const ids = new Set(state.photos.map((p) => p.id));
  for (const id of [...state.selected]) {
    if (!ids.has(id)) state.selected.delete(id);
  }
  for (const id of [...state.flipped]) {
    if (!ids.has(id)) state.flipped.delete(id);
  }
  renderGallery();
  updateSelectionBar();
  loadStats();
  loadTags();
  loadYears();
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

function renderGallery() {
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
    card.style.animationDelay = `${(idx % 16) * 0.025}s`;
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
        <button class="btn small danger delete-btn" title="移入回收站">${ICONS.trash}</button>
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
                <button class="btn small reanalyze-btn" title="重新评价">${ICONS.refresh}</button>
                ${deleteActions}
              </span>
            </div>
          </div>
        </div>
      </div>
    `;
    gallery.appendChild(card);

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
        if (await confirmDialog(`确定将“${photo.filename}”移入回收站？`, { title: "移入回收站", confirmText: "移入回收站", danger: true })) {
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
    showToast("请先输入照片文件夹路径", "error");
    return;
  }
  state.folder = folder;
  localStorage.setItem("photoFolder", folder);
  const force = $("forceScan").checked;
  try {
    const data = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({ folder, force }),
    });
    state.currentJobId = data.job_id;
    showProgress("开始扫描…");
    pollScan();
  } catch (e) {
    showToast("启动扫描失败：" + e.message, "error");
  }
}

async function startRebuildIndex() {
  const folder = $("folderInput").value.trim() || state.folder;
  if (!folder) {
    showToast("请先选择文件夹", "error");
    return;
  }
  state.folder = folder;
  localStorage.setItem("photoFolder", folder);
  try {
    const data = await api("/api/rebuild-index", {
      method: "POST",
      body: JSON.stringify({ folder }),
    });
    state.currentJobId = data.job_id;
    showProgress("正在建立索引…");
    pollScan();
  } catch (e) {
    showToast("重建索引失败：" + e.message, "error");
  }
}

function showProgress(text) {
  $("scanProgress").classList.remove("hidden");
  $("progressText").textContent = text;
  $("progressFill").style.width = "0%";
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
            ? `扫描完成：${job.processed}/${job.total}`
            : job.status === "cancelled"
            ? "扫描已取消"
            : "扫描出错：" + (job.error || "");
        setTimeout(hideProgress, 1500);
        state.currentJobId = null;
        loadPhotos();
        loadStats();
        return;
      }
      const pct = job.total ? Math.round((job.processed / job.total) * 100) : 0;
      $("progressFill").style.width = pct + "%";
      const phaseText = job.phase === "analyze" ? "正在分析" : "建立索引";
      $("progressText").textContent = `${phaseText} ${job.processed}/${job.total}：${job.current || ""}`;
      loadPhotos();
      state.pollingTimer = setTimeout(pollScan, 1000);
    })
    .catch((e) => {
      $("progressText").textContent = "获取进度失败：" + e.message;
      setTimeout(hideProgress, 2000);
    });
}

async function deletePhotos(ids) {
  ids = ids.filter((id) => {
    const p = state.photos.find((x) => x.id === id);
    return p && p.status !== "deleted";
  });
  if (!ids.length) return;
  if (!await confirmDialog(`确定将选中的 ${ids.length} 张照片移入回收站？`, { title: "移入回收站", confirmText: "移入回收站", danger: true })) return;
  try {
    const data = await api("/api/delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    if (data.errors && data.errors.length) {
      showToast("部分照片删除失败：" + data.errors.map((e) => e.error).join("; "), "error");
    }
    state.selected.clear();
    await loadPhotos();
    if (state.currentPreviewId) {
      const p = state.photos.find((x) => x.id === state.currentPreviewId);
      if (!p) closeModal("previewModal");
      else updatePreview();
    }
  } catch (e) {
    showToast("删除失败：" + e.message, "error");
  }
}

async function permanentDeletePhotos(ids) {
  ids = ids.filter((id) => {
    const p = state.photos.find((x) => x.id === id);
    return p && p.status === "deleted";
  });
  if (!ids.length) return;
  if (!await confirmDialog(`确定永久删除 ${ids.length} 张照片？\n文件将从磁盘移除，且不可恢复！`, { title: "永久删除", confirmText: "永久删除", danger: true })) return;
  try {
    const data = await api("/api/delete/permanent", {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
    if (data.errors && data.errors.length) {
      showToast("部分照片永久删除失败：" + data.errors.map((e) => e.error).join("; "), "error");
    }
    state.selected.clear();
    await loadPhotos();
    if (state.currentPreviewId) {
      const p = state.photos.find((x) => x.id === state.currentPreviewId);
      if (!p) closeModal("previewModal");
      else updatePreview();
    }
  } catch (e) {
    showToast("永久删除失败：" + e.message, "error");
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
  showLoading("正在让大模型重新欣赏这张照片…");
  try {
    await api("/api/reanalyze", {
      method: "POST",
      body: JSON.stringify({ id }),
    });
    await loadPhotos();
    if (state.currentPreviewId === id) updatePreview();
  } catch (e) {
    showToast("重新分析失败：" + e.message, "error");
  } finally {
    hideLoading();
  }
}

function preloadImage(url) {
  return new Promise((resolve) => {
    const probe = new Image();
    let settled = false;
    const done = () => {
      if (settled) return;
      settled = true;
      resolve();
    };
    probe.onload = done;
    probe.onerror = done;
    probe.src = url;
    setTimeout(done, 10000);
  });
}

async function navigatePreview(delta) {
  if (!state.photos.length) return;
  const idx = state.photos.findIndex((p) => p.id === state.currentPreviewId);
  if (idx < 0) return;
  const nextIdx = (idx + delta + state.photos.length) % state.photos.length;
  const nextPhoto = state.photos[nextIdx];
  const token = ++state.previewNavToken;
  const url = `/api/original/${nextPhoto.id}`;
  await preloadImage(url);
  if (token !== state.previewNavToken) return;
  state.currentPreviewId = nextPhoto.id;
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
      panel.innerHTML = `<div class="folder-history-empty">暂无已索引文件夹</div>`;
    } else {
      panel.innerHTML = data.folders.map((f) => `<div class="folder-history-item" data-path="${escapeHtml(f)}">${escapeHtml(f)}</div>`).join("");
      panel.querySelectorAll(".folder-history-item").forEach((el) => {
        el.addEventListener("click", () => {
          state.folder = el.dataset.path;
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
    showToast("请先选择文件夹", "error");
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
  if (!await confirmDialog(`确定重新分析选中的 ${ids.length} 张照片吗？`, { title: "重新分析", confirmText: "重新分析" })) return;
  showLoading(`正在重新分析选中的 ${ids.length} 张照片…`);
  let ok = 0;
  let fail = 0;
  try {
    for (let i = 0; i < ids.length; i++) {
      $("loadingText").textContent = `正在重新分析照片 ${i + 1}/${ids.length}…`;
      try {
        await api("/api/reanalyze", { method: "POST", body: JSON.stringify({ id: ids[i] }) });
        ok++;
      } catch (e) {
        fail++;
      }
    }
    state.selected.clear();
    await loadPhotos();
    showToast(`重新分析完成：成功 ${ok} 张，失败 ${fail} 张`, fail ? "error" : "success");
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
  if (exif.fnumber) shotParts.push(`f/${exif.fnumber}`);
  if (exif.exposure) shotParts.push(exif.exposure);
  if (exif.iso) shotParts.push(`ISO ${exif.iso}`);
  if (exif.focal_length) shotParts.push(exif.focal_length);
  const cameraHtml = (cameraText || shotParts.length) ? `
    ${cameraText ? `<div class="camera-info">${ICONS.camera} ${escapeHtml(cameraText)}</div>` : ""}
    ${shotParts.length ? `<div class="shot-info">${shotParts.map((p) => `<span class="tag">${escapeHtml(p)}</span>`).join("")}</div>` : ""}
  ` : "";
  const captureTime = formatCaptureTime(exif.datetime_original);
  $("previewInfo").innerHTML = `
    <h3>${escapeHtml(photo.filename)}</h3>
    ${cameraHtml}
    ${captureTime ? `<p>${ICONS.clock} <strong>拍摄时间：</strong>${escapeHtml(captureTime)}</p>` : ""}
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
    $("setGeocodingProvider").value = cfg.geocoding_provider || "nominatim";
    $("setGeocodingApiKey").value = cfg.geocoding_api_key || "";
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
    geocoding_provider: $("setGeocodingProvider").value,
    geocoding_api_key: $("setGeocodingApiKey").value,
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
  $("folderInput").value = state.folder;
  $("folderInput").addEventListener("change", () => {
    const val = $("folderInput").value.trim();
    if (val) {
      state.folder = val;
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
      localStorage.setItem("photoFolder", state.folder);
      $("folderInput").value = state.folder;
      closeModal("dirModal");
      loadPhotos();
    }
  });

  $("saveSettingsBtn").addEventListener("click", saveSettings);
  $("cleanCacheBtn").addEventListener("click", cleanCache);
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
  document.querySelectorAll(".clickable-stat").forEach((el) => {
    el.addEventListener("click", () => applyStatFilter(el.dataset.filter));
  });

  if (state.folder) {
    loadPhotos();
  } else {
    loadStats();
  }
}

document.addEventListener("DOMContentLoaded", init);
