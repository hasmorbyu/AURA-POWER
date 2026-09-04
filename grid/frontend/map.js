/* 3D map layer for the twin.
 *
 * Mapbox GL JS renders nothing without an access token. So this loads Mapbox GL
 * when the server reports one (MAPBOX_TOKEN), and MapLibre GL -- the
 * API-identical open fork -- with a free dark basemap when it doesn't. Every
 * call below is the shared Mapbox GL API surface, so there is one codebase
 * either way, and adding a token switches engines with no other edit.
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

/* voltage-class colours straight off the OpenGridWorks legend */
const KV_COLOR = { 765: "#ffffff", 400: "#00e5ff", 220: "#3b82f6" };

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
  onCorridorClick: null,

  async init(config) {
    this.engine = config.mapbox_token ? "mapbox" : "maplibre";
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
    this.ready = true;
    return this;
  },

  /* ---------- data plumbing ---------- */

  _addSources() {
    const empty = { type: "FeatureCollection", features: [] };
    for (const id of ["corridors", "future", "subs", "subs3d"]) {
      this.map.addSource(id, { type: "geojson", data: empty });
    }
  },

  _addLayers() {
    const m = this.map;

    // planned corridors: dashed, dim, behind everything live
    m.addLayer({
      id: "future-lines", type: "line", source: "future",
      paint: {
        "line-color": "#475569", "line-width": 1.1,
        "line-dasharray": [3, 3], "line-opacity": 0.45,
      },
    });

    // wide blurred underlay produces the OGW glow
    m.addLayer({
      id: "corridor-glow", type: "line", source: "corridors",
      filter: ["!", ["get", "failed"]],
      layout: { "line-cap": "round" },
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["*", ["get", "width"], 3.2],
        "line-blur": 7,
        "line-opacity": ["interpolate", ["linear"], ["get", "loading"], 0, 0.10, 0.6, 0.24, 1, 0.55],
      },
    });

    m.addLayer({
      id: "corridor-line", type: "line", source: "corridors",
      filter: ["!", ["get", "failed"]],
      layout: { "line-cap": "round" },
      paint: {
        "line-color": ["get", "color"],
        "line-width": ["get", "width"],
        "line-opacity": 0.95,
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

    // animated dashes, switched on only for corridors the last dispatch moved
    m.addLayer({
      id: "corridor-flow", type: "line", source: "corridors",
      filter: ["get", "active"],
      layout: { "line-cap": "round" },
      paint: {
        "line-color": "#ffffff", "line-width": ["*", ["get", "width"], 0.55],
        "line-dasharray": [0, 4, 3], "line-opacity": 0.85,
      },
    });

    // substations as extruded columns: height is live load, so demand centres
    // visibly grow through the day and drop dark red when they shed
    m.addLayer({
      id: "sub-extrusion", type: "fill-extrusion", source: "subs3d",
      paint: {
        "fill-extrusion-color": ["get", "color"],
        "fill-extrusion-height": ["get", "height"],
        "fill-extrusion-base": 0,
        "fill-extrusion-opacity": 0.82,
      },
    });

    m.addLayer({
      id: "sub-dot", type: "circle", source: "subs",
      paint: {
        "circle-radius": ["interpolate", ["linear"], ["get", "mag"], 0, 3.5, 1, 9],
        "circle-color": ["get", "color"],
        "circle-opacity": 0.9,
        "circle-stroke-width": 1.2,
        "circle-stroke-color": "#06080c",
      },
    });

    m.addLayer({
      id: "sub-label", type: "symbol", source: "subs",
      layout: {
        "text-field": ["get", "label"],
        "text-font": this.engine === "mapbox"
          ? ["DIN Pro Medium", "Arial Unicode MS Regular"]
          : ["Open Sans Regular", "Noto Sans Regular"],
        "text-size": 10.5,
        "text-offset": [0, -1.5],
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
  },

  _animateFlow() {
    // marching dashes: the only motion on the map, so movement always means
    // "load just shifted along here"
    const steps = [[0, 4, 3], [0.5, 4, 2.5], [1, 4, 2], [1.5, 4, 1.5], [2, 4, 1],
                   [2.5, 4, 0.5], [3, 4, 0], [0, 0.5, 3, 3.5], [0, 1, 3, 3], [0, 1.5, 3, 2.5]];
    let i = 0;
    setInterval(() => {
      if (!this.map.getLayer("corridor-flow")) return;
      this.map.setPaintProperty("corridor-flow", "line-dasharray", steps[i % steps.length]);
      i++;
    }, 90);
  },

  _wireInteractions() {
    const m = this.map;
    const pop = new this.gl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });

    for (const layer of ["corridor-line", "corridor-failed"]) {
      m.on("mouseenter", layer, (e) => {
        m.getCanvas().style.cursor = "pointer";
        const p = e.features[0].properties;
        pop.setLngLat(e.lngLat).setHTML(
          `<div class="pop-t">${p.from} ⇄ ${p.to}</div>` +
          `<div class="pop-r">${p.voltage_kv} kV · ${p.circuits_live}/${p.circuits_total} circuits<br>` +
          `${Math.round(p.flow_mw)} / ${Math.round(p.capacity_mw)} MW · peak ${Math.round(p.loading * 100)}%` +
          `${p.failed ? "<br><b>TRIPPED</b>" : "<br>click to trip"}</div>`
        ).addTo(m);
      });
      m.on("mouseleave", layer, () => { m.getCanvas().style.cursor = ""; pop.remove(); });
    }

    m.on("click", "corridor-line", (e) => {
      const p = e.features[0].properties;
      if (p.failed) return;
      if (this.onCorridorClick) this.onCorridorClick(p.key, p);
    });

    m.on("mouseenter", "sub-dot", (e) => {
      m.getCanvas().style.cursor = "pointer";
      const p = e.features[0].properties;
      pop.setLngLat(e.features[0].geometry.coordinates).setHTML(
        `<div class="pop-t">${p.name}</div>` +
        `<div class="pop-r">${p.role}<br>${p.detail}</div>`
      ).addTo(m);
    });
    m.on("mouseleave", "sub-dot", () => { m.getCanvas().style.cursor = ""; pop.remove(); });
  },

  /* ---------- rendering state ---------- */

  render(state, activeCorridors = []) {
    if (!this.ready) return;
    const active = new Set(activeCorridors);
    const at = Object.fromEntries(state.substations.map((s) => [s.name, [s.lon, s.lat]]));

    const corridors = { type: "FeatureCollection", features: [] };
    for (const [key, c] of Object.entries(state.corridors)) {
      const a = at[c.from], b = at[c.to];
      if (!a || !b) continue;
      const failed = c.circuits_live === 0;
      corridors.features.push({
        type: "Feature",
        geometry: { type: "LineString", coordinates: [a, b] },
        properties: {
          key, from: c.from, to: c.to, failed,
          loading: c.max_circuit_loading,
          flow_mw: c.flow_mw, capacity_mw: c.capacity_mw,
          voltage_kv: c.voltage_kv,
          circuits_live: c.circuits_live, circuits_total: c.circuits_total,
          active: active.has(key) && !failed,
          color: this.corridorColor(c),
          width: Math.max(1.2, Math.min(6.5, Math.sqrt(c.capacity_mw / 260))),
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
    const subs3d = { type: "FeatureCollection", features: [] };

    for (const s of state.substations) {
      const shedding = s.shed_mw > 0.1;
      const mag = Math.max(s.demand_mw, s.served_mw) / maxLoad;
      const color = shedding ? "#ef4444" : s.role === "source" ? "#10b981" : "#00e5ff";
      const detail = s.role === "demand"
        ? `${Math.round(s.served_mw)} / ${Math.round(s.demand_mw)} MW served${shedding ? ` · SHED ${Math.round(s.shed_mw)} MW` : ""}`
        : `injecting ${Math.round(s.served_mw || 0)} MW`;

      subs.features.push({
        type: "Feature",
        geometry: { type: "Point", coordinates: [s.lon, s.lat] },
        properties: {
          name: s.name, role: s.role, color, mag, detail,
          label: `${s.name} · ${Math.round(s.role === "demand" ? s.served_mw : s.demand_mw || s.served_mw)} MW`,
        },
      });

      subs3d.features.push({
        type: "Feature",
        geometry: { type: "Polygon", coordinates: [circlePoly(s.lon, s.lat, 0.0042 + 0.004 * mag)] },
        properties: { color, height: 200 + 5200 * mag },
      });
    }

    this.map.getSource("corridors").setData(corridors);
    this.map.getSource("future").setData(future);
    this.map.getSource("subs").setData(subs);
    this.map.getSource("subs3d").setData(subs3d);
  },

  corridorColor(c) {
    const l = c.max_circuit_loading;
    if (l >= 0.95) return "#ef4444";
    if (l >= 0.85) return "#f97316";
    if (l >= 0.60) return "#eab308";
    // below the stress band, tint by voltage class like the OGW legend
    return KV_COLOR[c.voltage_kv] || "#3b82f6";
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

/* small circle polygon so substations can be extruded as 3D columns */
function circlePoly(lon, lat, radiusDeg, sides = 14) {
  const ring = [];
  const latScale = Math.cos((lat * Math.PI) / 180) || 1;
  for (let i = 0; i <= sides; i++) {
    const t = (i / sides) * Math.PI * 2;
    ring.push([lon + (radiusDeg * Math.cos(t)) / latScale, lat + radiusDeg * Math.sin(t)]);
  }
  return ring;
}

window.GridMap = GridMap;
