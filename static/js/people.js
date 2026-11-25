/* ==========================================================================
   Lumina Album - People & Pets (人物与宠物)
   Local-first face & cat/dog clustering, inline naming, auto-merge, cover
   picking, and iOS-inspired "confirm additional photos" review strip.
   ========================================================================== */
"use strict";

const peopleState = {
  items: [],
  kind: "all", // "all" | "person" | "pet"
  includeHidden: false,
  mergeMode: false,
  mergeSelected: new Set(),
  currentPerson: null, // detail payload when inside one person/pet
  status: null,
  pollTimer: null,
  lastClusterVersion: 0,
  silentRefreshing: false,
  //: ETA state for the people progress card; uses the same estimator as the
  //: album scan, but writes to its own element.
  eta: null,
  //: True while the automatic scan is mirrored into the toolbar progress bar.
  homeTracking: false,
};

function kindBadgeLabel(kind) {
  if (kind === "cat") return "猫咪";
  if (kind === "dog") return "狗狗";
  return "";
}

function personDisplayName(person) {
  if (!person) return "未命名";
  const name = (person.name || "").trim();
  if (name) return name;
  if (person.kind === "cat") return "未命名猫咪";
  if (person.kind === "dog") return "未命名狗狗";
  return "未命名人物";
}

function renderPeopleScanStatus(job, options = {}) {
  const bar = $("peopleScanProgress");
  const textEl = $("peopleScanProgressText");
  const pctEl = $("peopleScanProgressPct");
  const etaEl = $("peopleScanEta");
  const fillEl = $("peopleScanProgressFill");
  const badge = $("peopleSilentBadge");
  const scanBtn = $("peopleScanBtn");
  if (!bar || !textEl) return;
  const running = Boolean(job && job.status === "running");
  const auto = Boolean(options.auto && running);
  bar.classList.toggle("hidden", !running);
  if (badge) badge.classList.toggle("hidden", !auto);
  if (running) {
    const total = Number(job.total) || 0;
    const done = Number(job.done) || 0;
    const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
    const foundParts = [];
    if (job.faces_found) foundParts.push(`${job.faces_found} 人脸`);
    if (job.pets_found) foundParts.push(`${job.pets_found} 宠物`);
    const suffix = foundParts.length ? ` · 已发现 ${foundParts.join(" / ")}` : "";
    textEl.textContent = (job.message || (total > 0 ? `正在扫描 ${done} / ${total} 张照片…` : "正在准备识别人物与宠物…")) + suffix;
    if (pctEl) pctEl.textContent = total > 0 ? `${pct}% · ${done}/${total}` : "准备中";
    if (fillEl) {
      fillEl.style.width = total > 0 ? `${pct}%` : "18%";
      fillEl.classList.toggle("is-indeterminate", total === 0);
    }
    // Share the album progress ETA estimator, but keep its own state and pill.
    if (etaEl) {
      if (total > 0) {
        updateEta(done, total, Number(job.elapsed) || 0, {
          holder: peopleState,
          el: etaEl,
        });
      } else {
        etaEl.classList.add("hidden");
        etaEl.textContent = "";
        peopleState.eta = null;
      }
    }
  } else {
    if (etaEl) {
      etaEl.classList.add("hidden");
      etaEl.textContent = "";
    }
    peopleState.eta = null;
  }
  if (scanBtn) {
    scanBtn.disabled = running;
  }
}

function stopPeoplePoll() {
  if (peopleState.pollTimer) {
    clearInterval(peopleState.pollTimer);
    peopleState.pollTimer = null;
  }
}

/** Mirror the automatic people scan into the toolbar progress strip. */
function paintAutoPeopleHome(job) {
  if (!job) return;
  const total = Number(job.total) || 0;
  const done = Number(job.done) || 0;
  const found = [];
  if (job.faces_found) found.push(`${job.faces_found} 人脸`);
  if (job.pets_found) found.push(`${job.pets_found} 宠物`);
  const suffix = found.length ? ` · 已发现 ${found.join(" / ")}` : "";
  $("progressText").textContent = `后台识别人物与宠物 ${total ? `${done}/${total}` : "…"}${suffix}`;
  // Reuse the album job painter for the bar width and ETA pill.
  paintJobProgress({
    processed: done,
    total,
    elapsed: Number(job.elapsed) || 0,
    status: job.status,
  });
}

