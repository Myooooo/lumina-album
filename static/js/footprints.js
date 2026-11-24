/* ==========================================================================
   Lumina Album - Footprints & Custom Places (足迹与自定义地点)
   Combines auto-detected EXIF cities with user-created places, supports
   forward-geocoding coordinate lookup, adding/removing photos (updating DB
   location only, never modifying file EXIF), and an iOS-style Map mode.
   ========================================================================== */
"use strict";

const fpState = {
  mode: "grid", // "grid" | "map"
  modeInitialized: false,
  bounds: null,
  editingPlaceId: null,
  pendingAssignPhotoIds: null, // used when creating a place from gallery multi-select
  removeMode: false,
  removeSelected: new Set(),
  pickerPlaceId: null,
  pickerPhotos: [],
  pickerSelected: new Set(),
};

function placeDisplayName(place) {
  if (!place) return "";
  return (place.name || place.city || "未命名地点").trim();
}

function footprintCardHtml(place, index) {
  const years = place.years || [];
  let span = "";
  if (years.length === 1) span = `${years[0]}`;
  else if (years.length > 1) span = `${years[0]} – ${years[years.length - 1]}`;
  const cover = place.cover || {};
  const label = placeDisplayName(place);
  const img = cover.id
    ? `<img class="footprint-img" src="/api/thumbnail/${cover.id}?v=${encodeURIComponent(cover.thumb_version || "")}"
            alt="${escapeHtml(label)}" loading="lazy" decoding="async" />`
    : '<span class="footprint-noimg"></span>';
  const postmarkYear = years.length ? String(years[years.length - 1]) : (place.source === "manual" ? "自建" : "LUMINA");
  const isManual = place.source === "manual";
  return `
    <button class="footprint-card${isManual ? " is-manual-place" : ""}" type="button"
            data-city="${escapeHtml(place.city || label)}"
            data-index="${index}"
            style="animation-delay:${Math.min(index, 14) * 0.04}s">
      <span class="footprint-shot">
        ${img}
        ${isManual ? '<span class="footprint-source-badge">自建</span>' : ""}
        <span class="footprint-count">${place.count}</span>
      </span>
      <span class="footprint-body">
        <span class="footprint-city">${escapeHtml(label)}</span>
        <span class="footprint-meta">${place.count} 张${span ? ` · ${span}` : ""}${place.lat != null && place.lon != null ? "" : " · 未定位"}</span>
        <span class="footprint-postmark" aria-hidden="true">${escapeHtml(postmarkYear)}</span>
      </span>
    </button>`;
}

function footprintPhotoHtml(photo) {
  const card = memoryPolaroidHtml(photo, { deactivate: true });
  if (!fpState.removeMode) return card;
  const checked = fpState.removeSelected.has(photo.id);
  return card.replace(
    'class="polaroid-card',
    `class="polaroid-card is-place-removable${checked ? " is-remove-selected" : ""}`
  );
}

function syncFootprintsModeButtons() {
  const sw = $("footprintsModeSwitch");
  if (!sw) return;
  sw.querySelectorAll("[data-fp-mode]").forEach((btn) => {
    const active = btn.dataset.fpMode === fpState.mode;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });
  const ink = $("footprintsModeInk");
  const active = sw.querySelector(".footprints-mode-btn.active");
  if (!ink || !active) return;
  const base = sw.getBoundingClientRect();
  const rect = active.getBoundingClientRect();
  if (!rect.width) {
    ink.style.opacity = "0";
    return;
  }
  ink.style.opacity = "1";
  ink.style.width = `${rect.width}px`;
  ink.style.transform = `translateX(${rect.left - base.left - 3}px)`;
}

function setFootprintsMode(mode) {
  const next = mode === "map" ? "map" : "grid";
  fpState.mode = next;
  fpState.modeInitialized = true;
  syncFootprintsModeButtons();
  const mapWrap = $("footprintsMapWrap");
  const body = $("footprintsBody");
  if (state.footprintPlace) {
    // Inside a specific place, always show its photo grid.
    if (mapWrap) mapWrap.classList.add("hidden");
    if (body) body.classList.remove("hidden");
    return;
  }
  if (next === "map") {
    if (body) body.classList.add("hidden");
    if (mapWrap) mapWrap.classList.remove("hidden");
    if (typeof renderFootprintsMap === "function") {
      renderFootprintsMap(state.footprintPlaces || [], fpState.bounds);
    } else if (mapWrap) {
      const canvas = $("footprintsMapCanvas");
      if (canvas) {
        canvas.innerHTML = '<div class="footprints-empty"><p>地图模式准备中…</p></div>';
      }
    }
  } else {
    if (mapWrap) mapWrap.classList.add("hidden");
    if (body) body.classList.remove("hidden");
    renderFootprintsGrid();
  }
}

