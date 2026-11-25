/* ==========================================================================
   Lumina Album - Boot and the four sheets
   Runs last: `init` binds every control once, and the sheets (足迹 / 那年今日 /
   相似回忆 / 导出) live here because they are opened from more than one place.
   ========================================================================== */
"use strict";

function init() {
  // Render embedded icons
  renderAllIcons();

  setSelectValue($("statusFilter"), "all");
  $("folderInput").addEventListener("change", () => {
    const val = $("folderInput").value.trim();
    if (val) openAlbumFolder(val);
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
  const statsReportBtn = $("statsReportBtn");
  if (statsReportBtn) {
    statsReportBtn.addEventListener("click", () => openStatsReport(null));
  }
  const statsYearSel = $("statsReportYearSelect");
  if (statsYearSel) {
    statsYearSel.addEventListener("change", () => {
      const y = statsYearSel.value ? Number(statsYearSel.value) : null;
      openStatsReport(y, { keepOpen: true });
    });
  }
  const shortcutsBtn = $("shortcutsBtn");
  if (shortcutsBtn) {
    shortcutsBtn.addEventListener("click", () => {
      const modal = $("shortcutsModal");
      if (modal) {
        showModal(modal);
        pushModal(modal);
      }
    });
  }
  initTheme();
  initDensity();
  initTopbarAutoCollapse();
  $("searchBtn").addEventListener("click", loadPhotos);
  $("resetFiltersBtn").addEventListener("click", resetFilters);
  const filtersToggleBtn = $("filtersToggleBtn");
  const filtersBar = $("filtersBar");
  if (filtersToggleBtn && filtersBar) {
    filtersToggleBtn.addEventListener("click", () => {
      const expanded = filtersBar.classList.toggle("is-expanded");
      filtersToggleBtn.setAttribute("aria-expanded", expanded ? "true" : "false");
    });
  }
  const activeFiltersBar = $("activeFiltersBar");
  if (activeFiltersBar) {
    activeFiltersBar.addEventListener("click", (e) => {
      const chip = e.target.closest("[data-clear-filter]");
      if (!chip) return;
      const key = chip.dataset.clearFilter;
      const map = {
        sort: "sortFilter",
        kind: "kindFilter",
        min_score: "minScoreFilter",
        tag: "tagFilter",
        year: "yearFilter",
      };
      if (key === "search") {
        const input = $("searchInput");
        if (input) input.value = "";
      } else if (map[key]) {
        setSelectValue($(map[key]), FILTER_DEFAULTS[key]);
      }
      updateFilterHighlights();
      loadPhotos();
    });
  }
  if ($("emptyResetFiltersBtn")) {
    $("emptyResetFiltersBtn").addEventListener("click", resetFilters);
  }
  $("loadMoreBtn").addEventListener("click", loadMorePhotos);
  $("cancelScanBtn").addEventListener("click", async () => {
    if (typeof peopleState !== "undefined" && peopleState.homeTracking) {
      try { await api("/api/people/scan/cancel", { method: "POST" }); } catch (e) {}
      return;
    }
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
  $("searchMode").addEventListener("change", () => {
    const mode = $("searchMode").value;
    const input = $("searchInput");
    if (input) {
      if (mode === "keyword") {
        input.placeholder = "按标题、标签、地点或文件名即时匹配（不调用模型）…";
      } else if (mode === "visual") {
        input.placeholder = "描述画面细节，让模型看图寻找（如：逆光的侧影、红衣女孩）…";
      } else {
        input.placeholder = "例如：去年秋天的金黄落叶、西湖落日…";
      }
    }
    loadPhotos();
  });
  const clearSimilarBtn = $("similarSearchClearBtn");
  if (clearSimilarBtn) {
    clearSimilarBtn.addEventListener("click", () => {
      hideSimilarSearchBanner();
      loadPhotos();
    });
  }
  const smartAlbumsBtn = $("smartAlbumsBtn");
  if (smartAlbumsBtn) {
    smartAlbumsBtn.addEventListener("click", openSmartAlbumsModal);
  }
  const createSmartBtn = $("createSmartAlbumBtn");
  if (createSmartBtn) {
    createSmartBtn.addEventListener("click", () => saveCurrentSmartAlbum());
  }
  const smartNameInput = $("smartAlbumNameInput");
  if (smartNameInput) {
    smartNameInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") saveCurrentSmartAlbum();
    });
  }
  if (typeof loadSmartAlbums === "function") loadSmartAlbums();

  $("deleteSelectedBtn").addEventListener("click", () => deletePhotos([...state.selected]));
  $("restoreSelectedBtn").addEventListener("click", () => restorePhotos([...state.selected]));
  $("reanalyzeSelectedBtn").addEventListener("click", () => reanalyzeSelected([...state.selected]));
  const compareSelectedBtn = $("compareSelectedBtn");
  if (compareSelectedBtn) {
    compareSelectedBtn.addEventListener("click", () => {
      const picked = [...state.selected].map((id) => photoById(id)).filter(Boolean);
      openComparePhotos(picked);
    });
  }
  const exportOrigBtn = $("exportOriginalsSelectedBtn");
  if (exportOrigBtn) {
    exportOrigBtn.addEventListener("click", exportSelectedOriginals);
  }
  const assignPlaceSelectedBtn = $("assignPlaceSelectedBtn");
  if (assignPlaceSelectedBtn) {
    assignPlaceSelectedBtn.addEventListener("click", () => {
      if (typeof assignSelectedPhotosToPlace === "function") assignSelectedPhotosToPlace();
    });
  }
  const exportSelectedBtn = $("exportSelectedBtn");
  if (exportSelectedBtn) {
    exportSelectedBtn.addEventListener("click", () => {
      const ids = [...state.selected];
      if (!ids.length) return;
      openExportDialog({
        ids,
        subject: `选中的 ${ids.length} 张照片`,
      });
    });
  }
  $("permanentDeleteSelectedBtn").addEventListener("click", () => permanentDeletePhotos([...state.selected]));
  $("clearSelectionBtn").addEventListener("click", () => {
    state.selected.clear();
    updateSelectionBar();
    renderGallery();
  });
  initGalleryEvents();

  $("previewPrevBtn").addEventListener("click", () => navigatePreview(-1));
  $("previewNextBtn").addEventListener("click", () => navigatePreview(1));
  const previewFilmstrip = $("previewFilmstrip");
  if (previewFilmstrip) {
    previewFilmstrip.addEventListener("click", (e) => {
      const thumb = e.target.closest(".filmstrip-thumb");
      if (!thumb) return;
      const targetId = Number(thumb.dataset.photoId);
      if (Number.isFinite(targetId)) jumpToPreview(targetId);
    });
  }
  const previewSimilarBtn = $("previewSimilarBtn");
  if (previewSimilarBtn) {
    previewSimilarBtn.addEventListener("click", () => findSimilarPhotos(state.currentPreviewId));
  }
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
  $("previewInfo").addEventListener("pointerover", (e) => {
    const pill = e.target.closest(".preview-person-pill");
    if (!pill) return;
    const box = document.querySelector(`#previewFaceBoxes .preview-face-box[data-face-id="${pill.dataset.faceId}"]`);
    if (box) box.classList.add("is-active");
  });
  $("previewInfo").addEventListener("pointerout", (e) => {
    const pill = e.target.closest(".preview-person-pill");
    if (!pill) return;
    if (pill.contains(e.relatedTarget)) return;
    const box = document.querySelector(`#previewFaceBoxes .preview-face-box[data-face-id="${pill.dataset.faceId}"]`);
    if (box) box.classList.remove("is-active");
  });
  $("previewLiveBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    const photo = photoById(state.currentPreviewId);
    if (photo) playLiveMotion(photo, { loud: true });
  });

  // Fullscreen Viewer Controls
  const fsCloseBtn = $("fsCloseBtn");
  if (fsCloseBtn) fsCloseBtn.addEventListener("click", closeFullscreenViewer);
  const fsPrevBtn = $("fsPrevBtn");
  if (fsPrevBtn) fsPrevBtn.addEventListener("click", () => navigateFullscreen(-1));
  const fsNextBtn = $("fsNextBtn");
  if (fsNextBtn) fsNextBtn.addEventListener("click", () => navigateFullscreen(1));
  const fsZoomInBtn = $("fsZoomInBtn");
  if (fsZoomInBtn) fsZoomInBtn.addEventListener("click", () => zoomFullscreen(1.25, undefined, undefined, true));
  const fsZoomOutBtn = $("fsZoomOutBtn");
  if (fsZoomOutBtn) fsZoomOutBtn.addEventListener("click", () => zoomFullscreen(0.8, undefined, undefined, true));
  const fsResetBtn = $("fsResetBtn");
  if (fsResetBtn) fsResetBtn.addEventListener("click", () => resetFullscreenZoom(true));
  const fsInfoBtn = $("fsInfoBtn");
  if (fsInfoBtn) fsInfoBtn.addEventListener("click", toggleFullscreenInfo);
  const fsFavoriteBtn = $("fsFavoriteBtn");
  if (fsFavoriteBtn) fsFavoriteBtn.addEventListener("click", toggleCurrentPreviewFavorite);
  const fsModalEl = $("fullscreenModal");
  if (fsModalEl) {
    fsModalEl.addEventListener("mousemove", resetFsIdleTimer, { passive: true });
    fsModalEl.addEventListener("touchstart", resetFsIdleTimer, { passive: true });
    fsModalEl.addEventListener("click", (e) => {
      if (e.target === fsModalEl || e.target === $("fsImageContainer")) resetFsIdleTimer();
    });
  }
  const fsDeleteBtn = $("fsDeleteBtn");
  if (fsDeleteBtn) {
    fsDeleteBtn.addEventListener("click", () => {
      if (state.currentPreviewId) deletePhotos([state.currentPreviewId]);
    });
  }
  const fsRestoreBtn = $("fsRestoreBtn");
  if (fsRestoreBtn) {
    fsRestoreBtn.addEventListener("click", () => {
      if (state.currentPreviewId) restorePhotos([state.currentPreviewId]);
    });
  }

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
        resetFullscreenZoom(true);
      } else {
        zoomFullscreen(2.0, e.clientX, e.clientY, true);
      }
    });

    // Mobile touch gestures: swipe to navigate (1x), drag to pan (>1x), pinch to zoom
    let touchMode = null;
    let touchStartX = 0;
    let touchStartY = 0;
    let pinchStartDist = 0;
    let pinchStartScale = 1;
    const touchDist = (t1, t2) => Math.hypot(t2.clientX - t1.clientX, t2.clientY - t1.clientY);

    fsContainer.addEventListener("touchstart", (e) => {
      if (e.target.closest("button")) return;
      resetFsIdleTimer();
      if (e.touches.length === 2) {
        touchMode = "pinch";
        pinchStartDist = touchDist(e.touches[0], e.touches[1]) || 1;
        pinchStartScale = state.fullscreen.scale;
      } else if (e.touches.length === 1) {
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
        if (state.fullscreen.scale > 1.05) {
          touchMode = "pan";
          state.fullscreen.isDragging = true;
          state.fullscreen.startX = touchStartX - state.fullscreen.panX;
          state.fullscreen.startY = touchStartY - state.fullscreen.panY;
        } else {
          touchMode = "swipe";
        }
      }
    }, { passive: true });

    fsContainer.addEventListener("touchmove", (e) => {
      if (touchMode === "pinch" && e.touches.length === 2) {
        e.preventDefault();
        const dist = touchDist(e.touches[0], e.touches[1]);
        const targetScale = Math.max(0.5, Math.min(6.0, pinchStartScale * (dist / pinchStartDist)));
        const factor = targetScale / state.fullscreen.scale;
        const midX = (e.touches[0].clientX + e.touches[1].clientX) / 2;
        const midY = (e.touches[0].clientY + e.touches[1].clientY) / 2;
        zoomFullscreen(factor, midX, midY, false);
      } else if (touchMode === "pan" && e.touches.length === 1) {
        e.preventDefault();
        state.fullscreen.panX = e.touches[0].clientX - state.fullscreen.startX;
        state.fullscreen.panY = e.touches[0].clientY - state.fullscreen.startY;
        applyFsTransform(false);
      }
    }, { passive: false });

    fsContainer.addEventListener("touchend", (e) => {
      if (touchMode === "swipe" && e.changedTouches && e.changedTouches.length) {
        const dx = e.changedTouches[0].clientX - touchStartX;
        const dy = e.changedTouches[0].clientY - touchStartY;
        if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy) * 1.4) {
          navigateFullscreen(dx > 0 ? -1 : 1);
        }
      }
      state.fullscreen.isDragging = false;
      touchMode = null;
    }, { passive: true });
  }

  // Lightbox photo stage horizontal touch swipe
  const previewMediaCol = document.querySelector("#previewModal .preview-media-col");
  if (previewMediaCol) {
    let pTouchX = 0;
    let pTouchY = 0;
    previewMediaCol.addEventListener("touchstart", (e) => {
      if (e.touches.length !== 1) return;
      pTouchX = e.touches[0].clientX;
      pTouchY = e.touches[0].clientY;
    }, { passive: true });
    previewMediaCol.addEventListener("touchend", (e) => {
      if (!e.changedTouches || !e.changedTouches.length) return;
      const dx = e.changedTouches[0].clientX - pTouchX;
      const dy = e.changedTouches[0].clientY - pTouchY;
      if (Math.abs(dx) > 48 && Math.abs(dx) > Math.abs(dy) * 1.4) {
        navigatePreview(dx > 0 ? -1 : 1);
      }
    }, { passive: true });
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
      pushModal($("importModal"));
      planImport({ silent: true });
      return;
    }
    const picked = state.currentDirPath;
    closeModal("dirModal");
    openAlbumFolder(picked);
  });

  $("saveSettingsBtn").addEventListener("click", saveSettings);
  const vecStartBtn = $("vectorIndexStartBtn");
  if (vecStartBtn) vecStartBtn.addEventListener("click", startVectorIndex);
  const vecCancelBtn = $("vectorIndexCancelBtn");
  if (vecCancelBtn) vecCancelBtn.addEventListener("click", cancelVectorIndex);
  // Keep the percentage beside each slider in step while it is dragged.
  ["setVideoPreviewVolume", "setLiveVolume"].forEach((id) => {
    const input = $(id);
    if (!input) return;
    input.addEventListener("input", () => {
      const label = $(`${id}Value`);
      if (label) label.textContent = `${input.value}%`;
    });
  });
  if ($("downloadFaceModelsBtn")) $("downloadFaceModelsBtn").addEventListener("click", downloadFaceModels);
  if ($("installFfmpegBtn")) $("installFfmpegBtn").addEventListener("click", installFfmpeg);
  if ($("setMapTileProvider")) $("setMapTileProvider").addEventListener("change", syncMapTileFields);
  if ($("refreshFfmpegBtn")) $("refreshFfmpegBtn").addEventListener("click", paintFfmpegStatus);
  if ($("ffmpegInstallCancelBtn")) $("ffmpegInstallCancelBtn").addEventListener("click", cancelFfmpegInstall);
  if ($("ffmpegInstallCloseBtn")) $("ffmpegInstallCloseBtn").addEventListener("click", closeFfmpegInstallModal);
  if ($("ffmpegInstallXBtn")) $("ffmpegInstallXBtn").addEventListener("click", closeFfmpegInstallModal);
  if ($("refreshFaceStatusBtn")) $("refreshFaceStatusBtn").addEventListener("click", paintPeopleEngineStatus);
  if ($("reclusterPeopleBtn")) $("reclusterPeopleBtn").addEventListener("click", reclusterPeopleFromSettings);
  if ($("copyFaceModelsPathBtn")) $("copyFaceModelsPathBtn").addEventListener("click", copyFaceModelsPath);
  if ($("openFaceModelsDirBtn")) $("openFaceModelsDirBtn").addEventListener("click", openFaceModelsDir);
  if ($("modelDownloadCancelBtn")) $("modelDownloadCancelBtn").addEventListener("click", cancelModelDownload);
  if ($("modelDownloadCloseBtn")) $("modelDownloadCloseBtn").addEventListener("click", closeModelDownloadModal);
  if ($("modelDownloadXBtn")) $("modelDownloadXBtn").addEventListener("click", closeModelDownloadModal);
  $("cleanCacheBtn").addEventListener("click", cleanCache);
  $("hideFolderBtn").addEventListener("click", hideCurrentFolder);
  $("viewHiddenFoldersBtn").addEventListener("click", openHiddenFolders);
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
  $("savePromptPresetBtn").addEventListener("click", saveCustomPrompt);
  $("deletePromptPresetBtn").addEventListener("click", deleteCustomPrompt);
  $("promptPresetName").addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    saveCustomPrompt();
  });
  // Typing in the editor drops a built-in highlight; touching the name field
  // turns the next save into a new prompt rather than an update.
  $("setSystemPrompt").addEventListener("input", onPromptTextInput);
  $("promptPresetName").addEventListener("input", onPromptNameInput);
  $("customPromptBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleCustomPromptMenu();
  });
  document.addEventListener("click", (e) => {
    const wrap = $("promptCustomMenuWrap");
    if (wrap && !wrap.contains(e.target)) closeCustomPromptMenu();
  });

  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (state.importPickPending && btn.dataset.close === "dirModal") {
        // Cancelling the folder browser returns to the import dialog.
        state.importPickPending = false;
        closeModal("dirModal");
        showModal($("importModal"));
        pushModal($("importModal"));
        return;
      }
      closeModal(btn.dataset.close);
    });
  });

  // Escape closes the topmost dialog, whichever it is.
  //
  // The keyboard handler further down only knows about the fullscreen viewer and
  // the photo preview, because those were the only two dialogs when it was
  // written. Every other sheet - settings, import, the folder browser and the
  // four newer ones - could only be dismissed by aiming at the × button, and two
  // of them could be opened at once on top of each other.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    // The preview and the viewer handle their own Escape (arrow keys and all);
    // this must not steal the key from them.
    const fullscreen = $("fullscreenModal");
    if (fullscreen && !fullscreen.classList.contains("hidden")) return;
    const preview = $("previewModal");
    if (preview && !preview.classList.contains("hidden")) return;
    const confirm = $("confirmModal");
    if (confirm && !confirm.classList.contains("hidden")) return;

    // Topmost means "most recently opened", not "last in the document": the
    // folder browser is opened over the import sheet while sitting *before* it in
    // the markup, so picking by DOM order closed the wrong one.
    const topmost = state.modalStack[state.modalStack.length - 1];
    if (!topmost) return;
    e.preventDefault();
    closeModal(topmost);
  });
  // A dialog is closed only by its own close/confirm/cancel controls. Clicking
  // the dimmed area around it must not discard a half-written edit or a
  // settings change by accident; there is intentionally no backdrop handler.
  document.addEventListener("click", (e) => {
    if (
      !e.target.closest("#folderHistoryBtn") &&
      !e.target.closest("#folderHistory")
    ) {
      closeFolderHistory();
    }
  });

  // Keyboard Shortcuts: Global (?, /, 1/2/3) + Lightbox (←/→/J/K/F/D/E/Space/Esc)
  document.addEventListener("keydown", (e) => {
    const fsModal = $("fullscreenModal");
    if (fsModal && !fsModal.classList.contains("hidden")) {
      if (e.key === "Escape" || e.code === "Space") {
        e.preventDefault();
        closeFullscreenViewer();
      } else if (e.key === "ArrowLeft" || e.key === "k" || e.key === "K") {
        e.preventDefault();
        navigateFullscreen(-1);
      } else if (e.key === "ArrowRight" || e.key === "j" || e.key === "J") {
        e.preventDefault();
        navigateFullscreen(1);
      } else if (e.key === "i" || e.key === "I") {
        e.preventDefault();
        toggleFullscreenInfo();
      } else if (e.key === "f" || e.key === "F" || e.key === "p" || e.key === "P") {
        e.preventDefault();
        resetFsIdleTimer();
        toggleCurrentPreviewFavorite();
      } else if (e.key === "d" || e.key === "D" || e.key === "x" || e.key === "X") {
        e.preventDefault();
        const photo = photoById(state.currentPreviewId);
        if (photo && photo.status !== "deleted") deletePhotos([photo.id]);
      } else if (e.key === "e" || e.key === "E") {
        e.preventDefault();
        closeFullscreenViewer();
        openEditPreview();
      } else if (e.key === "+" || e.key === "=") {
        zoomFullscreen(1.25, undefined, undefined, true);
      } else if (e.key === "-") {
        zoomFullscreen(0.8, undefined, undefined, true);
      } else if (e.key === "0") {
        resetFullscreenZoom(true);
      }
      return;
    }

    const tag = (e.target && e.target.tagName || "").toLowerCase();
    if (["input", "textarea", "select"].includes(tag) || (e.target && e.target.isContentEditable)) {
      return;
    }

    // Toggle shortcuts cheat-sheet with `?`
    if (e.key === "?" && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      const modal = $("shortcutsModal");
      if (modal && !modal.classList.contains("hidden")) {
        closeModal("shortcutsModal");
      } else if (modal) {
        showModal(modal);
        pushModal(modal);
      }
      return;
    }

    const previewOpen = !$("previewModal").classList.contains("hidden");
    if (previewOpen) {
      if (e.key === "ArrowLeft" || e.key === "k" || e.key === "K") {
        e.preventDefault();
        navigatePreview(-1);
      } else if (e.key === "ArrowRight" || e.key === "j" || e.key === "J") {
        e.preventDefault();
        navigatePreview(1);
      } else if (e.key === "Escape") {
        closeModal("previewModal");
      } else if (e.key === "f" || e.key === "F" || e.key === "p" || e.key === "P") {
        e.preventDefault();
        toggleCurrentPreviewFavorite();
      } else if (e.key === "e" || e.key === "E") {
        e.preventDefault();
        openEditPreview();
      } else if (e.key === "d" || e.key === "D" || e.key === "x" || e.key === "X") {
        e.preventDefault();
        const photo = photoById(state.currentPreviewId);
        if (photo && photo.status !== "deleted") deletePhotos([photo.id]);
      } else if (e.code === "Space") {
        e.preventDefault();
        openFullscreenViewer();
      }
      return;
    }

    if (isOverlayOpen() || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "/") {
      const searchInput = $("searchInput");
      if (searchInput && state.view === "album") {
        e.preventDefault();
        searchInput.focus();
        searchInput.select();
      }
    } else if (state.view === "album" && (e.key === "1" || e.key === "2" || e.key === "3")) {
      const map = { "1": "comfortable", "2": "compact", "3": "wall" };
      if (typeof applyDensity === "function") {
        applyDensity(map[e.key], { persist: true, notify: true });
      }
    }
  });

  document.querySelectorAll("select").forEach(enhanceSelect);
  updateFilterHighlights();
  updateTrashActions();
  document.querySelectorAll(".clickable-stat").forEach((el) => {
    el.addEventListener("click", () => applyStatFilter(el.dataset.filter));
  });

  // Awaited, because the very first view request must already carry the album.
  // It used to run un-awaited in parallel with `initViews()`, so a deep link to
  // #memories asked the server for "folder = (empty)" - which means every album -
  // and the reader saw a library they had not chosen.
  //
  // The hidden list is read first: the boot album must never be a hidden one,
  // and `selectBestFolder` needs to know which folders to leave out.
  const albumReady = loadHiddenFolders().then(() => loadInitialFolder());
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
  initTimelineScrubber();
  // The views are wired only once the album is known, so no view can ask for
  // "all albums" by accident.
  albumReady.finally(() => {
    initViews();
  });
  initPeople();
  initFootprints();
  initMemories();
  initExport();
  initDuplicates();
}

