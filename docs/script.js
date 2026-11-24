(function () {
  "use strict";

  document.querySelectorAll("[data-instrument-ledger]").forEach(function (ledger) {
    const index = ledger.querySelector("[data-instrument-index]");
    const tabs = Array.from(index.querySelectorAll("a"));
    const panels = tabs.map(function (tab) {
      return document.getElementById(tab.hash.slice(1));
    });

    function select(active, moveFocus) {
      tabs.forEach(function (tab, position) {
        const selected = position === active;
        tab.setAttribute("aria-selected", String(selected));
        tab.tabIndex = selected ? 0 : -1;
        panels[position].hidden = !selected;
        panels[position].classList.toggle("is-entering", selected && moveFocus);
      });
      if (moveFocus) tabs[active].focus();
    }

    index.setAttribute("role", "tablist");
    tabs.forEach(function (tab, position) {
      const panel = panels[position];
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-controls", panel.id);
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", tab.id);
      panel.tabIndex = 0;

      tab.addEventListener("click", function (event) {
        event.preventDefault();
        select(position, true);
      });
      tab.addEventListener("keydown", function (event) {
        let next;
        switch (event.key) {
          case "ArrowLeft": next = (position + tabs.length - 1) % tabs.length; break;
          case "ArrowRight": next = (position + 1) % tabs.length; break;
          case "Home": next = 0; break;
          case "End": next = tabs.length - 1; break;
          case " ": next = position; break;
          default: return;
        }
        event.preventDefault();
        select(next, true);
      });
    });

    function selectFromHash() {
      const target = tabs.findIndex(function (tab) { return tab.hash === location.hash; });
      if (target !== -1) {
        select(target, false);
        panels[target].scrollIntoView();
        return true;
      }
      return false;
    }

    if (!selectFromHash()) select(0, false);
    window.addEventListener("hashchange", selectFromHash);
  });

  function copyWithSelection(code) {
    const selection = window.getSelection();
    if (!selection) return false;

    const original = Array.from({ length: selection.rangeCount }, function (_, index) {
      return selection.getRangeAt(index).cloneRange();
    });
    const range = document.createRange();
    range.selectNodeContents(code);
    selection.removeAllRanges();
    selection.addRange(range);
    try {
      if (document.execCommand("copy")) {
        selection.removeAllRanges();
        original.forEach(function (savedRange) { selection.addRange(savedRange); });
        return true;
      }
    } catch {
      // Keep the command selected for manual copying.
    }
    return false;
  }

  const copyStatus = document.querySelector("[data-copy-status]");
  document.querySelectorAll("[data-copy-command]").forEach(function (button) {
    const code = button.closest("[data-copy-block]").querySelector("code");
    const initial = button.textContent;
    let reset;
    button.hidden = false;

    button.addEventListener("click", async function () {
      let copied = false;
      try {
        if (navigator.clipboard) {
          await navigator.clipboard.writeText(code.textContent);
          copied = true;
        }
      } catch {
        // Local previews or browser permissions may require selection-based copying.
      }
      if (!copied) copied = copyWithSelection(code);
      clearTimeout(reset);
      copyStatus.textContent = copied ? button.dataset.copiedLabel : button.dataset.manualLabel;
      button.textContent = copyStatus.textContent;
      button.toggleAttribute("data-copied", copied);
      reset = setTimeout(function () {
        button.textContent = initial;
        button.removeAttribute("data-copied");
        copyStatus.textContent = "";
      }, 2200);
    });
  });
}());