function finishAutoPeopleHome(job) {
  if (!peopleState.homeTracking) return;
  peopleState.homeTracking = false;
  const done = Number(job.done) || 0;
  const total = Number(job.total) || 0;
  $("progressText").textContent = job.message || "人物与宠物识别完成";
  paintJobProgress({
    processed: done,
    total: total || done,
    elapsed: Number(job.elapsed) || 0,
    status: "completed",
  });
  if (state.progressHideTimer) clearTimeout(state.progressHideTimer);
  state.progressHideTimer = setTimeout(hideProgress, 3500);
  if (job.status === "error" && job.error) {
    showToast(`自动识别人物失败：${job.error}`, "error");
  }
}

function startPeoplePoll(options = {}) {
  stopPeoplePoll();
  const auto = Boolean(options.auto);
  const home = Boolean(options.home);
  let inFlight = false;

  const tick = async () => {
    if (inFlight) return;
    inFlight = true;
    try {
      const st = await api("/api/people/status");
      peopleState.status = st;
      const job = st.job || {};
      renderPeopleScanStatus(job, { auto });
      if (home && peopleState.homeTracking) paintAutoPeopleHome(job);
      const clusterVer = Number(job.cluster_version) || 0;
      if (job.status === "running") {
        if (clusterVer > peopleState.lastClusterVersion) {
          peopleState.lastClusterVersion = clusterVer;
          if (state.view === "people" && !peopleState.currentPerson && !peopleState.mergeMode) {
            refreshPeopleGridSilent();
          }
        }
        return;
      }
      stopPeoplePoll();
      peopleState.lastClusterVersion = clusterVer;
      renderPeopleScanStatus(job, { auto: false });
      if (home) finishAutoPeopleHome(job);
      if (job.status === "completed") {
        if (!auto) showToast(job.message || "人物与宠物识别完成", "success");
        if (state.view === "people") {
          if (peopleState.currentPerson) {
            openPersonDetail(peopleState.currentPerson.person.id);
          } else {
            refreshPeopleGridSilent();
          }
        }
      } else if (job.status === "cancelled") {
        if (state.view === "people" && !peopleState.currentPerson) {
          refreshPeopleGridSilent();
        }
      } else if (job.status === "error" && job.error) {
        if (!auto) showToast(`识别失败：${job.error}`, "error");
      }
    } catch (e) {
      stopPeoplePoll();
      renderPeopleScanStatus({ status: "error" }, { auto: false });
      if (home && peopleState.homeTracking) {
        peopleState.homeTracking = false;
        $("progressText").textContent = "读取人物识别进度失败";
        if (state.progressHideTimer) clearTimeout(state.progressHideTimer);
        state.progressHideTimer = setTimeout(hideProgress, 2500);
      }
    } finally {
      inFlight = false;
    }
  };

  peopleState.pollTimer = setInterval(tick, 800);
  tick();
}

/**
 * Start the automatic people scan that follows an album sync/organize.
 *
 * The album scan summary gets a short beat on the toolbar, then the same strip
 * switches to the people job. The people page is kept in sync by the shared
 * poller, so opening that tab while this runs still shows its progress card and
 * the "后台静默扫描中" badge.
 */
async function startAutoPeopleScanTracking() {
  await new Promise((resolve) => setTimeout(resolve, 900));
  if (state.currentJobId) return;
  try {
    const data = await api("/api/people/scan", {
      method: "POST",
      body: JSON.stringify({ folder: state.folder || "" }),
    });
    const job = data.job || {};
    peopleState.homeTracking = true;
    peopleState.eta = null;
    peopleState.lastClusterVersion = 0;
    showProgress("正在后台识别人物与宠物…");
    renderPeopleScanStatus(job, { auto: true });
    paintAutoPeopleHome(job);
    startPeoplePoll({ auto: true, home: true });
  } catch (e) {
    peopleState.homeTracking = false;
    if (state.progressHideTimer) clearTimeout(state.progressHideTimer);
    state.progressHideTimer = setTimeout(hideProgress, 2500);
  }
}