/* ==========================================================================
   Right-Edge Timeline Scrubber & Gallery Marquee Drag-Selection
   ========================================================================== */

const timelineState = {
  buckets: [],
  dragging: false,
};

function updateTimelineScrubber() {
  const scrubber = $("timelineScrubber");
  const ticksEl = $("timelineTicks");
  if (!scrubber || !ticksEl) return;
  const sortVal = $("sortFilter") ? $("sortFilter").value : "";
  const isTimeSort = sortVal === "time_desc" || sortVal === "time_asc";
  if (state.view !== "album" || !isTimeSort || state.photos.length < 16) {
    scrubber.classList.add("hidden");
    timelineState.buckets = [];
    return;
  }

  const buckets = [];
  let lastKey = "";
  for (let i = 0; i < state.photos.length; i += 1) {
    const dot = cardDateText(state.photos[i]);
    if (!dot) continue;
    const [y, m] = dot.split(".");
    const key = `${y}.${m}`;
    if (key !== lastKey) {
      buckets.push({ key, label: `${y} 年 ${Number(m)} 月`, year: y, id: state.photos[i].id, index: i });
      lastKey = key;
    }
  }
  if (buckets.length < 2) {
    scrubber.classList.add("hidden");
    timelineState.buckets = [];
    return;
  }

  timelineState.buckets = buckets;
  const seenYears = new Set();
  const maxTicks = Math.min(buckets.length, 24);
  const step = Math.max(1, Math.floor(buckets.length / maxTicks));
  let html = "";
  for (let i = 0; i < buckets.length; i += step) {
    const b = buckets[i];
    const isNewYear = !seenYears.has(b.year);
    if (isNewYear) seenYears.add(b.year);
    html += `<span class="timeline-tick${isNewYear ? " is-year" : ""}" data-idx="${i}">${isNewYear ? `<i>${b.year}</i>` : ""}</span>`;
  }
  ticksEl.innerHTML = html;
  scrubber.classList.remove("hidden");
}

