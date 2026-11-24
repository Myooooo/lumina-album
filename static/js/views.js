/* ==========================================================================
   Lumina Album - Views: the four pages, job progress and destructive actions
   ========================================================================== */
"use strict";

/* ==========================================================================
   Views
   The album, the places, the past days and the duplicates are four pages of one
   app, not a page and three dialogs. Exactly one is visible; the switcher marks it
   with a filled chip and remembers the choice.
   ========================================================================== */

//: The views, in the order the switcher shows them.
const VIEWS = [
  { name: "album", button: "albumViewBtn", panel: "albumView" },
  { name: "people", button: "peopleBtn", panel: "peopleView" },
  { name: "footprints", button: "footprintsBtn", panel: "footprintsView" },
  { name: "memories", button: "memoriesBtn", panel: "memoriesView" },
  { name: "duplicates", button: "duplicatesBtn", panel: "duplicatesView" },
];

//: Keep `--topbar-height` synced with the sticky header's real height so
//: `.album-sticky-bar` and `.duplicates-toolbar` dock flush underneath it.
function syncTopbarHeight() {
  const topbar = document.querySelector(".topbar");
  if (!topbar) return;
  // Fractional, not rounded: the filter bar sticks to this number, and rounding
  // it up or down left a sub-pixel slit between the two frosted layers that
  // showed the page through what should be one header.
  const h = topbar.getBoundingClientRect().height;
  if (h > 0) {
    document.documentElement.style.setProperty("--topbar-height", `${h.toFixed(2)}px`);
  }
}

let viewTransitionTimer = null;

/**
 * Show a strip/bar with its entrance animation, cancelling any in-flight exit.
 */
function showStrip(el) {
  if (!el) return;
  if (el._stripExitTimer) {
    clearTimeout(el._stripExitTimer);
    el._stripExitTimer = null;
  }
  el.classList.remove("closing");
  el.classList.remove("hidden");
  syncTopbarHeight();
}

/**
 * Hide a strip/bar after playing its `.closing` exit animation (160ms).
 */
function hideStrip(el) {
  if (!el || el.classList.contains("hidden")) return;
  if (el._stripExitTimer) return;
  const reduced =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced) {
    el.classList.remove("closing");
    el.classList.add("hidden");
    syncTopbarHeight();
    return;
  }
  el.classList.add("closing");
  el._stripExitTimer = setTimeout(() => {
    el._stripExitTimer = null;
    el.classList.remove("closing");
    el.classList.add("hidden");
    syncTopbarHeight();
  }, 160);
}

/** Show one view and mark it in the switcher, with an interruptible exit/enter transition. */
function showView(name) {
  const target = VIEWS.find((view) => view.name === name) || VIEWS[0];
  const previousName = state.view;

  for (const view of VIEWS) {
    const button = $(view.button);
    const active = view.name === target.name;
    if (button) {
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-selected", active ? "true" : "false");
    }
  }

  // Slide the ink under the selected label. Measured from the element, so it
  // follows the real text rather than an assumed width.
  moveViewInk(target.button);
  // A playing clip must not keep decoding behind a hidden panel, and the dialog
  // belongs to the view it was opened from.
  if (state.currentPreviewId) closeModal("previewModal");
  state.view = target.name;
  state.previewContext = null;
  // CSS joins the toolbar to the filter bar on the album and keeps them separate
  // elsewhere; the difference is a fact about the page, so it lives on the body.
  document.body.dataset.view = target.name;
  saveUiState({ view: target.name });

  if (viewTransitionTimer) {
    clearTimeout(viewTransitionTimer);
    viewTransitionTimer = null;
  }

  const targetPanel = $(target.panel);
  const previousView = VIEWS.find((v) => v.name === previousName);
  const previousPanel =
    previousView && previousView.name !== target.name ? $(previousView.panel) : null;

  // Hide any third views immediately so rapid clicks never leave two panels visible.
  for (const view of VIEWS) {
    if (view.name === target.name) continue;
    const panel = $(view.panel);
    if (!panel) continue;
    if (panel !== previousPanel) {
      panel.classList.remove("view-leaving");
      panel.classList.add("hidden");
    }
  }

  const reduced =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const activateTarget = () => {
    for (const view of VIEWS) {
      const panel = $(view.panel);
      if (!panel) continue;
      panel.classList.remove("view-leaving");
      panel.classList.toggle("hidden", view.name !== target.name);
    }
    syncTopbarHeight();
    if (typeof updateTimelineScrubber === "function") updateTimelineScrubber();
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  };

  if (
    !previousPanel ||
    previousPanel.classList.contains("hidden") ||
    reduced ||
    !state.viewsBooted
  ) {
    state.viewsBooted = true;
    activateTarget();
    return;
  }

  previousPanel.classList.add("view-leaving");
  viewTransitionTimer = setTimeout(() => {
    viewTransitionTimer = null;
    activateTarget();
  }, 140);
}

