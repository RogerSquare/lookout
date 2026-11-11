// Lookout Hub setup page. Talks to the hub's admin API with the session cookie; every change
// carries X-Lookout-CSRF so another website can't make it on your behalf.
"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", credentials: "same-origin",
                 headers: { "X-Lookout-CSRF": "1" } };
  if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, init);
  if (r.status === 401 && !opts.allow401) { showAuth(); throw new Error("Sign in again"); }
  const text = await r.text();
  if (!r.ok) throw new Error(text.replace(/^\d{3}: /, "") || r.statusText);
  return text ? JSON.parse(text) : {};
}

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.remove("show"), 2600);
}

function seg(el, value, onPick) {
  $$("button", el).forEach(b => {
    b.setAttribute("aria-checked", String(b.dataset.v === value));
    if (onPick) b.onclick = () => { if (b.getAttribute("aria-checked") !== "true") onPick(b.dataset.v); };
  });
}

/* ---------- sign in / first run ---------- */
let claimed = true;
async function boot() {
  const s = await api("/api/admin/session", { allow401: true });
  claimed = s.claimed;
  if (s.signed_in) return showApp();
  showAuth();
}

// ?setup=<code> comes from the QR code in the hub's first-start log
const setupFromLink = new URLSearchParams(location.search).get("setup");
if (setupFromLink) history.replaceState(null, "", location.pathname + location.hash);

// Opened from the setup link on a computer: offer to carry on on a phone instead (the QR code
// opens this page there, with the code). Phones go straight to the form.
const onPhone = matchMedia("(pointer: coarse)").matches && innerWidth < 700;
async function showHandoff() {
  try {
    const r = await api("/api/admin/setup-qr", { method: "POST", body: { code: setupFromLink }, allow401: true });
    $("#handoff-qr").innerHTML = r.qr_svg;          // SVG markup made by the hub
    $("#handoff").hidden = false;
    $("#auth-form").classList.add("handing-off");
    // Finished on the phone: this page becomes the normal sign-in
    const wait = setInterval(async () => {
      if ($("#handoff").hidden) return clearInterval(wait);
      const st = await api("/api/admin/session", { allow401: true }).catch(() => null);
      if (st?.claimed) {
        clearInterval(wait);
        claimed = true;
        $("#handoff").hidden = true;
        $("#auth-form").classList.remove("handing-off");
        showAuth();
        $("#auth-lede").textContent = "Set up on your phone. Sign in here with the same password.";
      }
    }, 3000);
  } catch { /* wrong or used-up code: just show the form */ }
}
$("#handoff-here").onclick = () => {
  $("#handoff").hidden = true;
  $("#auth-form").classList.remove("handing-off");
  $("#password").focus();
};

function showAuth() {
  $("#app").hidden = true; $("#auth").hidden = false;
  if (!claimed && setupFromLink) $("#code").value = setupFromLink;
  if (!claimed && setupFromLink && !onPhone && !showAuth.offered) { showAuth.offered = true; showHandoff(); }
  $("#code-field").hidden = claimed; $("#confirm-field").hidden = claimed;
  $("#auth-title").textContent = claimed ? "Sign in to Lookout Hub" : "Set up Lookout Hub";
  $("#auth-lede").textContent = claimed ? "Enter the hub's password."
    : "Enter the setup code from the hub's log, then choose a password for this page.";
  $("#auth-submit").textContent = claimed ? "Sign in" : "Set password";
  $("#password").autocomplete = claimed ? "current-password" : "new-password";
  (claimed || setupFromLink ? $("#password") : $("#code")).focus();
}

$("#auth-form").addEventListener("submit", async e => {
  e.preventDefault();
  const err = $("#auth-error"); err.textContent = "";
  const password = $("#password").value;
  try {
    if (claimed) {
      await api("/api/admin/login", { method: "POST", body: { password }, allow401: true });
    } else {
      if (password !== $("#confirm").value) { err.textContent = "The two passwords don't match."; return; }
      await api("/api/admin/setup", { method: "POST", body: { code: $("#code").value, password }, allow401: true });
      claimed = true;
    }
    $("#password").value = $("#confirm").value = "";
    showApp();
  } catch (x) { err.textContent = x.message; }
});

/* ---------- app shell ---------- */
const state = { config: null, cams: {}, alerts: { enabled: true, cameras: {} }, detector: null, source: null };

async function showApp() {
  $("#auth").hidden = true; $("#app").hidden = false;
  route();
  await refresh();
  loadApps();
  loadHubForm();
  listen();
  loadFound(true);
  loadBridge();
  setInterval(refreshTiles, 2000);
}

