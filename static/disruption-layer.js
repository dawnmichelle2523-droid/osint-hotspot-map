(function () {
  const STYLE = [
    ".disruption-control{background:rgba(13,20,36,.94);color:#E2E8F0;border:1px solid #C5A059;border-radius:10px;min-width:220px;max-width:min(280px,78vw);box-shadow:0 10px 24px rgba(0,0,0,.45);font:12px/1.35 'Segoe UI',system-ui,sans-serif;overflow:hidden}",
    ".disruption-master{width:100%;display:flex;align-items:center;gap:8px;background:#102A63;color:#D4AF37;border:0;padding:8px 10px;font:inherit;font-weight:750;cursor:pointer;text-align:left}",
    ".disruption-master:hover,.disruption-master:focus-visible{color:#F8FAFC;outline:2px solid #C5A059;outline-offset:-2px}",
    ".disruption-mark{width:8px;height:8px;border-radius:50%;background:#94A3B8;box-shadow:0 0 0 1px #060A13;flex:0 0 auto}",
    ".disruption-master[aria-pressed='true'] .disruption-mark{background:#D4AF37;box-shadow:0 0 8px #D4AF37}",
    ".disruption-panel{padding:8px 10px 10px;display:flex;flex-direction:column;gap:6px}",
    ".disruption-panel[hidden]{display:none !important}",
    ".disruption-panel label{display:flex;align-items:center;gap:8px;color:#E2E8F0;cursor:pointer;white-space:nowrap}",
    ".disruption-count{margin-left:auto;color:#D4AF37;font-weight:750;font-variant-numeric:tabular-nums}",
    ".disruption-panel input{accent-color:#D4AF37}",
    ".disruption-swatch{width:10px;height:10px;border-radius:2px;flex:0 0 auto}",
    ".disruption-swatch.air{background:rgba(225,29,72,.85)}",
    ".disruption-swatch.gps{background:#C084FC;border-radius:50%;box-shadow:0 0 0 2px rgba(192,132,252,.35)}",
    ".disruption-swatch.sea{background:#38BDF8}",
    ".disruption-status,.disruption-note{margin:0;color:#94A3B8;font-size:11px}",
    ".disruption-icon{background:transparent;border:0}",
    ".disruption-dot{display:block;width:12px;height:12px;border:2px solid #F8FAFC;box-shadow:0 0 0 1px #060A13}",
    ".disruption-dot-sea{background:#38BDF8;border-radius:50%}",
    ".disruption-dot-gps{background:#C084FC;border-radius:2px;transform:rotate(45deg)}",
    ".disruption-dot-air{background:#E11D48;border-radius:50%}",
    ".leaflet-tooltip.disruption-tip{background:#0D1424;color:#E2E8F0;border:1px solid #C5A059;border-radius:6px;box-shadow:none;font-size:12px;font-weight:650}",
    ".leaflet-tooltip.disruption-tip::before{display:none}",
    ".disruption-popup h3{margin:0 0 6px;color:#D4AF37;font-size:14px}",
    ".disruption-kicker{margin:0 0 4px;color:#94A3B8;font-size:10px;letter-spacing:.08em}",
    ".disruption-popup p{margin:0 0 6px}",
    ".disruption-chip{display:inline-block;border-radius:999px;padding:1px 7px;font-size:10px;font-weight:750;border:1px solid #C5A059;color:#D4AF37}",
    ".disruption-src{color:#94A3B8;font-size:11px}",
    ".disruption-src a{color:#D4AF37}"
  ].join("");

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[ch]));
  }

  function stamp(value) {
    const text = String(value || "").trim();
    if (!text) return "n/a";
    return text.replace("T", " ").replace(".000Z", " UTC").replace("Z", " UTC");
  }

  function level(item) {
    return String(item.severity || item.threat_level || "medium").toLowerCase();
  }

  function airColor(item) {
    const name = level(item);
    if (name === "high") return "#E11D48";
    if (name === "low") return "#3B82F6";
    return "#D97706";
  }

  function seaColor(item) {
    return level(item) === "high" ? "#38BDF8" : "#22D3EE";
  }

  function pairs(polygon) {
    return (polygon || []).map((pair) => [Number(pair[0]), Number(pair[1])]).filter((pair) => Number.isFinite(pair[0]) && Number.isFinite(pair[1]));
  }

  function centerOf(item) {
    const raw = item.center || item.coords || [];
    const lat = Number(raw[0]);
    const lng = Number(raw[1]);
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
    return [lat, lng];
  }

  function popupHtml(item, kind) {
    const title = item.name || item.chokepoint || "Advisory";
    const kicker = kind === "gps" ? "GNSS INTERFERENCE" : kind === "maritime" ? "MARITIME CHOKEPOINT" : "AIRSPACE ADVISORY";
    const corridor = item.corridor || item.fir || item.chokepoint || "";
    const summary = item.advisory_summary || item.summary || "";
    const guidance = item.operational_guidance || item.operational_impact || "";
    const posture = item.origin === "live" ? "Live public feed" : "Structured baseline";
    const source = item.source_url
      ? '<a href="' + esc(item.source_url) + '" target="_blank" rel="noopener noreferrer">' + esc(item.source || "Source") + "</a>"
      : esc(item.source || "Public baseline");
    let extra = "";
    if (item.fir) extra += "<p><strong>FIR</strong> " + esc(item.fir) + "</p>";
    if (kind === "maritime") extra += "<p><strong>Recent maritime incidents</strong> " + esc(item.live_incident_count || 0) + "</p>";
    if (kind === "gps" && item.radius_km) extra += "<p><strong>Radius</strong> " + esc(item.radius_km) + " km</p>";
    return '<div class="disruption-popup"><p class="disruption-kicker">' + kicker + "</p><h3>" + esc(title) + "</h3>" +
      '<p><span class="disruption-chip">' + esc(level(item).toUpperCase()) + "</span> " + esc(posture) + "</p>" +
      (corridor ? "<p><strong>Corridor</strong> " + esc(corridor) + "</p>" : "") +
      extra +
      (summary ? "<p>" + esc(summary) + "</p>" : "") +
      (guidance ? "<p><strong>Guidance</strong> " + esc(guidance) + "</p>" : "") +
      '<p class="disruption-src">' + source + " · Updated " + esc(stamp(item.updated_at)) + "</p>" +
      (item.geometry_note ? '<p class="disruption-src">' + esc(item.geometry_note) + "</p>" : "") +
      "</div>";
  }

  function feedLine(data) {
    const feeds = data.feeds || [];
    const live = feeds.filter((feed) => feed.ok).length;
    if (!feeds.length) return "Structured baseline advisories.";
    if (!live) return "Live feeds unreachable. Showing the structured baseline.";
    return live + " of " + feeds.length + " public feeds responded. Baseline corridors stay on the map.";
  }

  function boot() {
    const map = window.GeoRiskMap && window.GeoRiskMap.map;
    if (!map || !window.L || document.querySelector(".disruption-control")) return;
    if (!document.getElementById("disruption-layer-style")) {
      const style = document.createElement("style");
      style.id = "disruption-layer-style";
      style.textContent = STYLE;
      document.head.appendChild(style);
    }
    if (!map.getPane("disruption")) {
      map.createPane("disruption");
      map.getPane("disruption").style.zIndex = "430";
    }

    const groups = {
      airspace: L.layerGroup(),
      gps: L.layerGroup(),
      maritime: L.layerGroup()
    };
    let master = false;
    let current = null;
    let fetchedAt = 0;
    let token = 0;

    const control = L.control({ position: "topright" });
    control.onAdd = function () {
      const wrap = L.DomUtil.create("section", "disruption-control leaflet-control");
      wrap.setAttribute("aria-label", "Airspace and maritime disruption layer");
      wrap.innerHTML = [
        '<button type="button" class="disruption-master" id="disruption-master" aria-pressed="false" aria-expanded="false">',
        '<span class="disruption-mark" aria-hidden="true"></span>Airspace &amp; Maritime</button>',
        '<div class="disruption-panel" id="disruption-panel" hidden>',
        '<label><i class="disruption-swatch air" aria-hidden="true"></i><input type="checkbox" data-layer="airspace" checked> Airspace advisories <span class="disruption-count" data-count="airspace"></span></label>',
        '<label><i class="disruption-swatch gps" aria-hidden="true"></i><input type="checkbox" data-layer="gps" checked> GPS jamming <span class="disruption-count" data-count="gps"></span></label>',
        '<label><i class="disruption-swatch sea" aria-hidden="true"></i><input type="checkbox" data-layer="maritime" checked> Maritime chokepoints <span class="disruption-count" data-count="maritime"></span></label>',
        '<p class="disruption-status" id="disruption-ao"></p>',
        '<p class="disruption-status" id="disruption-status" role="status"></p>',
        '<p class="disruption-note">Public advisories only. Verify NOTAMs and maritime warnings before dispatch.</p>',
        "</div>"
      ].join("");
      L.DomEvent.disableClickPropagation(wrap);
      L.DomEvent.disableScrollPropagation(wrap);
      return wrap;
    };
    control.addTo(map);

    const button = document.getElementById("disruption-master");
    const panel = document.getElementById("disruption-panel");
    const status = document.getElementById("disruption-status");

    function wanted(name) {
      const box = panel.querySelector('[data-layer="' + name + '"]');
      return !!(box && box.checked);
    }

    function hideAll() {
      Object.values(groups).forEach((group) => {
        group.clearLayers();
        if (map.hasLayer(group)) map.removeLayer(group);
      });
    }

    function icon(kind) {
      const cls = kind === "gps" ? "disruption-dot-gps" : kind === "maritime" ? "disruption-dot-sea" : "disruption-dot-air";
      return L.divIcon({
        className: "disruption-icon",
        html: '<i class="' + cls + '"></i>',
        iconSize: [12, 12],
        iconAnchor: [6, 6],
        popupAnchor: [0, -8]
      });
    }

    function addShape(group, layer, item, kind) {
      const html = popupHtml(item, kind);
      const center = centerOf(item);
      layer.bindPopup(html, { maxWidth: 320, minWidth: 220, maxHeight: 280 });
      layer.bindTooltip(esc(item.name || item.chokepoint || "Advisory"), { className: "disruption-tip", sticky: true });
      group.addLayer(layer);
      if (!center) return;
      const marker = L.marker(center, { icon: icon(kind), pane: "markerPane" });
      marker.bindPopup(html, { maxWidth: 320, minWidth: 220, maxHeight: 280 });
      marker.bindTooltip(esc(item.name || item.chokepoint || "Advisory"), { className: "disruption-tip" });
      group.addLayer(marker);
    }

    function aoState() {
      if (window.GeoRiskAO) return window.GeoRiskAO;
      return { region: "all", country: "all" };
    }

    function scoped(items, ao) {
      if (!window.GeoSplit) return { items: items, fallback: false };
      if ((!ao.country || ao.country === "all") && (!ao.region || ao.region === "all")) return { items: items, fallback: false };
      const matched = items.filter((item) => GeoSplit.advisoryMatches(item, ao));
      if (ao.country && ao.country !== "all" && !matched.length) {
        const region = ao.region && ao.region !== "all" ? ao.region : GeoSplit.regionOf(ao.country);
        return { items: items.filter((item) => GeoSplit.advisoryMatches(item, { region: region, country: "all" })), fallback: true };
      }
      return { items: matched, fallback: false };
    }

    function paint() {
      hideAll();
      if (!master || !current) return;
      const ao = aoState();
      const airScope = scoped(current.airspace || [], ao);
      const gpsScope = scoped(current.gps_jamming || [], ao);
      const seaScope = scoped(current.maritime || [], ao);
      const air = airScope.items;
      const gps = gpsScope.items;
      const sea = seaScope.items;
      const aoLine = document.getElementById("disruption-ao");
      if (aoLine && window.GeoSplit) {
        const label = GeoSplit.aoLabel(ao);
        const fallback = airScope.fallback || gpsScope.fallback || seaScope.fallback;
        aoLine.textContent = (!ao.region || ao.region === "all") && (!ao.country || ao.country === "all")
          ? "All areas. Pin an Area of Operations to focus this panel."
          : (fallback
            ? "Area of Operations: " + label + ". No country-specific note, so the region is shown."
            : "Area of Operations: " + label + ".");
      }
      panel.querySelector('[data-count="airspace"]').textContent = String(air.length);
      panel.querySelector('[data-count="gps"]').textContent = String(gps.length);
      panel.querySelector('[data-count="maritime"]').textContent = String(sea.length);
      if (wanted("airspace")) {
        air.forEach((item) => {
          const ring = pairs(item.polygon);
          if (ring.length < 3) return;
          const color = airColor(item);
          addShape(groups.airspace, L.polygon(ring, {
            pane: "disruption", color: color, weight: 2, opacity: 0.95,
            fillColor: color, fillOpacity: 0.22
          }), item, "airspace");
        });
        if (!map.hasLayer(groups.airspace)) groups.airspace.addTo(map);
      }
      if (wanted("gps")) {
        gps.forEach((item) => {
          const center = centerOf(item);
          const radius = Number(item.radius_km);
          if (!center || !Number.isFinite(radius) || radius <= 0) return;
          addShape(groups.gps, L.circle(center, {
            pane: "disruption", radius: radius * 1000, color: "#C084FC", weight: 2,
            opacity: 0.95, dashArray: "5 6", fillColor: "#C084FC", fillOpacity: 0.12
          }), item, "gps");
        });
        if (!map.hasLayer(groups.gps)) groups.gps.addTo(map);
      }
      if (wanted("maritime")) {
        sea.forEach((item) => {
          const ring = pairs(item.polygon);
          if (ring.length < 3) return;
          const color = seaColor(item);
          addShape(groups.maritime, L.polygon(ring, {
            pane: "disruption", color: color, weight: 2, opacity: 0.95,
            fillColor: color, fillOpacity: 0.24
          }), item, "maritime");
        });
        if (!map.hasLayer(groups.maritime)) groups.maritime.addTo(map);
      }
      const shown = ["airspace", "gps", "maritime"].filter(wanted);
      status.textContent = shown.length
        ? feedLine(current)
        : "All sub-layers are hidden. Turn one on to draw the corridors.";
    }

    async function ensureData() {
      if (current && Date.now() - fetchedAt < 15 * 60 * 1000) return current;
      const response = await fetch("/api/disruptions", { cache: "no-store" });
      if (!response.ok) throw new Error("status " + response.status);
      const data = await response.json();
      if (!data || !Array.isArray(data.airspace) || !Array.isArray(data.gps_jamming) || !Array.isArray(data.maritime)) {
        throw new Error("schema");
      }
      current = data;
      fetchedAt = Date.now();
      return data;
    }

    async function setMaster(on) {
      master = !!on;
      button.setAttribute("aria-pressed", master ? "true" : "false");
      button.setAttribute("aria-expanded", master ? "true" : "false");
      panel.hidden = !master;
      if (!master) {
        token += 1;
        hideAll();
        status.textContent = "";
        return;
      }
      const mine = ++token;
      status.textContent = "Loading public advisories…";
      try {
        await ensureData();
        if (mine !== token || !master) return;
        paint();
      } catch (err) {
        if (mine !== token || !master) return;
        hideAll();
        status.textContent = "Advisories could not be loaded. The rest of the map is unchanged.";
      }
    }

    document.addEventListener("georisk-ao", () => { if (master) paint(); });
    button.addEventListener("click", () => { setMaster(!master); });
    panel.addEventListener("change", () => { if (master) paint(); });
    window.setInterval(() => {
      if (!master) return;
      fetchedAt = 0;
      setMaster(true);
    }, 15 * 60 * 1000);
    window.GeoRiskDisruption = {
      enable: function () { return setMaster(true); },
      disable: function () { return setMaster(false); }
    };
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