function renderFootprintsGrid() {
  const body = $("footprintsBody");
  if (!body) return;
  const places = state.footprintPlaces || [];
  if (!places.length) {
    body.innerHTML = `
      <div class="footprints-empty">
        <span class="footprints-empty-icon" data-icon="pin"></span>
        <p>还没有记录的足迹。</p>
        <p class="footprints-empty-hint">照片里带着 GPS 信息，或者点右上角「新建地点」手动创建地点并添加照片，这里就会亮起来。</p>
      </div>`;
    renderAllIcons(body);
    return;
  }
  body.innerHTML = `<div class="footprint-grid">${places
    .map(footprintCardHtml)
    .join("")}</div>`;
  body.scrollTop = 0;
}

function renderFootprints(data) {
  state.footprintPlaces = data.places || [];
  fpState.bounds = data.bounds || null;
  if (fpState.mode === "map" && !state.footprintPlace) {
    setFootprintsMode("map");
  } else {
    renderFootprintsGrid();
  }
}

let footprintSubTimer = null;

function renderPlaceDetailBar(place) {
  const bar = $("footprintsDetailBar");
  if (!bar) return;
  if (!place) {
    bar.classList.add("hidden");
    bar.innerHTML = "";
    return;
  }
  const isManual = place.source === "manual" && place.id != null;
  const coordsText =
    place.lat != null && place.lon != null
      ? `<span class="place-coords-pill" title="WGS-84 坐标">${Number(place.lat).toFixed(4)}, ${Number(place.lon).toFixed(4)}</span>`
      : `<span class="place-coords-pill is-missing">暂无坐标</span>`;

  if (isManual) {
    const rmCount = fpState.removeSelected.size;
    bar.innerHTML = `
      <button type="button" class="btn small primary" id="placeAddPhotosBtn">${ICONS.image}<span>添加照片</span></button>
      <button type="button" class="btn small${fpState.removeMode ? " active" : ""}" id="placeRemoveModeBtn">${ICONS.trash}<span>${fpState.removeMode ? "退出移出" : "移出照片"}</span></button>
      ${fpState.removeMode ? `<button type="button" class="btn small danger" id="placeConfirmRemoveBtn" ${rmCount ? "" : "disabled"}>确认移出 (${rmCount})</button>` : ""}
      <button type="button" class="btn small" id="placeEditBtn">${ICONS.edit}<span>编辑地点</span></button>
      <button type="button" class="btn small danger" id="placeDeleteBtn">${ICONS.trash}<span>删除地点</span></button>
      ${coordsText}
    `;
  } else {
    bar.innerHTML = `
      <span class="place-auto-hint">由照片 EXIF GPS 自动聚合</span>
      ${coordsText}
    `;
  }
  bar.classList.remove("hidden");

  const addBtn = $("placeAddPhotosBtn");
  if (addBtn) addBtn.addEventListener("click", () => openPlacePhotoPicker(place));
  const rmToggle = $("placeRemoveModeBtn");
  if (rmToggle) {
    rmToggle.addEventListener("click", () => {
      fpState.removeMode = !fpState.removeMode;
      fpState.removeSelected.clear();
      renderPlaceDetailBar(place);
      const body = $("footprintsBody");
      if (body && (place.photos || []).length) {
        state.footprintShown = 0;
        body.innerHTML = '<div class="footprint-photos"></div>';
        appendPlacePhotos();
      }
    });
  }
  const rmConfirm = $("placeConfirmRemoveBtn");
  if (rmConfirm) {
    rmConfirm.addEventListener("click", () => confirmRemovePlacePhotos(place));
  }
  const editBtn = $("placeEditBtn");
  if (editBtn) editBtn.addEventListener("click", () => openPlaceEditModal(place));
  const delBtn = $("placeDeleteBtn");
  if (delBtn) delBtn.addEventListener("click", () => deleteCustomPlace(place));
}

