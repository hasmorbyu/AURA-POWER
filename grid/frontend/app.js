/* App wiring.
 *
 * One screen, no modes: the forecast tick and the stress controls sit in the
 * same panel and hit the same API. Both return the identical InterventionResult,
 * so there is a single render path for "state changed" regardless of what
 * caused it. */

const el = (id) => document.getElementById(id);

let state = null;
let busyCount = 0;

/* ---------- transport ---------- */

async function api(path, opts) {
  busyCount++;
  el("busy").classList.remove("hidden");
  try {
    const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { /* non-JSON error */ }
      throw new Error(detail);
    }
    return await res.json();
  } finally {
    if (--busyCount <= 0) { busyCount = 0; el("busy").classList.add("hidden"); }
  }
}

function setControlsEnabled(on) {
  for (const id of ["btnTick", "btnTemp", "btnDemand", "btnReset"]) el(id).disabled = !on;
}

/* ---------- panel ---------- */

function paintPanel(s) {
  const r = s.risk;
  el("mServed").textContent = `${Math.round(r.total_served_mw)}`;
  el("mShed").textContent = `${Math.round(Math.max(0, r.total_shed_mw))}`;
  el("mPeak").textContent = `${Math.round(r.max_circuit_loading * 100)}%`;
  el("mRisk").textContent = r.corridors_at_risk;
  el("mFailed").textContent = r.failed_lines;
  el("mSwitched").textContent = r.switched_out_lines ?? 0;
  el("tickLabel").textContent = `tick ${s.tick} · ×${s.demand_scale.toFixed(2)}`;

  const shed = Math.max(0, r.total_shed_mw);
  const pill = el("statusPill");
  pill.classList.remove("warn", "alert");
  if (shed > 0.1) {
    pill.classList.add("alert");
    el("statusText").textContent = `${Math.round(shed)} MW SHED`;
  } else if (r.corridors_at_risk > 0) {
    pill.classList.add("warn");
    el("statusText").textContent = `${r.corridors_at_risk} AT RISK`;
  } else {
    el("statusText").textContent = "NOMINAL";
  }
}

async function refreshForecast() {
  try {
    const f = await api("/api/forecast?hours=12");
    ForecastChart.render(el("chart"), f);
    const last = f.past[f.past.length - 1];
    el("forecastNow").textContent = last ? `${Math.round(last.actual_mw)} MW now` : "";
  } catch (e) {
    console.warn("forecast unavailable", e);
  }
}

/* ---------- activity feed: only things that matter ---------- */

function feed(text, kind = "") {
  const div = document.createElement("div");
  div.className = `ev ${kind}`;
  const now = new Date();
  div.innerHTML =
    `<span class="t">${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}</span>` +
    `<span>${text}</span>`;
  const box = el("feed");
  box.appendChild(div);
  while (box.children.length > 4) box.removeChild(box.firstChild);
  setTimeout(() => div.remove(), 22000);
}

function reportIntervention(result, label) {
  const ra = result.risk_after, rb = result.risk_before;

  const switches = result.actions.filter((a) => a.kind.startsWith("switch"));
  const reroutes = result.actions.filter((a) => a.kind === "reroute");
  const sheds = result.actions.filter((a) => a.kind === "shed");
  const shedTotal = Math.max(0, ra.total_shed_mw);
  const newShed = shedTotal - Math.max(0, rb.total_shed_mw);

  feed(label, "");

  if (switches.length) {
    feed(`opened ${switches.length} bottleneck circuit${switches.length > 1 ? "s" : ""} for relief`, "warn");
  }
  if (result.mw_rerouted > 1) {
    feed(`rerouted ${Math.round(result.mw_rerouted)} MW across ${result.affected_corridors.length} corridors`, "ok");
  }
  if (ra.max_circuit_loading >= 0.95) {
    feed(`peak circuit at ${Math.round(ra.max_circuit_loading * 100)}% · ${ra.corridors_at_risk} corridors at risk`, "warn");
  }
  if (newShed > 0.5) {
    const where = sheds.map((s) => s.substation).slice(0, 3).join(", ") || "network";
    feed(`load shed ${Math.round(newShed)} MW at ${where}`, "danger");
  }
  if (!result.power_flow_validated) {
    feed("power flow still reports overloads — running beyond limits", "danger");
  }

  // one-line agent summary, the reference's AI box
  const bits = [];
  if (switches.length) bits.push(`opened ${switches.length} circuit${switches.length > 1 ? "s" : ""}`);
  if (reroutes.length) bits.push(`rerouted ${Math.round(result.mw_rerouted)} MW`);
  if (shedTotal > 0.1) bits.push(`shedding ${Math.round(shedTotal)} MW`);
  const validated = result.power_flow_validated ? "DC power flow validated" : "limits exceeded";
  const rejected = result.rejected_alternatives.length
    ? ` Rejected ${result.rejected_alternatives.length} dispatch${result.rejected_alternatives.length > 1 ? "es" : ""} that power flow refused.`
    : "";
  el("aiText").textContent =
    `${bits.length ? bits.join(", ") : "held dispatch"} — ${validated}.${rejected}`;
}

