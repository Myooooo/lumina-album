/* ==========================================================================
   Lumina UI Kit - Clean, Resilient Micro-Interactions & Components
   ========================================================================== */
"use strict";

window.LuminaUI = (() => {
  const getEl = (id) => document.getElementById(id);
  const ICONS = window.LuminaIcons || {};
  const escapeUiHtml = (text) =>
    String(text ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  //: Must match the ``modalPopOut`` / ``modalBackdropOut`` durations in style.css.
  const EXIT_MS = 170;

  const prefersReducedMotion = () =>
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /**
   * Show a modal with its entrance animation.
   *
   * Cancels a pending exit as well: re-opening a dialog within the closing
   * window must not let the earlier timer hide it again a moment later.
   */
  //: Where dynamic dialog layers start. Above every static layer in style.css
  //: except the toast (2000), which must stay above all dialogs.
  const MODAL_LAYER_BASE = 1100;

  //: Layers currently assigned, lowest first. The next dialog goes one above the
  //: highest open one, so opening and closing the same dialog all afternoon
  //: cannot march the z-index past the toast layer (2000) - the old counter only
  //: ever climbed, and resetting it per element let two open dialogs collide.
  const openLayers = [];
  let modalLayerCounter = 0;

  /**
   * Open a dialog, on a layer above whatever is already open.
   *
   * Every `.modal` inherits `z-index: 100` and `#previewModal` overrides it at 400,
   * so a dialog raised *from* the preview - 编辑回忆, 暂时收起 - opened underneath it and
   * its buttons looked broken. Static layers cannot express "whichever opened last",
   * which is the actual rule, so the layer is assigned here.
   */
  function syncModalScrollLock() {
    if (typeof document === "undefined") return;
    const hasOpen = Boolean(
      document.querySelector(".modal:not(.hidden), #fullscreenModal:not(.hidden)")
    );
    document.documentElement.classList.toggle("modal-open", hasOpen);
    if (document.body) document.body.classList.toggle("modal-open", hasOpen);
  }

  function showModal(el) {
    if (!el) return;
    if (el._luminaExitTimer) {
      clearTimeout(el._luminaExitTimer);
      el._luminaExitTimer = null;
    }
    // Re-opening the same dialog (a second confirm on top of the first) must also
    // move it to the front.
    if (el._luminaLayer) releaseLayer(el._luminaLayer);
    modalLayerCounter = openLayers.length ? Math.max(...openLayers) + 1 : 1;
    openLayers.push(modalLayerCounter);
    el.style.zIndex = String(MODAL_LAYER_BASE + modalLayerCounter);
    el._luminaLayer = MODAL_LAYER_BASE + modalLayerCounter;
    el.classList.remove("closing");
    el.classList.remove("hidden");
    syncModalScrollLock();
  }

  function releaseLayer(layer) {
    const index = openLayers.indexOf(layer - MODAL_LAYER_BASE);
    if (index !== -1) openLayers.splice(index, 1);
  }

  /**
   * Hide a modal, playing the reverse of its entrance animation first.
   *
   * ``onHidden`` runs once the dialog is really gone, so callers can chain
   * whatever follows without racing the animation.
   */
  const openSelectClosers = new Set();

  function closeAllSelectMenus(exceptCloser) {
    openSelectClosers.forEach((fn) => {
      if (fn !== exceptCloser) fn(true);
    });
  }

  function hideModal(el, onHidden) {
    closeAllSelectMenus();
    if (!el || el.classList.contains("hidden")) {
      syncModalScrollLock();
      if (typeof onHidden === "function") onHidden();
      return;
    }
    if (el._luminaExitTimer) clearTimeout(el._luminaExitTimer);
    const finish = () => {
      el._luminaExitTimer = null;
      el.classList.remove("closing");
      el.classList.add("hidden");
      // Hand the layer back so the next dialog can reuse it: a counter that only
      // ever climbs would eventually outgrow the toast layer in the stylesheet.
      if (el._luminaLayer) {
        releaseLayer(el._luminaLayer);
        el._luminaLayer = 0;
        el.style.zIndex = "";
      }
      syncModalScrollLock();
      if (typeof onHidden === "function") onHidden();
    };
    if (prefersReducedMotion()) {
      finish();
      return;
    }
    el.classList.add("closing");
    el._luminaExitTimer = setTimeout(finish, EXIT_MS);
  }

  /**
   * Render icons in data-icon containers automatically
   */
  function renderAllIcons(root = document) {
    root.querySelectorAll("[data-icon]").forEach((el) => {
      const name = el.getAttribute("data-icon");
      if (name && ICONS[name]) {
        el.innerHTML = ICONS[name];
      }
    });
    const folderInputIcon = getEl("folderInputIcon");
    if (folderInputIcon && ICONS.folder) folderInputIcon.innerHTML = ICONS.folder;
  }

 /**
   * Custom stylized dropdown wrapper with fluid transitions.
   * Portals to document.body while open so scrollable / overflow-hidden modals
   * (e.g. Settings -> 地点 -> 地图底图源) and backdrop-filter layers never clip it,
   * and flips upward automatically when near the bottom of the viewport.
   */
  function enhanceSelect(select) {
    if (!select || select.dataset.enhanced || select.dataset.noCustom) return;
    select.dataset.enhanced = "1";

    const wrapper = document.createElement("div");
    wrapper.className = "custom-select";
    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(select);

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "custom-select-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    const prefixText = select.dataset.prefix || "";
    const prefixMarkup = prefixText
      ? `<span class="custom-select-prefix">${escapeUiHtml(prefixText)} ·</span>`
      : "";
    trigger.innerHTML = `${prefixMarkup}<span class="custom-select-label"></span><span class="custom-select-arrow"></span>`;
    wrapper.appendChild(trigger);

    const menu = document.createElement("div");
    menu.className = "custom-select-menu";
    menu.setAttribute("role", "listbox");
    wrapper.appendChild(menu);

    let exitTimer = null;

    function updateLabel() {
      const opt = select.options[select.selectedIndex];
      const labelEl = trigger.querySelector(".custom-select-label");
      if (labelEl) labelEl.textContent = opt ? opt.text : "";
    }

    function buildMenu() {
      menu.innerHTML = "";
      [...select.options].forEach((opt) => {
        const item = document.createElement("div");
        item.className = "custom-select-option" + (opt.selected ? " selected" : "");
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", opt.selected ? "true" : "false");
        item.textContent = opt.text;
        item.addEventListener("click", () => {
          select.value = opt.value;
          select.dispatchEvent(new Event("change", { bubbles: true }));
          updateLabel();
          close();
        });
        menu.appendChild(item);
      });
    }

    function positionMenu() {
      const rect = trigger.getBoundingClientRect();
      const vw = window.innerWidth || document.documentElement.clientWidth || 1024;
      const vh = window.innerHeight || document.documentElement.clientHeight || 768;
      const gap = 5;
      const margin = 10;
      const width = Math.max(rect.width, 120);
      const left = Math.max(margin, Math.min(rect.left, vw - width - margin));

      menu.style.position = "fixed";
      menu.style.left = `${Math.round(left)}px`;
      menu.style.right = "auto";
      menu.style.width = `${Math.round(width)}px`;

      // Measure desired height (capped at 240px) before deciding up vs down.
      menu.style.maxHeight = "240px";
      const naturalHeight = Math.min(240, menu.scrollHeight + 12 || 200);
      const spaceBelow = vh - rect.bottom - margin;
      const spaceAbove = rect.top - margin;
      const openUp = spaceBelow < naturalHeight && spaceAbove > spaceBelow;

      if (openUp) {
        menu.classList.add("is-dropup");
        menu.style.top = "auto";
        menu.style.bottom = `${Math.round(vh - rect.top + gap)}px`;
        menu.style.maxHeight = `${Math.min(240, Math.max(110, Math.floor(spaceAbove - gap)))}px`;
      } else {
        menu.classList.remove("is-dropup");
        menu.style.bottom = "auto";
        menu.style.top = `${Math.round(rect.bottom + gap)}px`;
        menu.style.maxHeight = `${Math.min(240, Math.max(110, Math.floor(spaceBelow - gap)))}px`;
      }

      const dialog = wrapper.closest(".modal");
      if (dialog && dialog._luminaLayer) {
        menu.style.zIndex = String(dialog._luminaLayer + 20);
      } else {
        menu.style.zIndex = "1600";
      }
    }

    function onViewportScroll(e) {
      if (e && e.target && (e.target === menu || menu.contains(e.target))) return;
      close(true);
    }

    function onViewportResize() {
      close(true);
    }

    function onKeyDown(e) {
      if (e.key === "Escape") {
        e.stopPropagation();
        close();
        trigger.focus();
      }
    }

    function detachPortal() {
      window.removeEventListener("scroll", onViewportScroll, true);
      window.removeEventListener("resize", onViewportResize);
      document.removeEventListener("keydown", onKeyDown, true);
      openSelectClosers.delete(close);
      menu.classList.remove("is-dropup");
      menu.style.position = "";
      menu.style.top = "";
      menu.style.bottom = "";
      menu.style.left = "";
      menu.style.right = "";
      menu.style.width = "";
      menu.style.maxHeight = "";
      menu.style.zIndex = "";
      if (menu.parentNode !== wrapper) {
        wrapper.appendChild(menu);
      }
    }

    function open() {
      closeAllSelectMenus(close);
      if (exitTimer) {
        clearTimeout(exitTimer);
        exitTimer = null;
      }
      buildMenu();
      if (document.body && menu.parentNode !== document.body) {
        document.body.appendChild(menu);
      }
      menu.classList.remove("closing");
      menu.classList.add("open");
      positionMenu();
      trigger.classList.add("open");
      trigger.setAttribute("aria-expanded", "true");
      openSelectClosers.add(close);
      window.addEventListener("scroll", onViewportScroll, true);
      window.addEventListener("resize", onViewportResize);
      document.addEventListener("keydown", onKeyDown, true);
    }

    function close(immediate = false) {
      if (!menu.classList.contains("open")) return;
      if (exitTimer) {
        clearTimeout(exitTimer);
        exitTimer = null;
      }
      trigger.classList.remove("open");
      trigger.setAttribute("aria-expanded", "false");
      if (immediate || prefersReducedMotion()) {
        menu.classList.remove("open", "closing");
        detachPortal();
        return;
      }
      menu.classList.add("closing");
      exitTimer = setTimeout(() => {
        exitTimer = null;
        menu.classList.remove("open", "closing");
        detachPortal();
      }, EXIT_MS);
    }

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      if (menu.classList.contains("open")) close();
      else open();
    });

    document.addEventListener("click", (e) => {
      if (!wrapper.contains(e.target) && !menu.contains(e.target)) close();
    });

    select.classList.add("native-hidden");
    updateLabel();
  }

  function setSelectValue(select, value) {
    if (!select) return;
    select.value = value;
    if (select.dataset.enhanced) {
      const wrapper = select.parentElement;
      const label = wrapper ? wrapper.querySelector(".custom-select-label") : null;
      const opt = select.options[select.selectedIndex];
      if (label && opt) label.textContent = opt.text;
    }
  }

  /**
   * Warm paper-style toast notification (supports optional action button, e.g. Undo)
   */
  function showToast(message, type = "info", options = {}) {
    const container = getEl("toastContainer");
    if (!container) return;
    const el = document.createElement("div");
    el.className = `toast ${type}`;

    let iconSvg = ICONS.sparkles || "";
    if (type === "success") iconSvg = ICONS.check || "";
    else if (type === "error") iconSvg = ICONS.error || "";
    else if (type === "warning") iconSvg = ICONS.hourglass || "";

    el.innerHTML = iconSvg;
    // The message is text, never markup: it routinely carries a server error or
    // a file name, and this was the one render path that skipped escapeHtml.
    const text = document.createElement("span");
    text.className = "toast-message";
    text.textContent = String(message ?? "");
    el.appendChild(text);

    let dismissed = false;
    const dismiss = () => {
      if (dismissed) return;
      dismissed = true;
      el.classList.add("removing");
      setTimeout(() => el.remove(), 180);
    };

    if (options && options.action && typeof options.action.onClick === "function") {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "toast-action";
      btn.textContent = String(options.action.label || "撤销");
      btn.addEventListener("click", () => {
        dismiss();
        options.action.onClick();
      });
      el.appendChild(btn);
    }

    container.appendChild(el);
    const duration =
      Number(options && options.duration) ||
      (options && options.action ? 6000 : 3600);
    setTimeout(dismiss, duration);
  }

  function showLoading(text) {
    const textEl = getEl("loadingText");
    const modalEl = getEl("loadingModal");
    if (textEl) textEl.textContent = text || "正在处理…";
    showModal(modalEl);
  }

  function hideLoading() {
    hideModal(getEl("loadingModal"));
  }

  /**
   * Aesthetic confirmation modal with async promise.
   *
   * ``options.checkbox`` adds an opt-in row above the buttons and makes the
   * promise resolve with ``{ confirmed, checked }`` instead of a bare boolean.
   */
  //: Set while a confirm dialog is on screen. The dialog is a single shared
  //: element, so a second question asked before the first is answered would
  //: otherwise rewrite its contents and leave the first promise pending for
  //: ever. The newcomer settles the older one as "no" and takes over.
  let activeConfirm = null;

  function confirmDialog(message, options = {}) {
    return new Promise((resolve) => {
      const modal = getEl("confirmModal");
      if (!modal) {
        resolve(window.confirm(message));
        return;
      }
      if (activeConfirm) activeConfirm();

      const titleEl = getEl("confirmTitle");
      const msgEl = getEl("confirmMessage");
      const okBtn = getEl("confirmOkBtn");
      const cancelBtn = getEl("confirmCancelBtn");
      const closeBtn = modal.querySelector(".modal-close");
      const checkRow = getEl("confirmCheckRow");
      const checkInput = getEl("confirmCheckInput");
      const checkLabel = getEl("confirmCheckLabel");
      const inputRow = getEl("confirmInputRow");
      const inputEl = getEl("confirmInput");
      const inputLabel = getEl("confirmInputLabel");
      const wantsCheckbox = !!options.checkbox;
      const wantsInput = !!options.input;
      const wantsSelect = !!(options.select && Array.isArray(options.select.options) && options.select.options.length);

      let selectRow = getEl("confirmSelectRow");
      let selectEl = getEl("confirmSelect");
      if (wantsSelect && !selectRow && inputRow && inputRow.parentNode) {
        selectRow = document.createElement("label");
        selectRow.id = "confirmSelectRow";
        selectRow.className = "confirm-input-row";
        selectRow.innerHTML = `<span id="confirmSelectLabel"></span><select id="confirmSelect" data-no-custom="1" class="inline-select"></select>`;
        inputRow.parentNode.insertBefore(selectRow, checkRow || inputRow.nextSibling);
        selectEl = getEl("confirmSelect");
      }

      if (titleEl) {
        titleEl.innerHTML = `<span class="modal-title-icon">${ICONS.sparkles || ""}</span>${escapeUiHtml(options.title || "确认操作")}`;
      }
      if (msgEl) msgEl.textContent = message;
      if (okBtn) {
        okBtn.innerHTML = `<span class="btn-icon">${ICONS.check || ""}</span>${escapeUiHtml(options.confirmText || "确定")}`;
        okBtn.className = "btn primary" + (options.danger ? " danger" : "");
      }
      if (inputRow && inputEl) {
        if (wantsInput) {
          const spec = typeof options.input === "object" ? options.input : {};
          if (inputLabel) inputLabel.textContent = spec.label || "名称";
          inputEl.value = spec.value || "";
          inputEl.placeholder = spec.placeholder || "";
          inputRow.classList.remove("hidden");
        } else {
          inputRow.classList.add("hidden");
        }
      }
      if (selectRow && selectEl) {
        if (wantsSelect) {
          const selLabel = getEl("confirmSelectLabel");
          if (selLabel) selLabel.textContent = options.select.label || "方式";
          selectEl.innerHTML = options.select.options
            .map((opt) => `<option value="${escapeUiHtml(opt.value)}">${escapeUiHtml(opt.label)}</option>`)
            .join("");
          if (options.select.value !== undefined) selectEl.value = String(options.select.value);
          selectRow.classList.remove("hidden");
        } else {
          selectRow.classList.add("hidden");
        }
      }
      if (checkRow && checkInput && checkLabel) {
        if (wantsCheckbox) {
          checkLabel.textContent = options.checkbox.label || "";
          checkInput.checked = options.checkbox.checked !== false;
          checkRow.classList.remove("hidden");
        } else {
          checkRow.classList.add("hidden");
        }
      }

      function result(confirmed) {
        if (!wantsCheckbox && !wantsInput && !wantsSelect) return confirmed;
        return {
          confirmed,
          checked: checkInput ? checkInput.checked : false,
          value: inputEl ? inputEl.value.trim() : "",
          selected: selectEl ? selectEl.value : "",
        };
      }

      function cleanup() {
        activeConfirm = null;
        hideModal(modal);
        if (checkRow) checkRow.classList.add("hidden");
        if (selectRow) selectRow.classList.add("hidden");
        if (okBtn) okBtn.removeEventListener("click", onOk);
        if (cancelBtn) cancelBtn.removeEventListener("click", onCancel);
        if (closeBtn) closeBtn.removeEventListener("click", onCancel);
        if (inputEl) inputEl.removeEventListener("keydown", onInputKey);
        document.removeEventListener("keydown", onKey);
      }

      function onOk() { const value = result(true); cleanup(); resolve(value); }
      function onCancel() { const value = result(false); cleanup(); resolve(value); }
      function onInputKey(e) {
        if (e.key === "Enter") {
          e.preventDefault();
          onOk();
        }
      }
      function onKey(e) {
        // Escape is the reflex for "not this" and every other dialog in the app
        // answers it; this one used to require aiming at the × button.
        if (e.key === "Escape") {
          e.stopPropagation();
          onCancel();
        }
      }

      if (okBtn) okBtn.addEventListener("click", onOk);
      if (cancelBtn) cancelBtn.addEventListener("click", onCancel);
      if (closeBtn) closeBtn.addEventListener("click", onCancel);
      if (inputEl) inputEl.addEventListener("keydown", onInputKey);
      document.addEventListener("keydown", onKey);
      activeConfirm = onCancel;
      showModal(modal);
      if (wantsInput && inputEl) {
        requestAnimationFrame(() => {
          inputEl.focus();
          inputEl.select();
        });
      }
    });
  }

  return {
    renderAllIcons,
    enhanceSelect,
    setSelectValue,
    showToast,
    showModal,
    hideModal,
    showLoading,
    hideLoading,
    confirmDialog,
  };
})();
