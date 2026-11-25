/* ==========================================================================
   Lumina Album - Gallery: paging, polaroid rendering, culling, endless scroll
   ========================================================================== */
"use strict";

function hideSimilarSearchBanner() {
  const banner = $("similarSearchBanner");
  if (banner) banner.classList.add("hidden");
  state.similarTargetId = null;
}

async function loadSmartAlbums() {
  try {
    const data = await api("/api/smart-albums");
    state.smartAlbums = data.albums || [];
    renderSmartAlbums();
  } catch (_e) {
    // Non-critical strip
  }
}

function summarizeSmartAlbumRules(r) {
  if (!r || typeof r !== "object") return "默认全部";
  const parts = [];
  if (r.folder === "__all__") parts.push("全部可见相册");
  if (r.min_score) parts.push(`${r.min_score}分+`);
  if (r.tag) parts.push(`#${r.tag}`);
  if (r.year) parts.push(`${r.year}年`);
  if (r.favorite === "1" || r.status === "favorite") parts.push("已珍藏");
  if (r.kind === "video") parts.push("视频");
  else if (r.kind === "live") parts.push("实况");
  if (r.search) parts.push(`「${r.search}」`);
  return parts.length ? parts.join(" · ") : "当前目录全部回忆";
}

function renderSmartAlbums() {
  const list = $("smartAlbumsList");
  const badge = $("smartAlbumsCount");
  const albums = state.smartAlbums || [];
  if (badge) {
    badge.textContent = String(albums.length);
    badge.classList.toggle("hidden", !albums.length);
  }
  if (!list) return;
  if (!albums.length) {
    list.innerHTML = '<div class="people-empty">还没有创建智能影集，可在上方输入名称保存当前筛选条件。</div>';
    return;
  }
  list.innerHTML = albums
    .map(
      (a) =>
        `<div class="smart-album-row" data-smart-album-id="${a.id}"><div class="smart-album-row-info"><div class="smart-album-row-name">${escapeHtml(a.name)}</div><div class="smart-album-row-meta">${escapeHtml(summarizeSmartAlbumRules(a.rules))}</div></div><div class="smart-album-row-actions"><button type="button" class="btn small primary" data-smart-apply="${a.id}">打开</button><button type="button" class="btn small danger" data-smart-del="${a.id}">删除</button></div></div>`
    )
    .join("");
  list.querySelectorAll("[data-smart-apply]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = Number(btn.dataset.smartApply);
      const album = (state.smartAlbums || []).find((x) => x.id === id);
      if (album) {
        closeModal("smartAlbumsModal");
        applySmartAlbum(album);
      }
    });
  });
  list.querySelectorAll("[data-smart-del]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteSmartAlbum(Number(btn.dataset.smartDel));
    });
  });
}

function openSmartAlbumsModal() {
  const modal = $("smartAlbumsModal");
  if (!modal) return;
  loadSmartAlbums();
  showModal(modal);
  pushModal(modal);
  const inp = $("smartAlbumNameInput");
  if (inp) {
    inp.value = "";
    setTimeout(() => inp.focus(), 60);
  }
}

function applySmartAlbum(album) {
  const r = (album && album.rules) || {};
  if (r.folder) {
    state.folder = r.folder;
    const inp = $("folderInput");
    if (inp) inp.value = r.folder === "__all__" ? "全部可见相册" : r.folder;
  }
  if (r.status !== undefined) setSelectValue($("statusFilter"), r.status || "all");
  if (r.kind !== undefined) setSelectValue($("kindFilter"), r.kind || "");
  if (r.min_score !== undefined) setSelectValue($("minScoreFilter"), r.min_score || "");
  if (r.tag !== undefined) setSelectValue($("tagFilter"), r.tag || "");
  if (r.year !== undefined) setSelectValue($("yearFilter"), r.year || "");
  if (r.favorite !== undefined) setSelectValue($("favoriteFilter"), r.favorite || "");
  if (r.sort !== undefined) setSelectValue($("sortFilter"), r.sort || "score_desc");
  const modeVal = r.search_mode === "vector" ? "smart" : r.search_mode || "smart";
  if (r.search_mode !== undefined) setSelectValue($("searchMode"), modeVal);
  const searchEl = $("searchInput");
  if (searchEl && r.search !== undefined) searchEl.value = r.search || "";
  if (state.view !== "album") showView("album");
  loadPhotos();
  showToast(`已打开智能影集「${album.name}」`, "info");
}