function initTimelineScrubber() {
  const scrubber = $("timelineScrubber");
  const rail = $("timelineRail");
  const pill = $("timelinePill");
  if (!scrubber || !rail || !pill) return;

  rail.setAttribute("tabindex", "0");
  rail.setAttribute("role", "slider");
  rail.setAttribute("aria-label", "时间轴快速定位");
  timelineState.activeIdx = 0;

  const bucketAtClientY = (clientY) => {
    const rect = rail.getBoundingClientRect();
    if (!rect.height || !timelineState.buckets.length) return null;
    const ratio = Math.max(0, Math.min(1, (clientY - rect.top) / rect.height));
    const idx = Math.min(timelineState.buckets.length - 1, Math.floor(ratio * timelineState.buckets.length));
    return { bucket: timelineState.buckets[idx], idx, topPx: ratio * rect.height };
  };

  const jumpToIndex = (idx, topPx) => {
    if (!timelineState.buckets.length) return;
    const clamped = Math.max(0, Math.min(timelineState.buckets.length - 1, idx));
    const bucket = timelineState.buckets[clamped];
    if (!bucket) return;
    timelineState.activeIdx = clamped;
    const rect = rail.getBoundingClientRect();
    const yPx = topPx !== undefined
      ? topPx
      : (rect.height * (timelineState.buckets.length > 1 ? clamped / (timelineState.buckets.length - 1) : 0));
    pill.textContent = bucket.label;
    pill.style.top = `${yPx}px`;
    pill.classList.remove("hidden");
    const card = document.querySelector(`#gallery .polaroid-card[data-id="${bucket.id}"]`);
    if (card) {
      card.scrollIntoView({ behavior: "auto", block: "center" });
      if (typeof scheduleCardSweep === "function") scheduleCardSweep();
    }
  };

  const jumpTo = (clientY) => {
    const hit = bucketAtClientY(clientY);
    if (!hit) return;
    jumpToIndex(hit.idx, hit.topPx);
  };

  rail.addEventListener("pointermove", (e) => {
    const hit = bucketAtClientY(e.clientY);
    if (!hit) return;
    pill.textContent = hit.bucket.label;
    pill.style.top = `${hit.topPx}px`;
    pill.classList.remove("hidden");
    if (timelineState.dragging) jumpToIndex(hit.idx, hit.topPx);
  });

  rail.addEventListener("pointerdown", (e) => {
    const tick = e.target.closest(".timeline-tick");
    if (tick && tick.dataset.idx !== undefined) {
      const idx = Number(tick.dataset.idx);
      if (Number.isFinite(idx)) {
        jumpToIndex(idx);
        return;
      }
    }
    timelineState.dragging = true;
    if (rail.setPointerCapture) rail.setPointerCapture(e.pointerId);
    jumpTo(e.clientY);
  });

  rail.addEventListener("pointerup", () => {
    timelineState.dragging = false;
  });

  rail.addEventListener("pointerleave", () => {
    if (!timelineState.dragging) pill.classList.add("hidden");
  });

  rail.addEventListener("keydown", (e) => {
    if (!timelineState.buckets.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowRight") {
      e.preventDefault();
      jumpToIndex(timelineState.activeIdx + 1);
    } else if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
      e.preventDefault();
      jumpToIndex(timelineState.activeIdx - 1);
    } else if (e.key === "Home") {
      e.preventDefault();
      jumpToIndex(0);
    } else if (e.key === "End") {
      e.preventDefault();
      jumpToIndex(timelineState.buckets.length - 1);
    }
  });

  rail.addEventListener("blur", () => {
    if (!timelineState.dragging) pill.classList.add("hidden");
  });
}

