/* USB Doctor AI — web frontend (vanilla JS, no build step, no CDN).
 *
 * Design rules enforced here:
 *  · nothing is rendered from assumptions — an absent value shows as "—" / "not reported";
 *  · every destructive action shows exactly what will happen and needs a second click;
 *  · live progress comes from the real event stream, never from a timer.
 */
"use strict";

const state = {
  token: null,
  selected: null,          // device path
  devices: [],
  devicesRaw: {},
  lastScan: null,
  status: null,
  eventSource: null,
  cursor: 0,
  activeJob: null,
  plans: [],
  settings: {},
  showInternal: false,
};

/* ───────────────────────────── helpers ───────────────────────────── */
const $ = (id) => document.getElementById(id);
const el = (tag, cls, html) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (html !== undefined) node.innerHTML = html;
  return node;
};
const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const dash = (value) => (value === null || value === undefined || value === "" ? "—" : escapeHtml(value));

function humanSize(bytes) {
  if (bytes === null || bytes === undefined) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = Number(bytes), i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i += 1; }
  return `${value.toFixed(value >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function toast(message, kind = "") {
  const node = el("div", `toast ${kind}`, escapeHtml(message));
  document.body.appendChild(node);
  setTimeout(() => node.remove(), kind === "error" ? 9000 : 4200);
}

function banner(message, kind = "") {
  const node = $("banner");
  if (!message) { node.classList.add("hidden"); return; }
  node.className = `banner ${kind}`;
  node.innerHTML = message;
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (state.token) headers["X-USBDoctor-Token"] = state.token;
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  let payload = null;
  const text = await response.text();
  if (text) { try { payload = JSON.parse(text); } catch { payload = { error: text }; } }
  if (!response.ok) {
    const message = (payload && (payload.error || payload.detail)) || `Request failed (${response.status})`;
    const hint = payload && payload.hint ? ` — ${payload.hint}` : "";
    const error = new Error(message + hint);
    error.status = response.status;
    throw error;
  }
  return payload;
}

/* ───────────────────────────── boot ───────────────────────────── */
document.addEventListener("DOMContentLoaded", () => {
  const params = new URLSearchParams(location.search);
  const meta = document.querySelector('meta[name="usbdoctor-token"]');
  state.token = params.get("token") || (meta && meta.content) || null;
  restoreTheme();
  bindNavigation();
  bindActions();
  boot();
});

async function boot() {
  try {
    const status = await api("/api/status");
    state.status = status;
    $("brandVersion").textContent = `v${status.version}`;
    $("adminPill").textContent = status.is_admin ? "Administrator rights" : "Standard user — limited raw access";
    $("adminPill").className = `pill ${status.is_admin ? "admin" : "limited"}`;
    renderTools(status.tools);
    renderAbout(status);
    $("connDot").className = "conn-dot live";
    $("connDot").title = "Connected to the local engine";
  } catch (error) {
    toast(`Cannot reach the local engine: ${error.message}`, "error");
    $("connDot").className = "conn-dot dead";
  }
  await Promise.all([loadDevices(), loadHistoryStats()]);
  connectEvents();
  navigate("dashboard");
}

/* ───────────────────────────── navigation ───────────────────────────── */
const PAGE_META = {
  dashboard: ["Dashboard", "Connect a drive and start with a read-only health scan."],
  devices: ["Devices", "Every drive the operating system reports, with what we could verify about it."],
  scan: ["Health Scan", "Read-only diagnosis: identity, partitions, file system, SMART, surface and speed."],
  repair: ["Repair Center", "Evidence-matched repairs, each with its risk and rollback."],
  recovery: ["Data Recovery", "Image first, then recover files — your source device is never written to."],
  performance: ["Performance", "Real measured throughput and capacity verification."],
  assistant: ["AI Assistant", "Plain-language explanation of what your hardware actually reported."],
  history: ["History", "Every scan, repair and recovery, with verification status."],
  reports: ["Reports", "Export the health report as Markdown or a self-contained HTML file."],
  settings: ["Settings", "Safety policy, tools and limits."],
};

function bindNavigation() {
  document.querySelectorAll(".nav-item").forEach((button) => {
    button.addEventListener("click", () => navigate(button.dataset.page));
  });
}

function navigate(page) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  document.querySelectorAll(".page").forEach((section) => section.classList.toggle("hidden", section.id !== `page-${page}`));
  const [title, sub] = PAGE_META[page] || ["USB Doctor AI", ""];
  $("pageTitle").textContent = title;
  $("pageSub").textContent = sub;
  if (page === "history") loadHistory();
  if (page === "settings") loadSettings();
  if (page === "reports") renderReportPlaceholder();
  if (page === "assistant") renderAssistant();
  if (page === "performance") renderPerformance();
  if (location.hash !== `#${page}`) history.replaceState(null, "", `#${page}`);
}

/* ───────────────────────────── theme ───────────────────────────── */
function restoreTheme() {
  const saved = localStorage.getItem("usbdoctor.theme") || "dark";
  document.documentElement.dataset.theme = saved;
  updateThemeLabel(saved);
}
function updateThemeLabel(theme) {
  $("themeIcon").textContent = theme === "dark" ? "◐" : "☀";
  $("themeLabel").textContent = theme === "dark" ? "Dark" : "Light";
}
function bindTheme() {
  $("themeToggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("usbdoctor.theme", next);
    updateThemeLabel(next);
  });
}

