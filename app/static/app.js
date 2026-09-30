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

  // GET again (never re-send a form this page came from), or say the tap was not saved
  function refresh() { location.replace(location.href); }
  function notSaved() { alert("That wasn't saved. Check your internet connection and try again."); }
  function markThen(url, delay) {
    post(url).then((r) => (r.ok ? setTimeout(refresh, delay) : notSaved())).catch(notSaved);
  }

  // --- demo: show WhatsApp text instead of opening WhatsApp -------------------
  const preview = document.getElementById("preview-dialog");
  let previewMark = null;
  function showPreview(el) {
    document.getElementById("preview-text").textContent = el.dataset.msg || "";
    previewMark = el.dataset.mark || null;
    preview.showModal();
  }
  document.getElementById("preview-ok")?.addEventListener("click", () => {
    if (previewMark) markThen(previewMark, 0);
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
      markThen(mark.dataset.mark, 1500);
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
  // a typed price (or one sent back after a form error) is kept until services change
  let manual = form.dataset.mode === "edit" || price.value.trim() !== "";
  let beforeFree = null;

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
      if (!price.readOnly) beforeFree = price.value;
      price.value = "0";
      price.readOnly = true;
      return;
    }
    if (price.readOnly) {
      if (beforeFree && beforeFree !== "0") price.value = beforeFree;
      else manual = false;
      beforeFree = null;
    }
    price.readOnly = false;
    if (!manual) price.value = total ? (total % 100 ? (total / 100).toFixed(2) : String(total / 100)) : "";
  }

  form.addEventListener("change", (e) => {
    if (e.target.name === "service_id" || e.target.name === "car_type_id") manual = false;
    recalc();
  });
  recalc();

  if (form.dataset.mode !== "new") return;
  const plate = form.querySelector("#plate");
  const info = document.getElementById("lookup-info");
  let timer = null;
  let lastQuery = "";
  let auto = null; // what the last match filled in, so a corrected plate takes it back

  function undoAuto() {
    if (!auto) return;
    auto.fields.forEach(([el, value]) => { if (el.value === value) el.value = ""; });
    auto.boxes.forEach(([el, was]) => { el.checked = was; });
    auto = null;
  }

  function fillIfEmpty(sel, value) {
    const el = form.querySelector(sel);
    if (el && !el.value.trim() && value) {
      el.value = value;
      auto.fields.push([el, value]);
    }
  }

  function autoCheck(el) {
    if (el && !el.checked) {
      auto.boxes.push([el, false]);
      el.checked = true;
    }
  }

  async function lookup() {
    const q = plate.value.trim();
    if (q.replace(/[^a-z0-9]/gi, "").length < 3) {
      lastQuery = "";
      info.hidden = true;
      undoAuto();
      recalc();
      return;
    }
    if (q === lastQuery) return;
    lastQuery = q;
    let d;
    try {
      const r = await fetch(form.dataset.lookup + "?plate=" + encodeURIComponent(q), { credentials: "same-origin" });
      d = await r.json();
    } catch (_) { return; }
    if (plate.value.trim() !== q) return; // the plate changed while we waited
    undoAuto();
    if (!d.found) {
      info.hidden = true;
      recalc();
      return;
    }

    auto = { fields: [], boxes: [] };
    fillIfEmpty("#make", d.make);
    fillIfEmpty("#phone", d.phone_display);
    if (d.car_type_id) {
      const radio = form.querySelector('input[name="car_type_id"][value="' + d.car_type_id + '"]');
      const current = form.querySelector('input[name="car_type_id"]:checked');
      if (radio && !radio.checked) {
        auto.boxes.push(current ? [current, true] : [radio, false]);
        radio.checked = true;
      }
    }
    if (!form.querySelector('input[name="service_id"]:checked')) {
      d.last_service_ids.forEach((id) => autoCheck(form.querySelector('input[name="service_id"][value="' + id + '"]')));
    }

    info.textContent = "";
    const b = document.createElement("b");
    b.textContent = "Returning customer";
    info.append(b, " · " + d.visits + " visit" + (d.visits === 1 ? "" : "s"));
    const l = d.loyalty;
    if (l.every) {
      if (l.next_free) {
        info.append(" · this wash is FREE (every " + ordinal(l.every) + ")");
        autoCheck(free);
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
  if (plate.value.trim()) lookup();
})();