function initMarqueeSelection(gallery) {
  const box = $("marqueeBox");
  if (!gallery || !box) return;
  let active = false;
  let startPageX = 0;
  let startPageY = 0;
  let curClientX = 0;
  let curClientY = 0;
  let additive = false;
  let baseSelected = null;
  let scrollRaf = null;

  const updateMarquee = () => {
    const startClientX = startPageX - window.scrollX;
    const startClientY = startPageY - window.scrollY;
    const left = Math.min(startClientX, curClientX);
    const top = Math.min(startClientY, curClientY);
    const width = Math.abs(curClientX - startClientX);
    const height = Math.abs(curClientY - startClientY);
    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
    box.style.width = `${width}px`;
    box.style.height = `${height}px`;
    if (width < 8 && height < 8) return;

    const right = left + width;
    const bottom = top + height;
    const next = additive ? new Set(baseSelected) : new Set();
    for (const card of gallery.querySelectorAll(".polaroid-card")) {
      if (card.classList.contains("is-culled")) continue;
      const r = card.getBoundingClientRect();
      const intersects = r.right >= left && r.left <= right && r.bottom >= top && r.top <= bottom;
      const id = Number(card.dataset.id);
      if (intersects && Number.isFinite(id)) next.add(id);
    }
    state.selected = next;
    for (const card of gallery.querySelectorAll(".polaroid-card")) {
      const id = Number(card.dataset.id);
      const cb = card.querySelector(".card-check input");
      if (cb) cb.checked = state.selected.has(id);
    }
    updateSelectionBar();
  };

  const autoScrollStep = () => {
    if (!active) {
      scrollRaf = null;
      return;
    }
    const edge = 52;
    const vh = window.innerHeight;
    let dy = 0;
    if (curClientY < edge) {
      dy = -Math.ceil(((edge - curClientY) / edge) * 18);
    } else if (curClientY > vh - edge) {
      dy = Math.ceil(((curClientY - (vh - edge)) / edge) * 18);
    }
    if (dy !== 0) {
      window.scrollBy(0, dy);
      updateMarquee();
      scrollRaf = requestAnimationFrame(autoScrollStep);
    } else {
      scrollRaf = null;
    }
  };

  gallery.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.target !== gallery) return;
    active = true;
    startPageX = e.clientX + window.scrollX;
    startPageY = e.clientY + window.scrollY;
    curClientX = e.clientX;
    curClientY = e.clientY;
    additive = e.shiftKey || e.metaKey || e.ctrlKey;
    baseSelected = new Set(state.selected);
    box.style.left = `${curClientX}px`;
    box.style.top = `${curClientY}px`;
    box.style.width = "0px";
    box.style.height = "0px";
    box.classList.remove("hidden");
    e.preventDefault();
  });

  window.addEventListener("mousemove", (e) => {
    if (!active) return;
    curClientX = e.clientX;
    curClientY = e.clientY;
    updateMarquee();
    if (!scrollRaf) {
      const edge = 52;
      if (curClientY < edge || curClientY > window.innerHeight - edge) {
        scrollRaf = requestAnimationFrame(autoScrollStep);
      }
    }
  });

  window.addEventListener("mouseup", () => {
    if (!active) return;
    active = false;
    if (scrollRaf) {
      cancelAnimationFrame(scrollRaf);
      scrollRaf = null;
    }
    box.classList.add("hidden");
  });
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

/* Footprints & Custom Places now live in static/js/footprints.js */

/* ==========================================================================
   那年今日

   Photos taken on this calendar day in earlier years, grouped by year so the
   page can say "一年前的今天" instead of presenting forty photos as one pile.
   The window is a setting (0 = the exact day) because a trip rarely lands on the
   same date every year.
   ========================================================================== */

/** "去年" / "两年前" / "五年前的今天" - a year count reads as a date, not a number. */
function yearsAgoText(years) {
  if (years <= 0) return "今年的今天";
  if (years === 1) return "一年前的今天";
  if (years === 2) return "两年前的今天";
  return `${years} 年前的今天`;
}

/**
 * One memory thumbnail.
 *
 * ``analysed`` puts a small mark on photos the model has already read, and shows
 * its poetic caption/title on the Polaroid frame underneath.
 */
function memoryCardHtml(photo) {
  // The album's own card, so a memory looks like the photo it is.
  //
  // No "already analysed" badge: in this view nearly every card has been read, so the
  // mark was on almost all of them and distinguished nothing. The caption, the
  // location and the tags are what tell one memory from another.
  return memoryPolaroidHtml(photo);
}

function memoryYearHtml(bucket, index) {
  return `
    <section class="memory-year" style="animation-delay:${Math.min(index, 10) * 0.05}s">
      <header class="memory-year-head">
        <span class="memory-year-stamp" aria-hidden="true">${escapeHtml(String(bucket.year))}</span>
        <span class="memory-year-title">${yearsAgoText(bucket.years_ago)}</span>
        <span class="memory-year-meta">${bucket.year} · ${bucket.count} 张</span>
      </header>
      <div class="memory-strip">
        ${bucket.photos.map(memoryCardHtml).join("")}
      </div>
    </section>`;
}

function renderMemories(data) {
  const body = $("memoriesBody");
  if (!body) return;
  if (!data.years.length) {
    body.innerHTML = `
      <div class="memories-empty">
        <span class="memories-empty-icon" data-icon="sun"></span>
        <p>往年的这一天，还没有留下什么。</p>
        <p class="memories-empty-hint">在设置里把「回忆的时间范围」放宽一些，也许能找到邻近日子的照片。</p>
      </div>`;
    renderAllIcons(body);
    return;
  }
  state.memoriesData = data;
  body.innerHTML = data.years.map(memoryYearHtml).join("");
  renderAllIcons(body);
  // Register the new pictures with the lazy loader here, not at the call site:
  // re-entering the view re-runs this function and replaces the markup, and a caller
  // that forgot the second step left every picture at `data-src` and `opacity: 0`.
  watchPolaroids(body);
}

/**
 * Ask which format to export in, then hand off to `exportKeepsake`.
 *
 * A dialog rather than a dropdown: there are exactly two answers, they lead to
 * visibly different files, and each one deserves a sentence explaining when to
 * pick it. `options` carries whatever the server needs to rebuild the same set -
 * a list of ids, or a date and window.
 */
function openExportDialog(options) {
  state.exportRequest = options;
  const lede = $("exportLede");
  if (lede) {
    // ``subject`` is a whole clause written by the caller ("选中的 3 张照片",
    // "10 月 2 日的回忆") rather than a fragment spliced into a template - the
    // template read as "把这次的 3 张收进…" for one entry and repeated 的 for the
    // other, because no single sentence frame fits both.
    //
    // A subject starting with a digit picks up a thin space after 把, which is
    // where Chinese typography wants one; one starting with a character does not.
    const subject = options.subject || "这次选中的回忆";
    const joiner = /^[0-9]/.test(subject) ? " " : "";
    lede.textContent = `把${joiner}${subject}收进一份可以带走的回忆里。`;
  }
  showSheet($("exportModal"));
}