function openPlace(index) {
  const place = (state.footprintPlaces || [])[index];
  if (!place) return;
  state.footprintPlace = place;
  fpState.removeMode = false;
  fpState.removeSelected.clear();

  const photos = place.photos || [];
  adoptPhotos(photos);

  const body = $("footprintsBody");
  const mapWrap = $("footprintsMapWrap");
  const title = $("footprintsTitle");
  const lede = $("footprintsLede");
  const back = $("footprintsBackBtn");
  const headerActions = $("footprintsHeaderActions");

  const renderPlaceView = () => {
    if (mapWrap) mapWrap.classList.add("hidden");
    if (body) body.classList.remove("hidden");
    if (headerActions) headerActions.classList.add("hidden");
    if (title) title.textContent = placeDisplayName(place);
    if (lede) {
      const years = place.years || [];
      const span =
        years.length > 1
          ? ` · ${years[0]} – ${years[years.length - 1]}`
          : years.length === 1
            ? ` · ${years[0]}`
            : "";
      const addrPart = place.address && place.address !== placeDisplayName(place) ? ` · ${place.address}` : "";
      lede.textContent = `${place.count} 张回忆${span}${addrPart}`;
    }
    if (back) back.classList.remove("hidden");
    renderPlaceDetailBar(place);
    if (!body) return;
    body.classList.remove("is-transitioning-out");
    if (!photos.length) {
      const emptyHint =
        place.source === "manual"
          ? `<div class="footprints-empty">
               <p>这个地点还没有照片。</p>
               <p class="footprints-empty-hint">点上方「添加照片」挑选回忆，仅覆盖数据库地点字段，不会修改原图 EXIF。</p>
               <button type="button" class="btn primary" id="placeEmptyAddBtn">${ICONS.image}<span>立即添加照片</span></button>
             </div>`
          : '<div class="footprints-empty"><p>这个地方的照片暂时打不开。</p></div>';
      body.innerHTML = emptyHint;
      const emptyAdd = $("placeEmptyAddBtn");
      if (emptyAdd) emptyAdd.addEventListener("click", () => openPlacePhotoPicker(place));
      return;
    }
    state.footprintShown = 0;
    body.innerHTML = '<div class="footprint-photos"></div>';
    appendPlacePhotos();
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  };

  if (footprintSubTimer) {
    clearTimeout(footprintSubTimer);
    footprintSubTimer = null;
  }
  const reduced =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!body || reduced) {
    renderPlaceView();
    return;
  }
  body.classList.add("is-transitioning-out");
  footprintSubTimer = setTimeout(() => {
    footprintSubTimer = null;
    renderPlaceView();
  }, 140);
}

const PLACE_PAGE_SIZE = 60;

function appendPlacePhotos() {
  const list = document.querySelector("#footprintsBody .footprint-photos");
  const place = state.footprintPlace;
  if (!list || !place) return;
  const photos = place.photos || [];
  const from = state.footprintShown || 0;
  const to = Math.min(from + PLACE_PAGE_SIZE, photos.length);
  if (from >= to) return;
  const html = photos.slice(from, to).map(footprintPhotoHtml).join("");
  list.insertAdjacentHTML("beforeend", html);
  watchPolaroids(list);
  state.footprintShown = to;
  const body = $("footprintsBody");
  if (body && body.scrollHeight <= body.clientHeight + 40 && to < photos.length) {
    appendPlacePhotos();
  }
}

function closePlace() {
  state.footprintPlace = null;
  state.footprintShown = 0;
  fpState.removeMode = false;
  fpState.removeSelected.clear();
  const title = $("footprintsTitle");
  const lede = $("footprintsLede");
  const back = $("footprintsBackBtn");
  const body = $("footprintsBody");
  const headerActions = $("footprintsHeaderActions");

  const renderListView = () => {
    if (title) title.textContent = "足迹";
    if (back) back.classList.add("hidden");
    if (headerActions) headerActions.classList.remove("hidden");
    renderPlaceDetailBar(null);
    const total = (state.footprintPlaces || []).length;
    if (lede) {
      lede.textContent = total
        ? `这些回忆散落在 ${total} 个地方，共 ${state.footprintPlaces.reduce(
            (sum, place) => sum + place.count,
            0
          )} 张。`
        : "还没有记录的足迹。";
    }
    if (body) body.classList.remove("is-transitioning-out");
    setFootprintsMode(fpState.mode);
    window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  };

  if (footprintSubTimer) {
    clearTimeout(footprintSubTimer);
    footprintSubTimer = null;
  }
  const reduced =
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!body || reduced) {
    renderListView();
    return;
  }
  body.classList.add("is-transitioning-out");
  footprintSubTimer = setTimeout(() => {
    footprintSubTimer = null;
    renderListView();
  }, 140);
}

