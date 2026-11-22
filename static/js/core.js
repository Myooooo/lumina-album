/* ==========================================================================
   Lumina Album (拾光相册) - Core: state, API client and formatting
   Loaded before every other slice. See index.html for the order; the functions
   are global on purpose (no bundler), and the boot code runs in app.js once the
   document is ready.
   ========================================================================== */
"use strict";

const state = {
  // Filled from the server on start (see loadUiState): the album's memory lives
  // next to the album, not in the browser, so clearing site data cannot lose it.
  folder: "",
  folderHistory: [],
  //: Albums hidden from every path selector. They are opened only from the
  //: settings panel's hidden list, and never remembered as the open album.
  hiddenFolders: [],
  folderHidden: false,
  //: How many photos the open album holds, regardless of the active filters.
  //: The empty state needs this to tell "this album has nothing" apart from
  //: "these filters match nothing"; reading it off the stats row was a frame
  //: behind while a folder switch was still loading.
  albumTotal: 0,
  importSource: "",
  photos: [],
  selected: new Set(),
  currentDirPath: "",
  currentPreviewId: null,
  //: Set when a sheet (足迹 / 那年今日 / 相似回忆) owns the detail dialog:
  //: ``{ids, title}``. Navigation then walks ``ids`` instead of the gallery, because
  //: those sheets show photos the album page may not have loaded at all.
  previewContext: null,
  //: Which of the four views is on screen.
  view: "album",
  //: True once the first view has been painted. The very first showView() has
  //: nothing to animate away from, so it activates immediately rather than
  //: waiting out the exit duration behind an empty screen.
  viewsBooted: false,
  //: The places 足迹 has loaded, and the one currently opened. The place view
  //: replaces the list inside the same sheet, so both live here rather than in the
  //: DOM alone - going back has to redraw the grid without a second request.
  footprintPlaces: [],
  //: How many of the open place's photos have been painted so far.
  footprintShown: 0,
  footprintPlace: null,
  //: The payload 那年今日 last rendered, so a thumbnail click can walk that day.
  memoriesData: null,
  previewNavToken: 0,
  pollingTimer: null,
  pollTick: 0,
  //: Ids of the dialogs currently open, oldest first. Escape closes the last one,
  //: which is the one on top - and "on top" has to follow the order they were
  //: opened, not document order, because the folder browser is opened over the
  //: import sheet while sitting *before* it in the markup.
  modalStack: [],
  //: Speed estimate for the running job (see updateEta).
  eta: null,
  //: ``id -> photo`` for the rows currently in ``state.photos``. Rebuilding this
  //: map on every change keeps lookups O(1); a long album used to run a linear
  //: ``find`` for every click, every favourite toggle and every card render.
  photoById: new Map(),
  //: Ids whose detail payload (EXIF, raw size) has already been fetched.
  //: ``id -> detail payload`` (EXIF, raw size, video info). Kept beside the
  //: rows so a background poll, which rebuilds them from the list, cannot lose
  //: what the dialog needs.
  details: new Map(),
  //: Ids with a detail request in flight, so a fast double-click asks once.
  detailRequested: new Set(),
  //: Length of the video preview clip, filled from /api/config so the dialog
  //: label matches what the server actually produced.
  videoClipSeconds: 3,
  //: How the detail-page preview behaves: "off", "once" or "loop".
  videoPreviewMode: "once",
  //: Preview volume, 0-100.
  videoPreviewVolume: 50,
  //: Pending timer that hides the "generating a preview" cue after its fade.
  renderingHideTimer: null,
  //: Live Photo motion volume, 0-100. Silent by default.
  liveVolume: 0,
  //: Whether the server found ffmpeg. Known from startup, so a full redo can
  //: warn about blanking video metadata before it starts rather than after.
  ffmpegAvailable: true,
  //: ``id -> position`` in ``state.photos``, for the few callers that splice by
  //: index instead of by row.
  photoIndex: new Map(),
  pageSize: 100,
  offset: 0,
  hasMore: false,
  renderedCount: 0,
  loadingMore: false,
  currentJobId: null,
  // Camera-album state: the card paired with the open folder, every pairing,
  // and the last preview the sync dialog rendered.
  card: null,
  cardAll: [],
  cardPreview: null,
  cardBannerDismissed: false,
  cardPendingSeen: 0,
  cardTimer: null,
  historyCloseTimer: null,
  //: Which status filter this album opens on ("analyzed", or "all" for a folder
  //: that has only been indexed). Set once the album is known.
  defaultStatusFilter: "analyzed",
  fullscreen: {
    scale: 1,
    panX: 0,
    panY: 0,
    isDragging: false,
    startX: 0,
    startY: 0,
  },
};

