/* AURA app wiring.
 *
 * One narrative, driven entirely by real backend state:
 *   LightGBM forecast -> stress event (UNMANAGED consequence) -> judge sees
 *   the overload -> [AURA REBALANCE] -> real DC-OPF + power-flow validation
 *   -> before/after proof.
 *
 * Two rules this file exists to enforce:
 *  1. Nothing is computed here that the backend already computes. Every number
 *     rendered comes from /api/state, /api/forecast, or an intervention
 *     response. The only local derivations are presentational (region labels
 *     from real coordinates, "power at risk" summed from live flows).
 *  2. A stress event NEVER triggers the optimizer. /api/disturbance shows the
 *     unmanaged grid; only an explicit click calls /api/optimize.
 */

const el = (id) => document.getElementById(id);

const S = {
  config: null,          // /api/config -- thresholds + loading bands
  state: null,           // /api/state
  preview: null,         // /api/optimize/preview (uncommitted)
  lastOptimize: null,    // last committed /api/optimize response
  forecast: null,        // /api/forecast
  optimizing: false,
  centroid: null,
};

const REROUTE_HOLD_MS = 5000;

/* ---------- transport ---------- */

async function api(path, opts, busyLabel) {
  if (busyLabel) { el("busyText").textContent = busyLabel; el("busy").classList.remove("hidden"); }
  try {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { /* non-JSON error */ }
      throw new Error(detail);
    }
    return await res.json();
  } finally {
    if (busyLabel) el("busy").classList.add("hidden");
  }
}

/* ---------- small presentational helpers ---------- */

const fmtMW = (mw) => `${Math.round(mw).toLocaleString()} MW`;
const pct = (frac) => `${Math.round(frac * 100)}%`;

/* Compass region from real coordinates, used only to phrase the optimizer's
   "from X to Y" sentence in words a judge can read. */
function regionOf(lat, lon) {
  if (!S.centroid) return "";
  const ns = lat >= S.centroid.lat + 0.02 ? "North" : lat <= S.centroid.lat - 0.02 ? "South" : "";
  const ew = lon >= S.centroid.lon + 0.02 ? "East" : lon <= S.centroid.lon - 0.02 ? "West" : "";
  return (ns + (ns && ew ? "-" : "") + ew) || "Central";
}

function corridorRegions(keys) {
  if (!S.state) return [];
  const at = Object.fromEntries(S.state.substations.map((s) => [s.name, s]));
  const regions = new Set();
  for (const key of keys) {
    for (const name of key.split("|")) {
      const s = at[name];
      if (s) regions.add(regionOf(s.lat, s.lon));
    }
  }
  return [...regions];
}

function bandFor(loading) {
  const bands = S.config?.loading_bands || [];
  for (const b of bands) if (b.max == null || loading < b.max) return b;
  return bands[bands.length - 1] || { key: "safe", label: "Safe" };
}

/* ---------- event log: only meaningful grid events ---------- */

function logEvent(text, kind = "") {
  const div = document.createElement("div");
  div.className = `ev ${kind}`;
  const d = new Date();
  const ts = [d.getHours(), d.getMinutes(), d.getSeconds()]
    .map((n) => String(n).padStart(2, "0")).join(":");
  div.innerHTML = `<span class="t">${ts}</span>${text}`;
  const feed = el("feed");
  feed.appendChild(div);
  feed.scrollTop = feed.scrollHeight;
}

/* Narrate one real response as a sequence of lines. Every value shown is a
   field from that response -- no invented intermediate events. */
function logStress(result) {
  const t = result.trigger, ra = result.risk_after;
  if (t.type === "temperature") logEvent(`Heatwave stress applied — +${t.temperature_delta_c}°C`, "warn");
  if (t.type === "demand") logEvent(`Demand surge applied — ×${t.demand_multiplier}`, "warn");
  if (t.type === "line_failure") logEvent(`Corridor tripped — ${t.corridor} (${t.count} circuits)`, "danger");
  if (t.citywide_forecast_mw) logEvent(`Demand now ${fmtMW(t.citywide_forecast_mw)} citywide`, "warn");

  if (ra.overloaded_corridors > 0) {
    logEvent(`${ra.overloaded_corridors} corridor${ra.overloaded_corridors > 1 ? "s" : ""} overloaded — unmanaged response`, "danger");
    for (const key of (ra.overloaded_corridor_keys || []).slice(0, 3)) {
      const c = S.state?.corridors?.[key];
      if (c) logEvent(`${key.replace("|", " ⇄ ")} — ${Math.round(c.flow_mw)}/${Math.round(c.capacity_mw)} MW — ${pct(c.max_circuit_loading)}`, "danger");
    }
  } else if (ra.corridors_at_risk > 0) {
    logEvent(`${ra.corridors_at_risk} corridor${ra.corridors_at_risk > 1 ? "s" : ""} at risk`, "warn");
  } else {
    logEvent("Grid absorbed the stress — no corridors at risk", "ok");
  }
}

