/* Ops navigation, dismissible cues, and copy confirmation.
   This file does not call the network. */
(function () {
  var header = document.getElementById("live-bar");
  var nav = document.getElementById("page-nav");
  var toggle = header ? header.querySelector(".nav-toggle") : null;
  var toastNode = null;
  var toastTimer = 0;

  function currentPath() {
    var path = window.location.pathname || "/";
    if (path.length > 1 && path.charAt(path.length - 1) === "/") path = path.slice(0, -1);
    return path || "/";
  }

  function markCurrent() {
    if (!nav) return;
    var path = currentPath();
    var links = nav.querySelectorAll("a[href]");
    Array.prototype.forEach.call(links, function (link) {
      if (link.classList.contains("nav-cta")) return;
      var href = link.getAttribute("href") || "";
      if (href === path) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }

  function placeDrawer() {
    if (!header) return;
    var row = header.querySelector(".nav-row");
    var bottom = row ? row.getBoundingClientRect().bottom : 64;
    header.style.setProperty("--shell-height", Math.ceil(bottom) + "px");
  }

  function setMenu(open) {
    if (!header || !toggle || !nav) return;
    placeDrawer();
    header.classList.toggle("nav-open", open);
    document.body.classList.toggle("nav-locked", open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    nav.setAttribute("aria-hidden", open || window.innerWidth > 1100 ? "false" : "true");
    if (open) {
      var first = nav.querySelector("a[href]");
      if (first) first.focus();
    }
  }

  function closeDetails(except) {
    if (!nav) return;
    Array.prototype.forEach.call(nav.querySelectorAll("details[open]"), function (node) {
      if (node !== except) node.removeAttribute("open");
    });
  }

  function toast(message) {
    var text = message || "Copied!";
    if (!toastNode) {
      toastNode = document.createElement("div");
      toastNode.className = "georisk-toast";
      toastNode.setAttribute("role", "status");
      toastNode.setAttribute("aria-live", "polite");
      document.body.appendChild(toastNode);
    }
    toastNode.textContent = text;
    toastNode.classList.add("is-on");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toastNode.classList.remove("is-on");
    }, 1600);
  }

  function friendly(message) {
    var text = String(message || "").replace(/\s+/g, " ").trim();
    if (!text || /traceback|sqlite|exception|errno|stack/i.test(text)) {
      return "That did not go through. Please try again.";
    }
    return text;
  }

  window.georiskToast = toast;
  window.georiskFriendly = friendly;

  if (header && toggle && nav) {
    markCurrent();
    if (window.innerWidth <= 1100) nav.setAttribute("aria-hidden", "true");
    toggle.addEventListener("click", function () {
      setMenu(!header.classList.contains("nav-open"));
    });
    var backdrop = header.querySelector(".nav-backdrop");
    if (backdrop) {
      backdrop.addEventListener("click", function () { setMenu(false); });
    }
    nav.addEventListener("click", function (event) {
      var link = event.target.closest("a[href]");
      if (link && window.innerWidth <= 1100) setMenu(false);
    });
    document.addEventListener("click", function (event) {
      var open = nav.querySelector("details[open]");
      if (!open || open.contains(event.target)) return;
      open.removeAttribute("open");
    });
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") return;
      closeDetails(null);
      if (header.classList.contains("nav-open")) {
        setMenu(false);
        toggle.focus();
      }
    });
    window.addEventListener("resize", function () {
      placeDrawer();
      if (window.innerWidth > 1100) {
        header.classList.remove("nav-open");
        document.body.classList.remove("nav-locked");
        toggle.setAttribute("aria-expanded", "false");
        toggle.setAttribute("aria-label", "Open menu");
        nav.setAttribute("aria-hidden", "false");
      } else if (!header.classList.contains("nav-open")) {
        nav.setAttribute("aria-hidden", "true");
      }
    });
  }

  var dismissed = [];
  try { dismissed = JSON.parse(window.localStorage.getItem("georisk.how.dismissed") || "[]"); }
  catch (err) { dismissed = []; }
  if (!Array.isArray(dismissed)) dismissed = [];

  Array.prototype.forEach.call(document.querySelectorAll(".how-it-works"), function (panel) {
    var key = panel.getAttribute("data-how") || "";
    if (key && dismissed.indexOf(key) !== -1) {
      panel.hidden = true;
      return;
    }
    var button = panel.querySelector(".how-dismiss");
    if (!button) return;
    button.addEventListener("click", function () {
      panel.hidden = true;
      if (!key || dismissed.indexOf(key) !== -1) return;
      dismissed.push(key);
      try { window.localStorage.setItem("georisk.how.dismissed", JSON.stringify(dismissed)); }
      catch (err) { /* the cue can stay hidden for this view */ }
    });
  });
})();