/**
 * Put the ink under the given switcher button.
 *
 * Uses `offsetLeft`/`offsetWidth`, which are layout numbers rather than a transform,
 * so the ink's own CSS transition can animate between them. A button that is hidden
 * or not yet laid out measures 0, in which case the ink is simply hidden rather than
 * parked in the top-left corner.
 */
function moveViewInk(buttonId) {
  const ink = $("viewInk");
  const button = $(buttonId);
  if (!ink || !button) return;
  const wrapper = ink.parentElement;
  if (!wrapper) return;
  const width = button.offsetWidth;
  if (!width) {
    ink.style.opacity = "0";
    return;
  }
  ink.style.opacity = "1";
  ink.style.width = `${width}px`;
  ink.style.transform = `translateX(${button.offsetLeft}px)`;
}

/** The view named in the URL hash, or the album. */
function viewFromHash() {
  const hash = (window.location.hash || "").replace("#", "").trim();
  return VIEWS.some((view) => view.name === hash) ? hash : "album";
}

function openViewByName(name) {
  if (name === "people") return openPeople();
  if (name === "footprints") return openFootprints();
  if (name === "memories") return openMemories();
  if (name === "duplicates") return openDuplicates();
  showView("album");
}

function initViews() {
  syncTopbarHeight();
  window.addEventListener("resize", syncTopbarHeight);
  if (typeof ResizeObserver === "function") {
    const topbar = document.querySelector(".topbar");
    if (topbar) new ResizeObserver(syncTopbarHeight).observe(topbar);
  }
  for (const view of VIEWS) {
    const button = $(view.button);
    if (!button) continue;
    button.addEventListener("click", () => {
      // The hash keeps the view shareable and survives a reload without a round
      // trip to the server.
      if (viewFromHash() === view.name) {
        openViewByName(view.name);
      } else {
        window.location.hash = view.name === "album" ? "" : view.name;
      }
    });
  }
  window.addEventListener("hashchange", () => openViewByName(viewFromHash()));
  const initial = viewFromHash();
  if (initial === "album") showView("album");
  else openViewByName(initial);
}

/**
 * Re-fetch whatever view is on screen.
 *
 * The folder can change from the input, from the history list, or from the picker;
 * each of those used to call `loadPhotos()` alone, which only refreshes the gallery.
 * The other three views kept their old album's photos until they were reopened.
 */
async function reloadCurrentView() {
  // The album is refreshed whatever page is on screen: switching the folder while
  // reading 那年今日 used to leave the gallery holding the previous album's photos,
  // so going back to 相册 showed the wrong book.
  const albumRefresh = state.view === "album" ? loadPhotos() : loadPhotos({ quiet: true });
  switch (state.view) {
    case "people":
      await Promise.all([albumRefresh, openPeople()]);
      break;
    case "footprints":
      await Promise.all([albumRefresh, openFootprints()]);
      break;
    case "memories":
      await Promise.all([albumRefresh, openMemories()]);
      break;
    case "duplicates":
      await albumRefresh;
      await loadDuplicates(dupState.threshold);
      break;
    default:
      await albumRefresh;
      break;
  }
}

