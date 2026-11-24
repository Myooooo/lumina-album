/* ==========================================================================
   Lumina Album - Viewer: detail overlay, fullscreen zoom, video and Live Photo
   ========================================================================== */
"use strict";

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
      pushModal(fsModal);
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
  pushModal(fsModal);
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

/**
 * Show one photo in the detail dialog.
 *
 * ``context`` is ``{ids, title}`` when a sheet is opening it - 足迹's places, 那年今日's
 * days, 相似回忆's groups. The dialog then walks that list with the arrow keys and
 * labels itself with the title, instead of falling back to the album's rows, which
 * may not contain the photo at all.
 */
function findVisiblePhotoCardRect(id) {
  if (typeof document === "undefined" || !id) return null;
  const cardThumb =
    document.querySelector(`.polaroid-card[data-id="${id}"] .polaroid-photo-frame`) ||
    document.querySelector(`.dup-card[data-id="${id}"] .dup-shot`);
  if (!cardThumb) return null;
  const r = cardThumb.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0 || r.bottom <= 0 || r.top >= window.innerHeight) {
    return null;
  }
  return r;
}

function openPreview(id, context = null) {
  const modal = $("previewModal");
  const content = modal ? modal.querySelector(".preview-content") : null;
  const sourceRect = !prefersReducedMotion() ? findVisiblePhotoCardRect(id) : null;

  state.previewNavToken += 1;
  state.currentPreviewId = id;
  state.previewContext = context && context.ids && context.ids.length ? context : null;
  if (modal) modal.classList.remove("is-flip-opening", "is-flip-closing");
  if (content) {
    content.style.transition = "";
    content.style.transform = "";
    content.style.opacity = "";
  }
  showModal(modal);
  pushModal(modal);
  updatePreview();

  // Single unified directional FLIP on .preview-content (never fights modalPop
  // and always has deterministic geometry on frame 1 even before image decode).
  if (sourceRect && modal && content && typeof requestAnimationFrame === "function") {
    modal.classList.add("is-flip-opening");
    const target = content.getBoundingClientRect();
    if (target.width > 0 && target.height > 0) {
      const rawDx = sourceRect.left + sourceRect.width / 2 - (target.left + target.width / 2);
      const rawDy = sourceRect.top + sourceRect.height / 2 - (target.top + target.height / 2);
      const dx = Math.max(-180, Math.min(180, rawDx * 0.36));
      const dy = Math.max(-140, Math.min(140, rawDy * 0.36));
      content.style.transition = "none";
      content.style.transform = `translate3d(${dx.toFixed(1)}px, ${dy.toFixed(1)}px, 0) scale(0.92)`;
      content.style.opacity = "0";
      void content.offsetWidth;
      requestAnimationFrame(() => {
        content.style.transition = "transform 260ms var(--ease-out), opacity 190ms var(--ease-out)";
        content.style.transform = "translate3d(0, 0, 0) scale(1)";
        content.style.opacity = "1";
      });
      setTimeout(() => {
        modal.classList.remove("is-flip-opening");
        content.style.transition = "";
        content.style.transform = "";
        content.style.opacity = "";
      }, 280);
    }
  }
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
  showRenderingIndicator(false);
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
  // Cleared through the same helper the playback path uses, so the button's
  // state never depends on which code path ran last. Photos without a motion
  // hide it outright; the one below is shown again by the `hasLive` branch.
  hideLiveButton();
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
  //
  // Deliberately not tied to the video preview mode: that setting lives on the
  // 视频处理 tab and talks about video previews. Letting it silence Live Photos
  // meant a switch labelled "video" quietly changed how photographs behave.
  if (hasLive) {
    // Shown first, then playLiveMotion hides it again for the duration of the
    // motion - the still is what the button belongs to.
    setLiveButtonVisible(true);
    playLiveMotion(photo);
  }
}

/** Load and start the preview for ``photo``, honouring the preview settings. */
/**
 * Show or hide the "generating a preview" indicator.
 *
 * It sits over the poster while the first clip is encoded. Going away is a
 * fade, so the clip underneath appears to replace it rather than cut in.
 */