function route() {
  const view = (location.hash || "#cameras").slice(1);
  $$(".view").forEach(v => v.hidden = v.id !== `view-${view}`);
  $$(".rail a").forEach(a => a.setAttribute("aria-current", a.dataset.view === view ? "page" : "false"));
}
addEventListener("hashchange", () => { if ($("#cam-sheet").open) $("#cam-sheet").close(); route(); });

async function refresh() {
  const [config, st, det, src, hist] = await Promise.all([api("/api/admin/config"), api("/api/state"),
    api("/api/admin/detector"), api("/api/admin/source"), api("/api/admin/history")]);
  state.config = config; state.detector = det; state.source = src; state.alerts = st.alerts; state.history = hist;
  state.cams = Object.fromEntries(st.cameras.map(c => [c.id, c]));
  renderAll();
}

function renderAll() { renderWall(); renderDetection(); renderAlerts(); renderHub(); renderRail(); }

/* ---------- apps (pairing) ---------- */
const platformNames = { ios: "iPhone", watchos: "Apple Watch", android: "Android", windows: "Windows", macos: "Mac", other: "App" };
// The Settings-style tile for each kind of device (glyphs drawn here, white on the color)
const deviceIcons = {
  ios: ["var(--indigo)", `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8.2 1.5h7.6a2.7 2.7 0 0 1 2.7 2.7v15.6a2.7 2.7 0 0 1-2.7 2.7H8.2a2.7 2.7 0 0 1-2.7-2.7V4.2a2.7 2.7 0 0 1 2.7-2.7zm0 1.8a.9.9 0 0 0-.9.9v15.6c0 .5.4.9.9.9h7.6c.5 0 .9-.4.9-.9V4.2a.9.9 0 0 0-.9-.9h-1.3c-.2.6-.6.9-1.2.9h-1.4c-.6 0-1-.3-1.2-.9z"/></svg>`],
  watchos: ["var(--blue)", `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 1.5h6l.7 3.1A3.5 3.5 0 0 1 18.5 8v8a3.5 3.5 0 0 1-2.8 3.4L15 22.5H9l-.7-3.1A3.5 3.5 0 0 1 5.5 16V8a3.5 3.5 0 0 1 2.8-3.4zM9 6.3c-1 0-1.7.7-1.7 1.7v8c0 1 .7 1.7 1.7 1.7h6c1 0 1.7-.7 1.7-1.7V8c0-1-.7-1.7-1.7-1.7z"/></svg>`],
};
deviceIcons.other = deviceIcons.android = deviceIcons.windows = deviceIcons.macos = ["var(--gray)", deviceIcons.ios[1]];

function ago(t) {
  if (!t) return "not seen yet";
  const s = Date.now() / 1000 - t;
  if (s < 90) return "seen just now";
  if (s < 3600) return `seen ${Math.round(s / 60)} min ago`;
  if (s < 86400) return `seen ${Math.round(s / 3600)} h ago`;
  return `seen ${new Date(t * 1000).toLocaleDateString()}`;
}
async function loadApps() {
  const d = await api("/api/admin/devices");
  $("#legacy-sw").checked = d.allow_legacy_token;
  $("#devices-empty").hidden = d.devices.length > 0;
  const names = Object.fromEntries(d.devices.map(x => [x.id, x.name]));
  $("#devices").replaceChildren(...d.devices.map(dev => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="ico"></span><span class="grow"><b></b><br><span class="muted"></span></span><button class="danger">Remove</button>`;
    const icon = deviceIcons[dev.platform] || deviceIcons.ios;
    $(".ico", li).style.setProperty("--c", icon[0]); $(".ico", li).innerHTML = icon[1];
    $("b", li).textContent = dev.name;
    const how = dev.via && names[dev.via] ? `, added by ${names[dev.via]}` : "";
    $(".muted", li).textContent = `${platformNames[dev.platform] || "App"}, paired ${new Date(dev.paired * 1000).toLocaleDateString()}${how}, ${ago(dev.last_seen)}`;
    $("button", li).onclick = async () => {
      if (!confirm(`Remove ${dev.name}? It stops seeing cameras and getting alerts until it's paired again.`)) return;
      await api(`/api/admin/devices/${dev.id}`, { method: "DELETE" }); toast(`Removed ${dev.name}`); loadApps();
    };
    return li;
  }));
}
let pairTimer;
$("#pair-btn").onclick = async () => {
  const p = await api("/api/admin/pairing", { method: "POST" });
  $("#pair-qr").innerHTML = p.qr_svg;            // SVG markup made by the hub
  $("#pair-open").href = p.link;                  // lookout://pair?... opens the app on this phone
  $("#pair-code").textContent = p.code.replace(/(\d{3})(\d{3})/, "$1 $2");
  $("#pair-panel").hidden = false;
  clearInterval(pairTimer);
  const tick = () => {
    const left = Math.round(p.expires - Date.now() / 1000);
    if (left <= 0) { $("#pair-expiry").textContent = "This code has expired."; $("#pair-panel").classList.add("expired"); clearInterval(pairTimer); return; }
    $("#pair-panel").classList.remove("expired");
    $("#pair-expiry").textContent = `Works once, for the next ${Math.ceil(left / 60)} min.`;
  };
  tick(); pairTimer = setInterval(tick, 5000);
};
$("#pair-done").onclick = () => { $("#pair-panel").hidden = true; clearInterval(pairTimer); };
$("#legacy-sw").onchange = async e => {
  await api("/api/admin/settings", { method: "PUT", body: { allow_legacy_token: e.target.checked } });
  toast(e.target.checked ? "Shared app key accepted" : "Shared app key turned off");
};

