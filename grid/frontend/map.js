/* 3D map layer for the twin.
 *
 * Mapbox GL JS renders nothing without an access token. So this loads Mapbox GL
 * when the server reports one (MAPBOX_TOKEN), and MapLibre GL -- the
 * API-identical open fork -- with a free dark basemap when it doesn't. Every
 * call below is the shared Mapbox GL API surface, so there is one codebase
 * either way, and adding a token switches engines with no other edit.
 *
 * Substations render as DOM antenna markers rather than a GL layer (see
 * _markerFor/_upsertMarker below) specifically so their size/colour animate
 * via ordinary CSS transitions on state change.
 *
 * Transmission corridors carry TWO independent, simultaneous encodings, per
 * the AURA visual-encoding spec:
 *   - THICKNESS = abs(flow_mw), normalised against the network's own current
 *     max flow every render. Never driven by capacity -- a big corridor
 *     carrying little power must look thin.
 *   - COLOUR = loading (flow/capacity) against the five-band table served by
 *     GET /api/config (safe/elevated/high/overloaded/critical), so the map's
 *     colour scale can never drift from what the backend's risk() actually
 *     means by those words.
 * A stable grid must not look like a uniform glowing spiderweb: only
 * elevated-and-above bands get glow/animation at all; "safe" is deliberately
 * near-invisible. An AURA-reroute overlay (gold, temporary) marks whatever
 * the last optimize response actually changed, laid on top of -- never
 * instead of -- the real thickness/colour encoding.
 */

const DELHI = { center: [77.14, 28.66], zoom: 10.1, pitch: 52, bearing: -18 };

const MAPLIBRE_STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";
const MAPBOX_STYLE = "mapbox://styles/mapbox/dark-v11";

const GL = {
  mapbox: {
    js: "https://api.mapbox.com/mapbox-gl-js/v3.7.0/mapbox-gl.js",
    css: "https://api.mapbox.com/mapbox-gl-js/v3.7.0/mapbox-gl.css",
    global: "mapboxgl",
  },
  maplibre: {
    js: "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.js",
    css: "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/dist/maplibre-gl.css",
    global: "maplibregl",
  },
};

// Presentation-only tuning per band (not backend business logic, just how
// loud each band reads visually): bloom opacity and flow-dash speed.
// "safe" gets no dash layer at all -- deliberately absent, not just slow.
const BAND_STYLE = {
  safe:       { bloomOpacity: 0.05, pulseMult: 0 },
  elevated:   { bloomOpacity: 0.22, pulseMult: 1.0 },
  high:       { bloomOpacity: 0.32, pulseMult: 1.6 },
  overloaded: { bloomOpacity: 0.42, pulseMult: 2.4 },
  critical:   { bloomOpacity: 0.55, pulseMult: 3.5 },
};
const ANIMATED_BANDS = ["elevated", "high", "overloaded", "critical"];

function loadAsset(tag, attrs) {
  return new Promise((resolve, reject) => {
    const node = document.createElement(tag);
    Object.assign(node, attrs);
    node.onload = resolve;
    node.onerror = () => reject(new Error(`failed to load ${attrs.src || attrs.href}`));
    document.head.appendChild(node);
  });
}

