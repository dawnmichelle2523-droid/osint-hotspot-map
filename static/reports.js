(function () {
  const scopeInput = document.getElementById("scope");
  const periodInput = document.getElementById("period");
  const daysLabel = document.getElementById("days-label");
  const daysInput = document.getElementById("days");
  const categoryInput = document.getElementById("category");
  const preview = document.getElementById("preview");
  const generateStatus = document.getElementById("generate-status");
  const sendStatus = document.getElementById("send-status");
  const scheduleStatus = document.getElementById("schedule-status");
  let reportId = null;
  let reportHtml = "";

  function text(node, value) {
    node.textContent = value || "";
  }

  async function readJson(response) {
    let payload = {};
    try {
      payload = await response.json();
    } catch (err) {
      throw new Error("That did not work. Try again in a moment.");
    }
    if (!response.ok || payload.ok === false) {
      var reason = payload.error || "";
      if (!reason || /traceback|exception|sqlite|sql|stack/i.test(reason)) {
        reason = "That did not work. Try again in a moment.";
      }
      throw new Error(reason);
    }
    return payload;
  }

  periodInput.addEventListener("change", function () {
    daysLabel.hidden = periodInput.value !== "custom";
  });

  function payload() {
    const body = {
      scope: scopeInput.value.trim(),
      period: periodInput.value === "custom" ? "weekly" : periodInput.value,
      category: categoryInput.value
    };
    if (periodInput.value === "custom") body.days = Number(daysInput.value);
    return body;
  }

  async function loadScopes() {
    const payload = await readJson(await fetch("/api/reports/scopes"));
    const list = document.getElementById("scope-list");
    const options = payload.regions.concat(payload.countries);
    list.innerHTML = options.map(function (name) {
      return "<option value=\"" + name.replace(/"/g, "&quot;") + "\"></option>";
    }).join("");
    payload.categories.forEach(function (item) {
      const option = document.createElement("option");
      option.value = item.code;
      option.textContent = item.code + " — " + item.name;
      categoryInput.appendChild(option);
    });
  }

  async function generate() {
    const button = document.getElementById("generate");
    button.classList.add("is-busy");
    button.setAttribute("aria-busy", "true");
    button.disabled = true;
    text(generateStatus, "Preparing the report…");
    const response = await fetch("/api/reports/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload())
    });
    const body = await readJson(response);
    reportId = body.id;
    reportHtml = body.html || "";
    preview.srcdoc = reportHtml;
    text(generateStatus, body.scope + " " + (body.cadence_title || "report") + " is ready.");
    generateStatus.className = "note status status-ok";
  }

  document.getElementById("generate").addEventListener("click", function () {
    generate().catch(function (error) {
      generateStatus.className = "note status status-bad";
      text(generateStatus, error.message || "The report could not be prepared.");
    }).then(function () {
      const button = document.getElementById("generate");
      button.classList.remove("is-busy");
      button.removeAttribute("aria-busy");
      button.disabled = false;
    });
  });

  document.getElementById("print").addEventListener("click", function () {
    if (!preview.contentWindow) return;
    preview.contentWindow.focus();
    preview.contentWindow.print();
  });

  document.getElementById("download").addEventListener("click", function () {
    if (!reportHtml) {
      text(generateStatus, "Generate a report first.");
      return;
    }
    const blob = new Blob([reportHtml], { type: "text/html" });
    const link = document.createElement("a");
    const name = (scopeInput.value || "report").replace(/[^\w.& -]+/g, "").trim() || "report";
    link.href = URL.createObjectURL(blob);
    link.download = "GeoRiskIntel-" + name + "-" + periodInput.value + ".html";
    link.click();
    URL.revokeObjectURL(link.href);
  });

  document.getElementById("send-now").addEventListener("click", function () {
    const body = payload();
    body.recipients = document.getElementById("send-recipients").value;
    if (reportId) body.report_id = reportId;
    text(sendStatus, "Sending…");
    fetch("/api/reports/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(readJson).then(function (result) {
      text(sendStatus, "Sent to " + (result.recipients || []).join(", "));
    }).catch(function (error) { text(sendStatus, error.message); });
  });

  function scheduleBody() {
    return {
      client_label: document.getElementById("client-label").value,
      recipients: document.getElementById("schedule-recipients").value,
      scope: document.getElementById("schedule-scope").value,
      cadence: document.getElementById("schedule-cadence").value,
      enabled: document.getElementById("schedule-enabled").checked
    };
  }

  function renderSchedules(rows) {
    const host = document.getElementById("schedule-rows");
    if (!rows.length) {
      host.innerHTML = "<tr><td colspan=\"7\">No subscriptions yet.</td></tr>";
      return;
    }
    host.innerHTML = rows.map(function (row) {
      const recipients = (row.recipients || []).join(", ");
      const sent = row.last_sent_at || "—";
      const enabled = row.enabled ? "Yes" : "No";
      return "<tr data-id=\"" + row.id + "\">" +
        "<td>" + escapeText(row.client_label) + "</td>" +
        "<td class=\"wrap\">" + escapeText(recipients) + "</td>" +
        "<td>" + escapeText(row.scope) + "</td>" +
        "<td>" + escapeText(row.cadence) + "</td>" +
        "<td>" + enabled + "</td>" +
        "<td>" + escapeText(sent) + "</td>" +
        "<td>" +
          "<button type=\"button\" data-test=\"" + row.id + "\">Send Test Now</button> " +
          "<button type=\"button\" class=\"ghost\" data-toggle=\"" + row.id + "\">" + (row.enabled ? "Disable" : "Enable") + "</button> " +
          "<button type=\"button\" class=\"ghost\" data-delete=\"" + row.id + "\">Delete</button>" +
        "</td></tr>";
    }).join("");
    host.querySelectorAll("[data-test]").forEach(function (button) {
      button.addEventListener("click", function () { sendTest(button.dataset.test); });
    });
    host.querySelectorAll("[data-toggle]").forEach(function (button) {
      button.addEventListener("click", function () { toggleSchedule(button.dataset.toggle, rows); });
    });
    host.querySelectorAll("[data-delete]").forEach(function (button) {
      button.addEventListener("click", function () { removeSchedule(button.dataset.delete); });
    });
  }

  function escapeText(value) {
    return String(value || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  async function loadSchedules() {
    const payload = await readJson(await fetch("/api/reports/schedules"));
    renderSchedules(payload.schedules || []);
  }

  document.getElementById("schedule-form").addEventListener("submit", function (event) {
    event.preventDefault();
    text(scheduleStatus, "Saving…");
    fetch("/api/reports/schedules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(scheduleBody())
    }).then(readJson).then(function () {
      text(scheduleStatus, "Subscription saved.");
      return loadSchedules();
    }).catch(function (error) { text(scheduleStatus, error.message); });
  });

  function sendTest(id) {
    text(scheduleStatus, "Sending test…");
    fetch("/api/reports/schedules/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: Number(id) })
    }).then(readJson).then(function (result) {
      text(scheduleStatus, "Test sent to " + (result.recipients || []).join(", "));
      return loadSchedules();
    }).catch(function (error) { text(scheduleStatus, error.message); });
  }

  function toggleSchedule(id, rows) {
    const current = rows.find(function (row) { return String(row.id) === String(id); });
    if (!current) return;
    const body = {
      id: current.id,
      client_label: current.client_label,
      recipients: current.recipients,
      scope: current.scope,
      cadence: current.cadence,
      enabled: !current.enabled
    };
    fetch("/api/reports/schedules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(readJson).then(loadSchedules).catch(function (error) { text(scheduleStatus, error.message); });
  }

  function removeSchedule(id) {
    fetch("/api/reports/schedules/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: Number(id) })
    }).then(readJson).then(function () {
      text(scheduleStatus, "Subscription removed.");
      return loadSchedules();
    }).catch(function (error) { text(scheduleStatus, error.message); });
  }

  loadScopes().catch(function (error) { text(generateStatus, error.message); });
  loadSchedules().catch(function (error) { text(scheduleStatus, error.message); });
})();