function renderRail() {
  const cams = Object.values(state.cams);
  const live = cams.filter(c => c.connected).length;
  $("#rail-status").textContent = `${live} of ${cams.length} cameras live` +
    (state.source?.type === "frigate" ? " · Frigate" : "");
}

/* ---------- cameras ---------- */
function camState(c) { return c.person ? "person" : c.connected ? "live" : "offline"; }
const stateWords = { person: "Person now", live: "Live", offline: "Offline" };   // as in the iPhone app

function renderWall() {
  const wall = $("#wall");
  const cams = state.config.cameras;
  $("#wall-empty").hidden = cams.length > 0;
  const live = Object.values(state.cams).filter(c => c.connected).length;
  $("#cam-summary").textContent = cams.length ? `${live} of ${cams.length} live` : "";
  wall.replaceChildren(...cams.map(cfg => {
    const c = state.cams[cfg.id] || { connected: false };
    const t = document.createElement("button");
    t.className = "tile"; t.dataset.id = cfg.id;
    const st = camState(c);
    t.dataset.state = st; t.classList.toggle("offline", st === "offline");
    t.setAttribute("aria-label", `${cfg.name}, ${stateWords[st]}. Edit camera`);
    t.innerHTML = `<span class="pic"><img alt=""><span class="none">No picture</span><span class="role"></span></span><span class="cap"><b></b><span class="state"><span class="dot"></span><span class="sw"></span></span></span>`;
    $("b", t).textContent = cfg.name;
    $(".sw", t).textContent = stateWords[st];
    const role = $(".role", t);
    role.textContent = cfg.role === "door" ? "Door" : ""; role.hidden = cfg.role !== "door";
    $("img", t).src = `/api/cameras/${encodeURIComponent(cfg.id)}/live.jpg?t=${Date.now()}`;
    $("img", t).onerror = e => e.target.removeAttribute("src");
    t.onclick = () => openSheet(cfg);
    return t;
  }));
}

function updateTile(id) {
  const t = $(`.tile[data-id="${CSS.escape(id)}"]`); const c = state.cams[id];
  if (!t || !c) return;
  const st = camState(c);
  t.dataset.state = st; t.classList.toggle("offline", st === "offline");
  $(".sw", t).textContent = stateWords[st];
}

function refreshTiles() {
  if ($("#view-cameras").hidden || document.hidden) return;
  $$(".tile").forEach(t => {
    if (!state.cams[t.dataset.id]?.connected) return;
    const img = new Image();
    img.onload = () => { $("img", t).src = img.src; };
    img.src = `/api/cameras/${encodeURIComponent(t.dataset.id)}/live.jpg?t=${Date.now()}`;
  });
}

$("#add-btn").onclick = () => openSheet(null);
$("#find-btn").onclick = async () => {
  const box = $("#found"); box.hidden = false; box.textContent = "Looking for cameras on your network…";
  try {
    const r = await api("/api/admin/discover");
    if (!r.cameras.length) { box.textContent = r.note || "No cameras answered. Add one with its stream address instead."; return; }
    box.replaceChildren(Object.assign(document.createElement("span"), { textContent: "Found:", className: "muted" }),
      ...r.cameras.map(f => {
        const b = document.createElement("button");
        b.textContent = f.name || f.url;
        b.onclick = () => openSheet(null, { name: f.name || "", url: f.url });
        return b;
      }));
  } catch (x) { box.textContent = x.message; }
};