function initExport() {
  const htmlBtn = $("exportHtmlBtn");
  const pngBtn = $("exportPngBtn");
  const run = (format, button) => {
    const request = state.exportRequest;
    if (!request) return;
    closeModal("exportModal");
    exportKeepsake({
      format,
      button: null,
      payload: {
        ...request,
        format,
        title: request.title || "拾光回忆",
      },
    });
  };
  if (htmlBtn) htmlBtn.addEventListener("click", () => run("html", htmlBtn));
  if (pngBtn) pngBtn.addEventListener("click", () => run("png", pngBtn));
}

/**
 * Download a keepsake of the given photos.
 *
 * The response is a file, not JSON, so this goes through `fetch` directly rather
 * than `api()`: it has to be read as a blob and handed to a temporary anchor. The
 * filename comes from `Content-Disposition` so the server stays the one place that
 * decides how an export is named.
 */
async function exportKeepsake(options) {
  const button = options.button;
  const label = options.format === "png" ? "长图" : "网页";
  if (button) {
    button.disabled = true;
    button.classList.add("loading");
  }
  showLoading(`正在生成${label}回忆，照片较多时请稍候…`);
  try {
    const exportPayload = Object.assign(
      { theme: (state && state.theme) || "paper" },
      options.payload || {}
    );
    const resp = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(exportPayload),
    });
    if (!resp.ok) {
      let detail = "";
      try {
        detail = (await resp.json()).error || "";
      } catch (e) {
        detail = resp.statusText;
      }
      throw new Error(detail || `HTTP ${resp.status}`);
    }
    const blob = await resp.blob();
    const disposition = resp.headers.get("Content-Disposition") || "";
    const match = /filename="([^"]+)"/.exec(disposition);
    const filename = match ? match[1] : `lumina.${options.format === "png" ? "png" : "html"}`;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
    showToast(`${label}已保存：${filename}`, "success");
    const requested = Number(resp.headers.get("X-Export-Requested") || 0);
    const included = Number(resp.headers.get("X-Export-Included") || requested);
    const limit = Number(resp.headers.get("X-Export-Limit") || included);
    if (requested > included) {
      showToast(
        `这次选了 ${requested} 张，单份回忆最多收 ${limit} 张，已收录前 ${included} 张。`,
        "warning"
      );
    }
  } catch (e) {
    showToast(`导出失败：${e.message}`, "error");
  } finally {
    hideLoading();
    if (button) {
      button.disabled = false;
      button.classList.remove("loading");
    }
  }
}

async function openMemories() {
  showView("memories");
  const body = $("memoriesBody");
  const lede = $("memoriesLede");
  const actions = $("memoriesActions");
  if (actions) actions.hidden = true;
  if (body) body.innerHTML = '<div class="memories-empty">正在翻找旧时光…</div>';
  try {
    const params = new URLSearchParams();
    if (state.folder) params.set("folder", state.folder);
    const data = await api(`/api/memories?${params.toString()}`);
    if (lede) {
      lede.textContent = data.total
        ? `今天这个日子，你在过去留下了 ${data.total} 张回忆。`
        : "今天这个日子，过去还没有留下回忆。";
    }
    renderMemories(data);
    if (actions) actions.hidden = !data.total;
    // Remembered so the export buttons can reproduce exactly this view.
    state.memoriesQuery = data.total
      ? {
          month: data.month,
          day: data.day,
          window: data.window,
          folder: state.folder || undefined,
          subtitle: `${data.month} 月 ${data.day} 日`,
        }
      : null;
  } catch (e) {
    if (body) {
      body.innerHTML = `<div class="memories-empty">翻找失败：${escapeHtml(e.message)}</div>`;
    }
    if (lede) lede.textContent = "";
  }
}

/**
 * The ids in 那年今日 that the model has not read yet.
 *
 * A photo counts as read when it has both a status of ``analyzed`` and a score -
 * the same test the gallery uses, so the two views agree about what "已收录" means.
 */
function memoryIdsNeedingAnalysis() {
  const data = state.memoriesData;
  if (!data || !Array.isArray(data.years)) return [];
  const ids = [];
  for (const bucket of data.years) {
    for (const photo of bucket.photos || []) {
      if (photo.status !== "analyzed" || photo.score === null) ids.push(photo.id);
    }
  }
  return ids;
}

function initMemories() {
  const body = $("memoriesBody");
  if (body) {
    // A thumbnail opens that photo in the detail dialog, on top of this sheet -
    // which is what the reader wants: look, then come back to the year strip.
    body.addEventListener("click", (e) => {
      const card = e.target.closest(".polaroid-card");
      if (!card) return;
      const id = Number(card.dataset.id);
      if (!Number.isFinite(id)) return;
      // Walk this day's photos, not the gallery's - the sheet shows a set the
      // album page may not have loaded.
      const day = state.memoriesData;
      adoptPhotos(day ? day.years.flatMap((bucket) => bucket.photos) : []);
      openPreview(id, {
        ids: day
          ? day.years
              .slice()
              .reverse()
              .flatMap((bucket) => bucket.photos.map((row) => row.id))
          : [id],
        title: "那年今日",
      });
    });
  }

  const analyzeBtn = $("memoriesAnalyzeBtn");
  if (analyzeBtn) {
    analyzeBtn.addEventListener("click", async () => {
      const ids = memoryIdsNeedingAnalysis();
      if (!ids.length) {
        showToast("这一天的照片都已经解读过了。", "info");
        return;
      }
      const confirmed = await confirmDialog(
        `把这一天还没解读的 ${ids.length} 张照片交给大模型？\n任务在后台进行，可以随时取消。`,
        { title: "一键整理", confirmText: "开始整理" }
      );
      if (!confirmed) return;
      try {
        const data = await api("/api/analyze/batch", {
          method: "POST",
          body: JSON.stringify({
            ids,
            folder: state.folder || "",
            subtitle: "那年今日",
          }),
        });
        state.currentJobId = data.job_id;
        showProgress(`正在解读这一天的 ${data.total} 张回忆…`);
        pollScan();
        // The marks on the thumbnails come from the fresh payload once the job
        // reports back, so the sheet is left alone until then.
      } catch (e) {
        showToast("整理失败：" + e.message, "error");
      }
    });
  }

  const exportHtmlBtn = $("memoriesExportHtmlBtn");
  if (exportHtmlBtn) {
    exportHtmlBtn.addEventListener("click", () => {
      if (!state.memoriesQuery) return;
      openExportDialog({
        ...state.memoriesQuery,
        title: "那年今日",
        subject: `${state.memoriesQuery.month} 月 ${state.memoriesQuery.day} 日的回忆`,
      });
    });
  }
}

/* ==========================================================================
   Duplicate review

   A sheet of groups, each showing the copies of one picture. Picking which to
   discard is a visual judgement, so the group is the unit: every copy is on
   screen at a reachable size, the recommended keeper is marked, and ticking a
   card queues it for the bin. Nothing is deleted here - the selection goes
   through the same 收起 path as the gallery, so it lands in the bin and can be
   restored.
   ========================================================================== */

//: Which photos the reviewer has ticked for removal, by group index then id.
const dupState = {
  groups: [],
  // id -> true. A Set of ids rather than per-group sets: the request only needs
  // ids, and a photo can only be in one group anyway.
  chosen: new Set(),
  threshold: 6,
  loading: false,
};

/** The slider's numbers, in the order the slider offers them. */
const DUP_THRESHOLD_LABELS = [
  [0, "几乎相同"],
  [3, "很接近"],
  [6, "宽松"],
  [9, "很宽松"],
  [12, "只看构图"],
];

function dupThresholdLabel(value) {
  let label = DUP_THRESHOLD_LABELS[0][1];
  for (const [at, text] of DUP_THRESHOLD_LABELS) {
    if (value >= at) label = text;
  }
  return label;
}

/** "1.2 MB" for the space a group would free, or "" when it frees nothing. */
function dupWastedText(bytes) {
  return bytes > 0 ? `可省出 ${fmtSize(bytes)}` : "";
}

function dupCardHtml(photo, keepId) {
  const isKeeper = photo.id === keepId;
  const chosen = dupState.chosen.has(photo.id);
  const pixels = photo.width && photo.height ? `${photo.width} × ${photo.height}` : "";
  // A div, not a label: the picture has to open the photo for comparison, and a
  // label would tick the checkbox instead - exactly the mis-click reported.
  return `
    <div class="dup-card${isKeeper ? " is-keeper" : ""}${chosen ? " is-chosen" : ""}"
         data-id="${photo.id}">
      <input type="checkbox" class="dup-check" ${chosen ? "checked" : ""}
             title="选中后会移到回收站" aria-label="收起这张" />
      <button class="dup-shot" type="button" data-open="${photo.id}"
              title="查看这张照片">
        <img class="dup-img" src="${thumbnailUrl(photo)}" alt="${escapeHtml(photo.filename)}"
             loading="lazy" decoding="async" />
        ${isKeeper ? '<span class="dup-keep-badge">建议保留</span>' : ""}
      </button>
      <span class="dup-meta">
        <span class="dup-name" title="${escapeHtml(photo.filename)}">${escapeHtml(photo.filename)}</span>
        <span class="dup-spec">${pixels ? `${pixels} · ` : ""}${fmtSize(photo.size)}</span>
      </span>
    </div>`;
}

