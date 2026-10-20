/* ==========================================================================
   Lumina Album (拾光相册) - Frontend Core Controller
   ========================================================================== */
"use strict";

const state = {
  // Filled from the server on start (see loadUiState): the album's memory lives
  // next to the album, not in the browser, so clearing site data cannot lose it.
  folder: "",
  folderHistory: [],
  importSource: "",
  photos: [],
  selected: new Set(),
  currentDirPath: "",
  currentPreviewId: null,
  previewNavToken: 0,
  pollingTimer: null,
  pollTick: 0,
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
  //: Live Photo motion volume, 0-100. Silent by default.
  liveVolume: 0,
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
const PROMPT_FORMAT_MARKER = "Return ONLY a JSON object, no markdown, with exactly these keys:";

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
  if (state.folder) {
    try {
      const stats = await api(`/api/stats?folder=${encodeURIComponent(state.folder)}`);
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

function dimensionValues(dims, score) {
  if (!hasDimensionScores(dims, score)) return "";
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

async function loadPhotos(options = {}) {
  const quiet = !!options.quiet;
  if (!quiet) updateFilterHighlights();
  const shouldSkeleton = !quiet && !state.photos.length;
  if (shouldSkeleton) renderSkeleton();

  const params = getFilterParams(0);
  const mode = $("searchMode").value;
  const query = $("searchInput").value.trim();
  let data;
  let hasMore = false;
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
      hasMore = !!data.has_more;
    }

    if (data.warning && !quiet) showToast(data.warning, "warning");
    const nextPhotos = data.photos || [];
    if (quiet) {
      applyQuietRefresh(nextPhotos);
      return;
    }
    indexPhotos(nextPhotos);
    state.offset = nextPhotos.length;
    state.hasMore = hasMore;
    state.renderedCount = 0;
    const ids = new Set(state.photos.map((p) => p.id));
    for (const id of [...state.selected]) {
      if (!ids.has(id)) state.selected.delete(id);
    }
    renderGallery({ animate: true });
    updateLoadMoreButton();
    updateSelectionBar();
    loadStats();
    loadTags();
    loadYears();
    // The open folder decides whether a delete can also reach a card, so the
    // badge is refreshed whenever the gallery is (re)loaded.
    refreshCardStatus();
  } catch (e) {
    if (quiet) {
      console.warn("后台刷新照片失败", e);
      return;
    }
    if (shouldSkeleton) {
      indexPhotos([]);
      renderGallery();
    }
    showToast("读取照片失败：" + e.message, "error");
  }
}

/**
 * Compare a freshly polled page against the rows already loaded.
 *
 * Returns ``{ sameShape, changed }``. ``sameShape`` means the polled page is a
 * prefix of what is on screen - the same photos, in the same order - so the
 * loaded list does not have to be rebuilt. ``changed`` names the photos whose
 * status, score or favourite flag moved and whose card therefore needs a
 * repaint.
 *
 * A longer polled page than the loaded list also counts as "different shape":
 * the first page grew, so a photo entered the filter.
 */
function diffPhotoRows(current, nextPhotos) {
  if (nextPhotos.length > current.length) {
    return { sameShape: false, changed: [] };
  }
  const changed = [];
  for (let i = 0; i < nextPhotos.length; i += 1) {
    const fresh = nextPhotos[i];
    const old = current[i];
    if (fresh.id !== old.id) return { sameShape: false, changed: [] };
    if (
      old.status !== fresh.status ||
      old.score !== fresh.score ||
      Boolean(old.favorite) !== Boolean(fresh.favorite)
    ) {
      changed.push(fresh.id);
    }
  }
  return { sameShape: true, changed };
}

/**
 * Apply a background poll without disturbing what the user is looking at.
 *
 * The previous implementation assigned the polled first page straight over
 * `state.photos`, which silently truncated a deep album to 100 rows: the cards
 * stayed on screen, but every one of them beyond the first page was gone from
 * the model, so clicking one opened an empty detail dialog. When the ids also
 * differed it rebuilt the grid from page one and threw the user back to the top.
 *
 * The polled page is now compared against the same leading slice of the loaded
 * list:
 *   same ids  - patch only the rows whose status/score/favourite moved, and the
 *               cards that show them; nothing is laid out again and the scroll
 *               position is untouched;
 *   other ids - a photo really did enter or leave the filter, so the grid is
 *               rebuilt, but the scroll position is restored afterwards.
 *
 * Either way the rows the user has paged in are kept.
 */
function applyQuietRefresh(nextPhotos) {
  const current = state.photos;
  const { sameShape, changed } = diffPhotoRows(current, nextPhotos);

  if (sameShape) {
    if (!changed.length) return;
    const byId = new Map(nextPhotos.map((p) => [p.id, p]));
    for (const id of changed) {
      const old = state.photoById.get(id);
      const fresh = byId.get(id);
      if (!old || !fresh) continue;
      // The detail extras (EXIF, raw size) may have just arrived with the
      // analysis, so the next dialog open re-fetches them.
      invalidatePhotoDetail(id);
      // Keep the object identity: the card's DOM was built from this row.
      Object.assign(old, fresh);
      refreshCard(id);
    }
    updateSelectionBar();
    if (state.currentPreviewId && changed.includes(state.currentPreviewId)) {
      updatePreview({ skipImage: true });
    }
    return;
  }

  // A row entered or left the filter. Rebuild, keeping the viewport.
  const scrollY = window.scrollY;
  const kept = current.filter((p) => !nextPhotos.some((fresh) => fresh.id === p.id));
  indexPhotos(nextPhotos.concat(kept));
  const ids = new Set(state.photos.map((p) => p.id));
  for (const id of [...state.selected]) {
    if (!ids.has(id)) state.selected.delete(id);
  }
  renderGallery({ animate: false });
  window.scrollTo({ top: scrollY, left: 0, behavior: "instant" });
  updateSelectionBar();
  if (state.currentPreviewId && !state.photoById.has(state.currentPreviewId)) {
    applyPreviewAfterRemoval(null);
  }
}

async function loadMorePhotos() {
  if (state.loadingMore || !state.hasMore) return;
  state.loadingMore = true;
  $("loadMoreBtn").classList.add("loading");
  try {
    const params = getFilterParams(state.offset);
    const data = await api(`/api/photos?${params.toString()}`);
    const nextPhotos = data.photos || [];
    indexPhotos(state.photos.concat(nextPhotos));
    state.offset += nextPhotos.length;
    state.hasMore = !!data.has_more;
    renderGallery({ append: true, animate: true });
    updateLoadMoreButton();
  } catch (e) {
    showToast("加载更多照片失败：" + e.message, "error");
  } finally {
    state.loadingMore = false;
    $("loadMoreBtn").classList.remove("loading");
  }
}

function updateLoadMoreButton() {
  $("loadMoreBtn").classList.toggle("hidden", !state.hasMore);
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

/**
 * Build the markup for one card.
 *
 * Split out of `renderGallery` so a row that changed (a favourite toggled, a
 * score arrived) can be refreshed without rebuilding the whole grid: the same
 * function paints the replacement card.
 */
function cardHtml(photo) {
  const isDeleted = photo.status === "deleted";
  const isVideo = photo.media_kind === "video";
  const dims = photo.dimensions || {};
  const tagsHtml = (photo.tags || []).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("");
  const rawBadge = rawBadgeHtml(photo, "card");

  // A photo that has only been indexed has no score yet. Printing "总分 -"
  // for it is noise, so the whole score row is dropped until there is
  // something to show. The sub-scores keep their own rule: they appear only
  // when the breakdown is actually meaningful.
  const hasScore = photo.score !== null && photo.score !== undefined;
  const scorePills = hasScore
    ? `<div class="score-pills">
            <span class="score-pill total">总分 ${scoreText(photo.score)}</span>
            ${dimensionValues(dims, photo.score)}
          </div>`
    : "";

  let deleteActions = "";
  if (isDeleted) {
    // A trashed photo is not analysed again; offering the button would only
    // invite the model to describe a file that is on its way out.
    deleteActions = `
      <button class="btn small restore-btn" title="恢复"><span class="btn-icon">${ICONS.restore}</span>恢复</button>
      <button class="btn small danger permanent-btn" title="彻底移除"><span class="btn-icon">${ICONS.close}</span></button>
    `;
  } else {
    deleteActions = `
      <button class="btn small danger delete-btn" title="暂时收起"><span class="btn-icon">${ICONS.trash}</span></button>
    `;
  }
  // A video is analysed now, from its poster frame, so it keeps the button.
  const reanalyzeAction = isDeleted
    ? ""
    : `<button class="btn small reanalyze-btn" title="重新解读"><span class="btn-icon">${ICONS.refresh}</span></button>`;

  // A video card is a strip of film, not a polaroid: dark stock, sprocket holes
  // down both edges, and a play mark on the frame. It keeps the same box, the
  // same entrance animation and the same hover lift, so the grid still reads as
  // one collection - just a different material.
  return `
    <div class="polaroid-inner">
      <div class="polaroid-front">
        ${isVideo ? '<div class="film-strip" aria-hidden="true"></div>' : '<div class="tape"></div>'}
        <button class="favorite-btn ${photo.favorite ? "active" : ""}" title="${photo.favorite ? "取消珍藏" : "珍藏"}">${photo.favorite ? ICONS.heartFilled : ICONS.heart}</button>
        ${rawBadge}
        ${liveBadgeHtml(photo)}
        <div class="polaroid-photo-frame"><img class="polaroid-photo" data-src="${thumbnailUrl(photo)}" alt="${escapeHtml(photo.filename)}" />${
          isVideo
            ? `<span class="video-play-mark">${ICONS.play || ""}</span>${videoDurationBadge(photo)}`
            : ""
        }</div>
        <div class="polaroid-body">
          ${photo.location ? `<div class="photo-location">${ICONS.pin} <span>${escapeHtml(photo.location)}</span></div>` : ""}
          ${photo.reason ? `<div class="photo-comment">${ICONS.quote} ${escapeHtml(photo.reason)}</div>` : ""}
          <div class="back-tags">${tagsHtml || ""}</div>
          ${scorePills}
          <div class="card-actions">
            <label class="card-check" title="选择">
              <input type="checkbox"${state.selected.has(photo.id) ? " checked" : ""} />
            </label>
            <span class="card-btn-group">
              ${reanalyzeAction}
              ${deleteActions}
            </span>
          </div>
        </div>
      </div>
    </div>
  `;
}

/**
 * The thumbnail URL for a card.
 *
 * ``v`` is the row's content hash, which changes whenever the JPEG is
 * re-rendered, so the URL can be cached forever (see `_image_cache_timeout`).
 * That is what makes scrolling a long album up and down cost no requests.
 */
function thumbnailUrl(photo) {
  const version = photo.thumb_version ? `?v=${encodeURIComponent(photo.thumb_version)}` : "";
  return `/api/thumbnail/${photo.id}${version}`;
}

/* ==========================================================================
   Card lifecycle: thumbnails and off-screen culling
   Two costs, one observer plus one sweep.

   Every decoded <img> keeps its bitmap in memory until the element is gone -
   `loading="lazy"` only defers the *first* load - so a 4000-photo album held
   ~3 GB of decoded pixels after one scroll through it. And every tilted card is
   its own composited layer, so a few thousand of them make each scroll frame
   re-rasterise the lot.

   Entering cards get their `src` (a content-addressed URL, so coming back to a
   card is served from the browser cache) and leave `is-culled`; leaving cards
   drop the bitmap and take `is-culled` so the browser can skip their layout,
   paint and compositing. The margin is wide enough that every switch happens
   well outside the viewport: the tilt, the shadows, the tape and the hover lift
   are exactly what they were.

   The observer alone is not enough. Its callbacks describe where a card was
   when it was observed, and after a jump - clicking "回到顶部", a search result,
   a browser find - the queued entries are stale, so cards that are now on
   screen can stay culled and look blank. `sweepCardState` runs once the scroll
   has settled and re-derives the whole grid from the current positions, which
   makes the state self-correcting whatever the observer managed to deliver.
   ========================================================================== */

//: How far outside the viewport a card keeps its bitmap and its tilt.
const CARD_ROOT_MARGIN = "1500px 0px";
//: The same margin as a number, for the geometry sweep.
const CARD_MARGIN_PX = 1500;
//: Below this many loaded cards the bookkeeping costs more than it saves.
const CARD_CULL_MIN_CARDS = 120;
//: How long the scroll must be still before the sweep runs.
const CARD_SWEEP_DELAY_MS = 120;

let cardObserver = null;
let cardSweepTimer = null;

function cardObserverInstance() {
  if (cardObserver || typeof IntersectionObserver === "undefined") return cardObserver;
  cardObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        const img = entry.target;
        const card = img.closest(".polaroid-card");
        if (entry.isIntersecting) {
          attachThumb(img);
          if (card) {
            card.dataset.seen = "1";
            card.classList.remove("is-culled");
          }
        } else {
          if (shouldReleaseThumb(img)) releaseThumb(img);
          if (card && card.dataset.seen && state.photos.length >= CARD_CULL_MIN_CARDS) {
            card.classList.add("is-culled");
          }
        }
      }
      if (!entries.length) return;
      scheduleCardSweep();
    },
    { rootMargin: CARD_ROOT_MARGIN }
  );
  return cardObserver;
}

