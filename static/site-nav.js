(function () {
  "use strict";

  var bar = document.getElementById("live-bar");
  var nav = document.getElementById("page-nav");
  if (!bar || !bar.classList.contains("gri-shell") || !nav) return;

  var toggle = bar.querySelector(".nav-toggle");
  var narrowQuery = window.matchMedia("(max-width: 1080px)");
  var backdrop = document.createElement("button");
  backdrop.type = "button";
  backdrop.className = "nav-backdrop";
  backdrop.hidden = true;
  backdrop.setAttribute("aria-label", "Close menu");
  bar.insertAdjacentElement("afterend", backdrop);

  var skip = document.createElement("a");
  skip.className = "skip-link";
  skip.href = "#page-main";
  skip.textContent = "Skip to content";
  document.body.insertBefore(skip, document.body.firstChild);
  var target = document.querySelector("main")
    || document.getElementById("workspace")
    || document.getElementById("risk-stage");
  if (target) {
    if (!target.id) target.id = "page-main";
    skip.href = "#" + target.id;
  } else {
    skip.hidden = true;
  }

  var toast = document.createElement("div");
  toast.id = "gri-toast";
  toast.setAttribute("role", "status");
  toast.setAttribute("aria-live", "polite");
  document.body.appendChild(toast);
  var toastTimer = 0;

  function griToast(message) {
    toast.textContent = message || "";
    toast.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toast.classList.remove("show");
    }, 1800);
  }
  window.griToast = griToast;

  function pathName() {
    var path = window.location.pathname || "/";
    if (path.length > 1 && path.charAt(path.length - 1) === "/") path = path.slice(0, -1);
    return path || "/";
  }

  function ensureWatchLink() {
    if (nav.querySelector('a[href="/sentinel-zones"]')) return;
    var menu = nav.querySelector(".nav-menu");
    if (!menu) return;
    var link = document.createElement("a");
    link.href = "/sentinel-zones";
    link.textContent = "Sentinel zones";
    var after = menu.querySelector('a[href="/reports"]');
    if (after && after.nextSibling) menu.insertBefore(link, after.nextSibling);
    else menu.appendChild(link);
  }

  function markCurrent() {
    var path = pathName();
    var links = nav.querySelectorAll("a[href]");
    Array.prototype.forEach.call(links, function (link) {
      if (link.classList.contains("nav-cta")) return;
      var href = link.getAttribute("href") || "";
      if (href === path) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    var more = nav.querySelector(".nav-more");
    if (more) more.classList.toggle("has-current", !!more.querySelector("a[aria-current='page']"));
  }

  function isNarrow() {
    return narrowQuery.matches;
  }

  function placeDrawer() {
    if (isNarrow()) nav.style.top = Math.ceil(bar.getBoundingClientRect().height) + "px";
    else nav.style.top = "";
  }

  function syncInert() {
    if (isNarrow() && !bar.classList.contains("nav-open")) nav.setAttribute("inert", "");
    else nav.removeAttribute("inert");
  }

  function setOpen(open) {
    if (!isNarrow()) open = false;
    var was = bar.classList.contains("nav-open");
    bar.classList.toggle("nav-open", open);
    if (toggle) {
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      var label = toggle.querySelector(".nav-toggle-label");
      if (label) label.textContent = open ? "Close" : "Menu";
    }
    backdrop.hidden = !open;
    document.body.style.overflow = open ? "hidden" : "";
    placeDrawer();
    syncInert();
    if (open && !was) {
      var first = nav.querySelector("a, summary");
      if (first) first.focus();
    } else if (!open && was && toggle) {
      toggle.focus();
    }
  }

  if (toggle) {
    toggle.addEventListener("click", function () {
      setOpen(!bar.classList.contains("nav-open"));
    });
  }
  backdrop.addEventListener("click", function () { setOpen(false); });
  nav.addEventListener("click", function (event) {
    var link = event.target.closest("a");
    if (link && isNarrow()) setOpen(false);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      var more = nav.querySelector(".nav-more[open]");
      if (more && !isNarrow()) {
        more.removeAttribute("open");
        var summary = more.querySelector("summary");
        if (summary) summary.focus();
        return;
      }
      if (bar.classList.contains("nav-open")) setOpen(false);
    }
  });
  document.addEventListener("click", function (event) {
    if (isNarrow()) return;
    var openMenu = nav.querySelector(".nav-more[open]");
    if (openMenu && !openMenu.contains(event.target)) openMenu.removeAttribute("open");
  });

  function onNarrowChange() {
    if (!isNarrow()) setOpen(false);
    placeDrawer();
    syncInert();
  }
  if (narrowQuery.addEventListener) narrowQuery.addEventListener("change", onNarrowChange);
  else if (narrowQuery.addListener) narrowQuery.addListener(onNarrowChange);

  document.querySelectorAll("[data-how]").forEach(function (panel) {
    var key = "gri-how-" + (panel.getAttribute("data-how") || "");
    try {
      if (window.sessionStorage && sessionStorage.getItem(key) === "1") panel.hidden = true;
    } catch (err) { /* storage can be blocked */ }
    var button = panel.querySelector("[data-how-dismiss]");
    if (!button) return;
    button.addEventListener("click", function () {
      panel.hidden = true;
      try { sessionStorage.setItem(key, "1"); } catch (err) { /* ignore */ }
    });
  });

  ensureWatchLink();
  markCurrent();
  placeDrawer();
  syncInert();
  window.addEventListener("resize", placeDrawer);
}());