/* ───────────────────────────── events ───────────────────────────── */
function connectEvents() {
  if (state.eventSource) state.eventSource.close();
  if (!state.token) return;
  const source = new EventSource(`/api/events?token=${encodeURIComponent(state.token)}&since=${state.cursor}`);
  state.eventSource = source;

  const handler = (event) => {
    let payload;
    try { payload = JSON.parse(event.data); } catch { return; }
    state.cursor = Math.max(state.cursor, payload.seq || 0);
    handleEvent(payload);
  };
  ["job_started", "stage", "progress", "step_start", "step_finish", "job_finished", "cancel_requested", "error"].forEach((type) => {
    source.addEventListener(type, handler);
  });
  source.onerror = () => {
    $("connDot").className = "conn-dot dead";
    source.close();
    setTimeout(connectEvents, 2500); // the local server may be restarting
  };
  source.onopen = () => { $("connDot").className = "conn-dot live"; };
}

function handleEvent(event) {
  const { type, job_id: jobId, payload = {} } = event;
  if (type === "job_finished") {
    if (state.activeJob && state.activeJob.id === jobId) {
      const job = state.activeJob;
      state.activeJob = null;
      if (payload.error) {
        toast(`${job.label} failed: ${payload.error}`, "error");
        stopProgress(job.progressBar);
      } else {
        job.onDone && job.onDone();
      }
    }
    return;
  }
  if (type === "progress" && state.activeJob && state.activeJob.id === jobId) {
    const percent = Number(payload.percent || 0);
    const bar = state.activeJob.progressBar;
    if (bar) bar.style.width = `${Math.min(100, percent)}%`;
    if (state.activeJob.percentNode) state.activeJob.percentNode.textContent = `${percent.toFixed(0)}%`;
    if (state.activeJob.stageNode && payload.stage) state.activeJob.stageNode.textContent = payload.stage;
    return;
  }
  if (type === "stage" && state.activeJob && state.activeJob.id === jobId) {
    if (state.activeJob.stageNode && payload.message) state.activeJob.stageNode.textContent = payload.message;
    return;
  }
  if (type === "error") toast(payload.message || "An error occurred", "error");
}

/* ───────────────────────────── devices ───────────────────────────── */
async function loadDevices() {
  try {
    const data = await api("/api/devices");
    state.devices = data.devices || [];
    state.devicesRaw = data;
    state.showInternal = state.showInternal || Boolean(state.settings.allow_internal_disks);
    renderDeviceTable();
    renderDeviceStrip();
    fillDeviceSelects();
    renderWarnings(data.warnings || []);
    $("deviceSource").textContent = `source: ${data.source}`;
    if (!state.selected && state.devices.length) {
      const firstUsb = state.devices.find((d) => (d.bus || "").toLowerCase() === "usb");
      selectDevice((firstUsb || state.devices[0]).path);
    } else if (state.selected) {
      const still = state.devices.find((d) => d.path === state.selected);
      if (!still) { state.selected = null; renderHero(); }
      else renderHero();
    }
  } catch (error) {
    toast(error.message, "error");
  }
}

function visibleDevices() {
  return state.devices.filter((device) => {
    if (state.showInternal) return true;
    if (device.is_system_disk) return false;
    return (device.bus || "").toLowerCase() !== "sata" && (device.bus || "").toLowerCase() !== "nvme";
  });
}

function deviceBadge(device) {
  if (device.read_only) return '<span class="badge warn">read-only</span>';
  if (device.is_system_disk) return '<span class="badge bad">system disk</span>';
  if (device.bus === "usb") return '<span class="badge">USB</span>';
  return `<span class="badge">${escapeHtml((device.bus || "disk").toUpperCase())}</span>`;
}

