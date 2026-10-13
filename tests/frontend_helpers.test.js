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

const PHASE_LABELS = { plan: "正在比对", copy: "正在拷贝", index: "正在索引", analyze: "正在解读" };

const helpers = new Function(
  "PHASE_LABELS",
  `${extract("phaseLabel")}
   ${extract("progressLabel")}
   ${extract("jobSummaryText")}
   ${extract("formatEta")}
   return { phaseLabel, progressLabel, jobSummaryText, formatEta };`
)(PHASE_LABELS);

// ``nextPreviewAfterRemoval`` reads the module-level ``state``; bind a stub.
const nextPreviewAfterRemoval = new Function(
  "state",
  `${extract("nextPreviewAfterRemoval")} return nextPreviewAfterRemoval;`
);
const next = (ids, current, removed) =>
  nextPreviewAfterRemoval(
    { photos: ids.map((id) => ({ id })), currentPreviewId: current }
  )(removed);

let failures = 0;
let checks = 0;

function check(label, actual, expected) {
  checks += 1;
  if (actual === expected) return;
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

/* ------------------------------------------------------------ progressLabel */
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

console.log(
  failures
    ? `\n${failures} of ${checks} checks FAILED`
    : `\nAll ${checks} frontend helper checks passed`
);
process.exit(failures ? 1 : 0);