async function startPeopleScan(force = false) {
  try {
    const data = await api("/api/people/scan", {
      method: "POST",
      body: JSON.stringify({ folder: state.folder || "", force: !!force }),
    });
    peopleState.homeTracking = false;
    peopleState.eta = null;
    renderPeopleScanStatus(data.job, { auto: false });
    showToast("已在后台静默扫描人物与宠物，可继续浏览相册", "info");
    startPeoplePoll({ auto: false, home: false });
  } catch (e) {
    showToast(e.message || "无法启动人物识别", "warning");
    if (e.message && e.message.includes("设置")) {
      openSettings();
      if (typeof showSettingsTab === "function") {
        setTimeout(() => showSettingsTab("people"), 80);
      }
    }
  }
}

function peopleCardHtml(person, index, options = {}) {
  const isUnnamed = !(person.name || "").trim();
  const isPet = person.kind === "cat" || person.kind === "dog";
  const isSelected = peopleState.mergeSelected.has(person.id);
  const animate = options.animate !== false;
  const avatar = person.thumb_url
    ? `<img class="person-avatar-img" src="${escapeHtml(person.thumb_url)}" alt="${escapeHtml(personDisplayName(person))}" loading="lazy" decoding="async" />`
    : `<span class="person-avatar-fallback">${isPet ? ICONS.paw : ICONS.people}</span>`;
  const styleAttr = animate
    ? ` style="animation-delay:${Math.min(index, 18) * 0.03}s"`
    : ` style="animation:none"`;
  return `
    <div class="person-card${isUnnamed ? " is-unnamed" : ""}${isPet ? " is-pet" : ""}${person.is_hidden ? " is-hidden-person" : ""}${isSelected ? " is-merge-selected" : ""}"
         data-person-id="${person.id}"${styleAttr}>
      ${peopleState.mergeMode ? `<span class="person-merge-check">${isSelected ? ICONS.check : ""}</span>` : ""}
      <button type="button" class="person-avatar-btn" data-open-person="${person.id}">
        <span class="person-avatar-ring">
          ${avatar}
          ${person.is_favorite ? `<span class="person-fav-dot" title="已置顶">${ICONS.heartFilled}</span>` : ""}
          ${isPet ? `<span class="person-pet-badge" title="${kindBadgeLabel(person.kind)}">${ICONS.paw}</span>` : ""}
          ${person.pending_count > 0 ? `<span class="person-pending-dot" title="有 ${person.pending_count} 张疑似照片待确认">${person.pending_count}</span>` : ""}
        </span>
      </button>
      <div class="person-card-meta">
        <button type="button" class="person-name-btn" data-rename-person="${person.id}" title="点击命名或修改姓名">
          ${escapeHtml(personDisplayName(person))}
        </button>
        <span class="person-count">${person.count} 张${person.is_hidden ? " · 已隐藏" : ""}</span>
      </div>
    </div>
  `;
}

function updateMergeBar() {
  const bar = $("peopleMergeBar");
  const countEl = $("peopleMergeCount");
  const runBtn = $("peopleMergeRunBtn");
  const modeBtn = $("peopleMergeModeBtn");
  if (modeBtn) modeBtn.classList.toggle("active", peopleState.mergeMode);
  if (!bar) return;
  bar.classList.toggle("hidden", !peopleState.mergeMode);
  const count = peopleState.mergeSelected.size;
  if (countEl) countEl.textContent = String(count);
  if (runBtn) runBtn.disabled = count < 2;
}