/** True when a dialog or the fullscreen viewer is on top of the gallery. */
function isOverlayOpen() {
  const fullscreen = $("fullscreenModal");
  if (fullscreen && !fullscreen.classList.contains("hidden")) return true;
  return Array.from(document.querySelectorAll(".modal")).some(
    (modal) => !modal.classList.contains("hidden")
  );
}

function initGalleryEvents() {
  const gallery = $("gallery");
  if (!gallery) return;

  // After any scroll - wheel, drag, keyboard or the back-to-top animation -
  // re-derive the culled state once things are still.
  window.addEventListener("scroll", scheduleCardSweep, { passive: true });
  window.addEventListener("resize", scheduleCardSweep);

  initEndlessGallery();

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

  let lastCheckedId = null;
  let lastCheckShift = false;
  gallery.addEventListener("click", (e) => {
    if (e.target.closest(".card-check")) {
      lastCheckShift = !!e.shiftKey;
    }
  }, true);

  gallery.addEventListener("change", (e) => {
    const checkbox = e.target.closest(".card-check input");
    if (!checkbox) return;
    const card = checkbox.closest(".polaroid-card");
    const id = Number(card && card.dataset.id);
    if (!Number.isFinite(id)) return;
    const checked = checkbox.checked;

    if (lastCheckShift && lastCheckedId !== null && lastCheckedId !== id) {
      const order = visiblePhotoOrder();
      const a = order.indexOf(lastCheckedId);
      const b = order.indexOf(id);
      if (a >= 0 && b >= 0) {
        const [start, end] = a < b ? [a, b] : [b, a];
        for (let i = start; i <= end; i += 1) {
          const targetId = order[i];
          if (checked) state.selected.add(targetId);
          else state.selected.delete(targetId);
          const targetBox = gallery.querySelector(`.polaroid-card[data-id="${targetId}"] .card-check input`);
          if (targetBox) targetBox.checked = checked;
        }
      }
    } else {
      if (checked) state.selected.add(id);
      else state.selected.delete(id);
    }
    lastCheckedId = id;
    lastCheckShift = false;
    updateSelectionBar();
  });

  initMarqueeSelection(gallery);

  // `load`/`error` do not bubble, so they are caught on the way down.
  //
  // Bound to the document rather than to the gallery: a polaroid is also rendered in
  // 那年今日 and in an opened place in 足迹, and those pictures loaded correctly while
  // staying at `opacity: 0` because nothing added `is-loaded` outside the grid.
  for (const event of ["load", "error"]) {
    document.addEventListener(
      event,
      (e) => {
        if (e.target.classList && e.target.classList.contains("polaroid-photo")) {
          e.target.classList.add("is-loaded");
        }
      },
      true
    );
  }
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
  if (count > 0) $("selectedCount").textContent = count;
  const hasDeleted = state.photos.some((p) => state.selected.has(p.id) && p.status === "deleted");
  const hasActive = state.photos.some((p) => state.selected.has(p.id) && p.status !== "deleted");
  $("restoreSelectedBtn").classList.toggle("hidden", !hasDeleted);
  $("permanentDeleteSelectedBtn").classList.toggle("hidden", !hasDeleted);
  $("deleteSelectedBtn").classList.toggle("hidden", !hasActive);
  $("reanalyzeSelectedBtn").classList.toggle("hidden", !hasActive);
  if (count > 0) showStrip(bar);
  else hideStrip(bar);
}

/* ==========================================================================
   Confirming a destructive or expensive scan

   "重新整理全部" turns a cheap catch-up into something much bigger, and what it
   means depends on which button is pressed. The two are different enough that
   the checkbox alone cannot say it, so each run asks first - and the dialog is
   the only place that names the consequence before the model starts working.

   These build on ``confirmDialog`` from js/ui-kit.js rather than a dialog of
   their own: that one already owns ``#confirmModal`` and is what every other
   confirmation in the app uses. A second implementation meant two pieces of
   code writing into one element, and whichever ran second won - which is how
   the empty-trash prompt ended up showing the text of the scan prompt.
   ========================================================================== */