async function saveCurrentSmartAlbum(explicitName = "") {
  const inp = $("smartAlbumNameInput");
  const name = (explicitName || (inp ? inp.value : "")).trim();
  if (!name) {
    showToast("请先输入影集名称", "warning");
    if (inp) inp.focus();
    return;
  }
  const rules = {
    folder: state.folder || "__all__",
    status: $("statusFilter").value,
    kind: $("kindFilter").value,
    min_score: $("minScoreFilter").value,
    tag: $("tagFilter").value,
    year: $("yearFilter").value,
    favorite: $("favoriteFilter").value,
    sort: $("sortFilter").value,
    search_mode: $("searchMode").value,
    search: $("searchInput").value.trim(),
  };
  try {
    await api("/api/smart-albums", {
      method: "POST",
      body: JSON.stringify({ name, rules }),
    });
    if (inp) inp.value = "";
    showToast(`已保存智能影集「${name}」`, "success");
    await loadSmartAlbums();
  } catch (e) {
    showToast("保存智能影集失败：" + e.message, "error");
  }
}

async function deleteSmartAlbum(albumId) {
  if (!albumId) return;
  const album = (state.smartAlbums || []).find((a) => a.id === albumId);
  const label = album ? `「${album.name}」` : "这个智能影集";
  const confirmed = await confirmDialog(
    `确定删除虚拟智能影集${label}吗？\n仅移除这条筛选规则，不会影响磁盘上的任何原图。`,
    { title: "删除智能影集", confirmText: "确认删除", danger: true }
  );
  if (!confirmed) return;
  try {
    await api(`/api/smart-albums/${albumId}`, { method: "DELETE" });
    state.smartAlbums = (state.smartAlbums || []).filter((a) => a.id !== albumId);
    renderSmartAlbums();
    showToast(`已删除智能影集${label}`, "info");
  } catch (e) {
    showToast("删除失败：" + e.message, "error");
  }
}

async function findSimilarPhotos(photoId) {
  if (!photoId) return;
  showLoading("正在按视觉向量寻找相似画面…");
  try {
    const params = new URLSearchParams();
    if (state.folder) params.set("folder", state.folder);
    const data = await api(`/api/photo/${photoId}/similar?${params.toString()}`);
    closeModal("previewModal");
    if (state.view !== "album") showView("album");
    const target = data.target || photoById(photoId);
    const photos = data.photos || [];
    state.similarTargetId = photoId;
    indexPhotos(photos);
    state.offset = photos.length;
    state.hasMore = false;
    state.renderedCount = 0;
    renderGallery({ animate: true });
    updateLoadMoreButton();
    const banner = $("similarSearchBanner");
    const text = $("similarSearchText");
    if (banner && text) {
      const label = (target && (target.title || target.filename)) || `#${photoId}`;
      text.textContent = photos.length
        ? `与「${label}」画面相似的 ${photos.length} 张照片`
        : `暂未在当前相册找到与「${label}」相似的画面`;
      banner.classList.remove("hidden");
    }
  } catch (e) {
    showToast("以图搜图失败：" + e.message, "error");
  } finally {
    hideLoading();
  }
}

