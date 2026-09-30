(function () {
  "use strict";
  const csrf = document.querySelector('meta[name="csrf"]')?.content || "";
  const isDemo = document.body.dataset.demo === "1";

  function post(url) {
    const body = new FormData();
    body.append("csrf", csrf);
    return fetch(url, {
      method: "POST",
      body,
      keepalive: true,
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" },
    });
  }

  function money(cents) {
    const r = Math.floor(cents / 100), c = cents % 100;
    return "R" + r.toLocaleString("en-US") + (c ? "." + String(c).padStart(2, "0") : "");
  }

  function ordinal(n) {
    const v = n % 100;
    return n + (v >= 11 && v <= 13 ? "th" : { 1: "st", 2: "nd", 3: "rd" }[n % 10] || "th");
  }

  // --- "12 min" timers on the board -------------------------------------------
  function since(ms) {
    const min = Math.max(0, Math.floor(ms / 60000));
    if (min < 1) return "just now";
    if (min < 60) return min + " min";
    return Math.floor(min / 60) + " h " + String(min % 60).padStart(2, "0") + " min";
  }
  function tick() {
    document.querySelectorAll("[data-since]").forEach((el) => {
      const t = Date.parse(el.dataset.since);
      if (!isNaN(t)) el.textContent = since(Date.now() - t);
    });
  }
  tick();
  setInterval(tick, 30000);

  // --- demo: show WhatsApp text instead of opening WhatsApp -------------------
  const preview = document.getElementById("preview-dialog");
  let previewMark = null;
  function showPreview(el) {
    document.getElementById("preview-text").textContent = el.dataset.msg || "";
    previewMark = el.dataset.mark || null;
    preview.showModal();
  }
  document.getElementById("preview-ok")?.addEventListener("click", () => {
    if (previewMark) post(previewMark).then(() => location.reload());
    else preview.close();
  });

  // --- clicks ---------------------------------------------------------------
  const startDialog = document.getElementById("start-dialog");
  document.addEventListener("click", (e) => {
    const start = e.target.closest("[data-start]");
    if (start && startDialog) {
      e.preventDefault();
      const form = startDialog.querySelector("form");
      form.reset();
      form.action = start.dataset.start;
      startDialog.querySelector(".dlg-plate").textContent = start.dataset.plate || "";
      startDialog.showModal();
      return;
    }

    const close = e.target.closest("[data-close]");
    if (close) {
      close.closest("dialog").close();
      return;
    }

    // WhatsApp link that also records something (ready / review asked)
    const mark = e.target.closest("[data-mark]");
    if (mark) {
      if (isDemo) {
        e.preventDefault();
        showPreview(mark);
        return;
      }
      // let the link open WhatsApp; update the board when the owner comes back
      post(mark.dataset.mark).finally(() => setTimeout(() => location.reload(), 1500));
      return;
    }

    const pv = e.target.closest("[data-preview]");
    if (pv && isDemo) {
      e.preventDefault();
      showPreview(pv);
    }
  });

  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset.confirm;
    if (msg && !confirm(msg)) e.preventDefault();
  });

  // --- car form: prices and returning customers -------------------------------
  const form = document.getElementById("visit-form");
  if (!form) return;
  const data = JSON.parse(document.getElementById("price-data").textContent);
  const price = form.querySelector("#price");
  const free = form.querySelector("#is_free");
  let manual = form.dataset.mode === "edit";

  price.addEventListener("input", () => { manual = price.value.trim() !== ""; });

  function recalc() {
    const type = form.querySelector('input[name="car_type_id"]:checked')?.value;
    let total = 0;
    form.querySelectorAll('input[name="service_id"]').forEach((cb) => {
      const p = (data.matrix[cb.value] || {})[type];
      const tag = cb.parentElement.querySelector(".chip-price");
      if (tag) tag.textContent = p != null ? money(p) : "";
      if (cb.checked) total += p || 0;
    });
    if (free.checked) {
      price.value = "0";
      price.readOnly = true;
      return;
    }
    price.readOnly = false;
    if (!manual) price.value = total ? (total % 100 ? (total / 100).toFixed(2) : String(total / 100)) : "";
  }

  form.addEventListener("change", (e) => {
    if (e.target === free && !free.checked) manual = false;
    if (e.target.name === "service_id" || e.target.name === "car_type_id") manual = false;
    recalc();
  });
  recalc();

  if (form.dataset.mode !== "new") return;
  const plate = form.querySelector("#plate");
  const info = document.getElementById("lookup-info");
  let timer = null;
  let lastQuery = "";

  function fillIfEmpty(sel, value) {
    const el = form.querySelector(sel);
    if (el && !el.value.trim() && value) el.value = value;
  }

  async function lookup() {
    const q = plate.value.trim();
    if (q.replace(/[^a-z0-9]/gi, "").length < 3 || q === lastQuery) {
      if (q.replace(/[^a-z0-9]/gi, "").length < 3) info.hidden = true;
      return;
    }
    lastQuery = q;
    let d;
    try {
      const r = await fetch(form.dataset.lookup + "?plate=" + encodeURIComponent(q), { credentials: "same-origin" });
      d = await r.json();
    } catch (_) { return; }
    if (!d.found) { info.hidden = true; return; }

    fillIfEmpty("#make", d.make);
    fillIfEmpty("#phone", d.phone_display);
    if (d.car_type_id) {
      const radio = form.querySelector('input[name="car_type_id"][value="' + d.car_type_id + '"]');
      if (radio) radio.checked = true;
    }
    const anyService = form.querySelector('input[name="service_id"]:checked');
    if (!anyService) {
      d.last_service_ids.forEach((id) => {
        const cb = form.querySelector('input[name="service_id"][value="' + id + '"]');
        if (cb) cb.checked = true;
      });
    }

    info.textContent = "";
    const b = document.createElement("b");
    b.textContent = "Returning customer";
    info.append(b, " · " + d.visits + " visit" + (d.visits === 1 ? "" : "s"));
    const l = d.loyalty;
    if (l.every) {
      if (l.next_free) {
        info.append(" · this wash is FREE (every " + ordinal(l.every) + ")");
        free.checked = true;
      } else {
        info.append(" · loyalty " + l.count + "/" + l.needed);
      }
    }
    info.hidden = false;
    recalc();
  }

  plate.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(lookup, 350);
  });
  plate.addEventListener("blur", lookup);
})();
