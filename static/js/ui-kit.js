/* ==========================================================================
   Lumina UI Kit - Clean, Resilient Micro-Interactions & Components
   ========================================================================== */
"use strict";

window.LuminaUI = (() => {
  const getEl = (id) => document.getElementById(id);
  const ICONS = window.LuminaIcons || {};

  //: Must match the ``modalPopOut`` / ``dropdownOut`` durations in style.css.
  const EXIT_MS = 190;

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

  //: Monotonic, so a dialog opened later always wins. Bounded by releasing the
  //: value when a dialog closes, which keeps it from growing without limit over a
  //: long session.
  let modalLayerCounter = 0;

  /**
   * Open a dialog, on a layer above whatever is already open.
   *
   * Every `.modal` inherits `z-index: 100` and `#previewModal` overrides it at 400,
   * so a dialog raised *from* the preview - 编辑回忆, 暂时收起 - opened underneath it and
   * its buttons looked broken. Static layers cannot express "whichever opened last",
   * which is the actual rule, so the layer is assigned here.
   */
  function showModal(el) {
    if (!el) return;
    if (el._luminaExitTimer) {
      clearTimeout(el._luminaExitTimer);
      el._luminaExitTimer = null;
    }
    // Re-opening the same dialog (a second confirm on top of the first) must also
    // move it to the front.
    modalLayerCounter += 1;
    el.style.zIndex = String(MODAL_LAYER_BASE + modalLayerCounter);
    el._luminaLayer = MODAL_LAYER_BASE + modalLayerCounter;
    el.classList.remove("closing");
    el.classList.remove("hidden");
  }

  /**
   * Hide a modal, playing the reverse of its entrance animation first.
   *
   * ``onHidden`` runs once the dialog is really gone, so callers can chain
   * whatever follows without racing the animation.
   */
  function hideModal(el, onHidden) {
    if (!el || el.classList.contains("hidden")) {
      if (typeof onHidden === "function") onHidden();
      return;
    }
    if (el._luminaExitTimer) clearTimeout(el._luminaExitTimer);
    const finish = () => {
      el._luminaExitTimer = null;
      el.classList.remove("closing");
      el.classList.add("hidden");
      // Hand the layer back: the dialog is gone, and a counter that only ever
      // climbs would eventually outgrow what the stylesheet expects.
      if (el._luminaLayer) {
        el._luminaLayer = 0;
        el.style.zIndex = "";
      }
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
   * Custom stylized dropdown wrapper with fluid transitions
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
    trigger.innerHTML = '<span class="custom-select-label"></span><span class="custom-select-arrow"></span>';
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

    function open() {
      if (exitTimer) {
        clearTimeout(exitTimer);
        exitTimer = null;
      }
      buildMenu();
      menu.classList.remove("closing");
      menu.classList.add("open");
      trigger.classList.add("open");
      trigger.setAttribute("aria-expanded", "true");
    }

    function close() {
      if (!menu.classList.contains("open")) return;
      trigger.classList.remove("open");
      trigger.setAttribute("aria-expanded", "false");
      menu.classList.add("closing");
      exitTimer = setTimeout(() => {
        exitTimer = null;
        menu.classList.remove("open", "closing");
      }, prefersReducedMotion() ? 0 : EXIT_MS);
    }

    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      if (menu.classList.contains("open")) close();
      else {
        document.querySelectorAll(".custom-select-menu.open").forEach((m) => m.classList.remove("open"));
        document.querySelectorAll(".custom-select-trigger.open").forEach((t) => t.classList.remove("open"));
        open();
      }
    });

    document.addEventListener("click", (e) => {
      if (!wrapper.contains(e.target)) close();
    });

    select.classList.add("native-hidden");
    updateLabel();

    // A dropdown must cover the dialog it lives in. The dialogs take 1100+ layers
    // from `showModal`, so the menu cannot use a constant: it reads the owning
    // dialog's layer when it opens and sits just above it.
    if (menu) {
      const ownerLayer = () => {
        const dialog = wrapper.closest(".modal");
        if (!dialog || !dialog._luminaLayer) return "";
        return String(dialog._luminaLayer + 1);
      };
      trigger.addEventListener("click", () => {
        const value = ownerLayer();
        menu.style.zIndex = value;
      });
    }
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
   * Warm paper-style toast notification
   */
  function showToast(message, type = "info") {
    const container = getEl("toastContainer");
    if (!container) return;
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    
    let iconSvg = ICONS.sparkles || "";
    if (type === "success") iconSvg = ICONS.check || "";
    else if (type === "error") iconSvg = ICONS.error || "";
    else if (type === "warning") iconSvg = ICONS.hourglass || "";

    el.innerHTML = `${iconSvg}<span>${message}</span>`;
    container.appendChild(el);

    setTimeout(() => {
      el.classList.add("removing");
      setTimeout(() => el.remove(), 180);
    }, 3600);
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
      const wantsCheckbox = !!options.checkbox;

      if (titleEl) {
        titleEl.innerHTML = `<span class="modal-title-icon">${ICONS.sparkles || ""}</span>${options.title || "确认操作"}`;
      }
      if (msgEl) msgEl.textContent = message;
      if (okBtn) {
        okBtn.innerHTML = `<span class="btn-icon">${ICONS.check || ""}</span>${options.confirmText || "确定"}`;
        okBtn.className = "btn primary" + (options.danger ? " danger" : "");
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
        return wantsCheckbox
          ? { confirmed, checked: checkInput ? checkInput.checked : false }
          : confirmed;
      }

      function cleanup() {
        activeConfirm = null;
        hideModal(modal);
        if (checkRow) checkRow.classList.add("hidden");
        if (okBtn) okBtn.removeEventListener("click", onOk);
        if (cancelBtn) cancelBtn.removeEventListener("click", onCancel);
        if (closeBtn) closeBtn.removeEventListener("click", onCancel);
        modal.removeEventListener("click", onOverlay);
        document.removeEventListener("keydown", onKey);
      }

      function onOk() { const value = result(true); cleanup(); resolve(value); }
      function onCancel() { const value = result(false); cleanup(); resolve(value); }
      function onOverlay(e) { if (e.target === modal) onCancel(); }
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
      modal.addEventListener("click", onOverlay);
      document.addEventListener("keydown", onKey);
      activeConfirm = onCancel;
      showModal(modal);
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