function renderPeopleGrid(options = {}) {
  const body = $("peopleBody");
  const lede = $("peopleLede");
  const backBtn = $("peopleBackBtn");
  const titleEl = $("peopleTitle");
  const detailBar = $("peopleDetailBar");
  const pendingBanner = $("peoplePendingBanner");
  const filterRow = $("peopleFilterRow");
  if (!body) return;

  peopleState.currentPerson = null;
  if (backBtn) backBtn.classList.add("hidden");
  if (titleEl) titleEl.textContent = "人物与宠物";
  if (detailBar) detailBar.classList.add("hidden");
  if (pendingBanner) pendingBanner.classList.add("hidden");
  if (filterRow) filterRow.classList.remove("hidden");
  syncPeopleKindSwitch();

  const items = peopleState.items || [];
  const st = peopleState.status || {};
  const accelText = st.accelerator ? ` · 本地加速：${st.accelerator}` : "";
  if (lede) {
    lede.textContent = items.length
      ? `本地共归类 ${items.length} 位人物与宠物，所有数据均保留在本机${accelText}。`
      : `本地离线识别照片中的人物与宠物猫狗，数据不出本机${accelText}。`;
  }

  if (!items.length) {
    const ready = !!st.ready;
    const hint = ready
      ? "点右上角「扫描识别」，系统会在后台自动找出照片中的人物与猫狗并聚类。"
      : "尚未检测到本地推理依赖或模型文件，请前往「设置 → 人物与宠物」查看环境状态并一键下载本地模型。";
    body.innerHTML = `
      <div class="people-empty">
        <span class="people-empty-icon" data-icon="people"></span>
        <p>${ready ? "还没有归类的人物或宠物。" : "本地人物与宠物识别尚未就绪"}</p>
        <p class="people-empty-hint">${escapeHtml(hint)}</p>
        <div class="people-empty-actions">
          ${ready
            ? `<button type="button" class="btn primary" id="peopleEmptyScanBtn"><span class="btn-icon" data-icon="sparkles"></span>立即扫描识别</button>`
            : `<button type="button" class="btn primary" id="peopleEmptySettingsBtn"><span class="btn-icon" data-icon="settings"></span>前往人物设置</button>`}
        </div>
      </div>`;
    renderAllIcons(body);
    const scanBtn = $("peopleEmptyScanBtn");
    if (scanBtn) scanBtn.addEventListener("click", () => startPeopleScan(false));
    const setBtn = $("peopleEmptySettingsBtn");
    if (setBtn) {
      setBtn.addEventListener("click", () => {
        openSettings();
        if (typeof showSettingsTab === "function") setTimeout(() => showSettingsTab("people"), 80);
      });
    }
    return;
  }

  const animate = options.animate !== false;
  body.innerHTML = `<div class="people-grid">${items.map((p, idx) => peopleCardHtml(p, idx, { animate })).join("")}</div>`;
  renderAllIcons(body);
  updateMergeBar();
}

async function refreshPeopleGridSilent() {
  if (peopleState.silentRefreshing) return;
  peopleState.silentRefreshing = true;
  try {
    const params = new URLSearchParams();
    if (state.folder) params.set("folder", state.folder);
    if (peopleState.kind !== "all") params.set("kind", peopleState.kind);
    if (peopleState.includeHidden) params.set("include_hidden", "1");
    const data = await api(`/api/people?${params.toString()}`);
    peopleState.items = data.people || [];
    peopleState.status = data.status || null;
    if (data.job) {
      peopleState.lastClusterVersion = Math.max(
        peopleState.lastClusterVersion,
        Number(data.job.cluster_version) || 0
      );
      renderPeopleScanStatus(data.job);
    }
    if (state.view === "people" && !peopleState.currentPerson && !peopleState.mergeMode) {
      renderPeopleGrid({ animate: false });
    }
  } catch (e) {
    // Silent live refresh failure is harmless; next tick or completion will retry.
  } finally {
    peopleState.silentRefreshing = false;
  }
}