/* cameras Frigate has that the hub doesn't */
async function loadFound(scan) {
  try { state.found = await api(`/api/admin/found${scan ? "?scan=1" : ""}`); } catch { state.found = null; }
  renderFound();
}
function renderFound() {
  const f = state.found, box = $("#frigate-found");
  box.hidden = !f || !f.available || (!f.cameras.length && !f.auto);
  if (box.hidden) return;
  $("#auto-add").checked = f.auto;
  $("#frigate-found-list").replaceChildren(...f.cameras.map(c => {
    const row = document.createElement("div"); row.className = "row";
    row.innerHTML = `<span class="grow"><b></b><br><span class="muted"></span></span><button class="primary">Add</button><button>Not now</button>`;
    $("b", row).textContent = c.name; $(".muted", row).textContent = c.url;
    const [add, skip] = row.querySelectorAll("button");
    add.onclick = async () => { try { await api(`/api/admin/found/${encodeURIComponent(c.id)}/add`, { method: "POST" }); toast(`${c.name} added`); await refresh(); await loadFound(); } catch (x) { toast(x.message); } };
    skip.onclick = async () => { try { await api(`/api/admin/found/${encodeURIComponent(c.id)}/dismiss`, { method: "POST" }); await loadFound(); } catch (x) { toast(x.message); } };
    return row;
  }));
}
$("#auto-add").onchange = async e => {
  try { await api("/api/admin/settings", { method: "PUT", body: { auto_add_cameras: e.target.checked } }); await loadFound(true); }
  catch (x) { toast(x.message); e.target.checked = !e.target.checked; }
};

/* camera bridge: controls, with a test button for each */
async function loadBridge(refresh) {
  try { state.bridge = await api(`/api/admin/bridge${refresh ? "?refresh=1" : ""}`); } catch (x) { state.bridge = null; $("#br-status").textContent = x.message; }
  renderBridge();
}
const testActions = {
  sleep: [["Sleep", { action: "sleep" }], ["Wake", { action: "wake" }]],
  night_vision: [["Night vision auto", { action: "night_vision", value: "auto" }]],
  status_light: [["Status light off", { action: "status_light", value: "off" }], ["On", { action: "status_light", value: "on" }]],
  ptz: [["◀", { pan: -1 }], ["▶", { pan: 1 }]],
};
function renderBridge() {
  const b = state.bridge;
  if (!b) return;
  $("#bridge-note").textContent = b.enabled ? `${Object.keys(b.bridge_cameras).length} cameras on the bridge.` : "Off.";
  if (!document.activeElement?.closest?.("#bridge-form")) { $("#br-url").value = b.url || ""; $("#br-key").value = b.api_key || ""; }
  $("#br-off").hidden = !b.enabled;
  $("#bridge-cams").replaceChildren(...(b.enabled ? state.config.cameras : []).map(cfg => {
    const info = b.cameras[cfg.id] || { caps: [] };
    const row = document.createElement("div"); row.className = "row";
    row.innerHTML = `<span class="grow"><b></b><br><span class="muted"></span></span>`;
    $("b", row).textContent = cfg.name || cfg.id;
    $(".muted", row).textContent = info.caps.length ? (info.asleep ? "Asleep · " : "") + info.caps.join(", ").replaceAll("_", " ")
                                                    : `Not on the bridge (looked for "${info.bridge || cfg.id}")`;
    for (const cap of info.caps) for (const [label, body] of testActions[cap] || []) {
      const btn = document.createElement("button"); btn.textContent = label;
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          if ("pan" in body) await api(`/api/admin/cameras/${encodeURIComponent(cfg.id)}/ptz`, { method: "POST", body });
          else await api(`/api/admin/cameras/${encodeURIComponent(cfg.id)}/control`, { method: "POST", body });
          toast(`${cfg.name || cfg.id}: ${label}`); await loadBridge();
        } catch (x) { toast(x.message); }
        btn.disabled = false;
      };
      row.append(btn);
    }
    return row;
  }));
}
$("#bridge-form").addEventListener("submit", async e => {
  e.preventDefault();
  const st = $("#br-status"); st.textContent = "Connecting…";
  try {
    const r = await api("/api/admin/bridge", { method: "PUT", body: { url: $("#br-url").value.trim(), api_key: $("#br-key").value } });
    st.textContent = ""; toast(`Camera bridge connected (${r.cameras.length} cameras)`); await loadBridge();
  } catch (x) { st.textContent = x.message; }
});
$("#br-off").onclick = async () => {
  try { await api("/api/admin/bridge", { method: "PUT", body: {} }); toast("Camera controls off"); await loadBridge(); }
  catch (x) { toast(x.message); }
};

/* camera sheet + zone editor */
const sheet = $("#cam-sheet");
let editing = null, zone = [], role = "other";

