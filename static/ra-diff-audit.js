/* Local risk-assessment baselines, live OSINT diff, and human sign-off.
   Snapshots, reviewer name, and role stay in localStorage. The diff request
   sends only country, region, since_iso, original_incident_ids, and
   original_risk_level. */
(function () {
  const STORAGE_KEY = "georisk.ra.baselines.v1";
  const ACTIVE_KEY = "georisk.ra.baseline-active.v1";
  const REGION_BY_COUNTRY = {
    Syria: "Middle East",
    Iraq: "Middle East",
    Lebanon: "Middle East",
    Israel: "Middle East",
    Palestine: "Middle East",
    Jordan: "Middle East",
    Iran: "Middle East",
    "Saudi Arabia": "Middle East",
    Yemen: "Middle East",
    Bahrain: "Middle East",
    Kuwait: "Middle East",
    Qatar: "Middle East",
    UAE: "Middle East",
    Oman: "Middle East",
    Turkey: "Middle East",
    Egypt: "Middle East",
    Sudan: "Africa",
    Chad: "Africa",
    Libya: "Africa",
    Ukraine: "UK & Europe",
    Afghanistan: "South Asia",
    "United States": "Americas",
    Canada: "Americas"
  };
  const SECTOR_NOTE = {
    media: "Media assessment: brief the team on profile, fixers, and the movement protocol before issue.",
    government: "Public-sector assessment: confirm the duty-of-care owner and the official movement protocol.",
    tmc: "TMC assessment: update the traveller advice and the supplier controls before the booking is released.",
    insurance: "Insurance assessment: record the upgraded controls against the policy conditions before sign-off.",
    ngo: "Humanitarian assessment: reconfirm acceptance, medevac, and the movement protocol with the security focal point.",
    psc: "Ops-room assessment: assign each upgraded control to the on-duty operator before issue.",
    schools: "BS 8848 trip: brief the safeguarding lead and confirm the supervision ratio still holds.",
    private: "Private travel: keep the profile low and confirm the emergency contact plan on this device.",
    csuite: "Executive assessment: update the movement corridor and the close-protection controls before the brief.",
    medical: "Medical assessment: pair any new hazard with the medevac trigger and hospital viability."
  };
  const SIGN_OFF = ["Approved", "Approved with Conditions", "Hold"];

  const state = { items: [], activeId: "", aiSession: false, checking: false };

  function $(id) {
    return document.getElementById(id);
  }

  function escapeText(value) {
    return String(value || "").replace(/[&<>]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[ch]));
  }

  function readStore() {
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
      return Array.isArray(parsed) ? parsed : [];
    } catch (err) {
      return [];
    }
  }

  function writeStore() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state.items.slice(0, 30)));
      if (state.activeId) localStorage.setItem(ACTIVE_KEY, state.activeId);
      else localStorage.removeItem(ACTIVE_KEY);
    } catch (err) {}
  }

  function active() {
    return state.items.find((item) => item.id === state.activeId) || null;
  }

  function nowIso() {
    return new Date().toISOString();
  }

  function formatUtc(iso) {
    const date = new Date(iso || "");
    if (Number.isNaN(date.getTime())) return "";
    const pad = (n) => String(n).padStart(2, "0");
    return date.getUTCFullYear() + "-" + pad(date.getUTCMonth() + 1) + "-" + pad(date.getUTCDate())
      + " " + pad(date.getUTCHours()) + ":" + pad(date.getUTCMinutes()) + ":" + pad(date.getUTCSeconds()) + " UTC";
  }

  function shortUtc(iso) {
    const date = new Date(iso || "");
    if (Number.isNaN(date.getTime())) return "";
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    return date.getUTCDate() + " " + months[date.getUTCMonth()];
  }

  function countryName() {
    const select = $("country");
    return select ? select.value : "";
  }

  function regionName() {
    return REGION_BY_COUNTRY[countryName()] || "";
  }

  function templateId() {
    const select = $("template");
    return select ? select.value : "security";
  }

  function fieldValue(id) {
    const el = $(id);
    return el ? el.value : "";
  }

  function readMatrix() {
    const rows = [];
    document.querySelectorAll("#matrix tbody tr").forEach((tr) => {
      const row = {};
      tr.querySelectorAll("[data-field]").forEach((field) => {
        const key = field.dataset.field;
        row[key] = ["likelihood", "impact", "residualL", "residualI"].includes(key) ? Number(field.value) : field.value;
      });
      rows.push(row);
    });
    return rows;
  }

  function inherentLevel() {
    const rank = { Low: 1, Moderate: 2, Medium: 2, High: 3, "Very High": 4, Severe: 5, Extreme: 5 };
    let best = "Low";
    document.querySelectorAll("#matrix tbody tr").forEach((tr) => {
      const band = tr.querySelector(".band");
      const name = ((band && band.textContent) || "Low").split("·")[0].trim();
      if ((rank[name] || 0) > (rank[best] || 0)) best = name;
    });
    return best;
  }

  function readForm() {
    return {
      template: templateId(),
      country: countryName(),
      city: fieldValue("city"),
      purpose: fieldValue("purpose"),
      window: fieldValue("window"),
      names: fieldValue("names"),
      contacts: fieldValue("contacts"),
      medical: fieldValue("medical"),
      passport: fieldValue("passport"),
      enterprise: !!($("enterprise") && $("enterprise").checked),
      org: fieldValue("org-name"),
      advisor: fieldValue("advisor"),
      advisorDecision: fieldValue("advisor-decision"),
      docRef: fieldValue("doc-ref"),
      docVersion: fieldValue("doc-version"),
      docClass: fieldValue("doc-class"),
      docStatus: fieldValue("doc-status"),
      rows: readMatrix()
    };
  }

  function setControl(id, value, kind) {
    const el = $(id);
    if (!el || value == null) return;
    if (el.type === "checkbox") el.checked = !!value;
    else el.value = value;
    el.dispatchEvent(new Event(kind, { bubbles: true }));
  }

  function ensureOption(select, value) {
    if (!select || !value) return;
    if (![...select.options].some((option) => option.value === value)) {
      const extra = document.createElement("option");
      extra.value = value;
      extra.textContent = value;
      select.appendChild(extra);
    }
    select.value = value;
  }

  function writeRows(savedRows) {
    const rows = Array.isArray(savedRows) ? savedRows : [];
    const add = $("add-row");
    let guard = 0;
    while (document.querySelectorAll("#matrix tbody tr").length < rows.length && add && guard < 24) {
      add.click();
      guard += 1;
    }
    const trs = document.querySelectorAll("#matrix tbody tr");
    rows.forEach((row, index) => {
      const tr = trs[index];
      if (!tr || !row) return;
      tr.querySelectorAll("[data-field]").forEach((field) => {
        const key = field.dataset.field;
        if (row[key] == null) return;
        field.value = String(row[key]);
        if (key === "context" && field.value) field.dataset.edited = "1";
      });
    });
    const matrix = $("matrix");
    if (matrix) matrix.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function applyForm(form) {
    if (!form) return;
    const template = $("template");
    if (template && form.template) {
      template.value = form.template;
      template.dispatchEvent(new Event("change", { bubbles: true }));
    }
    writeRows(form.rows);
    setControl("city", form.city || "", "input");
    setControl("purpose", form.purpose || "Media", "change");
    setControl("window", form.window || "", "input");
    setControl("names", form.names || "", "input");
    setControl("contacts", form.contacts || "", "input");
    setControl("medical", form.medical || "", "input");
    setControl("passport", form.passport || "", "input");
    setControl("org-name", form.org || "", "input");
    if ($("enterprise")) {
      $("enterprise").checked = !!form.enterprise;
      $("enterprise").dispatchEvent(new Event("change", { bubbles: true }));
    }
    setControl("advisor", form.advisor || "", "input");
    setControl("advisor-decision", form.advisorDecision || "Not used", "change");
    setControl("doc-ref", form.docRef || "", "input");
    setControl("doc-version", form.docVersion || "", "input");
    setControl("doc-class", form.docClass || "", "change");
    setControl("doc-status", form.docStatus || "", "change");
    const country = $("country");
    ensureOption(country, form.country || "");
    if (country && form.country) country.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function auditRow(event, extra) {
    const row = extra || {};
    return {
      event: event,
      reviewer: row.reviewer || "",
      role: row.role || "",
      status: row.status || "Recorded",
      at: row.at || nowIso()
    };
  }

  function diffRequestBody(payload) {
    const ids = Array.isArray(payload.original_incident_ids) ? payload.original_incident_ids : [];
    return {
      country: String(payload.country || ""),
      region: String(payload.region || ""),
      since_iso: String(payload.since_iso || ""),
      original_incident_ids: ids.map((item) => String(item)).slice(0, 500),
      original_risk_level: String(payload.original_risk_level || "Low")
    };
  }

  async function postDiff(payload) {
    const response = await fetch("/api/ra/diff", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(diffRequestBody(payload)),
      cache: "no-store"
    });
    let data = {};
    try { data = await response.json(); } catch (err) { data = {}; }
    if (!response.ok) {
      const error = new Error((data && data.error) || "The change check could not be completed.");
      throw error;
    }
    return data;
  }

  function bannerCopy(data, sinceIso) {
    const when = shortUtc(sinceIso);
    const since = when ? " since " + when : "";
    if (data && data.escalated) {
      const high = Number(data.new_high_count) || 0;
      const med = Number(data.new_med_count) || 0;
      let lead = "Risk tier increased";
      if (high > 0) lead = high + " new HIGH incident" + (high === 1 ? "" : "s");
      else if (med >= 3) lead = med + " new MED incident" + (med === 1 ? "" : "s");
      return { tone: "alert", text: lead + since + " — Control Upgrade Recommended" };
    }
    return { tone: "calm", text: "No material escalation since baseline" };
  }

  function sectorLine() {
    const sector = $("sector");
    const id = sector && sector.value ? sector.value : templateId();
    return SECTOR_NOTE[id] || "";
  }

  function renderAiLabel() {
    const label = $("ra-ai-label");
    if (!label) return;
    const item = active();
    const assisted = state.aiSession || (item && item.aiAssisted);
    if (assisted) {
      label.textContent = "AI-Assisted Draft — Qualified Human Verification Required";
      label.className = "ai-flag";
    } else {
      label.textContent = "No Grok auto-draft used. Qualified human sign-off is still required before issue.";
      label.className = "note";
    }
  }

  function renderAudit(item) {
    const body = document.querySelector("#ra-audit-log tbody");
    if (!body) return;
    const rows = item && Array.isArray(item.audit) ? item.audit : [];
    if (!rows.length) {
      body.innerHTML = "<tr><td colspan=\"5\">No audit events yet. Save a baseline snapshot to open the trail.</td></tr>";
      return;
    }
    body.innerHTML = rows.map((row) => {
      return "<tr><td>" + escapeText(row.event) + "</td><td>" + escapeText(row.reviewer || "—")
        + "</td><td>" + escapeText(row.role || "—") + "</td><td>" + escapeText(row.status || "Recorded")
        + "</td><td>" + escapeText(formatUtc(row.at)) + "</td></tr>";
    }).join("");
  }

  function renderDiff(item) {
    const banner = $("ra-diff-banner");
    const meta = $("ra-baseline-meta");
    const upgrades = $("ra-diff-upgrades");
    const list = $("ra-diff-list");
    if (!banner || !list || !upgrades) return;
    if (!item) {
      banner.hidden = true;
      banner.textContent = "";
      if (meta) meta.textContent = "No baseline snapshot is open. Save one before departure, then check what changed.";
      upgrades.innerHTML = "";
      list.innerHTML = "";
      return;
    }
    if (meta) {
      meta.textContent = "Baseline " + formatUtc(item.sinceIso) + " · " + (item.country || "Unspecified country")
        + " · original risk " + (item.originalRiskLevel || "Low");
    }
    const last = item.lastDiff;
    if (!last) {
      banner.hidden = true;
      banner.textContent = "";
      upgrades.innerHTML = "";
      list.innerHTML = "";
      return;
    }
    banner.hidden = false;
    banner.className = "diff-banner " + (last.tone === "alert" ? "alert" : "calm");
    banner.textContent = last.banner || "";
    const lines = []
      .concat(Array.isArray(last.upgrades) ? last.upgrades : [])
      .concat(last.escalated && sectorLine() ? [sectorLine()] : []);
    upgrades.innerHTML = lines.length
      ? "<h3>Controls to upgrade</h3><ul>" + lines.map((line) => "<li>" + escapeText(line) + "</li>").join("") + "</ul>"
      : "";
    const incidents = Array.isArray(last.incidents) ? last.incidents : [];
    list.innerHTML = incidents.map((incident, index) => {
      const source = incident.source
        ? (String(incident.source).startsWith("http")
          ? ' · <a href="' + escapeText(incident.source) + '" target="_blank" rel="noopener noreferrer">Source</a>'
          : " · " + escapeText(incident.source))
        : "";
      return "<article class=\"diff-item\"><p><span class=\"band " + bandClass(incident.severity) + "\">"
        + escapeText(incident.severity || "") + "</span> " + escapeText(incident.category || "")
        + " · " + escapeText(incident.confidence_tier || "") + "</p><h3>" + escapeText(incident.title)
        + "</h3><p class=\"note\">" + escapeText(formatUtc(incident.timestamp)) + source + "</p>"
        + "<p class=\"no-print\"><button type=\"button\" class=\"ghost\" data-add-incident=\"" + index
        + "\">Add to Risk Matrix</button></p></article>";
    }).join("");
  }

  function bandClass(severity) {
    const key = String(severity || "").toUpperCase();
    if (key === "HIGH") return "High";
    if (key === "MED" || key === "MEDIUM") return "Medium";
    if (key === "LOW") return "Low";
    return "Medium";
  }

  function renderList() {
    const select = $("ra-snapshot-list");
    if (!select) return;
    const current = select.value;
    select.innerHTML = "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = state.items.length ? "Select a saved assessment" : "No saved assessments yet";
    select.appendChild(placeholder);
    state.items.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.name + " · " + shortUtc(item.sinceIso);
      select.appendChild(option);
    });
    select.value = state.items.some((item) => item.id === (state.activeId || current)) ? (state.activeId || current) : "";
  }

  function render() {
    renderList();
    renderAiLabel();
    const item = active();
    renderAudit(item);
    renderDiff(item);
    const name = $("ra-snapshot-name");
    if (name && item && document.activeElement !== name) name.value = item.name;
    const reviewer = $("ra-reviewer-name");
    const role = $("ra-reviewer-role");
    const status = $("ra-signoff-status");
    if (item && reviewer && document.activeElement !== reviewer) reviewer.value = item.reviewerName || "";
    if (item && role && document.activeElement !== role) role.value = item.reviewerRole || "";
    if (item && status && item.signOffStatus && SIGN_OFF.includes(item.signOffStatus)) status.value = item.signOffStatus;
  }

  function defaultName() {
    const typed = fieldValue("ra-snapshot-name").trim();
    if (typed) return typed.slice(0, 80);
    const country = countryName() || "Assessment";
    return country + " · " + shortUtc(nowIso());
  }

  async function captureIds(country, region, level) {
    const since = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();
    const data = await postDiff({
      country: country,
      region: region,
      since_iso: since,
      original_incident_ids: [],
      original_risk_level: level
    });
    return (data.new_incidents || []).map((item) => item && item.id).filter(Boolean);
  }

  async function saveSnapshot() {
    const note = $("ra-snapshot-note");
    const name = defaultName();
    const country = countryName();
    const region = regionName();
    const level = inherentLevel();
    const form = readForm();
    const sinceIso = nowIso();
    if (note) note.textContent = "Saving the baseline on this device…";
    let ids = [];
    let idNote = "";
    try {
      ids = await captureIds(country, region, level);
    } catch (err) {
      idNote = " Live incident ids could not be captured; the baseline time will still apply.";
    }
    let item = state.items.find((entry) => entry.name.toLowerCase() === name.toLowerCase());
    const created = !item;
    if (!item) {
      item = {
        id: "ra-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 7),
        name: name,
        createdAt: sinceIso,
        sinceIso: sinceIso,
        originalIncidentIds: ids,
        originalRiskLevel: level,
        aiAssisted: state.aiSession,
        country: country,
        region: region,
        reviewerName: fieldValue("ra-reviewer-name"),
        reviewerRole: fieldValue("ra-reviewer-role"),
        signOffStatus: fieldValue("ra-signoff-status") || "Approved",
        audit: [auditRow("Created")],
        lastDiff: null,
        form: form
      };
      state.items.unshift(item);
    } else {
      item.name = name;
      item.form = form;
      item.country = country;
      item.region = region;
      item.aiAssisted = item.aiAssisted || state.aiSession;
      item.reviewerName = fieldValue("ra-reviewer-name");
      item.reviewerRole = fieldValue("ra-reviewer-role");
      if (!item.originalIncidentIds || !item.originalIncidentIds.length) item.originalIncidentIds = ids;
      if (!item.sinceIso) item.sinceIso = sinceIso;
      if (!item.originalRiskLevel) item.originalRiskLevel = level;
    }
    state.activeId = item.id;
    state.items = state.items.slice(0, 30);
    writeStore();
    render();
    scheduleOsintRead();
    if (note) {
      note.textContent = created
        ? "Baseline saved in this browser." + idNote
        : "Assessment details updated. The original baseline time is unchanged." + idNote;
    }
  }

  function openSnapshot(id) {
    const item = state.items.find((entry) => entry.id === id);
    if (!item) return;
    state.activeId = item.id;
    state.aiSession = !!item.aiAssisted;
    writeStore();
    applyForm(item.form);
    render();
    scheduleOsintRead();
    const note = $("ra-snapshot-note");
    if (note) note.textContent = "Opened “" + item.name + "”. Personal fields stayed on this device.";
  }

  function deleteSnapshot() {
    const item = active();
    if (!item) return;
    state.items = state.items.filter((entry) => entry.id !== item.id);
    state.activeId = "";
    writeStore();
    render();
    const note = $("ra-snapshot-note");
    if (note) note.textContent = "Deleted “" + item.name + "” from this browser.";
  }

  async function checkChanged() {
    if (state.checking) return;
    const item = active();
    const banner = $("ra-diff-banner");
    if (!item) {
      if (banner) {
        banner.hidden = false;
        banner.className = "diff-banner alert";
        banner.textContent = "Save a baseline snapshot first. The check compares live OSINT with that saved time.";
      }
      return;
    }
    state.checking = true;
    const button = $("ra-check-changed");
    if (button) button.disabled = true;
    try {
      const data = await postDiff({
        country: item.country || countryName(),
        region: item.region || regionName(),
        since_iso: item.sinceIso,
        original_incident_ids: item.originalIncidentIds || [],
        original_risk_level: item.originalRiskLevel || "Low"
      });
      const copy = bannerCopy(data, item.sinceIso);
      item.lastDiff = {
        banner: copy.text,
        tone: copy.tone,
        escalated: !!data.escalated,
        checkedAt: data.checked_at_utc || nowIso(),
        upgrades: Array.isArray(data.recommended_control_upgrades) ? data.recommended_control_upgrades : [],
        incidents: Array.isArray(data.new_incidents) ? data.new_incidents.map(publicIncident) : []
      };
      item.audit = Array.isArray(item.audit) ? item.audit : [];
      item.audit.push(auditRow("Diff Checked", {
        status: data.escalated ? "Escalation" : "No material escalation",
        at: data.checked_at_utc || nowIso()
      }));
      if (item.audit.length > 80) item.audit = item.audit.slice(-80);
      writeStore();
      render();
      const host = $("ra-change-record");
      if (host && host.scrollIntoView) host.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      if (banner) {
        banner.hidden = false;
        banner.className = "diff-banner alert";
        banner.textContent = "The change check could not be completed. Your assessment stayed on this device.";
      }
    } finally {
      state.checking = false;
      if (button) button.disabled = false;
    }
  }

  function publicIncident(incident) {
    return {
      id: incident.id || "",
      title: incident.title || "",
      category: incident.category || "",
      severity: incident.severity || "",
      confidence_tier: incident.confidence_tier || "",
      timestamp: incident.timestamp || "",
      source: incident.source || ""
    };
  }

  function controlText(incident, upgrades) {
    const category = String(incident.category || "").toLowerCase();
    const match = (upgrades || []).find((line) => {
      const text = String(line).toLowerCase();
      return category && text.includes(category.split("&")[0].trim().slice(0, 18));
    });
    if (match) return match;
    if (String(incident.severity).toUpperCase() === "HIGH") {
      return "New high-severity OSINT since the baseline. Raise this hazard and name an owner before sign-off.";
    }
    return "Review this new incident and update the residual score if the control no longer holds.";
  }

  function addToMatrix(index) {
    const item = active();
    const incident = item && item.lastDiff && item.lastDiff.incidents ? item.lastDiff.incidents[index] : null;
    const add = $("add-row");
    if (!incident || !add) return;
    add.click();
    const rows = document.querySelectorAll("#matrix tbody tr");
    const tr = rows[rows.length - 1];
    if (!tr) return;
    const threat = tr.querySelector('[data-field="threat"]');
    const control = tr.querySelector('[data-field="control"]');
    const likelihood = tr.querySelector('[data-field="likelihood"]');
    const impact = tr.querySelector('[data-field="impact"]');
    if (threat) threat.value = "OSINT update: " + incident.title;
    if (control) control.value = controlText(incident, item.lastDiff.upgrades);
    const score = String(incident.severity).toUpperCase() === "HIGH" ? "4" : "3";
    if (likelihood) likelihood.value = score;
    if (impact) impact.value = score;
    const matrix = $("matrix");
    if (matrix) matrix.dispatchEvent(new Event("input", { bubbles: true }));
    if (item.form) item.form = readForm();
    writeStore();
  }

  function recordSignOff() {
    const item = active();
    const note = $("ra-signoff-note");
    if (!item) {
      if (note) note.textContent = "Save a baseline snapshot before sign-off so the audit trail has a home.";
      return;
    }
    const reviewer = fieldValue("ra-reviewer-name").trim();
    const role = fieldValue("ra-reviewer-role").trim();
    const status = SIGN_OFF.includes(fieldValue("ra-signoff-status")) ? fieldValue("ra-signoff-status") : "Approved";
    if (!reviewer || !role) {
      if (note) note.textContent = "Enter the reviewer name and qualification before sign-off. Both stay on this device.";
      return;
    }
    item.reviewerName = reviewer;
    item.reviewerRole = role;
    item.signOffStatus = status;
    item.aiAssisted = item.aiAssisted || state.aiSession;
    item.audit = Array.isArray(item.audit) ? item.audit : [];
    item.audit.push(auditRow("Human Reviewer Signed Off", {
      reviewer: reviewer,
      role: role,
      status: status
    }));
    if (item.form) item.form = readForm();
    writeStore();
    render();
    if (note) note.textContent = "Sign-off recorded at " + formatUtc(item.audit[item.audit.length - 1].at) + ". It prints with the PDF.";
  }

  function noteOsint(detail) {
    const item = active();
    if (!item) return;
    const text = String(detail || "").replace(/\s+/g, " ").trim();
    if (!text || text.indexOf("Loading") === 0 || text.indexOf("Select a country") === 0) return;
    item.audit = Array.isArray(item.audit) ? item.audit : [];
    const recent = [...item.audit].reverse().find((row) => row.event === "OSINT Refreshed");
    const age = recent ? Date.now() - new Date(recent.at).getTime() : Infinity;
    if (recent && age < 60000) {
      const prior = recent.detail || "";
      if (prior === text || text.indexOf("Live OSINT refresh") === 0) return;
      if (prior.indexOf("Live OSINT refresh") === 0) {
        recent.detail = text.slice(0, 180);
        recent.at = nowIso();
        writeStore();
        renderAudit(item);
      }
      return;
    }
    const entry = auditRow("OSINT Refreshed", { status: "Recorded" });
    entry.detail = text.slice(0, 180);
    item.audit.push(entry);
    writeStore();
    renderAudit(item);
  }

  function scheduleOsintRead() {
    const country = countryName();
    window.setTimeout(() => {
      const text = String((($("incident-note") || {}).textContent) || "").replace(/\s+/g, " ").trim();
      if (text && text.indexOf("Loading") !== 0 && text.indexOf("Select a country") !== 0) noteOsint(text);
      else noteOsint("Live OSINT refresh for " + (country || "the selected country"));
    }, 1200);
  }

  function watchOsint() {
    const arm = () => scheduleOsintRead();
    let timer = 0;
    const watch = (node) => {
      if (!node || !window.MutationObserver) return;
      const observer = new MutationObserver(() => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => noteOsint(node.textContent || ""), 600);
      });
      observer.observe(node, { childList: true, subtree: true, characterData: true });
    };
    watch($("incident-note"));
    watch($("osint-pull"));
    ["country", "city", "pull-osint"].forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener(id === "pull-osint" ? "click" : "input", arm);
      if (id === "country") el.addEventListener("change", arm);
    });
    arm();
  }

  function clearStoredCorridor() {
    ["georisk.ra.v2", "georisk.ra.v1"].forEach((key) => {
      try {
        const saved = JSON.parse(localStorage.getItem(key) || "null");
        if (!saved || typeof saved !== "object") return;
        saved.corridorSummary = "";
        saved.corridorMeta = "";
        localStorage.setItem(key, JSON.stringify(saved));
      } catch (err) {}
    });
  }

  function clearLocalBrowserData() {
    const confirmed = window.confirm("Clear GeoRiskGlobal baselines, the reviewer name and qualification, and the pasted corridor brief from this browser?");
    if (!confirmed) return;
    ["georisk.ra.baselines.v1", "georisk.ra.baseline-active.v1", "georisk.corridor.v1"].forEach((key) => {
      try { localStorage.removeItem(key); } catch (err) {}
    });
    clearStoredCorridor();
    state.items = [];
    state.activeId = "";
    state.aiSession = false;
    ["ra-reviewer-name", "ra-reviewer-role", "ra-snapshot-name"].forEach((id) => {
      const field = $(id);
      if (field) field.value = "";
    });
    const pasted = $("corridor-pasted");
    const pastedBody = $("corridor-pasted-body");
    const pastedMeta = $("corridor-pasted-meta");
    const offer = $("corridor-import");
    if (pastedBody) pastedBody.textContent = "";
    if (pastedMeta) pastedMeta.textContent = "";
    if (pasted) pasted.hidden = true;
    if (offer) offer.hidden = true;
    writeStore();
    render();
    const note = $("ra-snapshot-note");
    if (note) note.textContent = "Baselines, reviewer details, and the pasted corridor brief were cleared from this browser.";
  }

  function onAutoDraft() {
    state.aiSession = true;
    const item = active();
    if (item) {
      item.aiAssisted = true;
      writeStore();
    }
    renderAiLabel();
  }

  function bind() {
    const save = $("ra-save-snapshot");
    const open = $("ra-open-snapshot");
    const remove = $("ra-delete-snapshot");
    const clearBrowser = $("ra-clear-browser");
    const check = $("ra-check-changed");
    const sign = $("ra-signoff");
    const list = $("ra-snapshot-list");
    const draft = $("autodraft");
    if (save) save.addEventListener("click", () => { saveSnapshot(); });
    if (open) open.addEventListener("click", () => { openSnapshot(fieldValue("ra-snapshot-list")); });
    if (remove) remove.addEventListener("click", () => {
      if (active() && window.confirm("Delete this saved assessment from this browser?")) deleteSnapshot();
    });
    if (clearBrowser) clearBrowser.addEventListener("click", clearLocalBrowserData);
    if (check) check.addEventListener("click", () => { checkChanged(); });
    if (sign) sign.addEventListener("click", recordSignOff);
    if (list) list.addEventListener("change", () => {
      if (list.value) openSnapshot(list.value);
    });
    if (draft) draft.addEventListener("click", onAutoDraft);
    const results = $("ra-diff-list");
    if (results) results.addEventListener("click", (event) => {
      const target = event.target;
      const button = target && target.closest ? target.closest("[data-add-incident]") : null;
      if (!button) return;
      addToMatrix(Number(button.getAttribute("data-add-incident")));
    });
    ["ra-reviewer-name", "ra-reviewer-role", "ra-signoff-status"].forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener("input", () => {
        const item = active();
        if (!item) return;
        item.reviewerName = fieldValue("ra-reviewer-name");
        item.reviewerRole = fieldValue("ra-reviewer-role");
        item.signOffStatus = fieldValue("ra-signoff-status");
        writeStore();
      });
    });
  }

  function init() {
    state.items = readStore().filter((item) => item && item.id && item.name);
    try { state.activeId = localStorage.getItem(ACTIVE_KEY) || ""; } catch (err) { state.activeId = ""; }
    if (!state.items.some((item) => item.id === state.activeId)) state.activeId = "";
    bind();
    watchOsint();
    render();
    if (state.activeId) openSnapshot(state.activeId);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
