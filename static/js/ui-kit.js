/* ==========================================================================
   Lumina UI Kit - Clean, Resilient Micro-Interactions & Components
   ========================================================================== */
"use strict";

window.LuminaUI = (() => {
  const getEl = (id) => document.getElementById(id);
  const ICONS = window.LuminaIcons || {};

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
    trigger.innerHTML = '<span class="custom-select-label"></span><span class="custom-select-arrow"></span>';
    wrapper.appendChild(trigger);

    const menu = document.createElement("div");
    menu.className = "custom-select-menu";
    wrapper.appendChild(menu);

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
      buildMenu();
      menu.classList.add("open");
      trigger.classList.add("open");
    }

    function close() {
      menu.classList.remove("open");
      trigger.classList.remove("open");
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
      setTimeout(() => el.remove(), 350);
    }, 3600);
  }

  function showLoading(text) {
    const textEl = getEl("loadingText");
    const modalEl = getEl("loadingModal");
    if (textEl) textEl.textContent = text || "正在处理…";
    if (modalEl) modalEl.classList.remove("hidden");
  }

  function hideLoading() {
    const modalEl = getEl("loadingModal");
    if (modalEl) modalEl.classList.add("hidden");
  }

  /**
   * Aesthetic confirmation modal with async promise
   */
  function confirmDialog(message, options = {}) {
    return new Promise((resolve) => {
      const modal = getEl("confirmModal");
      if (!modal) {
        resolve(window.confirm(message));
        return;
      }

      const titleEl = getEl("confirmTitle");
      const msgEl = getEl("confirmMessage");
      const okBtn = getEl("confirmOkBtn");
      const cancelBtn = getEl("confirmCancelBtn");
      const closeBtn = modal.querySelector(".modal-close");

      if (titleEl) {
        titleEl.innerHTML = `<span class="modal-title-icon">${ICONS.sparkles || ""}</span>${options.title || "确认操作"}`;
      }
      if (msgEl) msgEl.textContent = message;
      if (okBtn) {
        okBtn.innerHTML = `<span class="btn-icon">${ICONS.check || ""}</span>${options.confirmText || "确定"}`;
        okBtn.className = "btn primary" + (options.danger ? " danger" : "");
      }

      function cleanup() {
        modal.classList.add("hidden");
        if (okBtn) okBtn.removeEventListener("click", onOk);
        if (cancelBtn) cancelBtn.removeEventListener("click", onCancel);
        if (closeBtn) closeBtn.removeEventListener("click", onCancel);
        modal.removeEventListener("click", onOverlay);
      }

      function onOk() { cleanup(); resolve(true); }
      function onCancel() { cleanup(); resolve(false); }
      function onOverlay(e) { if (e.target === modal) onCancel(); }

      if (okBtn) okBtn.addEventListener("click", onOk);
      if (cancelBtn) cancelBtn.addEventListener("click", onCancel);
      if (closeBtn) closeBtn.addEventListener("click", onCancel);
      modal.addEventListener("click", onOverlay);
      modal.classList.remove("hidden");
    });
  }

  return {
    renderAllIcons,
    enhanceSelect,
    setSelectValue,
    showToast,
    showLoading,
    hideLoading,
    confirmDialog,
  };
})();