const GridMap = {
  gl: null,
  map: null,
  engine: null,
  ready: false,
  bands: [],
  overloadThreshold: 1.0,
  onCorridorClick: null,

  async init(config) {
    this.engine = config.mapbox_token ? "mapbox" : "maplibre";
    this.bands = config.loading_bands || [];
    this.overloadThreshold = config.overload_threshold ?? 1.0;
    const lib = GL[this.engine];

    await loadAsset("link", { rel: "stylesheet", href: lib.css });
    await loadAsset("script", { src: lib.js });
    this.gl = window[lib.global];

    if (this.engine === "mapbox") this.gl.accessToken = config.mapbox_token;

    this.map = new this.gl.Map({
      container: "map",
      style: this.engine === "mapbox" ? MAPBOX_STYLE : MAPLIBRE_STYLE,
      center: DELHI.center,
      zoom: DELHI.zoom,
      pitch: DELHI.pitch,
      bearing: DELHI.bearing,
      antialias: true,
      attributionControl: false,
    });

    await new Promise((r) => this.map.on("load", r));
    this._addSources();
    this._addLayers();
    this._wireInteractions();
    // catenary control points are computed in screen space (see
    // _catenaryCoords), so a pan/zoom leaves the old geometry stale -- redraw
    // corridor geometry, cheaply, once the camera settles
    this.map.on("moveend", () => this._lastState && this.render(this._lastState, this._lastReroute));
    this.ready = true;
    return this;
  },

  /* band = which of the five loading bands a corridor is in right now */
  bandFor(loading) {
    for (const b of this.bands) {
      if (b.max == null || loading < b.max) return b;
    }
    return this.bands[this.bands.length - 1] || { key: "safe", color: "#2dd4a8" };
  },

  /* ---------- substation markers ----------
   * Real DOM markers (not a GL symbol layer) so a size/colour change on
   * re-render is a CSS transition the browser animates for free -- the same
   * "grows with load, turns red when shedding" behaviour a fill-extrusion
   * column gets via its implicit paint-property transition, just carried by
   * the antenna icon instead of a cylinder. One persistent element per
   * substation, reused across every render() call. */
  markers: {},

  towerSVG() {
    return `<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
      <g fill="none" stroke="currentColor" stroke-width="6" stroke-linecap="round">
        <path d="M50 34 L64 90 H36 Z"/>
        <path d="M40 52 L60 74 M60 52 L40 74 M42.5 74 L57.5 90 M57.5 74 L42.5 90"/>
        <line x1="50" y1="16" x2="50" y2="34"/>
        <path d="M39 26 A13 13 0 0 1 39 6"/>
        <path d="M31 31 A22 22 0 0 1 31 1"/>
        <path d="M23 36 A31 31 0 0 1 23 -4"/>
        <path d="M61 26 A13 13 0 0 0 61 6"/>
        <path d="M69 31 A22 22 0 0 0 69 1"/>
        <path d="M77 36 A31 31 0 0 0 77 -4"/>
      </g>
      <circle cx="50" cy="12" r="6" fill="currentColor"/>
      <rect x="22" y="88" width="56" height="7" rx="3.5" fill="currentColor"/>
    </svg>`;
  },

  _markerFor(name, lngLat) {
    let entry = this.markers[name];
    if (entry) return entry;

    const el = document.createElement("div");
    el.className = "sub-marker";
    el.innerHTML = this.towerSVG();

    const pop = new this.gl.Popup({ closeButton: false, closeOnClick: false, offset: 14 });
    el.addEventListener("mouseenter", () => {
      pop.setLngLat(marker.getLngLat()).setHTML(
        `<div class="pop-t">${el.dataset.name}</div>` +
        `<div class="pop-r">${el.dataset.role}<br>${el.dataset.detail}</div>`
      ).addTo(this.map);
    });
    el.addEventListener("mouseleave", () => pop.remove());

    const marker = new this.gl.Marker({ element: el, anchor: "bottom" }).setLngLat(lngLat).addTo(this.map);
    entry = { marker, el };
    this.markers[name] = entry;
    return entry;
  },

  MARKER_MIN_PX: 26,
  MARKER_MAX_PX: 68,
  LABEL_TEXT_SIZE: 10.5,

  markerHeightPx(mag) {
    return this.MARKER_MIN_PX + (this.MARKER_MAX_PX - this.MARKER_MIN_PX) * mag;
  },

  _upsertMarker(s, color, mag, detail) {
    const { el } = this._markerFor(s.name, [s.lon, s.lat]);
    const px = Math.round(this.markerHeightPx(mag));
    el.style.color = color;
    el.style.width = `${px}px`;
    el.style.height = `${px}px`;
    el.classList.toggle("alert", color === "#ef4444");
    el.dataset.name = s.name;
    el.dataset.role = s.role;
    el.dataset.detail = detail;
  },

  /* ---------- catenary geometry ----------
   * Real HV conductors sag under gravity and thermal expansion; a straight
   * LineString reads as a generic UI connector, not a physical cable. The
   * control point is offset a fixed pixel distance below the chord midpoint
   * -- computed in *screen* space via project/unproject so "12-24px of sag"
   * means the same thing at any zoom -- then tessellated into a polyline.
   * Points are ordered start-to-end in the actual power-flow direction, so a
   * single forward-stepping dash animation always reads as flowing the
   * correct physical way. */
  CURVE_STEPS: 20,

  _catenaryCoords(startLngLat, endLngLat, sagPx) {
    const p1 = this.map.project(startLngLat);
    const p2 = this.map.project(endLngLat);
    const mx = (p1.x + p2.x) / 2, my = (p1.y + p2.y) / 2;

    const dx = p2.x - p1.x, dy = p2.y - p1.y;
    const len = Math.hypot(dx, dy) || 1;
    // perpendicular to the chord, normalised to always point screen-downward
    // (positive y) so every cable sags "down" regardless of which end is
    // upstream -- direction only changes point *order*, never curve shape
    let nx = -dy / len, ny = dx / len;
    if (ny < 0) { nx = -nx; ny = -ny; }

    const cx = mx + nx * sagPx, cy = my + ny * sagPx;
    const pts = [];
    for (let i = 0; i <= this.CURVE_STEPS; i++) {
      const t = i / this.CURVE_STEPS, omt = 1 - t;
      const x = omt * omt * p1.x + 2 * omt * t * cx + t * t * p2.x;
      const y = omt * omt * p1.y + 2 * omt * t * cy + t * t * p2.y;
      const ll = this.map.unproject([x, y]);
      pts.push([ll.lng, ll.lat]);
    }
    return pts;
  },

  /* ---------- data plumbing ---------- */

  _addSources() {
    const empty = { type: "FeatureCollection", features: [] };
    for (const id of ["corridors", "future", "subs"]) {
      this.map.addSource(id, { type: "geojson", data: empty });
    }
  },

  _addLayers() {
    const m = this.map;
    const live = ["!", ["get", "failed"]];

    // planned corridors: dashed, dim, behind everything live
    m.addLayer({
      id: "future-lines", type: "line", source: "future",
      paint: {
        "line-color": "#475569", "line-width": 1.1,
        "line-dasharray": [3, 3], "line-opacity": 0.45,
      },
    });

    // Layer 1 -- ambient bloom: casts colour onto the ground. Width tracks
    // flow (thickness encoding); opacity tracks band (colour encoding) -- a
    // thick-but-safe corridor gets a wide, near-invisible glow; a thin-but-
    // critical one gets a narrow, blazing one. The two never conflate.
    m.addLayer({
      id: "corridor-bloom", type: "line", source: "corridors",
      filter: live,
      layout: { "line-cap": "round" },
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["get", "bloomWidth"],
        "line-blur": 8,
        // flicker (over the overload threshold) overrides the steady glow
        "line-opacity": ["coalesce", ["feature-state", "flicker"], ["get", "bloomOpacity"]],
      },
    });

    // Layer 2 -- energy conductor: the real cable body. Width = flow, always.
    m.addLayer({
      id: "corridor-conductor", type: "line", source: "corridors",
      filter: live,
      layout: { "line-cap": "round" },
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["get", "condWidth"],
        "line-opacity": 0.95,
      },
    });

    // Layer 3 -- superconducting core: a hairline of near-white at the centre
    m.addLayer({
      id: "corridor-core", type: "line", source: "corridors",
      filter: live,
      layout: { "line-cap": "round" },
      paint: {
        "line-color": "#e0ffff",
        "line-width": ["get", "coreWidth"],
        "line-opacity": 0.85,
      },
    });

    // tripped corridors read as broken infrastructure, not just recoloured
    m.addLayer({
      id: "corridor-failed", type: "line", source: "corridors",
      filter: ["get", "failed"],
      paint: {
        "line-color": "#ef4444", "line-width": 1.6,
        "line-dasharray": [2, 2], "line-opacity": 0.75,
      },
    });

    // Directional current-packet overlay, one layer per animated band so each
    // can advance at its own pulse speed -- "safe" deliberately has none.
    for (const band of ANIMATED_BANDS) {
      m.addLayer({
        id: `corridor-flow-${band}`, type: "line", source: "corridors",
        filter: ["all", live, ["==", ["get", "band"], band]],
        layout: { "line-cap": "round" },
        paint: {
          "line-color": "#ffffff",
          "line-width": 2,
          "line-dasharray": [4, 16],
          "line-opacity": 0.85,
        },
      });
    }

    // AURA-reroute overlay: temporary, restrained gold highlight on whatever
    // the *last* optimize response actually changed -- "power moved here,"
    // layered on top of the real thickness/colour, never substituting for it.
    m.addLayer({
      id: "corridor-reroute", type: "line", source: "corridors",
      filter: ["all", live, ["get", "rerouted"]],
      layout: { "line-cap": "round" },
      paint: {
        "line-color": "#ffd54a",
        "line-width": ["+", ["get", "condWidth"], 2],
        "line-dasharray": [1, 2],
        "line-opacity": 0.9,
      },
    });

    // substation labels only -- the substation markers themselves are DOM
    // antenna elements (see _markerFor/_upsertMarker) so their size/colour
    // transitions animate via CSS instead of a GL paint-property transition
    m.addLayer({
      id: "sub-label", type: "symbol", source: "subs",
      layout: {
        "text-field": ["get", "label"],
        "text-font": this.engine === "mapbox"
          ? ["DIN Pro Medium", "Arial Unicode MS Regular"]
          : ["Open Sans Regular", "Noto Sans Regular"],
        "text-size": 10.5,
        "text-offset": ["get", "offset"],
        "text-anchor": "bottom",
        "text-allow-overlap": false,
      },
      paint: {
        "text-color": "#dbe6f3",
        "text-halo-color": "#06080c",
        "text-halo-width": 1.6,
      },
    });

    this._animateFlow();
    this._animateFlicker();
    this._animateReroute();
  },

  /* marching dashes per band: same phase-cycling trick as before (GL has no
   * real dash-offset paint property), but one independent ticker per animated
   * band so critical corridors visibly race past elevated ones. */
  _animateFlow() {
    const steps = [
      [0, 4, 3], [0.5, 4, 2.5], [1, 4, 2], [1.5, 4, 1.5], [2, 4, 1],
      [2.5, 4, 0.5], [3, 4, 0], [0, 0.5, 3, 3.5], [0, 1, 3, 3], [0, 1.5, 3, 2.5],
    ];

    for (const band of ANIMATED_BANDS) {
      let i = 0;
      const baseMs = 90 / BAND_STYLE[band].pulseMult;
      const tick = () => {
        if (!this.map.getLayer(`corridor-flow-${band}`)) return;
        this.map.setPaintProperty(`corridor-flow-${band}`, "line-dasharray", steps[i % steps.length]);
        i++;
        // a touch of timing jitter on the worst band reads as "surging", not smooth
        const jitterMs = band === "critical" ? (Math.random() - 0.5) * 14 : 0;
        setTimeout(tick, Math.max(20, baseMs + jitterMs));
      };
      tick();
    }
  },

  /* a slower, steadier dash for the reroute overlay -- "power is moving here"
   * reads differently from "this line is dangerously loaded" */
  _animateReroute() {
    const steps = [[0, 3], [1, 3], [2, 3], [3, 3], [0, 0, 3, 3], [0, 1, 3, 2]];
    let i = 0;
    setInterval(() => {
      if (!this.map.getLayer("corridor-reroute")) return;
      this.map.setPaintProperty("corridor-reroute", "line-dasharray", steps[i % steps.length]);
      i++;
    }, 110);
  },

  /* Thermal flashing: a sharp 5Hz sine modulates bloom opacity on whatever is
   * currently over the (backend-defined) overload threshold, via feature-state
   * so it never touches the GeoJSON source (cheap enough to run every frame). */
  _criticalIds: new Set(),

  _animateFlicker() {
    const step = () => {
      requestAnimationFrame(step);
      if (!this.map.getSource("corridors")) return;
      const t = performance.now() / 1000;
      const opacity = 0.25 + 0.45 * (0.5 + 0.5 * Math.sin(2 * Math.PI * 5 * t));
      for (const id of this._criticalIds) {
        this.map.setFeatureState({ source: "corridors", id }, { flicker: opacity });
      }
    };
    requestAnimationFrame(step);
  },

  _wireInteractions() {
    const m = this.map;
    const pop = new this.gl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });

    for (const layer of ["corridor-conductor", "corridor-failed"]) {
      m.on("mouseenter", layer, (e) => {
        m.getCanvas().style.cursor = "pointer";
        const p = e.features[0].properties;
        pop.setLngLat(e.lngLat).setHTML(
          `<div class="pop-t">${p.from} ⇄ ${p.to}</div>` +
          `<div class="pop-r">${p.voltage_kv} kV · ${p.circuits_live}/${p.circuits_total} circuits<br>` +
          `${Math.round(p.flow_mw)} / ${Math.round(p.capacity_mw)} MW · ${Math.round(p.loading * 100)}% · ${p.band}` +
          `${p.failed ? "<br><b>TRIPPED</b>" : "<br>click to trip"}</div>`
        ).addTo(m);
      });
      m.on("mouseleave", layer, () => { m.getCanvas().style.cursor = ""; pop.remove(); });
    }

    m.on("click", "corridor-conductor", (e) => {
      const p = e.features[0].properties;
      if (p.failed) return;
      if (this.onCorridorClick) this.onCorridorClick(p.key, p);
    });
  },

  /* ---------- rendering state ---------- */

  _corridorIds: {},
  _nextId: 1,

  _idFor(key) {
    if (!(key in this._corridorIds)) this._corridorIds[key] = this._nextId++;
    return this._corridorIds[key];
  },

  render(state, rerouteKeys = []) {
    if (!this.ready) return;
    this._lastState = state;
    this._lastReroute = rerouteKeys;

    const at = Object.fromEntries(state.substations.map((s) => [s.name, [s.lon, s.lat]]));
    const rerouted = new Set(rerouteKeys);
    const wasCritical = this._criticalIds;
    this._criticalIds = new Set();
    this._lastCorridorCenter = this._lastCorridorCenter || {};

    // thickness is normalised against the network's OWN current max flow --
    // never against capacity -- so a 100 MW corridor always looks the same
    // regardless of how big its rated capacity is
    const maxFlow = Math.max(1, ...Object.values(state.corridors).map((c) => Math.abs(c.flow_mw)));

    const corridors = { type: "FeatureCollection", features: [] };
    for (const [key, c] of Object.entries(state.corridors)) {
      const a = at[c.from], b = at[c.to];
      if (!a || !b) continue;
      const failed = c.circuits_live === 0;
      const id = this._idFor(key);
      this._lastCorridorCenter[key] = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];

      // --- encoding 1: thickness/height = |flow|, independent of capacity ---
      const flowFrac = Math.min(1, Math.abs(c.flow_mw) / maxFlow);
      const condWidth = 1 + 3.5 * flowFrac;      // 1 - 4.5px, the real thickness
      const bloomWidth = condWidth * 3.2;         // proportional halo
      const coreWidth = Math.max(0.6, condWidth * 0.32);

      // --- encoding 2: colour/opacity = loading vs capacity, from the band table ---
      const loading = c.max_circuit_loading;
      const band = this.bandFor(loading);
      const style = BAND_STYLE[band.key] || BAND_STYLE.safe;
      const overloaded = loading >= this.overloadThreshold;
      if (overloaded && !failed) this._criticalIds.add(id);

      // gravity + thermal sag: bigger flows hang a little more, and an
      // overloaded conductor visibly expands and sags further (+30%)
      let sagPx = 12 + 12 * flowFrac;
      if (overloaded) sagPx *= 1.3;

      const [start, end] = c.flow_direction === -1 ? [b, a] : [a, b];
      const coords = failed ? [a, b] : this._catenaryCoords(start, end, sagPx);

      corridors.features.push({
        type: "Feature",
        id,
        geometry: { type: "LineString", coordinates: coords },
        properties: {
          key, from: c.from, to: c.to, failed,
          loading,
          flow_mw: c.flow_mw, capacity_mw: c.capacity_mw,
          voltage_kv: c.voltage_kv,
          circuits_live: c.circuits_live, circuits_total: c.circuits_total,
          band: band.key,
          color: band.color,
          bloomWidth, condWidth, coreWidth,
          bloomOpacity: style.bloomOpacity,
          rerouted: rerouted.has(key),
        },
      });
    }

    const future = { type: "FeatureCollection", features: (state.future_corridors || []).map((f) => ({
      type: "Feature",
      geometry: { type: "LineString", coordinates: [at[f.from], at[f.to]] },
      properties: { from: f.from, to: f.to },
    })).filter((f) => f.geometry.coordinates.every(Boolean)) };

    const maxLoad = Math.max(1, ...state.substations.map((s) => Math.max(s.demand_mw, s.served_mw)));
    const subs = { type: "FeatureCollection", features: [] };

    for (const s of state.substations) {
      const shedding = s.shed_mw > 0.1;
      const mag = Math.max(s.demand_mw, s.served_mw) / maxLoad;
      const color = shedding ? "#ef4444" : s.role === "source" ? "#10b981" : "#00e5ff";
      const detail = s.role === "demand"
        ? `${Math.round(s.served_mw)} / ${Math.round(s.demand_mw)} MW served${shedding ? ` · SHED ${Math.round(s.shed_mw)} MW` : ""}`
        : `injecting ${Math.round(s.served_mw || 0)} MW`;

      // clear the actual tower height (which varies with load) plus a fixed
      // margin, so the name always sits above the antenna instead of over it
      const clearancePx = this.markerHeightPx(mag) + 10;
      const offset = [0, -clearancePx / this.LABEL_TEXT_SIZE];

      subs.features.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [s.lon, s.lat] },
        properties: {
          offset,
          label: `${s.name} · ${Math.round(s.role === "demand" ? s.served_mw : s.demand_mw || s.served_mw)} MW`,
        },
      });

      this._upsertMarker(s, color, mag, detail);
    }

    this.map.getSource("corridors").setData(corridors);
    this.map.getSource("future").setData(future);
    this.map.getSource("subs").setData(subs);

    // anything that dropped out of critical this render must have its stale
    // flicker feature-state cleared, or its bloom would stay frozen at
    // whatever opacity the flicker last set instead of resuming loading-based
    for (const id of wasCritical) {
      if (!this._criticalIds.has(id)) {
        this.map.removeFeatureState({ source: "corridors", id }, "flicker");
      }
    }
  },

  focusCorridorKey(key) {
    const center = this._lastCorridorCenter?.[key];
    if (!center) return;
    this.map.flyTo({ center, zoom: Math.max(this.map.getZoom(), 11.5), duration: 700 });
  },

  recenter() { this.map.flyTo({ ...DELHI, duration: 900 }); },
  togglePitch() {
    const flat = this.map.getPitch() > 10;
    this.map.easeTo({ pitch: flat ? 0 : DELHI.pitch, duration: 600 });
    return !flat;
  },
  setLayer(id, visible) {
    if (this.map.getLayer(id)) this.map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
  },
};

window.GridMap = GridMap;