/* ---------- the single state-change path ---------- */

async function applyResult(result, label) {
  reportIntervention(result, label);
  state = await api("/api/state");
  GridMap.render(state, result.affected_corridors);
  paintPanel(state);
  // flow animation marks only what just moved; clear it after a beat
  setTimeout(() => GridMap.render(state, []), 2600);
}

async function doTick() {
  const r = await api("/api/tick", { method: "POST", body: JSON.stringify({ horizon_h: 2 }) });
  await applyResult(r, `forecast tick · ${Math.round(r.trigger.citywide_forecast_mw)} MW`);
  await refreshForecast();
}

async function doDisturbance(body, label) {
  const r = await api("/api/disturbance", { method: "POST", body: JSON.stringify(body) });
  await applyResult(r, label);
}

function guard(fn) {
  return async (...args) => {
    setControlsEnabled(false);
    try { await fn(...args); }
    catch (e) { console.error(e); feed(`request failed: ${e.message}`, "danger"); }
    finally { setControlsEnabled(true); }
  };
}

/* ---------- wiring ---------- */

el("temp").oninput = (e) => (el("tempOut").textContent = `+${e.target.value}°C`);
el("dem").oninput = (e) => (el("demOut").textContent = `+${e.target.value}%`);

el("btnTick").onclick = guard(doTick);
el("btnTemp").onclick = guard(() => {
  const d = Number(el("temp").value);
  return doDisturbance({ kind: "temperature", temperature_delta_c: d }, `heatwave +${d}°C applied`);
});
el("btnDemand").onclick = guard(() => {
  const p = Number(el("dem").value);
  return doDisturbance({ kind: "demand", demand_multiplier: 1 + p / 100 }, `demand surge +${p}%`);
});
el("btnReset").onclick = guard(async () => {
  state = await api("/api/reset", { method: "POST" });
  el("feed").innerHTML = "";
  el("aiText").textContent = "Grid calibrated. Awaiting forecast tick.";
  GridMap.render(state, []);
  paintPanel(state);
  await refreshForecast();
  feed("grid reset to calibrated baseline", "ok");
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

/* ---------- boot ---------- */

(async function init() {
  setControlsEnabled(false);
  try {
    const config = await api("/api/config");
    await GridMap.init(config);

    GridMap.onCorridorClick = guard(async (key, props) => {
      await doDisturbance(
        { kind: "line_failure", corridor: key },
        `tripped ${props.from} ⇄ ${props.to} (${props.circuits_live} circuits)`
      );
    });

    state = await api("/api/state");
    GridMap.render(state, []);
    paintPanel(state);
    await refreshForecast();

    feed(`${GridMap.engine === "mapbox" ? "Mapbox" : "MapLibre"} 3D twin online · ${state.substations.length} substations, ${Object.keys(state.corridors).length} corridors`, "ok");
  } catch (e) {
    console.error(e);
    feed(`startup failed: ${e.message}`, "danger");
  } finally {
    setControlsEnabled(true);
  }
})();
