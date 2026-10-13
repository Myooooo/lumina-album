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

const PHASE_LABELS = { plan: "正在比对", copy: "正在拷贝", index: "正在索引", analyze: "正在解读" };

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
check("seconds", helpers.formatEta(12), "剩 12 秒");
check("just under a minute", helpers.formatEta(59), "剩 59 秒");
check("exactly a minute", helpers.formatEta(60), "剩 1 分");
check("minutes and seconds", helpers.formatEta(200), "剩 3 分 20 秒");
check("exactly an hour", helpers.formatEta(3600), "剩 1 小时");
check("hours and minutes", helpers.formatEta(3900), "剩 1 小时 5 分");
check("negative clamps", helpers.formatEta(-9), "即将完成");

/* ------------------------------------------------------------------- ETA -- */
section("remaining-time estimate");

// A steady job: 5 files every second, 100 in total.
helpers.reset();
clock.set(1000);
helpers.updateEta(10, 100, 2); // the server says 2s have passed
check("the first estimate is seeded from the server's elapsed time", helpers.eta().rate, 5);
check("so an estimate is available immediately", helpers.lastPainted(), "剩 18 秒");

clock.advance(1000);
helpers.updateEta(15, 100, 3);
clock.advance(1000);
helpers.updateEta(20, 100, 4);
check("a steady job keeps a steady estimate", helpers.lastPainted(), "剩 17 秒");

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
check("first poll after a refresh still has an estimate", helpers.lastPainted(), "剩 45 秒");
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
  "a scan phase names the current file",
  helpers.progressLabel({ phase: "copy", processed: 2, total: 8, current: "DCIM/a.jpg" }),
  "正在拷贝 2/8：DCIM/a.jpg"
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

console.log(
  failures
    ? `\n${failures} of ${checks} checks FAILED`
    : `\nAll ${checks} frontend helper checks passed`
);
process.exit(failures ? 1 : 0);
