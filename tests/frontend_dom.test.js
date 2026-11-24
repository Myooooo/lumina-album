/* ==========================================================================
   Frontend DOM smoke test - run with:  node tests/frontend_dom.test.js

   Loads the real index.html and the shipped classic scripts into jsdom, boots
   the app against a stubbed API, and checks the things only a live DOM can
   prove: the gallery paints from the first page, toast text is escaped, dialogs
   layer in the order they were opened and Escape closes the topmost one.

   Requires `npm install` (jsdom is the only dev dependency). The application
   itself still has no build step.
   ========================================================================== */
"use strict";

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const STATIC = path.join(__dirname, "..", "static");
const indexHtml = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const scriptFiles = [...indexHtml.matchAll(/<script src="\/static\/([^"]+)"><\/script>/g)].map(
  (match) => match[1]
);

let passed = 0;
const failures = [];
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed += 1;
  } else {
    failures.push({ name, actual, expected });
  }
  return ok;
}

function makeRow(overrides = {}) {
  return {
    id: 1,
    path: "E:\\album\\a.jpg",
    filename: "a.jpg",
    size: 1024,
    width: 400,
    height: 300,
    location: null,
    dimensions: { technical: 8, memory: 7, uniqueness: 6 },
    score: 8,
    tags: ["海边"],
    reason: "风吹过。",
    title: "海边",
    analyzed_at: "2026-01-01T00:00:00+00:00",
    status: "analyzed",
    favorite: false,
    error: null,
    raw_path: null,
    has_raw: false,
    is_raw: false,
    raw_ext: "",
    thumb_version: "v1",
    media_kind: "photo",
    duration_ms: null,
    has_live: false,
    ...overrides,
  };
}

const API = {
  "/api/config": {
    api_base_url: "http://localhost:1234/v1",
    model: "test-model",
    system_prompt: "",
    proxy_max_edge: 1280,
    gallery_thumb_size: 640,
    proxy_quality: 80,
    thumb_quality: 50,
    scan_concurrency: 3,
    index_concurrency: 4,
    request_timeout: 120,
    model_retries: 3,
    geocoding_provider: "nominatim",
    geocoding_api_key: "",
    geocoding_interval: 1,
    geocoding_retries: 3,
    video_clip_seconds: 3,
    video_clip_max_edge: 960,
    video_clip_crf: 28,
    video_clip_audio: true,
    video_pregenerate: false,
    video_poster_ratio: 0.1,
    video_preview_mode: "once",
    video_preview_volume: 50,
    video_processing: "ffmpeg",
    merge_live_photos: true,
    live_volume: 0,
    memory_window_days: 0,
  },
  "/api/ui-state": { state: { folder: "E:\\album", folder_history: ["E:\\album"] } },
  "/api/stats": { total: 1, analyzed: 1, favorite: 0, pending: 0, deleted: 0 },
  "/api/photos": { photos: [makeRow()], has_more: false },
  "/api/tags": { tags: ["海边"] },
  "/api/years": { years: [2025] },
  "/api/card/status": { cards: [], current: null },
  "/api/scan/jobs": { jobs: [] },
  "/api/prompt-presets": {
    presets: {
      playful: "【评价风格】\n像朋友聊天。\n\n【评分标准】\n9-10 留下。",
    },
    default: "playful",
    custom: [
      {
        id: "c1",
        name: "我的语气",
        text: "【评价风格】\n像家里人说话。\n\n【评分标准】\n7-8 留着。",
      },
    ],
    markers: {
      preamble: "你在帮一个人整理他拍过的照片",
      anti_ai: "【写作要求：说人话】",
      format: "【输出格式：必须严格遵守】",
      legacy_format:
        "Return ONLY a JSON object, no markdown, with exactly these keys:",
    },
  },
  "/api/video/status": { available: true, extensions: [], version: "" },
  "/api/folders": { folders: ["E:\\album"] },
  "/api/folders/hidden": { folders: ["E:\\album-hidden"] },
};

