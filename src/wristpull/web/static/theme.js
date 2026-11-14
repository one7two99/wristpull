// Applies the viewer's theme choice before the first paint (no flash); app.js owns the toggle.
(function () {
  try {
    var mode = localStorage.getItem("wristpull-theme");
    if (mode === "light" || mode === "dark") document.documentElement.setAttribute("data-theme", mode);
  } catch (e) { /* private mode: follow the system */ }
})();