/**
 * The "ffmpeg is not installed" question.
 *
 * Returns ``{confirmed, skipVideos}``. Only asked on a full redo: an ordinary
 * scan merely picks up new files, while a redo re-reads everything - and with no
 * ffmpeg that would replace the duration, resolution and codec of every video
 * already in the library with blanks.
 */
async function askMissingFfmpeg() {
  const answer = await confirmDialog(
    "本次整理会重新读取整个文件夹，但本机没有 ffmpeg，视频的时长、分辨率与编码读不出来。\n" +
      "如果不跳过已索引的视频，它们已有的这些信息会被清空（评分与标签不受影响）。",
    {
      title: "没有检测到 ffmpeg",
      confirmText: "继续整理",
      checkbox: { label: "跳过已索引的视频", checked: true },
    }
  );
  return {
    confirmed: typeof answer === "object" ? answer.confirmed : answer,
    skipVideos: typeof answer === "object" ? answer.checked : true,
  };
}

/** The consequence of "重新整理全部", stated per button. */
async function askForceScan(mode) {
  const isScan = mode === "scan";
  const message = isScan
    ? "将先重新读取整个文件夹的文件信息，然后把每一张照片、每一段视频重新送交大模型解读。\n" +
      "评分、标签、标题与简评会被新结果覆盖，手动编辑过的内容同样会被覆盖（珍藏状态保留）。\n" +
      "这会按张数消耗模型调用，耗时可能很长。"
    : "将重新读取整个文件夹的文件信息：文件大小、EXIF、拍摄地点，以及视频的时长、分辨率与编码。\n" +
      "不调用大模型，评分、标签与简评保持不变。";
  return confirmDialog(message, {
    title: isScan ? "重新整理全部并重新解读？" : "重新整理全部？",
    confirmText: isScan ? "重新整理并解读" : "重新整理",
    danger: isScan,
  });
}

async function startScan() {
  const folder = $("folderInput").value.trim();
  if (!folder) {
    showToast("先告诉我们照片文件夹在哪里吧", "error");
    return;
  }
  const force = $("forceScan").checked;
  let skipVideos = false;
  if (force) {
    if (!(await askForceScan("scan"))) return;
    if (!state.ffmpegAvailable) {
      const answer = await askMissingFfmpeg();
      if (!answer.confirmed) return;
      skipVideos = answer.skipVideos;
    }
  }
  state.folder = folder;
  rememberFolder(folder);
  saveUiState({ folder });
  try {
    const data = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({ folder, force, skip_indexed_videos: skipVideos }),
    });
    state.currentJobId = data.job_id;
    showProgress(force ? "重新整理并解读…" : "开始整理…");
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
  const force = $("forceScan").checked;
  let skipVideos = false;
  if (force) {
    if (!(await askForceScan("rebuild"))) return;
    if (!state.ffmpegAvailable) {
      const answer = await askMissingFfmpeg();
      if (!answer.confirmed) return;
      skipVideos = answer.skipVideos;
    }
  }
  state.folder = folder;
  rememberFolder(folder);
  saveUiState({ folder });
  try {
    const data = await api("/api/rebuild-index", {
      method: "POST",
      body: JSON.stringify({ folder, force, skip_indexed_videos: skipVideos }),
    });
    state.currentJobId = data.job_id;
    showProgress(force ? "重新整理文件信息…" : "正在同步相册…");
    pollScan();
  } catch (e) {
    showToast("同步相册失败：" + e.message, "error");
  }
}

function showProgress(text) {
  showStrip($("scanProgress"));
  $("progressText").textContent = text;
  $("progressFill").style.width = "0%";
  state.pollTick = 0;
  // A new job must not inherit the previous run's speed estimate.
  state.eta = null;
}