function attachThumb(img) {
  const src = img.dataset.src;
  if (!src || img.getAttribute("src")) return;
  img.setAttribute("src", src);
}

/** Should this card's bitmap be given back now that it is far away? */
function shouldReleaseThumb(img) {
  if (!img.getAttribute("src")) return false;
  return state.photos.length >= CARD_CULL_MIN_CARDS;
}

function releaseThumb(img) {
  img.removeAttribute("src");
  img.classList.remove("is-loaded");
}

/**
 * Queue a geometry sweep for when the scroll settles.
 *
 * Debounced rather than rAF-throttled: the sweep reads every card's position,
 * which is only worth doing once, after the user has stopped moving.
 */
function scheduleCardSweep() {
  if (cardSweepTimer) clearTimeout(cardSweepTimer);
  cardSweepTimer = setTimeout(() => {
    cardSweepTimer = null;
    sweepCardState();
  }, CARD_SWEEP_DELAY_MS);
}

/**
 * Bring every card's culled state back in line with where it actually is.
 *
 * Two passes on purpose: the positions are read first, in one layout, and only
 * then are classes changed. Interleaving the two would force a fresh layout per
 * card. Cards are read in DOM order, so a collapsed card earlier in the list
 * shifts the ones after it; their heights are stable to within the placeholder
 * size, so a sweep can only ever be one iteration behind, and the next one (the
 * observer fires again after any class change) settles it.
 */
function sweepCardState() {
  const gallery = $("gallery");
  if (!gallery || !state.photos.length) return;
  const cards = gallery.children;
  if (!cards.length) return;
  const enough = state.photos.length >= CARD_CULL_MIN_CARDS;
  const viewport = window.innerHeight || document.documentElement.clientHeight;
  const top = -CARD_MARGIN_PX;
  const bottom = viewport + CARD_MARGIN_PX;

  const near = new Array(cards.length);
  for (let i = 0; i < cards.length; i += 1) {
    const rect = cards[i].getBoundingClientRect();
    near[i] = rect.bottom > top && rect.top < bottom;
  }
  for (let i = 0; i < cards.length; i += 1) {
    const card = cards[i];
    const img = card.querySelector(".polaroid-photo");
    if (near[i]) {
      if (img && !img.getAttribute("src")) attachThumb(img);
      card.dataset.seen = "1";
      card.classList.remove("is-culled");
    } else if (enough && card.dataset.seen) {
      if (img && shouldReleaseThumb(img)) releaseThumb(img);
      card.classList.add("is-culled");
    }
  }
}

/** Start observing a card's image (called for every card that enters the DOM). */
function watchThumb(card) {
  const img = card.querySelector(".polaroid-photo");
  if (!img) return;
  const observer = cardObserverInstance();
  if (observer) observer.observe(img);
  // No IntersectionObserver (very old browser): load everything and keep every
  // card rendered, which is the behaviour the app had before.
  else attachThumb(img);
}

function renderGallery(options = {}) {
  const gallery = $("gallery");
  const empty = $("emptyState");
  if (!options.append) {
    gallery.innerHTML = "";
    state.renderedCount = 0;
  }
  if (!state.photos.length) {
    empty.classList.remove("hidden");
    updateLoadMoreButton();
    return;
  }
  empty.classList.add("hidden");

  const startIndex = options.append ? state.renderedCount : 0;
  const fragment = document.createDocumentFragment();
  state.photos.slice(startIndex).forEach((photo, offset) => {
    const idx = startIndex + offset;
    const card = document.createElement("div");
    card.className = photo.media_kind === "video" ? "polaroid-card is-video" : "polaroid-card";
    if (options.animate === false) {
      card.style.animation = "none";
    } else {
      card.style.animationDelay = `${(idx % 16) * 0.025}s`;
    }
    card.dataset.id = photo.id;
    card.innerHTML = cardHtml(photo);
    fragment.appendChild(card);
    watchThumb(card);
  });
  // One insertion instead of N: the browser lays the new cards out once.
  gallery.appendChild(fragment);
  state.renderedCount = state.photos.length;
  // New cards have never been positioned; let the sweep decide which of them
  // are far enough away to cull straight away.
  scheduleCardSweep();
}

/** Rebuild one card in place, keeping its position and selection state. */
function refreshCard(id) {
  const card = $("gallery").querySelector(`.polaroid-card[data-id="${id}"]`);
  const photo = photoById(id);
  if (!card || !photo) return;
  card.style.animation = "none";
  const observer = cardObserverInstance();
  const oldImg = card.querySelector(".polaroid-photo");
  if (observer && oldImg) observer.unobserve(oldImg);
  card.classList.toggle("is-video", photo.media_kind === "video");
  card.innerHTML = cardHtml(photo);
  watchThumb(card);
}

/**
 * Rebuild every rendered card without losing the scroll position.
 *
 * Kept for the operations that change many rows at once (a batch delete, a
 * batch re-analysis); single-row changes go through `refreshCard`.
 */
function rerenderLoadedGallery() {
  const scrollY = window.scrollY;
  renderGallery({ animate: false });
  window.scrollTo({ top: scrollY, left: 0, behavior: "instant" });
}

/**
 * Drop rows from the loaded list and their cards from the grid.
 *
 * Deleting or trashing photos used to filter `state.photos` and rebuild every
 * card, which at 3000 photos meant a 300 ms stall and a fresh decode of every
 * thumbnail still on screen. Removing just the affected cards keeps the work
 * proportional to how many photos actually changed.
 */
function removePhotoRows(ids) {
  const removed = new Set(ids);
  if (!removed.size) return;
  indexPhotos(state.photos.filter((p) => !removed.has(p.id)));
  const gallery = $("gallery");
  const observer = cardObserverInstance();
  for (const id of removed) {
    const card = gallery.querySelector(`.polaroid-card[data-id="${id}"]`);
    if (!card) continue;
    const img = card.querySelector(".polaroid-photo");
    if (observer && img) observer.unobserve(img);
    card.remove();
    state.selected.delete(id);
  }
  state.renderedCount = state.photos.length;
  if (!state.photos.length) renderGallery({ animate: false });
}

function updateCardFavorite(id) {
  const photo = photoById(id);
  const card = document.querySelector(`.polaroid-card[data-id="${id}"]`);
  if (!photo || !card) return;
  const btn = card.querySelector(".favorite-btn");
  btn.classList.toggle("active", !!photo.favorite);
  btn.title = photo.favorite ? "取消珍藏" : "珍藏";
  btn.innerHTML = photo.favorite ? ICONS.heartFilled : ICONS.heart;
}

/* ==========================================================================
   Gallery events
   One listener per event type on the grid, instead of six per card. At 4000
   cards the old scheme registered 24,000 closures, all of them kept alive for
   as long as the cards were on the page.
   ========================================================================== */

function initGalleryEvents() {
  const gallery = $("gallery");
  if (!gallery) return;

  // After any scroll - wheel, drag, keyboard or the back-to-top animation -
  // re-derive the culled state once things are still.
  window.addEventListener("scroll", scheduleCardSweep, { passive: true });
  window.addEventListener("resize", scheduleCardSweep);

  gallery.addEventListener("click", async (e) => {
    const card = e.target.closest(".polaroid-card");
    if (!card) return;
    const id = Number(card.dataset.id);
    const photo = photoById(id);
    if (!photo) return;

    if (e.target.closest(".favorite-btn")) {
      await toggleCardFavorite(photo);
      return;
    }
    if (e.target.closest(".reanalyze-btn")) {
      await reanalyze(id);
      return;
    }
    if (e.target.closest(".delete-btn")) {
      await deletePhotos([id]);
      return;
    }
    if (e.target.closest(".restore-btn")) {
      await restorePhotos([id]);
      return;
    }
    if (e.target.closest(".permanent-btn")) {
      await permanentDeletePhotos([id]);
      return;
    }
    if (e.target.closest("button") || e.target.closest("input") || e.target.closest("a")) {
      return;
    }
    openPreview(id);
  });

  gallery.addEventListener("change", (e) => {
    const checkbox = e.target.closest(".card-check input");
    if (!checkbox) return;
    const card = checkbox.closest(".polaroid-card");
    const id = Number(card && card.dataset.id);
    if (!Number.isFinite(id)) return;
    if (checkbox.checked) state.selected.add(id);
    else state.selected.delete(id);
    updateSelectionBar();
  });

  // `load`/`error` do not bubble, so they are caught on the way down.
  gallery.addEventListener(
    "load",
    (e) => {
      if (e.target.classList && e.target.classList.contains("polaroid-photo")) {
        e.target.classList.add("is-loaded");
      }
    },
    true
  );
  gallery.addEventListener(
    "error",
    (e) => {
      if (e.target.classList && e.target.classList.contains("polaroid-photo")) {
        e.target.classList.add("is-loaded");
      }
    },
    true
  );
}