function logOptimize(result) {
  const rb = result.risk_before, ra = result.risk_after;
  logEvent("AURA optimizer started", "aura");
  logEvent(`${Math.round(result.mw_rerouted)} MW redistribution found`, "aura");

  const reroutes = result.actions.filter((a) => a.kind === "reroute");
  if (reroutes.length) logEvent(`${reroutes.length} alternate corridors activated`, "aura");

  // per-corridor before -> after, straight from utilisation_before/after
  const improved = (result.affected_corridors || [])
    .map((k) => ({ k, before: result.utilisation_before[k] ?? 0, after: result.utilisation_after[k] ?? 0 }))
    .filter((x) => x.before - x.after > 0.02)
    .sort((a, b) => (b.before - b.after) - (a.before - a.after))
    .slice(0, 3);
  for (const x of improved) {
    logEvent(`✓ ${x.k.replace("|", " ⇄ ")} loading reduced — ${pct(x.before)} → ${pct(x.after)}`, "ok");
  }

  logEvent(`✓ Max loading ${pct(rb.max_circuit_loading)} → ${pct(ra.max_circuit_loading)}`, "ok");
  if (result.power_flow_validated) logEvent("✓ Power flow validated", "ok");
  else logEvent("Power flow still reports overloads", "danger");

  if (ra.overloaded_corridors === 0 && ra.total_shed_mw <= 0.1) {
    logEvent("✓ Grid stabilized — 0 overloaded corridors", "ok");
  } else if (ra.overloaded_corridors === 0) {
    logEvent(`Partially stabilized — ${fmtMW(ra.total_shed_mw)} shed`, "warn");
  } else {
    logEvent(`${ra.overloaded_corridors} corridors still overloaded`, "danger");
  }
}

/* ---------- header status cards ---------- */

function paintHeader() {
  const st = S.state, r = st.risk;
  const phaseLabel = { stable: "STABLE", at_risk: "AT RISK", overload: "OVERLOAD", shedding: "SHEDDING" };
  const phaseClass = { stable: "ok", at_risk: "warn", overload: "bad", shedding: "bad" };

  const g = el("statGrid");
  g.textContent = S.optimizing ? "OPTIMIZING" : (phaseLabel[st.phase] || "—");
  g.className = `mono ${S.optimizing ? "" : phaseClass[st.phase] || ""}`;

  el("statDemand").textContent = fmtMW(r.total_served_mw);
  el("statMaxLoad").textContent = pct(r.max_circuit_loading);
  el("statMaxLoad").className = `mono ${r.max_circuit_loading >= 1 ? "bad" : r.max_circuit_loading >= 0.9 ? "warn" : "ok"}`;
  el("statOverloaded").textContent = r.overloaded_corridors;
  el("statOverloaded").className = `mono ${r.overloaded_corridors ? "bad" : "ok"}`;
  el("statShed").textContent = fmtMW(Math.max(0, r.total_shed_mw));
  el("statShed").className = `mono ${r.total_shed_mw > 0.1 ? "bad" : "ok"}`;

  const peak = S.forecast?.future?.length
    ? Math.max(...S.forecast.future.map((f) => f.predicted_mw)) : null;
  el("statPeak").textContent = peak ? fmtMW(peak) : "—";
}

/* ---------- forecast + insight ---------- */

function paintForecast() {
  const f = S.forecast;
  if (!f) return;
  ForecastChart.render(el("chart"), f);

  const last = f.past[f.past.length - 1];
  el("forecastNow").textContent = last ? `${Math.round(last.actual_mw)} MW now` : "";

  if (!f.future.length) return;
  const peakPt = f.future.reduce((a, b) => (b.predicted_mw > a.predicted_mw ? b : a));
  const change = last ? (peakPt.predicted_mw - last.actual_mw) / last.actual_mw : 0;

  el("insPeak").textContent = fmtMW(peakPt.predicted_mw);
  el("insTime").textContent = peakPt.ts.slice(11, 16);
  el("insChange").textContent = `${change >= 0 ? "+" : ""}${(change * 100).toFixed(1)}%`;
  el("insChange").className = `mono ${change > 0.05 ? "warn" : ""}`;
  el("insImpact").textContent = S.state
    ? `${S.state.risk.corridors_at_risk} at risk`
    : "—";
}

