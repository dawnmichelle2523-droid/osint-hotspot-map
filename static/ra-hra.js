/* Eight-section High Risk Assessment layout.
   Staff names, phones, email, and IMEI stay in this browser.
   Nothing in this file calls xAI or posts those fields. */
(function () {
  var STORE = "georisk.ra.hra.v1";
  var LOCAL_IDS = ["hra-pocs", "hra-ops-in", "hra-ops-out", "hra-external", "hra-manager"];

  function $(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value || "").replace(/[&<>"]/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[ch];
    });
  }
  function field(id) { var node = $(id); return node ? node.value : ""; }
  function setField(id, value) { var node = $(id); if (node && value != null) node.value = value; }

  function readStore() {
    try {
      var parsed = JSON.parse(localStorage.getItem(STORE) || "null");
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (err) { return {}; }
  }
  function writeStore(extra) {
    var state = readStore();
    document.querySelectorAll("[data-hra]").forEach(function (node) {
      if (node.type === "checkbox") state[node.id] = node.checked;
      else if (node.id) state[node.id] = node.value;
    });
    LOCAL_IDS.forEach(function (id) { state[id] = field(id); });
    state.roster = readRoster();
    if (extra) Object.keys(extra).forEach(function (key) { state[key] = extra[key]; });
    try { localStorage.setItem(STORE, JSON.stringify(state)); } catch (err) {}
  }
  function readRoster() {
    return Array.prototype.map.call(document.querySelectorAll("#hra-roster tbody tr"), function (row) {
      var cells = row.querySelectorAll("input");
      return {
        role: cells[0] ? cells[0].value : "",
        callsign: cells[1] ? cells[1].value : "",
        tracking: cells[2] ? cells[2].value : "",
        phone: cells[3] ? cells[3].value : "",
        email: cells[4] ? cells[4].value : "",
        imei: cells[5] ? cells[5].value : ""
      };
    });
  }
  function rosterRow(item) {
    item = item || {};
    var keys = ["role", "callsign", "tracking", "phone", "email", "imei"];
    return "<tr>" + keys.map(function (key) {
      var local = key === "phone" || key === "email" || key === "imei";
      return "<td><input data-roster=\"" + key + "\" " + (local ? "data-hra-local=\"1\" " : "data-hra=\"1\" ") + "value=\"" + esc(item[key] || "") + "\" autocomplete=\"off\"></td>";
    }).join("") + "</tr>";
  }
  function paintRoster(rows) {
    var body = document.querySelector("#hra-roster tbody");
    if (!body) return;
    var list = rows && rows.length ? rows : [{ role: "", callsign: "", tracking: "Not started" }];
    body.innerHTML = list.map(rosterRow).join("");
  }
  function band(score) {
    if (score <= 4) return "LOW";
    if (score <= 9) return "MEDIUM";
    if (score <= 16) return "HIGH";
    return "SEVERE";
  }
  function paintRating() {
    var max = 0;
    var tags = [];
    document.querySelectorAll("#matrix tbody tr").forEach(function (row) {
      var likelihood = row.querySelector("[data-field=\"likelihood\"]");
      var impact = row.querySelector("[data-field=\"impact\"]");
      var threat = row.querySelector("[data-field=\"threat\"]");
      var score = (Number(likelihood && likelihood.value) || 0) * (Number(impact && impact.value) || 0);
      if (score > max) max = score;
      if (score >= 10 && threat && threat.value && tags.indexOf(threat.value) < 0) tags.push(threat.value);
    });
    var name = $("country") ? $("country").value : "";
    var profile = window.GeoSplit ? GeoSplit.countryTemplate(name) : null;
    if (!max && profile && profile.rating) max = profile.rating.score;
    var label = band(max || 0);
    if ($("hra-band")) $("hra-band").textContent = label;
    if ($("hra-score")) $("hra-score").textContent = String(max || 0);
    if ($("hra-tags")) $("hra-tags").textContent = "Primary areas of risk: " + ((tags.length ? tags : (profile && profile.risk_tags) || ["Baseline"]).join(" | "));
    var sector = $("template");
    var sectorName = sector && sector.options[sector.selectedIndex] ? sector.options[sector.selectedIndex].textContent : "Sector template";
    var templateTitle = profile ? profile.title : "Country template";
    if ($("template-badge")) $("template-badge").textContent = "Country template: " + templateTitle + " · Sector template: " + sectorName;
    var region = window.GeoSplit ? (GeoSplit.regionOf(name) || "") : "";
    if ($("ao-print-line")) $("ao-print-line").textContent = "Area of Operations: " + (region ? region + " · " : "") + (name || "Select a country");
    if ($("doc-title") && name) $("doc-title").textContent = name + " High Risk Assessment";
  }
  function paintProfile() {
    var name = $("country") ? $("country").value : "";
    if (!name || !window.GeoSplit) return;
    var profile = GeoSplit.countryTemplate(name);
    if ($("geo-summary-kicker")) $("geo-summary-kicker").textContent = profile.baseline ? name + " is using the baseline country template for " + profile.region + "." : name + " country template · " + profile.region + ".";
    if ($("geo-stability")) $("geo-stability").textContent = profile.stability || "";
    if ($("geo-actors")) $("geo-actors").textContent = profile.actors || "";
    if ($("geo-borders")) $("geo-borders").textContent = profile.borders || "";
    if ($("hra-medical-plan")) $("hra-medical-plan").textContent = (profile.sections && profile.sections.medical) || "";
    var medevac = $("hra-medevac-plan");
    if (medevac && !medevac.dataset.dirty) medevac.value = (profile.sections && profile.sections.medical) || "";
    var facilities = $("hra-facilities");
    if (facilities) {
      facilities.innerHTML = (profile.facilities || []).map(function (item) {
        return "<article><h4>" + esc(item.name) + "</h4><p>" + esc(item.city || "") + "</p><p class=\"note\">" + esc(item.note || "") + "</p></article>";
      }).join("");
    }
    var refs = $("hra-references");
    if (refs) refs.innerHTML = (profile.references || []).map(function (item) { return "<li>" + esc(item) + "</li>"; }).join("");
    if (window.GeoRiskRows && !GeoRiskRows.dirty()) GeoRiskRows.replace(profile.hazards || []);
    paintRating();
    var regionSelect = $("ao-region");
    if (regionSelect && profile.region) regionSelect.value = profile.region;
  }
  function loadSummary() {
    var name = $("country") ? $("country").value : "";
    var region = $("ao-region") ? $("ao-region").value : "";
    if (!name) return;
    var query = new URLSearchParams();
    if (region && region !== "all") query.set("region", region);
    query.set("country", name);
    fetch("/api/geo/summary?" + query.toString(), { cache: "no-store" }).then(function (response) {
      return response.json();
    }).then(function (summary) {
      if (!summary || !summary.ok) return;
      if ($("geo-stability")) $("geo-stability").textContent = summary.stability || "";
      if ($("geo-actors")) $("geo-actors").textContent = summary.actors || "";
      if ($("geo-borders")) $("geo-borders").textContent = summary.borders || "";
      if ($("geo-trend")) $("geo-trend").textContent = summary.trend ? summary.trend.sentence : "";
    }).catch(function () {
      if ($("geo-trend")) $("geo-trend").textContent = "Live counts could not be loaded. The country baseline is still shown.";
    });
  }
  function fillRegions() {
    var regionSelect = $("ao-region");
    var jump = $("ao-country-jump");
    if (!regionSelect || !window.GeoSplit) return;
    var current = regionSelect.value;
    regionSelect.innerHTML = GeoSplit.REGIONS.map(function (row) {
      return "<option value=\"" + esc(row.label) + "\">" + esc(row.label) + "</option>";
    }).join("");
    var country = $("country") ? $("country").value : "";
    var implied = GeoSplit.regionOf(country) || current || "Middle East";
    if ([].some.call(regionSelect.options, function (option) { return option.value === implied; })) regionSelect.value = implied;
    fillJump();
  }
  function fillJump() {
    var jump = $("ao-country-jump");
    var regionSelect = $("ao-region");
    if (!jump || !window.GeoSplit) return;
    var names = GeoSplit.countriesFor(regionSelect ? regionSelect.value : "all");
    if (names.indexOf("Sudan") < 0 && (!regionSelect || regionSelect.value === "Africa" || regionSelect.value === "all")) names.push("Sudan");
    jump.innerHTML = names.map(function (name) { return "<option>" + esc(name) + "</option>"; }).join("");
    var country = $("country") ? $("country").value : "";
    if (country && [].some.call(jump.options, function (option) { return option.value === country; })) jump.value = country;
  }
  function moveSlot(slotId, nodeId) {
    var slot = $(slotId);
    var node = $(nodeId);
    if (slot && node && node.parentElement !== slot) slot.appendChild(node);
  }
  function restore() {
    var state = readStore();
    Object.keys(state).forEach(function (key) {
      var node = $(key);
      if (!node || key === "roster") return;
      if (node.type === "checkbox") node.checked = !!state[key];
      else node.value = state[key] == null ? "" : state[key];
    });
    paintRoster(state.roster || []);
  }
  function clearLocal() {
    try { localStorage.removeItem(STORE); } catch (err) {}
    document.querySelectorAll("[data-hra-local], #pii textarea, #pii input, #names, #contacts, #medical, #passport").forEach(function (node) {
      if ("value" in node) node.value = "";
    });
    paintRoster([{}]);
    writeStore();
    var note = $("ao-pin-note");
    if (note) note.textContent = "Local staff details were cleared from this browser. Nothing was sent to the server.";
  }
  function readyBanner() {
    var params = new URLSearchParams(window.location.search);
    if (params.get("auto") !== "1") return;
    var name = ($("country") && $("country").value) || params.get("country") || "this country";
    var banner = document.createElement("p");
    banner.className = "note";
    banner.id = "hra-auto-ready";
    banner.textContent = name + " High Risk Assessment is ready, with the 8-section template and the live 7-day OSINT feed. Preview and download the PDF.";
    var button = $("download");
    if (button && button.parentElement) button.parentElement.insertBefore(banner, button);
    if (button) {
      button.textContent = "Preview & Download Country Risk Assessment PDF";
      button.focus();
    }
  }
  function boot() {
    var matrix = $("matrix");
    var matrixPanel = matrix && matrix.closest("section");
    if (matrixPanel && $("hra-matrix-slot") && matrixPanel.id !== "hra-2") $("hra-matrix-slot").appendChild(matrixPanel);
    moveSlot("hra-incident-slot", "live-incidents");
    moveSlot("hra-vault-slot", "pii");
    moveSlot("hra-signoff-slot", "ra-audit-panel");
    fillRegions();
    restore();
    paintProfile();
    loadSummary();
    readyBanner();
    var regionSelect = $("ao-region");
    var jump = $("ao-country-jump");
    if (regionSelect) regionSelect.addEventListener("change", function () {
      fillJump();
      if (jump && jump.value && $("country")) {
        $("country").value = jump.value;
        $("country").dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    if (jump) jump.addEventListener("change", function () {
      if ($("country")) {
        var option = [].some.call($("country").options, function (item) { return item.value === jump.value; });
        if (!option) {
          var extra = document.createElement("option");
          extra.value = jump.value;
          extra.textContent = jump.value;
          $("country").appendChild(extra);
        }
        $("country").value = jump.value;
        $("country").dispatchEvent(new Event("change", { bubbles: true }));
      }
    });
    var pin = $("ao-pin");
    if (pin) pin.addEventListener("click", function () {
      if (!window.GeoSplit) return;
      var saved = GeoSplit.writeAo(localStorage, { region: regionSelect ? regionSelect.value : "", country: $("country") ? $("country").value : "" });
      if ($("ao-pin-note")) $("ao-pin-note").textContent = "Pinned. This browser will open on " + GeoSplit.aoLabel(saved) + ". No names are stored.";
    });
    var clearPin = $("ao-clear");
    if (clearPin) clearPin.addEventListener("click", function () {
      if (window.GeoSplit) GeoSplit.clearAo(localStorage);
      if ($("ao-pin-note")) $("ao-pin-note").textContent = "Pinned Area of Operations cleared from this browser.";
    });
    var add = $("hra-add-roster");
    if (add) add.addEventListener("click", function () {
      var body = document.querySelector("#hra-roster tbody");
      if (body) body.insertAdjacentHTML("beforeend", rosterRow({}));
      writeStore();
    });
    var clear = $("hra-clear-local");
    if (clear) clear.addEventListener("click", clearLocal);
    document.addEventListener("input", function (event) {
      if (event.target && event.target.dataset && event.target.id === "hra-medevac-plan") event.target.dataset.dirty = "1";
      if (event.target && (event.target.dataset.hra || event.target.dataset.hraLocal || event.target.dataset.roster)) writeStore();
    });
    document.addEventListener("change", function (event) {
      if (event.target && (event.target.dataset.hra || event.target.id === "country" || event.target.id === "template" || event.target.id === "sector")) {
        paintProfile();
        if (event.target.id === "country") loadSummary();
      }
    });
    document.addEventListener("georisk-hra-refresh", function () { paintProfile(); paintRating(); });
    document.addEventListener("georisk-catalog", function () { paintProfile(); loadSummary(); });
    var params = new URLSearchParams(window.location.search);
    if (!params.get("country") && window.GeoSplit) {
      var pinned = GeoSplit.readAo(localStorage);
      if (pinned && pinned.country && pinned.country !== "all" && $("country")) {
        $("country").value = pinned.country;
        $("country").dispatchEvent(new Event("change", { bubbles: true }));
      }
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
  if (window.GeoSplit) {
    fetch("/api/geo/catalog", { cache: "no-store" }).then(function (response) { return response.json(); }).then(function (payload) {
      GeoSplit.setCatalog(payload);
      document.dispatchEvent(new CustomEvent("georisk-catalog"));
    }).catch(function () {});
  }
})();