/** Flip one photo's favourite flag and repaint just that card. */
async function toggleCardFavorite(photo) {
  const newVal = !photo.favorite;
  try {
    await api("/api/favorite", {
      method: "POST",
      body: JSON.stringify({ id: photo.id, favorite: newVal }),
    });
    photo.favorite = newVal;
    updateCardFavorite(photo.id);
    loadStats();
    if ($("favoriteFilter").value === "1" && !newVal) {
      await loadPhotos();
    }
  } catch (err) {
    showToast("珍藏操作失败：" + err.message, "error");
  }
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
  saveUiState({ folder });
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
  saveUiState({ folder });
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
  // A new job must not inherit the previous run's speed estimate.
  state.eta = null;
}

function hideProgress() {
  $("scanProgress").classList.add("hidden");
  $("progressEta").classList.add("hidden");
  state.eta = null;
  state.currentJobId = null;
  if (state.pollingTimer) {
    clearTimeout(state.pollingTimer);
    state.pollingTimer = null;
  }
}

/* ==========================================================================
   Progress + remaining-time estimate
   ========================================================================== */

//: How strongly a fresh sample pulls the rate estimate. Small enough that one
//: slow file cannot swing the number, large enough to follow a real change.
const ETA_RATE_ALPHA = 0.2;
//: Weight of the whole-run average against the recent-rate average. The overall
//: average is what the user asked for - it is the steady signal - while the
//: recent rate is what notices that the job got faster or slower.
const ETA_OVERALL_WEIGHT = 0.6;
//: The displayed estimate is smoothed as well, so a wobble in the underlying
//: rate does not make the pill flicker between two values.
const ETA_DISPLAY_ALPHA = 0.35;
//: A new sample is ignored if it arrives sooner than this; at a one-second poll
//: interval the deltas would otherwise be mostly rounding noise.
const ETA_MIN_SAMPLE_SECONDS = 0.75;

/**
 * Track how fast a job is moving and estimate what is left.
 *
 * Two rates are combined. The **overall** rate (``processed / elapsed``) is the
 * average speed of everything done so far and is the stable part of the number;
 * ``elapsed`` comes from the server, so it includes the time before a page
 * refresh. The **recent** rate is an exponential moving average of the samples
 * since the page last loaded, which is what notices that the job speeded up or
 * slowed down.
 *
 * Mixing them is what removes the jitter: a single slow file moves the recent
 * rate but barely dents the average, so the estimate drifts instead of jumping.
 * Progress only ever moves forward, so a stale sample can never push the
 * estimate back up.
 */
function updateEta(processed, total, elapsedSeconds) {
  const now = performance.now();
  const previous = state.eta;
  const overallElapsed = Number(elapsedSeconds);

  if (!previous || !Number.isFinite(previous.processed) || processed < previous.processed) {
    state.eta = seedEta(processed, total, now, overallElapsed);
    const seeded = state.eta.rate;
    if (total > 0 && processed >= total) {
      // Already done: say so rather than leave a stale estimate standing.
      paintEta(0);
    } else if (seeded > 0 && total > 0 && processed > 0) {
      // A resumed job already knows how fast it has been going, so the estimate
      // appears on the first poll after a refresh instead of a tick later.
      paintEta(smoothEta((total - processed) / seeded));
    } else {
      paintEta(null);
    }
    return;
  }

  const seconds = (now - previous.at) / 1000;
  const delta = processed - previous.processed;
  const rate = previous.rate;

  if (seconds < ETA_MIN_SAMPLE_SECONDS && delta === 0) {
    // Nothing to learn yet; keep the current estimate on screen.
    return;
  }

  let recent = previous.recent;
  if (delta > 0 && seconds > 0) {
    const sample = delta / seconds;
    recent = recent > 0 ? recent * (1 - ETA_RATE_ALPHA) + sample * ETA_RATE_ALPHA : sample;
  }

  // The overall rate is recomputed from the server's elapsed time every tick,
  // so it stays correct across a refresh instead of restarting from zero.
  const overall =
    Number.isFinite(overallElapsed) && overallElapsed > 0 && processed > 0
      ? processed / overallElapsed
      : 0;

  let combined;
  if (overall > 0 && recent > 0) {
    combined = overall * ETA_OVERALL_WEIGHT + recent * (1 - ETA_OVERALL_WEIGHT);
  } else {
    combined = overall || recent || rate || 0;
  }

  state.eta = {
    processed,
    total,
    at: now,
    rate: combined,
    recent,
    shown: previous.shown,
    shownSeconds: previous.shownSeconds,
  };

  if (!combined || !total || processed <= 0 || processed >= total) {
    paintEta(processed >= total ? 0 : null);
    return;
  }
  paintEta(smoothEta((total - processed) / combined));
}

/** First sample for a job: reuse whatever the server knows about its speed. */
function seedEta(processed, total, now, elapsedSeconds) {
  let rate = 0;
  if (Number.isFinite(elapsedSeconds) && elapsedSeconds > 0 && processed > 0) {
    rate = processed / elapsedSeconds;
  }
  return {
    processed,
    total,
    at: now,
    rate,
    recent: 0,
    shown: state.eta ? state.eta.shown : null,
    shownSeconds: state.eta ? state.eta.shownSeconds : null,
  };
}

/** Ease the displayed seconds towards the new estimate. */
function smoothEta(seconds) {
  const previous = state.eta && state.eta.shownSeconds;
  if (!Number.isFinite(previous)) return seconds;
  return previous + (seconds - previous) * ETA_DISPLAY_ALPHA;
}

/** How a duration reads at a glance: "预计 12 秒" / "预计 3 分 20 秒". */
function formatEta(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "";
  const total = Math.max(0, Math.round(seconds));
  if (total < 5) return "即将完成";
  if (total < 60) return `预计 ${total} 秒`;
  if (total < 3600) {
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return rest ? `预计 ${minutes} 分 ${rest} 秒` : `预计 ${minutes} 分`;
  }
  const hours = Math.floor(total / 3600);
  const minutes = Math.round((total % 3600) / 60);
  return minutes ? `预计 ${hours} 小时 ${minutes} 分` : `预计 ${hours} 小时`;
}

/**
 * Paint (or hide) the ETA pill next to the progress label.
 *
 * The entrance animation plays once, when the pill first appears, and not again
 * on every value change: a badge that re-animates on each poll reads as flicker
 * rather than as a live number. The text itself is compared against what is
 * already on screen, so the pill never re-renders the same string.
 */
function paintEta(seconds) {
  const el = $("progressEta");
  if (!el) return;
  const value = seconds === 0 ? 0 : seconds;
  const text = value === 0 ? "即将完成" : formatEta(value);

  if (state.eta) {
    state.eta.shown = text || null;
    if (Number.isFinite(value)) state.eta.shownSeconds = value;
  }

  if (!text) {
    el.classList.add("hidden");
    el.textContent = "";
    return;
  }

  const wasHidden = el.classList.contains("hidden");
  if (el.textContent !== text) el.textContent = text;
  if (!wasHidden) return;

  // First appearance (including right after a page refresh): slide it in.
  el.classList.remove("hidden");
  el.style.animation = "none";
  void el.offsetWidth;
  el.style.animation = "";
}

/**
 * Drive the toolbar progress bar from a job payload.
 *
 * Shared by the scan poller and the one-shot jobs (emptying the bin, a card
 * sync), so every progress bar in the app reports the same way.
 */
function paintJobProgress(job) {
  const total = Number(job.total) || 0;
  const processed = Number(job.processed) || 0;
  const pct = total ? Math.round((processed / total) * 100) : 0;
  $("progressFill").style.width = pct + "%";
  if (job.status === "completed") {
    // Nothing left to wait for; the summary line replaces the estimate.
    paintEta(0);
  } else {
    // ``elapsed`` is the server's clock for the whole job, so a refreshed page
    // recomputes the average speed from the real start instead of from zero.
    updateEta(processed, total, Number(job.elapsed));
  }
}

function pollScan() {
  if (!state.currentJobId) return;
  api(`/api/scan/status/${state.currentJobId}`)
    .then((job) => {
      if (job.status === "completed" || job.status === "cancelled" || job.status === "error") {
        paintJobProgress(job);
        $("progressText").textContent = jobSummaryText(job);
        setTimeout(hideProgress, job.report ? 6000 : 1500);
        state.currentJobId = null;
        loadPhotos().then(() => {
          // The photos that were on screen may be gone now; whichever view is
          // open has to follow. loadPhotos already re-reads the gallery, so this
          // only has to handle the dialog.
          if (!$("previewModal").classList.contains("hidden")) {
            const still = state.photos.some((p) => p.id === state.currentPreviewId);
            if (!still) closeModal("previewModal");
            else updatePreview({ skipImage: true });
          }
        });
        // An import is what turns this folder into a camera album, and it may
        // have arrived on a card that already has deletions queued.
        const imported = job.report && job.report.card_album;
        refreshCardStatus().then(() => {
          if (imported && imported.source) offerCardSync(imported.source);
        });
        return;
      }
      paintJobProgress(job);
      $("progressText").textContent = progressLabel(job);
      state.pollTick += 1;
      loadStats();
      if (state.pollTick % 4 === 0 && !$("searchInput").value.trim()) {
        loadPhotos({ quiet: true });
      }
      state.pollingTimer = setTimeout(pollScan, 1000);
    })
    .catch((e) => {
      $("progressText").textContent = "读取进度失败：" + e.message;
      paintEta(null);
      setTimeout(hideProgress, 2000);
    });
}

/**
 * One-line description of where a job is.
 *
 * ``phase`` covers the scan/import pipeline; the "trash" phase is the bin sweep,
 * which counts photos rather than files and reads better on its own.
 */
function progressLabel(job) {
  if (job.phase === "trash") {
    const total = Number(job.total) || 0;
    if (!total) return "正在清空回收站…";
    const name = job.current ? `：${job.current}` : "";
    return `正在清空回收站 ${job.processed}/${total}${name}`;
  }
  const total = Number(job.total) || 0;
  if (!total) return `${phaseLabel(job.phase)}…`;
  return `${phaseLabel(job.phase)} ${job.processed}/${total}${job.current ? "：" + job.current : ""}`;
}

const PHASE_LABELS = {
  plan: "正在比对",
  copy: "正在拷贝",
  index: "正在索引",
  analyze: "正在解读",
};

function phaseLabel(phase) {
  return PHASE_LABELS[phase] || "整理照片";
}

/**
 * Build the end-of-job message.
 *
 * Import jobs carry a report and get a summary of what actually changed; scan
 * jobs get a plain processed/total line.
 *
 * Three things used to read badly here. Zero counters were printed as
 * "覆盖 0" on every run. The follow-up was spelled by stripping "正在" off a
 * phase label, which produced "已索引" with no hint of what that meant. And a
 * cancelled import was announced as "导入完成". Zero counters are now dropped,
 * the follow-up says what actually happened to the photos, and the job's own
 * status drives the opening words.
 */