/* ---------- grid alerts ---------- */

function paintAlerts() {
  const st = S.state;
  const threshold = S.config?.at_risk_threshold ?? 0.9;
  const box = el("alertList");
  box.innerHTML = "";

  const rows = Object.entries(st.corridors)
    .map(([key, c]) => ({ key, c }))
    .filter(({ c }) => c.circuits_live === 0 || c.max_circuit_loading >= threshold)
    .sort((a, b) => b.c.max_circuit_loading - a.c.max_circuit_loading);

  el("alertCount").textContent = rows.length ? `${rows.length}` : "";
  if (!rows.length) {
    box.innerHTML = '<div class="empty-note">No corridors at risk.</div>';
    return;
  }

  for (const { key, c } of rows) {
    const failed = c.circuits_live === 0;
    const band = failed ? { key: "failed", label: "Failed" } : bandFor(c.max_circuit_loading);
    const div = document.createElement("div");
    div.className = `alert ${band.key}`;
    div.innerHTML =
      `<div class="a-top"><span class="a-name">${c.from} ⇄ ${c.to}</span>` +
      `<span class="a-band">${band.label}</span></div>` +
      `<div class="a-detail">${failed
        ? `TRIPPED · ${c.circuits_total} circuits out`
        : `${Math.round(c.flow_mw)} / ${Math.round(c.capacity_mw)} MW · ${pct(c.max_circuit_loading)} · ${c.voltage_kv} kV`}</div>`;
    // clicking an alert selects the exact same physical asset on the map --
    // both read state.corridors[key], so they can never disagree
    div.onclick = () => GridMap.focusCorridorKey(key);
    box.appendChild(div);
  }
}

/* ---------- optimizer card ---------- */

function powerAtRiskMW() {
  const limit = S.config?.overload_threshold ?? 1.0;
  return Object.values(S.state.corridors).reduce((sum, c) => {
    const over = Math.abs(c.flow_mw) - c.capacity_mw * limit;
    return sum + Math.max(0, over);
  }, 0);
}

function paintOptimizer() {
  const st = S.state, r = st.risk;
  const stable = st.phase === "stable";
  const body = el("optBody");
  const pill = el("optState");
  const btn = el("btnOptimize");

  if (S.optimizing) {
    pill.textContent = "OPTIMIZING…"; pill.className = "pill busy";
    btn.disabled = true;
    body.innerHTML = '<div class="empty-note">Searching alternate power-flow paths…</div>';
    return;
  }

  if (stable) {
    pill.textContent = S.lastOptimize ? "INTERVENTION COMPLETE" : "STABLE";
    pill.className = `pill ${S.lastOptimize ? "done" : ""}`;
    btn.disabled = true;
    body.innerHTML = S.lastOptimize
      ? '<div class="empty-note">Grid stable after AURA intervention. Nothing further to fix.</div>'
      : '<div class="empty-note">Grid is stable. Nothing for AURA to fix right now.</div>';
    return;
  }

  pill.textContent = "READY TO INTERVENE";
  pill.className = "pill ready";
  btn.disabled = false;

  const atRisk = powerAtRiskMW();
  const rows = [
    `<div class="opt-row"><label>Current grid risk</label><b class="${r.overloaded_corridors ? "hot" : ""}">` +
      `${r.overloaded_corridors} overloaded · ${r.corridors_at_risk} at risk</b></div>`,
    `<div class="opt-row"><label>Power at risk</label><b>${fmtMW(atRisk)}</b>` +
      `<span class="sub">flow above safe capacity on overloaded corridors</span></div>`,
  ];

  if (S.preview && S.preview.accepted) {
    const paths = S.preview.actions.filter((a) => a.kind === "reroute").length;
    rows.push(
      `<div class="opt-row"><label>AURA can</label><b>Redistribute ${fmtMW(S.preview.mw_rerouted)}</b>` +
      `<span class="sub">through ${paths} alternate path${paths === 1 ? "" : "s"}</span></div>`
    );
  }
  body.innerHTML = rows.join("");
}

/* ---------- validation card ---------- */