async function loadPhotos(options = {}) {
  const quiet = !!options.quiet;
  if (!quiet) {
    hideSimilarSearchBanner();
    updateFilterHighlights();
  }
  const shouldSkeleton = !quiet && !state.photos.length;
  if (shouldSkeleton) renderSkeleton();

  const params = getFilterParams(0);
  const mode = $("searchMode").value;
  const query = $("searchInput").value.trim();
  let data;
  let hasMore = false;
  try {
    if ((mode === "semantic" || mode === "smart" || mode === "visual") && query) {
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
      if (!quiet) showLoading("正在寻找相关回忆…");
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
    // A visual search only looks at a bounded number of photos; saying so keeps
    // "no results" from reading as "not in your library".
    if (data.checked && !quiet) {
      const total = (data.photos || []).length;
      showToast(
        `看图搜索只查看了最前面的 ${data.checked} 张${total ? "，其余可以用筛选缩小范围后再搜" : "，没有找到符合的画面"}`,
        "info"
      );
    }
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
    // The model being down is a different story from a failed read: it is the
    // likely reason a semantic search has nothing to show, and the message the
    // server sends already names the endpoint. Saying "读取照片失败" would hide
    // the one thing the reader can act on.
    const unreachable = /模型|model|无法连接/i.test(e.message || "");
    if (unreachable) {
      showToast(`暂时无法连线大模型，已保留你的搜索词，稍后可以重试。${e.message}`, "warning");
      return;
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

/**
 * Record what the reader is currently looking at, so a layout change can put it
 * back under their eyes.
 *
 * Appending cards is safe for the content *below* the fold, but the grid is
 * re-laid out as a whole: culling cards above the viewport, a card changing
 * height, or the very first activation of `content-visibility` all move
 * everything below them. `scrollTop` is preserved by the browser, so the page
 * appears to jump backwards even though the scroll offset never changed.
 *
 * Anchoring on an element rather than on a pixel offset is what makes the
 * correction exact: the delta measured from the same card is precisely how far
 * the content moved.
 */
function captureScrollAnchor() {
  const gallery = $("gallery");
  if (!gallery || !gallery.children.length) return null;
  const cards = gallery.children;
  for (let i = 0; i < cards.length; i += 1) {
    const rect = cards[i].getBoundingClientRect();
    // The first card still showing: everything above it is off-screen, which is
    // exactly the content whose height change displaces the view.
    if (rect.bottom > 0) return { id: cards[i].dataset.id, top: rect.top };
  }
  return null;
}

function restoreScrollAnchor(anchor) {
  if (!anchor) return;
  const gallery = $("gallery");
  if (!gallery) return;
  const card = gallery.querySelector(`.polaroid-card[data-id="${anchor.id}"]`);
  // The anchor itself may have been removed (a delete, a filter change), in
  // which case there is nothing meaningful to restore to.
  if (!card) return;
  const delta = card.getBoundingClientRect().top - anchor.top;
  // A sub-pixel difference is rounding noise, and scrolling for it would fight
  // the browser's own anchoring.
  if (Math.abs(delta) >= 1) {
    window.scrollBy({ top: delta, left: 0, behavior: "instant" });
  }
}

/**
 * Hold the view still until the grid has stopped moving under it.
 *
 * Appending the first page past `CARD_CULL_MIN_CARDS` is what wakes culling up,
 * and culling does not run at append time: it waits for the debounced sweep and
 * the thumbnail observer's first delivery, which lands a few hundred ms later.
 * `cullCard` pins each card's placeholder height so that step no longer moves
 * anything, but the grid still settles its row heights over the following
 * frames - so the correction is re-applied on a short schedule rather than once.
 * Each pass measures where the anchor *actually* is and scrolls by the
 * difference, which makes it a no-op once nothing is moving.
 */
const SCROLL_HOLD_PASSES = [0, 120, 260, 420, 620, 900];

function holdScrollAnchor(anchor) {
  if (!anchor) return;
  for (const delay of SCROLL_HOLD_PASSES) {
    if (delay === 0) {
      restoreScrollAnchor(anchor);
      continue;
    }
    setTimeout(() => restoreScrollAnchor(anchor), delay);
  }
}

async function loadMorePhotos() {
  if (state.loadingMore || !state.hasMore) return;
  state.loadingMore = true;
  $("loadMoreBtn").classList.add("loading");
  // Captured before the grid grows: the first append is also what wakes the
  // culling up (it needs CARD_CULL_MIN_CARDS rows), and that can move content
  // that is already on screen.
  const anchor = captureScrollAnchor();
  try {
    const params = getFilterParams(state.offset);
    const data = await api(`/api/photos?${params.toString()}`);
    const nextPhotos = data.photos || [];
    indexPhotos(state.photos.concat(nextPhotos));
    state.offset += nextPhotos.length;
    state.hasMore = !!data.has_more;
    renderGallery({ append: true, animate: true });
    updateLoadMoreButton();
    holdScrollAnchor(anchor);
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
    if (current) {
      // A tag opened from the detail sheet may not be in the current album's
      // stats list (for example the photo is in a different folder). Keep it
      // as an explicit option so the filter is not silently cleared.
      if (![...select.options].some((opt) => opt.value === current)) {
        const option = document.createElement("option");
        option.value = current;
        option.textContent = current;
        select.appendChild(option);
      }
      select.value = current;
    }
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
 * Format a compact editorial date ("2024.11.08") for a card's meta row.
 * Prefers capture time when available, falling back to analyzed_at.
 */
function cardDateText(photo) {
  const exif = photo && photo.exif && typeof photo.exif === "object" ? photo.exif : null;
  const raw =
    (photo && photo.captured_at) ||
    (exif &&
      (exif.datetime_original ||
        exif.DateTimeOriginal ||
        exif.datetime ||
        exif.create_date ||
        exif.DateTimeDigitized)) ||
    (photo && photo.taken_at) ||
    (photo && photo.analyzed_at) ||
    "";
  const match = /^(\d{4})[-:/](\d{2})[-:/](\d{2})/.exec(String(raw).trim());
  return match ? `${match[1]}.${match[2]}.${match[3]}` : "";
}

/**
 * Parse a verbose geocoded address (Amap / Nominatim / manual / GPS) into a
 * concise, editorial 1-to-2-part place label for photo cards (e.g. "杭州 · 西湖",
 * "京都 · 东山", "Sydney · Opera House"). The detail lightbox still shows the full address.
 */
function formatShortLocation(raw) {
  const text = String(raw || "").trim();
  if (!text) return "";

  // Raw GPS coordinates fallback (e.g. "30.2589, 120.1456")
  const gps = /^(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)$/.exec(text);
  if (gps) {
    const lat = Number(gps[1]);
    const lon = Number(gps[2]);
    if (Number.isFinite(lat) && Number.isFinite(lon)) {
      return `${Math.abs(lat).toFixed(2)}°${lat >= 0 ? "N" : "S"}, ${Math.abs(lon).toFixed(2)}°${lon >= 0 ? "E" : "W"}`;
    }
  }

  const stripCnSuffix = (s) =>
    s.replace(/(特别行政区|维吾尔自治区|回族自治区|壮族自治区|自治区|自治州|地区|盟|市|省|区|县|旗)$/, "");
  const isPostal = (s) => /^\d{3,6}(?:-\d{3,4})?$/.test(s);
  const isCountry = (s) =>
    /^(中国|中华人民共和国|China|People's Republic of China|PR China)$/i.test(s);
  const isRoadOrNumber = (s) =>
    /^\d+[号弄栋幢楼室]?$/.test(s) || /(路|街|大道|巷|弄|胡同|街道|镇|乡|村|社区)\d*号?$/.test(s);

  // Already concise manual label (e.g. "京都 · 西芳寺", "杭州，西湖")
  if (text.length <= 12 && !text.includes(",") && !/[省市区县]/.test(text)) {
    return text.replace(/[，、/]+/g, " · ").replace(/\s*·\s*/g, " · ");
  }

  // Case 1: Comma-separated (Nominatim display_name or manual comma list)
  if (/[,，]/.test(text)) {
    const parts = text
      .split(/[,，]/)
      .map((s) => s.trim())
      .filter((s) => s && !isPostal(s) && !isCountry(s));
    if (!parts.length) return text;

    // Detect Chinese Nominatim (specific -> district -> city -> province)
    const hasChinese = /[\u4e00-\u9fff]/.test(text);
    if (hasChinese) {
      const cityIdx = parts.findIndex((p) => /(市|自治州|地区|盟|特别行政区)$/.test(p));
      const distIdx = parts.findIndex((p) => /(区|县|旗)$/.test(p) && !/(自治区|特别行政区)$/.test(p));
      if (cityIdx >= 0) {
        const city = stripCnSuffix(parts[cityIdx]);
        const subRaw =
          distIdx >= 0 && distIdx !== cityIdx
            ? parts[distIdx]
            : parts.find((p, i) => i < cityIdx && !isRoadOrNumber(p));
        const sub = subRaw ? stripCnSuffix(subRaw) : "";
        if (sub && sub !== city) return `${city} · ${sub}`;
        return city;
      }
      const clean = parts.filter((p) => !isRoadOrNumber(p)).map(stripCnSuffix).filter(Boolean);
      const uniq = [...new Set(clean)];
      if (uniq.length >= 2) return `${uniq[uniq.length - 1]} · ${uniq[0]}`;
      return uniq[0] || parts[0];
    }

    // Non-Chinese Nominatim (e.g. "Opera House, Sydney, New South Wales, Australia")
    const filtered = parts.filter(
      (p) => !/^Council of /i.test(p) && !/^City of /i.test(p) && !/^\d+$/.test(p)
    );
    if (filtered.length >= 3) {
      const country = filtered[filtered.length - 1];
      const city = filtered.length >= 4 ? filtered[filtered.length - 3] : filtered[filtered.length - 2];
      const spot = filtered[0];
      if (spot && city && spot.toLowerCase().includes(city.toLowerCase()) && spot.length <= 22) {
        return spot;
      }
      if (spot && city && spot !== city && spot.length <= 18) return `${city} · ${spot}`;
      if (city && country && city !== country) return `${city} · ${country}`;
      return city || spot;
    }
    return filtered.slice(0, 2).join(" · ");
  }

  // Case 2: Continuous Chinese address (Amap formatted_address)
  // e.g. "浙江省杭州市西湖区北山街道断桥残雪" or "北京市朝阳区三里屯街道..."
  let rest = text;
  let prov = "";
  const provMatch = /^(.{2,8}?(?:特别行政区|维吾尔自治区|回族自治区|壮族自治区|自治区|省))/.exec(rest);
  if (provMatch) {
    prov = stripCnSuffix(provMatch[1]);
    rest = rest.slice(provMatch[1].length);
  }
  let city = "";
  const cityMatch = /^(.{2,10}?(?:自治州|地区|盟|市))/.exec(rest);
  if (cityMatch) {
    city = stripCnSuffix(cityMatch[1]);
    rest = rest.slice(cityMatch[1].length);
  }
  let dist = "";
  const distMatch = /^(.{2,8}?(?:区|县|旗|市))/.exec(rest);
  if (distMatch) {
    dist = stripCnSuffix(distMatch[1]);
  }
  if (prov || city || dist) {
    const primary = city || prov;
    const secondary = dist && dist !== primary ? dist : "";
    if (primary && secondary) return `${primary} · ${secondary}`;
    if (primary) return primary;
  }

  return text.length > 16 ? `${text.slice(0, 15)}…` : text;
}

const VINTAGE_FILM_STOCKS = [
  "KODAK PORTRA 400",
  "FUJIFILM SUPERIA 400",
  "KODAK GOLD 200",
  "CINESTILL 800T",
  "ILFORD HP5 PLUS 400",
];

function vintageFilmCode(photo) {
  const id = Math.abs(Number(photo.id) || 0);
  const stock = VINTAGE_FILM_STOCKS[id % VINTAGE_FILM_STOCKS.length];
  const frame = String((id % 36) + 1).padStart(2, "0");
  return { stock, frame: `${frame}A` };
}

function vintageDateStamp(photo) {
  const dot = cardDateText(photo);
  if (!dot) return "";
  const parts = dot.split(".");
  if (parts.length !== 3) return "";
  return `'${parts[0].slice(-2)} ${parts[1]} ${parts[2]}`;
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
            ${dimensionValues(dims, photo.score, isVideo)}
          </div>`
    : "";

  const dateStr = cardDateText(photo);
  const titleText = (photo.title || "").trim();
  const hasMetaRow = Boolean(photo.location || dateStr);
  const isHighScore = hasScore && Number(photo.score) >= 8.5;

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
  const film = vintageFilmCode(photo);
  const quartzDate = vintageDateStamp(photo);
  return `
    <div class="polaroid-inner"${isHighScore ? ' data-high-score="1"' : ""}>
      <div class="polaroid-front">
        ${isVideo ? '<div class="film-strip" aria-hidden="true"></div>' : '<div class="tape" aria-hidden="true"></div>'}
        ${isVideo ? "" : `<div class="vintage-film-header" aria-hidden="true"><span class="vintage-film-stock">${film.stock}</span><span class="vintage-sprockets"><i></i><i></i><i></i><i></i><i></i></span><span class="vintage-film-frame">▸ ${film.frame}</span></div>`}
        <button class="favorite-btn ${photo.favorite ? "active" : ""}" title="${photo.favorite ? "取消珍藏" : "珍藏"}">${photo.favorite ? ICONS.heartFilled : ICONS.heart}</button>
        ${rawBadge}
        ${liveBadgeHtml(photo)}
        <div class="polaroid-photo-frame"><img class="polaroid-photo" data-src="${thumbnailUrl(photo)}" alt="${escapeHtml(photo.filename)}" />${
          isVideo ? `<span class="video-card-play" aria-hidden="true">${ICONS.play}</span>${videoDurationBadge(photo)}` : ""
        }${quartzDate && !isVideo ? `<span class="vintage-date-stamp" aria-hidden="true">${quartzDate}</span>` : ""}<div class="wall-overlay" aria-hidden="true"><span class="wall-title">${escapeHtml(titleText || photo.filename || "")}</span>${hasScore ? `<span class="wall-score">${scoreText(photo.score)}</span>` : ""}</div></div>
        ${isVideo ? "" : `<div class="vintage-film-footer" aria-hidden="true"><span>EXP ${film.frame.replace("A", "")}/36</span><span class="vintage-sprockets"><i></i><i></i><i></i><i></i><i></i></span><span>SAFETY FILM</span></div>`}
        <div class="polaroid-body">
          ${
            hasMetaRow
              ? `<div class="card-meta-row">
                  ${photo.location ? `<div class="photo-location" title="${escapeHtml(photo.location)}"><span class="location-pin-icon" aria-hidden="true">${ICONS.pin}</span><span>${escapeHtml(formatShortLocation(photo.location))}</span></div>` : '<span class="photo-location-spacer"></span>'}
                  ${dateStr ? `<span class="card-date">${escapeHtml(dateStr)}</span>` : ""}
                </div>`
              : ""
          }
          ${titleText ? `<h3 class="photo-title">${escapeHtml(titleText)}</h3>` : ""}
          ${photo.reason ? `<div class="photo-comment"><span class="classic-icon" aria-hidden="true">${ICONS.quote}</span>${escapeHtml(photo.reason)}</div>` : ""}
          ${tagsHtml ? `<div class="back-tags">${tagsHtml}</div>` : ""}
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
 * The album's polaroid card, for a read-only grid.
 *
 * 那年今日 and a place in 足迹 both show photos the reader can look at but not act on
 * from here, so the card carries the tape, the frame, the caption, the location and
 * the tags - and none of the per-card buttons. Wrapping it in the same classes as the
 * gallery means the tilt cycle, the entrance and the hover all come from CSS that
 * already exists; a second, nearly-identical card would drift away from it.
 */
function memoryPolaroidHtml(photo, options = {}) {
  const { deactivate = false } = options;
  const isVideo = photo.media_kind === "video";
  const tags = Array.isArray(photo.tags) ? photo.tags.slice(0, 3) : [];
  const tagsHtml = tags.length
    ? `<div class="back-tags">${tags
        .map((tag) => `<span class="tag">${escapeHtml(String(tag))}</span>`)
        .join("")}</div>`
    : "";
  const hasScore = photo.score !== null && photo.score !== undefined;
  const score = hasScore
    ? `<span class="score-pill total">总分 ${Number(photo.score).toFixed(1)}</span>`
    : "";
  const dateStr = cardDateText(photo);
  const titleText = (photo.title || "").trim();
  const comment = (photo.reason || "").trim();
  const hasMetaRow = Boolean(photo.location || dateStr);
  const isHighScore = hasScore && Number(photo.score) >= 8.5;
  const film = vintageFilmCode(photo);
  const quartzDate = vintageDateStamp(photo);
  return `
    <div class="polaroid-card${isVideo ? " is-video" : ""}${deactivate ? " is-static" : ""}"
         data-id="${photo.id}">
      <div class="polaroid-inner"${isHighScore ? ' data-high-score="1"' : ""}>
        <div class="polaroid-front">
          ${isVideo ? '<div class="film-strip" aria-hidden="true"></div>' : '<div class="tape" aria-hidden="true"></div>'}
          ${isVideo ? "" : `<div class="vintage-film-header" aria-hidden="true"><span class="vintage-film-stock">${film.stock}</span><span class="vintage-sprockets"><i></i><i></i><i></i><i></i><i></i></span><span class="vintage-film-frame">▸ ${film.frame}</span></div>`}
          <div class="polaroid-photo-frame">
            <img class="polaroid-photo" data-src="${thumbnailUrl(photo)}"
                 alt="${escapeHtml(photo.filename || "")}" loading="lazy" decoding="async" />
            ${quartzDate && !isVideo ? `<span class="vintage-date-stamp" aria-hidden="true">${quartzDate}</span>` : ""}
          </div>
          ${isVideo ? "" : `<div class="vintage-film-footer" aria-hidden="true"><span>EXP ${film.frame.replace("A", "")}/36</span><span class="vintage-sprockets"><i></i><i></i><i></i><i></i><i></i></span><span>SAFETY FILM</span></div>`}
          <div class="polaroid-body">
            ${
              hasMetaRow
                ? `<div class="card-meta-row">
                    ${photo.location ? `<div class="photo-location" title="${escapeHtml(photo.location)}"><span class="location-pin-icon" aria-hidden="true">${ICONS.pin}</span><span>${escapeHtml(formatShortLocation(photo.location))}</span></div>` : '<span class="photo-location-spacer"></span>'}
                    ${dateStr ? `<span class="card-date">${escapeHtml(dateStr)}</span>` : ""}
                  </div>`
                : ""
            }
            ${titleText ? `<h3 class="photo-title">${escapeHtml(titleText)}</h3>` : ""}
            ${comment ? `<div class="photo-comment"><span class="classic-icon" aria-hidden="true">${ICONS.quote}</span>${escapeHtml(comment)}</div>` : ""}
            ${tagsHtml}
            ${score ? `<div class="card-actions card-actions-readonly"><div class="score-pills">${score}</div></div>` : ""}
          </div>
        </div>
      </div>
    </div>`;
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
            cullCard(card, card.getBoundingClientRect().height);
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

  // Culling the cards above the fold collapses them to contain-intrinsic-size,
  // which shortens everything above the reader. Measuring the first visible card
  // before and after keeps the view on the same photo instead of letting it
  // appear to scroll backwards.
  const anchor = captureScrollAnchor();

  const near = new Array(cards.length);
  const heights = new Array(cards.length);
  for (let i = 0; i < cards.length; i += 1) {
    const rect = cards[i].getBoundingClientRect();
    near[i] = rect.bottom > top && rect.top < bottom;
    heights[i] = rect.height;
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
      cullCard(card, heights[i]);
    }
  }
  restoreScrollAnchor(anchor);
}

/**
 * Take a card out of the render tree without moving anything around it.
 *
 * `contain-intrinsic-size: auto 586px` in the stylesheet is a single guess for
 * every card, but a polaroid's height depends on its photo's aspect ratio and how
 * many lines its caption takes. Culling a column of cards therefore *changed
 * their heights*, and since the placeholder only applies to cards the browser
 * has not measured, the ones above the fold silently grew or shrank - pushing
 * everything below them and making the page appear to jump backwards mid-scroll.
 *
 * Pinning the placeholder to the height the card actually has right now makes
 * culling a pure rendering decision: no layout change, no shift, nothing for the
 * scroll anchor to correct.
 */
function cullCard(card, height) {
  if (height > 0) {
    // Sub-pixel precision: rounding to whole pixels is what left a few px of
    // drift per culled card, which adds up to ~18 px over a column of sixteen.
    card.style.containIntrinsicSize = `auto ${height}px`;
  }
  card.classList.add("is-culled");
}

/**
 * Register every polaroid inside ``root`` with the lazy loader.
 *
 * The gallery calls `watchThumb` card by card as it builds the grid. 那年今日 and 足迹
 * insert a block of cards in one `innerHTML`, so they need the whole subtree swept -
 * without it the pictures stay `data-src` and the cards render empty.
 */
function watchPolaroids(root) {
  if (!root) return;
  for (const card of root.querySelectorAll(".polaroid-card")) watchThumb(card);
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
    // The album's own size, not the stats row: when a folder has just been
    // switched the row still shows the previous album for a frame, which made
    // a brand-new (empty) folder claim it was only hiding behind filters.
    const totalInAlbum = Number(state.albumTotal || 0);
    const hasFilterOrSearch =
      ($("searchInput") && $("searchInput").value.trim() !== "") ||
      ($("kindFilter") && $("kindFilter").value !== "") ||
      ($("minScoreFilter") && $("minScoreFilter").value !== "") ||
      ($("tagFilter") && $("tagFilter").value !== "") ||
      ($("yearFilter") && $("yearFilter").value !== "") ||
      ($("statusFilter") && $("statusFilter").value !== "all" && totalInAlbum > 0);
    const textEl = $("emptyStateText");
    const iconEl = $("emptyStateIcon");
    const resetBtn = $("emptyResetFiltersBtn");
    if (textEl) {
      if (hasFilterOrSearch && totalInAlbum > 0) {
        if (iconEl && ICONS && ICONS.search) iconEl.innerHTML = ICONS.search;
        textEl.innerHTML =
          '<span class="empty-state-title">没有寻访到相符的回忆</span>' +
          '<span class="empty-state-desc">不妨放宽筛选条件，或换一个关键词再试一次。</span>';
        if (resetBtn) resetBtn.classList.remove("hidden");
      } else {
        if (iconEl && ICONS && ICONS.collection) iconEl.innerHTML = ICONS.collection;
        textEl.innerHTML =
          '<span class="empty-state-title">这一册还是空白</span>' +
          '<span class="empty-state-desc">在上方选定存放照片的文件夹，点「开始整理」翻开第一页。</span>';
        if (resetBtn) resetBtn.classList.add("hidden");
      }
    }
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
      // Cap entrance stagger to the first 8 visible cards (max ~190ms) so
      // long pages never feel delayed, per Emil Kowalski choreography rules.
      card.style.animationDelay = `${Math.min(offset, 8) * 0.024}s`;
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
  if (typeof updateTimelineScrubber === "function") updateTimelineScrubber();
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
  if (!btn) return;
  btn.classList.toggle("active", !!photo.favorite);
  btn.title = photo.favorite ? "取消珍藏" : "珍藏";
  btn.innerHTML = photo.favorite ? ICONS.heartFilled : ICONS.heart;
  if (photo.favorite) {
    btn.classList.remove("is-popping");
    void btn.offsetWidth;
    btn.classList.add("is-popping");
    setTimeout(() => btn.classList.remove("is-popping"), 320);
  }
}

/* ==========================================================================
   Gallery events
   One listener per event type on the grid, instead of six per card. At 4000
   cards the old scheme registered 24,000 closures, all of them kept alive for
   as long as the cards were on the page.
   ========================================================================== */

/* ==========================================================================
   Endless gallery
   ========================================================================== */

//: How close to the bottom counts as "at the end", in pixels.
const AT_BOTTOM_SLACK = 4;

//: How far a gesture has to travel at the bottom before it pulls more in. Small
//: enough that a normal flick triggers it, large enough that the tail of the
//: scroll that *reached* the bottom does not.
const PULL_THRESHOLD = 60;

function isAtPageBottom() {
  const doc = document.documentElement;
  const scrolled = window.scrollY || window.pageYOffset || doc.scrollTop || 0;
  const height = Math.max(
    document.body ? document.body.scrollHeight : 0,
    doc.scrollHeight || 0
  );
  return scrolled + window.innerHeight >= height - AT_BOTTOM_SLACK;
}

/**
 * Pull the next page in when the reader asks for it by continuing past the end.
 *
 * The button is still there for anyone who prefers it, but the gesture people
 * actually make at the bottom of a gallery is "keep scrolling", and nothing used
 * to happen. Two details make this feel right rather than twitchy:
 *
 * - Only movement *at* the bottom counts. The wheel or touch that carries the
 *   reader to the bottom is consumed by that scroll, so it must not immediately
 *   fetch another page (that is the difference between "I reached the end" and
 *   "I want more").
 * - One fetch per visit to the bottom. The guard is the page height recorded once
 *   the previous fetch had settled: a completed fetch makes the page taller, and a
 *   pull only counts when the page is taller than that record. A momentum scroll or
 *   a thumb resting on the trackpad therefore cannot queue several pages, because
 *   the height does not change between them.
 */
function initEndlessGallery() {
  const stateful = {
    // Accumulated downward intent since arriving at the bottom.
    pull: 0,
    // Which visit to the end of the page this is. Bumped every time the reader is
    // seen somewhere other than the end; a fetch records the visit it served, so a
    // second pull in the *same* visit is refused while the next arrival is allowed.
    //
    // This is deliberately not a page-height comparison. That version could not
    // tell "still leaning on the trackpad" apart from "the page grew underneath me
    // and I scrolled down to the new end", so the second case silently stopped
    // working after the first fetch.
    visit: 0,
    // The visit a fetch has already been served for, or -1 before the first one.
    servedVisit: -1,
    // Set while a fetch is in flight, so one gesture cannot start two.
    settling: false,
  };

  // Leaving the end starts a new visit. `scroll` fires for wheel, drag, keyboard
  // and programmatic moves alike, so this catches all of them.
  const noteArrival = () => {
    if (!isAtPageBottom()) {
      stateful.visit += 1;
    }
  };

  const accumulate = (delta) => {
    // Only downward motion at the bottom is a request for more.
    if (delta <= 0 || !state.hasMore || state.loadingMore) return;
    if (state.loadingMore || stateful.settling) return;
    if (!isAtPageBottom()) return;
    stateful.pull += delta;
    if (stateful.pull < PULL_THRESHOLD) return;
    // One fetch per visit. `servedVisit` starts at -1 so the first pull at visit 0
    // is allowed, and a pull at the same visit after a fetch is refused.
    if (stateful.servedVisit >= stateful.visit) return;
    requestMore();
  };

  const requestMore = () => {
    if (state.loadingMore || !state.hasMore || stateful.settling) return;
    stateful.pull = 0;
    stateful.settling = true;
    stateful.servedVisit = stateful.visit;
    Promise.resolve(loadMorePhotos()).finally(() => {
      // The page has just grown, so whatever the reader was at the bottom of is no
      // longer the bottom. Re-check rather than assume: the browser may keep the
      // scroll pinned to the end, in which case this is still the same visit.
      setTimeout(() => {
        noteArrival();
        stateful.settling = false;
      }, 240);
    });
  };

  // A wheel event at the bottom has nowhere to scroll, so the browser reports the
  // whole delta here instead of consuming it.
  window.addEventListener(
    "wheel",
    (e) => {
      // Let the dialogs and the fullscreen viewer keep their own scrolling.
      if (state.currentPreviewId || isOverlayOpen()) return;
      accumulate(e.deltaY);
    },
    { passive: true }
  );

  // Touch: a drag past the end is the same gesture.
  let touchY = null;
  window.addEventListener(
    "touchstart",
    (e) => {
      touchY = e.touches.length === 1 ? e.touches[0].clientY : null;
      stateful.pull = 0;
    },
    { passive: true }
  );
  window.addEventListener(
    "touchmove",
    (e) => {
      if (touchY === null || e.touches.length !== 1) return;
      if (state.currentPreviewId || isOverlayOpen()) return;
      const y = e.touches[0].clientY;
      // Finger moving up means content moving down.
      accumulate(touchY - y);
      touchY = y;
    },
    { passive: true }
  );
  window.addEventListener("touchend", () => {
    touchY = null;
  });

  // Every scroll that leaves the end starts a new visit. The pull path re-checks
  // the position as well, so a jump that emits no intermediate event is still
  // counted correctly.
  window.addEventListener("scroll", noteArrival, { passive: true });

  // The button path must not be blocked by the gesture state.
  const btn = $("loadMoreBtn");
  if (btn) {
    btn.addEventListener("click", () => {
      stateful.pull = 0;
    });
  }
}