function openSheet(cfg, prefill = {}) {
  editing = cfg;
  const c = cfg || {};
  $("#sheet-title").textContent = cfg ? `Edit ${cfg.name}` : "Add camera";
  $("#c-name").value = c.name || prefill.name || "";
  $("#c-id").value = c.id || "";
  $("#c-id").readOnly = !!cfg;
  $("#c-url").value = c.url || prefill.url || "";
  $("#c-url").placeholder = c.detect_url && !c.url ? `Reads ${c.detect_url}` : "rtsp://user:password@192.168.1.30:554/stream1";
  $("#c-sub").value = c.sub_url || "";
  $("#c-mh").value = c.min_person_height || 0;
  $("#c-test-out").textContent = ""; $("#c-error").textContent = "";
  $("#c-delete").hidden = !cfg;
  zone = (c.zone || []).map(p => [...p]);
  role = c.role || "other";
  const p = c.ptz || {};
  $("#ptz-on").checked = !!c.ptz;
  $("#ptz-fields").hidden = !c.ptz;
  $("#ptz-url").value = p.url || ""; $("#ptz-cam").value = p.camera || ""; $("#ptz-key").value = p.api_key || "";
  $("#ptz-test").hidden = !c.ptz;                 // test after saving: the hub moves it with the saved settings
  $("#ptz-out").textContent = "";
  seg($("#role-seg"), role, v => { role = v; seg($("#role-seg"), role); });
  const frigate = state.source?.type === "frigate";
  $("#zone-block").classList.toggle("zone-block-off", frigate);
  $("#zone-help").textContent = frigate
    ? "Frigate decides who counts on this camera. Set zones and sizes in Frigate."
    : "Click the picture to outline the area people must stand in. Leave it empty to use the whole picture.";
  $("#zone-clear").disabled = $("#c-mh").disabled = frigate;
  const img = $("#zone-img");
  img.removeAttribute("src");
  $(".zone-wrap").style.aspectRatio = "16 / 9"; $(".zone-wrap").style.width = "";
  img.onload = () => {
    const w = $(".zone-wrap"), ratio = img.naturalWidth / img.naturalHeight;
    w.style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
    w.style.width = `min(100%, calc(52vh * ${ratio.toFixed(4)}))`;    // tall cameras stay on screen
    requestAnimationFrame(drawZone);
  };
  if (cfg) img.src = `/api/cameras/${encodeURIComponent(cfg.id)}/live.jpg?t=${Date.now()}`;
  updateMh();
  sheet.showModal();
  requestAnimationFrame(drawZone);
}

$("#c-name").addEventListener("input", () => {
  if (!editing) $("#c-id").value = $("#c-name").value.toLowerCase().trim().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 32);
});
$("#sheet-close").onclick = $("#c-cancel").onclick = () => sheet.close();

function updateMh() {
  const v = Number($("#c-mh").value);
  $("#mh-out").textContent = v ? `${Math.round(v * 100)}% of the picture` : "nobody (off)";
  drawZone();
}
$("#c-mh").oninput = updateMh;

function drawZone() {
  const cv = $("#zone-canvas"), r = cv.getBoundingClientRect();
  cv.width = r.width * devicePixelRatio; cv.height = r.height * devicePixelRatio;
  const g = cv.getContext("2d"), W = cv.width, H = cv.height;
  g.clearRect(0, 0, W, H);
  const mh = Number($("#c-mh").value);
  if (mh) {   // a person-height marker at the right edge: people shorter than this are ignored
    g.fillStyle = "rgba(168,123,47,.35)"; g.fillRect(W - 14 * devicePixelRatio, H * (1 - mh), 8 * devicePixelRatio, H * mh);
  }
  if (!zone.length) return;
  g.lineWidth = 2.5 * devicePixelRatio; g.strokeStyle = "#2EB872"; g.fillStyle = "rgba(46,184,114,.18)";
  g.beginPath(); zone.forEach(([x, y], i) => (i ? g.lineTo : g.moveTo).call(g, x * W, y * H));
  if (zone.length > 2) { g.closePath(); g.fill(); }
  g.stroke();
  g.fillStyle = "#fff";
  zone.forEach(([x, y]) => { g.beginPath(); g.arc(x * W, y * H, 4 * devicePixelRatio, 0, 7); g.fill(); });
}
$("#zone-canvas").addEventListener("click", e => {
  if (state.source?.type === "frigate") return;
  const r = e.currentTarget.getBoundingClientRect();
  zone.push([+((e.clientX - r.left) / r.width).toFixed(3), +((e.clientY - r.top) / r.height).toFixed(3)]);
  drawZone();
});
$("#zone-clear").onclick = () => { zone = []; drawZone(); };
addEventListener("resize", () => sheet.open && drawZone());

$("#c-test").onclick = async () => {
  const url = $("#c-sub").value || $("#c-url").value;
  const out = $("#c-test-out");
  if (!url || url.includes("***")) { out.textContent = "Enter the stream address (with its password) to test it."; return; }
  out.textContent = "Connecting…";
  try {
    const r = await api("/api/admin/cameras/probe", { method: "POST", body: { url } });
    out.textContent = r.ok ? `Works: ${r.width} × ${r.height}, ${r.codec.toUpperCase()}` : `Can't open it: ${r.error}`;
  } catch (x) { out.textContent = x.message; }
};

