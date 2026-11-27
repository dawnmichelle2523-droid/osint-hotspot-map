(function () {
  const REGIONS = ["All", "Global", "Middle East", "United States", "Canada", "South America", "APAC", "Australia", "Afghanistan", "Russia & Ukraine", "Europe", "Africa"];
  let region = "All";
  let items = [];

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }
  function relative(stamp) {
    const ms = Date.parse(stamp || "");
    if (!Number.isFinite(ms)) return "";
    const minutes = Math.max(0, Math.round((Date.now() - ms) / 60000));
    if (minutes < 1) return "just now";
    if (minutes < 60) return minutes + "m ago";
    const hours = Math.round(minutes / 60);
    if (hours < 48) return hours + "h ago";
    return Math.round(hours / 24) + "d ago";
  }
  function paintRegions() {
    const host = document.getElementById("news-regions");
    if (!host || host.childElementCount) return;
    host.innerHTML = REGIONS.map((name) => '<button type="button" data-news-region="' + esc(name) + '"' + (name === region ? ' aria-pressed="true"' : "") + ">" + esc(name) + "</button>").join("");
    host.addEventListener("click", (event) => {
      const button = event.target.closest("[data-news-region]");
      if (!button) return;
      region = button.dataset.newsRegion;
      host.querySelectorAll("button").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
      paint();
    });
  }
  function paint() {
    const host = document.getElementById("news-list");
    if (!host) return;
    const rows = items.filter((item) => region === "All" || item.region === region || item.country === region);
    if (!rows.length) {
      host.innerHTML = '<p class="headline-empty">No headlines for this region yet.</p>';
      return;
    }
    host.innerHTML = rows.map((item) => {
      const place = [item.country, item.area].filter(Boolean).join(" · ");
      const focus = item.lat != null && item.lng != null
        ? ' data-news-lat="' + esc(item.lat) + '" data-news-lng="' + esc(item.lng) + '"'
        : "";
      return '<article class="headline-card"' + focus + ' style="cursor:' + (focus ? "pointer" : "default") + '">' +
        '<p class="source-pill">' + esc(item.source_name || "Wire") + "</p>" +
        "<h3>" + esc(item.title || "Update") + "</h3>" +
        (item.summary && item.summary !== item.title ? '<p class="summary">' + esc(item.summary) + "</p>" : "") +
        '<p class="update-place">' + esc(place) + (place ? " · " : "") + esc(relative(item.timestamp)) +
        (item.source ? ' · <a href="' + esc(item.source) + '" target="_blank" rel="noopener noreferrer">Source</a>' : "") +
        "</p></article>";
    }).join("");
  }
  async function load() {
    paintRegions();
    try {
      const response = await fetch("/api/news-stream", { cache: "no-store" });
      const data = await response.json();
      items = Array.isArray(data.items) ? data.items : [];
      paint();
    } catch (err) {
      const host = document.getElementById("news-list");
      if (host) host.innerHTML = '<p class="headline-empty">News wires are unavailable.</p>';
    }
  }
  document.addEventListener("click", (event) => {
    const card = event.target.closest("#news-list [data-news-lat]");
    if (!card || event.target.closest("a")) return;
    if (window.GeoRiskMap && window.GeoRiskMap.focus) {
      window.GeoRiskMap.focus(card.dataset.newsLat, card.dataset.newsLng);
    }
  });
  if (document.getElementById("news-list")) {
    load();
    setInterval(load, 60000);
  }
})();
