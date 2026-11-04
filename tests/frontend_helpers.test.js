/* ==========================================================================
   Frontend helper tests - run with:  node tests/frontend_helpers.test.js

   These cover the pure functions behind the progress bar, the job summary and
   the detail dialog. They are extracted from static/app.js at run time, so the
   assertions follow the shipped source instead of a copy that can drift.
   ========================================================================== */
"use strict";

const fs = require("fs");
const path = require("path");

const APP = path.join(__dirname, "..", "static", "app.js");
const src = fs.readFileSync(APP, "utf8");

/** Lift one top-level function out of app.js, braces balanced. */
function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`static/app.js has no function ${name}`);
  let depth = 0;
  const from = src.indexOf("{", start);
  for (let i = from; i < src.length; i += 1) {
    if (src[i] === "{") depth += 1;
    else if (src[i] === "}") {
      depth -= 1;
      if (depth === 0) return src.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced braces in ${name}`);
}

/** Lift a top-level `const NAME = ...;` declaration. */
function extractConst(name) {
  const match = new RegExp(`^const ${name} = [^;]+;`, "m").exec(src);
  if (!match) throw new Error(`static/app.js has no const ${name}`);
  return match[0];
}

const PHASE_LABELS = { plan: "正在比对", copy: "正在拷贝", index: "正在索引", analyze: "正在解读", video: "正在准备视频" };

/* --------------------------------------------------------------- harness ---
   ``updateEta`` reads the module-level ``state`` and calls ``performance.now``
   and ``paintEta``. All three are supplied here so the tests can drive a fake
   clock and observe exactly what the pill was asked to display.
   ------------------------------------------------------------------------- */
const etaHarness = new Function(
  "PHASE_LABELS",
  "clock",
  `${extract("phaseLabel")}
   ${extract("progressLabel")}
   ${extract("jobSummaryText")}
   ${extractConst("ETA_RATE_ALPHA")}
   ${extractConst("ETA_OVERALL_WEIGHT")}
   ${extractConst("ETA_DISPLAY_ALPHA")}
   ${extractConst("ETA_MIN_SAMPLE_SECONDS")}
   ${extract("formatEta")}
   ${extract("seedEta")}
   ${extract("smoothEta")}
   ${extract("updateEta")}

   let state = { eta: null };
   const painted = [];
   // Same contract as the real one, without touching the DOM.
   function paintEta(seconds) {
     const value = seconds === 0 ? 0 : seconds;
     const text = value === 0 ? "即将完成" : formatEta(value);
     if (state.eta) {
       state.eta.shown = text || null;
       if (Number.isFinite(value)) state.eta.shownSeconds = value;
     }
     painted.push(text || null);
   }
   return {
     phaseLabel, progressLabel, jobSummaryText, formatEta, updateEta, seedEta, smoothEta,
     constants: { ETA_RATE_ALPHA, ETA_OVERALL_WEIGHT, ETA_DISPLAY_ALPHA, ETA_MIN_SAMPLE_SECONDS },
     eta: () => state.eta,
     painted: () => painted.slice(),
     lastPainted: () => painted[painted.length - 1],
     reset: () => { state = { eta: null }; painted.length = 0; },
   };`
)(PHASE_LABELS, () => Date.now());

// A controllable clock, so the ETA maths is deterministic.
const clock = (() => {
  let value = 1000;
  return {
    now: () => value,
    set: (next) => {
      value = next;
    },
    advance: (ms) => {
      value += ms;
    },
  };
})();

const helpers = new Function(
  "PHASE_LABELS",
  "performance",
  `${extract("phaseLabel")}
   ${extract("progressLabel")}
   ${extract("jobSummaryText")}
   ${extractConst("ETA_RATE_ALPHA")}
   ${extractConst("ETA_OVERALL_WEIGHT")}
   ${extractConst("ETA_DISPLAY_ALPHA")}
   ${extractConst("ETA_MIN_SAMPLE_SECONDS")}
   ${extract("formatEta")}
   ${extract("seedEta")}
   ${extract("smoothEta")}
   ${extract("updateEta")}

   let state = { eta: null };
   const painted = [];
   function paintEta(seconds) {
     const value = seconds === 0 ? 0 : seconds;
     const text = value === 0 ? "即将完成" : formatEta(value);
     if (state.eta) {
       state.eta.shown = text || null;
       if (Number.isFinite(value)) state.eta.shownSeconds = value;
     }
     painted.push(text || null);
   }
   return {
     phaseLabel, progressLabel, jobSummaryText, formatEta, updateEta,
     constants: { ETA_RATE_ALPHA, ETA_OVERALL_WEIGHT, ETA_DISPLAY_ALPHA, ETA_MIN_SAMPLE_SECONDS },
     eta: () => state.eta,
     painted: () => painted.slice(),
     lastPainted: () => painted[painted.length - 1],
     reset: () => { state = { eta: null }; painted.length = 0; },
   };`
)(PHASE_LABELS, clock);

// ``nextPreviewAfterRemoval`` reads the module-level ``state``; bind a stub.
const nextPreviewAfterRemoval = new Function(
  "state",
  `${extract("nextPreviewAfterRemoval")} return nextPreviewAfterRemoval;`
);
const next = (ids, current, removed) =>
  nextPreviewAfterRemoval(
    { photos: ids.map((id) => ({ id })), currentPreviewId: current }
  )(removed);

// ``folderHistoryEntries`` also reads ``state``; bind a stub with a history.
const folderHistoryEntries = new Function(
  "state",
  "folderPathKey",
  `${extract("folderPathKey")}
   ${extract("folderHistoryEntries")}
   return folderHistoryEntries;`
);
const historyEntries = (indexed, history) =>
  folderHistoryEntries({ folderHistory: history }, (p) =>
    String(p).replace(/[\/\\]+$/, "").toLowerCase()
  )(indexed);

// The gallery helpers: what a poll changed, and where a card's thumbnail lives.
const galleryHelpers = new Function(
  `${extract("diffPhotoRows")}
   ${extract("thumbnailUrl")}
   ${extract("formatDuration")}
   ${extract("formatBitrate")}
   ${extract("formatFps")}
   ${extract("fmtSize")}
   ${extract("formatExposure")}
   return {
     diffPhotoRows, thumbnailUrl, formatDuration, formatBitrate, formatFps,
     fmtSize, formatExposure,
   };`
)();
const {
  diffPhotoRows, thumbnailUrl, formatDuration, formatBitrate, formatFps,
  fmtSize, formatExposure,
} = galleryHelpers;

// The back-to-top curve.
const easeInOutCubic = new Function(`${extract("easeInOutCubic")} return easeInOutCubic;`)();

let failures = 0;
let checks = 0;

function check(label, actual, expected) {
  checks += 1;
  const same = JSON.stringify(actual) === JSON.stringify(expected);
  if (same) return;
  failures += 1;
  console.log(`  FAIL ${label}`);
  console.log(`       got:  ${JSON.stringify(actual)}`);
  console.log(`       want: ${JSON.stringify(expected)}`);
}

function section(title) {
  console.log(`\n${title}`);
}

/* ---------------------------------------------------------------- formatEta */
section("formatEta");
check("no estimate yet", helpers.formatEta(null), "");
check("garbage in", helpers.formatEta(NaN), "");
check("zero", helpers.formatEta(0), "即将完成");
check("a few seconds left", helpers.formatEta(3), "即将完成");
check("seconds", helpers.formatEta(12), "预计 12 秒");
check("just under a minute", helpers.formatEta(59), "预计 59 秒");
check("exactly a minute", helpers.formatEta(60), "预计 1 分");
check("minutes and seconds", helpers.formatEta(200), "预计 3 分 20 秒");
check("exactly an hour", helpers.formatEta(3600), "预计 1 小时");
check("hours and minutes", helpers.formatEta(3900), "预计 1 小时 5 分");
check("negative clamps", helpers.formatEta(-9), "即将完成");

/* ------------------------------------------------------------------- ETA -- */
section("remaining-time estimate");

// A steady job: 5 files every second, 100 in total.
helpers.reset();
clock.set(1000);
helpers.updateEta(10, 100, 2); // the server says 2s have passed
check("the first estimate is seeded from the server's elapsed time", helpers.eta().rate, 5);
check("so an estimate is available immediately", helpers.lastPainted(), "预计 18 秒");

clock.advance(1000);
helpers.updateEta(15, 100, 3);
clock.advance(1000);
helpers.updateEta(20, 100, 4);
check("a steady job keeps a steady estimate", helpers.lastPainted(), "预计 17 秒");

// One slow tick: three seconds pass and a single file is processed. The instant
// rate collapses from 5/s to 0.33/s. The old implementation followed it and the
// estimate leapt to well over a minute.
const beforeSlow = helpers.eta().shownSeconds;
clock.advance(3000);
helpers.updateEta(21, 100, 7);
const afterSlow = helpers.eta().shownSeconds;
check(
  "one slow tick barely moves the estimate",
  Math.abs(afterSlow - beforeSlow) <= 3,
  true
);
check(
  "the displayed estimate stays near the whole-run average",
  afterSlow > 15 && afterSlow < 30,
  true
);

// ...and it recovers once the job is moving again, without overshooting.
clock.advance(1000);
helpers.updateEta(26, 100, 8);
const recovered = helpers.eta().shownSeconds;
check("the estimate recovers after the slow patch", recovered > 15 && recovered < 30, true);
check(
  "and never counts back up sharply",
  Math.abs(recovered - afterSlow) <= 3,
  true
);

// Jitter: alternating fast and slow ticks must not make the pill flap.
helpers.reset();
clock.set(1000);
helpers.updateEta(10, 100, 2);
const samples = [];
for (let tick = 0; tick < 8; tick += 1) {
  clock.advance(1000);
  const step = tick % 2 === 0 ? 8 : 2;
  const processed = helpers.eta().processed + step;
  helpers.updateEta(processed, 100, 2 + tick + 1);
  samples.push(helpers.eta().shownSeconds);
}
const swing = Math.max(...samples) - Math.min(...samples);
check("alternating fast/slow ticks stay within a narrow band", swing <= 12, true);

helpers.reset();
clock.set(5000);
helpers.updateEta(50, 100, 10);
clock.advance(1000);
helpers.updateEta(0, 100, 0);
check("progress moving backwards resets rather than going negative", helpers.lastPainted(), null);

helpers.reset();
clock.set(9000);
helpers.updateEta(100, 100, 10);
check("a finished job says so", helpers.lastPainted(), "即将完成");

helpers.reset();
clock.set(1000);
helpers.updateEta(0, 100, 0);
check("no progress yet means no estimate", helpers.lastPainted(), null);

/* ------------------------------------------------- refresh keeps the estimate */
section("a page refresh keeps the estimate");
// Refreshing throws away the in-memory rate; the server's elapsed time is what
// lets the very first poll show a sensible number straight away.
helpers.reset();
clock.set(60000);
helpers.updateEta(30, 120, 15);
check("first poll after a refresh still has an estimate", helpers.lastPainted(), "预计 45 秒");
check("and it matches the whole-run speed", Math.round(helpers.eta().rate), 2);

/* ------------------------------------------------------------------- labels */
section("progressLabel");
check(
  "the bin sweep counts photos",
  helpers.progressLabel({ phase: "trash", processed: 3, total: 10, current: "a.jpg" }),
  "正在清空回收站 3/10：a.jpg"
);
check(
  "the bin sweep with an unknown total",
  helpers.progressLabel({ phase: "trash", processed: 0, total: 0 }),
  "正在清空回收站…"
);
check(
  "a scan phase names the current file, basename only",
  helpers.progressLabel({ phase: "copy", processed: 2, total: 8, current: "DCIM/a.jpg" }),
  "正在拷贝 2/8：a.jpg"
);
check(
  "a video job keeps its own label",
  helpers.progressLabel({ phase: "video", processed: 1, total: 1, current: "/x/y/clip.mp4" }),
  "正在准备视频 1/1：clip.mp4"
);
check(
  "an unknown total does not print 0/0",
  helpers.progressLabel({ phase: "index", processed: 0, total: 0 }),
  "正在索引…"
);

/* ------------------------------------------------- jobSummaryText: imports */
section("jobSummaryText - SD card imports");
const base = {
  copied: 12,
  failed: [],
  cancelled: false,
  total: 12,
  new_count: 10,
  overwrite_count: 2,
  skip_count: 34,
};
const importJob = (report, phase = "copy", status = "completed") => ({ status, phase, report });

check(
  "copy only",
  helpers.jobSummaryText(importJob(base)),
  "导入完成：新增 10，覆盖 2，跳过 34"
);
check(
  "indexed afterwards",
  helpers.jobSummaryText(importJob(base, "index")),
  "导入完成：新增 10，覆盖 2，跳过 34，已建立索引，尚未调用模型"
);
check(
  "analysed afterwards",
  helpers.jobSummaryText(importJob(base, "analyze")),
  "导入完成：新增 10，覆盖 2，跳过 34，已索引并完成模型解读"
);
check(
  "zero counters are dropped",
  helpers.jobSummaryText(importJob({ ...base, copied: 3, new_count: 0, overwrite_count: 3, skip_count: 0 })),
  "导入完成：覆盖 3"
);
check(
  "nothing needed copying",
  helpers.jobSummaryText(importJob({ ...base, copied: 0, new_count: 0, overwrite_count: 0, skip_count: 7 })),
  "导入完成：所有文件都已是最新（跳过 7）"
);
check(
  "nothing at all happened",
  helpers.jobSummaryText(importJob({ ...base, copied: 0, new_count: 0, overwrite_count: 0, skip_count: 0 })),
  "导入完成：所有文件都已是最新"
);
check(
  "failures are reported",
  helpers.jobSummaryText(importJob({ ...base, failed: [{ relative_path: "a", error: "x" }] })),
  "导入完成：新增 10，覆盖 2，跳过 34，1 个文件失败"
);
check(
  "a cancelled import is not '完成'",
  helpers.jobSummaryText(importJob({ ...base, cancelled: true, copied: 4 }, "copy", "cancelled")),
  "导入已取消：已拷贝 4 个文件"
);
check(
  "cancelled before anything was copied",
  helpers.jobSummaryText(importJob({ ...base, cancelled: true, copied: 0 }, "copy", "cancelled")),
  "导入已取消：还没有拷贝文件"
);
check(
  "a JPG+RAW import",
  helpers.jobSummaryText(
    importJob(
      { copied: 598, failed: [], cancelled: false, total: 598, new_count: 299, overwrite_count: 0, skip_count: 2 },
      "index"
    )
  ),
  "导入完成：新增 299，跳过 2，已建立索引，尚未调用模型"
);

/* --------------------------------------------- jobSummaryText: scan, trash */
section("jobSummaryText - scans and the bin sweep");
check(
  "a finished scan",
  helpers.jobSummaryText({ status: "completed", phase: "analyze", processed: 30, total: 30 }),
  "整理完成：共处理 30/30"
);
check(
  "a scan with nothing to do",
  helpers.jobSummaryText({ status: "completed", phase: "index", processed: 0, total: 0 }),
  "整理完成：没有需要处理的照片"
);
check(
  "a cancelled scan",
  helpers.jobSummaryText({ status: "cancelled", phase: "analyze", processed: 3, total: 30 }),
  "正在解读已取消"
);
check(
  "the bin sweep",
  helpers.jobSummaryText({
    status: "completed",
    phase: "trash",
    processed: 5,
    total: 5,
    report: { deleted: 5, raw_deleted: 2 },
  }),
  "回收站已清空，共移除 5 张照片，含 2 个 RAW"
);
check(
  "an already empty bin",
  helpers.jobSummaryText({
    status: "completed",
    phase: "trash",
    processed: 0,
    total: 0,
    report: { deleted: 0, raw_deleted: 0 },
  }),
  "回收站已经是空的"
);
check(
  "an error wins over everything",
  helpers.jobSummaryText({ status: "error", phase: "copy", error: "磁盘满了" }),
  "任务出错：磁盘满了"
);

/* ------------------------------------------------ nextPreviewAfterRemoval */
section("nextPreviewAfterRemoval");
check("the current photo survives", next([1, 2, 3], 2, [1]), 2);
check("moves on to the next photo", next([1, 2, 3], 2, [2]), 3);
check("skips over several removals", next([1, 2, 3, 4], 2, [2, 3]), 4);
check("the last photo closes the dialog", next([1, 2, 3], 3, [3]), null);
check("no dialog open stays closed", next([1, 2], null, [1]), null);
check("nothing removed keeps the position", next([1, 2], 1, []), 1);
check("the only photo closes it", next([9], 9, [9]), null);
check("removing earlier photos keeps the position", next([1, 2, 3], 3, [1, 2]), 3);

/* -------------------------------------------------- folderHistoryEntries */
section("folderHistoryEntries");
const tagsOf = (entries) => entries.map((e) => [...e.tags].join("+"));

check(
  "only the most recent folder is tagged 最近",
  tagsOf(historyEntries(["D:/a", "D:/b", "D:/c"], ["D:/c", "D:/b", "D:/a"])),
  ["最近+已索引", "已索引", "已索引"]
);
check(
  "the most recent folder comes first",
  historyEntries(["D:/a", "D:/b"], ["D:/b"]).map((e) => e.path),
  ["D:/b", "D:/a"]
);
check(
  "a folder that is both keeps both tags",
  tagsOf(historyEntries(["D:/a", "D:/b"], ["D:/a"])),
  ["最近+已索引", "已索引"]
);
check(
  "an indexed folder with no history still shows",
  tagsOf(historyEntries(["D:/only"], [])),
  ["已索引"]
);
check(
  "history entries that are not indexed still show",
  tagsOf(historyEntries([], ["D:/gone"])),
  ["最近"]
);
check(
  "case and a trailing separator are the same folder",
  tagsOf(historyEntries(["D:/Photos", "d:/photos/"], ["D:/PHOTOS"])),
  ["最近+已索引"]
);
check("an empty list stays empty", historyEntries([], []), []);

/* ---------------------------------------------------- diffPhotoRows (poll) */
section("diffPhotoRows - what a background poll changes");
const rows = (...ids) => ids.map((id) => ({ id, status: "analyzed", score: 7, favorite: false }));

check(
  "an identical page needs no work",
  diffPhotoRows(rows(1, 2, 3), rows(1, 2, 3)),
  { sameShape: true, changed: [] }
);
check(
  "a scored row is reported",
  diffPhotoRows(rows(1, 2, 3), [
    { id: 1, status: "analyzed", score: 7, favorite: false },
    { id: 2, status: "analyzed", score: 9, favorite: false },
    { id: 3, status: "analyzed", score: 7, favorite: false },
  ]),
  { sameShape: true, changed: [2] }
);
check(
  "a favourited row is reported",
  diffPhotoRows(rows(1, 2), [
    { id: 1, status: "analyzed", score: 7, favorite: true },
    { id: 2, status: "analyzed", score: 7, favorite: false },
  ]),
  { sameShape: true, changed: [1] }
);
check(
  "a row that left the filter is a rebuild",
  diffPhotoRows(rows(1, 2, 3), rows(1, 3)),
  { sameShape: false, changed: [] }
);
check(
  "a row that entered the filter is a rebuild",
  diffPhotoRows(rows(1, 2), rows(9, 1)),
  { sameShape: false, changed: [] }
);
check(
  "a polled page longer than the loaded list is a rebuild",
  diffPhotoRows(rows(1), rows(1, 2)),
  { sameShape: false, changed: [] }
);
check(
  "a polled page shorter than the loaded list is still a prefix",
  diffPhotoRows(rows(1, 2, 3, 4), rows(1, 2)),
  { sameShape: true, changed: [] }
);
check(
  "several changed rows are all reported",
  diffPhotoRows(rows(1, 2, 3), [
    { id: 1, status: "deleted", score: 7, favorite: false },
    { id: 2, status: "analyzed", score: 8, favorite: false },
    { id: 3, status: "analyzed", score: 7, favorite: true },
  ]),
  { sameShape: true, changed: [1, 2, 3] }
);
check("an empty page against an empty list", diffPhotoRows([], []), {
  sameShape: true,
  changed: [],
});

/* --------------------------------------------------------------- video */
section("fmtSize");
check("bytes", fmtSize(512), "512 B");
check("kilobytes", fmtSize(934824), "912.9 KB");
check("megabytes under a gigabyte", fmtSize(214 * 1024 * 1024), "214.0 MB");
check("just under the boundary stays MB", fmtSize(1023 * 1024 * 1024), "1023.0 MB");
check("a gigabyte rolls over", fmtSize(1024 * 1024 * 1024), "1.00 GB");
check("a phone video at 1.5 GB", fmtSize(1536 * 1024 * 1024), "1.50 GB");
check("a two-hour recording", fmtSize(9.4 * 1024 * 1024 * 1024), "9.40 GB");
check("zero is still zero", fmtSize(0), "0 B");
check("nothing is a dash", fmtSize(null), "-");

section("formatExposure");
check("already has a unit", formatExposure("1/120s"), "1/120s");
check("a bare fraction", formatExposure("1/120"), "1/120s");
check("a bare number under a second", formatExposure(0.5), "1/2s");
check("seconds", formatExposure(2), "2s");
check("empty", formatExposure(""), "");

section("formatDuration");
check("a few seconds", formatDuration(7000), "00:07");
check("just over a minute", formatDuration(83_000), "01:23");
check("an hour and change", formatDuration(3_723_000), "1:02:03");
check("exactly ten minutes", formatDuration(600_000), "10:00");
check("a long drone clip", formatDuration(1_500_000), "25:00");
check("unknown duration renders nothing", formatDuration(null), "");
check("a zero duration renders nothing", formatDuration(0), "");
check("nonsense renders nothing", formatDuration("abc"), "");

section("formatBitrate");
check("megabits", formatBitrate(45_000_000), "45.0 Mbps");
check("kilobits", formatBitrate(128_000), "128 kbps");
check("unknown renders nothing", formatBitrate(0), "");
check("missing renders nothing", formatBitrate(null), "");

section("formatFps");
check("whole frames", formatFps(25), "25 fps");
check("a fractional rate", formatFps(59.94), "59.94 fps");
check("unknown renders nothing", formatFps(0), "");
check("missing renders nothing", formatFps(null), "");

/* ------------------------------------------------------------ thumbnailUrl */
section("thumbnailUrl");
check("no version means a plain url", thumbnailUrl({ id: 7 }), "/api/thumbnail/7");
check(
  "the version rides along as a query",
  thumbnailUrl({ id: 7, thumb_version: "abc123" }),
  "/api/thumbnail/7?v=abc123"
);
check(
  "an empty version is dropped",
  thumbnailUrl({ id: 7, thumb_version: "" }),
  "/api/thumbnail/7"
);

/* ------------------------------------------------------------ easeInOutCubic */
section("easeInOutCubic (the back-to-top curve)");
check("starts at rest", easeInOutCubic(0), 0);
check("ends at rest", easeInOutCubic(1), 1);
check("crosses the middle", easeInOutCubic(0.5), 0.5);
check("accelerates off the mark", easeInOutCubic(0.1) < 0.1, true);
check("still accelerating at a quarter", easeInOutCubic(0.25) < 0.25, true);
check("decelerates into the top", easeInOutCubic(0.9) > 0.9, true);
check("never runs backwards", (() => {
  let previous = -1;
  for (let i = 0; i <= 100; i += 1) {
    const value = easeInOutCubic(i / 100);
    if (value < previous) return false;
    previous = value;
  }
  return true;
})(), true);
check("stays inside the range", (() => {
  for (let i = -5; i <= 105; i += 1) {
    const t = Math.min(1, Math.max(0, i / 100));
    const value = easeInOutCubic(t);
    if (value < 0 || value > 1) return false;
  }
  return true;
})(), true);
check("is symmetric around the midpoint", (() => {
  for (const t of [0.05, 0.2, 0.35, 0.49]) {
    if (Math.abs(easeInOutCubic(t) + easeInOutCubic(1 - t) - 1) > 1e-9) return false;
  }
  return true;
})(), true);

/* ------------------------------------------------------------ Live Photo button
   The replay control exists in the markup with `hidden` on it, so it never
   flashes on a photo that turns out not to be a Live Photo. Showing it has to
   lift that class - the original bug was a helper that only toggled the fade
   (`is-hidden`) and left `hidden` in place forever, so the button could never
   appear once the motion had finished. These pin the two classes apart. */
section("Live Photo replay button");

const LIVE_VIS = extract("setLiveButtonVisible");
const LIVE_HIDE = extract("hideLiveButton");

/** Run an extracted helper against a stub element that records its classes. */
function runLiveHelper(source, fnName) {
  const classes = new Set(["preview-live-btn", "hidden"]);
  const btn = {
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)),
    },
  };
  const fn = new Function("$", `${source}; return ${fnName};`)(
    (id) => (id === "previewLiveBtn" ? btn : null)
  );
  return { classes, call: fn };
}

{
  const { classes, call } = runLiveHelper(LIVE_VIS, "setLiveButtonVisible");
  call(true);
  check("showing it lifts the hidden gate", classes.has("hidden"), false);
  check("showing it clears the fade", classes.has("is-hidden"), false);
}

{
  // The real sequence is "show it, then hide it for the motion": the gate has
  // already been lifted by the show, so hiding must not put it back.
  const { classes, call } = runLiveHelper(LIVE_VIS, "setLiveButtonVisible");
  call(true);
  call(false);
  check("during playback it fades out", classes.has("is-hidden"), true);
  check(
    "during playback it stays laid out so it can fade back",
    classes.has("hidden"),
    false
  );
}

{
  const { classes, call } = runLiveHelper(LIVE_HIDE, "hideLiveButton");
  call();
  check("a still with no motion hides it outright", classes.has("hidden"), true);
  check("and takes the fade too", classes.has("is-hidden"), true);
}

/* The button must land where a video's play/pause sits: same size, same corner. */
{
  const css = fs.readFileSync(
    path.join(__dirname, "..", "static", "style.css"),
    "utf8"
  );
  const shared =
    /\.preview-sound-btn,\s*\.preview-video-pause,\s*\.preview-live-btn\s*\{([^}]*)\}/.exec(
      css
    );
  check("the three round controls share one rule", Boolean(shared), true);
  if (shared) {
    check("shared width", /width:\s*38px/.test(shared[1]), true);
    check("shared height", /height:\s*38px/.test(shared[1]), true);
    check("shared bottom offset", /bottom:\s*12px/.test(shared[1]), true);
  }
  check(
    "the live button shares the video control's corner",
    /\.preview-video-pause,\s*\.preview-live-btn\s*\{[^}]*right:\s*12px/.test(css),
    true
  );
}

/* --------------------------------------------------- scroll anchor on "load more"
   Pressing 展开更多回忆 appends cards. The first press is also what wakes card
   culling up, and culling used to resize the cards it collapsed - which shoved
   everything below them and made the page appear to jump backwards. The anchor
   pair has to be a no-op when nothing moved, and equal-and-opposite when it did. */
section("scroll anchor (展开更多回忆)");

const ANCHOR_RESTORE = extract("restoreScrollAnchor");

function restoreWith(card) {
  const calls = [];
  const fn = new Function(
    "$",
    "window",
    `const state = {photos: [1]};
     ${ANCHOR_RESTORE}
     return restoreScrollAnchor;`
  )(
    (id) => (id === "gallery" ? { querySelector: () => card } : null),
    { scrollBy: (opts) => calls.push(opts) }
  );
  return { calls, restore: fn };
}

{
  const { calls, restore } = restoreWith({
    getBoundingClientRect: () => ({ top: 40 }),
  });
  restore({ id: "7", top: 40 });
  check("nothing moved, nothing scrolls", calls.length, 0);

  restore({ id: "7", top: 10 });
  check("a 30px drift is corrected by 30px", calls.length, 1);
  check("in the right direction", calls[0].top, 30);
  check("instantly, not smoothly", calls[0].behavior, "instant");
}

{
  const { calls, restore } = restoreWith(null);
  restore({ id: "gone", top: 10 });
  check("a removed anchor is not chased", calls.length, 0);
}

check(
  "loadMorePhotos holds the anchor rather than restoring once",
  /holdScrollAnchor\(anchor\)/.test(src),
  true
);
check(
  "culling pins the measured height so it cannot resize a card",
  /function cullCard\(/.test(src) && /containIntrinsicSize/.test(src),
  true
);

/* ------------------------------------------------------ endless gallery gesture
   At the bottom of the gallery, continuing to scroll should pull the next page
   in. Two rules make it feel right: only movement *at* the bottom counts (the
   scroll that carried you there must not immediately fetch another page), and a
   visit to the bottom buys exactly one fetch however hard the reader keeps
   scrolling. */
section("endless gallery (下拉继续)");

check("the bottom slack is small enough to be exact", /AT_BOTTOM_SLACK = 4/.test(src), true);
check("there is a pull threshold", /PULL_THRESHOLD = 60/.test(src), true);
check("the gesture is wired up once", (src.match(/initEndlessGallery\(\)/g) || []).length >= 2, true);

/* One fetch per *visit to the end*, counted as arrivals.

   Four designs were tried and each failed a real browser test, which is why the
   comment here is long:
     - an `armed` flag cleared on fetch and set by a `scroll` listener: a
       programmatic jump between the top and the bottom emits no intermediate
       event, so the flag stayed disarmed for the rest of the session;
     - re-arming on a timer since the last fetch: a long momentum scroll could
       queue a second page;
     - recording the height in the fetch's `finally`: the grid is still laying
       out then, so the captured height was an intermediate one;
     - comparing the page height against that recorded baseline: it cannot tell
       "still leaning on the trackpad" apart from "the page grew underneath me and
       I scrolled down to the new end", so the second case stopped working.

   A visit counter is the honest signal: leaving the end starts a new visit, and a
   fetch serves exactly one visit. */
check(
  "leaving the end starts a new visit",
  /const noteArrival = \(\) => \{\s*if \(!isAtPageBottom\(\)\) \{\s*stateful\.visit \+= 1;/.test(src),
  true
);
check(
  "the visit is counted on scroll, which covers every way of moving",
  /addEventListener\("scroll", noteArrival, \{ passive: true \}\)/.test(src),
  true
);
check(
  "a fetch serves one visit, and only one",
  /stateful\.servedVisit >= stateful\.visit/.test(src) &&
    /stateful\.servedVisit = stateful\.visit;/.test(src),
  true
);
check(
  "the first pull is allowed before anything has been served",
  /servedVisit: -1/.test(src),
  true
);
check(
  "the arrival is re-checked once the fetch has grown the page",
  /setTimeout\(\(\) => \{\s*noteArrival\(\);/.test(src),
  true
);
check(
  "a fetch in flight cannot be started twice",
  /stateful\.settling/.test(src),
  true
);
check(
  "the page-height comparison is gone, not left half-wired",
  !/firedAtHeight/.test(src) && !/pageHeight/.test(src),
  true
);
check(
  "the old armed flag is gone, not left half-wired",
  !/stateful\.armed/.test(src) && !/\brearm\b/.test(src),
  true
);
check(
  "wheel and touch both count",
  /addEventListener\(\s*"wheel"/.test(src) && /addEventListener\(\s*"touchmove"/.test(src),
  true
);
check(
  "overlays keep their own scrolling",
  /state\.currentPreviewId \|\| isOverlayOpen\(\)/.test(src),
  true
);

/* The bottom test itself is arithmetic, so it can be exercised directly. */
{
  const factory = new Function(
    "window",
    "document",
    `${extractConst("AT_BOTTOM_SLACK")}
     ${extract("isAtPageBottom")}
     return isAtPageBottom();`
  );
  const probe = (scrollY, height, inner) =>
    factory(
      { scrollY, pageYOffset: scrollY, innerHeight: inner },
      {
        documentElement: { scrollTop: scrollY, scrollHeight: height },
        body: { scrollHeight: height },
      }
    );

  check("scrolled exactly to the end counts as the bottom", probe(1200, 2000, 800), true);
  check("a couple of px short is still the bottom", probe(1198, 2000, 800), true);
  check("a visible gap is not the bottom", probe(1190, 2000, 800), false);
  check("half way down is not the bottom", probe(600, 2000, 800), false);
}

/* ------------------------------------------------------------ visual search
   The visual mode looks at the pictures instead of the captions, which is the
   only way to find "那张逆光的照片". It costs a request per few photos, so the UI
   has to be honest about how much was actually looked at. */
section("看图搜索");

check(
  "the mode is offered in the picker",
  /value="visual"/.test(
    fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8")
  ),
  true
);
check(
  "visual goes through the search endpoint, not the list endpoint",
  /mode === "semantic" \|\| mode === "smart" \|\| mode === "visual"/.test(src),
  true
);
check(
  "the UI says how many photos were looked at",
  /看图搜索只查看了最前面的/.test(src),
  true
);
check(
  "an unknown mode is a clear error, not a silent empty result",
  /不支持的搜索方式/.test(
    fs.readFileSync(path.join(__dirname, "..", "photo_reviewer", "server.py"), "utf8")
  ),
  true
);

/* --------------------------------------------------------------- 足迹
   One card per place, with the years it was visited. No map: a real one needs a
   tile server and a connection, and this app is local on purpose - the place list
   says the same thing without either. */
section("足迹");

check(
  "the footprint cards are grouped by city",
  /footprint-card[\s\S]{0,200}data-city=/.test(src),
  true
);
check(
  "the cover thumbnail is content-addressed so it caches forever",
  /api\/thumbnail\/\$\{cover\.id\}\?v=/.test(src),
  true
);
check(
  "a place with no cover still renders a card",
  /footprint-noimg/.test(src),
  true
);
check(
  "the years are collapsed into a span",
  /years\[years\.length - 1\]/.test(src),
  true
);
check(
  "an empty library says so in the page's own voice",
  /还没有记录的足迹/.test(src),
  true
);

/* ------------------------------------------------------------- export
   Two formats, two download paths. The filename comes from the server so it stays
   the one place that decides how an export is named; the client only has to read
   the header and hand the blob to an anchor. */
section("export");

check("the export dialog offers both formats", /存成网页/.test(
  fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8")
) && /存成长图/.test(
  fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8")
), true);
check(
  "the download reads the filename from the response",
  /Content-Disposition/.test(src) && /filename="\(\[\^"\]\+\)"/.test(src),
  true
);
check(
  "the blob url is released so it does not leak",
  /URL\.revokeObjectURL/.test(src),
  true
);
check(
  "a failed export says so instead of failing silently",
  /导出失败/.test(src),
  true
);
check(
  "the date export reuses the same query the sheet loaded",
  /state\.memoriesQuery = data\.total/.test(src),
  true
);

/* ------------------------------------------------------------- 那年今日
   A year count has to read as a date, not as a number: "两年前的今天", never
   "2 年前的今天". The phrasing is the whole feature - it is what makes the sheet
   feel like a memory rather than a filtered list. */
section("那年今日");

const YEARS_AGO = new Function(`${extract("yearsAgoText")} return yearsAgoText;`)();

check("the current year is not a memory", YEARS_AGO(0), "今年的今天");
check("one year back reads naturally", YEARS_AGO(1), "一年前的今天");
check("two years back reads naturally", YEARS_AGO(2), "两年前的今天");
check("three years back switches to digits", YEARS_AGO(3), "3 年前的今天");
check("a distant year still reads", YEARS_AGO(12), "12 年前的今天");

check(
  "years are grouped, newest first, from the API",
  /data\.years\.map\(memoryYearHtml\)/.test(src),
  true
);
check(
  "an empty day says so in the page's own voice",
  /往年的这一天，还没有留下什么/.test(src),
  true
);
check(
  "the window is a setting, not a hard-coded guess",
  /memory_window_days:\s*parseInt\(\$\("setMemoryWindow"\)/.test(src),
  true
);
check(
  "the settings sheet offers the window",
  /id="setMemoryWindow"/.test(
    fs.readFileSync(path.join(__dirname, "..", "static", "index.html"), "utf8")
  ),
  true
);

/* ------------------------------------------------------------- duplicate review
   The review sheet starts from "keep one per group" because that is what almost
   everyone wants; ticking the surplus by hand across hundreds of groups is not a
   review. The keeper itself must never be selectable, or a group could be emptied
   and the "keep" promise would be a lie. */
section("duplicate review");

const DUP_LABEL = extract("dupThresholdLabel");
const DUP_KEEP_ONE = extract("applyKeepOneRule");
const DUP_CLEAR = extract("clearChosen");
const dupLabel = new Function(
  `${extractConst("DUP_THRESHOLD_LABELS")} ${DUP_LABEL} return dupThresholdLabel;`
)();

check("an exact match reads as such", dupLabel(0), "几乎相同");
check("a tight threshold reads as close", dupLabel(3), "很接近");
check("the default reads as loose", dupLabel(6), "宽松");
check("a very loose threshold says so", dupLabel(9), "很宽松");
check("the widest threshold describes composition", dupLabel(12), "只看构图");

check(
  "the sheet does NOT pre-tick anything - the reader decides",
  // Opening the sheet used to tick every copy but the suggested keeper, which
  // meant one stray click on a destructive button discarded a whole shoot. The
  // keeper is a suggestion now, applied only when asked for.
  /dupState\.chosen\.clear\(\);/.test(src) &&
    !/applyKeepOneRule\(\);\s*\n\s*const summary/.test(src),
  true
);
check(
  "the per-group shortcut exists",
  /dup-group-keep/.test(src) && /applyKeepOneRule\(index\)/.test(src),
  true
);
/* The suggested keeper is selectable now. "Keep the best copy" is advice, and
   there are real cases where the higher-resolution file is the one to drop - the
   reader decides, so nothing is locked. What the app does instead is warn when a
   whole group would be removed. */
check(
  "the keeper's checkbox is selectable",
  !/isKeeper \? "disabled" : ""/.test(src) &&
    /aria-label="收起这张"/.test(src),
  true
);
check(
  "a group with everything ticked is called out",
  /is-emptied/.test(src) && /整组都会被收起/.test(src),
  true
);
check(
  "the confirmation counts emptied groups",
  /dupEmptiedGroups\(\)/.test(src) && /会被全部收起/.test(src),
  true
);
check(
  "the picture opens the photo, the checkbox ticks",
  /class="dup-shot"/.test(src) && /data-open/.test(src),
  true
);
check(
  "the duplicate card is not a label any more",
  !/<label class="dup-card/.test(src),
  true
);
check(
  "the threshold is remembered",
  /saveUiState\(\{ dup_threshold/.test(src) && /saved\.dup_threshold/.test(src),
  true
);
check(
  "nothing is deleted directly - it goes through the bin",
  /api\("\/api\/delete"/.test(src) && /sync_card: false/.test(src),
  true
);
check(
  "groups animate in, staggered like the gallery cards",
  /animation-delay:\$\{Math\.min\(index, 12\) \* 0\.03\}s/.test(src),
  true
);

{
  // One group: the shortcut ticks its duplicates and leaves the keeper alone.
  const groups = [
    { keep_id: 2, photos: [{ id: 1 }, { id: 2 }, { id: 3 }] },
    { keep_id: 5, photos: [{ id: 4 }, { id: 5 }] },
  ];
  const chosen = new Set();
  const run = new Function(
    "dupState",
    `${DUP_KEEP_ONE}; return applyKeepOneRule;`
  )({ groups, chosen });
  run(0);
  check("the group's duplicates are ticked", [1, 3].every((id) => chosen.has(id)), true);
  check("the keeper is not ticked", chosen.has(2), false);
  check("the other group is untouched", chosen.has(4) || chosen.has(5), false);
  check("only this group was ticked", chosen.size, 2);
}

{
  // All groups: still never the default, but available on request.
  const groups = [
    { keep_id: 2, photos: [{ id: 1 }, { id: 2 }, { id: 3 }] },
    { keep_id: 5, photos: [{ id: 4 }, { id: 5 }] },
  ];
  const chosen = new Set([99]); // a tick the reader made by hand
  const run = new Function(
    "dupState",
    `${DUP_KEEP_ONE}; return applyKeepOneRule;`
  )({ groups, chosen });
  run();
  check("every duplicate is ticked", [1, 3, 4].every((id) => chosen.has(id)), true);
  check("the keepers are not ticked", chosen.has(2) || chosen.has(5), false);
  check("a hand-made tick survives", chosen.has(99), true);
}

{
  // Clearing one group leaves the others alone.
  const groups = [
    { keep_id: 2, photos: [{ id: 1 }, { id: 2 }] },
    { keep_id: 5, photos: [{ id: 4 }, { id: 5 }] },
  ];
  const chosen = new Set([1, 4]);
  const run = new Function(
    "dupState",
    `${DUP_CLEAR}; return clearChosen;`
  )({ groups, chosen });
  run(0);
  check("clearing a group drops its ticks", chosen.has(1), false);
  check("clearing a group leaves others alone", chosen.has(4), true);
  run();
  check("clearing everything empties the set", chosen.size, 0);
}

/* ------------------------------------------------------------ modal behaviour
   Two gaps surfaced once four new sheets existed, both of them older than the new
   code:

   - Escape only ever closed the fullscreen viewer and the photo preview, because
     those were the only dialogs when that handler was written. Everything else
     needed a click on the × button.
   - `showModal` does not close anything, so clicking three toolbar buttons left
     three sheets stacked on the page.

   "Topmost" has to follow the order dialogs were *opened*, not document order:
   the folder browser is opened over the import sheet while sitting before it in
   the markup. */
section("dialogs (Escape and stacking)");

check(
  "an open-order stack exists",
  /modalStack: \[\]/.test(src),
  true
);
check(
  "opening a dialog records it",
  /function pushModal\(el\)/.test(src) && /state\.modalStack\.push\(el\.id\)/.test(src),
  true
);
check(
  "closing a dialog removes it however it was dismissed",
  /state\.modalStack\.lastIndexOf\(id\)/.test(src),
  true
);
check(
  "Escape closes the most recently opened dialog",
  /state\.modalStack\[state\.modalStack\.length - 1\]/.test(src),
  true
);
check(
  "Escape still defers to the preview and the viewer",
  /if \(preview && !preview\.classList\.contains\("hidden"\)\) return;/.test(src) &&
    /if \(fullscreen && !fullscreen\.classList\.contains\("hidden"\)\) return;/.test(src),
  true
);
check(
  "Escape leaves the confirm dialog to its own promise",
  /if \(confirm && !confirm\.classList\.contains\("hidden"\)\) return;/.test(src),
  true
);
check(
  "a top-level sheet dismisses the others",
  /function showSheet\(el\)/.test(src) &&
    /if \(modal\.id === "confirmModal"\) continue;/.test(src),
  true
);
check(
  "the three entries switch views instead of opening dialogs",
  /showView\("footprints"\)/.test(src) &&
    /showView\("memories"\)/.test(src) &&
    /showView\("duplicates"\)/.test(src),
  true
);
check(
  "no code still reaches for the old modal ids",
  !/\$\("footprintsModal"\)/.test(src) &&
    !/\$\("memoriesModal"\)/.test(src) &&
    !/\$\("duplicatesModal"\)/.test(src),
  true
);
check(
  "the four views are declared with a switcher button each",
  /const VIEWS = \[/.test(src) && /albumViewBtn/.test(src),
  true
);
check(
  "the nested pair keeps using showModal directly",
  // The folder browser opens over the import sheet and must return to it, so it
  // must not go through the mutual-exclusion path.
  /showModal\(\$\("dirModal"\)\);\s*\n\s*pushModal\(\$\("dirModal"\)\);/.test(src),
  true
);

{
  // Exercise the stack itself: push, dedupe, remove, and the topmost lookup.
  const stack = [];
  const push = (id) => {
    const at = stack.lastIndexOf(id);
    if (at !== -1) stack.splice(at, 1);
    stack.push(id);
  };
  const remove = (id) => {
    const at = stack.lastIndexOf(id);
    if (at !== -1) stack.splice(at, 1);
  };

  push("importModal");
  push("dirModal");
  check("the browser is on top of the import sheet", stack[stack.length - 1], "dirModal");
  remove("dirModal");
  check("closing it reveals the sheet underneath", stack[stack.length - 1], "importModal");
  push("importModal");
  check("re-opening does not duplicate the entry", stack.length, 1);
  remove("importModal");
  check("an empty stack means nothing to close", stack.length, 0);
}

/* ------------------------------------------------------------- toolbar layout
   Ten controls in one wrapping flex line left one or two of them stranded on a
   second row at ordinary window widths - measured at 1468px, 重复照片 and the
   settings gear ended up alone on row two, which reads as a layout accident. The
   browsing entries now have their own row, so every control has a reason to be
   where it is. */
section("toolbar layout");

{
  const html = fs.readFileSync(
    path.join(__dirname, "..", "static", "index.html"),
    "utf8"
  );
  /* The four views are a tab strip now, not a row of buttons: it sits on its own
     centred line, and the settings gear is pinned to that line's right edge so it
     cannot pull the tabs off centre. */
  check("the switcher has its own row", /class="view-switcher-row"/.test(html), true);
  check(
    "the switcher is a tablist",
    /class="view-switcher" role="tablist"/.test(html),
    true
  );
  check(
    "it has a sliding ink element",
    /class="view-ink" id="viewInk"/.test(html),
    true
  );
  check(
    "there are four tabs",
    (html.match(/class="view-tab"/g) || []).length,
    4
  );
  check(
    "相册 is the first tab, left of 足迹",
    /albumViewBtn[\s\S]{0,400}footprintsBtn/.test(html),
    true
  );
  check(
    "the third tab reads 相似回忆",
    /duplicatesBtn[\s\S]{0,220}相似回忆/.test(html),
    true
  );
  check(
    "the album is the default view",
    /id="albumView" class="page-view"/.test(html),
    true
  );
  check(
    "the album controls stay in the toolbar",
    /folder-row[\s\S]{0,2200}scanBtn[\s\S]{0,600}importBtn[\s\S]{0,900}cardStatusBtn/
      .test(html),
    true
  );
  check(
    "settings lives on the switcher's row",
    /view-switcher-row[\s\S]{0,1400}id="settingsBtn"/.test(html),
    true
  );
  check(
    "the multi-select shelf sits outside the album view",
    // Inside `#albumView` the page's entrance transform would become its containing
    // block and push this fixed bar below the fold. It is a sibling of the view now,
    // placed just above the preview dialog.
    (() => {
      const album = html.indexOf('id="albumView"');
      const shelf = html.indexOf('id="selectionBar"');
      const preview = html.indexOf('id="previewModal"');
      return album < shelf && shelf < preview;
    })(),
    true
  );
}