$("#cam-form").addEventListener("submit", async e => {
  e.preventDefault();
  const body = { name: $("#c-name").value.trim(), role, min_person_height: Number($("#c-mh").value) };
  if ($("#c-url").value) body.url = $("#c-url").value.trim();
  if ($("#c-sub").value) body.sub_url = $("#c-sub").value.trim();
  if (zone.length >= 3) body.zone = zone;
  if ($("#ptz-on").checked) {
    body.ptz = { type: "wyze-bridge", url: $("#ptz-url").value.trim(), camera: $("#ptz-cam").value.trim() };
    if ($("#ptz-key").value) body.ptz.api_key = $("#ptz-key").value;
  }
  if (editing) for (const k of ["detect_url", "streams", "hls_src", "hls_variants", "loop"]) if (k in editing && !body.url) body[k] = editing[k];
  const id = $("#c-id").value.trim();
  try {
    await api(`/api/admin/cameras/${encodeURIComponent(id)}`, { method: "PUT", body });
    sheet.close(); toast(editing ? "Camera saved" : "Camera added"); await refresh();
  } catch (x) { $("#c-error").textContent = x.message; }
});

$("#ptz-on").onchange = e => {
  $("#ptz-fields").hidden = !e.target.checked;
  if (e.target.checked && !$("#ptz-cam").value) $("#ptz-cam").value = $("#c-id").value;
};
$$("#ptz-test button").forEach(b => b.onclick = async () => {
  if (!editing) return;
  const body = { pan: Number(b.dataset.pan || 0), tilt: Number(b.dataset.tilt || 0) };
  $("#ptz-out").textContent = "Moving…";
  try { await api(`/api/admin/cameras/${encodeURIComponent(editing.id)}/ptz`, { method: "POST", body });
        $("#ptz-out").textContent = "Moved"; }
  catch (x) { $("#ptz-out").textContent = x.message; }
});

$("#c-delete").onclick = async () => {
  if (!editing || !confirm(`Delete ${editing.name}? Its alerts stop right away.`)) return;
  try { await api(`/api/admin/cameras/${encodeURIComponent(editing.id)}`, { method: "DELETE" });
        sheet.close(); toast("Camera deleted"); await refresh(); }
  catch (x) { $("#c-error").textContent = x.message; }
};

/* ---------- detection ---------- */
const sizeNames = { nano: "Nano", small: "Small", medium: "Medium", large: "Large" };
const backendNames = { cpu: "Processor", "openvino-cpu": "Processor (OpenVINO)", "openvino-gpu": "Intel graphics",
                       cuda: "NVIDIA graphics", coreml: "Apple Neural Engine" };

function renderDetection() {
  const src = state.source, det = state.detector, s = state.config.settings;
  const frigate = src.type === "frigate";
  seg($("#source-seg"), src.type, pickSource);
  $("#frigate-form").hidden = !frigate && !$("#frigate-form").dataset.open;
  $("#builtin-panel").hidden = frigate;
  $("#source-note").textContent = frigate
    ? (src.connected ? `Connected to Frigate ${src.version}. Lookout alerts follow Frigate's person alerts.`
                     : "Waiting for Frigate to answer…")
    : "This hub looks for people itself.";
  if (frigate) { $("#fr-url").value = src.url || ""; $("#fr-user").value = src.user || "";
                 $("#fr-pass").value = src.password || ""; $("#fr-sev").value = src.severity || "alert"; }
  renderHistory(frigate);
  if (frigate) return;
  const ch = det.choice || {};
  $("#det-summary").innerHTML = "";
  $("#det-summary").append(det.active === "pc-gpu"
    ? "Your PC's graphics card is doing the checks right now; this hub takes over when it's off."
    : `Using the ${sizeNames[ch.size] || ch.model} model on the ${(backendNames[ch.backend] || ch.backend || "").toLowerCase()}: ` +
      `${det.checks_per_s} ${det.checks_per_s === 1 ? "check" : "checks"} per second for each camera.`);
  const n = state.config.cameras.length, aim = s.detect_fps || 5;
  if (det.active !== "pc-gpu" && det.checks_per_s < aim)
    $("#det-summary").append(` That's below your ${aim}: ${n} ${n === 1 ? "camera shares" : "cameras share"} this processor. Most accurate lets detection use more of it.`);
  if (ch.throttled) $("#det-summary").append(" It slowed itself down because detection was falling behind.");
  seg($("#power-seg"), s.detect_power || "balanced", async v => {
    await api("/api/admin/settings", { method: "PUT", body: { detect_power: v } }); toast("Saved"); await refresh();
  });
  $("#fps").value = s.detect_fps || 5; $("#fps-out").textContent = $("#fps").value;
  const hw = det.profile?.hardware || {};
  $("#hw-line").textContent = hw.cpus ? `${hw.cpus} processor threads · ${hw.memory_gb} GB memory · ${hw.arch}` : "";
  $("#profile-rows").replaceChildren(...(det.profile?.results || []).map(r => {
    const tr = document.createElement("tr");
    tr.className = r.model === ch.model ? "chosen" : "";
    tr.innerHTML = `<td></td><td></td><td class="num"></td><td></td>`;
    tr.cells[0].textContent = sizeNames[r.size] || r.model;
    tr.cells[1].textContent = backendNames[r.backend] || r.backend;
    tr.cells[2].textContent = `${Math.round(r.ms)} ms`;
    if (r.model === ch.model) tr.cells[3].innerHTML = `<span class="badge">In use</span>`;
    return tr;
  }));
}