function hideProgress() {
  hideStrip($("scanProgress"));
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
        // Quietly trigger incremental local face/pet scan after album sync/organize.
        if (job.status === "completed" && job.phase !== "trash" && state.faceRecognitionEnabled && state.faceAutoScan) {
          api("/api/people/scan", {
            method: "POST",
            body: JSON.stringify({ folder: state.folder || "" }),
          }).catch(() => {});
        }
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
  // A video job works on one file and names it: "正在准备视频 1/1：a.mp4". The
  // basename alone keeps the toolbar from showing a full album path.
  const current = job.current ? String(job.current).split(/[\\/]/).pop() : "";
  if (!total) return `${phaseLabel(job.phase)}…`;
  return `${phaseLabel(job.phase)} ${job.processed}/${total}${current ? "：" + current : ""}`;
}

const PHASE_LABELS = {
  plan: "正在比对",
  copy: "正在拷贝",
  index: "正在索引",
  analyze: "正在解读",
  // A transcode is a single-item job that can run for minutes. Without its own
  // label it fell through to "整理照片", which describes nothing the user is
  // actually waiting for.
  video: "正在准备视频",
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

  // A batch re-analysis reports how many it actually got through.
  if (report && typeof report === "object" && "analysed" in report) {
    if (job.status === "cancelled") {
      return `重新解读已取消：已完成 ${report.analysed} 张`;
    }
    const parts = [`重新解读完成：${report.analysed} 张`];
    if (Number(report.failed) > 0) parts.push(`${report.failed} 张失败`);
    if (report.paused) {
      parts.push("模型服务连续失败，已提前暂停");
    }
    return parts.join("，");
  }

  if (job.status === "cancelled") return `${phaseLabel(job.phase)}已取消`;
  if (job.stalled) {
    return `模型服务连续失败，${phaseLabel(job.phase)}已暂停（${job.processed}/${job.total}）`;
  }
  if (!job.total) return "整理完成：没有需要处理的照片";
  if (job.processed < job.total && job.phase === "analyze") {
    // Stopped short: say so rather than implying the whole album was read.
    return `解读已暂停：共处理 ${job.processed}/${job.total}`;
  }
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
    if (!data.errors || !data.errors.length) {
      const removedIds = ids.slice();
      showToast(`已暂时收起 ${removedIds.length} 张回忆`, "info", {
        action: {
          label: "撤销",
          onClick: async () => {
            try {
              await api("/api/restore", {
                method: "POST",
                body: JSON.stringify({ ids: removedIds, sync_card: syncCard }),
              });
              showToast(`已恢复 ${removedIds.length} 张回忆`, "success");
              await loadPhotos({ quiet: false });
              await refreshCardStatus();
            } catch (err) {
              showToast("撤销失败：" + err.message, "error");
            }
          },
        },
      });
    }
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

/**
 * Move the preview one card in ``delta`` direction.
 *
 * Deliberately not circular. Wrapping from the first photo to the last meant
 * "上一张" on the first card jumped to somewhere in the middle of the album, and
 * the next press navigated from there - the user lost their place with no way
 * to tell what had happened. The ends are ends: the side arrows are hidden there
 * and the keyboard says so.
 */
async function navigatePreview(delta) {
  // A sheet that owns the dialog defines the order; otherwise it is the gallery.
  let order = state.previewContext
    ? state.previewContext.ids.slice()
    : visiblePhotoOrder();
  if (!order.length) return;
  let idx = order.indexOf(state.currentPreviewId);
  if (idx < 0) return;

  // At the end of the loaded pages, fetch the next page before deciding that
  // there is genuinely no next photo. Only for the gallery: a sheet's list is
  // complete already.
  if (delta > 0 && idx === order.length - 1 && state.hasMore && !state.previewContext) {
    const loadedBefore = state.photos.length;
    await loadMorePhotos();
    if (state.photos.length > loadedBefore) {
      order = visiblePhotoOrder();
      idx = order.indexOf(state.currentPreviewId);
      if (idx < 0) return;
    }
  }

  const target = idx + delta;
  if (target < 0) {
    showToast("已经到顶了。", "info");
    return;
  }
  if (target >= order.length) {
    showToast("已经到底了。", "info");
    return;
  }

  const nextId = order[target];
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

/**
 * Whether there is a photo before/after the one on screen.
 *
 * Drives the side arrows and the "at the end" toasts. A page still to load
 * counts as a neighbour on the way forward, because pressing the arrow fetches
 * it rather than stopping.
 */
/**
 * Whether there is a photo in that direction to move to.
 *
 * Reads the same order `navigatePreview` walks. It used to measure the gallery's
 * order unconditionally, so a photo opened from 足迹 or 相似回忆 was either missing
 * from that order or alone in it and both arrows vanished - the sheet's own list
 * was never consulted.
 */
function previewHasNeighbour(delta) {
  const order = state.previewContext
    ? state.previewContext.ids
    : visiblePhotoOrder();
  if (!order.length) return false;
  const idx = order.indexOf(state.currentPreviewId);
  if (idx < 0) return false;
  if (delta < 0) return idx > 0;
  // Only the gallery can grow past its end; a sheet's list is complete already.
  return idx < order.length - 1 || (!state.previewContext && !!state.hasMore);
}

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
  const visible = (path) => path && !isHiddenFolder(path);
  push(state.folderHistory.find(visible), "最近");
  (indexed || []).filter(visible).forEach((f) => push(f, "已索引"));
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

/**
 * Switch the album to ``folder``.
 *
 * This is the one path every album change takes. A hidden album - opened from
 * the settings panel's hidden list, or typed into the path field - gets its
 * 「已隐藏」 marker and is never remembered as the open album: a refresh or a
 * backend restart has to land back on the last visible one.
 */
function openAlbumFolder(folder) {
  const clean = String(folder || "").trim();
  if (!clean) return;
  const hidden = isHiddenFolder(clean);
  state.folderHidden = hidden;
  $("folderInput").value = clean;
  paintFolderHiddenBadge();
  if (hidden) {
    // Keep it out of the in-memory history too: the dropdown is a path
    // selector, and hidden albums do not belong there.
    state.folderHistory = state.folderHistory.filter(
      (item) => folderPathKey(item) !== folderPathKey(clean)
    );
  } else {
    rememberFolder(clean);
    saveUiState({ folder: clean });
  }
  // Goes through the shared path so the filters and the search box are cleared
  // first: a query written for the previous album must not be re-sent.
  applyFolderChange(clean);
}

/** Switch the album to ``folder``; the dropdown only offers visible albums. */
function selectFolder(folder) {
  openAlbumFolder(folder);
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

/**
 * Re-analyse several photos at once.
 *
 * This used to be a `for` loop in the page: one `/api/reanalyze` per photo,
 * behind a blocking overlay. Selecting a few hundred photos meant a spinner for
 * hours with no way to stop it, and a refresh threw the whole run away.
 *
 * It is now a background job on the server, so the toolbar shows the same
 * progress bar, remaining-time estimate and cancel button as a scan, and a
 * reloaded page picks the run back up (see resumeScanJob). The endpoint probes
 * the model first, so a stopped server fails immediately instead of writing a
 * failure row for every photo.
 */
async function reanalyzeSelected(ids) {
  ids = ids.filter((id) => {
    const p = photoById(id);
    return p && p.status !== "deleted";
  });
  if (!ids.length) return;
  const confirmed = await confirmDialog(
    `确定重新解读选中的 ${ids.length} 张照片吗？\n任务会在后台进行，可以随时取消。`,
    { title: "重新解读", confirmText: "开始解读" }
  );
  if (!confirmed) return;
  try {
    const data = await api("/api/analyze/batch", {
      method: "POST",
      body: JSON.stringify({ ids, folder: state.folder || "" }),
    });
    state.selected.clear();
    updateSelectionBar();
    renderGallery({ animate: false });
    state.currentJobId = data.job_id;
    showProgress(`正在重新解读 ${data.total} 张照片…`);
    pollScan();
  } catch (e) {
    showToast("重新解读失败：" + e.message, "error");
  }
}

