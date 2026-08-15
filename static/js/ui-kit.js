/* Small reusable UI primitives for Lumina. */
"use strict";

window.LuminaUI = (() => {
  const getEl = (id) => document.getElementById(id);

function enhanceSelect(select) {
  if (select.dataset.enhanced || select.dataset.noCustom) return;
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
    trigger.querySelector(".custom-select-label").textContent = opt ? opt.text : "";
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
    else open();
  });
  document.addEventListener("click", (e) => {
    if (!wrapper.contains(e.target)) close();
  });

  select.classList.add("native-hidden");
  updateLabel();
}

function setSelectValue(select, value) {
  select.value = value;
  if (select.dataset.enhanced) {
    const wrapper = select.parentElement;
    const label = wrapper.querySelector(".custom-select-label");
    const opt = select.options[select.selectedIndex];
    if (label && opt) label.textContent = opt.text;
  }
}

function showToast(message, type = "info") {
  const container = getEl("toastContainer");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  container.appendChild(el);
  setTimeout(() => {
    el.classList.add("removing");
    setTimeout(() => el.remove(), 400);
  }, 3200);
}

function showLoading(text) {
  getEl("loadingText").textContent = text || "正在处理…";
  getEl("loadingModal").classList.remove("hidden");
}

function hideLoading() {
  getEl("loadingModal").classList.add("hidden");
}

function confirmDialog(message, options = {}) {
  return new Promise((resolve) => {
    const modal = getEl("confirmModal");
    getEl("confirmTitle").textContent = options.title || "确认操作";
    getEl("confirmMessage").textContent = message;
    const okBtn = getEl("confirmOkBtn");
    okBtn.textContent = options.confirmText || "确定";
    okBtn.className = "btn primary" + (options.danger ? " danger" : "");
    const cancelBtn = getEl("confirmCancelBtn");
    const closeBtn = modal.querySelector(".modal-close");

    function cleanup() {
      modal.classList.add("hidden");
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      closeBtn.removeEventListener("click", onCancel);
      modal.removeEventListener("click", onOverlay);
    }
    function onOk() { cleanup(); resolve(true); }
    function onCancel() { cleanup(); resolve(false); }
    function onOverlay(e) { if (e.target === modal) onCancel(); }

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    closeBtn.addEventListener("click", onCancel);
    modal.addEventListener("click", onOverlay);
    modal.classList.remove("hidden");
  });
}


  return {
    enhanceSelect,
    setSelectValue,
    showToast,
    showLoading,
    hideLoading,
    confirmDialog,
  };
})();
