"use strict";
(() => {
  document.querySelectorAll("[data-ui-open]").forEach(button => button.addEventListener("click", () => {
    const dialog = document.getElementById(button.dataset.uiOpen);
    if (button.dataset.uiExpand) document.getElementById(button.dataset.uiExpand).open = true;
    if (dialog && !dialog.open) dialog.showModal();
  }));
  document.querySelectorAll("[data-ui-close]").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
  document.querySelectorAll("dialog[data-ui-autopen]").forEach(dialog => dialog.showModal());
  const menus = [...document.querySelectorAll(".filter-menu,.action-menu")];
  document.addEventListener("click", event => menus.forEach(menu => { if (!menu.contains(event.target)) menu.open = false; }));
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape") return;
    const menu = menus.find(menu => menu.open && menu.contains(document.activeElement));
    if (menu) { event.preventDefault(); menu.open = false; menu.querySelector("summary").focus(); }
  });
  for (const menu of document.querySelectorAll(".filter-menu")) {
    const update = () => {
      const count = [...menu.querySelectorAll("select,input")].filter(field => field.value).length;
      const badge = menu.querySelector(".filter-count"); badge.hidden = !count; badge.textContent = count;
      menu.classList.toggle("has-filter", count > 0);
    };
    menu.closest("form").addEventListener("change", update);
    menu.closest("form").addEventListener("submit", () => { update(); menu.open = false; });
    // Module scripts restore their saved filter values after this deferred script.
    window.addEventListener("load", update, {once: true});
    menu.querySelectorAll("select").forEach(field => new MutationObserver(update).observe(field, {childList: true, subtree: true}));
  }
})();