function jobSummaryText(job) {
  if (job.status === "error") return "任务出错：" + (job.error || "");

  const report = job.report;
  const isImport = report && typeof report === "object" && "copied" in report;

  if (isImport) {
    const cancelled = job.status === "cancelled" || report.cancelled;
    if (cancelled) {
      return report.copied
        ? `导入已取消：已拷贝 ${report.copied} 个文件`
        : "导入已取消：还没有拷贝文件";
    }

    const copied = Number(report.copied) > 0;
    const skipped = Number(report.skip_count) > 0 ? Number(report.skip_count) : 0;
    // "跳过" describes work that did not need doing, so it only belongs next to
    // a real result; on its own the line reads as if nothing happened at all.
    const counts = [
      ["新增", report.new_count],
      ["覆盖", report.overwrite_count],
    ]
      .filter(([, value]) => Number(value) > 0)
      .map(([label, value]) => `${label} ${value}`);
    if (copied && skipped) counts.push(`跳过 ${skipped}`);

    const head = counts.length
      ? `导入完成：${counts.join("，")}`
      : skipped
        ? `导入完成：所有文件都已是最新（跳过 ${skipped}）`
        : "导入完成：所有文件都已是最新";
    // What happened *after* the copy matters: an import that stopped at
    // "建索引" leaves the photos unscored, and the user should see that here
    // rather than wonder why nothing has a score yet.
    const followUp = {
      index: "已建立索引，尚未调用模型",
      analyze: "已索引并完成模型解读",
    }[job.phase];
    const failure = (report.failed || []).length
      ? `，${report.failed.length} 个文件失败`
      : "";
    const tail = followUp ? `，${followUp}` : "";
    return `${head}${failure}${tail}`;
  }

  if (job.phase === "trash") {
    const removed = report && report.deleted;
    if (job.status === "cancelled") return "清空回收站已取消";
    const raw = report && report.raw_deleted ? `，含 ${report.raw_deleted} 个 RAW` : "";
    return removed
      ? `回收站已清空，共移除 ${removed} 张照片${raw}`
      : "回收站已经是空的";
  }

  if (job.status === "cancelled") return `${phaseLabel(job.phase)}已取消`;
  if (!job.total) return "整理完成：没有需要处理的照片";
  return `整理完成：共处理 ${job.processed}/${job.total}`;
}

async function deletePhotos(ids) {
  ids = ids.filter((id) => {
    const p = photoById(id);
    return p && p.status !== "deleted";
  });
  if (!ids.length) return;
  await refreshCardStatus();
  const card = state.card;
  const canSync = !!(card && card.usable);
  let confirmMessage;
  if (ids.length === 1) {
    const photo = photoById(ids[0]);
    confirmMessage = `确定把“${photo ? photo.filename : "这张照片"}”暂时收起吗？`;
  } else {
    confirmMessage = `确定把选中的 ${ids.length} 张照片暂时收起吗？`;
  }
  if (card && !canSync) {
    const why = {
      disconnected: "SD 卡当前未连接",
      read_only: "SD 卡处于写保护状态",
      volume_mismatch: "该路径现在不是上次那张卡",
    }[card.state] || "SD 卡当前不可用";
    confirmMessage += `\n${why}，这次删除会先记录下来，等卡片再次连接时可以一并同步。`;
  } else if (canSync) {
    confirmMessage += `\n这是相机相册，可以同时从 ${card.source} 移除。`;
  }
  const answer = await confirmDialog(confirmMessage, {
    title: "暂时收起",
    confirmText: "暂时收起",
    danger: true,
    checkbox: canSync
      ? { label: "同步删除 SD 卡中的照片", checked: true }
      : null,
  });
  const confirmed = typeof answer === "object" ? answer.confirmed : answer;
  if (!confirmed) return;
  const syncCard = typeof answer === "object" ? answer.checked : false;
  // Decide where the detail dialog goes *before* the photos are filtered:
  // the "next photo" is the one that followed the current one in the album.
  const previewNext = nextPreviewAfterRemoval(ids);
  try {
    const data = await api("/api/delete", {
      method: "POST",
      body: JSON.stringify({ ids, sync_card: syncCard }),
    });
    if (data.errors && data.errors.length) {
      showToast("部分照片暂时收起失败：" + data.errors.map((e) => e.error).join("; "), "error");
    }
    reportCardResult(data.card, "删除");
    removePhotoRows(ids);
    state.selected.clear();
    updateSelectionBar();
    loadStats();
    applyPreviewAfterRemoval(previewNext);
    await refreshCardStatus();
  } catch (e) {
    showToast("暂时收起失败：" + e.message, "error");
  }
}

async function permanentDeletePhotos(ids) {
  ids = ids.filter((id) => {
    const p = photoById(id);
    return p && p.status === "deleted";
  });
  if (!ids.length) return;
  const hasRaw = ids.some((id) => {
    const p = photoById(id);
    return p && (p.has_raw || p.is_raw);
  });
  const answer = await confirmDialog(
    `确定彻底移除 ${ids.length} 张照片？\n文件将从磁盘移除，且不可恢复。`,
    {
      title: "彻底移除",
      confirmText: "彻底移除",
      danger: true,
      // A raw file trashed alongside the photo is deleted with it by default,
      // exactly like emptying the bin does.
      checkbox: hasRaw ? { label: "同时删除 RAW", checked: true } : null,
    }
  );
  const confirmed = typeof answer === "object" ? answer.confirmed : answer;
  if (!confirmed) return;
  const deleteRaw = typeof answer === "object" ? answer.checked : true;
  const previewNext = nextPreviewAfterRemoval(ids);
  try {
    const data = await api("/api/delete/permanent", {
      method: "POST",
      body: JSON.stringify({ ids, delete_raw: deleteRaw }),
    });
    if (data.errors && data.errors.length) {
      showToast("部分照片彻底移除失败：" + data.errors.map((e) => e.error).join("; "), "error");
    }
    if (data.raw_removed) {
      showToast(`已彻底移除，含 ${data.raw_removed} 个 RAW 文件。`, "success");
    }
    removePhotoRows(ids);
    state.selected.clear();
    updateSelectionBar();
    loadStats();
    applyPreviewAfterRemoval(previewNext);
  } catch (e) {
    showToast("彻底移除失败：" + e.message, "error");
  }
}