function dupGroupHtml(group, index) {
  const wasted = dupWastedText(group.wasted_bytes);
  // A group where everything is ticked would remove the picture from the album
  // altogether. That is allowed - the reader may genuinely want all of them gone -
  // but it must not happen by accident, so the header says so loudly.
  const allChosen = group.photos.every((photo) => dupState.chosen.has(photo.id));
  return `
    <section class="dup-group${allChosen ? " is-emptied" : ""}" data-group="${index}"
             style="animation-delay:${Math.min(index, 12) * 0.03}s">
      <header class="dup-group-head">
        <span class="dup-group-title">${group.photos.length} 张相似</span>
        ${wasted ? `<span class="dup-group-wasted">${wasted}</span>` : ""}
        ${allChosen ? '<span class="dup-group-warning">整组都会被收起</span>' : ""}
        ${group.photos.length >= 2 ? `<button class="dup-group-compare" type="button" data-compare="${index}" title="叠加拖动分割线对比两张细节">${ICONS.compare || ""}对比细节</button>` : ""}
        <button class="dup-group-keep" type="button" data-keep="${index}">只留建议的那张</button>
      </header>
      <div class="dup-grid">
        ${group.photos.map((p) => dupCardHtml(p, group.keep_id)).join("")}
      </div>
    </section>`;
}

function renderDuplicates() {
  const body = $("duplicatesBody");
  if (!body) return;
  if (!dupState.groups.length) {
    body.innerHTML = `
      <div class="duplicates-empty">
        <span class="duplicates-empty-icon" data-icon="sparkles"></span>
        <p>没有找到看起来相似的回忆。</p>
        <p class="duplicates-empty-hint">把「相似程度」放宽一点，也许能找出同一场景下的连拍。</p>
      </div>`;
    renderAllIcons(body);
    updateDupSelection();
    return;
  }
  body.innerHTML = dupState.groups.map(dupGroupHtml).join("");
  renderAllIcons(body);
  updateDupSelection();
}

/** Groups where every photo is ticked - about to disappear from the album. */
function dupEmptiedGroups() {
  return dupState.groups.filter(
    (group) =>
      group.photos.length > 0 &&
      group.photos.every((photo) => dupState.chosen.has(photo.id))
  );
}

/** Restyle the group headers after a tick, without re-rendering the list. */
function markEmptiedGroups() {
  const body = $("duplicatesBody");
  if (!body) return;
  for (const section of body.querySelectorAll(".dup-group")) {
    const index = Number(section.dataset.group);
    const group = dupState.groups[index];
    if (!group) continue;
    const allChosen = group.photos.every((photo) => dupState.chosen.has(photo.id));
    section.classList.toggle("is-emptied", allChosen);
    const head = section.querySelector(".dup-group-head");
    if (!head) continue;
    let warning = head.querySelector(".dup-group-warning");
    if (allChosen && !warning) {
      warning = document.createElement("span");
      warning.className = "dup-group-warning";
      warning.textContent = "整组都会被收起";
      const keep = head.querySelector(".dup-group-keep");
      head.insertBefore(warning, keep || null);
    } else if (!allChosen && warning) {
      warning.remove();
    }
  }
}

function updateDupSelection() {
  const count = dupState.chosen.size;
  const button = $("dupTrashBtn");
  const summary = $("dupSelectionSummary");
  const footer = $("duplicatesFooter");
  if (button) button.disabled = count === 0;
  if (footer) footer.classList.toggle("has-selection", count > 0);
  if (summary) {
    summary.textContent = count
      ? `已选中 ${count} 张待收起的照片`
      : "勾选要收起的相似照片，原图会移入回收站";
  }
}

/** Load (or reload) the groups for the current threshold. */
async function loadDuplicates(threshold) {
  if (dupState.loading) return;
  dupState.loading = true;
  const body = $("duplicatesBody");
  if (body) {
    body.innerHTML = '<div class="duplicates-empty">正在比对照片…</div>';
  }
  try {
    const params = new URLSearchParams();
    if (state.folder) params.set("folder", state.folder);
    if (threshold !== undefined) params.set("threshold", String(threshold));
    if (dupState.mode && dupState.mode !== "phash") params.set("mode", dupState.mode);
    const data = await api(`/api/duplicates?${params.toString()}`);
    dupState.groups = data.groups || [];
    dupState.threshold = data.threshold;
    // Nothing is pre-ticked: the keeper is a suggestion and the reader decides.
    dupState.chosen.clear();
    const summary = data.summary || {};
    const lede = $("duplicatesLede");
    if (lede) {
      lede.textContent = summary.groups
        ? `找到 ${summary.groups} 组相似的回忆，共 ${summary.photos} 张。收起多余的就够了，原图会先进回收站。`
        : "没有找到看起来相似的照片。";
    }
    renderDuplicates();
  } catch (e) {
    if (body) {
      body.innerHTML = `<div class="duplicates-empty">比对失败：${escapeHtml(e.message)}</div>`;
    }
  } finally {
    dupState.loading = false;
  }
}

/**
 * Tick every copy except the recommended keeper, for one group or for all.
 *
 * Deliberately *not* applied when the sheet opens. Keeping the best copy is a
 * suggestion, not a decision, and pre-ticking dozens of files in a destructive
 * dialog means one stray click discards a whole afternoon's shoot. The reader
 * chooses; the keeper is only marked as the one worth keeping.
 */
function applyKeepOneRule(groupIndex) {
  const targets =
    groupIndex === undefined ? dupState.groups : [dupState.groups[groupIndex]];
  for (const group of targets) {
    if (!group) continue;
    for (const photo of group.photos) {
      if (photo.id !== group.keep_id) dupState.chosen.add(photo.id);
    }
  }
}

/**
 * Open one of the top-level sheets, dismissing any other that is already open.
 *
 * The toolbar buttons are mutually exclusive in the reader's mind - "重复照片"
 * and "那年今日" are two answers to "what do I do next", not two windows to stack -
 * and since `showModal` does not close anything, clicking three of them left three
 * sheets on top of each other with the photo grid unreachable behind them.
 *
 * Deliberately not applied to the nested pairs: the folder browser opens *over*
 * the import sheet and must return to it, so those keep using `showModal`
 * directly.
 */
function showSheet(el) {
  if (!el) return;
  for (const modal of document.querySelectorAll(".modal")) {
    if (modal === el) continue;
    if (modal.classList.contains("hidden")) continue;
    // The confirm dialog is a promise with its own cleanup; closing it from here
    // would settle that promise in a way its caller does not expect.
    if (modal.id === "confirmModal") continue;
    hideModal(modal);
    const at = state.modalStack.lastIndexOf(modal.id);
    if (at !== -1) state.modalStack.splice(at, 1);
  }
  showModal(el);
  pushModal(el);
}

/**
 * Record a dialog as open, for the Escape handler to unwind in the right order.
 *
 * ``showModal`` lives in ui-kit.js and knows nothing about this stack, so the
 * bookkeeping is done by the two helpers every caller already goes through:
 * `showSheet` for the top-level sheets and `pushModal` for the dialogs that are
 * opened directly (the settings sheet, the import sheet, the folder browser, the
 * photo preview).
 */
function pushModal(el) {
  if (!el || !el.id) return;
  const at = state.modalStack.lastIndexOf(el.id);
  if (at !== -1) state.modalStack.splice(at, 1);
  state.modalStack.push(el.id);
}

function openDuplicates() {
  showView("duplicates");
  const slider = $("dupThreshold");
  if (slider) {
    slider.value = String(dupState.threshold);
    $("dupThresholdLabel").textContent = dupThresholdLabel(dupState.threshold);
  }
  loadDuplicates(dupState.threshold);
}

async function trashChosenDuplicates() {
  const ids = [...dupState.chosen];
  if (!ids.length) return;
  // A group where every photo is ticked would lose the picture completely. Say so
  // in the confirmation rather than letting it happen quietly.
  const emptied = dupEmptiedGroups();
  const warning = emptied.length
    ? `\n\n注意：有 ${emptied.length} 组照片会被全部收起，这些画面将不再出现在相册里。`
    : "";
  const confirmed = await confirmDialog(
    `确定把选中的 ${ids.length} 张相似照片收起吗？\n它们会先进回收站，随时可以恢复。${warning}`,
    { title: "收起重复的回忆", confirmText: "收起", danger: true }
  );
  if (!confirmed) return;
  try {
    const data = await api("/api/delete", {
      method: "POST",
      body: JSON.stringify({ ids, sync_card: false }),
    });
    if (data.errors && data.errors.length) {
      showToast("部分照片收起失败：" + data.errors.map((e) => e.error).join("; "), "error");
    }
    showToast(`已收起 ${ids.length} 张重复的回忆`, "success");
    removePhotoRows(ids);
    loadStats();
    // Reload rather than patch the groups: a photo that was trashed leaves its
    // group, and the counts in the header have to follow.
    await loadDuplicates(dupState.threshold);
  } catch (e) {
    showToast("收起失败：" + e.message, "error");
  }
}