async function loadPeopleGrid() {
  const body = $("peopleBody");
  if (body) body.innerHTML = '<div class="people-empty">正在整理人物与宠物…</div>';
  try {
    const params = new URLSearchParams();
    if (state.folder) params.set("folder", state.folder);
    if (peopleState.kind !== "all") params.set("kind", peopleState.kind);
    if (peopleState.includeHidden) params.set("include_hidden", "1");
    const data = await api(`/api/people?${params.toString()}`);
    peopleState.items = data.people || [];
    peopleState.status = data.status || null;
    peopleState.lastClusterVersion = Number((data.job && data.job.cluster_version) || 0);
    renderPeopleScanStatus(data.job);
    if (data.job && data.job.status === "running") startPeoplePoll();
    renderPeopleGrid();
  } catch (e) {
    if (body) body.innerHTML = `<div class="people-empty">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

async function promptRenamePerson(personId, currentName = "") {
  const answer = await confirmDialog(
    "为这位人物或宠物起个名字；留空则恢复未命名。",
    {
      title: "修改名称",
      confirmText: "保存",
      input: {
        label: "名称",
        value: currentName || "",
        placeholder: "留空恢复未命名",
      },
    }
  );
  if (!answer || !answer.confirmed) return;
  const newName = String(answer.value || "").trim();
  if (newName === (currentName || "").trim()) return;
  try {
    const resp = await fetch(`/api/people/${personId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newName }),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.status === 409 && data.conflict) {
      const ok = await confirmDialog(
        `已存在名为「${newName}」的集合，是否把当前照片合并进去？\n合并后后续新照片会自动归入「${newName}」。`,
        { title: "合并同名人物", confirmText: "确认合并" }
      );
      if (!ok) return;
      const merged = await api(`/api/people/${personId}`, {
        method: "PATCH",
        body: JSON.stringify({ name: newName, merge_if_exists: true }),
      });
      showToast(`已合并到「${newName}」`, "success");
      if (peopleState.currentPerson) {
        openPersonDetail(merged.merged_into || personId);
      } else {
        loadPeopleGrid();
      }
      return;
    }
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    showToast(newName ? `已标注为「${newName}」，后续相似照片将自动归入` : "已清除名称", "success");
    if (peopleState.currentPerson) {
      openPersonDetail(personId);
    } else {
      loadPeopleGrid();
    }
  } catch (e) {
    showToast(`命名失败：${e.message}`, "error");
  }
}

function renderPendingFacesBanner(person, pendingFaces) {
  const banner = $("peoplePendingBanner");
  if (!banner) return;
  if (!pendingFaces || !pendingFaces.length) {
    banner.classList.add("hidden");
    banner.innerHTML = "";
    return;
  }
  const label = personDisplayName(person);
  banner.innerHTML = `
    <div class="people-pending-head">
      <span class="people-pending-title">${ICONS.sparkles} 发现 ${pendingFaces.length} 张疑似「${escapeHtml(label)}」的照片，确认后将越认越准</span>
    </div>
    <div class="people-pending-strip">
      ${pendingFaces
        .map(
          (f) => `
        <div class="pending-face-card" data-face-id="${f.face_id}">
          <button type="button" class="pending-face-thumb" data-open-photo="${f.photo_id}" title="查看原图">
            <img src="${escapeHtml(f.thumb_url)}" alt="" loading="lazy" decoding="async" />
          </button>
          <div class="pending-face-actions">
            <button type="button" class="btn small primary" data-face-confirm="${f.face_id}" title="确认是此人">是</button>
            <button type="button" class="btn small" data-face-reject="${f.face_id}" title="不是此人">否</button>
          </div>
        </div>`
        )
        .join("")}
    </div>
  `;
  banner.classList.remove("hidden");
}