function showRenderingIndicator(visible) {
  const el = $("previewRendering");
  if (!el) return;
  if (visible) {
    clearTimeout(state.renderingHideTimer);
    state.renderingHideTimer = null;
    // Take it out of `hidden` first, then flip opacity on the next frame, or
    // the browser coalesces both changes and the fade-in never runs.
    el.classList.remove("hidden");
    void el.offsetWidth;
    el.classList.add("visible");
    return;
  }
  el.classList.remove("visible");
  // `display: none` would cut the fade short, so the element is only hidden
  // once the transition has finished.
  clearTimeout(state.renderingHideTimer);
  state.renderingHideTimer = setTimeout(() => {
    el.classList.add("hidden");
    state.renderingHideTimer = null;
  }, 420);
}

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
  // The poster is already on screen; the indicator covers the gap while the
  // first preview is encoded. It is shown straight away and dismissed by
  // `onReady`, which also handles the cache-hit case (it flashes and goes).
  showRenderingIndicator(true);
  video.src = wanted;
  const onReady = () => {
    if (video.dataset.photoId !== String(photo.id)) return;
    showRenderingIndicator(false);
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
  // Assigned rather than added: `playPreviewClip` runs once per opened video, so
  // `addEventListener` accumulated one more pair of listeners every time and an
  // old one would repaint for whichever video was on screen by then. Assignment
  // replaces the handler instead of stacking a new one.
  video.onpause = paintPauseButton;
  video.onplay = paintPauseButton;
  video.addEventListener(
    "error",
    () => {
      // Ignore the abort that `stopPreviewVideo` causes when the source is
      // cleared: that fires on the same element and would hide a perfectly
      // good clip that is already loading.
      const current = video.getAttribute("src") || "";
      if (!current || current !== wanted) return;
      // No ffmpeg, or a codec ffmpeg could not read: keep the poster.
      showRenderingIndicator(false);
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
  // The replay button belongs to the still. While the motion is on screen it
  // would sit on top of the footage, inviting a press that does nothing useful,
  // so it fades out for the duration and fades back in with the picture.
  setLiveButtonVisible(false);

  const fadeOut = () => {
    livePlaying = false;
    // A short hold on the last frame before fading. Cutting to the fade the
    // instant the clip ends reads as a truncation rather than a finish.
    setTimeout(() => {
      if (video.dataset.photoId !== String(photo.id)) return;
      video.classList.add("is-fading");
      setLiveButtonVisible(true);
      // Slightly longer than the fade in style.css so the element is never
      // hidden mid-transition.
      setTimeout(() => {
        if (video.dataset.photoId !== String(photo.id)) return;
        video.classList.add("hidden");
        video.classList.remove("is-fading");
      }, 420);
    }, 240);
  };

  video.addEventListener(
    "canplay",
    () => {
      if (video.dataset.photoId !== String(photo.id)) return;
      video.classList.remove("hidden");
      const played = video.play();
      if (played && typeof played.catch === "function") {
        played.catch(() => {
          video.classList.add("hidden");
          setLiveButtonVisible(true);
        });
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
      setLiveButtonVisible(true);
      // Autoplay is allowed to fail quietly (a motion that will not decode
      // should not throw an error at someone who just opened a photo), but a
      // press deserves an answer.
      if (options.loud) showToast("这段实况视频无法播放。", "error");
    },
    { once: true }
  );
}


/**
 * Show or hide the Live Photo replay button, with the fade declared in CSS.
 *
 * Two classes are in play and they mean different things:
 *
 * - ``hidden`` is the layout gate. The button ships with it in index.html so it
 *   never flashes on a photo that turns out not to be a Live Photo, and it used
 *   to be cleared by nobody - which is why the button never appeared at all on
 *   a motionless frame. Showing it has to lift this first; ``display: none``
 *   beats any opacity the CSS would otherwise animate.
 * - ``is-hidden`` is the fade. It is used while the motion plays, so the control
 *   is out of the way but still in the layout, and it fades back in with the
 *   still rather than blinking.
 *
 * Order matters: drop ``hidden`` before touching ``is-hidden`` so the element is
 * laid out (and its transition armed) before the opacity is asked to change.
 */
function setLiveButtonVisible(visible) {
  const btn = $("previewLiveBtn");
  if (!btn) return;
  if (visible) {
    btn.classList.remove("hidden");
    btn.classList.remove("is-hidden");
    return;
  }
  btn.classList.add("is-hidden");
}

/** Put the button away entirely, for photos that have no motion at all. */
function hideLiveButton() {
  const btn = $("previewLiveBtn");
  if (!btn) return;
  btn.classList.add("is-hidden");
  btn.classList.add("hidden");
}


/**
 * Close a dialog, playing the reverse of its entrance animation first.
 *
 * ``hideModal`` keeps the element in the DOM for the length of the exit, so a
 * dialog that is re-opened mid-animation is handled (see ui-kit.js).
 */
function closeModal(id, onHidden) {
  const modal = $(id);
  const closingPreviewId = id === "previewModal" ? state.currentPreviewId : null;
  let previewContent = null;
  if (id === "previewModal" && modal && !modal.classList.contains("hidden") && !prefersReducedMotion()) {
    previewContent = modal.querySelector(".preview-content");
    const targetCard = findVisiblePhotoCardRect(closingPreviewId);
    if (previewContent && targetCard) {
      const rect = previewContent.getBoundingClientRect();
      if (rect.width > 0 && rect.height > 0) {
        const rawDx = targetCard.left + targetCard.width / 2 - (rect.left + rect.width / 2);
        const rawDy = targetCard.top + targetCard.height / 2 - (rect.top + rect.height / 2);
        const dx = Math.max(-160, Math.min(160, rawDx * 0.34));
        const dy = Math.max(-120, Math.min(120, rawDy * 0.34));
        modal.classList.remove("is-flip-opening");
        modal.classList.add("is-flip-closing");
        previewContent.style.transition = "transform 170ms var(--ease-out), opacity 150ms var(--ease-out)";
        previewContent.style.transform = `translate3d(${dx.toFixed(1)}px, ${dy.toFixed(1)}px, 0) scale(0.92)`;
        previewContent.style.opacity = "0";
      }
    }
  }
  hideModal(modal, () => {
    if (modal) modal.classList.remove("is-flip-opening", "is-flip-closing");
    if (previewContent) {
      previewContent.style.transition = "";
      previewContent.style.transform = "";
      previewContent.style.opacity = "";
    }
    if (typeof onHidden === "function") onHidden();
  });
  // Keep the open-order stack honest however the dialog was dismissed - the ×
  // button, Escape or a code path like "cancelling the folder
  // browser returns to the import sheet".
  const at = state.modalStack.lastIndexOf(id);
  if (at !== -1) state.modalStack.splice(at, 1);
  if (id === "previewModal") {
    // A looping preview would otherwise keep decoding (and playing) behind a
    // closed dialog until something else happened to stop it.
    stopPreviewVideo();
    state.currentPreviewId = null;
    state.previewContext = null;
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
  // Each arrow is hidden at its own end of the list: there is nothing to the
  // left of the first photo, and nothing to the right of the last one. A single
  // "has more than one photo" flag kept both lit at both ends, which is what
  // made pressing 上一张 on the first card jump somewhere.
  $("previewPrevBtn").classList.toggle("hidden", !previewHasNeighbour(-1));
  $("previewNextBtn").classList.toggle("hidden", !previewHasNeighbour(1));
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

    <div class="meta-list-block">
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
        <span class="dimension-section-heading">${ICONS.score} 多维度评分</span>
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
      <div class="meta-item-row is-error" title="解析错误">
        ${ICONS.error}
        <div class="meta-content">${escapeHtml(photo.error)}</div>
      </div>
    ` : ""}
  `;
}