function paintValidation() {
  const box = el("validationBody");
  const res = S.lastOptimize;
  if (!res) {
    box.innerHTML = '<div class="empty-note">Waiting for AURA intervention.</div>';
    return;
  }
  const rb = res.risk_before, ra = res.risk_after;

  const row = (label, before, after, betterWhenLower = true) => {
    const good = betterWhenLower ? after <= before : after >= before;
    return `<div class="vl">${label}</div>` +
      `<div class="vv before">${before}</div>` +
      `<div class="vv">→</div>` +
      `<div class="vv after ${good ? "good" : "bad"}">${after}</div><div></div>`;
  };

  let status, cls;
  if (ra.overloaded_corridors === 0 && ra.total_shed_mw <= 0.1) {
    status = "✓ GRID STABILIZED — blackout avoided, 0 MW shed"; cls = "good";
  } else if (ra.overloaded_corridors === 0) {
    status = `PARTIALLY STABILIZED — ${fmtMW(ra.total_shed_mw)} load shed was unavoidable`; cls = "partial";
  } else {
    status = `CRITICAL — ${ra.overloaded_corridors} corridors still overloaded`; cls = "bad";
  }

  box.innerHTML =
    '<div class="val-table">' +
    '<div class="vh"></div><div class="vh">Before</div><div class="vh"></div><div class="vh">After</div><div></div>' +
    row("Max loading", pct(rb.max_circuit_loading), pct(ra.max_circuit_loading)) +
    row("Overloaded corridors", rb.overloaded_corridors, ra.overloaded_corridors) +
    row("Stressed substations", rb.stressed_substations, ra.stressed_substations) +
    row("Power moved", "0 MW", fmtMW(res.mw_rerouted), false) +
    row("Load shed", fmtMW(Math.max(0, rb.total_shed_mw)), fmtMW(Math.max(0, ra.total_shed_mw))) +
    "</div>" +
    `<div class="val-status ${cls}">${status}</div>`;
}

/* ---------- technical drawer ---------- */

function paintTechnical() {
  if (S.forecast) el("techHorizon").textContent = `+${S.forecast.future.length}h`;
  if (S.state) {
    el("techNetwork").textContent =
      `${S.state.substations.length} substations · ${Object.keys(S.state.corridors).length} corridors`;
  }
  const res = S.lastOptimize;
  if (res) {
    const sw = res.actions.filter((a) => a.kind.startsWith("switch")).length;
    const paths = res.actions.filter((a) => a.kind === "reroute").length;
    el("aiText").textContent =
      `${res.message} ${paths} corridors re-dispatched, ${sw} circuits switched, ` +
      `${Math.round(res.estimated_loss_mw)} MW estimated losses.`;
    el("rejectedList").innerHTML = res.rejected_alternatives.length
      ? res.rejected_alternatives.map((r) =>
          `attempt ${r.attempt}: ${r.reason}` +
          (r.violating_assets?.length
            ? ` [${r.violating_assets.slice(0, 3).map((a) => `${a.corridor} @ ${Math.round(a.loading_pct)}%`).join(", ")}]`
            : "")).join("<br>")
      : "none — first dispatch validated";
  }
}

/* ---------- legend (both encodings, from /api/config) ---------- */

function paintLegend() {
  const bands = S.config?.loading_bands || [];
  el("legend").innerHTML =
    '<div class="lg-label">Power flow (thickness)</div>' +
    '<div class="lg-row"><i class="taper"></i><span>low MW → high MW</span></div>' +
    '<div class="lg-label">Grid loading (colour)</div>' +
    '<div class="lg-row">' +
    bands.map((b) => `<span class="lg-band"><i class="sw" style="background:${b.color}"></i>${b.label}</span>`).join("") +
    "</div>";
}

/* ---------- the one render path ---------- */

function paintAll() {
  paintHeader();
  paintForecast();
  paintAlerts();
  paintOptimizer();
  paintValidation();
  paintTechnical();
}

async function refreshState(rerouteKeys = []) {
  S.state = await api("/api/state");
  GridMap.render(S.state, rerouteKeys);
  paintAll();
  if (rerouteKeys.length) {
    // the gold overlay marks what AURA just changed, then fades -- the real
    // thickness/colour encoding underneath stays at the new values
    setTimeout(() => { GridMap.render(S.state, []); }, REROUTE_HOLD_MS);
  }
}

/* Preview is only meaningful when there is something to fix. */
async function refreshPreview() {
  if (!S.state || S.state.phase === "stable") { S.preview = null; return; }
  try {
    S.preview = await api("/api/optimize/preview");
  } catch (e) {
    console.warn("preview unavailable", e);
    S.preview = null;
  }
  paintOptimizer();
}

/* ---------- actions ---------- */