$("#fps").oninput = () => { $("#fps-out").textContent = $("#fps").value; };
$("#fps").onchange = async () => {
  await api("/api/admin/settings", { method: "PUT", body: { detect_fps: Number($("#fps").value) } });
  toast("Saved"); await refresh();
};
$("#bench-btn").onclick = async () => {
  const b = $("#bench-btn"); b.disabled = true; $("#bench-note").textContent = "Timing each model, about 10 seconds…";
  try { await api("/api/admin/detector/benchmark", { method: "POST" }); await refresh(); $("#bench-note").textContent = "Done."; }
  catch (x) { $("#bench-note").textContent = x.message; }
  b.disabled = false;
};

function renderHistory(frigate) {
  const h = state.history || {};
  $("#history-panel").hidden = frigate;          // Frigate as the source gives history already
  $("#history-note").textContent = !h.enabled ? "Off. Connect Frigate to see past visitors and their clips in the apps."
    : h.connected ? `From Frigate ${h.version}.` : "Waiting for Frigate to answer…";
  if (document.activeElement?.closest?.("#history-form")) return;
  $("#hi-url").value = h.url || ""; $("#hi-user").value = h.user || ""; $("#hi-pass").value = h.password || "";
  $("#hi-off").hidden = !h.enabled;
}
$("#history-form").addEventListener("submit", async e => {
  e.preventDefault();
  const st = $("#hi-status"); st.textContent = "Connecting…";
  const body = { url: $("#hi-url").value.trim() };
  if ($("#hi-user").value) body.user = $("#hi-user").value;
  if ($("#hi-pass").value) body.password = $("#hi-pass").value;
  try { await api("/api/admin/history", { method: "PUT", body }); st.textContent = ""; toast("Event history connected"); await refresh(); }
  catch (x) { st.textContent = x.message; }
});
$("#hi-off").onclick = async () => {
  try { await api("/api/admin/history", { method: "PUT", body: {} }); toast("Event history off"); await refresh(); }
  catch (x) { toast(x.message); }
};

async function pickSource(v) {
  if (v === "frigate") { $("#frigate-form").dataset.open = "1"; $("#frigate-form").hidden = false; seg($("#source-seg"), v, pickSource); $("#fr-url").focus(); return; }
  delete $("#frigate-form").dataset.open;
  try { await api("/api/admin/source", { method: "PUT", body: { type: "builtin" } }); toast("This hub is detecting again"); await refresh(); }
  catch (x) { toast(x.message); }
}
$("#frigate-form").addEventListener("submit", async e => {
  e.preventDefault();
  const st = $("#fr-status"); st.textContent = "Connecting…";
  const body = { type: "frigate", url: $("#fr-url").value.trim(), severity: $("#fr-sev").value };
  if ($("#fr-user").value) body.user = $("#fr-user").value;
  if ($("#fr-pass").value) body.password = $("#fr-pass").value;
  try {
    const r = await api("/api/admin/source", { method: "PUT", body });
    st.textContent = r.imported.length ? `Connected. Added ${r.imported.join(", ")}.` : "Connected.";
    toast("Frigate connected"); await refresh();
  } catch (x) { st.textContent = x.message; }
});

/* ---------- alerts ---------- */
function renderAlerts() {
  $("#alerts-all").checked = state.alerts.enabled;
  $("#alert-cams").replaceChildren(...state.config.cameras.map(cfg => {
    const row = document.createElement("label"); row.className = "switch-row";
    row.innerHTML = `<img class="thumb" alt=""><span class="grow"><b></b><br><span class="muted"></span></span><input type="checkbox" role="switch">`;
    $(".thumb", row).src = `/api/cameras/${encodeURIComponent(cfg.id)}/live.jpg?w=160&t=${Date.now()}`;
    $(".thumb", row).onerror = e => e.target.removeAttribute("src");
    $("b", row).textContent = cfg.name;
    $(".muted", row).textContent = cfg.role === "door" ? "Says “Someone’s at the door”" : "Says “Person detected”";
    const sw = $("input", row);
    sw.checked = state.alerts.cameras[cfg.id] !== false; sw.disabled = !state.alerts.enabled;
    sw.onchange = () => setAlerts(sw.checked, cfg.id);
    return row;
  }));
}
$("#alerts-all").onchange = e => setAlerts(e.target.checked);
async function setAlerts(enabled, camera) {
  const r = await fetch("/api/alerts", { method: "POST", headers: { "Content-Type": "application/json" },
                                         body: JSON.stringify(camera ? { enabled, camera } : { enabled }) });
  if (r.ok) { state.alerts = await r.json(); renderAlerts(); toast(enabled ? "Alerts on" : "Alerts off"); }
}

