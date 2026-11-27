/* Registers the GeoRiskIntel service worker and offers Install App when the browser asks. */
(function () {
  var deferred = null;

  function standalone() {
    return window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
  }

  function removeButton() {
    var button = document.getElementById("pwa-install");
    if (button) button.remove();
  }

  function showButton() {
    if (standalone() || document.getElementById("pwa-install")) return;
    var style = document.getElementById("pwa-install-style");
    if (!style) {
      style = document.createElement("style");
      style.id = "pwa-install-style";
      style.textContent = [
        "#pwa-install{color:#d4af37;background:#102a63;border:1px solid #c5a059;border-radius:999px;",
        "padding:6px 12px;font:700 12px/1.2 Segoe UI,system-ui,sans-serif;letter-spacing:.02em;cursor:pointer}",
        "#pwa-install:hover{color:#f8fafc}",
        "body > #pwa-install{position:fixed;right:16px;bottom:18px;z-index:5000;box-shadow:0 8px 24px rgba(0,0,0,.35)}",
        "@media print{#pwa-install{display:none!important}}"
      ].join("");
      document.head.appendChild(style);
    }
    var button = document.createElement("button");
    button.id = "pwa-install";
    button.type = "button";
    button.textContent = "Install App";
    button.setAttribute("aria-label", "Install GeoRiskIntel");
    button.addEventListener("click", function () {
      if (!deferred || typeof deferred.prompt !== "function") return;
      var promptEvent = deferred;
      deferred = null;
      var choice = promptEvent.prompt();
      var settled = promptEvent.userChoice || choice;
      if (settled && typeof settled.then === "function") settled.then(removeButton, removeButton);
      else removeButton();
    });
    var host = document.getElementById("live-bar");
    if (host) host.appendChild(button);
    else document.body.appendChild(button);
  }

  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferred = event;
    showButton();
  });

  window.addEventListener("appinstalled", function () {
    deferred = null;
    removeButton();
  });

  function register() {
    if (!("serviceWorker" in navigator)) return;
    navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function () {});
  }

  if (document.readyState === "complete") register();
  else window.addEventListener("load", register);
})();
