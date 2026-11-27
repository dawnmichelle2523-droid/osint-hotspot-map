/* Area of Operations: region and country filters, local pin, price quote.
   Rich country prose is loaded from /api/geo/catalog (no xAI call).
   The pin stores only a region and a country in this browser. */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.GeoSplit = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  var AO_KEY = "georisk.ao.v1";
  var REGIONS = [
    { id: "middle-east", label: "Middle East", lat: 28, lng: 45, zoom: 4 },
    { id: "europe", label: "Europe", lat: 50, lng: 15, zoom: 4 },
    { id: "africa", label: "Africa", lat: 2, lng: 20, zoom: 3 },
    { id: "asia-pacific", label: "Asia-Pacific", lat: 12, lng: 115, zoom: 3 },
    { id: "americas", label: "Americas", lat: 15, lng: -90, zoom: 3 },
    { id: "global", label: "Global", lat: 20, lng: 10, zoom: 2 },
    { id: "maritime", label: "Maritime", lat: 15, lng: 55, zoom: 4 }
  ];
  var REGION_TEXT = {
    "Middle East": {
      stability: "The Middle East Area of Operations mixes tightly governed Gulf capitals with active conflicts in Syria, Yemen, and parts of Iraq and the Levant.",
      actors: "State forces, Iran-aligned armed groups, Israeli and Lebanese armed actors, and Ansarallah in Yemen are the main security drivers.",
      borders: "Conflict-zone airspace restrictions cover Syria, Iraq, Iran, and Yemen. Maritime risk sits on the Red Sea and the Strait of Hormuz."
    },
    Europe: {
      stability: "Most of Europe is politically stable and commercially open. The war in Ukraine is the exception.",
      actors: "National governments, NATO and EU institutions, and the parties to the war in Ukraine are the main drivers.",
      borders: "Ukrainian and western Russian airspace is restricted. Elsewhere, commercial aviation is generally open."
    },
    Africa: {
      stability: "Africa's Area of Operations runs from stable commercial capitals to active conflicts in the Sahel, Sudan, Somalia, and eastern Congo.",
      actors: "National armies, junta authorities in parts of the Sahel, and jihadist groups are the main drivers.",
      borders: "Libya, Sudan, Somalia, and Mali have published conflict-zone airspace restrictions."
    },
    "Asia-Pacific": {
      stability: "Asia-Pacific is mostly commercially open, with sharp exceptions in Afghanistan, Myanmar, and the South China Sea.",
      actors: "National governments and the authorities in Afghanistan are the strategic drivers.",
      borders: "Afghan airspace needs a current risk acceptance. Most regional hubs are otherwise open."
    },
    Americas: {
      stability: "The United States and Canada are stable. Parts of Latin America and the Caribbean carry high crime and protest risk.",
      actors: "National governments, criminal groups, and protest movements are the main drivers.",
      borders: "US and Canadian borders are regulated. Haiti has published civil-aviation restrictions."
    },
    Global: {
      stability: "Global is the worldwide view for travel advice, disasters, and cyber notes. Pick a country to focus.",
      actors: "Government travel-advice services and disaster agencies drive this view.",
      borders: "No single border regime applies. Use the country template before a deployment."
    },
    Maritime: {
      stability: "Maritime covers sea lanes and chokepoints. Risk is highest around Hormuz, Bab el-Mandeb, and the southern Red Sea.",
      actors: "State navies, UKMTO, and attackers on commercial shipping are the drivers.",
      borders: "Air routes over these seas often sit under the same conflict-zone bulletins as the adjacent countries."
    }
  };
  var COUNTRIES = [
    ["Syria", "Middle East", 33.5138, 36.2765], ["Iraq", "Middle East", 33.3152, 44.3661], ["Iran", "Middle East", 35.6892, 51.389],
    ["Lebanon", "Middle East", 33.8938, 35.5018], ["Israel", "Middle East", 31.7683, 35.2137], ["Palestine", "Middle East", 31.9522, 35.2332],
    ["Jordan", "Middle East", 31.9454, 35.9284], ["Yemen", "Middle East", 15.3694, 44.191], ["Saudi Arabia", "Middle East", 24.7136, 46.6753],
    ["Bahrain", "Middle East", 26.0667, 50.5577], ["Kuwait", "Middle East", 29.3759, 47.9774], ["Qatar", "Middle East", 25.2854, 51.531],
    ["UAE", "Middle East", 24.4539, 54.3773], ["Oman", "Middle East", 23.588, 58.3829], ["Turkey", "Middle East", 39.9334, 32.8597],
    ["Egypt", "Middle East", 30.0444, 31.2357], ["United Kingdom", "Europe", 51.5074, -0.1278], ["Ireland", "Europe", 53.3498, -6.2603],
    ["France", "Europe", 48.8566, 2.3522], ["Germany", "Europe", 52.52, 13.405], ["Italy", "Europe", 41.9028, 12.4964],
    ["Spain", "Europe", 40.4168, -3.7038], ["Poland", "Europe", 52.2297, 21.0122], ["Ukraine", "Europe", 50.4501, 30.5234],
    ["Russia", "Europe", 55.7558, 37.6173], ["Sudan", "Africa", 15.5007, 32.5599], ["South Sudan", "Africa", 4.8594, 31.5713],
    ["Chad", "Africa", 12.1348, 15.0557], ["Libya", "Africa", 32.8872, 13.1913], ["Nigeria", "Africa", 9.0765, 7.3986],
    ["Somalia", "Africa", 2.0469, 45.3182], ["Kenya", "Africa", -1.2921, 36.8219], ["Ethiopia", "Africa", 9.032, 38.7469],
    ["Mali", "Africa", 12.6392, -8.0029], ["Afghanistan", "Asia-Pacific", 34.5553, 69.2075], ["Pakistan", "Asia-Pacific", 33.6844, 73.0479],
    ["India", "Asia-Pacific", 28.6139, 77.209], ["China", "Asia-Pacific", 39.9042, 116.4074], ["Japan", "Asia-Pacific", 35.6762, 139.6503],
    ["Australia", "Asia-Pacific", -33.8688, 151.2093], ["United States", "Americas", 38.9072, -77.0369], ["Canada", "Americas", 45.4215, -75.6972],
    ["Mexico", "Americas", 19.4326, -99.1332], ["Brazil", "Americas", -15.7939, -47.8828], ["Colombia", "Americas", 4.711, -74.0721],
    ["Haiti", "Americas", 18.5944, -72.3074], ["Maritime", "Maritime", 20, 38.5]
  ].map(function (row) { return { name: row[0], region: row[1], lat: row[2], lng: row[3] }; });
  var ALIASES = { usa: "United States", us: "United States", "u.s.": "United States", america: "United States", uk: "United Kingdom", britain: "United Kingdom", uae: "UAE", "united arab emirates": "UAE", turkiye: "Turkey" };
  var GULF = ["Bahrain", "Kuwait", "Qatar", "UAE", "Oman"];
  var LEGACY = {
    "Middle East": ["Syria", "Iraq", "Saudi Arabia", "Iran", "Lebanon", "Israel", "Palestine", "Jordan", "Yemen", "Sudan", "Chad", "Bahrain", "Kuwait", "Qatar", "UAE", "Oman", "Turkey", "Egypt", "Gulf States", "Maritime"],
    "United States": ["United States"], "Canada": ["Canada"],
    "South America": ["Brazil", "Colombia", "Venezuela", "Peru", "Chile", "Argentina"],
    APAC: ["Japan", "Taiwan", "China", "Philippines", "South Korea", "Indonesia", "Vietnam", "Thailand"],
    Australia: ["Australia"], Afghanistan: ["Afghanistan"],
    "Russia & Ukraine": ["Ukraine", "Russia", "Belarus"],
    "UK & Europe": ["United Kingdom", "Ireland", "France", "Germany", "Italy", "Spain", "Poland", "Ukraine", "Turkey"],
    Africa: ["Nigeria", "Kenya", "Ethiopia", "Somalia", "Sudan", "Chad", "Libya", "Mali"],
    "South Asia": ["India", "Pakistan", "Bangladesh", "Afghanistan"]
  };
  var LEGACY_MACRO = {
    "Middle East": "Middle East", "United States": "Americas", Canada: "Americas", "South America": "Americas",
    APAC: "Asia-Pacific", Australia: "Asia-Pacific", Afghanistan: "Asia-Pacific", "Russia & Ukraine": "Europe",
    "UK & Europe": "Europe", Europe: "Europe", Africa: "Africa", "South Asia": "Asia-Pacific", Global: "Global",
    Maritime: "Maritime", "Gulf States": "Middle East", "Asia-Pacific": "Asia-Pacific", Americas: "Americas"
  };
  var RATES = {
    country_base: { gbp: 149, usd: 189 }, country_extra: { gbp: 49, usd: 62 },
    region_base: { gbp: 399, usd: 499 }, region_extra: { gbp: 199, usd: 249 },
    global: { gbp: 899, usd: 1149 }, pass: { gbp: 59, usd: 75 },
    briefing: {
      monthly: { country: { gbp: 29, usd: 37 }, region: { gbp: 69, usd: 89 }, global: { gbp: 129, usd: 165 } },
      weekly: { country: { gbp: 59, usd: 75 }, region: { gbp: 129, usd: 165 }, global: { gbp: 249, usd: 315 } },
      daily: { country: { gbp: 119, usd: 149 }, region: { gbp: 249, usd: 315 }, global: { gbp: 449, usd: 569 } },
      bundle: { country: { gbp: 169, usd: 215 }, region: { gbp: 349, usd: 445 }, global: { gbp: 649, usd: 825 } }
    }
  };
  var CATEGORIES = ["Conflict/War", "Terrorism", "Crime", "Civil Unrest", "Kidnap/Detention", "Road/Airspace", "Digital/InfoSec", "Health/Environmental"];
  var catalog = null;

  function norm(value) {
    return String(value || "").toLowerCase().replace(/&/g, " and ").replace(/[^a-z0-9' ]+/g, " ").replace(/\s+/g, " ").trim();
  }
  function regionLabels() {
    return REGIONS.map(function (row) { return row.label; });
  }
  function canonicalCountry(value) {
    var text = norm(value);
    if (!text) return "";
    if (ALIASES[text]) return ALIASES[text];
    var found = "";
    COUNTRIES.forEach(function (row) { if (norm(row.name) === text) found = row.name; });
    if (found) return found;
    if (text === "gulf states") return "Gulf States";
    if (text === "maritime") return "Maritime";
    return "";
  }
  function regionOf(value) {
    var name = canonicalCountry(value) || String(value || "").trim();
    if (name === "Gulf States") return "Middle East";
    if (name === "Maritime") return "Maritime";
    var found = "";
    COUNTRIES.forEach(function (row) { if (row.name === name) found = row.region; });
    return found;
  }
  function macroRegion(value) {
    var text = String(value || "").replace(/\s+/g, " ").trim();
    if (!text || text === "all") return "";
    if (LEGACY_MACRO[text]) return LEGACY_MACRO[text];
    if (regionLabels().indexOf(text) >= 0) return text;
    return regionOf(text);
  }
  function isMacro(value) { return regionLabels().indexOf(String(value || "")) >= 0; }
  function propsOf(feature) {
    if (feature && feature.properties && typeof feature.properties === "object") return feature.properties;
    return feature || {};
  }
  function eventCountryName(props) {
    var sea = ["Red Sea / Maritime", "Strait of Hormuz / Gulf", "Maritime"];
    var fields = ["country", "region", "theatre", "location_name", "area"];
    for (var i = 0; i < fields.length; i += 1) {
      var raw = String((props && props[fields[i]]) || "").replace(/\s+/g, " ").trim();
      if (sea.indexOf(raw) >= 0) return "Maritime";
      var canon = canonicalCountry(raw);
      if (canon && canon !== "Gulf States") return canon;
    }
    return "";
  }
  function eventMacro(props) {
    var country = eventCountryName(props);
    if (country && regionOf(country)) return regionOf(country);
    return macroRegion((props && (props.theatre || props.region)) || "");
  }
  function matchesGlobal(props) {
    var feed = String((props && (props.feed_category || props.category)) || "");
    return (props && props.theatre) === "Global" || (props && props.region) === "Global" || ["Travel Advisory", "Disaster", "Seismic", "Cyber", "Natural Disaster"].indexOf(feed) >= 0;
  }
  function matchesGulf(props) {
    var country = eventCountryName(props);
    return GULF.indexOf(country) >= 0 || (props && (props.theatre === "Gulf States" || props.region === "Gulf States"));
  }
  function matchesMaritime(props) {
    if (eventCountryName(props) === "Maritime") return true;
    var blob = [props && props.region, props && props.theatre, props && props.country, props && props.category].join(" ").toLowerCase();
    return blob.indexOf("maritime") >= 0;
  }
  function matchesLegacy(props, name) {
    var members = LEGACY[name] || [];
    var country = eventCountryName(props);
    var regionName = String((props && props.region) || "");
    var theatre = String((props && props.theatre) || "");
    return members.indexOf(country) >= 0 || members.indexOf(regionName) >= 0 || theatre === name || regionName === name;
  }
  function eventMatchesScope(props, scope) {
    var name = String(scope || "").replace(/\s+/g, " ").trim();
    if (!name || name === "all") return true;
    var record = props || {};
    if (name === "Global") return matchesGlobal(record);
    if (name === "Gulf States") return matchesGulf(record);
    if (name === "Maritime") return matchesMaritime(record);
    if (LEGACY[name] && regionLabels().indexOf(name) < 0) return matchesLegacy(record, name);
    if (isMacro(name)) return eventMacro(record) === name;
    if (LEGACY[name]) return matchesLegacy(record, name);
    var country = canonicalCountry(name);
    if (country) return eventCountryName(record) === country;
    var wanted = norm(name);
    return ["country", "region", "theatre"].some(function (key) { return norm(record[key]) === wanted; });
  }
  function eventMatches(feature, region, country) {
    var props = propsOf(feature);
    if (country && country !== "all" && !eventMatchesScope(props, country)) return false;
    if (region && region !== "all" && !eventMatchesScope(props, region)) return false;
    return true;
  }
  function countriesFor(region) {
    var macro = !region || region === "all" || region === "Global" ? "" : (macroRegion(region) || "");
    var names = COUNTRIES.filter(function (row) {
      return row.name !== "Maritime" && (!macro || row.region === macro);
    }).map(function (row) { return row.name; });
    if (!macro || macro === "Middle East") names.push("Gulf States");
    if (macro === "Maritime") return ["Maritime"];
    return names;
  }
  function countryCenter(name) {
    var canon = canonicalCountry(name) || name;
    var found = null;
    COUNTRIES.forEach(function (row) { if (row.name === canon) found = [row.lat, row.lng]; });
    if (found) return found;
    if (canon === "Gulf States") return [26.2, 50.6];
    if (canon === "Maritime") return [20, 38.5];
    return null;
  }
  function regionView(name) {
    var label = isMacro(name) ? name : (macroRegion(name) || name);
    var found = null;
    REGIONS.forEach(function (row) { if (row.label === label) found = { center: [row.lat, row.lng], zoom: row.zoom }; });
    return found;
  }
  function setCatalog(payload) {
    catalog = payload && typeof payload === "object" ? payload : null;
    if (catalog && catalog.region_text) REGION_TEXT = catalog.region_text;
    return catalog;
  }
  function trendSentence(total, high, top) {
    if (total <= 0) return "No live OSINT events are stored for this Area of Operations. Baseline monitoring still applies.";
    var lead = total + " live OSINT event" + (total === 1 ? "" : "s");
    var sentence = high <= 0 ? lead + ", none at high severity." : (high === 1 ? lead + ", 1 at high severity." : lead + ", " + high + " at high severity.");
    if (top) sentence += " The busiest category is " + top + ".";
    if (high >= 3 || (total && high / total >= 0.4)) sentence += " The activity trend is elevated.";
    else if (high === 0) sentence += " The activity trend is steady.";
    else sentence += " The activity trend is mixed.";
    return sentence;
  }
  function composeSummary(events, region, country) {
    var regionName = isMacro(region) ? region : (macroRegion(region) || "");
    if (LEGACY[region] && regionLabels().indexOf(region) < 0) regionName = region;
    var countryName = country && country !== "all" ? (canonicalCountry(country) || "") : "";
    if (countryName && !regionName) regionName = regionOf(countryName) || regionName;
    var matched = [];
    (events || []).forEach(function (feature) {
      var props = propsOf(feature);
      if (!eventMatches(props, regionName, countryName)) return;
      var title = String(props.title || "").replace(/\s+/g, " ").trim();
      if (!title) return;
      var severity = String(props.severity || "medium").toLowerCase();
      if (["high", "medium", "low"].indexOf(severity) < 0) severity = "medium";
      matched.push({ title: title.slice(0, 180), severity: severity, category: String(props.feed_category || props.category || "OSINT"), timestamp: String(props.timestamp || "") });
    });
    matched.sort(function (a, b) { return a.timestamp < b.timestamp ? 1 : a.timestamp > b.timestamp ? -1 : 0; });
    var counts = { high: 0, medium: 0, low: 0 };
    var categories = {};
    matched.forEach(function (item) {
      counts[item.severity] += 1;
      categories[item.category] = (categories[item.category] || 0) + 1;
    });
    var top = "";
    Object.keys(categories).sort(function (a, b) { return categories[b] - categories[a] || (a < b ? -1 : 1); }).forEach(function (name, index) { if (index === 0) top = name; });
    var profile = catalog && catalog.profiles ? catalog.profiles[countryName] : null;
    var shown = countryName ? (regionOf(countryName) || regionName) : (macroRegion(regionName) || regionName);
    var copy = REGION_TEXT[shown] || REGION_TEXT[macroRegion(regionName)] || REGION_TEXT.Global;
    var stability, actors, borders, scope, title;
    if (countryName && profile) {
      scope = "country"; title = countryName;
      stability = profile.stability; actors = profile.actors; borders = profile.borders;
    } else if (countryName) {
      scope = "country"; title = countryName;
      stability = countryName + " uses the " + (shown || "Global") + " baseline. Confirm the current government travel advice before movement. " + (copy ? copy.stability : "");
      actors = copy ? copy.actors : "";
      borders = copy ? copy.borders : "";
    } else if (regionName) {
      scope = "region"; title = regionName;
      stability = copy ? copy.stability : "";
      actors = copy ? copy.actors : "";
      borders = copy ? copy.borders : "";
    } else {
      scope = "none"; title = "All regions"; shown = "";
      stability = "Select a region or a country to read the geopolitical summary for that Area of Operations.";
      actors = ""; borders = "";
    }
    return {
      ok: true, xai_credits: 0, scope: scope, title: title, region: shown || "", country: countryName || "",
      stability: stability, actors: actors, borders: borders,
      trend: { total: matched.length, high: counts.high, medium: counts.medium, low: counts.low, top_category: top, headlines: matched.slice(0, 3).map(function (item) { return item.title; }), sentence: trendSentence(matched.length, counts.high, top) }
    };
  }
  function countryTemplate(country) {
    var name = canonicalCountry(country) || String(country || "").trim() || "Selected country";
    if (catalog && catalog.profiles && catalog.profiles[name]) return catalog.profiles[name];
    var region = regionOf(name) || "Global";
    var copy = REGION_TEXT[region] || REGION_TEXT.Global;
    var hazards = CATEGORIES.map(function (category) {
      return { category: category, threat: category, likelihood: 2, impact: 3, mitigation: "Confirm the current picture for " + name + " before movement.", control: "Confirm the current picture for " + name + " before movement.", residual_likelihood: 2, residual_impact: 2 };
    });
    return {
      ok: true, xai_credits: 0, country: name, region: region, baseline: true,
      title: name + " baseline template", branding: "GeoRiskIntel",
      rating: { band: "MEDIUM", score: 6, scale: "1-25" }, risk_tags: ["Baseline"],
      stability: name + " uses the GeoRiskIntel " + region + " baseline. " + copy.stability,
      actors: copy.actors, borders: copy.borders,
      situation: name + " uses the GeoRiskIntel " + region + " baseline. " + copy.stability + " " + copy.actors + " " + copy.borders,
      sections: { threat: copy.stability, security: copy.actors, airspace: copy.borders, medical: "Name a hospital and a medevac hub for " + name + " before departure.", comms: "Agree an encrypted channel and a check-in cadence. Keep staff detail on this device only.", legal: "Confirm visa and local rules for " + name + "." },
      hazards: hazards,
      facilities: [{ name: "Confirm a receiving hospital", city: name, note: "Add the facility your assistance provider accepts. Do not enter a private mobile number here." }],
      references: ["UK FCDO foreign travel advice for " + name, "US State Department travel advisory for " + name, "GeoRiskIntel live 7-day OSINT feed for this country"],
      local_only_fields: ["roster", "phone", "email", "imei", "poc_contact", "staff_details", "passport", "medical_notes"],
      hra_sections: ["Header banner", "1. Purpose and outline plan", "2. Risk assessment", "3. Itinerary and logistics", "4. Medical and evacuation", "5. Contacts", "6. Consultation and sign-off", "7. Travelling staff details", "8. References, support and data protection"]
    };
  }
  function money(amount, currency) {
    var symbol = currency === "gbp" ? "£" : "$";
    return symbol + String(Math.round(amount)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  function quoteAo(mode, count, briefing, currency, billing) {
    var chosen = ["country", "region", "global", "pass"].indexOf(String(mode || "").toLowerCase()) >= 0 ? String(mode).toLowerCase() : "country";
    var cycle = String(billing || "").toLowerCase() === "annual" ? "annual" : "monthly";
    var code = String(currency || "").toLowerCase() === "usd" ? "usd" : "gbp";
    var extra = ["none", "monthly", "weekly", "daily", "bundle"].indexOf(String(briefing || "").toLowerCase()) >= 0 ? String(briefing).toLowerCase() : "none";
    var units = parseInt(count, 10);
    if (!isFinite(units)) units = 1;
    units = Math.min(30, Math.max(1, units));
    if (chosen === "global" || chosen === "pass") units = 1;
    var base = 0, suggestion = "", includes = [], scope = chosen;
    if (chosen === "country") {
      base = RATES.country_base[code] + RATES.country_extra[code] * (units - 1);
      if (units >= 4) {
        suggestion = "Four or more countries in one region: a Regional Area of Operations starts at " + money(RATES.region_base[code], code) + " a month and may cost less than adding further countries. Compare the region price before you add another country.";
        if (base >= RATES.region_base[code]) suggestion += " A single region at " + money(RATES.region_base[code], code) + " a month costs less than these " + units + " countries (" + money(base, code) + ").";
      }
    } else if (chosen === "region") {
      base = RATES.region_base[code] + RATES.region_extra[code] * (units - 1);
      if (base >= RATES.global[code]) suggestion = "Global All-Access at " + money(RATES.global[code], code) + " a month costs less than this regional total and includes every region, Okta SSO, and White-Label Studio.";
    } else if (chosen === "global") {
      base = RATES.global[code];
      includes = ["Okta SSO", "White-Label Studio", "Priority custom risk assessments"];
    } else base = RATES.pass[code];
    var briefingTotal = 0;
    if (chosen === "pass") {
      if (extra !== "none") suggestion = (suggestion ? suggestion + " " : "") + "Briefings are monthly add-ons for a subscription Area of Operations. They are not added to the 7-day pass.";
      extra = "none";
    } else if (extra !== "none") {
      var rate = RATES.briefing[extra][scope][code];
      briefingTotal = scope === "global" ? rate : rate * units;
    }
    var multiplier = cycle === "annual" && chosen !== "pass" ? 10 : 1;
    var total = (base + briefingTotal) * multiplier;
    var period = chosen === "pass" ? "a one-off" : (cycle === "annual" ? "a year" : "a month");
    var lead;
    if (chosen === "country") lead = "Country-by-country Area of Operations for " + units + " " + (units === 1 ? "country" : "countries") + " is " + money(base * multiplier, code) + " " + period + ".";
    else if (chosen === "region") lead = "Region-by-region Area of Operations for " + units + " " + (units === 1 ? "region" : "regions") + " is " + money(base * multiplier, code) + " " + period + ".";
    else if (chosen === "global") lead = "Global All-Access is " + money(base * multiplier, code) + " " + period + ". It includes Okta SSO, White-Label Studio, and priority custom risk assessments.";
    else lead = "Single-country 7-day pass is " + money(base, code) + ", a one-off for a short trip, school trip, or single assignment.";
    if (briefingTotal) {
      var names = { monthly: "Monthly briefing", weekly: "Weekly briefing", daily: "Daily briefing", bundle: "All-three briefings bundle" };
      lead += " " + names[extra] + " adds " + money(briefingTotal * multiplier, code) + ". Total " + money(total, code) + " " + period + ".";
    } else lead += " Total " + money(total, code) + " " + period + ".";
    return {
      ok: true, xai_credits: 0, mode: chosen, count: units, briefing: extra, currency: code,
      billing: chosen === "pass" ? "one-off" : cycle, base: base * multiplier, briefing_total: briefingTotal * multiplier,
      total: total, total_label: money(total, code) + (chosen === "pass" ? " one-off" : (cycle === "annual" ? "/yr" : "/mo")),
      suggestion: suggestion, summary: lead, includes: includes
    };
  }
  function aoLabel(ao) {
    var region = ao && ao.region && ao.region !== "all" ? ao.region : "";
    var country = ao && ao.country && ao.country !== "all" ? ao.country : "";
    if (region && country) return region + " · " + country;
    return region || country || "All regions";
  }
  function readAo(storage) {
    if (!storage) return null;
    try {
      var raw = JSON.parse(storage.getItem(AO_KEY) || "null");
      if (!raw || typeof raw !== "object") return null;
      var region = raw.region === "all" ? "all" : (LEGACY[raw.region] && regionLabels().indexOf(raw.region) < 0 ? raw.region : (macroRegion(raw.region) || "all"));
      var country = !raw.country || raw.country === "all" ? "all" : (canonicalCountry(raw.country) || (raw.country === "Gulf States" || raw.country === "Maritime" ? raw.country : "all"));
      if (region === "all" && country === "all") return null;
      return { region: region, country: country };
    } catch (err) { return null; }
  }
  function writeAo(storage, ao) {
    var region = ao && LEGACY[ao.region] && regionLabels().indexOf(ao.region) < 0 ? ao.region : (macroRegion(ao && ao.region) || "all");
    var country = "all";
    if (ao && ao.country && ao.country !== "all") country = canonicalCountry(ao.country) || (ao.country === "Gulf States" || ao.country === "Maritime" ? ao.country : "all");
    var payload = { region: region || "all", country: country || "all" };
    storage.setItem(AO_KEY, JSON.stringify(payload));
    return payload;
  }
  function clearAo(storage) { try { storage.removeItem(AO_KEY); } catch (err) {} }
  function aoFromQuery(params, stored) {
    params = params || {};
    var rawRegion = String(params.region || "").replace(/\s+/g, " ").trim();
    var rawCountry = String(params.country || "").replace(/\s+/g, " ").trim();
    var governorate = String(params.governorate || "").replace(/\s+/g, " ").trim();
    if (rawRegion || rawCountry || governorate) {
      var country = governorate ? "Syria" : (rawCountry ? (canonicalCountry(rawCountry) || "") : "");
      var region = "all";
      if (LEGACY[rawRegion] && regionLabels().indexOf(rawRegion) < 0) region = rawRegion;
      else if (rawRegion) region = macroRegion(rawRegion) || "all";
      if (country) {
        var implied = regionOf(country);
        if (region === "all") region = implied || "all";
        else if (implied && regionLabels().indexOf(region) >= 0 && macroRegion(region) !== implied) region = implied;
        else if (LEGACY[region] && (LEGACY[region] || []).indexOf(country) < 0) region = implied || region;
      }
      if (governorate) { country = "Syria"; if (region === "all") region = "Middle East"; }
      return { region: region || "all", country: country || "all", source: "url" };
    }
    if (stored && (stored.region || stored.country)) return { region: stored.region || "all", country: stored.country || "all", source: "pinned" };
    return { region: "all", country: "all", source: "default" };
  }
  function advisoryMatches(item, ao) {
    var region = (ao && ao.region) || "all";
    var country = (ao && ao.country) || "all";
    if ((!country || country === "all") && (!region || region === "all")) return true;
    var blob = [item && item.name, item && item.region, item && item.corridor, item && item.fir, item && item.country, item && item.chokepoint].concat((item && item.match) || []).join(" ").toLowerCase();
    if (country && country !== "all") {
      var name = (canonicalCountry(country) || country).toLowerCase();
      return name.length > 2 && blob.indexOf(name) >= 0;
    }
    var macro = macroRegion(region) || region;
    if (macro === "Global") return (item && item.region) === "Global";
    return macroRegion(item && item.region) === macro;
  }

  return {
    AO_KEY: AO_KEY, REGIONS: REGIONS, COUNTRIES: COUNTRIES, RATES: RATES, CATEGORIES: CATEGORIES,
    canonicalCountry: canonicalCountry, regionOf: regionOf, macroRegion: macroRegion, isMacro: isMacro,
    eventMatchesScope: eventMatchesScope, eventMatches: eventMatches, countriesFor: countriesFor,
    countryCenter: countryCenter, regionView: regionView, setCatalog: setCatalog,
    composeSummary: composeSummary, countryTemplate: countryTemplate, quoteAo: quoteAo,
    aoLabel: aoLabel, readAo: readAo, writeAo: writeAo, clearAo: clearAo, aoFromQuery: aoFromQuery,
    advisoryMatches: advisoryMatches
  };
});