async function main() {
  const dom = new JSDOM(indexHtml, {
    url: "http://127.0.0.1:5050/",
    runScripts: "dangerously",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  const { document } = window;
  const problems = [];

  window.addEventListener("error", (event) => problems.push(event.message));
  process.on("unhandledRejection", (reason) => problems.push(String(reason)));

  // The browser surface the app uses that jsdom does not implement.
  window.matchMedia = (query) => ({
    matches: /prefers-reduced-motion/.test(query),
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return false;
    },
  });
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  window.IntersectionObserver = class {
    constructor(callback) {
      this.callback = callback;
    }
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  };
  window.requestAnimationFrame = (callback) => window.setTimeout(() => callback(Date.now()), 0);
  window.cancelAnimationFrame = (id) => window.clearTimeout(id);
  window.scrollTo = () => {};
  window.HTMLElement.prototype.scrollIntoView = () => {};

  const calls = [];
  window.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), body: options.body || "" });
    const key = String(url).split("?")[0];
    const decoded = decodeURIComponent(String(url));
    let body = API[key] ?? {};
    if (key === "/api/stats" && decoded.includes("E:\\empty")) {
      body = { total: 0, analyzed: 0, favorite: 0, pending: 0, deleted: 0 };
    } else if (key === "/api/photos" && decoded.includes("E:\\empty")) {
      body = { photos: [], has_more: false };
    } else if (key === "/api/photos" && decoded.includes("status=favorite")) {
      body = { photos: [], has_more: false };
    }
    return {
      ok: true,
      status: 200,
      statusText: "OK",
      headers: { get: () => null },
      json: async () => body,
      text: async () => "",
      blob: async () => new window.Blob([]),
    };
  };

  // The document has to be parsed before the app's scripts are injected: the
  // boot listener is registered when app.js runs, and we call `init` ourselves
  // afterwards so the timing is ours, not jsdom's.
  if (document.readyState === "loading") {
    await new Promise((resolve) =>
      document.addEventListener("DOMContentLoaded", resolve, { once: true })
    );
  }

  for (const relative of scriptFiles) {
    const script = document.createElement("script");
    script.textContent = fs.readFileSync(path.join(STATIC, relative), "utf8");
    document.head.appendChild(script);
  }

  // Boot exactly the way index.html's DOMContentLoaded listener would.
  window.init();
  await new Promise((resolve) => window.setTimeout(resolve, 400));

  check("the album page is the boot view", document.body.dataset.view, "album");
  check("the gallery renders the first page", document.querySelectorAll("#gallery .polaroid-card").length, 1);
  check("the stats row shows the library size", document.getElementById("statTotal").textContent, "1");

  const thumb = document.querySelector("#gallery img");
  const thumbUrl = thumb ? thumb.getAttribute("data-src") || thumb.getAttribute("src") : "";
  check("the thumbnail URL is content-addressed", /\/api\/thumbnail\/1/.test(thumbUrl), true);
  check("the thumbnail URL carries the version token", /v1/.test(thumbUrl), true);

  // --- hidden albums ----------------------------------------------------
  const badge = document.getElementById("folderHiddenBadge");
  check("a visible album carries no hidden marker", badge.classList.contains("hidden"), true);

  window.openAlbumFolder("E:\\album-hidden");
  await new Promise((resolve) => window.setTimeout(resolve, 80));
  check("opening a hidden album shows the marker", badge.classList.contains("hidden"), false);
  check(
    "the path field names the hidden album",
    document.getElementById("folderInput").value,
    "E:\\album-hidden"
  );
  check(
    "hidden albums stay out of the history dropdown",
    window.folderHistoryEntries(["E:\\album", "E:\\album-hidden"]).map((entry) => entry.path),
    ["E:\\album"]
  );
  check(
    "a hidden album is never saved as the current album",
    calls.filter(
      (call) => call.url === "/api/ui-state" && call.body.includes("album-hidden")
    ).length,
    0
  );

  window.openAlbumFolder("E:\\album");
  await new Promise((resolve) => window.setTimeout(resolve, 80));
  check("the marker clears on a visible album", badge.classList.contains("hidden"), true);

  // --- toast: text, never markup -----------------------------------------
  window.LuminaUI.showToast('<img src=x onerror="window.__pwned = 1">', "warning");
  const toast = document.querySelector("#toastContainer .toast");
  check("the toast rendered", Boolean(toast), true);
  check("the toast contains no injected element", toast ? toast.querySelector("img") : "missing", null);
  check("the toast kept the text as text", toast ? toast.textContent.includes("<img") : false, true);

  // --- dialog layering ------------------------------------------------
  const settings = document.getElementById("settingsModal");
  const edit = document.getElementById("editModal");
  window.LuminaUI.showModal(settings);
  window.LuminaUI.showModal(edit);
  check(
    "a dialog opened later sits above the one already open",
    Number(edit.style.zIndex) > Number(settings.style.zIndex),
    true
  );
  window.LuminaUI.hideModal(edit);
  window.LuminaUI.hideModal(settings);
  check("closing a dialog releases its inline layer", settings.style.zIndex, "");

  let highest = 0;
  for (let i = 0; i < 300; i += 1) {
    window.LuminaUI.showModal(settings);
    highest = Math.max(highest, Number(settings.style.zIndex));
    window.LuminaUI.hideModal(settings);
  }
  check("repeated open/close never climbs past the toast layer", highest <= 1101, true);

  // --- Escape unwinds by open order -----------------------------------
  window.LuminaUI.showModal(settings);
  window.pushModal(settings);
  window.LuminaUI.showModal(edit);
  window.pushModal(edit);
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  check("Escape closes the dialog opened last", edit.classList.contains("hidden"), true);
  check("Escape leaves the dialog underneath open", settings.classList.contains("hidden"), false);
  document.dispatchEvent(new window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  check("a second Escape closes the next dialog", settings.classList.contains("hidden"), true);

  // --- a backdrop click never dismisses a dialog ------------------------
  window.LuminaUI.showModal(settings);
  window.pushModal(settings);
  settings.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
  check(
    "clicking the backdrop leaves the dialog open",
    settings.classList.contains("hidden"),
    false
  );
  settings.querySelector(".modal-close").click();
  await new Promise((resolve) => window.setTimeout(resolve, 260));
  check(
    "the close button still closes it",
    settings.classList.contains("hidden"),
    true
  );

  // --- empty state ------------------------------------------------------
  window.openAlbumFolder("E:\\empty");
  await new Promise((resolve) => window.setTimeout(resolve, 120));
  const emptyText = document.getElementById("emptyStateText");
  const emptyReset = document.getElementById("emptyResetFiltersBtn");
  check(
    "a brand-new folder says the album is empty",
    emptyText.textContent.includes("这一册还是空白"),
    true
  );
  check(
    "an empty album offers no filter reset",
    emptyReset.classList.contains("hidden"),
    true
  );

  window.openAlbumFolder("E:\\album");
  await new Promise((resolve) => window.setTimeout(resolve, 120));
  check(
    "the album opens on all memories, not the analyzed subset",
    document.getElementById("statusFilter").value,
    "all"
  );
  window.applyStatFilter("favorite");
  await new Promise((resolve) => window.setTimeout(resolve, 120));
  check(
    "a filter with no match says so",
    emptyText.textContent.includes("没有寻访到"),
    true
  );
  check(
    "a filter with no match offers the reset",
    emptyReset.classList.contains("hidden"),
    false
  );

  // --- prompt editor ----------------------------------------------------
  await window.openSettings();
  await new Promise((resolve) => window.setTimeout(resolve, 120));

  // --- clicking a detail tag lands on the album filtered by that tag ------
  document.getElementById("favoriteFilter").value = "1";
  await window.openAlbumWithTag("旅行");
  await new Promise((resolve) => window.setTimeout(resolve, 120));
  check(
    "a detail tag returns to the album with that tag selected",
    document.getElementById("tagFilter").value,
    "旅行"
  );
  check(
    "a detail tag clears the previous favourites-only filter",
    document.getElementById("favoriteFilter").value,
    ""
  );
  check(
    "a detail tag keeps the album on all memories",
    document.getElementById("statusFilter").value,
    "all"
  );
  const liveBadge = window.liveBadgeHtml({ has_live: true });
  check(
    "the Live Photo badge carries a Live label for the vintage card",
    liveBadge.includes("live-badge-label") && liveBadge.includes("Live"),
    true
  );
  const rawBadge = window.rawBadgeHtml({ has_raw: true, raw_ext: "ARW" });
  check(
    "the RAW card badge carries its material/class and extension",
    rawBadge.includes("raw-badge-card") && rawBadge.includes("RAW") && rawBadge.includes("ARW"),
    true
  );
  check(
    "a raw-only photo gets the distinct raw-only marker",
    window.rawBadgeHtml({ is_raw: true, filename: "IMG_1.CR3" }).includes("raw-only"),
    true
  );

  // --- fullscreen switch clears its transform animation before zoom ------
  const appState = window.eval("state");
  const fsPhotoA = appState.photos[0];
  const fsPhotoB = appState.photos[1];
  if (fsPhotoA && fsPhotoB) {
    appState.currentPreviewId = fsPhotoA.id;
    window.openFullscreenViewer();
    window.renderFullscreenMedia(fsPhotoB, 1);
    check(
      "the fullscreen switch animation starts on the next photo",
      document.getElementById("fsImage").classList.contains("fs-switch-right"),
      true
    );
    await new Promise((resolve) => window.setTimeout(resolve, 320));
    check(
      "the switch animation is cleared so zoom can take over",
      document.getElementById("fsImage").classList.contains("fs-switch-right"),
      false
    );
    window.zoomFullscreen(1.25);
    check(
      "the next fullscreen photo can be zoomed",
      appState.fullscreen.scale,
      1.25
    );
    window.closeFullscreenViewer();
  }

  check(
    "the map key hint is a single element",
    document.querySelectorAll("#footprintsMapKeyHint").length,
    1
  );

  // --- map source fields keep custom values while hidden -----------------
  const mapProvider = document.getElementById("setMapTileProvider");
  const mapUrlRow = document.getElementById("setMapTileUrlRow");
  const mapKeyRow = document.getElementById("setMapApiKeyRow");
  const mapUrl = document.getElementById("setMapTileUrl");
  check(
    "the custom map URL is hidden on the default AMap source",
    mapUrlRow.classList.contains("hidden"),
    true
  );
  mapUrl.value = "https://example.test/{z}/{x}/{y}.png";
  mapProvider.value = "custom";
  mapProvider.dispatchEvent(new window.Event("change", { bubbles: true }));
  check(
    "choosing custom reveals the URL field",
    mapUrlRow.classList.contains("hidden"),
    false
  );
  mapProvider.value = "amap";
  mapProvider.dispatchEvent(new window.Event("change", { bubbles: true }));
  check(
    "switching away hides the URL field",
    mapUrlRow.classList.contains("hidden"),
    true
  );
  check(
    "the custom URL value survives the switch",
    mapUrl.value,
    "https://example.test/{z}/{x}/{y}.png"
  );
  mapProvider.value = "carto";
  mapProvider.dispatchEvent(new window.Event("change", { bubbles: true }));
  check(
    "CartoDB asks for its API key",
    mapKeyRow.classList.contains("hidden"),
    false
  );
  mapProvider.value = "amap";
  mapProvider.dispatchEvent(new window.Event("change", { bubbles: true }));

  const customTrigger = document.getElementById("customPromptBtn");
  const customMenu = document.getElementById("customPromptMenu");
  const promptTextarea = document.getElementById("setSystemPrompt");
  const promptName = document.getElementById("promptPresetName");
  const deletePrompt = document.getElementById("deletePromptPresetBtn");
  check(
    "saved prompts live behind a closed dropdown",
    customMenu.classList.contains("hidden"),
    true
  );

  customTrigger.click();
  await new Promise((resolve) => window.setTimeout(resolve, 20));
  const savedOption = document.querySelector('[data-custom-id="c1"]');
  check("opening it lists every saved prompt", Boolean(savedOption), true);
  if (savedOption) savedOption.click();
  await new Promise((resolve) => window.setTimeout(resolve, 20));
  check(
    "picking a saved prompt loads it into the editor",
    promptTextarea.value.includes("像家里人"),
    true
  );
  check(
    "the trigger is highlighted for a saved prompt",
    customTrigger.classList.contains("active"),
    true
  );
  check(
    "the delete action follows the selected prompt",
    deletePrompt.classList.contains("hidden"),
    false
  );
  check("the delete action reads 删除", deletePrompt.textContent.trim(), "删除");
  check("the menu closes after picking", customMenu.classList.contains("hidden"), true);

  // Editing the text keeps the saved prompt as the base being edited.
  promptTextarea.value += "\n再加一句。";
  promptTextarea.dispatchEvent(new window.Event("input", { bubbles: true }));
  check(
    "editing the text keeps the saved prompt active",
    customTrigger.classList.contains("active"),
    true
  );

  // A built-in is highlighted only while its text is exactly in the editor.
  const builtinBtn = document.querySelector('.preset-btn[data-preset="playful"]');
  builtinBtn.click();
  await new Promise((resolve) => window.setTimeout(resolve, 20));
  check(
    "choosing a built-in clears the saved prompt",
    customTrigger.classList.contains("active"),
    false
  );
  check("the built-in is highlighted", builtinBtn.classList.contains("active"), true);
  promptTextarea.value += "改写";
  promptTextarea.dispatchEvent(new window.Event("input", { bubbles: true }));
  check(
    "editing after a built-in drops its highlight",
    builtinBtn.classList.contains("active"),
    false
  );
  check(
    "a blank name falls back to a 自定义 name",
    window.eval("nextCustomPromptName()"),
    "自定义1"
  );

  // Touching the name field of a selected saved prompt means "save a new one".
  customTrigger.click();
  await new Promise((resolve) => window.setTimeout(resolve, 20));
  document.querySelector('[data-custom-id="c1"]').click();
  await new Promise((resolve) => window.setTimeout(resolve, 20));
  check(
    "the saved prompt is selected again",
    customTrigger.classList.contains("active"),
    true
  );
  promptName.value = "新名字";
  promptName.dispatchEvent(new window.Event("input", { bubbles: true }));
  check(
    "editing the name clears the saved selection",
    customTrigger.classList.contains("active"),
    false
  );
  check(
    "and hides the delete action",
    deletePrompt.classList.contains("hidden"),
    true
  );

  check(
    "the confirm dialog can host a themed name input",
    Boolean(document.getElementById("confirmInputRow")) &&
      Boolean(document.getElementById("confirmInput")),
    true
  );
  check(
    "settings exposes the face and ffmpeg one-click installers",
    Boolean(document.getElementById("downloadFaceModelsBtn")) &&
      document.getElementById("downloadFaceModelsBtn").textContent.includes("一键下载安装") &&
      Boolean(document.getElementById("installFfmpegBtn")) &&
      Boolean(document.getElementById("ffmpegInstallModal")),
    true
  );
  check(
    "the unnamed-cluster default is five",
    document.getElementById("setFaceMinCluster").value,
    "5"
  );
  check(
    "the map provider defaults to AMap",
    document.getElementById("setMapTileProvider").value,
    "amap"
  );
  check(
    "the duplicate fullscreen expand control is gone",
    Boolean(document.getElementById("previewExpandBtn")),
    false
  );

  const naming = window.LuminaUI.confirmDialog("为人物改名", {
    title: "修改名称",
    input: { label: "名称", value: "旧名字", placeholder: "输入名称" },
  });
  await new Promise((resolve) => window.setTimeout(resolve, 30));
  const confirmInput = document.getElementById("confirmInput");
  check(
    "the themed name dialog opens with its current value",
    confirmInput && confirmInput.value,
    "旧名字"
  );
  confirmInput.value = "新名字";
  document.getElementById("confirmOkBtn").click();
  const namingResult = await naming;
  check("the themed name dialog returns the typed value", namingResult.value, "新名字");
  check("confirming the name dialog is confirmed", namingResult.confirmed, true);

  window.LuminaUI.hideModal(document.getElementById("settingsModal"));

  check("booting and exercising the page raised no errors", problems, []);

  // The page arms a toast timer and the card-status poller; closing the window
  // disposes of every timer jsdom knows about, so the process can exit.
  window.close();

  if (failures.length) {
    for (const failure of failures) {
      console.log(`  FAIL ${failure.name}\n       got:  ${JSON.stringify(failure.actual)}\n       want: ${JSON.stringify(failure.expected)}`);
    }
    console.log(`\n${failures.length} of ${passed + failures.length} DOM checks FAILED`);
    process.exitCode = 1;
    return;
  }
  console.log(`All ${passed} DOM checks passed`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
