/* Govee control — UI logic. Plain JS, no build step, no network.
 *
 * Backend: window.pywebview.api.<method>(...) (available after `pywebviewready`),
 * pushes: window.onState(state). Commands return {ok:true,...} | {ok:false,error}.
 * Errors are shown as non-blocking toasts; never alert/confirm/prompt.
 * Every visible string comes from i18n.js: t(key, params) (中文 / English).
 */
(function () {
  "use strict";

  // =====================================================================
  // small helpers
  // =====================================================================
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  const I18N = window.GoveeI18n;
  const t = I18N.t;
  // until the backend says (get_language), guess from the browser and fill the page
  I18N.setLang(I18N.guess());
  I18N.apply(document);

  function el(tag, attrs, children) {
    const e = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        const v = attrs[k];
        if (v === undefined || v === null || v === false) continue;
        if (k === "class") e.className = v;
        else if (k === "text") e.textContent = v;
        else if (k === "html") e.innerHTML = v;
        else if (k === "style") e.style.cssText = v;
        else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
        else if (k === "dataset") Object.assign(e.dataset, v);
        else e.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const c of [].concat(children || [])) {
      if (c === null || c === undefined || c === false) continue;
      e.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
    }
    return e;
  }

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const debounce = (fn, ms) => {
    let t = null;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
  };

  function hexToRgb(h) {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(h || "").trim());
    if (!m) return [0, 0, 0];
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const rgbToHex = (c) => "#" + c.map((x) => clamp(Math.round(x), 0, 255).toString(16).padStart(2, "0")).join("");
  const normHex = (h) => rgbToHex(hexToRgb(h));
  const isHex = (h) => /^#?[0-9a-f]{6}$/i.test(String(h || "").trim());

  function rgbToHsv([r, g, b]) {
    r /= 255; g /= 255; b /= 255;
    const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
    let h = 0;
    if (d) {
      if (mx === r) h = ((g - b) / d) % 6;
      else if (mx === g) h = (b - r) / d + 2;
      else h = (r - g) / d + 4;
      h /= 6; if (h < 0) h += 1;
    }
    return [h, mx ? d / mx : 0, mx];
  }
  function hsvToRgb(h, s, v) {
    h = ((h % 1) + 1) % 1;
    const i = Math.floor(h * 6), f = h * 6 - i;
    const p = v * (1 - s), q = v * (1 - f * s), t = v * (1 - (1 - f) * s);
    const [r, g, b] = [[v, t, p], [q, v, p], [p, v, t], [p, q, v], [t, p, v], [v, p, q]][i % 6];
    return [r * 255, g * 255, b * 255];
  }
  const lerp = (a, b, f) => [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
  const scale = (c, k) => [c[0] * k, c[1] * k, c[2] * k];
  function hsvLerp(a, b, f) {
    const [h0, s0, v0] = rgbToHsv(a), [h1, s1, v1] = rgbToHsv(b);
    const dh = ((h1 - h0 + 1.5) % 1) - 0.5;
    return hsvToRgb(h0 + dh * f, s0 + (s1 - s0) * f, v0 + (v1 - v0) * f);
  }
  function gradientAt(pal, pos, hsv) {
    const L = pal.length;
    pos = ((pos % L) + L) % L;
    const i = Math.floor(pos);
    return (hsv ? hsvLerp : lerp)(pal[i % L], pal[(i + 1) % L], pos - i);
  }

  function paintRange(input) {
    const min = +input.min || 0, max = +input.max || 100;
    input.style.setProperty("--p", ((+input.value - min) / (max - min || 1)) * 100 + "%");
  }

  const ICON_X = '<svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  const ICON_TRASH = '<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>';

  // =====================================================================
  // toasts (non-blocking; identical messages are merged)
  // =====================================================================
  const toastBox = $("#toasts");
  const liveToasts = new Map();
  /** action (optional): {label, run} adds a button; such toasts stay until used or clicked away. */
  function toast(msg, kind, action) {
    msg = String(msg || "");
    if (!msg) return;
    kind = kind || "info";
    const key = kind + "|" + msg;
    const ttl = action ? 0 : kind === "error" ? 5200 : 2800;
    let t = liveToasts.get(key);
    if (t) {
      clearTimeout(t.timer);
    } else {
      t = { node: el("div", { class: "toast " + kind + (action ? " has-action" : "") }, [el("span", { text: msg })]) };
      if (action) {
        t.node.appendChild(el("button", {
          class: "btn btn-sm btn-primary toast-action", text: action.label,
          onclick: (e) => { e.stopPropagation(); drop(); action.run(); },
        }));
      }
      t.node.addEventListener("click", () => drop());
      toastBox.appendChild(t.node);
      liveToasts.set(key, t);
      while (toastBox.children.length > 4) toastBox.firstChild.remove();
    }
    function drop() {
      clearTimeout(t.timer);
      liveToasts.delete(key);
      t.node.classList.add("out");
      setTimeout(() => t.node.remove(), 260);
    }
    if (ttl) t.timer = setTimeout(drop, ttl);
  }

  // =====================================================================
  // backend bridge
  // =====================================================================
  let markReady;
  const ready = new Promise((r) => { markReady = r; });
  const apiAvailable = () => !!(window.pywebview && window.pywebview.api && window.pywebview.api.get_state);
  window.addEventListener("pywebviewready", () => markReady());
  (function poll() {   // safety net if the event fired before this script ran
    if (apiAvailable()) markReady(); else setTimeout(poll, 250);
  })();

  /** Call an Api method. Shows {ok:false} errors / warnings as toasts. Never throws. */
  async function api(name, ...args) {
    await ready;
    try {
      const fn = window.pywebview.api[name];
      if (typeof fn !== "function") throw new Error(t("err.noMethod", { name }));
      const r = await fn(...args);
      if (r && typeof r === "object" && r.ok === false) toast(r.error || t("err.failed"), "error");
      else if (r && typeof r === "object" && r.warning) toast(r.warning, "warn");
      return r;
    } catch (e) {
      const msg = (e && (e.message || e.toString())) || t("err.unknown");
      toast(t("err.failedDetail", { msg }), "error");
      return { ok: false, error: msg };
    }
  }
  const ok = (r) => !!(r && r.ok);

  /** Run an api call with a busy indicator on a button / element. */
  async function busy(node, promise) {
    if (node) node.classList.add("busy");
    try { return await promise; } finally { if (node) node.classList.remove("busy"); }
  }

  async function refreshState() {
    const s = await api("get_state");
    if (s && s.segment_count) window.onState(s);
    return s;
  }

  // =====================================================================
  // application state
  // =====================================================================
  let S = null;                 // last state from the backend
  let effects = [];             // [{id,label}]
  const effectLabel = (id) => (I18N.has("fx." + id) ? t("fx." + id)
    : (effects.find((e) => e.id === id) || {}).label || id);
  /** Built-in default palette "默认" is stored by that name: show it per language. */
  const DEFAULT_PALETTE = String.fromCharCode(0x9ed8, 0x8ba4);   // "默认" (config._default_palettes)
  const palName = (name) => (name === DEFAULT_PALETTE ? t("palette.default") : name);
  let presets = [];             // [{name,effects,colors,speed_pct,width?}]
  let sceneCats = [];           // [{category, scenes:[{id,name,hint}]}]
  let lastSceneId = null;
  const N = () => (S && S.segment_count) || 15;
  const modeKind = () => (S && S.mode && S.mode.kind) || null;

  // =====================================================================
  // navigation
  // =====================================================================
  let page = "segments";
  const pageEnter = {};
  function go(p) {
    page = p;
    $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.page === p));
    $$(".page").forEach((s) => s.classList.toggle("active", s.id === "page-" + p));
    if (pageEnter[p]) pageEnter[p]();
  }
  $("#nav").addEventListener("click", (e) => {
    const b = e.target.closest(".nav-item");
    if (b) go(b.dataset.page);
  });

  // =====================================================================
  // modal
  // =====================================================================
  const overlay = $("#overlay"), modalBox = $("#modal");
  let modalClose = null;
  let modalSticky = false;   // sticky modals ignore clicks on the backdrop
  function openModal(build, opts) {
    opts = opts || {};
    closeModal();
    modalBox.innerHTML = "";
    modalBox.className = "modal" + (opts.wide ? " wide" : "");
    modalBox.style.width = "";
    modalSticky = !!opts.sticky;
    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      overlay.hidden = true;
      modalBox.innerHTML = "";
      modalClose = null;
      modalSticky = false;
      if (opts.onClose) opts.onClose();
    };
    modalClose = close;
    build(modalBox, close);
    overlay.hidden = false;
    return close;
  }
  function closeModal() { if (modalClose) modalClose(); }
  overlay.addEventListener("mousedown", (e) => { if (e.target === overlay && !modalSticky) closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !modalSticky) closeModal(); });

  function modalHead(title, close) {
    return el("div", { class: "modal-head" }, [
      el("span", { style: "width:32px" }),
      el("h2", { text: title }),
      el("button", { class: "icon-btn", title: t("common.close"), html: ICON_X, onclick: close }),
    ]);
  }

  // =====================================================================
  // colour picker (SV square + hue slider + hex + quick swatches)
  // =====================================================================
  const QUICK_COLORS = ["#ff0000", "#ff5000", "#ff8a00", "#ffd000", "#a8ff00", "#00e050", "#00d6b4",
    "#00b4ff", "#0050ff", "#5a10ff", "#a000ff", "#ff00c8", "#ff0060"];

  function ColorPicker(root, opts) {
    let [h, s, v] = rgbToHsv(hexToRgb(opts.color || "#ff5000"));
    const knob = el("div", { class: "knob" });
    const sv = el("div", { class: "cp-sv" }, [knob]);
    const hue = el("input", { type: "range", class: "range range-hue", min: "0", max: "360", step: "1" });
    const prev = el("div", { class: "cp-preview" });
    const hex = el("input", { class: "text-input cp-hex", maxlength: "7", spellcheck: "false" });
    const quickRow = el("div", { class: "cp-quick" });
    root.appendChild(el("div", { class: "cp" }, [sv, hue, el("div", { class: "cp-row" }, [prev, hex]), quickRow]));

    const get = () => rgbToHex(hsvToRgb(h, s, v));
    function render(skipHex) {
      sv.style.setProperty("--hue-color", rgbToHex(hsvToRgb(h, 1, 1)));
      knob.style.left = s * 100 + "%";
      knob.style.top = (1 - v) * 100 + "%";
      knob.style.background = get();
      hue.value = Math.round(h * 360);
      hue.style.setProperty("--thumb", rgbToHex(hsvToRgb(h, 1, 1)));
      prev.style.background = get();
      if (!skipHex) hex.value = get();
    }
    const emitInput = () => opts.onInput && opts.onInput(get());
    const emitCommit = () => opts.onCommit && opts.onCommit(get());

    function svAt(e) {
      const r = sv.getBoundingClientRect();
      s = clamp((e.clientX - r.left) / r.width, 0, 1);
      v = clamp(1 - (e.clientY - r.top) / r.height, 0, 1);
      render(); emitInput();
    }
    let dragging = false;
    sv.addEventListener("pointerdown", (e) => { dragging = true; sv.setPointerCapture(e.pointerId); svAt(e); });
    sv.addEventListener("pointermove", (e) => { if (dragging) svAt(e); });
    sv.addEventListener("pointerup", () => { if (dragging) { dragging = false; emitCommit(); } });
    sv.addEventListener("pointercancel", () => { dragging = false; });
    hue.addEventListener("input", () => { h = (+hue.value % 360) / 360; render(); emitInput(); });
    hue.addEventListener("change", emitCommit);
    hex.addEventListener("input", () => {
      if (isHex(hex.value)) { [h, s, v] = rgbToHsv(hexToRgb(hex.value)); render(true); emitInput(); }
    });
    hex.addEventListener("change", () => {
      if (isHex(hex.value)) { [h, s, v] = rgbToHsv(hexToRgb(hex.value)); render(); emitCommit(); }
      else { render(); toast(t("toast.badHex"), "warn"); }
    });

    function setQuick(colors) {
      quickRow.innerHTML = "";
      for (const c of colors || []) {
        quickRow.appendChild(el("button", {
          class: "swatch", title: c, style: "background:" + c,
          onclick: () => { set(c); emitInput(); emitCommit(); },
        }));
      }
      quickRow.hidden = !(colors && colors.length);
    }
    function set(c) { [h, s, v] = rgbToHsv(hexToRgb(c)); render(); }

    setQuick(opts.quick);
    render();
    return { get, set, setQuick };
  }

  /** Modal colour picker: resolves the chosen colour via onPick. */
  function openColorModal(title, color, onPick) {
    openModal((box, close) => {
      box.appendChild(modalHead(title, close));
      const holder = el("div");
      box.appendChild(holder);
      const picker = ColorPicker(holder, { color, quick: QUICK_COLORS });
      box.appendChild(el("div", { class: "modal-actions" }, [
        el("div", { class: "spacer" }),
        el("button", { class: "btn btn-ghost", text: t("common.cancel"), onclick: close }),
        el("button", { class: "btn btn-primary btn-wide", text: t("common.ok"), onclick: () => { onPick(picker.get()); close(); } }),
      ]));
    });
    modalBox.style.width = "min(420px, 100%)";
  }

  // =====================================================================
  // 配色推荐 (palette recommender) — shared by 分段配色 and DIY
  // =====================================================================
  // labels: t("rec.style.<id>"), t("rec.mood.<id>"), t("rec.method.<id>")
  const STYLES = [
    ["refreshing", "conic-gradient(#3ddc84,#2ec5e8,#ffe14d,#ff6b8a,#3ddc84)"],
    ["retro", "conic-gradient(#d9822b,#b5452b,#e0b03c,#d9822b)"],
    ["elegant", "conic-gradient(#8a5cf6,#d24bb0,#3d6fd8,#8a5cf6)"],
    ["vivid", "conic-gradient(#ff2a2a,#ffd400,#00e05a,#00a2ff,#c000ff,#ff2a2a)"],
    ["pastel", "conic-gradient(#f39ab6,#9ad0f5,#b8e6a0,#f5d58a,#f39ab6)"],
    ["neon", "conic-gradient(#ff00d4,#00f0ff,#7c00ff,#ff00d4)"],
  ];
  const MOODS = ["work", "leisure", "joy", "warm", "in_love", "party", "gourmet", "calm", "happiness",
    "lively", "romantic", "focus"];
  const METHODS = [
    ["gradient", '<rect x="5" y="5" width="14" height="14" rx="2"/><path d="M9 5v14M13 5v14"/>'],
    ["complementary", '<circle cx="12" cy="12" r="8"/><path d="M12 4l2.5 8-2.5 8-2.5-8z"/>'],
    ["random", '<rect x="4" y="9" width="10" height="10" rx="2"/><rect x="11" y="4" width="9" height="9" rx="2"/>'],
    ["double_complementary", '<circle cx="12" cy="12" r="8"/><rect x="8.5" y="8.5" width="7" height="7" rx="1"/>'],
    ["split_complementary", '<circle cx="12" cy="12" r="8"/><path d="M12 4l4 6-4 10-4-10z"/>'],
    ["analogous", '<circle cx="12" cy="12" r="8"/><path d="M12 12L8 5.2M12 12l4-6.8"/>'],
    ["triadic", '<circle cx="12" cy="12" r="8"/><path d="M12 5l6 10.5H6z"/>'],
  ];
  const rec = { style: "refreshing", hue: 0, mood: "work", method: "gradient", n: 4, result: [] };

  /**
   * opts: { title, maxN, defaultN, primaryLabel, onUse(colors) -> Promise|void }
   * The result can also be saved as a palette (save_palette).
   */
  function openRecommender(opts) {
    const maxN = opts.maxN || 8;
    rec.n = clamp(opts.defaultN || rec.n, 1, maxN);
    rec.result = [];
    openModal((box, close) => {
      box.appendChild(modalHead(t("common.recommend"), close));

      // 基色
      const styleRow = el("div", { class: "style-row" });
      for (const [id, ring] of STYLES) {
        styleRow.appendChild(el("button", {
          class: "opt" + (rec.style === id ? " active" : ""), dataset: { id },
          onclick: (e) => { rec.style = id; mark(styleRow, e.currentTarget); },
        }, [el("span", { class: "ring", style: "background:" + ring }), t("rec.style." + id)]));
      }
      const hue = el("input", { type: "range", class: "range range-hue", min: "0", max: "359", step: "1", value: String(rec.hue) });
      const paintHue = () => hue.style.setProperty("--thumb", rgbToHex(hsvToRgb(rec.hue / 360, 0.85, 1)));
      hue.addEventListener("input", () => { rec.hue = +hue.value; paintHue(); });
      paintHue();

      // 场景
      const moodRow = el("div", { class: "chips" });
      for (const id of MOODS) {
        moodRow.appendChild(el("button", {
          class: "chip" + (rec.mood === id ? " active" : ""),
          onclick: (e) => { rec.mood = id; mark(moodRow, e.currentTarget); }, text: t("rec.mood." + id),
        }));
      }

      // 配色方法
      const methodRow = el("div", { class: "method-row" });
      for (const [id, icon] of METHODS) {
        methodRow.appendChild(el("button", {
          class: "opt" + (rec.method === id ? " active" : ""),
          onclick: (e) => { rec.method = id; mark(methodRow, e.currentTarget); },
        }, [el("span", { html: '<svg class="method-ico" viewBox="0 0 24 24">' + icon + "</svg>" }), t("rec.method." + id)]));
      }

      // 颜色数
      const nVal = el("span", { text: String(rec.n) });
      const stepper = el("div", { class: "stepper" }, [
        el("button", { text: "−", onclick: () => { rec.n = clamp(rec.n - 1, 1, maxN); nVal.textContent = rec.n; } }),
        nVal,
        el("button", { text: "+", onclick: () => { rec.n = clamp(rec.n + 1, 1, maxN); nVal.textContent = rec.n; } }),
      ]);

      // 结果
      const resultRow = el("div", { class: "result-row" }, [el("span", { class: "muted small", text: t("rec.hint") })]);
      const useBtn = el("button", { class: "btn btn-primary", text: opts.primaryLabel || t("rec.use"), disabled: true });
      const nameInput = el("input", { class: "text-input", placeholder: t("rec.palName"), maxlength: "20", style: "width:140px", spellcheck: "false" });
      const saveBtn = el("button", { class: "btn", text: t("rec.saveAsPal"), disabled: true });
      const genBtn = el("button", { class: "btn btn-primary btn-wide", text: t("rec.generate") });

      function showResult() {
        resultRow.innerHTML = "";
        for (const c of rec.result) resultRow.appendChild(el("span", { class: "swatch", title: c, style: "background:" + c }));
        useBtn.disabled = saveBtn.disabled = !rec.result.length;
      }
      genBtn.addEventListener("click", async () => {
        const r = await busy(genBtn, api("generate_palette", rec.style, rec.hue, rec.mood, rec.method, rec.n));
        if (ok(r)) { rec.result = r.colors.slice(0, maxN); showResult(); }
      });
      useBtn.addEventListener("click", async () => {
        const r = await busy(useBtn, Promise.resolve(opts.onUse(rec.result.slice())));
        if (r !== false) close();
      });
      saveBtn.addEventListener("click", async () => {
        const name = nameInput.value.trim();
        if (!name) { toast(t("rec.nameFirst"), "warn"); nameInput.focus(); return; }
        const r = await busy(saveBtn, api("save_palette", name, rec.result));
        if (ok(r)) { toast(t("rec.saved", { name }), "ok"); refreshState(); }
      });

      box.appendChild(el("div", { class: "modal-sec" }, [el("div", { class: "modal-label", text: t("rec.base") }), styleRow,
        el("div", { style: "margin-top:16px" }, [hue])]));
      box.appendChild(el("div", { class: "modal-sec" }, [el("div", { class: "modal-label", text: t("rec.mood") }), moodRow]));
      box.appendChild(el("div", { class: "modal-sec" }, [el("div", { class: "modal-label", text: t("rec.method") }), methodRow]));
      // sticky footer: generate + result stay visible on short windows
      box.appendChild(el("div", { class: "modal-foot" }, [
        el("div", { class: "modal-actions", style: "margin-top:0" }, [
          el("span", { class: "modal-label", style: "margin:0", text: t("rec.count") }), stepper,
          el("div", { class: "spacer" }), genBtn]),
        el("div", { style: "margin-top:10px" }, [resultRow]),
        el("div", { class: "modal-actions", style: "margin-top:8px" }, [useBtn, el("div", { class: "spacer" }), nameInput, saveBtn]),
      ]));
    }, { wide: true });
  }
  function mark(row, node) {
    for (const c of row.children) c.classList.toggle("active", c === node);
  }

  // =====================================================================
  // top bar: connection, power, brightness, strip preview
  // =====================================================================
  // link status -> t("status.<status>"); statuses with a banner: t("banner.<status>")
  const statusText = (st) => (I18N.has("status." + st) ? t("status." + st) : st);
  const topStrip = $("#topStrip");
  function buildStrip(n, force) {
    if (topStrip.children.length === n && !force) return;
    topStrip.innerHTML = "";
    for (let i = 0; i < n; i++) topStrip.appendChild(el("div", { class: "led", title: t("strip.led", { n: i + 1 }) }));
  }
  function paintStrip(colors) {
    const leds = topStrip.children;
    for (let i = 0; i < leds.length; i++) {
      const c = colors && colors[i];
      if (c && c !== "#000000") {
        leds[i].style.backgroundColor = c;
        leds[i].style.boxShadow = "0 0 10px " + c + "88";
      } else {
        leds[i].style.backgroundColor = "";
        leds[i].style.boxShadow = "";
      }
    }
  }

  function renderTop() {
    const st = S.status || "disconnected";
    $("#conn").dataset.status = st;
    $("#connText").textContent = statusText(st);
    const banner = $("#busyBanner");
    const bannerKey = "banner." + st;
    banner.hidden = !I18N.has(bannerKey);
    if (!banner.hidden) banner.textContent = t(bannerKey) + (S.device_stale ? " " + t("banner.chooseOther") : "");

    $("#power").dataset.power = S.power === true ? "on" : S.power === false ? "off" : "unknown";
    $("#power").title = S.power === null || S.power === undefined ? t("top.powerUnknown") : t("top.power");

    if (!brightUserActive()) setBrightUI(S.brightness);

    buildStrip(N());
    paintStrip(S.frame);
    let cap = t("strip.none");
    if (S.frame) cap = S.mode && S.mode.device ? t("strip.device") : S.mode ? t("strip.live") : t("strip.current");
    else if (lastSceneId !== null && !S.mode) cap = t("strip.scene");
    $("#stripCap").textContent = cap;
  }

  // power
  $("#power").addEventListener("click", async (e) => {
    const b = e.target.closest(".power-btn");
    if (!b) return;
    await busy($("#power"), api("set_power", b.dataset.on === "1"));
  });

  // brightness: send on every `input`; at most one call in flight, the newest value wins
  const bright = $("#brightness");
  let brPending = null, brInflight = false, brDrag = false, brLastTouch = 0;
  const brightUserActive = () => brDrag || brInflight || brPending !== null || Date.now() - brLastTouch < 800;
  function setBrightUI(v) {
    if (typeof v !== "number") return;
    bright.value = v; paintRange(bright);
    $("#brightVal").textContent = v + "%";
  }
  async function pumpBrightness() {
    if (brInflight) return;
    brInflight = true;
    try {
      while (brPending !== null) {
        const v = brPending; brPending = null;
        await api("set_brightness", v);
      }
    } finally {
      brInflight = false;
      brLastTouch = Date.now();
    }
  }
  bright.addEventListener("pointerdown", () => { brDrag = true; });
  window.addEventListener("pointerup", () => { if (brDrag) { brDrag = false; brLastTouch = Date.now(); } });
  bright.addEventListener("input", () => {
    const v = +bright.value;
    paintRange(bright);
    $("#brightVal").textContent = v + "%";
    brPending = v; brLastTouch = Date.now();
    pumpBrightness();
  });

  // =====================================================================
  // sidebar "当前" card
  // =====================================================================
  const SCREEN_MODES = ["dominant", "fade", "projection", "region"];
  const screenModeName = (m) => (SCREEN_MODES.includes(m) ? t("screen.mode." + m) : null);
  function describeMode(m) {
    if (!m) return null;
    const a = m.args || {};
    if (m.kind === "preset") return t("now.preset", { name: a.name || "" });
    if (m.kind === "effect") {
      const e = a.effect;
      if (Array.isArray(e)) return t("now.mix", { names: e.map(effectLabel).join(" + ") });
      return t("now.effect", { name: effectLabel(e) });
    }
    if (m.kind === "screen") return screenModeName(a.mode) ? t("now.screenMode", { mode: screenModeName(a.mode) }) : t("now.screen");
    if (m.kind === "music") return t("now.music");
    if (m.kind === "color_test") return t("now.colorTest");
    return t("now.running");
  }
  function renderNow() {
    if (!S) return;
    let d = describeMode(S.mode);
    if (d && S.mode.device) d += t("now.onDevice");
    $("#nowTitle").textContent = d || (lastSceneId !== null ? t("now.scene", { name: sceneTitle(lastSceneId) }) : t("now.idle"));
    $("#nowStop").hidden = !d;
  }
  $("#nowStop").addEventListener("click", (e) => busy(e.currentTarget, api("stop_mode")));

  // =====================================================================
  // state push
  // =====================================================================
  let lastKeys = {};
  window.onState = function (state) {
    if (!state || typeof state !== "object") return;
    const prev = S;
    S = state;
    if (S.mode) lastSceneId = null;
    if (S.language && (S.language.resolved !== I18N.lang() || S.language.setting !== langSetting)) {
      useLanguageInfo(S.language);
    }
    renderTop();

    const keys = {
      mode: JSON.stringify(S.mode),
      palettes: JSON.stringify([S.palettes, S.active_palette]),
      settings: JSON.stringify([S.autostart, S.profile, S.settings && S.settings.power_sync,
        S.settings && S.settings.transition, S.settings && S.settings.color_cal, S.color_test,
        S.device, S.device_others, S.version]),
    };
    const changed = (k) => keys[k] !== lastKeys[k];
    if (changed("mode")) {
      renderNow();
      onModeChanged();
    }
    if (changed("palettes")) { renderPalettes(); renderMusicSources(); }
    if (changed("settings")) renderSettings();
    if (!prev) firstState();
    lastKeys = keys;
  };

  function onModeChanged() {
    renderPresetList();
    renderScreenRun();
    renderMusicRun();
    renderSceneApplied();
    const k = modeKind();
    if (k !== "effect" && k !== "preset") diy.previewing = false;
  }

  // =====================================================================
  // 分段配色
  // =====================================================================
  let segColors = [];
  const segSel = new Set();
  const segBig = $("#segBig");
  let segPicker = null;

  function initSegColors() {
    const saved = S.settings && S.settings.segment_colors;
    if (Array.isArray(saved) && saved.length === N()) segColors = saved.map(normHex);
    else segColors = Array.from({ length: N() }, (_, i) => activePalette()[i % activePalette().length]);
  }
  function activePalette() {
    const p = (S && S.palettes) || {};
    return (p[S && S.active_palette] || Object.values(p)[0] || ["#ff4d4d", "#ffd43d", "#3dd6c6", "#7a5cff"]).map(normHex);
  }

  function renderSegBig() {
    if (segBig.children.length !== N()) {
      segBig.innerHTML = "";
      for (let i = 0; i < N(); i++) {
        segBig.appendChild(el("div", { class: "seg-cell", dataset: { i: String(i) } }, [
          el("div", { class: "blk" }), el("div", { class: "num", text: String(i + 1) }),
        ]));
      }
    }
    $$(".seg-cell", segBig).forEach((c, i) => {
      c.querySelector(".blk").style.background = segColors[i] || "#2a2e36";
      c.classList.toggle("sel", segSel.has(i));
    });
    const n = segSel.size;
    $("#segSelInfo").textContent = n ? t("seg.selected", { n }) : t("seg.noneHint");
  }

  // click / drag selection
  let dragAnchor = null, dragBase = null;
  const cellAt = (x, y) => {
    const c = document.elementFromPoint(x, y);
    const cell = c && c.closest && c.closest(".seg-cell");
    return cell && segBig.contains(cell) ? +cell.dataset.i : null;
  };
  segBig.addEventListener("pointerdown", (e) => {
    const i = cellAt(e.clientX, e.clientY);
    if (i === null) return;
    segBig.setPointerCapture(e.pointerId);
    const additive = e.ctrlKey || e.metaKey || e.shiftKey;
    dragBase = additive ? new Set(segSel) : new Set();
    if (additive && segSel.has(i) && !e.shiftKey) { segSel.delete(i); dragAnchor = null; renderSegBig(); return; }
    dragAnchor = i;
    applyDrag(i);
  });
  segBig.addEventListener("pointermove", (e) => {
    if (dragAnchor === null) return;
    const i = cellAt(e.clientX, e.clientY);
    if (i !== null) applyDrag(i);
  });
  const endDrag = () => { dragAnchor = null; };
  segBig.addEventListener("pointerup", endDrag);
  segBig.addEventListener("pointercancel", endDrag);
  function applyDrag(i) {
    segSel.clear();
    dragBase.forEach((x) => segSel.add(x));
    for (let k = Math.min(dragAnchor, i); k <= Math.max(dragAnchor, i); k++) segSel.add(k);
    renderSegBig();
  }
  $("#segSelAll").addEventListener("click", () => { for (let i = 0; i < N(); i++) segSel.add(i); renderSegBig(); });
  $("#segSelNone").addEventListener("click", () => { segSel.clear(); renderSegBig(); });

  /** set_segments / fill succeeded: the strip now shows static colours, not the last scene. */
  function paintedStatic(r) {
    if (r && Array.isArray(r.colors)) segColors = r.colors.map(normHex);
    renderSegBig();
    if (lastSceneId !== null) {
      lastSceneId = null;
      renderSceneApplied(); renderNow();
      if (S) renderTop();
    }
  }

  async function paintSelection(color, btn) {
    if (!segSel.size) { toast(t("seg.pickFirst"), "warn"); return; }
    const next = segColors.slice();
    segSel.forEach((i) => { next[i] = color; });
    const r = await busy(btn, api("set_segments", next));
    if (ok(r)) paintedStatic(r);
  }
  $("#segApplySel").addEventListener("click", (e) => paintSelection(segPicker.get(), e.currentTarget));
  $("#segFill").addEventListener("click", async (e) => {
    const c = segPicker.get();
    const r = await busy(e.currentTarget, api("fill", c));
    if (ok(r)) paintedStatic(r);
  });
  $("#segPalTile").addEventListener("click", async (e) => {
    const pal = activePalette();
    const r = await busy(e.currentTarget, api("set_segments", pal));
    if (ok(r)) paintedStatic(r);
  });
  $("#segRecommend").addEventListener("click", () => openRecommender({
    maxN: Math.min(15, N()), defaultN: 5, primaryLabel: t("seg.applyToStrip"),
    onUse: async (colors) => {
      const r = await api("set_segments", colors);
      if (!ok(r)) return false;
      paintedStatic(r);
      toast(t("seg.tiled", { n: N() }), "ok");
    },
  }));

  function renderPalettes() {
    if (!S) return;
    const pal = activePalette();
    const sw = $("#segPalSwatches");
    sw.innerHTML = "";
    for (const c of pal) {
      sw.appendChild(el("button", {
        class: "swatch", title: c, style: "background:" + c,
        onclick: () => { segPicker.set(c); paintSelection(c); },
      }));
    }

    const list = $("#palList");
    list.innerHTML = "";
    const pals = S.palettes || {};
    for (const name of Object.keys(pals)) {
      const row = el("div", { class: "pal-row" + (name === S.active_palette ? " active" : ""), title: t("seg.setActivePal") });
      const bar = el("div", { class: "pal-bar" }, pals[name].map((c) => el("i", { style: "background:" + c })));
      const del = el("button", { class: "icon-btn", title: t("seg.deletePal"), html: ICON_TRASH });
      row.append(el("span", { class: "pal-name", text: palName(name) }), bar, del);
      row.addEventListener("click", async (e) => {
        if (e.target.closest(".icon-btn, .confirm-inline")) return;
        if (name === S.active_palette) return;
        const r = await api("set_active_palette", name);
        if (ok(r)) refreshState();
      });
      del.addEventListener("click", () => {
        inlineConfirm(del, t("common.deleteNamedQ", { name: palName(name) }), async () => {
          const r = await api("delete_palette", name);
          if (ok(r)) refreshState();
        });
      });
      list.appendChild(row);
    }
  }

  /** Replace `anchor` with "text [删除] [取消]" until answered (no confirm() dialogs). */
  function inlineConfirm(anchor, text, onYes) {
    const box = el("span", { class: "confirm-inline" });
    const restore = () => { box.replaceWith(anchor); };
    box.append(text,
      el("button", { class: "btn btn-danger", text: t("common.delete"), onclick: (e) => { e.stopPropagation(); restore(); onYes(); } }),
      el("button", { class: "btn btn-ghost", text: t("common.cancel"), onclick: (e) => { e.stopPropagation(); restore(); } }));
    box.addEventListener("click", (e) => e.stopPropagation());
    anchor.replaceWith(box);
    setTimeout(() => { if (box.isConnected) restore(); }, 6000);
  }

  // =====================================================================
  // DIY 效果
  // =====================================================================
  const MAX_COLORS = 8;
  const diy = {
    name: t("diy.newName"), tab: "simple", simple: "fade1", mix: ["rainbow1", "rainbow2"],
    colors: ["#ff5000", "#3ecfe0"], speed: 50, width: 4,
    group: 2,             // 投影: segments per zone (1-5), sent as the effect's width
    selected: null,       // name of the preset loaded into the editor
    previewing: false,    // editor contents are what the strip is playing
  };
  const diyEffects = () => (diy.tab === "simple" ? [diy.simple] : diy.mix.slice());
  const hasAurora = () => diyEffects().includes("aurora");
  const hasProjection = () => diyEffects().includes("projection");
  // the one width slider: aurora band width (2-8) or, for 投影 alone, segments per zone (1-5)
  const widthIsGroup = () => hasProjection() && !hasAurora();

  /** Static thumbnail colours for an effect card (like the Govee picker; no animation). */
  function thumb(id, colors, n) {
    n = n || 15;
    const P = (colors && colors.length ? colors : ["#ff5000", "#3ecfe0"]).map(hexToRgb);
    const L = P.length;
    const dark = [42, 45, 52];
    const out = [];
    let seed = 7;
    const rnd = () => { seed = (seed * 9301 + 49297) % 233280; return seed / 233280; };
    for (let i = 0; i < n; i++) {
      let c;
      switch (id) {
        case "fade1": c = P[0]; break;
        case "fade2": c = scale(gradientAt(P, (i / n) * L), 0.85); break;
        case "fade3": c = P[Math.floor((i * L) / n) % L]; break;
        case "jump1": c = P[1 % L]; break;
        case "jump2": c = P[Math.floor(rnd() * L)]; break;
        case "flash1": c = i % 2 ? scale(P[0], 0.35) : P[0]; break;
        case "flash2": c = rnd() < 0.4 ? P[Math.floor(rnd() * L)] : dark; break;
        case "flash3": c = i % 4 < 2 ? P[0] : dark; break;
        case "marquee1": c = P[Math.floor(i / 2) % L]; break;
        case "marquee2": c = P[Math.floor((i + 1) / 2) % L]; break;
        case "marquee3": c = P[Math.floor(Math.abs(i - 7) / 2) % L]; break;
        case "flow1": c = gradientAt(P, (i / n) * L); break;
        case "flow2": c = gradientAt(P, (Math.abs(i - (n - 1) / 2) / (n / 2)) * L); break;
        case "rainbow1": c = L < 2 ? hsvToRgb(i / n, 1, 1) : gradientAt(P, (i / n) * L, true); break;
        case "rainbow2": c = L < 2 ? hsvToRgb(1 - i / n, 1, 1) : gradientAt(P, ((n - 1 - i) / n) * L, true); break;
        case "aurora": c = scale(gradientAt(P, i / Math.max(2, diy.width), true), 0.82 + 0.18 * Math.sin(i * 1.3)); break;
        case "projection": c = P[Math.floor(i / Math.max(1, diy.group)) % L]; break;
        default: c = P[i % L];
      }
      out.push(rgbToHex(c));
    }
    return out;
  }
  const thumbNode = (id, colors) => el("div", { class: "fx-thumb" }, thumb(id, colors).map((c) => el("i", { style: "background:" + c })));

  const ICON_CHEVRON = '<svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg>';
  /** 简单: the chosen effect as one row (like the Govee editor); click opens the picker. */
  function renderEffectRow() {
    const row = $("#fxRow");
    row.innerHTML = "";
    row.append(thumbNode(diy.simple, diy.colors), el("span", { class: "mix-name", text: effectLabel(diy.simple) }),
      el("span", { class: "chev", html: ICON_CHEVRON }));
  }

  function renderMixList() {
    const list = $("#mixList");
    list.innerHTML = "";
    diy.mix.forEach((id, k) => {
      list.appendChild(el("div", { class: "mix-item" }, [
        el("span", { class: "mix-idx", text: String(k + 1) }),
        thumbNode(id, diy.colors),
        el("span", { class: "mix-name", text: effectLabel(id) }),
        el("button", {
          class: "icon-btn", title: t("common.remove"), html: ICON_X,
          onclick: () => { diy.mix.splice(k, 1); renderDiyEditor(); liveApply(); },
        }),
      ]));
    });
    if (!diy.mix.length) list.appendChild(el("div", { class: "empty", text: t("diy.mixEmpty") }));
    $("#mixAdd").hidden = diy.mix.length >= 8;
  }

  /** Effect picker modal. forMix: append to the mix list; otherwise choose the 简单 effect
   *  and preview it at once (Govee: "单击即刻应用效果"). */
  function openEffectPicker(forMix) {
    openModal((box, close) => {
      box.appendChild(modalHead(t("diy.pickTitle"), close));
      box.appendChild(el("p", { class: "muted small", style: "text-align:center;margin:-6px 0 14px",
        text: forMix ? t("diy.pickForMix") : t("diy.pickInstant") }));
      const grid = el("div", { class: "effect-grid" });
      for (const e of effects) {
        grid.appendChild(el("button", {
          class: "fx-card" + (!forMix && e.id === diy.simple ? " selected" : ""), dataset: { id: e.id },
          onclick: () => {
            close();
            if (forMix) { diy.mix.push(e.id); renderDiyEditor(); liveApply(); }
            else { diy.simple = e.id; renderDiyEditor(); diyApply(); }
          },
        }, [thumbNode(e.id, diy.colors), el("div", { class: "fx-name", text: effectLabel(e.id) })]));
      }
      box.appendChild(grid);
    }, { wide: true });
  }
  $("#mixAdd").addEventListener("click", () => openEffectPicker(true));
  $("#fxRow").addEventListener("click", () => openEffectPicker(false));

  function renderDiyColors() {
    const box = $("#diyColors");
    box.innerHTML = "";
    diy.colors.forEach((c, k) => {
      const dot = el("button", { class: "cdot", title: t("diy.editColorTip", { color: c }), style: "background:" + c });
      dot.addEventListener("click", () => openColorModal(t("diy.editColor"), c, (nc) => {
        diy.colors[k] = nc; renderDiyEditor(); liveApply();
      }));
      const x = el("button", { class: "cdot-x", title: t("common.remove"), text: "✕" });
      x.addEventListener("click", () => {
        if (diy.colors.length <= 1) { toast(t("diy.keepOneColor"), "warn"); return; }
        diy.colors.splice(k, 1); renderDiyEditor(); liveApply();
      });
      box.appendChild(el("div", { class: "dot-wrap" }, [dot, x]));
    });
    if (diy.colors.length < MAX_COLORS) {
      box.appendChild(el("button", {
        class: "cdot-add", title: t("diy.addColor"), text: "+",
        onclick: () => openColorModal(t("diy.addColor"), suggestColor(), (nc) => {
          if (diy.colors.length >= MAX_COLORS) return;
          diy.colors.push(nc); renderDiyEditor(); liveApply();
        }),
      }));
    }
    $("#diyColorCount").textContent = diy.colors.length + " / " + MAX_COLORS;
  }
  function suggestColor() {   // a saturated hue away from the last colour (never white)
    const last = diy.colors[diy.colors.length - 1];
    const h = last ? rgbToHsv(hexToRgb(last))[0] + 0.33 : 0.05;
    return rgbToHex(hsvToRgb(h, 1, 1));
  }

  function renderDiyEditor() {
    $$("#diyTabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === diy.tab));
    $("#simpleSec").hidden = diy.tab !== "simple";
    $("#mixSec").hidden = diy.tab !== "mix";
    if (document.activeElement !== $("#diyName")) $("#diyName").value = diy.name;
    renderEffectRow();
    renderMixList();
    renderDiyNote();
    renderDiyColors();
    renderWidth();
    const sp = $("#diySpeed"); sp.value = diy.speed; paintRange(sp);
    $("#speedVal").textContent = diy.speed;
  }

  function renderWidth() {
    const group = widthIsGroup();
    $("#widthSec").hidden = !(hasAurora() || hasProjection());
    $("#widthTitle").textContent = group ? t("diy.widthGroup") : t("diy.widthAurora");
    $("#widthHint").textContent = group ? t("diy.widthGroupHint") : t("diy.widthAuroraHint");
    const w = $("#diyWidth");
    w.min = group ? 1 : 2; w.max = group ? 5 : 8;
    w.value = group ? diy.group : diy.width; paintRange(w);
    $("#widthVal").textContent = t("diy.segs", { n: +w.value });
  }

  function loadPreset(p) {
    const fx = (p.effects || []).filter((id) => effects.some((e) => e.id === id));
    diy.name = p.name;
    diy.colors = (p.colors || []).slice(0, MAX_COLORS).map(normHex);
    if (!diy.colors.length) diy.colors = ["#ff5000"];
    diy.speed = typeof p.speed_pct === "number" ? p.speed_pct : 50;
    diy.width = typeof p.width === "number" ? clamp(Math.round(p.width), 2, 8) : 4;
    diy.group = typeof p.width === "number" && (p.effects || []).includes("projection") && !(p.effects || []).includes("aurora")
      ? clamp(Math.round(p.width), 1, 5) : 2;
    if (fx.length > 1) { diy.tab = "mix"; diy.mix = fx; diy.simple = fx[0]; }
    else { diy.tab = "simple"; diy.simple = fx[0] || "fade1"; diy.mix = fx.slice(); }
    diy.selected = p.name;
  }

  function renderPresetList() {
    const ul = $("#presetList");
    if (!ul) return;
    ul.innerHTML = "";
    const running = modeKind() === "preset" ? (S.mode.args || {}).name : null;
    if (!presets.length) ul.appendChild(el("li", { class: "empty", text: t("diy.noPresets") }));
    for (const p of presets) {
      const del = el("button", { class: "icon-btn", title: t("common.delete"), html: ICON_TRASH });
      const li = el("li", {
        class: "preset-item" + (p.name === diy.selected ? " selected" : "") + (p.name === running ? " running" : ""),
      }, [
        el("div", { class: "preset-top" }, [
          p.name === running ? el("span", { class: "playing-dot", title: t("diy.playing") }) : null,
          el("span", { class: "preset-name", text: p.name, title: p.name }), del,
        ]),
        el("div", { class: "preset-bar" }, (p.colors || []).map((c) => el("i", { style: "background:" + c }))),
        el("div", { class: "preset-meta", text: (p.effects || []).map(effectLabel).join(" + ") }),
      ]);
      li.addEventListener("click", async (e) => {
        if (e.target.closest(".icon-btn, .confirm-inline")) return;
        loadPreset(p);
        renderDiyEditor();
        renderPresetList();
        const r = await busy(li, api("apply_preset", p.name));
        if (ok(r)) diy.previewing = true;
      });
      del.addEventListener("click", (e) => {
        e.stopPropagation();
        inlineConfirm(del, t("common.deleteQ"), async () => {
          const r = await api("delete_preset", p.name);
          if (ok(r)) {
            presets = r.presets;
            if (diy.selected === p.name) diy.selected = null;
            renderPresetList();
            toast(t("diy.deleted", { name: p.name }), "ok");
          }
        });
      });
      ul.appendChild(li);
    }
  }

  function diyArgs() {
    const fx = diyEffects();
    return { fx, effect: fx.length === 1 ? fx[0] : fx,
      width: hasAurora() ? diy.width : hasProjection() ? diy.group : null };
  }
  async function diyApply(btn) {
    const { fx, effect, width } = diyArgs();
    if (!fx.length) { toast(t("diy.pickEffect"), "warn"); return; }
    const r = await busy(btn, api("start_effect", effect, diy.speed, diy.colors.slice(), width));
    if (ok(r)) diy.previewing = true;
  }
  // single mappable effect with <= 8 colours: the strip runs it by itself (api.list_effects "device")
  const runsOnDevice = () => {
    const fx = diyEffects();
    return fx.length === 1 && !!(effects.find((e) => e.id === fx[0]) || {}).device && diy.colors.length <= MAX_COLORS;
  };
  function renderDiyNote() {
    const fx = diyEffects();
    const note = $("#diyRunNote");
    if (!fx.length) { note.textContent = ""; return; }
    const device = runsOnDevice();
    note.textContent = device ? t("diy.noteDevice") : fx.length > 1 ? t("diy.noteMix") : "";
  }
  // re-apply edits while the editor contents are playing (debounced); a strip-run effect is a
  // re-upload that blanks the strip, so it waits longer for the edits to settle
  const diyPlaying = () => diy.previewing && (modeKind() === "effect" || modeKind() === "preset");
  const liveApplyNow = () => { if (diyPlaying() && diyEffects().length) diyApply(); };
  const liveApplyLater = debounce(liveApplyNow, 280);
  const liveApplyDeviceLater = debounce(liveApplyNow, 800);
  const liveApply = () => { if (diyPlaying()) (runsOnDevice() ? liveApplyDeviceLater : liveApplyLater)(); };

  $("#diyTabs").addEventListener("click", (e) => {
    const t = e.target.closest(".tab");
    if (!t || t.dataset.tab === diy.tab) return;
    diy.tab = t.dataset.tab;
    if (diy.tab === "mix" && !diy.mix.length) diy.mix = [diy.simple];
    renderDiyEditor();
    liveApply();
  });
  $("#diyName").addEventListener("input", (e) => { diy.name = e.target.value; });
  $("#diySpeed").addEventListener("input", (e) => { diy.speed = +e.target.value; paintRange(e.target); $("#speedVal").textContent = diy.speed; });
  $("#diySpeed").addEventListener("change", liveApply);
  $("#diyWidth").addEventListener("input", (e) => {
    if (widthIsGroup()) diy.group = +e.target.value; else diy.width = +e.target.value;
    paintRange(e.target);
    $("#widthVal").textContent = t("diy.segs", { n: +e.target.value });
    renderEffectRow(); renderMixList();
  });
  $("#diyWidth").addEventListener("change", liveApply);
  $("#diyApply").addEventListener("click", (e) => diyApply(e.currentTarget));
  $("#diyStop").addEventListener("click", (e) => busy(e.currentTarget, api("stop_mode")));
  $("#diyNew").addEventListener("click", () => {
    Object.assign(diy, { name: t("diy.newName"), tab: "simple", simple: "fade1", mix: [], colors: ["#ff5000", "#3ecfe0"],
      speed: 50, width: 4, group: 2, selected: null, previewing: false });
    renderDiyEditor(); renderPresetList();
    $("#diyName").focus(); $("#diyName").select();
  });
  $("#diyRecommend").addEventListener("click", () => openRecommender({
    maxN: MAX_COLORS, defaultN: Math.max(2, diy.colors.length), primaryLabel: t("rec.use"),
    onUse: (colors) => { diy.colors = colors.slice(0, MAX_COLORS).map(normHex); renderDiyEditor(); liveApply(); },
  }));

  let overwriteArmed = null;
  $("#diySave").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const name = diy.name.trim();
    if (!name) { toast(t("diy.nameFirst"), "warn"); $("#diyName").focus(); return; }
    const { fx, width } = diyArgs();
    if (!fx.length) { toast(t("diy.pickEffect"), "warn"); return; }
    const exists = presets.some((p) => p.name === name);
    if (exists && name !== diy.selected && overwriteArmed !== name) {
      overwriteArmed = name;
      btn.textContent = t("diy.overwriteQ");
      setTimeout(() => { overwriteArmed = null; btn.textContent = t("diy.save"); }, 3500);
      toast(t("diy.exists", { name }), "warn");
      return;
    }
    overwriteArmed = null; btn.textContent = t("diy.save");
    const r = await busy(btn, api("save_preset", name, fx, diy.colors.slice(), diy.speed, width));
    if (ok(r)) {
      presets = r.presets;
      diy.selected = name; diy.name = name;
      renderPresetList();
      toast(t("diy.saved", { name }), "ok");
    }
  });

  async function loadPresets() {
    const r = await api("list_presets");
    if (Array.isArray(r)) presets = r;
  }

  // =====================================================================
  // 情景
  // =====================================================================
  // category order of the Govee app; names: I18N.sceneCategory / I18N.sceneNames
  const CAT_ORDER = ["Natural", "Life", "Emotion", "Festival", "Dance", "Movie", "Games", "Music", "Relaxation"];
  const clean = I18N.clean;
  /** Govee's hints are English; the known ones have their own wording per language. */
  function sceneHint(hint) {
    const h = clean(hint);
    if (!h) return "";
    if (/^siren\s*:/i.test(h)) return t("scenes.hint.siren");
    if (/^sunrise/i.test(h)) return t("scenes.hint.sunrise");
    if (/^sunset/i.test(h)) return t("scenes.hint.sunset");
    return h;
  }
  const sceneIndex = new Map();   // id -> {cat, name, hint} (catalogue data, English)
  function sceneInfo(id) {
    const s = sceneIndex.get(id);
    if (!s) return null;
    const names = I18N.sceneNames(s.cat, s.name);
    return Object.assign({}, names, { hint: sceneHint(s.hint), warn: /^siren\s*:/i.test(clean(s.hint)) });
  }
  const sceneTitle = (id) => (sceneInfo(id) || {}).title || String(id);
  let sceneTab = null;

  async function loadScenes() {
    const r = await api("list_scenes");
    if (!Array.isArray(r)) return;
    const rank = (c) => { const i = CAT_ORDER.indexOf(clean(c)); return i < 0 ? 100 : i; };
    sceneCats = r.slice().sort((a, b) => rank(a.category) - rank(b.category));
    sceneIndex.clear();
    for (const c of sceneCats) for (const s of c.scenes) {
      sceneIndex.set(s.id, { cat: c.category, name: s.name, hint: s.hint });
    }
    if (sceneTab === null && sceneCats.length) sceneTab = sceneCats[0].category;
    renderSceneTabs();
    renderScenes();
  }
  function renderSceneTabs() {
    const box = $("#sceneTabs");
    box.innerHTML = "";
    const searching = !!$("#sceneSearch").value.trim();
    for (const c of sceneCats) {
      box.appendChild(el("button", {
        class: "tab" + (!searching && c.category === sceneTab ? " active" : ""),
        text: I18N.sceneCategory(c.category),
        onclick: () => { sceneTab = c.category; $("#sceneSearch").value = ""; renderSceneTabs(); renderScenes(); },
      }));
    }
  }
  function renderScenes() {
    const grid = $("#sceneGrid");
    grid.innerHTML = "";
    const q = $("#sceneSearch").value.trim().toLowerCase();
    let list = [];
    if (q) {
      for (const id of sceneIndex.keys()) {   // either language matches
        const s = sceneInfo(id);
        if (s.zh.toLowerCase().includes(q) || s.en.toLowerCase().includes(q)) list.push(id);
      }
    } else {
      const cat = sceneCats.find((c) => c.category === sceneTab);
      list = cat ? cat.scenes.map((s) => s.id) : [];
    }
    if (!list.length) grid.appendChild(el("div", { class: "empty", text: q ? t("scenes.notFound", { q }) : t("scenes.empty") }));
    const zh = I18N.lang() === "zh";
    for (const id of list) {
      const s = sceneInfo(id);
      // Chinese shows the English catalogue name underneath; English shows it alone
      const sub = zh && s.zh !== s.en ? s.en : "";
      const hint = s.hint || sub;
      const b = el("button", {
        class: "scene-btn" + (id === lastSceneId ? " applied" : ""), dataset: hint ? { id: String(id), hint } : { id: String(id) },
      }, [
        s.warn ? el("span", { class: "flag", text: "⚡" }) : null,
        el("span", { class: "scene-zh", text: s.title }),
        sub ? el("span", { class: "scene-en", text: sub }) : null,
      ]);
      b.addEventListener("click", async () => {
        const r = await busy(b, api("apply_scene", id));
        if (ok(r)) {
          lastSceneId = id;
          renderSceneApplied(); renderNow(); renderTop();
          toast(t("scenes.applied", { name: s.title }), "ok");
        }
      });
      grid.appendChild(b);
    }
  }
  function renderSceneApplied() {
    $$(".scene-btn").forEach((b) => b.classList.toggle("applied", +b.dataset.id === lastSceneId));
  }
  $("#sceneSearch").addEventListener("input", () => { renderSceneTabs(); renderScenes(); });

  // =====================================================================
  // 屏幕同步（区域模式 = 屏幕 6 竖条，不再需要校准）
  // =====================================================================
  const SCREEN_DEFAULTS = { saturation: 2.0, transition_s: 1.5, gain: [1, 1, 1], tempo: 50 };
  const scr = { monitor: 1, mode: "dominant", saturation: 2.0, transition_s: 1.5, gain: [1, 1, 1], tempo: 50, loaded: false };
  // mode help: t("screen.help.<mode>")
  let monitorsLoaded = false;
  let monitors = null;   // list_monitors result (re-rendered on a language switch)

  function loadScreenSettings() {
    const s = (S.settings && S.settings.screen) || {};
    Object.assign(scr, {
      monitor: s.monitor || 1, mode: SCREEN_MODES.includes(s.mode) ? s.mode : "dominant",
      tempo: typeof s.tempo === "number" ? s.tempo : SCREEN_DEFAULTS.tempo,
      saturation: typeof s.saturation === "number" ? s.saturation : SCREEN_DEFAULTS.saturation,
      transition_s: typeof s.transition_s === "number" ? s.transition_s : SCREEN_DEFAULTS.transition_s,
      gain: Array.isArray(s.gain) && s.gain.length === 3 ? s.gain.slice() : [1, 1, 1],
      loaded: true,
    });
    renderScreenControls();
  }
  function renderScreenControls() {
    const set = (id, v, txt) => { const i = $(id); i.value = v; paintRange(i); $(id + "Val").textContent = txt; };
    set("#scrSat", scr.saturation, "× " + (+scr.saturation).toFixed(2));
    set("#scrTransition", scr.transition_s, t("screen.seconds", { v: (+scr.transition_s).toFixed(1) }));
    set("#scrGainR", scr.gain[0], "× " + (+scr.gain[0]).toFixed(2));
    set("#scrGainG", scr.gain[1], "× " + (+scr.gain[1]).toFixed(2));
    set("#scrGainB", scr.gain[2], "× " + (+scr.gain[2]).toFixed(2));
    set("#scrTempo", scr.tempo, String(Math.round(scr.tempo)));
    $$("#scrMode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === scr.mode));
    $("#scrModeHelp").textContent = t("screen.help." + scr.mode);
    $("#scrTempoField").hidden = !(scr.mode === "fade" || scr.mode === "projection");
    const sel = $("#scrMonitor");
    if (sel.options.length && [...sel.options].some((o) => +o.value === scr.monitor)) sel.value = String(scr.monitor);
  }
  function renderScreenRun() {
    const on = modeKind() === "screen";
    const st = $("#scrState");
    st.classList.toggle("on", on);
    const m = screenModeName(S.settings && S.settings.screen && S.settings.screen.mode) || screenModeName("dominant");
    st.textContent = on ? t("screen.syncing", { mode: m }) : t("common.notRunning");
    $("#scrStop").disabled = !on;
  }

  async function loadMonitors() {
    const r = await api("list_monitors");
    monitors = Array.isArray(r) ? r : [];
    renderMonitors();
    if (!monitors.length) return;
    if (!monitors.some((m) => m.index === scr.monitor)) scr.monitor = (monitors.find((m) => m.primary) || monitors[0]).index;
    $("#scrMonitor").value = String(scr.monitor);
    monitorsLoaded = true;
  }
  function renderMonitors() {
    if (monitors === null) return;   // still loading: the static "loading" option stays
    const sel = $("#scrMonitor");
    sel.innerHTML = "";
    if (!monitors.length) {
      sel.appendChild(el("option", { value: "1", text: t("screen.noDisplay") }));
      return;
    }
    for (const m of monitors) {
      sel.appendChild(el("option", {
        value: String(m.index),
        text: t("screen.displayItem", { i: m.index, w: m.width, h: m.height }) + (m.primary ? t("screen.primary") : ""),
      }));
    }
    sel.value = String(scr.monitor);
  }

  const screenOpts = () => ({
    monitor: scr.monitor, mode: scr.mode, saturation: +scr.saturation, transition_s: +scr.transition_s,
    gain: scr.gain.map(Number), tempo: +scr.tempo,
  });
  async function startScreen(btn) {
    await busy(btn, api("start_screen_sync", screenOpts()));
  }
  const restartScreenLater = debounce(() => {
    if (modeKind() === "screen") api("start_screen_sync", screenOpts());
  }, 300);
  const restartScreenIfRunning = () => { if (modeKind() === "screen") restartScreenLater(); };

  $("#scrMonitor").addEventListener("change", (e) => { scr.monitor = +e.target.value; restartScreenIfRunning(); });
  $("#scrMode").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || b.dataset.mode === scr.mode) return;
    scr.mode = b.dataset.mode;
    renderScreenControls();
    restartScreenIfRunning();
  });
  function bindScreenSlider(id, apply) {
    const i = $(id);
    i.addEventListener("input", () => { apply(+i.value); renderScreenControls(); });
    i.addEventListener("change", restartScreenIfRunning);
  }
  bindScreenSlider("#scrSat", (v) => { scr.saturation = v; });
  bindScreenSlider("#scrTransition", (v) => { scr.transition_s = v; });
  bindScreenSlider("#scrTempo", (v) => { scr.tempo = v; });
  bindScreenSlider("#scrGainR", (v) => { scr.gain[0] = v; });
  bindScreenSlider("#scrGainG", (v) => { scr.gain[1] = v; });
  bindScreenSlider("#scrGainB", (v) => { scr.gain[2] = v; });
  $("#scrReset").addEventListener("click", () => {
    Object.assign(scr, { saturation: SCREEN_DEFAULTS.saturation, transition_s: SCREEN_DEFAULTS.transition_s,
      gain: SCREEN_DEFAULTS.gain.slice(), tempo: SCREEN_DEFAULTS.tempo });
    renderScreenControls();
    restartScreenIfRunning();
  });
  $("#scrStart").addEventListener("click", (e) => startScreen(e.currentTarget));
  $("#scrStop").addEventListener("click", (e) => busy(e.currentTarget, api("stop_mode")));

  pageEnter.screen = () => {
    if (S && !scr.loaded) loadScreenSettings();
    if (!monitorsLoaded) loadMonitors().then(renderScreenControls);
  };

  // =====================================================================
  // 音乐
  // =====================================================================
  let musicSrc = null;   // "pal:<name>" | "diy:<name>"
  let musicSens = null;
  function musicSources() {
    const out = [];
    const pals = (S && S.palettes) || {};
    for (const name of Object.keys(pals)) out.push({ key: "pal:" + name, name: palName(name), kind: t("music.kindPalette"), colors: pals[name] });
    for (const p of presets) out.push({ key: "diy:" + p.name, name: p.name, kind: t("music.kindDiy"), colors: p.colors || [] });
    return out;
  }
  function renderMusicSources() {
    if (!S) return;
    const box = $("#musicSources");
    box.innerHTML = "";
    const list = musicSources();
    if (!musicSrc || !list.some((s) => s.key === musicSrc)) musicSrc = "pal:" + S.active_palette;
    for (const s of list) {
      box.appendChild(el("button", {
        class: "src" + (s.key === musicSrc ? " selected" : ""),
        onclick: () => { musicSrc = s.key; renderMusicSources(); restartMusicIfRunning(); },
      }, [
        el("span", { class: "src-name" }, [s.name, el("span", { class: "src-kind", text: s.kind })]),
        el("span", { class: "pal-bar" }, s.colors.map((c) => el("i", { style: "background:" + c }))),
      ]));
    }
  }
  function musicColors() {
    const s = musicSources().find((x) => x.key === musicSrc);
    return s && s.colors.length ? s.colors.slice() : null;
  }
  function renderMusicSens() {
    const i = $("#musicSens");
    i.value = musicSens; paintRange(i);
    $("#musicSensVal").textContent = "× " + (+musicSens).toFixed(1);
  }
  function renderMusicRun() {
    const on = modeKind() === "music";
    const st = $("#musicState");
    st.classList.toggle("on", on);
    st.textContent = on ? t("music.running") : t("common.notRunning");
    $("#musicStop").disabled = !on;
  }
  const musicOpts = () => ({ sensitivity: +musicSens, colors: musicColors() });
  const restartMusicLater = debounce(() => { if (modeKind() === "music") api("start_music", musicOpts()); }, 300);
  const restartMusicIfRunning = () => { if (modeKind() === "music") restartMusicLater(); };
  $("#musicSens").addEventListener("input", (e) => { musicSens = +e.target.value; renderMusicSens(); });
  $("#musicSens").addEventListener("change", restartMusicIfRunning);
  $("#musicStart").addEventListener("click", (e) => busy(e.currentTarget, api("start_music", musicOpts())));
  $("#musicStop").addEventListener("click", (e) => busy(e.currentTarget, api("stop_mode")));
  pageEnter.music = () => { renderMusicSources(); };

  // =====================================================================
  // 设置
  // =====================================================================
  function renderSettings() {
    if (!S) return;
    const sw = $("#autostart");
    sw.setAttribute("aria-checked", S.autostart ? "true" : "false");
    const ps = !S.settings || S.settings.power_sync !== false;
    $("#powerSync").setAttribute("aria-checked", ps ? "true" : "false");
    renderTransition();
    renderColorCal();
    renderDevice();
    const p = S.profile || {};
    const facts = [
      [t("facts.model"), t("facts.modelValue")],
      [t("facts.segments"), t("facts.segmentsValue", { n: p.segment_count || N() })],
      [t("facts.brightness"), t("facts.brightnessValue", { max: p.brightness_max != null ? p.brightness_max : "?" })],
      [t("facts.order"), p.bit_order === "start_at_end" ? t("facts.orderEnd") : t("facts.orderPlug")],
      [t("facts.rate"), t("facts.rateValue", { n: p.max_packets_per_s != null ? p.max_packets_per_s : "?" })],
      [t("facts.sceneVersion"), String(p.scene_cmd_version != null ? p.scene_cmd_version : "?")],
    ];
    const dl = $("#facts");
    dl.innerHTML = "";
    for (const [k, v] of facts) dl.append(el("dt", { text: k }), el("dd", { text: v }));
    const about = $("#about");
    about.innerHTML = "";
    about.append(el("dt", { text: t("about.version") }), el("dd", { text: S.version || "?" }));
    if (S.data_dir) about.append(el("dt", { text: t("about.dataDir") }), el("dd", { text: S.data_dir }));
  }
  // ---- 设备: current strip, 搜索设备 (pick one), 忘记设备 (back to auto-discovery) ----
  let devFound = null; // last 搜索设备 result: [{name, address, rssi}] (null = not searched)
  const devName = (d) => (d && d.name) || t("device.unnamed");
  function renderDevice() {
    const d = S && S.device;
    const cur = $("#devCurrent");
    cur.textContent = d ? t("device.current", { name: devName(d), address: d.address }) : t("device.none");
    cur.classList.toggle("none", !d);
    $("#devForget").disabled = !d;
    const others = (S && S.device_others) || [];
    const o = $("#devOthers");
    o.hidden = !others.length;
    if (others.length) o.textContent = t("device.others", { list: others.map((x) => devName(x) + " (" + x.address + ")").join(", ") });
    renderDeviceList();
  }
  function renderDeviceList() {
    const list = $("#devList");
    list.innerHTML = "";
    list.hidden = devFound === null;
    if (devFound === null) return;
    if (!devFound.length) {
      list.append(el("div", { class: "muted small", text: t("device.foundNone") }));
      return;
    }
    const current = S && S.device && S.device.address;
    for (const d of devFound) {
      const inUse = d.address === current;
      const btn = el("button", {
        class: "btn btn-sm" + (inUse ? " btn-ghost" : " btn-primary"), disabled: inUse,
        text: inUse ? t("device.inUse") : t("device.use"),
        onclick: async (e) => {
          const r = await busy(e.currentTarget, api("select_device", d.address, d.name));
          if (ok(r)) {
            toast(t("device.selected", { name: devName(d) }), "ok");
            await refreshState();
          }
          renderDeviceList();
        },
      });
      list.append(el("div", { class: "device-row" }, [
        el("div", {}, [
          el("div", { class: "dev-name", text: devName(d) }),
          el("div", { class: "dev-addr", text: d.address }),
        ]),
        el("div", { class: "spacer" }),
        el("span", { class: "dev-rssi", text: d.rssi != null ? t("device.signal", { rssi: d.rssi }) : t("device.signalUnknown") }),
        btn,
      ]));
    }
  }
  $("#devScan").addEventListener("click", async (e) => {
    const list = $("#devList");
    list.hidden = false;
    list.innerHTML = "";
    list.append(el("div", { class: "muted small", text: t("device.scanning") }));
    const r = await busy(e.currentTarget, api("scan_devices"));
    devFound = ok(r) && Array.isArray(r.devices) ? r.devices : null;
    renderDeviceList();
  });
  $("#devForget").addEventListener("click", async (e) => {
    const r = await busy(e.currentTarget, api("forget_device"));
    if (ok(r)) {
      toast(t("device.forgotten"), "ok");
      await refreshState();
    }
  });
  $("#autostart").addEventListener("click", async (e) => {
    const sw = e.currentTarget;
    const want = sw.getAttribute("aria-checked") !== "true";
    const r = await busy(sw, api("set_autostart", want));
    if (ok(r)) {
      sw.setAttribute("aria-checked", r.autostart ? "true" : "false");
      toast(r.autostart ? t("settings.autostartOn") : t("settings.autostartOff"), "ok");
    }
  });
  function transitionSetting() {
    const t = (S && S.settings && S.settings.transition) || {};
    return { rate: [3, 5, 8, 10].includes(t.rate) ? t.rate : 5, mix: t.mix === "perceptual" ? "perceptual" : "linear" };
  }
  function renderTransition() {
    const t = transitionSetting();
    $$("#trRate button").forEach((b) => b.classList.toggle("active", +b.dataset.rate === t.rate));
    $$("#trMix button").forEach((b) => b.classList.toggle("active", b.dataset.mix === t.mix));
  }
  async function setTransition(btn, opts) {
    const r = await busy(btn, api("set_transition", opts));
    if (ok(r)) {
      if (S && S.settings) S.settings.transition = r.transition;
      renderTransition();
    }
  }
  $("#trRate").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || +b.dataset.rate === transitionSetting().rate) return;
    setTransition(b, { rate: +b.dataset.rate });
  });
  $("#trMix").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || b.dataset.mix === transitionSetting().mix) return;
    setTransition(b, { mix: b.dataset.mix });
  });
  // ---- 颜色校准（白平衡）: sliders apply live (debounced), test colours, 完成 ----
  const CAL_KEYS = { r: "#calR", g: "#calG", b: "#calB" };
  let calPending = null;   // slider values not yet acknowledged by the backend
  let calTestColor = null; // hex of the test colour shown (lower case)
  function calSetting() {
    const c = (S && S.settings && S.settings.color_cal) || {};
    const v = (x) => (Number.isFinite(x) ? Math.max(0, Math.min(100, Math.round(x))) : 100);
    return { r: v(c.r), g: v(c.g), b: v(c.b) };
  }
  function renderColorCal() {
    const c = calPending || calSetting();
    for (const k of Object.keys(CAL_KEYS)) {
      const i = $(CAL_KEYS[k]);
      i.value = c[k];
      paintRange(i);
      $(CAL_KEYS[k] + "Val").textContent = c[k] + "%";
    }
    const testing = !!(S && S.color_test);
    if (testing && S.mode && S.mode.kind === "color_test" && S.mode.args) calTestColor = S.mode.args.color;
    if (!testing) calTestColor = null;
    $$("#calTests .chip").forEach((b) => b.classList.toggle("active", testing && b.dataset.color.toLowerCase() === calTestColor));
    $("#calDone").disabled = !testing;
    $("#calState").textContent = testing ? t("settings.calTesting") : t("settings.calIdle");
  }
  const sendColorCal = debounce(async () => {
    if (!calPending) return;
    const want = calPending;
    const r = await api("set_color_cal", want);
    if (ok(r) && S && S.settings) S.settings.color_cal = r.color_cal;
    if (calPending === want) calPending = null;
    renderColorCal();
  }, 200);
  for (const k of Object.keys(CAL_KEYS)) {
    $(CAL_KEYS[k]).addEventListener("input", (e) => {
      calPending = Object.assign({}, calPending || calSetting(), { [k]: +e.target.value });
      renderColorCal();
      sendColorCal();
    });
  }
  $("#calReset").addEventListener("click", async (e) => {
    calPending = null;
    const r = await busy(e.currentTarget, api("reset_color_cal"));
    if (ok(r)) {
      if (S && S.settings) S.settings.color_cal = r.color_cal;
      toast(t("settings.calReset"), "ok");
    }
    renderColorCal();
  });
  $("#calTests").addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const r = await busy(b, api("test_color", b.dataset.color));
    if (ok(r)) {
      calTestColor = r.color;
      if (S) S.color_test = true;
    }
    renderColorCal();
  });
  $("#calDone").addEventListener("click", async (e) => {
    const r = await busy(e.currentTarget, api("end_color_test"));
    if (ok(r)) {
      calTestColor = null;
      if (S) S.color_test = false;
      toast(r.restored ? t("settings.calRestored") : t("settings.calFinished"), "ok");
    }
    renderColorCal();
  });

  $("#powerSync").addEventListener("click", async (e) => {
    const sw = e.currentTarget;
    const want = sw.getAttribute("aria-checked") !== "true";
    const r = await busy(sw, api("set_power_sync", want));
    if (ok(r)) {
      sw.setAttribute("aria-checked", r.power_sync ? "true" : "false");
      toast(r.power_sync ? t("settings.powerSyncOn") : t("settings.powerSyncOff"), "ok");
    }
  });

  // ---- 语言 / Language: 自动 / 中文 / English, applied at once, saved by the backend ----
  let langSetting = "auto";
  function renderLanguage() {
    $$("#langCtl button").forEach((b) => b.classList.toggle("active", b.dataset.lang === langSetting));
  }
  /** Switch every visible string to `lang` ("zh" | "en"): static HTML + dynamic parts. */
  function applyLanguage(lang) {
    const prev = I18N.lang();
    const oldName = t("diy.newName");
    if (I18N.setLang(lang) === prev) { renderLanguage(); return; }
    I18N.apply(document);
    if (!diy.selected && diy.name === oldName) diy.name = t("diy.newName");
    if (overwriteArmed === null) $("#diySave").textContent = t("diy.save");
    buildStrip(N(), true);
    renderDiyEditor();
    renderPresetList();
    renderMonitors();
    renderLanguage();
    if (sceneCats.length) { renderSceneTabs(); renderScenes(); }
    if (S) {
      renderTop();
      renderNow();
      renderSegBig();
      renderPalettes();
      renderMusicSources();
      renderMusicSens();
      renderScreenControls();
      renderScreenRun();
      renderMusicRun();
      renderSettings();
    }
  }
  function useLanguageInfo(info) {
    if (!info || !info.resolved) return false;
    langSetting = info.setting || "auto";
    applyLanguage(info.resolved);
    renderLanguage();
    return true;
  }
  $("#langCtl").addEventListener("click", async (e) => {
    const b = e.target.closest("button");
    if (!b || b.dataset.lang === langSetting) return;
    const r = await busy(b, api("set_language", b.dataset.lang));
    if (ok(r)) useLanguageInfo(r);
  });

  // =====================================================================
  // startup
  // =====================================================================
  function firstState() {
    initSegColors();
    renderSegBig();
    renderPalettes();
    musicSens = (S.settings && S.settings.music && S.settings.music.sensitivity) || 1.0;
    renderMusicSens();
    loadScreenSettings();
    renderScreenRun();
    renderMusicRun();
    renderSettings();
  }

  // static UI that does not need the backend
  segPicker = ColorPicker($("#segPicker"), {
    color: "#ff5000", quick: QUICK_COLORS,
    onCommit: (c) => { if (segSel.size) paintSelection(c); },
  });
  $$(".range").forEach(paintRange);
  buildStrip(15);
  renderDiyEditor();

  ready.then(async () => {
    // the backend's language (setting + Windows display language) wins over the guess
    const info = await api("get_language");
    if (!useLanguageInfo(info)) applyLanguage(I18N.guess());
    const [state, fx] = await Promise.all([api("get_state"), api("list_effects")]);
    if (Array.isArray(fx)) effects = fx;
    if (state && state.segment_count) window.onState(state);
    await loadPresets();
    // load the running preset (or the first one) into the editor, without applying it
    const runningName = modeKind() === "preset" ? (S.mode.args || {}).name : null;
    const first = presets.find((p) => p.name === runningName) || presets[0];
    if (first) loadPreset(first);
    renderDiyEditor();
    renderPresetList();
    renderMusicSources();
    renderNow();
    loadScenes();
  });
})();