{
  const css = fs.readFileSync(
    path.join(__dirname, "..", "static", "style.css"),
    "utf8"
  );
  check("the secondary row has its own spacing", /\.folder-row\.row-secondary/.test(css), true);
  check(
    "the end-of-row item is pushed right",
    /\.folder-row \.row-end[\s\S]{0,60}margin-left:\s*auto/.test(css),
    true
  );
  check(
    "every button shares one height",
    // The rule puts the fixed height a little way into the block, after the flex
    // and gap declarations.
    /\.btn \{[\s\S]{0,900}?height:\s*36px/.test(css),
    true
  );
  check(
    "a disabled button looks disabled",
    /\.btn:disabled,[\s\S]{0,40}\.btn\[disabled\][\s\S]{0,120}opacity/.test(css),
    true
  );
}

/* ------------------------------------------------------------------- copy
   The sentences are the feature in a page like this, so the ones that are
   assembled rather than written out get a test. The export lede used to splice a
   fragment into a fixed frame, which read as "把这次的 3 张收进…" for a selection
   (no measure word) and repeated 的 for a date. */
section("generated copy");

{
  const ledeFor = new Function(
    `${extract("openExportDialog")};`
  );
  check("the export lede can be built", typeof ledeFor === "function", true);
}

check(
  "the export lede takes a whole clause, not a fragment",
  /options\.subject \|\|/.test(src),
  true
);
check(
  "a digit-led subject gets the space 把 needs",
  /\/\^\[0-9\]\/\.test\(subject\) \? " " : ""/.test(src),
  true
);
check(
  "both export entries pass a full clause",
  /subject: `选中的 \$\{ids\.length\} 张照片`/.test(src) &&
    /subject: `\$\{state\.memoriesQuery\.month\} 月/.test(src),
  true
);

/* Every generated sentence the reader can see, checked for the mistakes this
   section is about: a doubled 的, a missing space before a number, a sentence
   that ends without punctuation. */
check(
  "no template doubles 的的",
  !/的的/.test(src),
  true
);

/* --------------------------------------------------- photobook & motion system
   Emil Kowalski design engineering rules: full-bleed photobook spreads,
   sticky album filter console under topbar, paired enter/exit animations,
   and zero ease-in on UI exits. */
section("photobook layout and motion system");

{
  const html = fs.readFileSync(
    path.join(__dirname, "..", "static", "index.html"),
    "utf8"
  );
  const css = fs.readFileSync(
    path.join(__dirname, "..", "static", "style.css"),
    "utf8"
  );

  check(
    "album stats and filters are wrapped in the sticky console bar",
    /id="albumStickyBar" class="album-sticky-bar"[\s\S]{0,600}id="statsBar"[\s\S]{0,1800}class="filters"/.test(
      html
    ),
    true
  );
  check(
    "the album sticky bar is position: sticky under the topbar",
    /\.album-sticky-bar \{[\s\S]{0,160}position:\s*sticky;[\s\S]{0,160}top:\s*var\(--topbar-height/.test(
      css
    ),
    true
  );
  check(
    "page views spread full-bleed instead of a 1180px centered box",
    /\.page-view \.view-inner \{[\s\S]{0,120}max-width:\s*none/.test(css) &&
      !/max-width:\s*1180px/.test(css),
    true
  );
  check(
    "page views have both entrance (viewIn) and exit (viewOut) keyframes",
    /@keyframes viewIn/.test(css) &&
      /@keyframes viewOut/.test(css) &&
      /\.page-view\.view-leaving/.test(css),
    true
  );
  check(
    "showView sequences view-leaving before activating the incoming view",
    /previousPanel\.classList\.add\("view-leaving"\)/.test(src) &&
      /viewTransitionTimer = setTimeout/.test(src),
    true
  );
  check(
    "Footprints drill-down and return use is-transitioning-out exit transition",
    /body\.classList\.add\("is-transitioning-out"\)/.test(src) &&
      /\.footprints-body\.is-transitioning-out/.test(css),
    true
  );
  check(
    "Memories uses the album's polaroid and the album's grid",
    /\.memory-strip \{[\s\S]{0,200}grid-template-columns: repeat\(auto-fill, minmax\(300px, 1fr\)\)/
      .test(css),
    true
  );
  check(
    "足迹's place grid uses the same card and the same grid",
    /\.footprint-photos \{[\s\S]{0,200}grid-template-columns: repeat\(auto-fill, minmax\(300px, 1fr\)\)/
      .test(css),
    true
  );
  check(
    "the bespoke memory card is gone",
    !/\.memory-card \{/.test(css) && !/\.footprint-photo \{/.test(css),
    true
  );
  check(
    "Duplicates dead modal-close button is gone and replaced by the floating dock",
    !/data-close="duplicatesModal"/.test(html) &&
      /id="duplicatesFooter" class="duplicates-actions"/.test(html),
    true
  );
  check(
    "selection bar, scan progress, and card banner have paired exit animations",
    /\.selection-bar\.closing/.test(css) &&
      /\.scan-progress\.closing/.test(css) &&
      /\.card-banner\.closing/.test(css) &&
      /function hideStrip\(el\)/.test(src),
    true
  );
  check(
    "no ease-in rule remains in style.css",
    !/:\s*[^;]*\bease-in\b(?!-out)/.test(css),
    true
  );
}

/* ------------------------------------------------------- cross-view stability
   The four views differ in height, so switching between them adds and removes a
   scrollbar. That took 8px of width and shifted every centred element by 4px, which
   reads as the page jumping when only the content changed. */
section("cross-view stability");

{
  // Read the sources here: this block runs at file scope, after the scopes that
  // defined `css` and `src` for the earlier sections.
  const stylesheet = fs.readFileSync(
    path.join(__dirname, "..", "static", "style.css"),
    "utf8"
  );
  const script = fs.readFileSync(
    path.join(__dirname, "..", "static", "app.js"),
    "utf8"
  );

  check(
    "the scrollbar gutter is reserved",
    /html \{[\s\S]{0,80}scrollbar-gutter: stable/.test(stylesheet),
    true
  );
  check(
    "older browsers get the fallback",
    /@supports not \(scrollbar-gutter: stable\)[\s\S]{0,80}overflow-y: scroll/.test(
      stylesheet
    ),
    true
  );

  /* A polaroid must fill its track. Without `width: 100%` a card takes its content's
     width, so the album's card (with action buttons) filled the column while a
     place's card - which has none - shrank by a few pixels. */
  check(
    "the polaroid fills its track",
    /\.polaroid-card \{[\s\S]{0,400}?width: 100%/.test(stylesheet),
    true
  );
  check(
    "那年今日 does not inset its grid",
    /\.memory-year \{[\s\S]{0,240}padding: 0/.test(stylesheet),
    true
  );
  check(
    "all three grids share one column rule",
    (stylesheet.match(/repeat\(auto-fill, minmax\(300px, 1fr\)\)/g) || []).length >= 3,
    true
  );

  /* Re-entering a view replaces its markup, so the pictures must be re-registered
     with the lazy loader - otherwise they stay at `data-src` and `opacity: 0`. */
  check(
    "rendering 那年今日 registers its pictures",
    /function renderMemories[\s\S]{0,900}watchPolaroids\(body\)/.test(script),
    true
  );
  check(
    "appending a place's pictures registers them too",
    /list\.insertAdjacentHTML[\s\S]{0,200}watchPolaroids\(list\)/.test(script),
    true
  );
  check(
    "the reveal is not scoped to the gallery",
    /document\.addEventListener\(/.test(script) &&
      !/gallery\.addEventListener\([\s\S]{0,40}"load"/.test(script),
    true
  );

  /* Changing the album must refresh whatever view is on screen, not just the gallery. */
  check(
    "changing the album refreshes the open view",
    /function reloadCurrentView\(\)/.test(script) &&
      /case "footprints":/.test(script) &&
      /case "memories":/.test(script),
    true
  );
  check(
    "the folder input goes through it",
    /state\.folder = val;[\s\S]{0,200}reloadCurrentView\(\)/.test(script),
    true
  );

  const htmlDoc = fs.readFileSync(
    path.join(__dirname, "..", "static", "index.html"),
    "utf8"
  );
  check(
    "page title is 拾光相册 / Lumina without Album",
    /<title>拾光相册 \/ Lumina<\/title>/.test(htmlDoc) &&
      !/<title>[^<]*Album/i.test(htmlDoc),
    true
  );
  check(
    "favicon.svg is linked in the document head and exists on disk",
    /<link rel="icon" type="image\/svg\+xml" href="\/static\/favicon\.svg"/.test(htmlDoc) &&
      fs.existsSync(path.join(__dirname, "..", "static", "favicon.svg")),
    true
  );
  check(
    "brand sits inside the top view-switcher-row without a left icon",
    /class="view-switcher-row"[\s\S]{0,160}class="brand"[\s\S]{0,160}<h1>拾光相册/.test(
      htmlDoc
    ) && !/class="brand"[\s\S]{0,120}data-icon/.test(htmlDoc),
    true
  );
  check(
    "filter controls and custom select triggers share the 36px toolbar height",
    /\.filter-group select,\s*\.filter-group input \{[\s\S]{0,80}height:\s*36px/.test(
      stylesheet
    ) &&
      /\.custom-select-trigger \{[\s\S]{0,80}height:\s*36px/.test(stylesheet),
    true
  );
  check(
    "segmented search capsule integrates searchMode, searchInput, and searchBtn",
    /class="search-capsule"[\s\S]{0,600}id="searchMode"[\s\S]{0,400}id="searchInput"[\s\S]{0,300}id="searchBtn"/.test(
      htmlDoc
    ) && /\.search-capsule \{/.test(stylesheet),
    true
  );
  check(
    "curated photobook palette tokens (#f8f4ec, #261f19, #ae764e) are active in :root",
    /--bg:\s*#f8f4ec/i.test(stylesheet) &&
      /--text:\s*#261f19/i.test(stylesheet) &&
      /--primary:\s*#ae764e/i.test(stylesheet),
    true
  );
  check(
    "empty state distinguishes filter/search miss from an empty folder",
    /没有寻访到相符的回忆/.test(script) && /id="emptyResetFiltersBtn"/.test(htmlDoc),
    true
  );
}

console.log(
  failures
    ? `\n${failures} of ${checks} checks FAILED`
    : `\nAll ${checks} frontend helper checks passed`
);
process.exit(failures ? 1 : 0);
