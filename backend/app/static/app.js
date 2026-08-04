// Client intake form behaviour:
//   1. Live autofill — when a document is chosen, upload it to the extract
//      endpoint, then fill the fields it returns (client can still edit them).
//   2. DIN logic — if a partner enters a DIN, hide + un-require the fields
//      marked hide_if_din (Permanent Address, Place of Birth) for that partner.
//   3. Admin: click-to-select the intake link, confirm destructive actions.
// Kept external (not inline) to satisfy the Content-Security-Policy.

document.addEventListener("DOMContentLoaded", function () {
  setupLinkInputs();
  setupAutofill();
  setupDinToggle();
  setupConfirmForms();
});

// --- confirm before destructive submits -------------------------------------
// CSP forbids inline handlers, so the prompt text rides on data-confirm.
function setupConfirmForms() {
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) event.preventDefault();
    });
  });
}

// --- admin: select intake link on focus/click -------------------------------
function setupLinkInputs() {
  document.querySelectorAll(".link-input").forEach(function (el) {
    var selectAll = function () { el.select(); };
    el.addEventListener("focus", selectAll);
    el.addEventListener("click", selectAll);
  });
}

// --- live autofill ----------------------------------------------------------
function setupAutofill() {
  var form = document.getElementById("intake-form");
  if (!form) return;
  var extractUrl = form.getAttribute("data-extract-url");

  form.querySelectorAll("input[type=file][data-slot]").forEach(function (input) {
    input.addEventListener("change", function () {
      var slot = input.getAttribute("data-slot");
      var status = document.querySelector('[data-status="' + slot + '"]');
      var file = input.files && input.files[0];
      if (!file) { if (status) status.textContent = ""; return; }

      setStatus(status, "reading", "Reading document…");

      var fd = new FormData();
      fd.append("slot", slot);
      fd.append("file", file);

      fetch(extractUrl, { method: "POST", body: fd })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
        .then(function (res) {
          if (!res.ok || !res.body.ok) {
            setStatus(status, "warn", (res.body && res.body.error) || "Could not read this file.");
            return;
          }
          var fields = res.body.fields || {};
          var count = fillFields(slot, fields);
          if (count > 0) {
            setStatus(status, "ok", "Read " + count + " field" + (count === 1 ? "" : "s") + " — please review.");
          } else if (res.body.had_text) {
            setStatus(status, "warn", "Couldn't auto-read the details — please type them in.");
          } else {
            setStatus(status, "warn", "No readable text found — please type the details in.");
          }
        })
        .catch(function () {
          setStatus(status, "warn", "Upload failed — you can still type the details in.");
        });
    });
  });
}

function fillFields(slot, fields) {
  var n = 0;
  Object.keys(fields).forEach(function (name) {
    var el = document.querySelector('[name="' + cssEscape(name) + '"]');
    if (!el) return;
    el.value = fields[name];
    el.classList.add("autofilled");
    flash(el);
    var note = document.querySelector('.autofill-note[data-for="' + cssEscape(name) + '"]');
    if (note) note.textContent = "✓ auto-filled from your upload · edit if needed";
    n++;
  });
  return n;
}

// --- DIN show/hide ----------------------------------------------------------
function setupDinToggle() {
  document.querySelectorAll('input[name$="__din"]').forEach(function (din) {
    var section = din.getAttribute("name").split("__")[0];
    var apply = function () {
      var hasDin = din.value.trim().length > 0;
      document
        .querySelectorAll('.field[data-hide-if-din][data-section="' + section + '"]')
        .forEach(function (field) {
          // Keep the field visible & editable — a DIN only makes it optional,
          // so just show/hide the required asterisk. The server enforces the
          // same "optional when DIN present" rule.
          var star = field.querySelector(".req");
          if (star) star.style.display = hasDin ? "none" : "";
        });
    };
    din.addEventListener("input", apply);
    apply();
  });
}

// --- helpers ----------------------------------------------------------------
function setStatus(el, kind, text) {
  if (!el) return;
  el.className = "upload-status " + (kind || "");
  el.textContent = text;
}

function flash(el) {
  el.classList.remove("flash");
  // reflow to restart the animation
  void el.offsetWidth;
  el.classList.add("flash");
}

function cssEscape(s) {
  return String(s).replace(/"/g, '\\"');
}