function initDuplicates() {
  const body = $("duplicatesBody");
  if (body) {
    // Ticking a box is the only thing that selects. The whole card used to be a
    // <label>, so clicking the picture to compare it ticked the box instead.
    body.addEventListener("change", (e) => {
      const check = e.target.closest(".dup-check");
      if (!check) return;
      const card = check.closest(".dup-card");
      if (!card) return;
      const id = Number(card.dataset.id);
      if (check.checked) dupState.chosen.add(id);
      else dupState.chosen.delete(id);
      card.classList.toggle("is-chosen", check.checked);
      // The group header reports "整组都会被收起", so it has to follow the tick.
      markEmptiedGroups();
      updateDupSelection();
    });

    // The picture opens the photo, so the two can be compared side by side.
    body.addEventListener("click", (e) => {
      const shot = e.target.closest(".dup-shot");
      if (!shot) return;
      const id = Number(shot.dataset.open);
      if (!Number.isFinite(id)) return;
      const group = (dupState.groups || []).find((item) =>
        item.photos.some((photo) => photo.id === id)
      );
      adoptPhotos(group ? group.photos : []);
      openPreview(id, {
        ids: group ? group.photos.map((photo) => photo.id) : [id],
        title: group ? `${group.photos.length} 张相似` : "相似回忆",
      });
    });
  }

  const dupModeSelect = $("dupModeSelect");
  if (dupModeSelect) {
    dupModeSelect.addEventListener("change", () => {
      dupState.mode = dupModeSelect.value || "phash";
      loadDuplicates(dupState.threshold);
    });
  }
  const slider = $("dupThreshold");
  if (slider) {
    // Re-query on release rather than on every pixel: each step re-runs the
    // grouping over the whole library, which is a second of work.
    let pending = null;
    slider.addEventListener("input", () => {
      $("dupThresholdLabel").textContent = dupThresholdLabel(Number(slider.value));
    });
    slider.addEventListener("change", () => {
      const value = Number(slider.value);
      if (pending) clearTimeout(pending);
      pending = setTimeout(() => loadDuplicates(value), 60);
      // Remembered with the rest of the UI state: a reader who settled on 4
      // should not find 6 again next time.
      saveUiState({ dup_threshold: value });
    });
  }

  const selectAll = $("dupSelectAllBtn");
  if (selectAll) {
    selectAll.addEventListener("click", () => {
      applyKeepOneRule();
      renderDuplicates();
    });
  }
  const clear = $("dupClearBtn");
  if (clear) {
    clear.addEventListener("click", () => {
      dupState.chosen.clear();
      renderDuplicates();
    });
  }
  const trash = $("dupTrashBtn");
  if (trash) trash.addEventListener("click", trashChosenDuplicates);

  // The per-group shortcuts live in the header of each group, so they are delegated
  // from the list rather than bound to every group on every render.
  if (body) {
    body.addEventListener("click", (e) => {
      const compareBtn = e.target.closest(".dup-group-compare");
      if (compareBtn) {
        const index = Number(compareBtn.dataset.compare);
        if (Number.isFinite(index)) openCompareModal(index);
        return;
      }
      const button = e.target.closest(".dup-group-keep");
      if (!button) return;
      const index = Number(button.dataset.keep);
      if (!Number.isFinite(index)) return;
      applyKeepOneRule(index);
      renderDuplicates();
    });
  }
  initCompareModal();
}

/* ==========================================================================
   Duplicate Split-Curtain Comparison Modal
   ========================================================================== */

const compareState = {
  photos: [],
  leftId: null,
  rightId: null,
  split: 50,
  savedSplit: null,
  zoom: 1,
  panX: 0,
  panY: 0,
  isPanning: false,
  isDraggingHandle: false,
  panStartX: 0,
  panStartY: 0,
};

function setCompareSplit(pct) {
  const clamped = Math.max(0, Math.min(100, Number(pct) || 0));
  compareState.split = clamped;
  const stage = $("compareStage");
  const slider = $("compareSlider");
  if (stage) stage.style.setProperty("--split", `${clamped}%`);
  if (slider) slider.value = String(Math.round(clamped));
}

function applyCompareTransform(smooth = false) {
  const stage = $("compareStage");
  const leftImg = $("compareLeftImg");
  const rightImg = $("compareRightImg");
  const z = compareState.zoom;
  if (z <= 1) {
    compareState.panX = 0;
    compareState.panY = 0;
  } else if (stage) {
    const maxX = (stage.clientWidth * (z - 1)) / 2;
    const maxY = (stage.clientHeight * (z - 1)) / 2;
    compareState.panX = Math.max(-maxX, Math.min(maxX, compareState.panX));
    compareState.panY = Math.max(-maxY, Math.min(maxY, compareState.panY));
  }
  const tf = z <= 1 ? "" : `translate3d(${compareState.panX.toFixed(1)}px, ${compareState.panY.toFixed(1)}px, 0) scale(${z})`;
  for (const img of [leftImg, rightImg]) {
    if (!img) continue;
    img.classList.toggle("is-smooth", !!smooth && !compareState.isPanning);
    img.style.transform = tf;
  }
  if (stage) {
    stage.classList.toggle("is-zoomed", z > 1);
    stage.classList.toggle("is-panning", compareState.isPanning);
  }
  document.querySelectorAll("#compareZoomGroup .compare-zoom-btn").forEach((btn) => {
    btn.classList.toggle("active", Number(btn.dataset.compareZoom) === z);
  });
}

function setCompareZoom(zoom) {
  const next = [1, 2, 4].includes(Number(zoom)) ? Number(zoom) : 1;
  compareState.zoom = next;
  if (next === 1) {
    compareState.panX = 0;
    compareState.panY = 0;
  }
  applyCompareTransform(true);
}

function paintCompareImages() {
  const left = compareState.photos.find((p) => p.id === compareState.leftId) || compareState.photos[0];
  const right = compareState.photos.find((p) => p.id === compareState.rightId) || compareState.photos[1] || left;
  if (!left || !right) return;
  const leftImg = $("compareLeftImg");
  const rightImg = $("compareRightImg");
  if (leftImg) leftImg.src = `/api/proxy/${left.id}`;
  if (rightImg) rightImg.src = `/api/proxy/${right.id}`;
  const metaText = (p, side) => {
    const dims = p.width && p.height ? `${p.width}×${p.height}` : "";
    return `${side} · ${p.filename}${dims ? ` (${dims})` : ""} · ${fmtSize(p.size)}`;
  };
  const leftMeta = $("compareLeftMeta");
  const rightMeta = $("compareRightMeta");
  if (leftMeta) leftMeta.textContent = metaText(left, "左");
  if (rightMeta) rightMeta.textContent = metaText(right, "右");
  refreshCompareQuality(left.id, right.id);
}

async function refreshCompareQuality(leftId, rightId) {
  const bar = $("compareQualityBar");
  if (!bar || !leftId || !rightId) return;
  bar.textContent = "正在检测锐度与曝光…";
  try {
    const data = await api("/api/cull/quality", {
      method: "POST",
      body: JSON.stringify({ ids: [leftId, rightId] }),
    });
    const res = data.results || {};
    const fmt = (id, label) => {
      const m = res[String(id)];
      if (!m) return `${label}：无数据`;
      const flags = m.flags && m.flags.length ? ` · ${m.flags.join("/")}` : " · 曝光锐度正常";
      return `${label} 锐度 ${m.sharpness}${flags}`;
    };
    bar.textContent = `${fmt(leftId, "左图")}   |   ${fmt(rightId, "右图")}`;
  } catch (_e) {
    bar.textContent = "";
  }
}

function openComparePhotos(photos, keepId = null) {
  if (!Array.isArray(photos) || photos.length < 2) {
    showToast("请至少选择 2 张照片进行对比", "info");
    return;
  }
  compareState.photos = photos;
  const keeper = photos.find((p) => p.id === keepId) || photos[0];
  const other = photos.find((p) => p.id !== keeper.id) || photos[1];
  compareState.leftId = keeper.id;
  compareState.rightId = other.id;
  const leftSel = $("compareLeftSelect");
  const rightSel = $("compareRightSelect");
  const optsHtml = photos
    .map((p) => `<option value="${p.id}">${escapeHtml(p.filename)}${p.id === keepId ? "（建议保留）" : ""}</option>`)
    .join("");
  if (leftSel) {
    leftSel.innerHTML = optsHtml;
    leftSel.value = String(keeper.id);
  }
  if (rightSel) {
    rightSel.innerHTML = optsHtml;
    rightSel.value = String(other.id);
  }
  setCompareSplit(50);
  setCompareZoom(1);
  paintCompareImages();
  showModal($("compareModal"));
  pushModal($("compareModal"));
}

