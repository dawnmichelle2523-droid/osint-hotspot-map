(function () {
  function esc(value) {
    return String(value ?? "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));
  }
  function snapshot() {
    if (window.GeoRiskMap && window.GeoRiskMap.snapshot) return window.GeoRiskMap.snapshot();
    return { filters: {}, incidents: [] };
  }
  function shell() {
    const tools = document.getElementById("header-tools");
    if (tools && !document.getElementById("open-advisories")) {
      const wrap = document.createElement("div");
      wrap.innerHTML = '<button type="button" id="open-advisories">Advisories</button><button type="button" id="open-copilot">Co-Pilot</button><button type="button" id="open-grok-drawer">Grok</button>';
      tools.prepend(wrap);
    }
    if (document.getElementById("advisory-panel")) return;
    const host = document.createElement("div");
    host.innerHTML = [
      '<style>#advisory-panel[hidden],#copilot[hidden],#grok-drawer[hidden]{display:none !important}#advisory-panel{position:fixed;inset:0;z-index:2100;background:rgba(0,0,0,.72);display:flex;align-items:flex-start;justify-content:center;padding:64px 12px 12px}#copilot{display:flex}</style>',
      '<div id="advisory-panel" hidden>',
      '<div style="width:min(960px,100%);max-height:80vh;overflow:auto;background:#0D1424;border:1px solid #C5A059;border-radius:12px;padding:14px">',
      '<div style="display:flex;justify-content:space-between;gap:8px"><h2 style="margin:0;color:#D4AF37">Travel advisory cross-check</h2><button type="button" id="close-advisories">Close</button></div>',
      '<p style="color:#94A3B8">UK FCDO, US State Department, and Global Affairs Canada, side by side.</p>',
      '<label>Country <input id="advisory-country" value="Syria"></label>',
      '<p><button type="button" id="advisory-go">Compare</button> <button type="button" id="advisory-map">Use map theatre</button></p>',
      '<div id="advisory-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px"></div>',
      '<p id="advisory-note" style="color:#94A3B8"></p></div></div>',
      '<aside id="copilot" hidden style="position:fixed;top:0;right:0;bottom:0;width:min(380px,100%);z-index:2050;background:#0D1424;border-left:1px solid #C5A059;padding:14px;flex-direction:column;gap:8px">',
      '<div style="display:flex;justify-content:space-between"><strong style="color:#D4AF37">Grok Analyst</strong><button type="button" id="close-copilot">Close</button></div>',
      '<p id="copilot-context" style="color:#94A3B8;margin:0"></p>',
      '<div id="copilot-log" style="flex:1;overflow:auto;color:#E2E8F0"></div>',
      '<form id="copilot-form"><input id="copilot-q" placeholder="Ask about the incidents on screen" style="width:100%;margin-bottom:8px"><button type="submit">Ask</button></form>',
      '<p style="color:#94A3B8;font-size:12px">Do not paste personal or medical information.</p></aside>',
      '<aside id="grok-drawer" hidden style="position:fixed;top:0;left:0;bottom:0;width:min(360px,100%);z-index:2050;background:#0D1424;border-right:1px solid #C5A059;padding:14px;overflow:auto">',
      '<button type="button" id="close-grok">Close</button><h2 style="color:#D4AF37">Grok Ops</h2><div id="grok-drawer-body"></div><p><a href="/grok-ops">Open full dashboard</a></p></aside>'
    ].join("");
    document.body.appendChild(host);
    document.getElementById("open-advisories").addEventListener("click", () => { document.getElementById("advisory-panel").hidden = false; compare(); });
    document.getElementById("close-advisories").addEventListener("click", () => { document.getElementById("advisory-panel").hidden = true; });
    document.getElementById("advisory-go").addEventListener("click", compare);
    document.getElementById("advisory-map").addEventListener("click", () => {
      const filters = snapshot().filters || {};
      document.getElementById("advisory-country").value = filters.country && filters.country !== "all" ? filters.country : (filters.region && filters.region !== "all" ? filters.region : "Syria");
      compare();
    });
    document.getElementById("open-copilot").addEventListener("click", openCopilot);
    document.getElementById("close-copilot").addEventListener("click", () => { document.getElementById("copilot").hidden = true; });
    document.getElementById("copilot-form").addEventListener("submit", askCopilot);
    document.getElementById("open-grok-drawer").addEventListener("click", openGrok);
    document.getElementById("close-grok").addEventListener("click", () => { document.getElementById("grok-drawer").hidden = true; });
  }
  async function compare() {
    const country = document.getElementById("advisory-country").value || "Syria";
    const grid = document.getElementById("advisory-grid");
    const note = document.getElementById("advisory-note");
    grid.innerHTML = "<p>Loading official advice…</p>";
    try {
      const response = await fetch("/api/advisories/crosscheck?country=" + encodeURIComponent(country), { cache: "no-store" });
      const data = await response.json();
      const sources = data.sources || {};
      grid.innerHTML = ["uk", "us", "ca"].map((key) => {
        const item = sources[key] || {};
        return "<article style=\"border:1px solid #1E293B;border-radius:10px;padding:10px\"><h3 style=\"margin:0 0 6px;color:#D4AF37\">" + esc(item.agency || key) + "</h3><p><strong>" + esc(item.label || "Unavailable") + "</strong></p><p>" + esc(item.summary || item.message || "") + "</p>" + (item.url ? "<a href=\"" + esc(item.url) + "\" target=\"_blank\" rel=\"noopener\">Official source</a>" : "") + "</article>";
      }).join("");
      note.textContent = data.note || (data.cached ? "Showing a saved copy." : "");
    } catch (err) {
      grid.innerHTML = "<p>Advice could not be loaded. The map is unchanged.</p>";
    }
  }
  function openCopilot() {
    const snap = snapshot();
    const filters = snap.filters || {};
    document.getElementById("copilot-context").textContent = "Theatre " + (filters.country || filters.region || "all") + (filters.governorate ? " · " + filters.governorate : "") + " · " + (snap.incidents || []).length + " visible incidents";
    document.getElementById("copilot").hidden = false;
  }
  function askCopilot(event) {
    event.preventDefault();
    const question = document.getElementById("copilot-q").value.trim();
    if (!question || !window.GeoRiskStream) return;
    const log = document.getElementById("copilot-log");
    const block = document.createElement("div");
    block.innerHTML = "<p><strong>You</strong> " + esc(question) + "</p><p class=\"answer\"></p>";
    log.appendChild(block);
    const answer = block.querySelector(".answer");
    const snap = snapshot();
    window.GeoRiskStream.post("/api/grok/copilot", { question: question, filters: snap.filters, incidents: snap.incidents }, {
      onToken: (text) => { answer.textContent += text; },
      onError: (payload) => { answer.textContent = payload.message || "Co-pilot is unavailable."; }
    });
    document.getElementById("copilot-q").value = "";
  }
  async function openGrok() {
    document.getElementById("grok-drawer").hidden = false;
    const host = document.getElementById("grok-drawer-body");
    host.textContent = "Loading…";
    try {
      const response = await fetch("/api/grok/status", { cache: "no-store" });
      const data = await response.json();
      host.innerHTML = "<p>" + (data.configured ? "API configured" : "API key not set") + "</p><p>Tokens " + ((data.tokens && data.tokens.total) || 0) + "</p><ul>" + (data.theatres || []).slice(0, 8).map((item) => "<li>" + esc(item.name) + " (" + item.incidents + ")</li>").join("") + "</ul>";
    } catch (err) {
      host.textContent = "Grok status is unavailable.";
    }
  }
  function summarizeGovernorate() {
    const snap = snapshot();
    const filters = snap.filters || {};
    let name = String(filters.selectedGovernorate || "").trim();
    if (!name && filters.governorate && filters.governorate !== "all") name = filters.governorate;
    const status = document.getElementById("status");
    if (!name) {
      if (status) status.textContent = "Click a Syrian governorate first.";
      return;
    }
    const incidents = (snap.incidents || []).filter((item) => {
      const blob = (item.governorate + " " + item.location_name + " " + item.title).toLowerCase();
      return blob.includes(name.toLowerCase());
    });
    if (status) status.textContent = "Summarising " + name + "…";
    window.GeoRiskStream.post("/api/grok/summarize-governorate", { governorate: name, incidents: incidents.length ? incidents : snap.incidents }, {
      onToken: (text) => { if (status) status.textContent += text; },
      onError: (payload) => { if (status) status.textContent = payload.message || "Summary is unavailable."; },
      onDone: (payload) => { if (status && payload.message) status.textContent += " " + payload.message; }
    });
    if (status) status.textContent = "";
  }
  window.GeoRiskPhase1 = { summarizeGovernorate: summarizeGovernorate, compare: compare };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", shell);
  else shell();
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
})();