function renderDeviceTable() {
  const tbody = $("deviceTable").querySelector("tbody");
  const devices = visibleDevices();
  if (!devices.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">No devices discovered. Plug in a drive and rescan.</td></tr>`;
    return;
  }
  tbody.innerHTML = devices.map((device) => {
    const smart = device.health_hint ? escapeHtml(device.health_hint) : '<span class="muted">not reported</span>';
    const writable = device.can_write && device.can_write[0]
      ? '<span class="badge ok">writable</span>'
      : `<span class="badge warn" title="${escapeHtml((device.can_write && device.can_write[1]) || "")}">protected</span>`;
    return `<tr data-path="${escapeHtml(device.path)}" class="${state.selected === device.path ? "selected" : ""}">
      <td><strong>${escapeHtml(device.display_name)}</strong><div class="mono muted">${escapeHtml(device.path)}</div></td>
      <td class="mono">${humanSize(device.size_bytes)}</td>
      <td>${deviceBadge(device)}<div class="muted small">${dash(device.usb_speed)}</div></td>
      <td>${dash((device.filesystem || "—").toUpperCase())}</td>
      <td>${smart}</td>
      <td>${writable}</td>
      <td><button class="btn ghost small" data-scan="${escapeHtml(device.path)}">Scan</button></td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll("tr[data-path]").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.dataset.scan) return;
      selectDevice(row.dataset.path);
    });
  });
  tbody.querySelectorAll("button[data-scan]").forEach((button) => {
    button.addEventListener("click", () => {
      selectDevice(button.dataset.scan);
      navigate("scan");
      startScan();
    });
  });
}

function renderDeviceStrip() {
  const container = $("dashDevices");
  const devices = visibleDevices();
  if (!devices.length) {
    container.innerHTML = '<p class="muted">No devices discovered.</p>';
    return;
  }
  container.innerHTML = devices.map((device) => `
    <div class="device-card ${state.selected === device.path ? "selected" : ""}" data-path="${escapeHtml(device.path)}">
      <strong>${escapeHtml(device.display_name)}</strong>
      <div class="meta">${escapeHtml(device.path)}</div>
      <div class="meta">${humanSize(device.size_bytes)} · ${escapeHtml((device.filesystem || "unknown").toUpperCase())}</div>
      <div class="tags">${deviceBadge(device)}${
        device.read_only ? "" : '<span class="badge ok">usable</span>'
      }</div>
    </div>`).join("");
  container.querySelectorAll(".device-card").forEach((card) => {
    card.addEventListener("click", () => selectDevice(card.dataset.path));
  });
}

function renderWarnings(warnings) {
  const container = $("deviceWarnings");
  if (!warnings.length) { container.innerHTML = ""; return; }
  container.innerHTML = warnings.map((w) => `<div class="warn-item">${escapeHtml(w)}</div>`).join("");
}

function fillDeviceSelects() {
  const devices = visibleDevices();
  ["scanDevice", "repairDevice"].forEach((id) => {
    const select = $(id);
    const previous = select.value;
    select.innerHTML = devices.map((device) =>
      `<option value="${escapeHtml(device.path)}">${escapeHtml(device.display_name)} — ${escapeHtml(device.path)} (${humanSize(device.size_bytes)})</option>`
    ).join("");
    if (previous && devices.some((d) => d.path === previous)) select.value = previous;
    else if (state.selected) select.value = state.selected;
  });
}

function selectDevice(path) {
  state.selected = path;
  state.lastScan = null;
  $("scanDevice").value = path;
  $("repairDevice").value = path;
  renderDeviceTable();
  renderDeviceStrip();
  renderHero();
  renderScanResult();
  renderPlans([]);
  renderRepairResult(null);
  renderPerformance();
  renderAssistant();
  $("researchBtn").disabled = false;
  loadLastScan(path);
  api(`/api/devices/detail?path=${encodeURIComponent(path)}`)
    .then((detail) => { state.devicesRaw.detail = detail; renderHero(); })
    .catch(() => {});
}

async function loadLastScan(path) {
  try {
    state.lastScan = await api(`/api/scan/last?path=${encodeURIComponent(path)}`);
  } catch { state.lastScan = null; }
  renderHero();
  renderScanResult();
  renderPerformance();
  renderAssistant();
}

function renderHero() {
  const device = state.devices.find((d) => d.path === state.selected);
  const container = $("heroDevice");
  $("selectedBadge").textContent = device ? (device.bus || "disk").toUpperCase() : "none";
  if (!device) {
    container.className = "hero-empty";
    container.innerHTML = '<p class="muted">No drive selected. Pick one from <strong>Devices</strong>.</p>';
    return;
  }
  container.className = "hero-device";
  const scan = state.lastScan;
  const verdict = scan ? scan.health.verdict : null;
  const verdictBadge = verdict
    ? `<span class="badge ${verdictClass(verdict)}">${escapeHtml(verdict)}</span>`
    : '<span class="badge">not scanned</span>';
  container.innerHTML = `
    <h2>${escapeHtml(device.display_name)} ${verdictBadge}</h2>
    <div class="path">${escapeHtml(device.path)} · ${escapeHtml(device.kind)} · ${escapeHtml(device.platform)}</div>
    <div class="kv">
      <div><span>Capacity</span><strong>${humanSize(device.size_bytes)}</strong></div>
      <div><span>Interface</span><strong>${dash(device.bus)}${device.usb_speed ? ` · ${escapeHtml(device.usb_speed)}` : ""}</strong></div>
      <div><span>File system</span><strong>${dash((device.filesystem || "unknown").toUpperCase())}</strong></div>
      <div><span>VID / PID</span><strong class="mono">${dash(device.vid)} / ${dash(device.pid)}</strong></div>
      <div><span>Serial</span><strong class="mono">${dash(device.serial || "not reported")}</strong></div>
      <div><span>Firmware</span><strong class="mono">${dash(device.firmware || "not reported")}</strong></div>
    </div>
    ${verdict ? `<p class="muted small">${escapeHtml(scan.health.verdict_summary || "")} · confidence ${escapeHtml(scan.health.health_confidence)}</p>` : ""}
  `;
}

function verdictClass(verdict) {
  switch (verdict) {
    case "EXCELLENT": case "GOOD": return "ok";
    case "WARNING": case "UNKNOWN": return "warn";
    case "CRITICAL": return "bad";
    case "FAILING": return "crit";
    default: return "";
  }
}

/* ───────────────────────────── scan ───────────────────────────── */
function bindActions() {
  bindTheme();
  $("refreshBtn").addEventListener("click", refreshDevices);
  $("deviceRefresh").addEventListener("click", refreshDevices);
  $("showInternal").addEventListener("change", (event) => {
    state.showInternal = event.target.checked;
    renderDeviceTable();
    renderDeviceStrip();
    fillDeviceSelects();
  });
  $("startScan").addEventListener("click", startScan);
  $("scanDevice").addEventListener("change", (event) => selectDevice(event.target.value));
  $("scanModeSel").addEventListener("change", (event) => { $("scanMode").textContent = event.target.value; });
  $("loadPlans").addEventListener("click", loadPlans);
  $("repairDevice").addEventListener("change", (event) => selectDevice(event.target.value));
  $("approveBtn").addEventListener("click", () => openApproveDialog());
  $("approveCancel").addEventListener("click", () => $("approveDialog").close());
  $("approveConfirm").addEventListener("click", confirmRepair);
  $("dryRunBtn").addEventListener("click", dryRun);
  $("autoBtn").addEventListener("click", autoRepair);
  $("imageBtn").addEventListener("click", createImage);
  $("recoverBtn").addEventListener("click", recoverFiles);
  $("capacityBtn").addEventListener("click", verifyCapacity);
  $("researchBtn").addEventListener("click", researchModel);
  $("historyRefresh").addEventListener("click", loadHistory);
  $("reportMd").addEventListener("click", () => generateReport("markdown"));
  $("reportHtml").addEventListener("click", () => generateReport("html"));
  ["setInternal", "setOffline", "setMounted"].forEach((id) => $(id).addEventListener("change", saveSettings));
}

async function refreshDevices() {
  try {
    const data = await api("/api/devices/refresh", { method: "POST", body: {} });
    state.devices = data.devices || [];
    state.devicesRaw = data;
    renderDeviceTable(); renderDeviceStrip(); fillDeviceSelects(); renderWarnings(data.warnings || []);
    toast(`Found ${state.devices.length} device(s)`, "ok");
  } catch (error) { toast(error.message, "error"); }
}

async function loadHistoryStats() {
  try {
    const overview = await api("/api/history");
    $("statScans").textContent = overview.statistics.scans;
    $("statRepairs").textContent = overview.statistics.repairs_succeeded;
    $("statDevices").textContent = state.devices.length;
  } catch { /* the dashboard counters are cosmetic; failures surface elsewhere */ }
}

async function startScan() {
  const path = $("scanDevice").value;
  if (!path) { toast("No device selected", "error"); return; }
  const budget = Number($("scanBudget").value);
  $("scanResult").innerHTML = "";
  $("scanProgressWrap").classList.remove("hidden");
  $("scanProgressBar").style.width = "0%";
  $("scanStage").textContent = "starting…";
  $("scanPercent").textContent = "0%";
  $("startScan").disabled = true;
  try {
    const job = await api("/api/scan", {
      method: "POST",
      body: {
        device_path: path,
        mode: $("scanModeSel").value,
        surface_time_budget_s: budget === 0 ? null : budget,
        surface_limit_bytes: null,
      },
    });
    state.activeJob = {
      id: job.job_id,
      label: "Scan",
      progressBar: $("scanProgressBar"),
      stageNode: $("scanStage"),
      percentNode: $("scanPercent"),
      onDone: async () => {
        $("startScan").disabled = false;
        $("scanProgressBar").style.width = "100%";
        $("scanStage").textContent = "complete";
        await loadLastScan(path);
        loadHistoryStats();
        toast("Scan complete", "ok");
      },
    };
  } catch (error) {
    $("startScan").disabled = false;
    stopProgress($("scanProgressBar"));
    toast(error.message, "error");
  }
}

function stopProgress(bar) { if (bar) bar.style.width = "0%"; }

function renderScanResult() {
  const container = $("scanResult");
  const scan = state.lastScan;
  if (!scan) { container.innerHTML = ""; return; }
  const health = scan.health;
  const findings = (health.findings || []).map((finding) => `
    <div class="finding">
      <span class="finding-sev sev-${escapeHtml(finding.severity)}">${escapeHtml(finding.severity)}</span>
      <div><p>${escapeHtml(finding.message)}</p><code>${escapeHtml(finding.code)}</code></div>
    </div>`).join("") || '<p class="muted">No findings — nothing in the checked range failed.</p>';

  const smart = scan.smart
    ? (scan.smart.available
        ? `<div class="kv">
             <div><span>Health</span><strong>${dash(scan.smart.health_text || "reported")}</strong></div>
             <div><span>Temperature</span><strong>${scan.smart.temperature_c != null ? `${scan.smart.temperature_c} °C` : "not reported"}</strong></div>
             <div><span>Power-on hours</span><strong>${dash(scan.smart.power_on_hours ?? "not reported")}</strong></div>
             <div><span>Source</span><strong class="mono">${escapeHtml(scan.smart.source)}</strong></div>
           </div>`
        : `<p class="muted">SMART unavailable — ${escapeHtml(scan.smart.reason || "the device does not report it")}</p>`)
    : "";

  const surface = scan.surface
    ? `<p class="muted small">Scanned ${humanSize(scan.surface.scanned_bytes)} · ${scan.surface.read_errors?.length || 0} unreadable region(s)${
        scan.surface.read_speed_bps ? ` · ${(scan.surface.read_speed_bps / 1e6).toFixed(1)} MB/s` : ""}</p>`
    : "";

  const perf = scan.performance
    ? `<div class="kv">
         <div><span>Median read</span><strong>${scan.performance.median_mbps.toFixed(1)} MB/s</strong></div>
         <div><span>Peak</span><strong>${scan.performance.peak_mbps.toFixed(1)} MB/s</strong></div>
         <div><span>Rating</span><strong>${escapeHtml(scan.performance.rating)}</strong></div>
       </div>`
    : "";

  const limitations = (scan.limitations || []).map((note) => `<div class="warn-item">${escapeHtml(note)}</div>`).join("");

  container.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Result</h2><span class="badge ${verdictClass(health.verdict)}">${escapeHtml(health.verdict)}</span></div>
      <div class="verdict">
        <div class="verdict-badge" style="background:${escapeHtml(health.verdict_color || "#8b95a7")}22;color:${escapeHtml(health.verdict_color || "#8b95a7")};border:1px solid ${escapeHtml(health.verdict_color || "#8b95a7")}55">${escapeHtml(health.verdict)}</div>
        <div>
          <h3>${escapeHtml(scan.diagnosis.headline)}</h3>
          <p>${escapeHtml(scan.diagnosis.summary)}</p>
        </div>
      </div>
      <p class="muted small">Confidence: ${escapeHtml(health.health_confidence)} · ${scan.duration_s.toFixed(1)}s · mode ${escapeHtml(scan.mode)}</p>
      <h3>Findings</h3>
      ${findings}
      <h3>SMART</h3>
      ${smart || '<p class="muted">Not queried.</p>'}
      <h3>Surface</h3>
      ${surface || '<p class="muted">Not scanned.</p>'}
      <h3>Performance</h3>
      ${perf || '<p class="muted">Not benchmarked.</p>'}
      ${limitations ? `<h3>Limitations of this scan</h3><div class="warn-list">${limitations}</div>` : ""}
    </div>`;
}

/* ───────────────────────────── repair ───────────────────────────── */
async function loadPlans() {
  const path = $("repairDevice").value;
  if (!path) { toast("No device selected", "error"); return; }
  try {
    const data = await api("/api/plan", { method: "POST", body: { device_path: path, mode: $("repairModeSel").value } });
    state.plans = data.plans || [];
    $("repairMode").textContent = data.mode;
    renderPlans(state.plans);
    $("dryRunBtn").disabled = !state.plans.length;
    $("approveBtn").disabled = !state.plans.some((p) => p.requires_consent);
    if (!state.plans.length) toast("No repairs apply to this device", "warn");
  } catch (error) { toast(error.message, "error"); }
}

function renderPlans(plans) {
  const container = $("planList");
  if (!plans || !plans.length) {
    container.innerHTML = '<p class="muted">No operations apply (or no plan loaded yet).</p>';
    return;
  }
  container.innerHTML = plans.map((plan) => `
    <div class="plan ${plan.requires_consent ? "selected" : ""}" data-op="${escapeHtml(plan.operation_id)}">
      <input type="checkbox" ${plan.requires_consent ? "checked" : ""} ${plan.requires_consent ? "" : "disabled"}>
      <div class="plan-body">
        <div class="plan-title">
          <strong>${escapeHtml(plan.title)}</strong>
          <span class="badge ${plan.risk === "none" || plan.risk === "low" ? "ok" : plan.risk === "medium" ? "warn" : "bad"}">${escapeHtml(plan.risk)} risk</span>
          ${plan.verified_required ? '<span class="badge">verified</span>' : ""}
        </div>
        <p>${escapeHtml(plan.summary)}</p>
        <div class="why">${escapeHtml((plan.rationale || "").slice(0, 220))}</div>
      </div>
    </div>`).join("");
}

function selectedOperations() {
  return Array.from(document.querySelectorAll("#planList .plan"))
    .filter((node) => node.querySelector("input:checked") && !node.querySelector("input:disabled"))
    .map((node) => node.dataset.op);
}

function openApproveDialog() {
  const operations = selectedOperations();
  if (!operations.length) { toast("Select at least one operation", "warn"); return; }
  const list = $("approveList");
  list.innerHTML = state.plans.filter((p) => operations.includes(p.operation_id))
    .map((p) => `<li><strong>${escapeHtml(p.title)}</strong> — ${escapeHtml(p.risk)} risk<br><span class="muted small">${escapeHtml(p.summary)}</span></li>`)
    .join("");
  $("approveDialog").showModal();
}

async function confirmRepair() {
  $("approveDialog").close();
  const path = $("repairDevice").value;
  const operations = selectedOperations();
  try {
    const approval = await api("/api/repair/approve", {
      method: "POST",
      body: { device_path: path, operation_ids: operations, mode: $("repairModeSel").value },
    });
    const result = await api("/api/repair/execute", {
      method: "POST",
      body: {
        device_path: path,
        consent_token: approval.consent_token,
        operation_ids: operations,
        mode: $("repairModeSel").value,
        stop_on_failure: false,
      },
    });
    renderRepairResult(result);
    toast(`Repair finished: ${result.succeeded} succeeded, ${result.failed} failed`,
          result.failed ? "warn" : "ok");
    await loadLastScan(path);
    loadHistoryStats();
  } catch (error) { toast(error.message, "error"); }
}

async function dryRun() {
  const path = $("repairDevice").value;
  try {
    const result = await api("/api/repair/dry-run", {
      method: "POST",
      body: { device_path: path, operation_ids: selectedOperations(), mode: $("repairModeSel").value },
    });
    renderRepairResult(result);
    toast("Dry run finished — nothing was written", "ok");
  } catch (error) { toast(error.message, "error"); }
}

function renderRepairResult(result) {
  const container = $("repairResult");
  if (!result) { container.innerHTML = ""; return; }
  const rows = (result.steps || []).map((step) => `
    <tr>
      <td>${escapeHtml(step.plan.title)}</td>
      <td><span class="badge ${step.plan.risk === "none" || step.plan.risk === "low" ? "ok" : "warn"}">${escapeHtml(step.plan.risk)}</span></td>
      <td>${escapeHtml(step.result.status)}</td>
      <td>${step.result.verified === true ? '<span class="badge ok">verified</span>' : step.result.verified === false ? '<span class="badge bad">not verified</span>' : "—"}</td>
      <td class="muted small">${escapeHtml(step.result.message)}</td>
    </tr>`).join("");
  container.innerHTML = `
    <div class="card">
      <div class="card-head"><h2>Repair result</h2><span class="badge ${result.failed ? "warn" : "ok"}">${result.succeeded} succeeded · ${result.failed} failed</span></div>
      <div class="table-wrap"><table class="table">
        <thead><tr><th>Operation</th><th>Risk</th><th>Status</th><th>Verified</th><th>Detail</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="5" class="muted">Nothing ran.</td></tr>'}</tbody>
      </table></div>
      ${result.aborted ? `<p class="warn-item">Stopped: ${escapeHtml(result.abort_reason)}</p>` : ""}
    </div>`;
}

async function autoRepair() {
  const path = state.selected || $("scanDevice").value;
  if (!path) { toast("Select a drive first", "error"); return; }
  const mode = $("scanModeSel").value;
  try {
    const plan = await api("/api/plan", { method: "POST", body: { device_path: path, mode: "safe" } });
    const safe = (plan.plans || []).filter((p) => (p.risk === "none" || p.risk === "low") && p.requires_consent);
    const preview = safe.length
      ? safe.map((p) => `• ${p.title} (${p.risk} risk)`).join("\n")
      : "No automatically-safe repairs apply to this drive.";
    const ok = window.confirm(
      `Scan & Repair Automatically\n\nDevice: ${path}\nMode: ${mode}\n\n` +
      `Safe operations that will run:\n${preview}\n\n` +
      "A rollback file is written before every change. Continue?"
    );
    if (!ok) return;
    $("autoBtn").disabled = true;
    const result = await api("/api/repair/emergency", {
      method: "POST",
      body: { device_path: path, mode, confirm: true },
    });
    if (result.performed) {
      renderRepairResult(result.repair);
      toast(`Automatic repair finished: ${result.repair.succeeded} succeeded`, result.repair.failed ? "warn" : "ok");
    } else {
      toast(result.reason || "Nothing safe to run automatically", "warn");
    }
    await loadLastScan(path);
  } catch (error) {
    toast(error.message, "error");
  }
  $("autoBtn").disabled = false;
}

/* ───────────────────────────── recovery ───────────────────────────── */
async function createImage() {
  const path = state.selected || $("scanDevice").value;
  const output = $("imageOut").value.trim();
  if (!path) { toast("Select a drive first", "error"); return; }
  if (!output) { toast("Enter an image destination path", "error"); return; }
  if (!window.confirm(`Create a full image of ${path}?\n\nDestination: ${output}\n\nThis reads the device and writes only to the destination file.`)) return;
  $("recoveryProgress").classList.remove("hidden");
  $("recoveryBar").style.width = "0%";
  $("recoveryStage").textContent = "imaging…";
  try {
    const result = await api("/api/backup/image", { method: "POST", body: { device_path: path, output_path: output } });
    $("recoveryBar").style.width = "100%";
    $("recoveryResult").innerHTML = `
      <div class="card">
        <div class="card-head"><h2>Image complete</h2><span class="badge ok">verified length</span></div>
        <div class="kv">
          <div><span>Image</span><strong class="mono">${escapeHtml(result.image_path)}</strong></div>
          <div><span>Good bytes</span><strong>${humanSize(result.good_bytes)}</strong></div>
          <div><span>Bad regions</span><strong>${(result.bad_regions || []).length}</strong></div>
          <div><span>Engine</span><strong>${escapeHtml(result.engine)}</strong></div>
        </div>
      </div>`;
    toast("Image created", "ok");
  } catch (error) {
    toast(error.message, "error");
    $("recoveryProgress").classList.add("hidden");
  }
}

async function recoverFiles() {
  const path = state.selected || $("scanDevice").value;
  if (!path) { toast("Select a drive first", "error"); return; }
  const method = $("recoveryMethod").value;
  const outputDir = $("recoveryOut").value.trim() || null;
  if (!window.confirm(
    `Recover files from ${path}?\n\nMethod: ${method}\nOutput: ${outputDir || "app data folder"}\n\n` +
    "The source device is never written to."
  )) return;
  $("recoveryProgress").classList.remove("hidden");
  $("recoveryBar").style.width = "0%";
  $("recoveryStage").textContent = "recovering…";
  try {
    const result = await api("/api/recovery/run", {
      method: "POST",
      body: { device_path: path, method, output_dir: outputDir, include_deleted: $("includeDeleted").checked },
    });
    $("recoveryBar").style.width = "100%";
    const files = result.files_recovered ?? 0;
    $("recoveryResult").innerHTML = `
      <div class="card">
        <div class="card-head"><h2>Recovery complete</h2><span class="badge ${files ? "ok" : "warn"}">${files} file(s)</span></div>
        <div class="kv">
          <div><span>Output folder</span><strong class="mono">${escapeHtml(result.output_dir || outputDir || "—")}</strong></div>
          <div><span>Bytes recovered</span><strong>${humanSize(result.bytes_recovered ?? result.bytes_written)}</strong></div>
          <div><span>Method</span><strong>${escapeHtml(result.method || method)}</strong></div>
        </div>
        ${files === 0 ? '<p class="warn-item">No files were recovered. If the metadata is unreadable, try content carving, or image the device first and carve from the image.</p>' : ""}
      </div>`;
    toast(`Recovered ${files} file(s)`, files ? "ok" : "warn");
  } catch (error) {
    toast(error.message, "error");
    $("recoveryProgress").classList.add("hidden");
  }
}

/* ───────────────────────────── performance ───────────────────────────── */
function renderPerformance() {
  const scan = state.lastScan;
  if (!scan) {
    $("perfResult").innerHTML = '<p class="muted">Run a scan on a device to measure its speed.</p>';
    return;
  }
  const perf = scan.performance;
  if (!perf) { $("perfResult").innerHTML = '<p class="muted">This scan did not benchmark the device.</p>'; return; }
  $("perfResult").innerHTML = `
    <div class="kv">
      <div><span>Median</span><strong>${perf.median_mbps.toFixed(1)} MB/s</strong></div>
      <div><span>Peak</span><strong>${perf.peak_mbps.toFixed(1)} MB/s</strong></div>
      <div><span>Slowest</span><strong>${perf.min_mbps.toFixed(1)} MB/s</strong></div>
      <div><span>Rating</span><strong>${escapeHtml(perf.rating)}</strong></div>
    </div>
    <p class="muted small">${escapeHtml(perf.note || "")}</p>`;
}

async function verifyCapacity() {
  const path = state.selected || $("scanDevice").value;
  if (!path) { toast("Select a drive first", "error"); return; }
  if (!$("capacityConsent").checked) { toast("Tick the consent box first", "warn"); return; }
  if (!window.confirm(
    `Verify the real capacity of ${path}?\n\n` +
    "This writes a temporary pattern file to the drive, reads it back, then deletes it.\n" +
    "Do not interrupt the process."
  )) return;
  $("capacityBtn").disabled = true;
  $("capacityResult").innerHTML = '<p class="muted"><span class="spin"></span> writing and verifying…</p>';
  try {
    const approval = await api("/api/repair/approve", { method: "POST", body: { device_path: path, mode: "advanced", operation_ids: [] } });
    const result = await api("/api/capacity/test", {
      method: "POST",
      body: { device_path: path, consent_token: approval.consent_token },
    });
    const cls = result.verdict === "genuine" ? "ok" : result.verdict === "counterfeit" ? "crit" : "warn";
    $("capacityResult").innerHTML = `
      <div class="kv">
        <div><span>Verdict</span><strong>${escapeHtml(result.verdict)}</strong></div>
        <div><span>Verified</span><strong>${humanSize(result.tested_bytes)}</strong></div>
        <div><span>Real capacity</span><strong>${result.real_capacity_estimate ? humanSize(result.real_capacity_estimate) : "not determined"}</strong></div>
      </div>
      <p class="muted small">${escapeHtml(result.message)}</p>
      <span class="badge ${cls}">${escapeHtml(result.verdict)}</span>`;
    toast(result.message, cls === "ok" ? "ok" : "warn");
  } catch (error) {
    $("capacityResult").innerHTML = `<p class="warn-item">${escapeHtml(error.message)}</p>`;
    toast(error.message, "error");
  }
  $("capacityBtn").disabled = false;
}

/* ───────────────────────────── assistant ───────────────────────────── */
function renderAssistant() {
  const scan = state.lastScan;
  const container = $("assistantBody");
  if (!scan) {
    container.innerHTML = '<p class="muted">Run a scan to get a diagnosis.</p>';
    $("aiConfidence").textContent = "—";
    return;
  }
  const diagnosis = scan.diagnosis;
  $("aiConfidence").textContent = `confidence: ${diagnosis.confidence}`;
  const sections = (diagnosis.sections || []).map((section) => `
    <h3>${escapeHtml(section.heading)}</h3>
    <ul class="steps">${(section.bullets || []).map((b) => `<li>${escapeHtml(b)}</li>`).join("")}</ul>`).join("");
  const knowledge = (diagnosis.knowledge || []).map((entry) => `
    <div class="finding">
      <span class="finding-sev ${entry.software_fixable ? "sev-info" : "sev-warning"}">${entry.software_fixable ? "fixable" : "hardware"}</span>
      <div>
        <p><strong>${escapeHtml(entry.title)}</strong></p>
        <ul class="steps">${(entry.steps || []).slice(0, 4).map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ul>
        ${(entry.sources || []).map((url) => `<code><a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(url)}</a></code>`).join("<br>")}
        ${entry.when_not_to && entry.when_not_to.length ? `<p class="warn-item">${escapeHtml(entry.when_not_to.join(" "))}</p>` : ""}
      </div>
    </div>`).join("");
  container.innerHTML = `
    <div class="verdict">
      <div class="verdict-badge" style="background:var(--accent-soft);color:var(--accent-2);border:1px solid var(--line-strong)">AI</div>
      <div><h3>${escapeHtml(diagnosis.headline)}</h3><p>${escapeHtml(diagnosis.summary)}</p></div>
    </div>
    ${sections}
    ${diagnosis.recommended_steps && diagnosis.recommended_steps.length
      ? `<h3>Recommended order</h3><ol class="steps">${diagnosis.recommended_steps.map((s) => `<li>${escapeHtml(s)}</li>`).join("")}</ol>` : ""}
    ${knowledge ? `<h3>Related known issues</h3>${knowledge}` : ""}
    ${diagnosis.friendly_message ? `<div class="failure-card"><div class="failure-face">${diagnosis.replacement_advised ? "😅" : "✓"}</div><div><strong>${escapeHtml(diagnosis.friendly_message)}</strong></div></div>` : ""}`;
}

async function researchModel() {
  const path = state.selected || $("scanDevice").value;
  if (!path) { toast("Select a drive first", "error"); return; }
  $("researchBody").innerHTML = '<p class="muted"><span class="spin"></span> checking official sources…</p>';
  try {
    const data = await api("/api/research", { method: "POST", body: { device_path: path } });
    const pages = (data.official_pages || []).map((page) => `
      <div class="finding">
        <span class="finding-sev ${page.status === "ok" ? "sev-info" : "sev-warning"}">${escapeHtml(page.status)}</span>
        <div>
          <p><strong>${escapeHtml(page.title || page.url)}</strong></p>
          <code><a href="${escapeHtml(page.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(page.url)}</a></code>
          ${page.error ? `<p class="muted small">${escapeHtml(page.error)}</p>` : ""}
        </div>
      </div>`).join("");
    const links = (data.vendor_links || []).map((link) =>
      `<li><a href="${escapeHtml(link.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(link.title)}</a> — ${escapeHtml(link.notes)}</li>`
    ).join("");
    const queries = (data.queries || []).map((q) => `<li class="mono small">${escapeHtml(q)}</li>`).join("");
    $("researchBody").innerHTML = `
      <h3>Search terms built from this exact device</h3><ul class="steps">${queries}</ul>
      <h3>Official pages</h3>${pages || '<p class="muted">No page could be fetched.</p>'}
      <h3>Vendor support portals</h3><ul class="steps">${links}</ul>
      <p class="muted small">${escapeHtml(data.disclaimer || "")}</p>`;
  } catch (error) {
    $("researchBody").innerHTML = `<p class="warn-item">${escapeHtml(error.message)}</p>`;
  }
}

/* ───────────────────────────── history ───────────────────────────── */
async function loadHistory() {
  try {
    const data = await api("/api/history");
    const scans = data.recent_scans || [];
    $("historyTable").querySelector("tbody").innerHTML = scans.length ? scans.map((scan) => `
      <tr>
        <td class="mono small">${escapeHtml((scan.started_at || "").replace("T", " ").slice(0, 19))}</td>
        <td>${escapeHtml(scan.device_name || scan.device_path)}<div class="mono muted small">${escapeHtml(scan.device_path)}</div></td>
        <td><span class="badge ${verdictClass(scan.verdict)}">${escapeHtml(scan.verdict || "—")}</span></td>
        <td>${escapeHtml(scan.mode || "—")}</td>
        <td><button class="btn ghost small" data-scanid="${scan.id}">View</button></td>
      </tr>`).join("") : '<tr><td colspan="5" class="muted">No scans recorded yet.</td></tr>';

    const repairs = data.recent_repairs || [];
    $("repairHistoryTable").querySelector("tbody").innerHTML = repairs.length ? repairs.map((repair) => `
      <tr>
        <td class="mono small">${escapeHtml((repair.started_at || "").replace("T", " ").slice(0, 19))}</td>
        <td>${escapeHtml(repair.title || repair.operation_id)}</td>
        <td>${escapeHtml(repair.risk || "—")}</td>
        <td><span class="badge ${repair.status === "succeeded" ? "ok" : repair.status === "failed" ? "bad" : "warn"}">${escapeHtml(repair.status || "—")}</span></td>
        <td>${repair.verified === 1 ? '<span class="badge ok">verified</span>' : repair.verified === 0 ? '<span class="badge bad">not verified</span>' : "—"}</td>
      </tr>`).join("") : '<tr><td colspan="5" class="muted">No repairs recorded yet.</td></tr>';
  } catch (error) { toast(error.message, "error"); }
}

/* ───────────────────────────── reports ───────────────────────────── */
function renderReportPlaceholder() {
  if (!state.lastScan) {
    $("reportBody").textContent = "Run a scan first, then generate a report.";
    return;
  }
  $("reportBody").textContent = "Choose an export format above.";
}

async function generateReport(format) {
  const path = state.selected;
  if (!path) { toast("Select a drive first", "error"); return; }
  try {
    const response = await fetch(`/api/report?path=${encodeURIComponent(path)}&format=${format}&token=${encodeURIComponent(state.token || "")}`);
    const text = await response.text();
    if (!response.ok) {
      let message = text;
      try { message = JSON.parse(text).error; } catch { /* plain text body */ }
      throw new Error(message);
    }
    if (format === "html") {
      const blob = new Blob([text], { type: "text/html" });
      window.open(URL.createObjectURL(blob), "_blank", "noopener");
      toast("HTML report opened in a new tab", "ok");
    } else {
      $("reportBody").textContent = text;
      const blob = new Blob([text], { type: "text/markdown" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "usbdoctor-report.md";
      link.click();
      toast("Markdown report downloaded", "ok");
    }
  } catch (error) { toast(error.message, "error"); }
}

/* ───────────────────────────── settings ───────────────────────────── */
async function loadSettings() {
  try {
    const data = await api("/api/settings");
    state.settings = data.settings || {};
    $("setInternal").checked = Boolean(state.settings.allow_internal_disks);
    $("setOffline").checked = Boolean(state.settings.offline_mode);
    $("setMounted").checked = Boolean(state.settings.allow_mounted_write);
    renderTools(data.tools);
  } catch (error) { toast(error.message, "error"); }
}

function renderTools(tools) {
  const container = $("toolsList");
  if (!container || !tools) return;
  container.innerHTML = Object.entries(tools).map(([name, present]) =>
    `<div class="tool"><span class="mono">${escapeHtml(name)}</span><span class="${present ? "yes" : "no"}">${present ? "found" : "missing"}</span></div>`
  ).join("");
}

function renderAbout(status) {
  $("aboutBody").innerHTML = `
    <p><strong>${escapeHtml(status.app)} ${escapeHtml(status.version)}</strong> · ${escapeHtml(status.platform)} ·
    ${status.is_admin ? "administrator" : "standard user"} · state: <span class="mono">${escapeHtml(status.state_dir)}</span></p>
    <p>USB Doctor AI performs real reads and writes on your storage hardware. Repairs are limited to
    file-system metadata that can be rebuilt from on-disk copies. It cannot repair physically damaged
    memory, and it will say so instead of pretending.</p>`;
}

async function saveSettings() {
  try {
    const body = {
      allow_internal_disks: $("setInternal").checked,
      offline_mode: $("setOffline").checked,
      allow_mounted_write: $("setMounted").checked,
    };
    await api("/api/settings", { method: "POST", body });
    Object.assign(state.settings, body);
    toast("Settings saved", "ok");
    await loadDevices();
  } catch (error) { toast(error.message, "error"); }
}

/* Keep the UI honest if the local engine restarts. */
setInterval(() => {
  if (!state.eventSource || state.eventSource.readyState === EventSource.CLOSED) connectEvents();
  loadHistoryStats();
}, 20000);