async function restorePhotos(ids) {
  ids = ids.filter((id) => {
    const p = photoById(id);
    return p && p.status === "deleted";
  });
  if (!ids.length) return;
  await refreshCardStatus();
  const card = state.card;
  // Restoring only touches the card when a deletion actually reached it; a
  // card that is away queues the undo for its next connection.
  const syncCard = !!(card && card.usable);
  const previewNext = nextPreviewAfterRemoval(ids);
  try {
    const data = await api("/api/restore", {
      method: "POST",
      body: JSON.stringify({ ids, sync_card: syncCard }),
    });
    if (data.errors && data.errors.length) {
      showToast("部分照片恢复失败：" + data.errors.map((e) => e.error).join("; "), "error");
    }
    reportCardResult(data.card, "恢复");
    removePhotoRows(ids);
    state.selected.clear();
    updateSelectionBar();
    loadStats();
    applyPreviewAfterRemoval(previewNext);
    await refreshCardStatus();
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
    // visible without reloading the whole list.
    const idx = photoIndexById(id);
    if (idx >= 0 && updated) {
      if ($("statusFilter").value === "pending") {
        removePhotoRows([id]);
      } else {
        // Same row, new contents: one card is repainted, the grid is not.
        Object.assign(state.photos[idx], updated);
        invalidatePhotoDetail(id);
        refreshCard(id);
      }
    }

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

/**
 * Which photo should be open once ``removedIds`` leave the album.
 *
 * Returns the id to keep showing, or ``null`` when the dialog should close.
 * Called *before* ``state.photos`` is filtered, because that list is what says
 * what came after the photo on screen.
 *
 * Only the next photo counts - the album's order is meaningful, and jumping
 * backwards would be disorienting. Deleting the last photo therefore closes the
 * dialog, which is also what the user asked for.
 */
function nextPreviewAfterRemoval(removedIds) {
  const currentId = state.currentPreviewId;
  if (!currentId) return null;
  const removed = new Set(removedIds);
  if (!removed.has(currentId)) return currentId;
  const order = state.photos.map((p) => p.id);
  const index = order.indexOf(currentId);
  for (let i = index + 1; i < order.length; i += 1) {
    if (!removed.has(order[i])) return order[i];
  }
  return null;
}

/**
 * Move the open detail dialog onto ``nextId`` (or close it).
 *
 * Runs after the gallery has been re-rendered, so ``state.photos`` already
 * reflects the removal and ``updatePreview`` can find the new photo.
 */
function applyPreviewAfterRemoval(nextId) {
  if (!state.currentPreviewId) return;
  if (nextId === null || nextId === undefined) {
    closeModal("previewModal");
    return;
  }
  if (nextId === state.currentPreviewId) {
    updatePreview({ skipImage: true });
    return;
  }
  state.currentPreviewId = nextId;
  state.previewNavToken += 1;
  const img = $("previewImage");
  img.classList.remove("preview-switch-left", "preview-switch-right");
  img.src = `/api/proxy/${nextId}`;
  // Force a reflow so the slide-in animation replays for the new photo.
  void img.offsetWidth;
  img.classList.add("preview-switch-right");
  updatePreview({ skipImage: true });
}

async function navigatePreview(delta) {
  let order = visiblePhotoOrder();
  if (!order.length) return;
  let idx = order.indexOf(state.currentPreviewId);
  if (idx < 0) return;

  // At the end of the loaded pages, fetch the next page before wrapping
  // back to the first card.
  if (delta > 0 && idx === order.length - 1 && state.hasMore) {
    const loadedBefore = state.photos.length;
    await loadMorePhotos();
    if (state.photos.length > loadedBefore) {
      order = visiblePhotoOrder();
      idx = order.indexOf(state.currentPreviewId);
      if (idx < 0) return;
    }
  }

  const nextId = order[(idx + delta + order.length) % order.length];
  const token = ++state.previewNavToken;
  const url = `/api/proxy/${nextId}`;
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

/* ==========================================================================
   Folder history dropdown (the chevron inside the path field)
   ========================================================================== */

function folderHistoryOpen() {
  return !$("folderHistory").classList.contains("hidden");
}

function closeFolderHistory() {
  const panel = $("folderHistory");
  const toggle = $("folderHistoryBtn");
  if (toggle) {
    toggle.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
  }
  if (panel.classList.contains("hidden")) return;
  // Let the lift-out play before the panel is taken out of flow. The duration
  // matches ``historyLift`` in style.css. Re-opening cancels the timer, so a
  // quick toggle cannot hide a panel that has just been shown again.
  panel.classList.add("closing");
  if (state.historyCloseTimer) clearTimeout(state.historyCloseTimer);
  state.historyCloseTimer = setTimeout(() => {
    state.historyCloseTimer = null;
    panel.classList.add("hidden");
    panel.classList.remove("closing");
  }, 170);
}

/** Cancel a pending close so the panel can be re-opened without a flicker. */
function cancelFolderHistoryClose() {
  if (state.historyCloseTimer) {
    clearTimeout(state.historyCloseTimer);
    state.historyCloseTimer = null;
  }
}

/**
 * Merge "recently used" with "currently indexed" into one de-duplicated list.
 *
 * ``folder_history`` keeps the newest entry first, so only its head is really
 * "the last one used" - tagging every remembered folder "最近" said nothing.
 * That one goes to the top; the rest are simply indexed albums, and a folder
 * that is both keeps both badges.
 */
function folderHistoryEntries(indexed) {
  const entries = [];
  const byKey = new Map();
  const push = (path, tag) => {
    const clean = String(path || "").trim();
    if (!clean) return;
    const key = folderPathKey(clean);
    const existing = byKey.get(key);
    if (existing) {
      if (tag) existing.tags.add(tag);
      return;
    }
    const entry = { key, path: clean, tags: new Set(tag ? [tag] : []) };
    byKey.set(key, entry);
    entries.push(entry);
  };
  push(state.folderHistory[0], "最近");
  (indexed || []).forEach((f) => push(f, "已索引"));
  return entries;
}

async function toggleFolderHistory() {
  if (folderHistoryOpen()) {
    closeFolderHistory();
    return;
  }
  const panel = $("folderHistory");
  let indexed = [];
  try {
    const data = await api("/api/folders");
    indexed = data.folders || [];
  } catch (e) {
    showToast("读取文件夹列表失败：" + e.message, "error");
  }
  const entries = folderHistoryEntries(indexed);
  if (!entries.length) {
    panel.innerHTML = `<div class="folder-history-empty">${ICONS.history || ""} 还没有整理过的文件夹</div>`;
  } else {
    panel.innerHTML = entries
      .map((entry) => {
        const tags = [...entry.tags]
          .map(
            (t) =>
              `<span class="history-tag${t === "最近" ? " recent" : ""}">${escapeHtml(t)}</span>`
          )
          .join("");
        const tagHtml = tags ? `<span class="history-tags">${tags}</span>` : "";
        return `<div class="folder-history-item" role="option" data-path="${escapeHtml(entry.path)}" title="${escapeHtml(entry.path)}">${ICONS.folder || ""}<span class="history-name">${escapeHtml(entry.path)}</span>${tagHtml}</div>`;
      })
      .join("");
    panel.querySelectorAll(".folder-history-item").forEach((el) => {
      el.addEventListener("click", () => {
        selectFolder(el.dataset.path);
        closeFolderHistory();
      });
    });
  }
  // A pending close must not hide the panel we are about to show.
  cancelFolderHistoryClose();
  panel.classList.remove("closing");
  panel.classList.remove("hidden");
  $("folderHistoryBtn").classList.add("open");
  $("folderHistoryBtn").setAttribute("aria-expanded", "true");
}

/** Switch the album to ``folder`` and remember the choice server-side. */
function selectFolder(folder) {
  const clean = String(folder || "").trim();
  if (!clean) return;
  state.folder = clean;
  rememberFolder(clean);
  saveUiState({ folder: clean });
  $("folderInput").value = clean;
  loadPhotos();
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
    const p = photoById(id);
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
        const updated = await api("/api/reanalyze", { method: "POST", body: JSON.stringify({ id: ids[i] }) });
        const idx = photoIndexById(ids[i]);
        if (idx >= 0 && updated) {
          if ($("statusFilter").value === "pending") {
            removePhotoRows([ids[i]]);
          } else {
            Object.assign(state.photos[idx], updated);
            invalidatePhotoDetail(ids[i]);
            refreshCard(ids[i]);
          }
        }
        ok++;
      } catch (e) {
        fail++;
      }
    }
    state.selected.clear();
    updateSelectionBar();
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
  const photo = photoById(state.currentPreviewId);
  if (!photo) return;
  
  const fsModal = $("fullscreenModal");
  const fsImg = $("fsImage");
  const fsVideo = $("fsVideo");
  const toolbar = fsModal ? fsModal.querySelector(".fs-viewer-toolbar") : null;
  if (!fsModal || !fsImg) return;

  state.fullscreen.scale = 1;
  state.fullscreen.panX = 0;
  state.fullscreen.panY = 0;
  state.fullscreen.isDragging = false;

  const isVideo = photo.media_kind === "video";
  if (fsVideo) {
    if (isVideo) {
      // The original file, with sound on: this is the "watch it properly"
      // path, and the user asked for it explicitly by clicking.
      fsImg.classList.add("hidden");
      fsImg.removeAttribute("src");
      fsVideo.classList.remove("hidden");
      fsVideo.muted = false;
      fsVideo.src = `/api/video/${photo.id}/stream`;
      fsVideo.load();
      // The zoom/pan toolbar belongs to the still viewer; the player has its
      // own controls and the two would fight over the same wheel gestures.
      if (toolbar) toolbar.classList.add("hidden");
      showModal(fsModal);
      attachFullscreenVideoFallback(fsVideo, photo);
      return;
    }
    fsVideo.pause();
    fsVideo.removeAttribute("src");
    fsVideo.load();
    fsVideo.classList.add("hidden");
  }
  if (toolbar) toolbar.classList.remove("hidden");
  fsImg.classList.remove("hidden");
  fsImg.src = `/api/original/${photo.id}`;
  applyFsTransform();
  showModal(fsModal);
}

/**
 * Recover when the browser cannot decode the original container.
 *
 * The three strategies line up with the server's: most files play as they are,
 * a stream copy rescues a container the browser refuses, and a real transcode
 * is the last resort. Asking ``/api/video/<id>/playable`` hides which happened;
 * the toolbar progress bar shows the wait when a transcode is needed.
 */
function attachFullscreenVideoFallback(video, photo) {
  let tried = false;
  video.onerror = async () => {
    if (tried) {
      showToast("这段视频无法在本机播放。", "error");
      return;
    }
    tried = true;
    try {
      const resp = await fetch(`/api/video/${photo.id}/playable`);
      if (resp.status === 200) {
        video.src = `/api/video/${photo.id}/playable`;
        video.load();
        video.play().catch(() => {});
        return;
      }
      if (resp.status === 202) {
        const data = await resp.json();
        showToast("这段视频需要转换才能播放，正在准备…", "info");
        await waitForVideoJob(data.job_id, photo.id);
        return;
      }
      const body = await resp.json().catch(() => ({}));
      showToast(body.error || "这段视频无法在本机播放。", "error");
    } catch (e) {
      showToast("转换视频失败：" + e.message, "error");
    }
  };
}

/** Follow a transcode job and swap the player over to the result. */
async function waitForVideoJob(jobId, photoId) {
  const video = $("fsVideo");
  for (let attempt = 0; attempt < 600; attempt += 1) {
    let status;
    try {
      status = await api(`/api/scan/status/${jobId}`);
    } catch (e) {
      return;
    }
    if (status.status === "completed") {
      invalidatePhotoDetail(photoId);
      if (video) {
        video.src = `/api/video/${photoId}/playable`;
        video.load();
        video.play().catch(() => {});
      }
      return;
    }
    if (status.status === "error" || status.status === "cancelled") {
      showToast(status.error || "视频转换失败。", "error");
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
}

function closeFullscreenViewer() {
  const fsModal = $("fullscreenModal");
  const fsVideo = $("fsVideo");
  if (fsVideo) {
    fsVideo.pause();
    fsVideo.removeAttribute("src");
    fsVideo.load();
    fsVideo.classList.add("hidden");
    fsVideo.onerror = null;
  }
  hideModal(fsModal);
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
  showModal($("previewModal"));
  updatePreview();
}

/**
 * Fetch the detail-only extras (EXIF, raw size) for the photo on screen.
 *
 * They used to ride along with every list row, which cost ~30% of the payload
 * and one `os.path.getsize` per photo on the album drive. The dialog paints
 * immediately from the row it already has and repaints once this lands; the
 * result is written back into the row so re-opening the photo costs nothing.
 */
/**
 * Fetch the detail-only extras (EXIF, raw size) for the photo on screen.
 *
 * They used to ride along with every list row, which cost ~30% of the payload
 * and one `os.path.getsize` per photo on the album drive. The dialog paints
 * immediately from the row it already has and repaints once this lands.
 *
 * The payload is kept in ``state.details`` rather than only written onto the
 * row, because a background poll rebuilds the row objects from the list - which
 * deliberately carries no EXIF. Writing it onto the old object alone meant the
 * parameters vanished and never came back: the fetch was remembered as done, so
 * nothing ever asked for them again until the page was reloaded. The cache is
 * re-applied whenever the rows are rebuilt.
 */
async function ensurePhotoDetail(id) {
  const photo = photoById(id);
  if (!photo) return;
  if (state.details.has(id)) {
    applyPhotoDetail(photo, state.details.get(id));
    return;
  }
  if (state.detailRequested.has(id)) return;
  state.detailRequested.add(id);
  try {
    const detail = await api(`/api/photo/${id}/detail`);
    state.details.set(id, detail);
    const target = photoById(id);
    if (target) applyPhotoDetail(target, detail);
    if (state.currentPreviewId === id) updatePreview({ skipImage: true });
  } catch (e) {
    // The row still renders; only the specs row stays empty until a retry.
    state.detailRequested.delete(id);
    console.warn("读取照片详情失败", e);
  }
}

/** Merge one detail payload into a list row. */
function applyPhotoDetail(row, detail) {
  const mediaKind = detail.media_kind || row.media_kind;
  Object.assign(row, {
    exif: detail.exif || {},
    raw_size: detail.raw_size,
    width: detail.width,
    height: detail.height,
    size: detail.size,
    media_kind: mediaKind,
    duration_ms: detail.duration_ms,
    video_info: detail.video_info || {},
    has_live: detail.has_live,
    play_state: detail.play_state,
    ffmpeg: detail.ffmpeg,
  });
  if (detail.location) row.location = detail.location;
  return row;
}

/** Forget a photo's detail payload so the next open fetches it again. */
function invalidatePhotoDetail(id) {
  state.details.delete(id);
  state.detailRequested.delete(id);
  const row = photoById(id);
  if (row) {
    // Keep the row renderable in the meantime: an empty EXIF block is honest,
    // a stale one would show wrong camera data while the refetch is in flight.
    row.exif = {};
    row.video_info = {};
  }
}

/**
 * Drop every cached payload for the photos in ``ids``.
 *
 * Called after an edit or a re-analysis: the server may have changed what the
 * detail endpoint would answer, and the list row only carries the visible part.
 */
function invalidatePhotoDetails(ids) {
  for (const id of ids) invalidatePhotoDetail(id);
}

/* ==========================================================================
   Video playback in the detail dialog

   The dialog shows the poster straight away, and swaps in a five-second,
   silent, looping preview once the browser can actually play it. The preview is
   deliberately muted: every browser blocks audible autoplay, and a dialog that
   the user only opened to look at something must not start talking. The speaker
   button is the opt-in, and its state is remembered for the session.

   A Live Photo plays its motion once on its own as the dialog opens, then fades
   back to the still - the same gesture an iPhone makes. It is muted because
   browsers only allow that automatically; pressing the 实况 chip replays it with
   sound, which is a deliberate act and therefore allowed.
   ========================================================================== */

//: Whether the user asked for sound in the preview clip. Session-wide.
let previewSoundOn = false;

/** Stop any video that is currently playing and put the element out of the way. */
function stopPreviewVideo() {
  const video = $("previewVideo");
  if (!video) return;
  video.pause();
  video.classList.remove("is-fading", "is-live-motion");
  // Clearing the source releases the decoder; without it every visited video
  // would keep a pipeline alive until the dialog closed.
  video.removeAttribute("src");
  video.load();
  video.classList.add("hidden");
  const pause = $("previewVideoPauseBtn");
  if (pause) pause.classList.add("hidden");
}

/**
 * Point the dialog at the right medium for ``photo``.
 *
 * ``skipImage`` is the "the row changed, repaint the panel" path used when the
 * detail payload lands; the video does not need to restart for that.
 */
function applyPreviewMedia(photo, options = {}) {
  const video = $("previewVideo");
  const soundBtn = $("previewSoundBtn");
  if (!video) return;

  const isVideo = photo.media_kind === "video";
  const hasLive = !!photo.has_live;

  // The dialog is painted twice: once from the list row, again when the detail
  // payload lands. That second pass must not restart what the first one began -
  // for a Live Photo it would cut the motion off a fraction of a second in.
  if (options.skipImage && video.dataset.photoId === String(photo.id)) {
    return;
  }

  // Reset first, then decide: switching from a video to a photo must not leave
  // the previous clip running underneath.
  stopPreviewVideo();
  if (soundBtn) soundBtn.classList.add("hidden");
  const liveBtn = $("previewLiveBtn");
  if (liveBtn) liveBtn.classList.add("hidden");
  $("previewImage").classList.toggle("hidden", false);

  if (isVideo) {
    video.dataset.photoId = String(photo.id);
    if (soundBtn) {
      soundBtn.classList.remove("hidden");
      paintSoundButton();
    }
    // The play/pause control stays on screen for a video whatever the preview
    // mode is: "不播放" and "播放一次" both stop on their own, and the button is
    // the only way back into the clip.
    const pause = $("previewVideoPauseBtn");
    if (pause) pause.classList.remove("hidden");
    playPreviewClip(photo);
    paintPauseButton();
    return;
  }

  delete video.dataset.photoId;
  // A Live Photo shows itself off: the still is on screen, the motion fades in
  // over it, plays once, and fades back out. The corner button is there to see
  // it again.
  if (hasLive && state.videoPreviewMode !== "off") {
    playLiveMotion(photo);
  }
  if (hasLive && liveBtn) liveBtn.classList.remove("hidden");
}

/** Load and start the preview for ``photo``, honouring the preview settings. */
function playPreviewClip(photo, options = {}) {
  const video = $("previewVideo");
  if (!video) return;
  const pause = $("previewVideoPauseBtn");
  const wanted = `/api/video/${photo.id}/clip`;
  video.dataset.photoId = String(photo.id);

  // "不播放" leaves the poster alone until the user presses play. The button
  // stays visible for exactly that reason.
  if (state.videoPreviewMode === "off" && !options.force) {
    video.classList.add("hidden");
    if (pause) pause.classList.remove("hidden");
    paintPauseButton();
    return;
  }

  video.muted = !previewSoundOn;
  video.volume = volumeFraction();
  // A manual start while the mode is "once" still plays a single pass; only
  // "loop" repeats.
  video.loop = state.videoPreviewMode === "loop";
  video.classList.add("hidden");
  video.src = wanted;
  const onReady = () => {
    if (video.dataset.photoId !== String(photo.id)) return;
    video.classList.remove("hidden");
    if (pause) pause.classList.remove("hidden");
    const played = video.play();
    if (played && typeof played.catch === "function") {
      // Autoplay can still be refused (a strict browser profile); the poster
      // stays and the button falls back to "play", which is what it now is.
      played.catch(() => {
        video.classList.add("hidden");
        paintPauseButton();
      });
    }
    paintPauseButton();
  };
  video.addEventListener("canplay", onReady, { once: true });
  // Both endings leave the button showing "play" again; the clip itself stays
  // on its last frame so the dialog still shows a picture.
  video.addEventListener("ended", paintPauseButton, { once: true });
  video.addEventListener("pause", paintPauseButton);
  video.addEventListener("play", paintPauseButton);
  video.addEventListener(
    "error",
    () => {
      // Ignore the abort that `stopPreviewVideo` causes when the source is
      // cleared: that fires on the same element and would hide a perfectly
      // good clip that is already loading.
      const current = video.getAttribute("src") || "";
      if (!current || current !== wanted) return;
      // No ffmpeg, or a codec ffmpeg could not read: keep the poster.
      video.classList.add("hidden");
      paintPauseButton();
    },
    { once: true }
  );
}

/** The preview volume, as a 0-1 fraction the media element understands. */
function volumeFraction(value) {
  const raw = value === undefined ? state.videoPreviewVolume : value;
  const percent = Number(raw);
  if (!Number.isFinite(percent)) return 0;
  return Math.max(0, Math.min(1, percent / 100));
}

function paintSoundButton() {
  const btn = $("previewSoundBtn");
  if (!btn) return;
  btn.innerHTML = `<span class="btn-icon">${previewSoundOn ? ICONS.volume : ICONS.volumeOff}</span>`;
  btn.title = previewSoundOn ? "关闭声音" : "打开声音";
  btn.classList.toggle("is-on", previewSoundOn);
  const video = $("previewVideo");
  if (video) video.muted = !previewSoundOn;
}

function togglePreviewSound() {
  previewSoundOn = !previewSoundOn;
  paintSoundButton();
}
/** Draw the play/pause mark to match what the element is actually doing. */
function paintPauseButton() {
  const video = $("previewVideo");
  const btn = $("previewVideoPauseBtn");
  if (!btn) return;
  // An element with no source still reports ``paused === false`` in some
  // browsers, which would draw a pause mark over a clip nobody started.
  const hasSource = !!video && !!video.getAttribute("src");
  const playing = hasSource && !video.paused && !video.ended;
  btn.innerHTML = `<span class="btn-icon">${playing ? ICONS.pause : ICONS.play}</span>`;
  btn.title = playing ? "暂停" : "播放";
}

/**
 * Start or stop the preview by hand.
 *
 * The button is always on screen for a video, in every preview mode: with the
 * preview set to 不播放 or 播放一次 the clip stops on its own, and without a
 * control there would be no way to see it again short of leaving the dialog.
 * When nothing is loaded yet, the press loads the clip.
 */
function togglePreviewPause(photo) {
  const video = $("previewVideo");
  const btn = $("previewVideoPauseBtn");
  if (!video || !btn) return;
  const id = state.currentPreviewId;
  if (!id) return;
  const loaded = (video.getAttribute("src") || "").length > 0;
  if (!loaded) {
    // Nothing has been requested yet (preview off, or it never started).
    const target = photo || photoById(id);
    if (target) playPreviewClip(target, { force: true });
    return;
  }
  if (video.paused) {
    video.play().catch(() => {});
  } else {
    video.pause();
  }
  paintPauseButton();
}

/**
 * Play or pause the short video a still carries.
 *
 * Unlike the looping preview this one is started by a deliberate press, so it
 * plays with sound - the user asked for it.
 */
let livePlaying = false;

/**
 * Play the short video a still carries, once, then fade back to the picture.
 *
 * The fade is the whole point: iOS turns a Live Photo into motion and back
 * without a cut, and a hard appear/disappear would look like a glitch. The
 * element is the same one the video preview uses, so there is only ever one
 * decoder alive.
 *
 * ``muted`` is true for the automatic play (browsers demand it) and false when
 * the user pressed the 实况 chip themselves.
 */
function playLiveMotion(photo, options = {}) {
  const video = $("previewVideo");
  if (!video || !photo || !photo.has_live) return;
  const wanted = `/api/photo/${photo.id}/live`;
  const volume = Math.max(0, Math.min(1, (Number(state.liveVolume) || 0) / 100));
  livePlaying = true;
  video.dataset.photoId = String(photo.id);
  video.loop = false;
  // Volume is a setting, not a boolean: at 0% the motion is silent, and above
  // it the user has asked for sound. `muted` alone cannot express "20%".
  video.volume = volume;
  video.muted = volume <= 0;
  video.classList.remove("is-fading");
  video.classList.add("hidden", "is-live-motion");
  video.src = wanted;

  const fadeOut = () => {
    livePlaying = false;
    video.classList.add("is-fading");
    // Matches the .is-fading transition in style.css.
    setTimeout(() => {
      if (video.dataset.photoId !== String(photo.id)) return;
      video.classList.add("hidden");
      video.classList.remove("is-fading");
    }, 420);
  };

  video.addEventListener(
    "canplay",
    () => {
      if (video.dataset.photoId !== String(photo.id)) return;
      video.classList.remove("hidden");
      const played = video.play();
      if (played && typeof played.catch === "function") {
        played.catch(() => video.classList.add("hidden"));
      }
    },
    { once: true }
  );
  video.addEventListener("ended", fadeOut, { once: true });
  video.addEventListener(
    "error",
    () => {
      livePlaying = false;
      video.classList.add("hidden");
      // Autoplay is allowed to fail quietly (a motion that will not decode
      // should not throw an error at someone who just opened a photo), but a
      // press deserves an answer.
      if (options.loud) showToast("这段实况视频无法播放。", "error");
    },
    { once: true }
  );
}


/**
 * Close a dialog, playing the reverse of its entrance animation first.
 *
 * ``hideModal`` keeps the element in the DOM for the length of the exit, so a
 * dialog that is re-opened mid-animation is handled (see ui-kit.js).
 */
function closeModal(id, onHidden) {
  hideModal($(id), onHidden);
  if (id === "previewModal") {
    // A looping preview would otherwise keep decoding (and playing) behind a
    // closed dialog until something else happened to stop it.
    stopPreviewVideo();
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
  const photo = photoById(id);
  if (!photo) return;
  // Pull the EXIF / raw size for this photo if it has not arrived yet; the
  // dialog is already painted by the time the response lands.
  ensurePhotoDetail(id);
  const hasNav = state.photos.length > 1;
  $("previewPrevBtn").classList.toggle("hidden", !hasNav);
  $("previewNextBtn").classList.toggle("hidden", !hasNav);
  const img = $("previewImage");
  if (!options.skipImage) {
    img.classList.remove("preview-switch-left", "preview-switch-right");
    img.src = `/api/proxy/${id}`;
  }
  const isDeleted = photo.status === "deleted";
  $("previewDeleteBtn").classList.toggle("hidden", isDeleted);
  $("previewRestoreBtn").classList.toggle("hidden", !isDeleted);
  $("previewPermanentDeleteBtn").classList.toggle("hidden", !isDeleted);
  // A trashed item is not analysed again, so the action is not offered either.
  // A video *is* analysed now (from its poster frame), so it keeps the button.
  $("previewReanalyzeBtn").classList.toggle("hidden", isDeleted);

  // Media: a video loops a short silent preview over its poster, a Live Photo
  // plays its motion once by itself, and a plain photo is just the picture.
  applyPreviewMedia(photo, options);

  const favBtn = $("previewFavoriteBtn");
  favBtn.classList.toggle("active", !!photo.favorite);
  favBtn.innerHTML = `${photo.favorite ? ICONS.heartFilled : ICONS.heart}<span>${photo.favorite ? "已珍藏" : "珍藏"}</span>`;
  favBtn.title = photo.favorite ? "取消珍藏 (快捷键 F)" : "珍藏 (快捷键 F)";

  const exif = photo.exif || {};
  const cameraText = [exif.make, exif.model].filter(Boolean).join(" ");
  const isVideo = photo.media_kind === "video";
  const info = photo.video_info || {};

  // Optical Specs Grid - Compact row with icons only.
  // A video uses the same row for its own four numbers (resolution, codec,
  // bitrate, frame rate) so the two kinds of item read alike at a glance.
  const specItems = [];
  if (cameraText) {
    specItems.push(`
      <div class="spec-badge camera-badge" title="拍摄机身/镜头：${escapeHtml(cameraText)}">
        ${ICONS.camera}
        <span class="spec-val">${escapeHtml(cameraText)}</span>
      </div>
    `);
  }
  if (isVideo) {
    if (photo.width && photo.height) {
      specItems.push(`
        <div class="spec-badge" title="分辨率：${photo.width} × ${photo.height}">
          ${ICONS.imageSize}
          <span class="spec-val">${photo.width} × ${photo.height}</span>
        </div>
      `);
    }
    const codecText = [info.video_codec, info.video_profile].filter(Boolean).join(" · ");
    if (codecText) {
      specItems.push(`
        <div class="spec-badge" title="编码格式：${escapeHtml(codecText)}">
          ${ICONS.video}
          <span class="spec-val">${escapeHtml(codecText)}</span>
        </div>
      `);
    }
    const bitrateText = formatBitrate(info.video_bitrate);
    if (bitrateText) {
      specItems.push(`
        <div class="spec-badge" title="视频码率：${bitrateText}">
          ${ICONS.fileSize}
          <span class="spec-val">${bitrateText}</span>
        </div>
      `);
    }
    const fpsText = formatFps(info.fps);
    if (fpsText) {
      specItems.push(`
        <div class="spec-badge" title="帧率：${fpsText}">
          ${ICONS.hourglass}
          <span class="spec-val">${fpsText}</span>
        </div>
      `);
    }
  } else {
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
    // A phone reports a 4.2 mm lens and a 26 mm equivalent; only the second
    // one says what the picture looks like, so it is the one shown.
    const focalText = exif.focal_length_35mm || exif.focal_length;
    if (focalText) {
      const equivalent = !!exif.focal_length_35mm;
      specItems.push(`
        <div class="spec-badge" title="${equivalent ? "等效焦距" : "焦距"}：${formatFocalLength(focalText)}">
          ${ICONS.focal}
          <span class="spec-val">${formatFocalLength(focalText)}</span>
        </div>
      `);
    }
    if (photo.has_live) {
      // A plain chip, drawn like the aperture and ISO badges beside it - it
      // reports that this still carries motion, nothing more. Playing it is the
      // play button's job, on the corner of the picture.
      specItems.push(`
        <div class="spec-badge live-spec" title="实况照片：这张照片带一段动态">
          ${ICONS.live}
          <span class="spec-val">Live</span>
        </div>
      `);
    }
  }

  const captureTime = formatCaptureTime(exif.datetime_original);
  const rawBadge = rawBadgeHtml(photo, "detail");

  $("previewInfo").innerHTML = `
    <div class="preview-header-block">
      <h3>${escapeHtml(photo.title || photo.filename)}</h3>
      ${rawBadge}
    </div>

    ${photo.reason ? `
      <div class="preview-quote-card">
        ${ICONS.quote} ${escapeHtml(photo.reason)}
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
      ${isVideo ? "" : `
      <div class="meta-item-row" title="尺寸分辨率">
        ${ICONS.imageSize}
        <div class="meta-content">${photo.width ? photo.width + " × " + photo.height : "-"}</div>
      </div>
      `}
      <div class="meta-item-row" title="文件大小">
        ${ICONS.fileSize}
        <div class="meta-content">${fileSizeText(photo)}</div>
      </div>
      ${videoDetailRows(photo)}
      <div class="meta-item-row" title="物理路径">
        ${ICONS.path}
        <div class="meta-content">${pathActionHtml(photo.path, "原图", photo.id)}</div>
      </div>
      ${rawDetailRow(photo)}
    </div>

    <div class="dimension-section">
      <div class="dimension-section-title">
        <span style="display:inline-flex;align-items:center;gap:6px;">${ICONS.score} 多维度评分</span>
        <span class="badge score">总分 ${scoreText(photo.score)}</span>
      </div>
      <div class="dimension-list">
        ${dimensionRows(photo.dimensions || {}, photo.score)}
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
  setSelectValue($("statusFilter"), state.defaultStatusFilter || "analyzed");
  setSelectValue($("favoriteFilter"), "");
  setSelectValue($("minScoreFilter"), "");
  setSelectValue($("tagFilter"), "");
  setSelectValue($("yearFilter"), "");
  setSelectValue($("kindFilter"), "");
  setSelectValue($("searchMode"), "smart");
  setSelectValue($("sortFilter"), "score_desc");
  $("searchInput").value = "";
  updateActiveStat();
  loadPhotos();
}

async function loadPromptPresets() {
  try {
    const data = await api("/api/prompt-presets");
    promptPresets = data.presets || {};
  } catch (e) {
    console.warn("加载提示词预设失败", e);
  }
}

function stripPromptFormat(text) {
  if (!text) return "";
  const idx = text.indexOf(PROMPT_FORMAT_MARKER);
  return idx >= 0 ? text.slice(0, idx).trim() : text.trim();
}

function activePresetFor(text) {
  const normalized = stripPromptFormat(text);
  return (
    Object.entries(promptPresets).find(
      ([, template]) => stripPromptFormat(template) === normalized
    )?.[0] || ""
  );
}

function highlightPromptPreset(text) {
  const active = activePresetFor(text);
  document.querySelectorAll(".preset-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.preset === active);
  });
}

function applyPromptPreset(name) {
  const template = promptPresets[name];
  if (!template) {
    showToast("提示词预设不可用", "error");
    return;
  }
  const promptText = stripPromptFormat(template);
  $("setSystemPrompt").value = promptText;
  highlightPromptPreset(promptText);
}

/* ==========================================================================
   Settings dialog: tabs
   ========================================================================== */

const SETTINGS_TABS = ["model", "image", "video", "geo", "data"];

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

async function openSettings() {
  try {
    await loadPromptPresets();
    const cfg = await api("/api/config");
    $("setApiBase").value = cfg.api_base_url || "";
    $("setApiKey").value = cfg.api_key || "";
    $("setModel").value = cfg.model || "";
    const promptText = stripPromptFormat(cfg.system_prompt || promptPresets.playful || "");
    $("setSystemPrompt").value = promptText;
    highlightPromptPreset(promptText);
    $("setMaxEdge").value = cfg.proxy_max_edge || 1280;
    $("setProxyQuality").value = cfg.proxy_quality ?? 80;
    $("setGalleryThumbSize").value = cfg.gallery_thumb_size || 640;
    $("setThumbQuality").value = cfg.thumb_quality ?? 50;
    $("setModelConcurrency").value = cfg.scan_concurrency || 1;
    $("setModelRetries").value = cfg.model_retries ?? 3;
    $("setTimeout").value = cfg.request_timeout || 120;
    $("setIndexThreads").value = cfg.index_concurrency || 4;
    setSelectValue($("setGeocodingProvider"), cfg.geocoding_provider || "nominatim");
    $("setGeocodingApiKey").value = cfg.geocoding_api_key || "";
    $("setGeocodingInterval").value = cfg.geocoding_interval || 1.0;
    $("setGeocodingRetries").value = cfg.geocoding_retries ?? 3;
    $("setVideoClipSeconds").value = cfg.video_clip_seconds ?? 3;
    $("setVideoClipMaxEdge").value = cfg.video_clip_max_edge || 960;
    $("setVideoClipCrf").value = cfg.video_clip_crf ?? 28;
    $("setVideoPosterRatio").value = cfg.video_poster_ratio ?? 0.1;
    $("setVideoClipAudio").checked = cfg.video_clip_audio !== false;
    $("setVideoPregenerate").checked = !!cfg.video_pregenerate;
    $("setMergeLivePhotos").checked = cfg.merge_live_photos !== false;
    setSelectValue($("setVideoPreviewMode"), cfg.video_preview_mode || "once");
    setVolume("video", cfg.video_preview_volume ?? 50);
    setVolume("live", cfg.live_volume ?? 0);
    state.videoClipSeconds = Number(cfg.video_clip_seconds) || 3;
    state.videoPreviewMode = cfg.video_preview_mode || "once";
    state.videoPreviewVolume = Number(cfg.video_preview_volume ?? 50);
    state.liveVolume = Number(cfg.live_volume ?? 0);
    paintFfmpegStatus();
    // Always open on the model tab: that is what most visits are for.
    showSettingsTab("model");
    showModal($("settingsModal"));
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
    scan_concurrency: parseInt($("setModelConcurrency").value, 10) || 1,
    model_retries: parseInt($("setModelRetries").value, 10) || 3,
    request_timeout: parseInt($("setTimeout").value, 10) || 120,
    index_concurrency: parseInt($("setIndexThreads").value, 10) || 4,
    geocoding_provider: $("setGeocodingProvider").value,
    geocoding_api_key: $("setGeocodingApiKey").value,
    geocoding_interval: parseFloat($("setGeocodingInterval").value) || 1.0,
    geocoding_retries: parseInt($("setGeocodingRetries").value, 10) || 3,
    video_clip_seconds: parseFloat($("setVideoClipSeconds").value) || 3,
    video_clip_max_edge: parseInt($("setVideoClipMaxEdge").value, 10) || 960,
    video_clip_crf: parseInt($("setVideoClipCrf").value, 10) || 28,
    video_poster_ratio: parseFloat($("setVideoPosterRatio").value) || 0.1,
    video_clip_audio: $("setVideoClipAudio").checked,
    video_pregenerate: $("setVideoPregenerate").checked,
    merge_live_photos: $("setMergeLivePhotos").checked,
    video_preview_mode: $("setVideoPreviewMode").value,
    video_preview_volume: clampVolume($("setVideoPreviewVolume").value),
    live_volume: clampVolume($("setLiveVolume").value),
  };
  try {
    await api("/api/config", {
      method: "POST",
      body: JSON.stringify(payload),
    });
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
    $("cardBanner").classList.add("hidden");
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
    banner.classList.remove("hidden");
  } else {
    banner.classList.add("hidden");
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
  } catch (e) {
    // Defaults are sensible, so a failure here changes nothing visible.
    console.warn("读取播放设置失败", e);
  }
}

function init() {
  // Render embedded icons
  renderAllIcons();

  setSelectValue($("statusFilter"), "analyzed");
  $("folderInput").addEventListener("change", () => {
    const val = $("folderInput").value.trim();
    if (val) {
      state.folder = val;
      rememberFolder(val);
      saveUiState({ folder: val });
      loadPhotos();
    }
  });

  $("scanBtn").addEventListener("click", startScan);
  $("rebuildIndexBtn").addEventListener("click", startRebuildIndex);
  $("importBtn").addEventListener("click", openImportDialog);
  $("importPickSourceBtn").addEventListener("click", pickImportSource);
  $("importPlanBtn").addEventListener("click", () => planImport());
  $("importRunBtn").addEventListener("click", runImport);
  $("importMode").addEventListener("change", () => planImport({ silent: true }));
  $("importRawPolicy").addEventListener("change", () => planImport({ silent: true }));
  $("cardStatusBtn").addEventListener("click", onCardStatusClick);
  $("cardBannerSyncBtn").addEventListener("click", () => openCardSyncDialog());
  $("cardBannerHideBtn").addEventListener("click", () => {
    state.cardBannerDismissed = true;
    paintCardStatus();
  });
  $("cardSyncRunBtn").addEventListener("click", runCardSync);
  $("cardSyncRefreshBtn").addEventListener("click", reloadCardPreview);
  $("cardSyncForgetBtn").addEventListener("click", forgetCurrentCard);
  $("cardSyncCancelBtn").addEventListener("click", () => closeModal("cardSyncModal"));
  $("cardSyncOpenBtn").addEventListener("click", () => {
    const source = state.cardPreview ? state.cardPreview.source : state.card && state.card.source;
    if (source) openFolderByPath(source);
  });
  $("browseBtn").addEventListener("click", openDirBrowser);
  $("openFolderBtn").addEventListener("click", openFolder);
  $("folderHistoryBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleFolderHistory();
  });
  $("settingsBtn").addEventListener("click", openSettings);
  $("searchBtn").addEventListener("click", loadPhotos);
  $("resetFiltersBtn").addEventListener("click", resetFilters);
  $("loadMoreBtn").addEventListener("click", loadMorePhotos);
  $("cancelScanBtn").addEventListener("click", async () => {
    if (state.currentJobId) {
      try { await api(`/api/scan/cancel/${state.currentJobId}`, { method: "POST" }); } catch (e) {}
    }
  });

  $("statusFilter").addEventListener("change", loadPhotos);
  $("kindFilter").addEventListener("change", loadPhotos);
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
  initGalleryEvents();

  $("previewPrevBtn").addEventListener("click", () => navigatePreview(-1));
  $("previewNextBtn").addEventListener("click", () => navigatePreview(1));
  $("previewEditBtn").addEventListener("click", openEditPreview);
  $("previewDeleteBtn").addEventListener("click", () => deletePhotos([state.currentPreviewId]));
  $("previewRestoreBtn").addEventListener("click", () => restorePhotos([state.currentPreviewId]));
  $("previewPermanentDeleteBtn").addEventListener("click", () => permanentDeletePhotos([state.currentPreviewId]));
  $("previewReanalyzeBtn").addEventListener("click", () => reanalyze(state.currentPreviewId));
  $("previewFavoriteBtn").addEventListener("click", toggleCurrentPreviewFavorite);
  $("previewImage").addEventListener("click", openFullscreenViewer);
  $("previewVideo").addEventListener("click", openFullscreenViewer);
  $("previewSoundBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    togglePreviewSound();
  });
  $("previewVideoPauseBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    togglePreviewPause(photoById(state.currentPreviewId));
  });
  $("previewInfo").addEventListener("click", handlePreviewInfoClick);
  $("previewLiveBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    const photo = photoById(state.currentPreviewId);
    if (photo) playLiveMotion(photo, { loud: true });
  });

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
    if (!state.currentDirPath) return;
    if (state.importPickPending) {
      state.importPickPending = false;
      $("importSource").value = state.currentDirPath;
      rememberImportSource(state.currentDirPath);
      closeModal("dirModal");
      showModal($("importModal"));
      planImport({ silent: true });
      return;
    }
    state.folder = state.currentDirPath;
    rememberFolder(state.folder);
    saveUiState({ folder: state.folder });
    $("folderInput").value = state.folder;
    closeModal("dirModal");
    loadPhotos();
  });

  $("saveSettingsBtn").addEventListener("click", saveSettings);
  // Keep the percentage beside each slider in step while it is dragged.
  ["setVideoPreviewVolume", "setLiveVolume"].forEach((id) => {
    const input = $(id);
    if (!input) return;
    input.addEventListener("input", () => {
      const label = $(`${id}Value`);
      if (label) label.textContent = `${input.value}%`;
    });
  });
  $("cleanCacheBtn").addEventListener("click", cleanCache);
  $("removeFolderBtn").addEventListener("click", removeCurrentFolder);
  document.querySelectorAll("#settingsModal .settings-tab").forEach((tab) => {
    tab.addEventListener("click", () => showSettingsTab(tab.dataset.tab));
    tab.addEventListener("keydown", onSettingsTabKeydown);
  });
  // Tab widths change with the window (and with the responsive label rule).
  window.addEventListener("resize", () => {
    if (!$("settingsModal").classList.contains("hidden")) moveSettingsTabInk();
  });
  $("editSaveBtn").addEventListener("click", saveEditedPhoto);
  $("editCancelBtn").addEventListener("click", () => closeModal("editModal"));
  initScoreEditors();
  initVolumeEditors();
  $("emptyTrashBtn").addEventListener("click", emptyTrash);
  document.querySelectorAll(".preset-btn").forEach((btn) => {
    btn.addEventListener("click", () => handlePromptPresetClick(btn.dataset.preset));
  });

  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (state.importPickPending && btn.dataset.close === "dirModal") {
        // Cancelling the folder browser returns to the import dialog.
        state.importPickPending = false;
        closeModal("dirModal");
        showModal($("importModal"));
        return;
      }
      closeModal(btn.dataset.close);
    });
  });
  document.querySelectorAll(".modal").forEach((modal) => {
    modal.addEventListener("click", (e) => {
      if (e.target !== modal) return;
      if (state.importPickPending && modal.id === "dirModal") {
        state.importPickPending = false;
        closeModal("dirModal");
        showModal($("importModal"));
        return;
      }
      closeModal(modal.id);
    });
  });
  document.addEventListener("click", (e) => {
    if (
      !e.target.closest("#folderHistoryBtn") &&
      !e.target.closest("#folderHistory")
    ) {
      closeFolderHistory();
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
  // Playback preferences are needed before the first dialog opens, not only
  // when someone visits the settings page.
  loadPlaybackConfig();
  // The card can be plugged in or pulled out at any time; the badge is the only
  // place that says so, so it keeps itself up to date.
  refreshCardStatus();
  startCardPolling();
  // Independent of the album: a job may belong to another folder, or to none.
  // Runs in parallel with the album load so a refreshed page shows the running
  // progress bar as early as possible.
  resumeScanJob();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshCardStatus();
  });
  initBackToTop();
}