function renderPersonDetail(payload) {
  peopleState.currentPerson = payload;
  const person = payload.person;
  const photos = payload.photos || [];
  const pendingPhotos = payload.pending_photos || [];
  const pending = payload.pending_faces || [];
  adoptPhotos(photos);
  adoptPhotos(pendingPhotos);

  const backBtn = $("peopleBackBtn");
  const titleEl = $("peopleTitle");
  const lede = $("peopleLede");
  const filterRow = $("peopleFilterRow");
  const detailBar = $("peopleDetailBar");
  const body = $("peopleBody");

  if (backBtn) backBtn.classList.remove("hidden");
  if (filterRow) filterRow.classList.add("hidden");
  if (titleEl) titleEl.textContent = personDisplayName(person);
  if (lede) {
    const kindText = kindBadgeLabel(person.kind);
    lede.textContent = `${photos.length} 张照片${kindText ? ` · ${kindText}` : ""}${pending.length ? ` · ${pending.length} 张待确认` : ""}`;
  }

  if (detailBar) {
    detailBar.innerHTML = `
      <button type="button" class="btn small primary" id="personDetailRenameBtn">${ICONS.edit}<span>${person.name ? "修改姓名" : "标注姓名"}</span></button>
      <button type="button" class="btn small${person.is_favorite ? " active" : ""}" id="personDetailFavBtn">${person.is_favorite ? ICONS.heartFilled : ICONS.heart}<span>${person.is_favorite ? "已置顶" : "置顶"}</span></button>
      <button type="button" class="btn small" id="personDetailCoverBtn">${ICONS.image}<span>更换封面</span></button>
      <button type="button" class="btn small" id="personDetailHideBtn">${person.is_hidden ? ICONS.eye : ICONS.eyeOff}<span>${person.is_hidden ? "取消隐藏" : "隐藏"}</span></button>
    `;
    detailBar.classList.remove("hidden");

    $("personDetailRenameBtn").addEventListener("click", () =>
      promptRenamePerson(person.id, person.name)
    );
    $("personDetailFavBtn").addEventListener("click", async () => {
      try {
        await api(`/api/people/${person.id}`, {
          method: "PATCH",
          body: JSON.stringify({ is_favorite: !person.is_favorite }),
        });
        openPersonDetail(person.id);
      } catch (e) {
        showToast(e.message, "error");
      }
    });
    $("personDetailHideBtn").addEventListener("click", async () => {
      try {
        await api(`/api/people/${person.id}`, {
          method: "PATCH",
          body: JSON.stringify({ is_hidden: !person.is_hidden }),
        });
        showToast(person.is_hidden ? "已恢复显示" : "已隐藏该人物（不删除照片）", "info");
        if (!person.is_hidden) loadPeopleGrid();
        else openPersonDetail(person.id);
      } catch (e) {
        showToast(e.message, "error");
      }
    });
    $("personDetailCoverBtn").addEventListener("click", () => {
      openPersonCoverPicker(payload);
    });
  }

  renderPendingFacesBanner(person, pending);

  if (!body) return;
  if (!photos.length) {
    body.innerHTML = '<div class="people-empty"><p>当前目录下暂无已确认的照片。</p></div>';
    return;
  }
  body.innerHTML = `<div class="footprint-photos people-photos">${photos
    .map((p) => memoryPolaroidHtml(p, { deactivate: true }))
    .join("")}</div>`;
  watchPolaroids(body);
  window.scrollTo({ top: 0, left: 0, behavior: "instant" });
}

function openPersonCoverPicker(payload) {
  const candidates = payload.cover_candidates || [];
  if (!candidates.length) {
    showToast("没有可选的封面头像", "info");
    return;
  }
  const banner = $("peoplePendingBanner");
  if (!banner) return;
  banner.innerHTML = `
    <div class="people-pending-head">
      <span class="people-pending-title">${ICONS.image} 点击下方任意一张作为「${escapeHtml(personDisplayName(payload.person))}」的封面</span>
      <button type="button" class="btn small" id="closeCoverPickerBtn">完成</button>
    </div>
    <div class="people-cover-grid">
      ${candidates
        .map(
          (c) => `
        <button type="button" class="cover-pick-btn${c.face_id === payload.person.cover_face_id ? " active" : ""}" data-pick-cover="${c.face_id}">
          <img src="${escapeHtml(c.thumb_url)}" alt="" loading="lazy" />
        </button>`
        )
        .join("")}
    </div>
  `;
  banner.classList.remove("hidden");
  const closeBtn = $("closeCoverPickerBtn");
  if (closeBtn) {
    closeBtn.addEventListener("click", () =>
      renderPendingFacesBanner(payload.person, payload.pending_faces || [])
    );
  }
}

async function openPersonDetail(personId) {
  const body = $("peopleBody");
  if (body) body.innerHTML = '<div class="people-empty">正在翻找相关回忆…</div>';
  try {
    const params = new URLSearchParams();
    if (state.folder) params.set("folder", state.folder);
    const payload = await api(`/api/people/${personId}?${params.toString()}`);
    renderPersonDetail(payload);
  } catch (e) {
    showToast(`加载人物失败：${e.message}`, "error");
    loadPeopleGrid();
  }
}