async function simulateStress() {
  const tempDelta = Number(el("temp").value);
  const demandPct = Number(el("dem").value);
  if (!tempDelta && !demandPct) {
    logEvent("Set a temperature or demand lever first", "warn");
    return;
  }

  // each lever is a real, separate stress call; state persists and compounds
  if (tempDelta) {
    const r = await api("/api/disturbance", {
      method: "POST", body: JSON.stringify({ kind: "temperature", temperature_delta_c: tempDelta }),
    }, "applying heatwave…");
    await refreshState();
    logStress(r);
  }
  if (demandPct) {
    const r = await api("/api/disturbance", {
      method: "POST", body: JSON.stringify({ kind: "demand", demand_multiplier: 1 + demandPct / 100 }),
    }, "applying demand surge…");
    await refreshState();
    logStress(r);
  }
  await refreshPreview();
}

async function tripCorridor(key, props) {
  const r = await api("/api/disturbance", {
    method: "POST", body: JSON.stringify({ kind: "line_failure", corridor: key }),
  }, `tripping ${props.from} ⇄ ${props.to}…`);
  await refreshState();
  logStress(r);
  await refreshPreview();
}

async function runOptimizer() {
  S.optimizing = true;
  paintOptimizer();
  paintHeader();
  try {
    const r = await api("/api/optimize", { method: "POST" }, "AURA optimizing dispatch…");
    S.lastOptimize = r;
    S.preview = null;
    S.optimizing = false;
    await refreshState(r.affected_corridors || []);
    logOptimize(r);
  } finally {
    S.optimizing = false;
    paintOptimizer();
  }
  await refreshPreview();
}

function guard(fn) {
  return async (...args) => {
    try { await fn(...args); }
    catch (e) { console.error(e); logEvent(`Request failed: ${e.message}`, "danger"); }
  };
}

/* ---------- wiring ---------- */

el("temp").oninput = (e) => (el("tempOut").textContent = `+${e.target.value}°C`);
el("dem").oninput = (e) => (el("demOut").textContent = `+${e.target.value}%`);

el("btnSimulate").onclick = guard(simulateStress);
el("btnOptimize").onclick = guard(runOptimizer);

el("btnReset").onclick = guard(async () => {
  S.state = await api("/api/reset", { method: "POST" }, "resetting grid…");
  S.lastOptimize = null;
  S.preview = null;
  el("feed").innerHTML = "";
  el("temp").value = 0; el("tempOut").textContent = "+0°C";
  el("dem").value = 0; el("demOut").textContent = "+0%";
  el("aiText").textContent = "Grid calibrated. Awaiting a stress event.";
  S.forecast = await api("/api/forecast?hours=12");
  GridMap.render(S.state, []);
  paintAll();
  logEvent("Grid reset to calibrated baseline", "ok");
});

el("btnRecenter").onclick = () => GridMap.recenter();
el("btnPitch").onclick = (e) => e.currentTarget.classList.toggle("active", GridMap.togglePitch());
el("btnLabels").onclick = (e) => {
  const on = !e.currentTarget.classList.contains("active");
  e.currentTarget.classList.toggle("active", on);
  GridMap.setLayer("sub-label", on);
};
el("btnFuture").onclick = (e) => {
  const on = !e.currentTarget.classList.contains("active");
  e.currentTarget.classList.toggle("active", on);
  GridMap.setLayer("future-lines", on);
};

const openDrawer = (open) => {
  el("techDrawer").classList.toggle("open", open);
  el("drawerBackdrop").classList.toggle("hidden", !open);
};
el("btnTechDrawer").onclick = () => { paintTechnical(); openDrawer(true); };
el("btnCloseDrawer").onclick = () => openDrawer(false);
el("drawerBackdrop").onclick = () => openDrawer(false);

/* ---------- boot ---------- */

(async function init() {
  try {
    S.config = await api("/api/config");
    await GridMap.init(S.config);

    GridMap.onCorridorClick = guard(async (key, props) => {
      if (!window.confirm(`Trip corridor ${props.from} ⇄ ${props.to}?\n\n${props.circuits_live} circuits will fail.`)) return;
      await tripCorridor(key, props);
    });

    S.state = await api("/api/state");
    S.centroid = {
      lat: S.state.substations.reduce((a, s) => a + s.lat, 0) / S.state.substations.length,
      lon: S.state.substations.reduce((a, s) => a + s.lon, 0) / S.state.substations.length,
    };
    S.forecast = await api("/api/forecast?hours=12");

    GridMap.render(S.state, []);
    paintLegend();
    paintAll();
    await refreshPreview();

    logEvent(`Digital twin online — ${S.state.substations.length} substations, ${Object.keys(S.state.corridors).length} corridors`, "ok");
    logEvent(`Baseline demand ${fmtMW(S.state.risk.total_served_mw)} · max loading ${pct(S.state.risk.max_circuit_loading)}`, "");
  } catch (e) {
    console.error(e);
    logEvent(`Startup failed: ${e.message}`, "danger");
  }
})();