let promptPresets = {};
//: Prompts the reader saved, and the fixed text shown read-only in settings.
let customPromptPresets = [];
let promptFixedText = "";
//: Markers of the fixed prompt blocks. The server owns the strings; these are
//: fallbacks for a page opened before the endpoint answers.
let promptMarkers = {
  preamble: "你在帮一个人整理他拍过的照片",
  anti_ai: "【写作要求：说人话】",
  format: "【输出格式：必须严格遵守】",
  legacy_format: "Return ONLY a JSON object, no markdown, with exactly these keys:",
};
//: The custom preset the editor is currently based on, so saving updates it
//: instead of creating a duplicate.
let activeCustomPresetId = "";

const ICONS = window.LuminaIcons;
const $ = (id) => document.getElementById(id);
const {
  renderAllIcons,
  enhanceSelect,
  setSelectValue,
  showToast,
  showModal,
  hideModal,
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

/** True when ``path`` is one of the albums hidden from the path selectors. */
function isHiddenFolder(path) {
  const key = folderPathKey(path);
  if (!key) return false;
  return state.hiddenFolders.some((item) => folderPathKey(item) === key);
}

/** Load the hidden-album list; a failure just means nothing is hidden. */
async function loadHiddenFolders() {
  try {
    const data = await api("/api/folders/hidden");
    state.hiddenFolders = Array.isArray(data.folders) ? data.folders : [];
  } catch (e) {
    console.warn("加载隐藏目录失败", e);
    state.hiddenFolders = [];
  }
}

/** Show or hide the 「已隐藏」 marker beside the path field. */
function paintFolderHiddenBadge() {
  const badge = $("folderHiddenBadge");
  if (badge) badge.classList.toggle("hidden", !state.folderHidden);
  const wrapper = document.querySelector(".folder-input-wrapper");
  if (wrapper) wrapper.classList.toggle("has-hidden-badge", !!state.folderHidden);
}

/* ==========================================================================
   Photo index
   A long album holds thousands of rows, and almost every interaction needs one
   of them by id. A linear `find` per click is fine at 100 photos and painful at
   3000, so the list keeps an id -> row map alongside it. Every place that
   replaces `state.photos` goes through `indexPhotos` so the map cannot drift.
   ========================================================================== */

function indexPhotos(list) {
  state.photos = list;
  const map = new Map();
  const positions = new Map();
  for (let i = 0; i < list.length; i += 1) {
    const row = list[i];
    // The list payload carries no EXIF or video metadata on purpose. A row that
    // has been opened before gets its detail payload put back here, so a poll
    // or a filter change cannot make the parameters disappear.
    const detail = state.details.get(row.id);
    if (detail) applyPhotoDetail(row, detail);
    map.set(row.id, row);
    positions.set(row.id, i);
  }
  state.photoById = map;
  state.photoIndex = positions;
  return list;
}

function photoById(id) {
  return state.photoById.get(id) || null;
}

/**
 * Make ``rows`` available to the detail dialog without changing the gallery.
 *
 * Rows are written into the lookup map only. `state.photos` is left alone on
 * purpose: it is the grid's list, and appending a place's photos to it would make
 * the grid render cards that belong to no album page and would reorder the
 * arrow-key walk for every other photo.
 *
 * A row already known is not overwritten, so a photo that the gallery has loaded -
 * with its detail payload applied - keeps its richer copy.
 */
function adoptPhotos(rows) {
  if (!Array.isArray(rows)) return;
  for (const row of rows) {
    if (!row || !Number.isFinite(Number(row.id))) continue;
    if (state.photoById.has(row.id)) continue;
    const detail = state.details.get(row.id);
    if (detail) applyPhotoDetail(row, detail);
    state.photoById.set(row.id, row);
  }
}

/** Position of a photo in the loaded list, or -1. */
function photoIndexById(id) {
  const index = state.photoIndex.get(id);
  return index === undefined ? -1 : index;
}

/* ==========================================================================
   Remembered UI state
   The folder history, the open album and the remembered card path are stored by
   the server (table ``ui_state``), so they survive clearing browser data and are
   shared by every browser pointed at this album.
   ========================================================================== */

async function loadUiState() {
  try {
    const data = await api("/api/ui-state");
    const saved = data.state || {};
    state.folder = typeof saved.folder === "string" ? saved.folder : "";
    state.folderHistory = Array.isArray(saved.folder_history)
      ? saved.folder_history.filter((x) => typeof x === "string")
      : [];
    state.importSource =
      typeof saved.import_source === "string" ? saved.import_source : "";
    // The duplicate sheet's threshold is a judgement the reader already made;
    // restoring it is the difference between a remembered preference and having
    // to re-decide every time the page opens.
    if (Number.isFinite(Number(saved.dup_threshold))) {
      dupState.threshold = Math.max(0, Math.min(Number(saved.dup_threshold), 24));
    }
  } catch (e) {
    // A brand-new database has no state yet; an empty one is a fine default.
    console.warn("读取界面状态失败", e);
  }
}

/** Persist one slice of UI state. Failure is never worth interrupting the user. */
function saveUiState(patch) {
  api("/api/ui-state", {
    method: "POST",
    body: JSON.stringify(patch),
  }).catch((e) => console.warn("保存界面状态失败", e));
}

function rememberFolder(folder) {
  const clean = String(folder || "").trim();
  if (!clean) return;
  // A hidden album is never part of the visible history.
  if (isHiddenFolder(clean)) return;
  state.folderHistory = [
    clean,
    ...state.folderHistory.filter((item) => folderPathKey(item) !== folderPathKey(clean)),
  ].slice(0, 20);
  saveUiState({ folder_history: state.folderHistory });
}

async function selectBestFolder() {
  let folders = [];
  try {
    const data = await api("/api/folders");
    folders = data.folders || [];
  } catch (e) {
    folders = [];
  }
  const candidates = [state.folder, ...state.folderHistory]
    .filter(Boolean)
    .filter((candidate) => !isHiddenFolder(candidate));
  const chosen =
    candidates.find((candidate) =>
      folders.some((folder) => folderPathKey(folder) === folderPathKey(candidate))
    ) ||
    folders[0] ||
    "";
  if (chosen) rememberFolder(chosen);
  return chosen;
}

/**
 * Pick up a job that was already running when this page loaded.
 *
 * A refresh must not lose a job: the server keeps running it, and the toolbar
 * has to show it again straight away - progress bar, counts and estimate
 * included. The estimate is rebuilt from the job's own ``elapsed`` time, so it
 * is right from the first poll rather than re-learned over several seconds.
 *
 * The current folder is preferred, but a job on another folder (or one with no
 * folder at all, like a bin sweep) is still shown: hiding it would make the
 * work look like it had stopped.
 */
async function resumeScanJob() {
  if (state.currentJobId) return;
  try {
    const found = await findRunningJob();
    if (!found) return;
    state.currentJobId = found.job_id;
    // A brand-new view: clear the previous run's rate so the estimate seeded
    // from the server's own ``elapsed`` is what gets shown.
    state.eta = null;
    $("scanProgress").classList.remove("hidden");
    $("progressText").textContent = progressLabel(found);
    paintJobProgress(found);
    pollScan();
  } catch (e) {
    console.warn("恢复扫描任务失败", e);
  }
}

/**
 * Find the job a freshly loaded page should be showing.
 *
 * The current album's job wins, but a job with no folder (a bin sweep) or one
 * belonging to another album is still returned: it is real work in progress,
 * and hiding it would make it look as if the refresh had stopped it.
 */
async function findRunningJob() {
  const isLive = (job) => job.status === "running" || job.status === "pending";
  if (state.folder) {
    const query = `?folder=${encodeURIComponent(state.folder)}`;
    const data = await api(`/api/scan/jobs${query}`);
    const mine = (data.jobs || []).find(isLive);
    if (mine) return mine;
  }
  const all = await api("/api/scan/jobs");
  return (all.jobs || []).find(isLive) || null;
}

async function loadInitialFolder() {
  await loadUiState();
  state.folder = await selectBestFolder();
  saveUiState({ folder: state.folder });
  $("folderInput").value = state.folder;
  // Whatever the stored state says, the boot album is always a visible one.
  state.folderHidden = isHiddenFolder(state.folder);
  paintFolderHiddenBadge();
  if (state.folder) {
    await applyDefaultStatusFilter();
    await loadPhotos();
  } else {
    indexPhotos([]);
    renderGallery();
    updateSelectionBar();
    loadStats();
    loadTags();
    loadYears();
  }
}

/**
 * Pick the filter this album should open on.
 *
 * The default is "已收录", which is what someone browsing a finished album
 * wants. A folder that has only been indexed - an import run with the model
 * switched off, say - has nothing under that filter, and landing on an empty
 * gallery looks like a failure. In that case fall back to "全部回忆".
 *
 * The answer is remembered on ``state`` so "重置" falls back to the same view
 * instead of undoing the fallback the user never chose.
 */
async function applyDefaultStatusFilter() {
  let value = "analyzed";
  const folder = state.folder || "";
  // Until this album's own numbers arrive it holds nothing as far as the
  // empty state is concerned; the previous album's total must not leak in.
  state.albumTotal = 0;
  if (folder) {
    try {
      const stats = await api(`/api/stats?folder=${encodeURIComponent(folder)}`);
      if (folder !== (state.folder || "")) return;
      state.albumTotal = Number(stats.total) || 0;
      if (!stats.analyzed && stats.total) value = "all";
    } catch (e) {
      console.warn("loadStats failed", e);
    }
  }
  state.defaultStatusFilter = value;
  setSelectValue($("statusFilter"), value);
  updateActiveStat();
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
  kind: "",
  min_score: "",
  sort: "score_desc",
  tag: "",
  year: "",
  search_mode: "smart",
  search: "",
};

function currentFilterValue(key) {
  switch (key) {
    case "kind": return $("kindFilter").value;
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
  // Past a gigabyte, "1024.0 MB" is a number nobody reads. The threshold is
  // 1024 MB rather than 1000: the unit is binary, so that is where the value
  // actually rolls over to 1.0 GB.
  if (bytes < 1024 * 1024 * 1024) return (bytes / 1024 / 1024).toFixed(1) + " MB";
  return (bytes / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

/** Write a volume setting into both of its inputs. */
function setVolume(key, value) {
  const editor = VOLUME_EDITORS.find((row) => row.key === key);
  if (editor) setVolumeValue(editor, value);
}

/**
 * File size as shown on the detail page.
 *
 * When a same-named raw file sits next to the photo, its size is part of what
 * the pair takes up on disk, so both numbers are shown: "10.2 MB + 30.4 MB".
 */
function fileSizeText(photo) {
  const image = fmtSize(photo.size);
  const rawSize = Number(photo.raw_size);
  if (!photo.has_raw || !rawSize) return image;
  return `${image} + ${fmtSize(rawSize)}`;
}

/**
 * A running time, as shown on a video card: 00:07 / 01:23 / 1:02:03.
 *
 * Returns an empty string when the duration is unknown, so a card whose ffprobe
 * failed simply does not show a badge instead of showing "00:00".
 */
function formatDuration(ms) {
  const total = Math.round(Number(ms) / 1000);
  if (!Number.isFinite(total) || total <= 0) return "";
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  const pad = (value) => String(value).padStart(2, "0");
  if (hours > 0) return `${hours}:${pad(minutes)}:${pad(seconds)}`;
  return `${pad(minutes)}:${pad(seconds)}`;
}

/** Human wording for a size, used by the codec rows. */
function formatBitrate(bits) {
  const value = Number(bits);
  if (!Number.isFinite(value) || value <= 0) return "";
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)} Mbps`;
  return `${Math.round(value / 1000)} kbps`;
}

function formatFps(fps) {
  const value = Number(fps);
  if (!Number.isFinite(value) || value <= 0) return "";
  const rounded = Math.round(value * 100) / 100;
  return `${Number.isInteger(rounded) ? rounded : rounded.toFixed(2)} fps`;
}

/**
 * Duration pill on a video card.
 *
 * Sits at the bottom-right of the frame, opposite the RAW badge, and uses the
 * same frosted-pill language as the rest of the cards.
 */
function videoDurationBadge(photo) {
  const text = formatDuration(photo.duration_ms);
  if (!text) return "";
  return `<span class="video-badge" title="时长 ${text}">${ICONS.play || ""}<span>${text}</span></span>`;
}

/**
 * The Live Photo mark: concentric rings, the way iOS draws it.
 *
 * A motion photo or an iPhone pair is still a photo, so its card keeps the
 * polaroid look - the badge only says "this one moves".
 */
function liveBadgeHtml(photo) {
  if (!photo.has_live) return "";
  return `<span class="live-badge" title="实况照片，详情页可以播放">${ICONS.live}</span>`;
}

/**
 * The extra metadata rows a video needs.
 *
 * The four headline numbers (resolution, codec, bitrate, frame rate) sit in the
 * spec-badge row above, next to the camera and aperture chips of a photo. What
 * is left is folded into two rows - the rest of the video track, and the audio
 * track - so the list does not turn into a wall of one-value-per-line.
 *
 * Rows carry an icon and a value only: the icon already says what the number
 * is, and a repeated word beside every value reads as clutter.
 */
function videoDetailRows(photo) {
  if (photo.media_kind !== "video") return "";
  const info = photo.video_info || {};

  const videoParts = [];
  const duration = formatDuration(photo.duration_ms);
  if (duration) videoParts.push(duration);
  if (info.container) {
    videoParts.push(String(info.container).replace(/^\./, "").toUpperCase());
  }

  const audioParts = [];
  if (info.has_audio) {
    if (info.audio_codec) audioParts.push(info.audio_codec);
    const audioBitrate = formatBitrate(info.audio_bitrate);
    if (audioBitrate) audioParts.push(audioBitrate);
    if (Number(info.audio_channels) > 0) {
      audioParts.push(`${info.audio_channels} 声道`);
    }
  } else if (Object.keys(info).length) {
    audioParts.push("无音轨");
  }

  const row = (icon, title, text) =>
    text
      ? `<div class="meta-item-row" title="${escapeHtml(title)}">
          ${icon}
          <div class="meta-content">${escapeHtml(text)}</div>
        </div>`
      : "";

  // Nothing was read from the file, so say so instead of showing a blank panel.
  // The most common cause is that the file is not there any more.
  if (!Object.keys(info).length) {
    const why = photo.error
      ? `无法读取这段视频的信息：${photo.error}`
      : "没能读取这段视频的信息。文件可能已被移动或删除，重新整理一次可以再试。";
    return row(ICONS.error, "视频信息", why);
  }

  return (
    row(ICONS.hourglass, "时长与封装", videoParts.join(" · ")) +
    row(ICONS.volume, "音频", audioParts.join(" · "))
  );
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

/**
 * Marker shown when a photo has a camera-raw counterpart on disk.
 *
 * The photo itself is still the JPEG (that is what the model analysed and what
 * every thumbnail/proxy is rendered from); the badge only tells the user that a
 * raw original exists next to it. When the raw file is the *only* version, the
 * badge says so and the note explains that rendering comes from the embedded
 * preview.
 */
function rawBadgeHtml(photo, variant = "card") {
  const rawOnly = !!photo.is_raw;
  const rawExt = (photo.raw_ext || "").trim();
  if (!rawOnly && !photo.has_raw) return "";
  const name = rawOnly
    ? `RAW · ${escapeHtml(photo.filename || "原始文件")}`
    : `RAW${rawExt ? " · " + escapeHtml(rawExt) : ""}`;
  const title = rawOnly
    ? "此照片只有 RAW 文件，缩略图与代理图由内嵌预览生成"
    : "同目录存在同名的 RAW 原始文件，当前显示与解读基于图片文件";
  const icon = rawOnly ? ICONS.raw : ICONS.rawLayers;
  const classes = ["raw-badge", variant === "detail" ? "raw-badge-detail" : "raw-badge-card"];
  if (rawOnly) classes.push("raw-only");
  return `<span class="${classes.join(" ")}" title="${escapeHtml(title)}">${icon}<span>${name}</span></span>`;
}

/** Extra metadata row listing the raw file that sits next to this photo. */
function rawDetailRow(photo) {
  const rawOnly = !!photo.is_raw;
  if (!rawOnly && !photo.has_raw) return "";
  const rawPath = rawOnly ? photo.path || "" : photo.raw_path || "";
  const tip = rawOnly
    ? "此照片只有 RAW 文件，缩略图与代理图由内嵌预览生成"
    : "同目录存在同名的 RAW 原始文件，当前显示与解读基于图片文件";
  return `
    <div class="meta-item-row raw-meta-row" title="${escapeHtml(tip)}">
      ${ICONS.raw}
      <div class="meta-content">RAW路径：${pathActionHtml(rawPath, "RAW 文件", photo.id)}</div>
    </div>
  `;
}

/**
 * Render a file path as a clickable value.
 *
 * Clicking copies the path; the small button on the right asks the server to
 * hand the file to the OS default application. The path itself is escaped, so
 * a crafted filename can never inject markup.
 */
function pathActionHtml(value, label, photoId) {
  const text = String(value || "");
  if (!text) return "";
  const copyTitle = escapeHtml(`点击复制路径：${text}`);
  const openTitle = escapeHtml(`用系统默认程序打开${label || "文件"}`);
  return (
    `<span class="path-link" data-path="${escapeHtml(text)}" title="${copyTitle}">` +
    `<span class="path-text">${escapeHtml(text)}</span>${ICONS.copy}` +
    `</span>` +
    `<button type="button" class="path-open-btn" data-open-path="${escapeHtml(text)}" ` +
    `data-photo-id="${photoId}" title="${openTitle}">${ICONS.externalLink}</button>`
  );
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
  // The backend already writes "1/120s"; adding another unit produced
  // "1/120ss". Only append when the value is still bare.
  if (/s$/i.test(s)) return s;
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

/**
 * The three scoring dimensions, in display order.
 *
 * "构图" used to be a fourth row. It measured much the same thing as "技术"
 * (both are craft judgements about the frame rather than about the moment), and
 * separating them made the model split hairs between two nearly identical
 * numbers. Composition now lives inside 技术, and older rows are folded in by
 * the migration in photo_reviewer/db.py.
 */
const DIMENSION_NAMES = {
  technical: "技术",
  memory: "回忆",
  uniqueness: "独特",
};
const DIMENSION_COLORS = {
  technical: "#5b8dd9",
  memory: "#d96b7b",
  uniqueness: "#6f9e7a",
};
//: The same three hues, lifted for the near-black film card. A 10% tint of the
//: paper palette disappears on black, and the "总分" chip in its light-theme
//: cream reads as a headlight; these are the versions that sit quietly on film.
const DIMENSION_COLORS_ON_DARK = {
  technical: "#9dc2e8",
  memory: "#ecabb4",
  uniqueness: "#a9cbae",
};

/**
 * Score rows that participate in the "the model did not fill this in" check.
 *
 * Some providers answer with a score and a title but an empty (or missing)
 * dimensions object. Treating those zeros as real ratings would show three 0.0
 * bars under a perfectly good total, so a photo whose dimension scores are all
 * zero while its total is not is reported as "尚未评分" instead.
 */
function hasDimensionScores(dims, score) {
  if (score === null || score === undefined) return false;
  const d = dims || {};
  const values = Object.keys(DIMENSION_NAMES).map((key) => Number(d[key]));
  if (values.some((value) => Number.isFinite(value) && value > 0)) return true;
  // All zero: only believe it when the total is zero as well.
  return !(Number(score) > 0);
}

function dimensionRows(dims, score) {
  if (!hasDimensionScores(dims, score)) {
    return `<div class="dimension-empty">这张照片还没有评分，使用“重新解读”或“编辑回忆”打分吧。</div>`;
  }
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

/**
 * The three dimension pills.
 *
 * ``onDark`` picks the lightened palette and a stronger tint: the film card is
 * near-black, so the paper theme's 10%-alpha backgrounds and mid-tone hues lose
 * both contrast and hue there.
 */
function dimensionValues(dims, score, onDark = false) {
  if (!hasDimensionScores(dims, score)) return "";
  const d = dims || {};
  const palette = onDark ? DIMENSION_COLORS_ON_DARK : DIMENSION_COLORS;
  const tint = onDark ? "26" : "1a";
  return Object.keys(DIMENSION_NAMES).map((key) => {
    const val = d[key] ?? 0;
    const color = palette[key] || (onDark ? "#e0c6a8" : "#b3815a");
    return `<span class="score-pill" style="color:${color};background-color:${color}${tint}">${DIMENSION_NAMES[key]} ${Number(val).toFixed(1)}</span>`;
  }).join("");
}

//: Guards against an older stats response landing after a folder switch.
let statsRequestToken = 0;

async function loadStats() {
  const folder = state.folder || "";
  const token = ++statsRequestToken;
  try {
    const q = folder ? `?folder=${encodeURIComponent(folder)}` : "";
    const stats = await api(`/api/stats${q}`);
    // A folder switch leaves the previous album's request in flight; its
    // numbers must not overwrite the new album's.
    if (token !== statsRequestToken || folder !== (state.folder || "")) return;
    state.albumTotal = Number(stats.total) || 0;
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

function getFilterParams(offset = 0) {
  const params = new URLSearchParams();
  if (state.folder) params.set("folder", state.folder);
  params.set("limit", String(state.pageSize));
  params.set("offset", String(offset));
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
  const kind = $("kindFilter").value;
  if (kind) params.set("kind", kind);
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

