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
    const f = e.target;
    const msg = f.dataset.confirm;
    if (msg && !confirm(msg)) e.preventDefault();
    if (e.defaultPrevented) return;
    // a second tap while a slow network still sends the first one would add the car (or booking) twice
    if (Date.now() - (f.sentAt || 0) < 10000) e.preventDefault();
    else f.sentAt = Date.now();
  });
  window.addEventListener("pageshow", (e) => {
    if (e.persisted) document.querySelectorAll("form").forEach((f) => { f.sentAt = 0; });  // came back with "Back"
  });

  // --- customer booking page ----------------------------------------------------
  const book = document.getElementById("book-form");
  if (book) bookingForm(book);

  function bookingForm(form) {
    const data = JSON.parse(document.getElementById("price-data").textContent);
    const total = document.getElementById("sum-total");
    const sum = total.parentElement;
    const when = document.getElementById("sum-when");
    const key = "book:" + form.dataset.slug;
    const remembered = ["name", "phone", "plate"];
    try {
      const saved = JSON.parse(localStorage.getItem(key) || "{}");
      remembered.forEach((n) => { if (!form.elements[n].value && saved[n]) form.elements[n].value = saved[n]; });
    } catch (_) { /* private mode */ }

    function update() {
      const type = form.querySelector('input[name="car_type_id"]:checked')?.value;
      let cents = 0, count = 0;
      const boxes = [...form.querySelectorAll('input[name="service_id"]')];
      boxes.forEach((cb) => {
        const p = (data.matrix[cb.value] || {})[type];
        cb.parentElement.querySelector(".svc-price").textContent = p != null ? money(p) : "";
        cb.disabled = p == null;  // no price for this car: not bookable online
        if (cb.disabled) cb.checked = false;
        if (cb.checked) { cents += p; count += 1; }
        cb.setCustomValidity("");
      });
      const first = boxes.find((cb) => !cb.disabled);
      if (first && !count) first.setCustomValidity("Pick at least one service.");
      total.textContent = count ? money(cents) : "—";
      const slot = form.querySelector('input[name="slot"]:checked');
      when.textContent = slot ? slot.dataset.label : "Pick a time";
      if (slot) sum.classList.remove("need");
    }

    function showDay(day) {
      form.querySelectorAll(".times").forEach((el) => el.classList.toggle("on", el.dataset.day === day));
      form.querySelectorAll('input[name="slot"]:checked').forEach((r) => { if (!r.value.startsWith(day)) r.checked = false; });
    }

    form.addEventListener("change", (e) => {
      if (e.target.name === "day") showDay(e.target.value);
      update();
    });
    form.addEventListener("submit", (e) => {
      if (!form.querySelector('input[name="slot"]:checked')) {
        e.preventDefault();
        sum.classList.add("need");
        when.textContent = "Pick a time first";
        document.getElementById("when").scrollIntoView({ behavior: "smooth", block: "start" });
        return;
      }
      try {
        localStorage.setItem(key, JSON.stringify(Object.fromEntries(remembered.map((n) => [n, form.elements[n].value.trim()]))));
      } catch (_) { /* private mode */ }
    });
    const picked = form.querySelector(".day-opt input:checked")?.parentElement;
    if (picked) {  // a day further on, picked before a form error, is scrolled into sight
      const strip = picked.parentElement;
      const hidden = picked.getBoundingClientRect().right - strip.getBoundingClientRect().right + 28;
      if (hidden > 0) strip.scrollLeft += hidden;
    }
    update();
  }

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

  // --- licence disc scanner ------------------------------------------------------
  const scanBtn = document.getElementById("scan-disc");
  const scanner = document.getElementById("scan-dialog");
  if (!scanBtn || !navigator.mediaDevices?.getUserMedia) return;  // the camera needs https
  scanBtn.hidden = false;
  if (matchMedia("(pointer: coarse)").matches && document.activeElement === plate) plate.blur();
  const hint = document.getElementById("scan-hint");
  const video = scanner.querySelector("video");
  let stream = null;
  let decoder = null;

  async function pdf417Reader() {
    if ("BarcodeDetector" in window) {  // Android Chrome reads PDF417 by itself
      try {
        if ((await BarcodeDetector.getSupportedFormats()).includes("pdf417")) return new BarcodeDetector({ formats: ["pdf417"] });
      } catch (_) { /* fall through to our own decoder */ }
    }
    const mod = await import(scanBtn.dataset.decoder);
    return new mod.BarcodeDetector({ formats: ["pdf417"] });
  }

  function stopCamera() {
    stream?.getTracks().forEach((t) => t.stop());
    stream = null;
    video.srcObject = null;
  }
  scanner.addEventListener("close", stopCamera);

  function fillFromDisc(d) {
    plate.value = d.plate;
    if (d.make) form.querySelector("#make").value = d.make;
    const type = d.car_type_id && form.querySelector('input[name="car_type_id"][value="' + d.car_type_id + '"]');
    if (type) type.checked = true;
    manual = false;
    recalc();
    lookup();  // a regular? fills the phone and last services
  }

  async function readDisc(raw) {
    const r = await fetch(form.dataset.disc + "?code=" + encodeURIComponent(raw), { credentials: "same-origin" });
    return r.ok ? r.json() : { found: false };
  }

  scanBtn.addEventListener("click", async () => {
    hint.textContent = "Point the camera at the barcode on the licence disc.";
    scanner.showModal();
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
      });
      if (!scanner.open) return stopCamera();
      video.srcObject = stream;
      await video.play();
      decoder = decoder || (await pdf417Reader());
    } catch (e) {
      stopCamera();
      hint.textContent = e.name === "NotAllowedError"
        ? "The camera is blocked. Allow it for this site, or type the plate."
        : "The scanner couldn't start on this phone. Type the plate instead.";
      return;
    }
    let tried = "";
    while (stream) {
      const codes = await decoder.detect(video).catch(() => []);
      const raw = codes.map((c) => c.rawValue).find((v) => v && v.includes("%"));
      if (raw && raw !== tried) {
        tried = raw;
        const d = await readDisc(raw).catch(() => ({ found: false }));
        if (d.found && stream) {
          navigator.vibrate?.(60);
          scanner.close();
          fillFromDisc(d);
          return;
        }
      }
      if (codes.length) hint.textContent = raw ? "Couldn't read this disc. Type the plate instead." : "That barcode isn't a licence disc.";
      await new Promise((r) => setTimeout(r, 120));
    }
  });
})();