function syncPeopleKindSwitch() {
  const sw = $("peopleKindSwitch");
  if (!sw) return;
  sw.querySelectorAll("[data-people-kind]").forEach((btn) => {
    const active = (btn.dataset.peopleKind || "all") === peopleState.kind;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  const ink = $("peopleKindInk");
  const active = sw.querySelector(".people-kind-btn.is-active");
  if (!ink || !active) return;
  const width = active.offsetWidth;
  if (!width) {
    ink.style.opacity = "0";
    if (!sw._inkRetry) {
      sw._inkRetry = setTimeout(() => {
        sw._inkRetry = null;
        syncPeopleKindSwitch();
      }, 220);
    }
    return;
  }
  if (sw._inkRetry) {
    clearTimeout(sw._inkRetry);
    sw._inkRetry = null;
  }
  ink.style.opacity = "1";
  ink.style.width = `${width}px`;
  ink.style.transform = `translateX(${active.offsetLeft}px)`;
}

async function openPeople(personId = null) {
  showView("people");
  syncPeopleKindSwitch();
  requestAnimationFrame(syncPeopleKindSwitch);
  setTimeout(syncPeopleKindSwitch, 320);
  peopleState.mergeMode = false;
  peopleState.mergeSelected.clear();
  updateMergeBar();
  if (personId) {
    await openPersonDetail(personId);
  } else {
    await loadPeopleGrid();
  }
}

function initPeople() {
  const backBtn = $("peopleBackBtn");
  if (backBtn) backBtn.addEventListener("click", () => loadPeopleGrid());

  const scanBtn = $("peopleScanBtn");
  if (scanBtn) scanBtn.addEventListener("click", () => startPeopleScan(false));

  const cancelScanBtn = $("peopleCancelScanBtn");
  if (cancelScanBtn) {
    cancelScanBtn.addEventListener("click", async () => {
      try {
        await api("/api/people/scan/cancel", { method: "POST" });
        showToast("正在取消人物识别…", "info");
      } catch (e) {}
    });
  }

  const mergeModeBtn = $("peopleMergeModeBtn");
  if (mergeModeBtn) {
    mergeModeBtn.addEventListener("click", () => {
      peopleState.mergeMode = !peopleState.mergeMode;
      peopleState.mergeSelected.clear();
      renderPeopleGrid();
    });
  }

  const showHiddenBtn = $("peopleShowHiddenBtn");
  if (showHiddenBtn) {
    showHiddenBtn.addEventListener("click", () => {
      peopleState.includeHidden = !peopleState.includeHidden;
      showHiddenBtn.classList.toggle("active", peopleState.includeHidden);
      loadPeopleGrid();
    });
  }

  const filterRow = $("peopleFilterRow");
  if (filterRow) {
    filterRow.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-people-kind]");
      if (!btn) return;
      peopleState.kind = btn.dataset.peopleKind || "all";
      syncPeopleKindSwitch();
      loadPeopleGrid();
    });
  }
  window.addEventListener("resize", syncPeopleKindSwitch);

  const mergeCancelBtn = $("peopleMergeCancelBtn");
  if (mergeCancelBtn) {
    mergeCancelBtn.addEventListener("click", () => {
      peopleState.mergeMode = false;
      peopleState.mergeSelected.clear();
      renderPeopleGrid();
    });
  }

  const mergeRunBtn = $("peopleMergeRunBtn");
  if (mergeRunBtn) {
    mergeRunBtn.addEventListener("click", async () => {
      const ids = [...peopleState.mergeSelected];
      if (ids.length < 2) return;
      const chosen = ids
        .map((id) => peopleState.items.find((p) => p.id === id))
        .filter(Boolean);
      const target =
        chosen.find((p) => (p.name || "").trim()) ||
        chosen.slice().sort((a, b) => b.count - a.count)[0];
      if (!target) return;
      const ok = await confirmDialog(
        `确定将选中的 ${ids.length} 位合并为「${personDisplayName(target)}」吗？`,
        { title: "合并人物", confirmText: "确认合并" }
      );
      if (!ok) return;
      try {
        await api("/api/people/merge", {
          method: "POST",
          body: JSON.stringify({
            source_ids: ids.filter((id) => id !== target.id),
            target_id: target.id,
          }),
        });
        showToast(`已合并为「${personDisplayName(target)}」`, "success");
        peopleState.mergeMode = false;
        peopleState.mergeSelected.clear();
        loadPeopleGrid();
      } catch (e) {
        showToast(`合并失败：${e.message}`, "error");
      }
    });
  }

  const pendingBanner = $("peoplePendingBanner");
  if (pendingBanner) {
    pendingBanner.addEventListener("click", async (e) => {
      const confirmBtn = e.target.closest("[data-face-confirm]");
      if (confirmBtn) {
        const fid = Number(confirmBtn.dataset.faceConfirm);
        try {
          await api(`/api/faces/${fid}`, {
            method: "PATCH",
            body: JSON.stringify({ action: "confirm" }),
          });
          showToast("已确认归入该人物", "success");
          if (peopleState.currentPerson) openPersonDetail(peopleState.currentPerson.person.id);
        } catch (err) {
          showToast(err.message, "error");
        }
        return;
      }
      const rejectBtn = e.target.closest("[data-face-reject]");
      if (rejectBtn) {
        const fid = Number(rejectBtn.dataset.faceReject);
        try {
          await api(`/api/faces/${fid}`, {
            method: "PATCH",
            body: JSON.stringify({ action: "reject" }),
          });
          if (peopleState.currentPerson) openPersonDetail(peopleState.currentPerson.person.id);
        } catch (err) {
          showToast(err.message, "error");
        }
        return;
      }
      const coverBtn = e.target.closest("[data-pick-cover]");
      if (coverBtn && peopleState.currentPerson) {
        const fid = Number(coverBtn.dataset.pickCover);
        const pid = peopleState.currentPerson.person.id;
        try {
          await api(`/api/people/${pid}`, {
            method: "PATCH",
            body: JSON.stringify({ cover_face_id: fid }),
          });
          showToast("已更新封面头像", "success");
          openPersonDetail(pid);
        } catch (err) {
          showToast(err.message, "error");
        }
        return;
      }
      const openPhoto = e.target.closest("[data-open-photo]");
      if (openPhoto) {
        const photoId = Number(openPhoto.dataset.openPhoto);
        if (Number.isFinite(photoId)) {
          const cur = peopleState.currentPerson;
          const pendingIds = cur
            ? [...new Set((cur.pending_faces || []).map((f) => Number(f.photo_id)).filter(Number.isFinite))]
            : [];
          if (pendingIds.length && cur) {
            openPreview(photoId, {
              ids: pendingIds,
              title: `${personDisplayName(cur.person)} · 待确认`,
            });
          } else {
            openPreview(photoId);
          }
        }
      }
    });
  }

  const body = $("peopleBody");
  if (body) {
    body.addEventListener("click", (e) => {
      // Photo card inside person detail
      const polaroid = e.target.closest(".polaroid-card");
      if (polaroid && peopleState.currentPerson) {
        if (e.target.closest("button:not(.polaroid-card)")) return;
        const pid = Number(polaroid.dataset.id);
        if (!Number.isFinite(pid)) return;
        const photos = peopleState.currentPerson.photos || [];
        openPreview(pid, {
          ids: photos.map((p) => p.id),
          title: personDisplayName(peopleState.currentPerson.person),
        });
        return;
      }

      // Rename click
      const renameBtn = e.target.closest("[data-rename-person]");
      if (renameBtn && !peopleState.mergeMode) {
        const id = Number(renameBtn.dataset.renamePerson);
        const person = peopleState.items.find((p) => p.id === id);
        if (person) promptRenamePerson(id, person.name);
        return;
      }

      // Avatar or card click
      const card = e.target.closest(".person-card");
      if (!card) return;
      const id = Number(card.dataset.personId);
      if (!Number.isFinite(id)) return;
      if (peopleState.mergeMode) {
        if (peopleState.mergeSelected.has(id)) peopleState.mergeSelected.delete(id);
        else peopleState.mergeSelected.add(id);
        card.classList.toggle("is-merge-selected", peopleState.mergeSelected.has(id));
        const chk = card.querySelector(".person-merge-check");
        if (chk) chk.innerHTML = peopleState.mergeSelected.has(id) ? ICONS.check : "";
        updateMergeBar();
        return;
      }
      openPersonDetail(id);
    });
  }
}
