/* RTVIO Studio front end. Plain JS, no build step: polls /api/state once a
   second and /api/sessions every few seconds, and renders. */
"use strict";

const $ = (id) => document.getElementById(id);
let STATE = null;
let SESSIONS = [];
let settingsDirty = 0;          // ms timestamp of the last local edit

async function api(method, path, body) {
  const res = await fetch(path, {
    method, headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) throw new Error((data && data.error) || res.statusText);
  return data;
}

const fmtBytes = (b) => b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : (b / 1e3).toFixed(0) + " KB";
const fmtDur = (s) => {
  if (s == null || isNaN(s)) return "–";
  s = Math.max(0, Math.round(s));
  const m = Math.floor(s / 60), r = s % 60;
  return m >= 60 ? `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}` : `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ------------------------------------------------------------ settings */

function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj); }
function patchFor(path, value) {
  const keys = path.split("."), out = {};
  let o = out;
  keys.slice(0, -1).forEach((k) => { o = o[k] = {}; });
  o[keys[keys.length - 1]] = value;
  return out;
}

function bindSettings() {
  document.querySelectorAll("[data-setting]").forEach((el) => {
    el.addEventListener("change", async () => {
      settingsDirty = Date.now();
      let v = el.type === "checkbox" ? el.checked : el.value;
      const t = el.dataset.type;
      if (t === "int") v = parseInt(v, 10);
      if (t === "float") v = parseFloat(v);
      if (el.dataset.setting === "recon.window_frames" && v !== "auto") {
        const n = parseInt(v, 10);
        v = isNaN(n) ? "auto" : n;
      }
      try { await api("POST", "/api/settings", patchFor(el.dataset.setting, v)); }
      catch (e) { alert("Could not save setting: " + e.message); }
    });
  });
  const q = document.querySelector('[data-setting="capture.jpeg_quality"]');
  q.addEventListener("input", () => { $("qOut").textContent = q.value; });
}

function renderSettings(settings) {
  if (Date.now() - settingsDirty < 3000) return;   // don't fight the user's edit
  document.querySelectorAll("[data-setting]").forEach((el) => {
    if (document.activeElement === el) return;
    const v = getPath(settings, el.dataset.setting);
    if (v === undefined) return;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  });
  $("qOut").textContent = settings.capture.jpeg_quality;
}

/* --------------------------------------------------------------- phone */

let previewOn = false;
function renderPhone(st) {
  const p = st.phone, s = p.status || {}, rec = p.recording;
  const pill = $("phonePill");
  let pillText = "phone disconnected", pillCls = "pill-off";
  if (p.connected && !p.remote_control) { pillText = "phone connected (old app – no remote control)"; pillCls = "pill-warn"; }
  else if (p.connected) {
    if (s.state === "recording") { pillText = "● recording"; pillCls = "pill-rec"; }
    else if (s.state === "finishing") { pillText = `uploading ${s.queued || 0} spooled frames`; pillCls = "pill-warn"; }
    else { pillText = "phone ready"; pillCls = "pill-ok"; }
  } else if (rec) { pillText = "phone link lost – take kept open"; pillCls = "pill-warn"; }
  pill.textContent = pillText;
  pill.className = "pill " + pillCls;

  $("lanIps").innerHTML = (st.lan.length ? st.lan : ["<this PC's IP>"]).map((ip) => `${esc(ip)} : ${st.phone_port}`).join("<br>");
  if (p.listen_error) $("lanIps").innerHTML = `<span style="color:var(--bad)">${esc(p.listen_error)}</span>`;
  const hasFrame = p.connected;
  if (hasFrame && !previewOn) { $("preview").src = "/api/preview.mjpg"; previewOn = true; }
  if (!hasFrame && previewOn) { $("preview").removeAttribute("src"); previewOn = false; }
  $("previewEmpty").classList.toggle("hidden", hasFrame);
  $("phoneModel").textContent = p.connected ? `${s.model || ""}${s.app ? " · app " + s.app : ""}` : "";

  const rows = [];
  if (p.connected) {
    rows.push(["Link", `${p.peer}${p.status_age_s != null && p.status_age_s > 3 ? ` <span style="color:var(--warn)">(status ${p.status_age_s}s old)</span>` : ""}`]);
    if (s.resolution) rows.push(["Camera", `${s.resolution[0]}×${s.resolution[1]} · ${s.fps_target || "?"} fps target · JPEG ${s.jpeg_quality || "?"}`]);
    if (s.fps != null) rows.push(["Capture", `${(+s.fps).toFixed(1)} fps · encode ${s.encode_ms != null ? (+s.encode_ms).toFixed(0) + " ms" : "–"}`]);
    if (s.battery != null) rows.push(["Battery", `${s.battery}%${s.charging ? " ⚡" : ""}${s.temp_c != null ? ` · ${(+s.temp_c).toFixed(0)} °C` : ""}`]);
    if (s.spool_free_mb != null) rows.push(["Phone storage", `${fmtBytes(s.spool_free_mb * 1e6)} free for spooling`]);
    if (s.gps_acc != null && s.gps_acc >= 0) rows.push(["GPS", `±${(+s.gps_acc).toFixed(0)} m`]);
    if (s.error) rows.push(["Last error", `<span style="color:var(--bad)">${esc(s.error)}</span>`]);
  }
  $("phoneKv").innerHTML = rows.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");

  // Record button
  const btn = $("recordBtn");
  btn.classList.remove("stop", "busy");
  let hint = "";
  if (rec && rec.stopping) {
    btn.textContent = s.state === "finishing" ? `Uploading… ${s.queued || 0} frames left` : "Stopping…";
    btn.classList.add("busy"); btn.disabled = true;
    hint = "The phone keeps every frame it captured and uploads the backlog before the take is closed.";
  } else if (rec) {
    btn.textContent = rec.confirmed ? "Stop recording" : "Starting…";
    btn.classList.add("stop"); btn.disabled = rec.origin === "legacy";
  } else {
    btn.textContent = "Start recording";
    btn.disabled = !(p.connected && p.remote_control && s.state === "armed");
    if (!p.connected) hint = "Waiting for the phone to connect.";
    else if (!p.remote_control) hint = "The connected app is an old build without remote control — install the new APK.";
    else if (s.state !== "armed") hint = `Phone state: ${s.state || "unknown"}`;
  }
  $("recHint").textContent = hint;

  $("recBadge").classList.toggle("hidden", !(rec && !rec.stopping));
  if (rec) $("recTime").textContent = fmtDur(rec.elapsed_s);
  const stats = rec ? [
    [rec.frames.toLocaleString(), "frames received"],
    [rec.fps ? rec.fps.toFixed(1) : "–", "fps arriving"],
    [rec.phone_backlog || 0, "queued on phone"],
    [rec.mb.toFixed(0) + " MB", "received"],
  ] : p.last_finalized ? [
    [p.last_finalized.frames_received.toLocaleString(), "frames (last take)"],
    [p.last_finalized.fps_mean.toFixed(1), "fps mean"],
    [fmtDur(p.last_finalized.duration_s), "duration"],
    [p.last_finalized.complete ? "yes" : "NO", "complete"],
  ] : [];
  $("recStats").innerHTML = stats.map(([v, l]) => `<div><b>${v}</b><span>${l}</span></div>`).join("");

  $("events").textContent = p.events.map(([t, e]) => `${t}  ${e}`).join("\n");
}

$("recordBtn").addEventListener("click", async () => {
  const btn = $("recordBtn");
  btn.disabled = true;
  try {
    if (STATE && STATE.phone.recording) await api("POST", "/api/record/stop");
    else await api("POST", "/api/record/start", {});
  } catch (e) { alert(e.message); }
  pollState();
});

/* ----------------------------------------------------------------- gpu */

function renderGpu(g) {
  const s = g.samples, last = s[s.length - 1];
  $("gpuUtil").textContent = last ? `${last.util.toFixed(0)}%` : "–";
  $("gpuMem").textContent = last ? `${(last.mem_mb / 1024).toFixed(1)}/${(last.mem_total_mb / 1024).toFixed(0)} GB · ${last.temp_c.toFixed(0)}°C${last.power_w ? " · " + last.power_w.toFixed(0) + " W" : ""}` : (g.error || "");
  const c = $("gpuSpark"), ctx = c.getContext("2d");
  ctx.clearRect(0, 0, c.width, c.height);
  if (s.length < 2) return;
  ctx.beginPath();
  s.forEach((p, i) => {
    const x = (i / (s.length - 1)) * c.width, y = c.height - (p.util / 100) * (c.height - 2) - 1;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.strokeStyle = "#4cc2ff"; ctx.lineWidth = 1.5; ctx.stroke();
  ctx.lineTo(c.width, c.height); ctx.lineTo(0, c.height); ctx.closePath();
  ctx.fillStyle = "rgba(76,194,255,.15)"; ctx.fill();
}

/* ------------------------------------------------------------ sessions */

function latestJobFor(sid) {
  if (!STATE) return null;
  const js = STATE.jobs.filter((j) => j.session === sid);
  return js.length ? js[js.length - 1] : null;
}

function stageLine(job, recon) {
  const pr = (job && job.progress) || (recon && recon.progress) || null;
  const state = job ? job.state : pr ? (pr.stage === "done" ? "done" : pr.stage === "error" ? "failed" : "stale") : null;
  if (!state) return { html: `<span class="muted">not reconstructed</span>`, frac: null };
  if (state === "queued") return { html: `<span class="stage">queued for GPU</span>`, frac: 0 };
  if (state === "done") {
    const t = pr && pr.elapsed_s ? ` in ${fmtDur(pr.elapsed_s)}` : "";
    const g = job && job.gpu_util_mean != null ? ` · GPU ${job.gpu_util_mean}% avg` : "";
    const n = pr && pr.points ? ` · ${(+pr.points).toLocaleString()} pts` : "";
    return { html: `<span class="stage done">✓ reconstructed${t}${n}${g}</span>`, frac: null };
  }
  if (state === "failed" || state === "cancelled") {
    const err = (job && job.error) || (pr && pr.error) || state;
    return { html: `<span class="stage failed">✗ ${esc(err).slice(0, 180)}</span>`, frac: null };
  }
  if (state === "stale") return { html: `<span class="muted">interrupted (${esc(pr.stage)})</span>`, frac: null };
  const frac = pr && pr.fraction != null ? pr.fraction : 0;
  const eta = pr && pr.eta_s != null ? ` · ETA ${fmtDur(pr.eta_s)}` : "";
  return { html: `<span class="stage">${esc((pr && pr.detail) || "starting…")}${eta}</span>`, frac };
}

function renderSessions() {
  const box = $("sessions");
  if (!SESSIONS.length) { box.innerHTML = `<div class="muted">No recordings yet. Connect the phone and press Start.</div>`; return; }
  box.innerHTML = SESSIONS.map((s) => {
    const m = s.meta, rec = s.recons[s.recons.length - 1];
    const job = latestJobFor(s.id);
    const facts = [];
    if (s.recording) facts.push(`<span style="color:var(--rec)">recording…</span>`);
    if (m) {
      facts.push(`${m.frames_received.toLocaleString()} frames`, fmtDur(m.duration_s), `${m.fps_mean} fps`);
      if (m.resolution) facts.push(`${m.resolution[0]}×${m.resolution[1]}`);
      if (!m.complete && m.frames_reported_sent != null) facts.push(`<span class="warn">phone sent ${m.frames_reported_sent}</span>`);
      if (m.frame_gaps) facts.push(`<span class="warn">${m.frame_gaps} gaps</span>`);
    }
    const st = stageLine(job, rec);
    const outs = rec ? rec.files : {};
    const base = rec ? `/files/${encodeURIComponent(s.id)}/${rec.name}/` : "";
    const running = job && ["queued", "running", "cancelling"].includes(job.state);
    const actions = [];
    if (outs["cloud_raw.ply"]) actions.push(`<button class="primary" data-view="${s.id}|${rec.name}|cloud">View cloud · ${fmtBytes(outs["cloud_raw.ply"])}</button>`);
    if (outs["mesh_poisson.ply"]) actions.push(`<button class="primary" data-view="${s.id}|${rec.name}|mesh">View mesh · ${fmtBytes(outs["mesh_poisson.ply"])}</button>`);
    if (outs["cloud_raw.ply"]) actions.push(`<a class="btn" href="${base}cloud_raw.ply" download="${s.id}_cloud_raw.ply">cloud_raw.ply</a>`);
    if (outs["mesh_poisson.ply"]) actions.push(`<a class="btn" href="${base}mesh_poisson.ply" download="${s.id}_mesh_poisson.ply">mesh_poisson.ply</a>`);
    if (outs["CHECKPOINT_REPORT.md"]) actions.push(`<a class="btn" href="${base}CHECKPOINT_REPORT.md" target="_blank">report</a>`);
    if (running && job.state !== "cancelling") actions.push(`<button data-cancel="${job.id}">Cancel</button>`);
    else if (m && !s.recording) actions.push(`<button data-recon="${s.id}">${rec ? "Reconstruct again" : "Reconstruct"}</button>`);
    if (job && job.state !== "queued") actions.push(`<button data-log="${job.id}">log</button>`);
    return `<div class="session${s.recording ? " live" : ""}">
      ${s.thumb ? `<img src="/files/${encodeURIComponent(s.id)}/${s.thumb}" loading="lazy" alt="">` : `<img alt="">`}
      <div>
        <div class="title">${esc(s.id)}</div>
        <div class="facts">${facts.join(" · ")}</div>
        ${st.frac != null ? `<div class="progress"><div style="width:${(st.frac * 100).toFixed(1)}%"></div></div>` : ""}
        <div>${st.html}</div>
      </div>
      <div class="actions">${actions.join("")}</div>
    </div>`;
  }).join("");
}

$("sessions").addEventListener("click", async (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  try {
    if (t.dataset.view) { const [sid, rec, kind] = t.dataset.view.split("|"); openViewer(sid, rec, kind); }
    if (t.dataset.recon) { await api("POST", `/api/sessions/${t.dataset.recon}/reconstruct`, {}); pollState(); }
    if (t.dataset.cancel) { await api("POST", `/api/jobs/${t.dataset.cancel}/cancel`); pollState(); }
    if (t.dataset.log) {
      const r = await api("GET", `/api/jobs/${t.dataset.log}/log`);
      const w = window.open("", "_blank");
      w.document.write(`<pre style="font:12px/1.4 Consolas,monospace;white-space:pre-wrap">${esc(r.lines.join("\n"))}</pre>`);
    }
  } catch (e) { alert(e.message); }
});

/* ------------------------------------------------------------- polling */

async function pollState() {
  try {
    STATE = await api("GET", "/api/state");
    renderPhone(STATE);
    renderGpu(STATE.gpu);
    renderSettings(STATE.settings);
    renderSessions();
  } catch (e) {
    $("phonePill").textContent = "studio server unreachable";
    $("phonePill").className = "pill pill-off";
  }
}
async function pollSessions() {
  try { SESSIONS = await api("GET", "/api/sessions"); renderSessions(); } catch (e) { /* next tick */ }
}

bindSettings();
pollState(); pollSessions();
setInterval(pollState, 1000);
setInterval(pollSessions, 2500);

/* -------------------------------------------------------------- viewer */

const V = { renderer: null, scene: null, camera: null, controls: null, obj: null, cams: null,
  sid: null, rec: null, kind: null, radius: 1, center: new THREE.Vector3() };

function initViewer() {
  if (V.renderer) return;
  const host = $("viewerCanvas");
  V.renderer = new THREE.WebGLRenderer({ antialias: true });
  V.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  host.appendChild(V.renderer.domElement);
  V.scene = new THREE.Scene();
  V.scene.background = new THREE.Color(0x07090c);
  V.camera = new THREE.PerspectiveCamera(55, 1, 0.001, 1e5);
  V.controls = new THREE.OrbitControls(V.camera, V.renderer.domElement);
  V.controls.enableDamping = true;
  V.controls.screenSpacePanning = true;
  V.scene.add(new THREE.HemisphereLight(0xffffff, 0x303040, 0.9));
  const sun = new THREE.DirectionalLight(0xffffff, 0.6);
  sun.position.set(1, 2, 1.5);
  V.scene.add(sun);
  const resize = () => {
    const w = host.clientWidth, h = host.clientHeight;
    V.renderer.setSize(w, h); V.camera.aspect = w / Math.max(h, 1); V.camera.updateProjectionMatrix();
  };
  window.addEventListener("resize", resize);
  V.resize = resize;
  (function loop() { requestAnimationFrame(loop); if (!$("viewer").classList.contains("hidden")) { V.controls.update(); V.renderer.render(V.scene, V.camera); } })();
}

function clearObj() {
  [V.obj, V.cams].forEach((o) => {
    if (!o) return;
    V.scene.remove(o);
    o.traverse((c) => { if (c.geometry) c.geometry.dispose(); if (c.material) c.material.dispose(); });
  });
  V.obj = V.cams = null;
}

function resetView() {
  const r = V.radius, c = V.center;
  V.camera.near = r / 2000; V.camera.far = r * 200; V.camera.updateProjectionMatrix();
  // Output is Y-up with the first camera looking down -Z (see
  // vggt_reconstruct's output frame), so start just behind and above it.
  V.camera.position.set(c.x + r * 0.35, c.y + r * 0.55, c.z + r * 1.4);
  V.controls.target.copy(c); V.controls.update();
}

async function loadCams(base) {
  try {
    const res = await fetch(base + "cameras.json");
    if (!res.ok) return;
    const cams = await res.json();
    const pts = cams.centers.map((p) => new THREE.Vector3(p[0], p[1], p[2]));
    const g = new THREE.BufferGeometry().setFromPoints(pts);
    V.cams = new THREE.Line(g, new THREE.LineBasicMaterial({ color: 0xffb020 }));
    V.cams.visible = $("showCams").checked;
    V.scene.add(V.cams);
  } catch (e) { /* optional */ }
}

function openViewer(sid, rec, kind) {
  initViewer();
  $("viewer").classList.remove("hidden");
  V.resize();
  V.sid = sid; V.rec = rec;
  showKind(kind);
}

function showKind(kind) {
  V.kind = kind;
  document.querySelectorAll("#viewerKind button").forEach((b) => b.classList.toggle("on", b.dataset.kind === kind));
  const file = kind === "mesh" ? "mesh_poisson.ply" : "cloud_raw.ply";
  const base = `/files/${encodeURIComponent(V.sid)}/${V.rec}/`;
  $("viewerTitle").textContent = `${V.sid} / ${V.rec} / ${file}`;
  $("viewerDownload").href = base + file;
  $("viewerDownload").setAttribute("download", `${V.sid}_${file}`);
  $("viewerLoading").classList.remove("hidden");
  $("viewerLoading").textContent = "loading " + file + "…";
  clearObj();
  new THREE.PLYLoader().load(base + file, (geom) => {
    $("viewerLoading").classList.add("hidden");
    geom.computeBoundingSphere();
    V.radius = Math.max(geom.boundingSphere.radius, 1e-6);
    V.center.copy(geom.boundingSphere.center);
    const hasColor = !!geom.getAttribute("color");
    if (kind === "mesh") {
      if (!geom.getAttribute("normal")) geom.computeVertexNormals();
      const lit = $("litMesh").checked;
      const mat = lit ? new THREE.MeshStandardMaterial({ vertexColors: hasColor, roughness: 0.9, metalness: 0, side: THREE.DoubleSide })
                      : new THREE.MeshBasicMaterial({ vertexColors: hasColor, side: THREE.DoubleSide });
      V.obj = new THREE.Mesh(geom, mat);
      const n = geom.index ? geom.index.count / 3 : geom.getAttribute("position").count / 3;
      $("viewerInfo").textContent = `${Math.round(n).toLocaleString()} triangles`;
    } else {
      const mat = new THREE.PointsMaterial({ size: parseFloat($("ptSize").value), sizeAttenuation: false, vertexColors: hasColor });
      V.obj = new THREE.Points(geom, mat);
      $("viewerInfo").textContent = `${geom.getAttribute("position").count.toLocaleString()} points`;
    }
    V.scene.add(V.obj);
    resetView();
    loadCams(base);
  }, (xhr) => {
    if (xhr.total) $("viewerLoading").textContent = `loading ${file}… ${(100 * xhr.loaded / xhr.total).toFixed(0)}%`;
  }, (err) => {
    $("viewerLoading").textContent = "failed to load " + file;
    console.error(err);
  });
}

document.querySelectorAll("#viewerKind button").forEach((b) => b.addEventListener("click", () => showKind(b.dataset.kind)));
$("viewerClose").addEventListener("click", () => { $("viewer").classList.add("hidden"); clearObj(); });
$("viewerReset").addEventListener("click", resetView);
$("ptSize").addEventListener("input", () => { if (V.obj && V.obj.isPoints) V.obj.material.size = parseFloat($("ptSize").value); });
$("litMesh").addEventListener("change", () => { if (V.kind === "mesh") showKind("mesh"); });
$("showCams").addEventListener("change", () => { if (V.cams) V.cams.visible = $("showCams").checked; });
window.addEventListener("keydown", (e) => { if (e.key === "Escape") $("viewerClose").click(); });
