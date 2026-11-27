/* 5×5 Likelihood × Impact matrix and the six-column mitigation table.

   Scores stay in the browser. This file does not call Anthropic or xAI,
   and it does not read traveller names, medical notes, or contact details.
*/
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (typeof root !== "undefined") root.GeoRiskMatrix = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  var LIKELIHOOD = [
    { score: 1, label: "Rare" },
    { score: 2, label: "Unlikely" },
    { score: 3, label: "Possible" },
    { score: 4, label: "Likely" },
    { score: 5, label: "Almost Certain" }
  ];
  var IMPACT = [
    { score: 1, label: "Negligible" },
    { score: 2, label: "Minor" },
    { score: 3, label: "Moderate" },
    { score: 4, label: "Major" },
    { score: 5, label: "Catastrophic" }
  ];
  var COLUMNS = [
    "Threat Category",
    "Inherent Rating",
    "Country / Operational Context",
    "Mandatory Mitigation Controls",
    "Residual Rating",
    "Action Owner"
  ];
  var OWNERS = ["Security Advisor", "Team Leader", "Driver/Fixer", "Ops Desk"];
  var BANDS = [
    { name: "Low", css: "Low", floor: 1, ceiling: 4 },
    { name: "Moderate", css: "Moderate", floor: 5, ceiling: 9 },
    { name: "High", css: "High", floor: 10, ceiling: 14 },
    { name: "Very High", css: "VeryHigh", floor: 15, ceiling: 19 },
    { name: "Severe", css: "Severe", floor: 20, ceiling: 25 }
  ];

  var CATEGORIES = [
    {
      id: "conflict",
      label: "Conflict/Armed Clashes",
      legacy: ["Conflict/War"],
      keywords: ["conflict", "armed", "clash", "crossfire", "shell", "artillery", "airstrike", "front line"],
      owner: "Security Advisor",
      context: "Armed clashes, indirect fire, and shifting control lines set the inherent score. Hold a stand-off from the front and do not cross ground that changed hands recently.",
      controls: "Pre-deployment: confirm the governorate control picture and agree a written abort line with the authorising manager. Movement: stay on the agreed route and hold a stand-off from clashes. Accommodation: use a site with a shelter plan and a second exit. Comms: 2-tap check-in at departure, arrival, and last light, with the ops desk escalating at +30, +60, and +120 minutes overdue. Contingency: shelter in place under indirect fire, and hibernate if the movement corridor closes."
    },
    {
      id: "terror",
      label: "Terrorism/Kidnap",
      legacy: ["Terrorism", "Kidnap/Detention"],
      keywords: ["terror", "terrorism", "kidnap", "hostage", "bombing", "ied"],
      owner: "Security Advisor",
      context: "Terrorism and kidnap risk turns on capability, intent, and how predictable the pattern of life is. Symbolic sites and a fixed hotel-to-meeting routine raise the score.",
      controls: "Pre-deployment: agree a no-go list and a kidnap protocol before tickets are issued. Movement: low-profile vehicle, varied timing, and no debate at a checkpoint. Accommodation: a vetted site away from government or security targets. Comms: the ops desk owns the 2-tap ladder (+30 / +60 / +120 minutes) and any duress word stays on this device only. Contingency: Conditional-Go if a fresh alert touches the route; No-Go if the kidnap protocol or the tracker plan is missing."
    },
    {
      id: "crime",
      label: "Crime/Road Safety",
      legacy: ["Crime", "Road/Airspace"],
      keywords: ["crime", "robbery", "carjack", "theft", "road", "traffic", "collision", "airspace", "notam", "airport"],
      owner: "Driver/Fixer",
      context: "Crime, road traffic, and the air picture share this row. Read the NOTAM and any EASA Conflict Zone Information Bulletin before a flight, and treat an unplanned roadblock as a stop, not a gap to force.",
      controls: "Pre-deployment: confirm vehicle condition, a daylight movement window, and the current NOTAM and EASA CZIB for the flight information region. Movement: the driver or fixer owns the route, with no night transit on an unassessed road and nothing of value on display. Accommodation: secure parking and a lit entrance. Comms: check in at each leg. Contingency: stop and reroute at a crowd or an unofficial roadblock. No-Go if the airspace bulletin does not allow the planned routing."
    },
    {
      id: "unrest",
      label: "Civil Unrest/Detention",
      legacy: ["Civil Unrest"],
      keywords: ["unrest", "protest", "riot", "demonstration", "detention", "arrest", "crowd"],
      owner: "Team Leader",
      context: "Civil unrest and detention risk follows gatherings, checkpoints, and document problems. Do not enter a crowd, and do not record a legal name in this assessment.",
      controls: "Pre-deployment: map likely gathering sites and confirm documents and accreditation. Movement: keep a stand-off from a crowd and leave if the density rises. Accommodation: avoid a ground-floor room facing a gathering site. Comms: the ops desk is told before the last checkpoint. Contingency: if detained, remain calm, show the agreed documents, and let the ops desk run the response. Shelter in place if a protest blocks the only exit."
    },
    {
      id: "medical",
      label: "Medical/Evacuation",
      legacy: ["Health/Environmental"],
      keywords: ["medical", "medevac", "health", "outbreak", "trauma", "hospital", "ambulance"],
      owner: "Team Leader",
      context: "In a hostile environment the golden hour is often spent reaching a facility that can actually take a trauma patient. Name the nearest suitable hospital and the medevac hub before departure.",
      controls: "Pre-deployment: name a trauma centre, a medevac hub, and the assistance provider, and confirm medical and war-risk cover where the country requires it. Movement: a trauma kit travels with the team. Accommodation: know the route from the lodging to the hospital in daylight. Comms: a medical trigger is a 2-tap plus a call to the ops desk, then the +30 / +60 / +120 minute ladder if contact is lost. Contingency: No-Go if medevac cover or a receiving hospital is not confirmed."
    },
    {
      id: "digital",
      label: "Digital/Comms/OPSEC",
      legacy: ["Digital/InfoSec"],
      keywords: ["digital", "cyber", "opsec", "comms", "surveillance", "blackout", "outage"],
      owner: "Ops Desk",
      context: "Communications and operational security fail quietly: a blackout, a seized device, or a name typed into a shared file. Staff detail, medical notes, and passport references stay in this browser.",
      controls: "Pre-deployment: encrypted devices and a clean handset, with no traveller names, medical notes, or passport details in a shared file. Movement: location sharing only with the ops desk. Accommodation: no sensitive discussion in a public area. Comms: an encrypted primary channel and a satellite or inReach alternate. Contingency: if the primary channel is silent for 30 minutes, switch to the alternate; at +120 minutes the ops desk starts the missing-person protocol."
    },
    {
      id: "environment",
      label: "Environmental/ERW",
      legacy: [],
      keywords: ["erw", "uxo", "ordnance", "unexploded", "landmine", "flood", "earthquake", "wildfire", "landslide"],
      owner: "Security Advisor",
      context: "Explosive remnants and environmental hazards are scored on their own, even when armed clashes are the headline. Stay on hard-top roads in former fighting ground.",
      controls: "Pre-deployment: check the seasonal weather, flood, and explosive-remnants picture for the route. Movement: stay on the carriageway, do not leave the road in a former conflict area, and treat an unusual object as ERW. Accommodation: an upper floor where flood is the hazard. Comms: a weather abort is agreed the evening before. Contingency: hibernate on a severe weather warning. No-Go if the route crosses unmarked ERW ground."
    }
  ];

  function band(score) {
    var n = Number(score) || 0;
    if (n <= 4) return BANDS[0];
    if (n <= 9) return BANDS[1];
    if (n <= 14) return BANDS[2];
    if (n <= 19) return BANDS[3];
    return BANDS[4];
  }

  function clampScore(value) {
    var n = Number(value);
    if (!isFinite(n)) return 1;
    n = Math.round(n);
    if (n < 1) return 1;
    if (n > 5) return 5;
    return n;
  }

  function cells() {
    var grid = [];
    var likelihood = 5;
    while (likelihood >= 1) {
      var row = [];
      var impact = 1;
      while (impact <= 5) {
        var score = likelihood * impact;
        var info = band(score);
        row.push({
          likelihood: likelihood,
          impact: impact,
          score: score,
          band: info.name,
          css: info.css
        });
        impact += 1;
      }
      grid.push(row);
      likelihood -= 1;
    }
    return grid;
  }

  function overall(rows, lKey, iKey) {
    var best = null;
    (rows || []).forEach(function (row) {
      if (!row) return;
      var likelihood = clampScore(row[lKey]);
      var impact = clampScore(row[iKey]);
      var score = likelihood * impact;
      var better = !best || score > best.score || (score === best.score && (impact > best.impact || (impact === best.impact && likelihood > best.likelihood)));
      if (better) {
        var info = band(score);
        best = { likelihood: likelihood, impact: impact, score: score, band: info.name, css: info.css };
      }
    });
    return best;
  }

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"]/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;" }[ch];
    });
  }

  function hasWord(blob, word) {
    var escaped = String(word || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    if (!escaped) return false;
    return new RegExp("(?:^|[^a-z0-9])" + escaped + "s?(?:$|[^a-z0-9])", "i").test(blob);
  }

  function mentions(spec, blob) {
    var text = String(blob || "").toLowerCase();
    var hit = false;
    (spec.keywords || []).forEach(function (word) {
      if (!hit && hasWord(text, word)) hit = true;
    });
    (spec.legacy || []).forEach(function (name) {
      if (!hit && text.indexOf(String(name).toLowerCase()) !== -1) hit = true;
    });
    return hit;
  }

  function categoryByLabel(label) {
    var text = String(label || "").trim().toLowerCase();
    if (!text) return null;
    var found = null;
    CATEGORIES.forEach(function (spec) {
      if (!found && spec.label.toLowerCase() === text) found = spec;
    });
    if (found) return found;
    CATEGORIES.forEach(function (spec) {
      if (found) return;
      (spec.legacy || []).forEach(function (name) {
        if (!found && String(name).toLowerCase() === text) found = spec;
      });
    });
    if (found) return found;
    CATEGORIES.forEach(function (spec) {
      if (!found && mentions(spec, text)) found = spec;
    });
    return found;
  }

  function hazardBlob(hazard) {
    if (!hazard) return "";
    return [hazard.threat, hazard.category, hazard.mitigation, hazard.control].join(" ");
  }

  function erwSignal(hazards, extra) {
    var spec = null;
    CATEGORIES.forEach(function (item) {
      if (item.id === "environment") spec = item;
    });
    var blob = (hazards || []).map(hazardBlob).join(" ") + " " + (extra || "");
    return spec ? mentions(spec, blob) : false;
  }

  function contextLine(spec, country, region, incidents, mitigation) {
    var place = country || "the Area of Operations";
    var where = region && region !== "all" && region.toLowerCase() !== String(place).toLowerCase()
      ? place + " (" + region + ")"
      : place;
    var matched = (incidents || []).filter(function (item) {
      var blob = [item && item.title, item && item.summary, item && item.category, item && item.area].join(" ");
      return mentions(spec, blob);
    });
    var live;
    if (matched.length) {
      var titles = matched.slice(0, 2).map(function (item) {
        var area = item && item.area ? " (" + item.area + ")" : "";
        return ((item && item.title) || "Incident") + area;
      });
      live = "Live 7-day OSINT for " + place + " records " + matched.length + " relevant item" + (matched.length === 1 ? "" : "s") + ": " + titles.join("; ") + ".";
    } else {
      live = "The live 7-day OSINT feed has no verified item in this category for " + place + ". Baseline monitoring remains in force.";
    }
    var local = mitigation ? "Country baseline: " + mitigation : "";
    return [where + ".", spec.context, local, live].filter(Boolean).join(" ");
  }

  function rowsFromHazards(hazards, options) {
    options = options || {};
    var country = options.country || "";
    var region = options.region || "";
    var incidents = options.incidents || [];
    var extra = options.situation || "";
    var list = Array.isArray(hazards) ? hazards : [];
    var buckets = {};
    CATEGORIES.forEach(function (spec) { buckets[spec.id] = []; });
    list.forEach(function (hazard) {
      var spec = categoryByLabel((hazard && (hazard.threat || hazard.category)) || "");
      if (!spec) return;
      buckets[spec.id].push(hazard);
    });
    return CATEGORIES.map(function (spec) {
      var group = buckets[spec.id];
      var best = null;
      group.forEach(function (hazard) {
        var likelihood = clampScore(hazard.likelihood || 2);
        var impact = clampScore(hazard.impact || 3);
        var score = likelihood * impact;
        if (!best || score > best.score) best = { hazard: hazard, likelihood: likelihood, impact: impact, score: score };
      });
      var source = best ? best.hazard : null;
      var likelihood = best ? best.likelihood : 2;
      var impact = best ? best.impact : 3;
      if (spec.id === "environment" && !source && erwSignal(list, extra)) {
        likelihood = 3;
        impact = 4;
      }
      var mitigation = source ? String(source.mitigation || source.control || "").trim() : "";
      var controls = spec.controls;
      if (mitigation && controls.indexOf(mitigation) === -1) controls = mitigation + " " + controls;
      return {
        threat: spec.label,
        likelihood: likelihood,
        impact: impact,
        control: controls,
        controlId: "",
        context: contextLine(spec, country, region, incidents, mitigation),
        baselineNote: mitigation,
        contextEdited: false,
        owner: spec.owner,
        residualL: source ? clampScore(source.residual_likelihood || source.residualL || 2) : 2,
        residualI: source ? clampScore(source.residual_impact || source.residualI || 2) : 2
      };
    });
  }

  function renderGrid(rows) {
    var inherent = overall(rows, "likelihood", "impact");
    var residual = overall(rows, "residualL", "residualI");
    var head = "<tr><th scope=\"col\">Likelihood × Impact</th>" + IMPACT.map(function (item) {
      return "<th scope=\"col\">" + item.score + " " + escapeHtml(item.label) + "</th>";
    }).join("") + "</tr>";
    var body = cells().map(function (line) {
      var label = LIKELIHOOD.filter(function (item) { return item.score === line[0].likelihood; })[0];
      var cellsHtml = line.map(function (cell) {
        var inherentHit = inherent && cell.likelihood === inherent.likelihood && cell.impact === inherent.impact;
        var residualHit = residual && cell.likelihood === residual.likelihood && cell.impact === residual.impact;
        var classes = ["risk-cell", cell.css];
        var marks = [];
        if (inherentHit) {
          classes.push("is-inherent");
          marks.push("Inherent");
        }
        if (residualHit) {
          classes.push("is-residual");
          marks.push("Residual");
        }
        var mark = marks.length ? "<span class=\"mark\">" + marks.join(" · ") + "</span>" : "";
        return "<td class=\"" + classes.join(" ") + "\" data-likelihood=\"" + cell.likelihood + "\" data-impact=\"" + cell.impact + "\" data-score=\"" + cell.score + "\" data-band=\"" + escapeHtml(cell.band) + "\">" + cell.score + mark + "</td>";
      }).join("");
      return "<tr><th class=\"risk-axis\" scope=\"row\">" + line[0].likelihood + " " + escapeHtml(label.label) + "</th>" + cellsHtml + "</tr>";
    }).join("");
    var legend = BANDS.map(function (item) {
      var live = (inherent && inherent.band === item.name) || (residual && residual.band === item.name);
      return "<span class=\"band " + item.css + (live ? " is-live" : "") + "\">" + item.name + " " + item.floor + "–" + item.ceiling + "</span>";
    }).join(" ");
    var summary = "Add a hazard to score the matrix.";
    if (inherent && residual) {
      summary = "Overall inherent " + inherent.band + " (" + inherent.likelihood + " × " + inherent.impact + " = " + inherent.score + "). Overall residual " + residual.band + " (" + residual.likelihood + " × " + residual.impact + " = " + residual.score + ").";
    }
    return "<table class=\"risk-grid-table\"><caption>5×5 Likelihood × Impact risk rating matrix (1–25)</caption><thead>" + head + "</thead><tbody>" + body + "</tbody></table><p class=\"risk-legend\" id=\"risk-band-legend\">" + legend + "</p><p class=\"note\" id=\"risk-grid-summary\">" + escapeHtml(summary) + "</p>";
  }

  return {
    LIKELIHOOD: LIKELIHOOD,
    IMPACT: IMPACT,
    COLUMNS: COLUMNS,
    OWNERS: OWNERS,
    BANDS: BANDS,
    CATEGORIES: CATEGORIES,
    band: band,
    clampScore: clampScore,
    cells: cells,
    overall: overall,
    categoryByLabel: categoryByLabel,
    contextLine: contextLine,
    rowsFromHazards: rowsFromHazards,
    renderGrid: renderGrid
  };
});