async function openFootprints(options = {}) {
  showView("footprints");
  if (!fpState.modeInitialized) {
    fpState.mode = state.footprintDefaultView === "map" ? "map" : "grid";
    fpState.modeInitialized = true;
  }
  syncFootprintsModeButtons();
  requestAnimationFrame(syncFootprintsModeButtons);

  const body = $("footprintsBody");
  const lede = $("footprintsLede");
  const title = $("footprintsTitle");
  const back = $("footprintsBackBtn");
  const headerActions = $("footprintsHeaderActions");
  const keepPlaceKey = options.reopenPlaceKey || null;

  state.footprintPlace = null;
  fpState.removeMode = false;
  fpState.removeSelected.clear();
  renderPlaceDetailBar(null);
  if (title) title.textContent = "足迹";
  if (back) back.classList.add("hidden");
  if (headerActions) headerActions.classList.remove("hidden");
  if (body && fpState.mode === "grid") {
    body.innerHTML = '<div class="footprints-empty">正在整理走过的地方…</div>';
  }
  if (lede) lede.textContent = "正在整理走过的地方…";
  try {
    const params = new URLSearchParams();
    if (state.folder) params.set("folder", state.folder);
    const data = await api(`/api/footprints?${params.toString()}`);
    if (lede) {
      lede.textContent = data.places.length
        ? `这些回忆散落在 ${data.places.length} 个地方，共 ${data.total} 张。`
        : "还没有记录的足迹。";
    }
    renderFootprints(data);
    if (keepPlaceKey) {
      const idx = (state.footprintPlaces || []).findIndex((p) => p.key === keepPlaceKey);
      if (idx >= 0) openPlace(idx);
    }
  } catch (e) {
    if (body) {
      body.innerHTML = `<div class="footprints-empty">整理失败：${escapeHtml(e.message)}</div>`;
    }
    if (lede) lede.textContent = "";
  }
}

/* --------------------------------------------------------------------------
   Create / Edit Custom Place Modal + Forward Geocoding
   -------------------------------------------------------------------------- */

function openPlaceEditModal(place = null, options = {}) {
  fpState.editingPlaceId = place && place.id != null ? place.id : null;
  fpState.pendingAssignPhotoIds = Array.isArray(options.assignPhotoIds) ? options.assignPhotoIds : null;

  const titleEl = $("placeEditTitle");
  if (titleEl) {
    titleEl.innerHTML = `<span class="modal-title-icon">${ICONS.pin}</span>${fpState.editingPlaceId ? "编辑地点" : "新建地点"}`;
  }
  if ($("placeFormName")) $("placeFormName").value = place ? placeDisplayName(place) : "";
  if ($("placeFormAddress")) $("placeFormAddress").value = place ? (place.address || "") : "";
  if ($("placeFormLat")) $("placeFormLat").value = place && place.lat != null ? String(place.lat) : "";
  if ($("placeFormLon")) $("placeFormLon").value = place && place.lon != null ? String(place.lon) : "";
  const statusEl = $("placeGeocodeStatus");
  const resultsEl = $("placeGeocodeResults");
  if (statusEl) {
    statusEl.classList.add("hidden");
    statusEl.textContent = "";
  }
  if (resultsEl) {
    resultsEl.classList.add("hidden");
    resultsEl.innerHTML = "";
  }
  showModal($("placeEditModal"));
  pushModal($("placeEditModal"));
  setTimeout(() => {
    if ($("placeFormName")) $("placeFormName").focus();
  }, 40);
}