/* ---------- hub ---------- */
function renderHub() {
  const h = state.config.hub;
  const facts = [["Version", h.version], ["Address", `${location.host}`], ["Detection", state.source.type === "frigate" ? "Frigate" : "This hub"],
                 ["Video restreams", h.go2rtc === "embedded" ? `rtsp://${h.public_host}:${h.rtsp_port}/<camera>` : "External go2rtc"]];
  $("#hub-facts").replaceChildren(...facts.flatMap(([k, v]) => [Object.assign(document.createElement("dt"), { textContent: k }),
                                                               Object.assign(document.createElement("dd"), { textContent: v })]));
}
async function loadHubForm() {
  const d = await api("/api/admin/devices");
  $("#hub-name").value = d.hub.name;
  $("#hub-remote").value = d.hub.remote_url || "";
  $("#hub-alerts-to").value = d.hub.alerts_to || "phone";
  $("#hub-alerts-to").disabled = !d.hub.push;
  if (!d.hub.push) $("#hub-alerts-note").textContent = "Push alerts aren't set up on this hub (see the apps guide).";
}
$("#hub-form").addEventListener("submit", async e => {
  e.preventDefault();
  const remote = $("#hub-remote").value.trim().replace(/\/+$/, "");
  try {
    await api("/api/admin/settings", { method: "PUT", body: { hub_name: $("#hub-name").value.trim(), remote_url: remote || null,
                                                               alerts_to: $("#hub-alerts-to").value } });
    $("#hub-note").textContent = remote ? "Saved. Pair your apps again so they learn the away address." : "Saved.";
  } catch (x) { $("#hub-note").textContent = x.message; }
});
$("#restart-btn").onclick = async () => {
  if (!confirm("Restart the hub? Cameras and alerts pause for about 20 seconds.")) return;
  await api("/api/admin/restart", { method: "POST" });
  $("#restart-note").textContent = "Restarting…";
  const wait = setInterval(async () => {
    try { const r = await fetch("/healthz"); if (r.ok) { clearInterval(wait); location.reload(); } } catch {}
  }, 2000);
};
$("#pw-form").addEventListener("submit", async e => {
  e.preventDefault();
  try { await api("/api/admin/password", { method: "POST", body: { current: $("#pw-old").value, new: $("#pw-new").value } });
        $("#pw-note").textContent = "Password changed. Other browsers are signed out."; e.target.reset(); }
  catch (x) { $("#pw-note").textContent = x.message; }
});
$("#signout-btn").onclick = async () => { await api("/api/admin/logout", { method: "POST", allow401: true }); location.reload(); };

/* ---------- live updates ---------- */
function listen() {
  const es = new EventSource("/events");
  const on = (t, f) => es.addEventListener(t, e => f(JSON.parse(e.data)));
  on("state", s => { state.alerts = s.alerts; state.cams = Object.fromEntries(s.cameras.map(c => [c.id, c]));
                     if (s.cameras.map(c => c.id).join() !== state.config.cameras.map(c => c.id).join()) refresh(); else { renderAll(); } });
  on("person_detected", e => { if (state.cams[e.camera]) { state.cams[e.camera].person = true; updateTile(e.camera); } });
  on("person_gone", e => { if (state.cams[e.camera]) { state.cams[e.camera].person = false; updateTile(e.camera); } });
  on("camera_status", e => { if (state.cams[e.camera]) { state.cams[e.camera].connected = e.connected; updateTile(e.camera); renderRail(); } });
  on("alerts", a => { state.alerts = a; renderAlerts(); });
  on("detector", async () => { state.detector = await api("/api/admin/detector"); renderDetection(); });
  on("devices", () => { loadApps(); if (!$("#pair-panel").hidden) { $("#pair-panel").hidden = true; toast("App paired"); } });
  on("camera_sleep", () => loadBridge());
  on("found_cameras", e => { if (state.found) { state.found.cameras = e.cameras; renderFound(); } if (e.added?.length) refresh(); });
  on("source", async () => { state.source = await api("/api/admin/source"); renderDetection(); renderRail(); });
}

boot().catch(x => { document.body.textContent = `Can't reach the hub: ${x.message}`; });