async function exportSelectedOriginals() {
  const ids = [...state.selected];
  if (!ids.length) return;
  const defaultDir = state.folder ? `${state.folder}/selected-export` : "";
  const ans = await confirmDialog(
    `将选中的 ${ids.length} 张照片原图拷贝到目标目录（原文件不动）。`,
    {
      title: "导出原图 / RAW",
      confirmText: "开始导出",
      input: { label: "目标文件夹路径", value: defaultDir, placeholder: "例如：/Users/name/Pictures/Lightroom-Import" },
      checkbox: { label: "同时导出同名 RAW 文件", checked: true },
    }
  );
  if (!ans || !ans.confirmed) return;
  const targetDir = (ans.value || "").trim();
  if (!targetDir) {
    showToast("请填写目标文件夹路径", "warning");
    return;
  }
  showLoading("正在导出原图与 RAW…");
  try {
    const report = await api("/api/cull/export-originals", {
      method: "POST",
      body: JSON.stringify({ ids, target_dir: targetDir, mode: "copy", include_raw: !!ans.checked }),
    });
    const rawTip = report.raw_exported ? `（含 ${report.raw_exported} 个 RAW）` : "";
    showToast(`已导出 ${report.exported} 张原图${rawTip}到 ${report.target_dir}`, "success");
  } catch (e) {
    showToast("导出原图失败：" + e.message, "error");
  } finally {
    hideLoading();
  }
}

function openCompareModal(groupIndex) {
  const group = (dupState.groups || [])[groupIndex];
  if (!group || !group.photos || group.photos.length < 2) return;
  return openComparePhotos(group.photos, group.keep_id);
}

function initCompareModal() {
  const slider = $("compareSlider");
  const stage = $("compareStage");
  const handle = $("compareHandle");
  if (slider) {
    slider.addEventListener("input", () => setCompareSplit(slider.value));
  }
  const zoomGroup = $("compareZoomGroup");
  if (zoomGroup) {
    zoomGroup.addEventListener("click", (e) => {
      const btn = e.target.closest(".compare-zoom-btn");
      if (!btn) return;
      setCompareZoom(Number(btn.dataset.compareZoom));
    });
  }
  if (handle && stage) {
    handle.addEventListener("mousedown", (e) => {
      e.stopPropagation();
      e.preventDefault();
      compareState.isDraggingHandle = true;
    });
  }
  if (stage) {
    stage.addEventListener("mousedown", (e) => {
      if (compareState.zoom <= 1 || e.button !== 0 || e.target.closest("#compareHandle")) return;
      compareState.isPanning = true;
      compareState.panStartX = e.clientX - compareState.panX;
      compareState.panStartY = e.clientY - compareState.panY;
      stage.classList.add("is-panning");
      e.preventDefault();
    });
    stage.addEventListener("dblclick", () => {
      setCompareZoom(compareState.zoom === 1 ? 2 : 1);
    });
  }
  window.addEventListener("mousemove", (e) => {
    if (compareState.isDraggingHandle && stage) {
      const rect = stage.getBoundingClientRect();
      if (rect.width > 0) {
        setCompareSplit(((e.clientX - rect.left) / rect.width) * 100);
      }
      return;
    }
    if (!compareState.isPanning) return;
    compareState.panX = e.clientX - compareState.panStartX;
    compareState.panY = e.clientY - compareState.panStartY;
    applyCompareTransform(false);
  });
  window.addEventListener("mouseup", () => {
    compareState.isDraggingHandle = false;
    if (compareState.isPanning) {
      compareState.isPanning = false;
      if (stage) stage.classList.remove("is-panning");
    }
  });
  const leftSel = $("compareLeftSelect");
  const rightSel = $("compareRightSelect");
  if (leftSel) {
    leftSel.addEventListener("change", () => {
      compareState.leftId = Number(leftSel.value);
      paintCompareImages();
    });
  }
  if (rightSel) {
    rightSel.addEventListener("change", () => {
      compareState.rightId = Number(rightSel.value);
      paintCompareImages();
    });
  }
  document.addEventListener("keydown", (e) => {
    const modal = $("compareModal");
    if (!modal || modal.classList.contains("hidden")) return;
    if (e.code === "Space" && !e.repeat) {
      e.preventDefault();
      compareState.savedSplit = compareState.split;
      setCompareSplit(0);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      setCompareSplit(compareState.split - 5);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      setCompareSplit(compareState.split + 5);
    }
  });
  document.addEventListener("keyup", (e) => {
    if (e.code === "Space" && compareState.savedSplit !== null) {
      setCompareSplit(compareState.savedSplit);
      compareState.savedSplit = null;
    }
  });
}

async function openStatsReport(year = null, options = {}) {
  const modal = $("statsReportModal");
  const body = $("statsReportBody");
  const yearSel = $("statsReportYearSelect");
  if (!modal || !body) return;
  if (!options.keepOpen) {
    showModal(modal);
    pushModal(modal);
  }
  body.innerHTML = '<div class="people-empty">正在汇总拍摄与装备数据…</div>';
  try {
    const params = new URLSearchParams();
    if (state.folder) params.set("folder", state.folder);
    if (year) params.set("year", String(year));
    const data = await api(`/api/stats/report?${params.toString()}`);
    if (yearSel) {
      const years = data.available_years || [];
      yearSel.innerHTML =
        '<option value="">全部年份</option>' +
        years.map((y) => `<option value="${y}">${y} 年</option>`).join("");
      yearSel.value = year ? String(year) : "";
    }
    renderStatsReport(data);
  } catch (e) {
    body.innerHTML = `<div class="people-empty">加载摄影年报失败：${escapeHtml(e.message)}</div>`;
  }
}

function renderStatsReport(data) {
  const body = $("statsReportBody");
  if (!body) return;
  const s = data.summary || {};
  const renderBars = (rows, labelKey, subFn) => {
    if (!rows || !rows.length) return '<div class="smart-albums-empty">暂无数据</div>';
    const maxVal = Math.max(1, ...rows.map((r) => Number(r.count) || 0));
    return `<div class="stats-bar-list">${rows
      .map((r) => {
        const pct = Math.round(((Number(r.count) || 0) / maxVal) * 100);
        return `<div class="stats-bar-row"><div class="stats-bar-meta"><span>${escapeHtml(r[labelKey])}</span><span class="stats-bar-sub">${escapeHtml(subFn(r))}</span></div><div class="stats-bar-track"><div class="stats-bar-fill" style="width:${pct}%"></div></div></div>`;
      })
      .join("")}</div>`;
  };
  const months = data.months || [];
  const maxMonth = Math.max(1, ...months.map((m) => Number(m.count) || 0));
  const monthHtml = months.length
    ? `<div class="stats-month-bars">${months
        .map((m) => {
          const pct = Math.max(4, Math.round(((Number(m.count) || 0) / maxMonth) * 100));
          return `<div class="stats-month-col" title="${escapeHtml(m.label)}：${m.count} 张"><div class="stats-month-bar" style="height:${pct}%"></div><span class="stats-month-lbl">${escapeHtml(m.label.replace(/^\d{4}-/, ""))}</span></div>`;
        })
        .join("")}</div>`
    : '<div class="smart-albums-empty">暂无拍摄时间记录</div>';
  const chips = (rows) =>
    rows && rows.length
      ? `<div class="stats-chip-cloud">${rows
          .map((r) => `<span class="stats-chip">${escapeHtml(r.name)} · ${r.count}</span>`)
          .join("")}</div>`
      : '<div class="smart-albums-empty">暂无数据</div>';
  body.innerHTML = `
    <div class="stats-kpi-grid">
      <div class="stats-kpi-card"><div class="stats-kpi-value">${s.total || 0}</div><div class="stats-kpi-label">总作品（视频 ${s.videos || 0}）</div></div>
      <div class="stats-kpi-card"><div class="stats-kpi-value">${s.avg_score != null ? Number(s.avg_score).toFixed(2) : "-"}</div><div class="stats-kpi-label">平均分（8.5+ 共 ${s.high_scores || 0} 张）</div></div>
      <div class="stats-kpi-card"><div class="stats-kpi-value">${s.favorites || 0}</div><div class="stats-kpi-label">珍藏作品</div></div>
      <div class="stats-kpi-card"><div class="stats-kpi-value">${s.cities_count || 0}</div><div class="stats-kpi-label">到访城市</div></div>
    </div>
    <div class="stats-sections-grid">
      <section class="stats-panel"><h3>拍摄活跃节奏</h3>${monthHtml}</section>
      <section class="stats-panel"><h3>常用焦段分布</h3>${renderBars(data.focal_buckets, "bucket", (r) => `${r.count} 张`)}</section>
      <section class="stats-panel"><h3>常用机身</h3>${renderBars(data.cameras, "name", (r) => `${r.count} 张${r.avg_score != null ? ` · 均分 ${r.avg_score}` : ""}`)}</section>
      <section class="stats-panel"><h3>常用镜头</h3>${renderBars(data.lenses, "name", (r) => `${r.count} 张${r.avg_score != null ? ` · 均分 ${r.avg_score}` : ""}`)}</section>
      <section class="stats-panel"><h3>常去城市</h3>${chips(data.top_cities)}</section>
      <section class="stats-panel"><h3>高频题材标签</h3>${chips(data.top_tags)}</section>
    </div>`;
}

document.addEventListener("DOMContentLoaded", init);
