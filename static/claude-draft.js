/* On-demand Claude controls. This file never calls Anthropic or xAI.
   The status request only reads whether the server has a key.
   A draft request runs only after a click on Draft with Claude. */
(function () {
  function text(value) {
    return value == null ? "" : String(value);
  }

  function money(value) {
    var number = Number(value);
    if (!isFinite(number)) return "";
    return "$" + number.toFixed(4);
  }

  function paint(status) {
    var ready = !!(status && status.configured);
    document.querySelectorAll("[data-claude-status]").forEach(function (pill) {
      pill.textContent = ready ? "Claude connected" : "Add ANTHROPIC_API_KEY";
      pill.classList.toggle("ready", ready);
      pill.classList.toggle("idle", !ready);
      pill.title = text(status && status.status);
    });
    var rates = (status && status.rates) || {};
    var ra = rates.ra || {};
    var brief = rates.brief || {};
    var line = ready
      ? "Claude is connected. Nothing is sent until you click Draft with Claude."
      : "Claude is waiting for an ANTHROPIC_API_KEY. Clicks use the local template and spend $0.";
    line += " Risk drafts use " + text(status && status.ra_model) +
      " at $" + text(ra.input_usd_per_mtok) + " / $" + text(ra.output_usd_per_mtok) +
      " per million input / output tokens. Briefings use " + text(status && status.brief_model) +
      " at $" + text(brief.input_usd_per_mtok) + " / $" + text(brief.output_usd_per_mtok) +
      ". These are published list prices, not an invoice.";
    document.querySelectorAll("[data-claude-rates]").forEach(function (node) {
      if (!node.dataset.claudeLocked) node.textContent = line;
    });
  }

  function loadStatus() {
    return fetch("/api/ai/claude-status", { cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(paint)
      .catch(function () {
        paint({ configured: false, status: "Claude status could not be read. No draft was sent." });
      });
  }

  function draft(url, body) {
    return fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (response) { return response.json(); });
  }

  function costLine(payload) {
    var estimate = (payload && payload.estimate) || {};
    var usage = (payload && payload.usage) || {};
    var parts = [];
    if (payload && payload.status) parts.push(payload.status);
    if (estimate.input_tokens != null) {
      parts.push("About " + estimate.input_tokens + " input tokens.");
    }
    if (estimate.output_cap != null) {
      parts.push("Output cap " + estimate.output_cap + " tokens.");
    }
    if (estimate.usd_cap != null) {
      parts.push("List-price ceiling " + money(estimate.usd_cap) + ".");
    }
    if (payload && payload.source === "claude" && usage.estimated_usd != null) {
      parts.push(
        "This click was about " + money(usage.estimated_usd) +
        " (" + (usage.input_tokens || 0) + " in / " + (usage.output_tokens || 0) + " out)."
      );
    } else {
      parts.push("Spend this click: $0.");
    }
    return parts.join(" ");
  }

  function showCost(payload) {
    document.querySelectorAll("[data-claude-rates]").forEach(function (node) {
      node.dataset.claudeLocked = "1";
      node.textContent = costLine(payload);
    });
  }

  window.GeoRiskClaude = {
    loadStatus: loadStatus,
    draft: draft,
    costLine: costLine,
    showCost: showCost
  };

  if (document.querySelector("[data-claude-status]")) loadStatus();
})();
