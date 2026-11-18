// Runs before the first paint (a plain script, not a module) so the page never flashes
// in the wrong theme. Everything else lives in js/app.js.
(function () {
  try {
    var theme = localStorage.getItem("pgharbor.theme");
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
    }
    var language = localStorage.getItem("pgharbor.language");
    if (language) {
      document.documentElement.setAttribute("lang", language);
    }
  } catch (error) {
    // Storage may be unavailable (private mode): the system theme is used.
  }
})();
