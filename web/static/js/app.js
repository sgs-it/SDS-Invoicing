/* SDS Invoicing Tracker — small offline helpers (no external libs) */
(function () {
  "use strict";

  // Toggle inline edit rows (used by clients / employees).
  document.addEventListener("click", function (e) {
    var t = e.target.closest(".js-toggle");
    if (t) {
      var target = document.getElementById(t.getAttribute("data-target"));
      if (target) {
        target.hidden = !target.hidden;
        if (!target.hidden) {
          var inp = target.querySelector("input:not([type=hidden])");
          if (inp) inp.focus();
        }
      }
      return;
    }

    // Delete a workflow step row.
    if (e.target.closest(".js-del-step")) {
      var row = e.target.closest("tr.step-row");
      if (row) {
        row.remove();
        renumberSteps();
      }
      return;
    }

    // Add a workflow step row.
    var addBtn = e.target.closest("#add-step");
    if (addBtn && window.STEP_TEMPLATE) {
      var table = document.getElementById("step-table");
      if (table) {
        table.insertAdjacentHTML("beforeend", window.STEP_TEMPLATE);
        renumberSteps();
      }
    }
  });

  function renumberSteps() {
    document.querySelectorAll("#step-table .step-idx").forEach(function (el, i) {
      el.textContent = i + 1;
    });
  }

  // Auto-dismiss flash messages.
  document.querySelectorAll(".flash").forEach(function (el) {
    setTimeout(function () {
      el.style.transition = "opacity .4s";
      el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 450);
    }, 6000);
  });

  // Mark print buttons' sibling links.
  document.querySelectorAll("[data-print]").forEach(function (btn) {
    btn.addEventListener("click", function () { window.print(); });
  });
})();