/* ==========================================================================
   Back to top
   The button only exists while the page is scrolled, and the return itself is
   eased rather than instant: it accelerates away from the bottom, then settles
   into the top instead of stopping dead. Both halves are one easing curve - a
   cubic-Bezier that starts fast and lands softly - so the motion reads as a
   single gesture rather than a scroll with a brake.
   ========================================================================== */

//: How long the return takes. Long enough to follow, short enough not to wait.
const BACK_TO_TOP_MS = 780;
//: Scroll further than this and the button appears.
const BACK_TO_TOP_TRIGGER = 320;

/** True when the visitor asked the system to avoid large movements. */
function motionIsReduced() {
  return (
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/**
 * The easing used for the return.
 *
 * Cubic in and out: it leaves the bottom gently, builds speed, then sheds it as
 * it lands, so the page arrives at the top rather than stopping there.
 */
function easeInOutCubic(t) {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/**
 * Scroll the window to the top over ``BACK_TO_TOP_MS``.
 *
 * `requestAnimationFrame` drives the position, which is what lets the curve be
 * a curve; `scroll-behavior: smooth` can only be the browser's own timing. A
 * wheel turn, a drag or a key press during the animation cancels it instead of
 * fighting the user. Someone who asked the system for reduced motion gets an
 * instant jump - the movement is the whole point of the animation, so it cannot
 * be kept while honouring that setting.
 */
function scrollToTopSmoothly() {
  const start = window.scrollY || window.pageYOffset || 0;
  if (start <= 0) return;
  if (motionIsReduced()) {
    window.scrollTo(0, 0);
    syncBackToTop();
    return;
  }
  const button = $("backToTopBtn");
  if (button) button.classList.add("returning");

  const started = performance.now();
  let cancelled = false;
  const cancel = () => {
    cancelled = true;
  };
  const stopListening = () => {
    window.removeEventListener("wheel", cancel);
    window.removeEventListener("touchstart", cancel);
    window.removeEventListener("keydown", cancel);
  };
  window.addEventListener("wheel", cancel, { passive: true });
  window.addEventListener("touchstart", cancel, { passive: true });
  window.addEventListener("keydown", cancel);

  const step = (now) => {
    if (cancelled) {
      stopListening();
      if (button) button.classList.remove("returning");
      return;
    }
    const progress = Math.min(1, (now - started) / BACK_TO_TOP_MS);
    const eased = easeInOutCubic(progress);
    window.scrollTo(0, Math.round(start * (1 - eased)));
    if (progress < 1) {
      requestAnimationFrame(step);
      return;
    }
    stopListening();
    if (button) button.classList.remove("returning");
    syncBackToTop();
  };
  requestAnimationFrame(step);
}

/** Show or hide the button for the current scroll position. */
function syncBackToTop() {
  const button = $("backToTopBtn");
  if (!button) return;
  const scrolled = (window.scrollY || window.pageYOffset || 0) > BACK_TO_TOP_TRIGGER;
  const hidden = button.classList.contains("hidden");
  if (scrolled && hidden) {
    button.classList.remove("leaving");
    button.classList.remove("hidden");
  } else if (!scrolled && !hidden && !button.classList.contains("leaving")) {
    button.classList.add("leaving");
    setTimeout(() => {
      // The scroll may have come back while the exit played.
      if ((window.scrollY || 0) <= BACK_TO_TOP_TRIGGER) {
        button.classList.add("hidden");
        button.classList.remove("leaving");
      } else {
        button.classList.remove("leaving");
      }
    }, 240);
  }
}

function initBackToTop() {
  const button = $("backToTopBtn");
  if (!button) return;
  renderAllIcons(button);
  button.addEventListener("click", scrollToTopSmoothly);
  let ticking = false;
  window.addEventListener(
    "scroll",
    () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        ticking = false;
        syncBackToTop();
      });
    },
    { passive: true }
  );
  syncBackToTop();
}

document.addEventListener("DOMContentLoaded", init);