async function lookupPlaceCoordinates() {
  const query = ($("placeFormName") ? $("placeFormName").value : "").trim();
  if (!query) {
    showToast("请先输入地点名称", "warning");
    return;
  }
  const btn = $("placeGeocodeBtn");
  const statusEl = $("placeGeocodeStatus");
  const resultsEl = $("placeGeocodeResults");
  if (btn) btn.disabled = true;
  if (statusEl) {
    statusEl.textContent = "正在调用当前配置的地理编码接口反查坐标…";
    statusEl.classList.remove("hidden");
  }
  if (resultsEl) {
    resultsEl.classList.add("hidden");
    resultsEl.innerHTML = "";
  }
  try {
    const data = await api("/api/places/geocode", {
      method: "POST",
      body: JSON.stringify({ query }),
    });
    const cands = data.candidates || [];
    if (!cands.length) {
      if (statusEl) {
        statusEl.textContent = data.message || "未查到匹配坐标，可换更完整的地名重试，或手工填写经纬度。";
      }
      return;
    }
    if (statusEl) {
      statusEl.textContent = `找到 ${cands.length} 个候选位置（点选即可填入坐标）：`;
    }
    if (resultsEl) {
      resultsEl.innerHTML = cands
        .map(
          (c, i) => `
          <button type="button" class="place-geocode-item" data-cand-idx="${i}">
            <span class="place-geocode-name">${escapeHtml(c.address || c.name)}</span>
            <span class="place-geocode-coords">${Number(c.lat).toFixed(5)}, ${Number(c.lon).toFixed(5)}</span>
          </button>`
        )
        .join("");
      resultsEl.classList.remove("hidden");
      resultsEl.querySelectorAll("[data-cand-idx]").forEach((item) => {
        item.addEventListener("click", () => {
          const picked = cands[Number(item.dataset.candIdx)];
          if (!picked) return;
          if ($("placeFormAddress")) $("placeFormAddress").value = picked.address || picked.name || query;
          if ($("placeFormLat")) $("placeFormLat").value = String(picked.lat);
          if ($("placeFormLon")) $("placeFormLon").value = String(picked.lon);
          resultsEl.querySelectorAll(".place-geocode-item").forEach((el) => el.classList.remove("active"));
          item.classList.add("active");
          showToast("已填入反查到的坐标与地址", "success");
        });
      });
      // Auto-select the top result if coordinate fields are currently empty.
      if (!$("placeFormLat").value && !$("placeFormLon").value) {
        const first = cands[0];
        if ($("placeFormAddress") && !$("placeFormAddress").value.trim()) {
          $("placeFormAddress").value = first.address || first.name || query;
        }
        $("placeFormLat").value = String(first.lat);
        $("placeFormLon").value = String(first.lon);
        const firstBtn = resultsEl.querySelector('[data-cand-idx="0"]');
        if (firstBtn) firstBtn.classList.add("active");
      }
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = `反查失败：${e.message}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function savePlaceForm() {
  const name = ($("placeFormName") ? $("placeFormName").value : "").trim();
  if (!name) {
    showToast("请输入地点名称", "warning");
    return;
  }
  const address = ($("placeFormAddress") ? $("placeFormAddress").value : "").trim();
  const rawLat = ($("placeFormLat") ? $("placeFormLat").value : "").trim();
  const rawLon = ($("placeFormLon") ? $("placeFormLon").value : "").trim();
  const payload = {
    name,
    address: address || name,
    lat: rawLat !== "" ? Number(rawLat) : null,
    lon: rawLon !== "" ? Number(rawLon) : null,
  };
  const btn = $("placeFormSaveBtn");
  if (btn) btn.disabled = true;
  try {
    if (fpState.editingPlaceId != null) {
      await api(`/api/places/${fpState.editingPlaceId}`, {
        method: "PATCH",
        body: JSON.stringify(payload),
      });
      closeModal("placeEditModal");
      showToast(`已更新地点「${name}」`, "success");
      const key = `place:${fpState.editingPlaceId}`;
      await openFootprints({ reopenPlaceKey: state.footprintPlace ? key : null });
    } else {
      if (fpState.pendingAssignPhotoIds && fpState.pendingAssignPhotoIds.length) {
        payload.photo_ids = fpState.pendingAssignPhotoIds;
      }
      const data = await api("/api/places", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      closeModal("placeEditModal");
      const geoNote = data.geocoded ? "（已自动反查坐标）" : "";
      const assignNote = data.assigned ? `，并归入 ${data.assigned} 张照片` : "";
      showToast(`已创建地点「${name}」${geoNote}${assignNote}`, "success");
      if (fpState.pendingAssignPhotoIds) {
        fpState.pendingAssignPhotoIds = null;
        state.selected.clear();
        updateSelectionBar();
        if (state.view === "album") loadPhotos();
      }
      if (state.view === "footprints") {
        await openFootprints();
      }
    }
  } catch (e) {
    showToast(`保存失败：${e.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function deleteCustomPlace(place) {
  if (!place || place.id == null) return;
  const label = placeDisplayName(place);
  const ok = await confirmDialog(
    `确定删除自定义地点「${label}」吗？\n不会删除任何照片原图，只会解除照片与该地点的绑定。`,
    { title: "删除地点", confirmText: "删除地点", danger: true }
  );
  if (!ok) return;
  try {
    await api(`/api/places/${place.id}`, { method: "DELETE" });
    showToast(`已删除地点「${label}」`, "info");
    await openFootprints();
  } catch (e) {
    showToast(`删除失败：${e.message}`, "error");
  }
}

async function confirmRemovePlacePhotos(place) {
  if (!place || place.id == null) return;
  const ids = [...fpState.removeSelected];
  if (!ids.length) return;
  const ok = await confirmDialog(
    `确定将选中的 ${ids.length} 张照片移出「${placeDisplayName(place)}」吗？\n仅清除数据库中的手动地点记录，原图与 EXIF 均不受影响。`,
    { title: "移出地点", confirmText: "确认移出", danger: true }
  );
  if (!ok) return;
  try {
    await api(`/api/places/${place.id}/photos`, {
      method: "DELETE",
      body: JSON.stringify({ photo_ids: ids }),
    });
    showToast(`已将 ${ids.length} 张照片移出该地点`, "success");
    fpState.removeMode = false;
    fpState.removeSelected.clear();
    await openFootprints({ reopenPlaceKey: place.key });
  } catch (e) {
    showToast(`移出失败：${e.message}`, "error");
  }
}

/* --------------------------------------------------------------------------
   Add Photos to Place Picker Modal
   -------------------------------------------------------------------------- */

function updatePlacePickerCount() {
  const countEl = $("placePickerCount");
  const confirmBtn = $("placePickerConfirmBtn");
  const n = fpState.pickerSelected.size;
  if (countEl) {
    countEl.textContent = `已选择 ${n} 张（仅覆盖数据库地点字段，不改原图 EXIF）`;
  }
  if (confirmBtn) confirmBtn.disabled = n === 0;
}

function renderPlacePickerGrid() {
  const grid = $("placePickerGrid");
  if (!grid) return;
  const unlocatedOnly = $("placePickerUnlocatedOnly") ? $("placePickerUnlocatedOnly").checked : false;
  const existingIds = new Set(
    ((state.footprintPlace && state.footprintPlace.photos) || []).map((p) => p.id)
  );
  const filtered = (fpState.pickerPhotos || []).filter((p) => {
    if (existingIds.has(p.id)) return false;
    if (unlocatedOnly && p.location && p.location.trim()) return false;
    return true;
  });
  if (!filtered.length) {
    grid.innerHTML = '<div class="footprints-empty"><p>没有符合条件的可添加照片。</p></div>';
    updatePlacePickerCount();
    return;
  }
  grid.innerHTML = filtered
    .map((p) => {
      const checked = fpState.pickerSelected.has(p.id);
      const locBadge = p.location ? `<span class="picker-loc-tag" title="${escapeHtml(p.location)}">${escapeHtml(p.location)}</span>` : "";
      return `
        <button type="button" class="place-picker-card${checked ? " is-selected" : ""}" data-pick-photo="${p.id}">
          <span class="place-picker-check">${checked ? ICONS.check : ""}</span>
          <img src="${thumbnailUrl(p)}" alt="${escapeHtml(p.filename || "")}" loading="lazy" decoding="async" />
          <span class="place-picker-meta">
            <span class="place-picker-name" title="${escapeHtml(p.filename || "")}">${escapeHtml(p.title || p.filename || "")}</span>
            ${locBadge}
          </span>
        </button>`;
    })
    .join("");
  updatePlacePickerCount();
}

async function loadPlacePickerPhotos() {
  const grid = $("placePickerGrid");
  if (grid) grid.innerHTML = '<div class="footprints-empty">正在加载照片…</div>';
  try {
    const params = new URLSearchParams();
    if (state.folder) params.set("folder", state.folder);
    params.set("status", "all");
    params.set("sort", "time_desc");
    params.set("limit", "400");
    const q = ($("placePickerSearch") ? $("placePickerSearch").value : "").trim();
    if (q) params.set("search", q);
    const data = await api(`/api/photos?${params.toString()}`);
    fpState.pickerPhotos = (data.photos || []).filter((p) => p.status !== "deleted");
    renderPlacePickerGrid();
  } catch (e) {
    if (grid) grid.innerHTML = `<div class="footprints-empty">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

async function openPlacePhotoPicker(place) {
  if (!place || place.id == null) return;
  fpState.pickerPlaceId = place.id;
  fpState.pickerSelected.clear();
  const titleEl = $("placePickerTitle");
  if (titleEl) {
    titleEl.innerHTML = `<span class="modal-title-icon">${ICONS.image}</span>添加照片到「${escapeHtml(placeDisplayName(place))}」`;
  }
  if ($("placePickerSearch")) $("placePickerSearch").value = "";
  if ($("placePickerUnlocatedOnly")) $("placePickerUnlocatedOnly").checked = false;
  updatePlacePickerCount();
  showModal($("placePhotoPickerModal"));
  pushModal($("placePhotoPickerModal"));
  await loadPlacePickerPhotos();
}

async function confirmAddPlacePhotos() {
  const placeId = fpState.pickerPlaceId;
  const ids = [...fpState.pickerSelected];
  if (!placeId || !ids.length) return;
  const btn = $("placePickerConfirmBtn");
  if (btn) btn.disabled = true;
  try {
    const data = await api(`/api/places/${placeId}/photos`, {
      method: "POST",
      body: JSON.stringify({ photo_ids: ids }),
    });
    closeModal("placePhotoPickerModal");
    showToast(`已添加 ${data.updated || ids.length} 张照片（数据库地点已更新，原图 EXIF 未改动）`, "success");
    await openFootprints({ reopenPlaceKey: `place:${placeId}` });
  } catch (e) {
    showToast(`添加失败：${e.message}`, "error");
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* --------------------------------------------------------------------------
   Quick-Assign Selected Gallery Photos to a Custom Place
   -------------------------------------------------------------------------- */

async function assignSelectedPhotosToPlace() {
  const ids = [...state.selected];
  if (!ids.length) return;
  try {
    const data = await api("/api/places");
    const places = data.places || [];
    if (!places.length) {
      openPlaceEditModal(null, { assignPhotoIds: ids });
      return;
    }
    const choices = places.map((p, i) => `${i + 1}. ${p.name}`).join("\n");
    const raw = window.prompt(
      `将选中的 ${ids.length} 张照片归入哪个自定义地点？（输入序号，或输入新地点名称直接新建）\n${choices}`,
      "1"
    );
    if (raw === null) return;
    const text = raw.trim();
    if (!text) return;
    const num = Number(text);
    if (Number.isInteger(num) && num >= 1 && num <= places.length) {
      const target = places[num - 1];
      const res = await api(`/api/places/${target.id}/photos`, {
        method: "POST",
        body: JSON.stringify({ photo_ids: ids }),
      });
      showToast(`已将 ${res.updated || ids.length} 张照片归入「${target.name}」`, "success");
      state.selected.clear();
      updateSelectionBar();
      if (state.view === "album") loadPhotos();
      return;
    }
    const existing = places.find((p) => p.name.toLowerCase() === text.toLowerCase());
    if (existing) {
      const res = await api(`/api/places/${existing.id}/photos`, {
        method: "POST",
        body: JSON.stringify({ photo_ids: ids }),
      });
      showToast(`已将 ${res.updated || ids.length} 张照片归入「${existing.name}」`, "success");
      state.selected.clear();
      updateSelectionBar();
      if (state.view === "album") loadPhotos();
      return;
    }
    // Create new place with auto forward geocoding and assign photos
    const res = await api("/api/places", {
      method: "POST",
      body: JSON.stringify({ name: text, photo_ids: ids, auto_geocode: true }),
    });
    showToast(`已新建地点「${text}」并归入 ${res.assigned || ids.length} 张照片`, "success");
    state.selected.clear();
    updateSelectionBar();
    if (state.view === "album") loadPhotos();
  } catch (e) {
    showToast(`归入地点失败：${e.message}`, "error");
  }
}

/* --------------------------------------------------------------------------
   Init Footprints Event Listeners
   -------------------------------------------------------------------------- */

function initFootprints() {
  const back = $("footprintsBackBtn");
  if (back) back.addEventListener("click", closePlace);

  const createBtn = $("footprintsCreatePlaceBtn");
  if (createBtn) createBtn.addEventListener("click", () => openPlaceEditModal(null));

  const modeSwitch = $("footprintsModeSwitch");
  if (modeSwitch) {
    modeSwitch.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-fp-mode]");
      if (!btn) return;
      setFootprintsMode(btn.dataset.fpMode);
    });
  }

  const geocodeBtn = $("placeGeocodeBtn");
  if (geocodeBtn) geocodeBtn.addEventListener("click", lookupPlaceCoordinates);

  const nameInput = $("placeFormName");
  if (nameInput) {
    nameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        lookupPlaceCoordinates();
      }
    });
  }

  const savePlaceBtn = $("placeFormSaveBtn");
  if (savePlaceBtn) savePlaceBtn.addEventListener("click", savePlaceForm);

  const pickerSearch = $("placePickerSearch");
  if (pickerSearch) {
    let timer = null;
    pickerSearch.addEventListener("input", () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(loadPlacePickerPhotos, 220);
    });
  }
  const pickerUnlocated = $("placePickerUnlocatedOnly");
  if (pickerUnlocated) pickerUnlocated.addEventListener("change", renderPlacePickerGrid);

  const pickerGrid = $("placePickerGrid");
  if (pickerGrid) {
    pickerGrid.addEventListener("click", (e) => {
      const card = e.target.closest("[data-pick-photo]");
      if (!card) return;
      const pid = Number(card.dataset.pickPhoto);
      if (!Number.isFinite(pid)) return;
      if (fpState.pickerSelected.has(pid)) fpState.pickerSelected.delete(pid);
      else fpState.pickerSelected.add(pid);
      card.classList.toggle("is-selected", fpState.pickerSelected.has(pid));
      const chk = card.querySelector(".place-picker-check");
      if (chk) chk.innerHTML = fpState.pickerSelected.has(pid) ? ICONS.check : "";
      updatePlacePickerCount();
    });
  }

  const selectVisBtn = $("placePickerSelectVisibleBtn");
  if (selectVisBtn) {
    selectVisBtn.addEventListener("click", () => {
      document.querySelectorAll("#placePickerGrid [data-pick-photo]").forEach((el) => {
        const pid = Number(el.dataset.pickPhoto);
        if (Number.isFinite(pid)) fpState.pickerSelected.add(pid);
      });
      renderPlacePickerGrid();
    });
  }
  const clearPickBtn = $("placePickerClearBtn");
  if (clearPickBtn) {
    clearPickBtn.addEventListener("click", () => {
      fpState.pickerSelected.clear();
      renderPlacePickerGrid();
    });
  }
  const confirmPickBtn = $("placePickerConfirmBtn");
  if (confirmPickBtn) confirmPickBtn.addEventListener("click", confirmAddPlacePhotos);

  const body = $("footprintsBody");
  if (!body) return;

  window.addEventListener(
    "scroll",
    () => {
      const place = state.footprintPlace;
      if (!place) return;
      const photos = place.photos || [];
      if ((state.footprintShown || 0) >= photos.length) return;
      if (state.view !== "footprints") return;
      const rect = body.getBoundingClientRect();
      if (rect.bottom > window.innerHeight + 600) return;
      appendPlacePhotos();
    },
    { passive: true }
  );

  body.addEventListener("click", (e) => {
    const photo = e.target.closest(".polaroid-card");
    if (photo) {
      const id = Number(photo.dataset.id);
      const place = state.footprintPlace;
      if (!Number.isFinite(id) || !place) return;
      if (fpState.removeMode) {
        if (fpState.removeSelected.has(id)) fpState.removeSelected.delete(id);
        else fpState.removeSelected.add(id);
        photo.classList.toggle("is-remove-selected", fpState.removeSelected.has(id));
        renderPlaceDetailBar(place);
        return;
      }
      if (e.target.closest("button:not(.polaroid-card)")) return;
      openPreview(id, {
        ids: (place.photos || []).map((row) => row.id),
        title: placeDisplayName(place),
      });
      return;
    }
    const card = e.target.closest(".footprint-card");
    if (!card) return;
    openPlace(Number(card.dataset.index));
  });
}
