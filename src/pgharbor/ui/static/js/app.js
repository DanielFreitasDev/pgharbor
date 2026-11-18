// The application: the key, the shell, the router and the heartbeat.

import { api, emit, on, rememberToken, takeToken, tokenFromText } from "./api.js";
import { brandMark, h, icon, mount } from "./dom.js";
import { initLanguage, language, LANGUAGES, setLanguage, t } from "./i18n.js";
import { errorText, guardUnsaved, hasUnsaved } from "./save.js";
import { nextTheme, openPauseDialog, pauseScope, pauseState, resumeAccess, setTheme, stoppedScreen, theme, themeIcon } from "./shell.js";
import { loadConfig, loadMeta, readOnly, store } from "./store.js";
import { badge, banner, button, confirm, dropdown, iconButton, initTooltips, link, loading, openDialog, segmented } from "./ui.js";
import { audit } from "./views/audit.js";
import { connectionEditor, connections } from "./views/connections.js";
import { defaults } from "./views/defaults.js";
import { file } from "./views/file.js";
import { overview } from "./views/overview.js";
import { panel } from "./views/panel.js";
import { sessions } from "./views/sessions.js";
import { tools } from "./views/tools.js";

const PULSE_MS = 4000;
const NAV = [
  { id: "overview", path: "", icon: "overview", group: "watch" },
  { id: "audit", path: "audit", icon: "list", group: "watch" },
  { id: "sessions", path: "sessions", icon: "users", group: "watch" },
  { id: "connections", path: "connections", icon: "database", group: "manage" },
  { id: "defaults", path: "defaults", icon: "sliders", group: "manage" },
  { id: "tools", path: "tools", icon: "toolbox", group: "manage" },
  { id: "file", path: "file", icon: "file", group: "manage" },
  { id: "panel", path: "panel", icon: "shield", group: "panel" },
];
const PRIMARY = ["overview", "connections", "audit", "sessions"]; // the bottom navigation

const root = document.getElementById("root");
let cleanup = null;
let current = { id: null, hash: null };
let pulseTimer = null;
let page = null;
let banners = null;
let alive = true;

// ----------------------------------------------------------------------- routes

function parse(hash) {
  const [path, search] = (hash || "#/").replace(/^#\/?/, "").split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { parts, params: new URLSearchParams(search || "") };
}

function resolve({ parts, params }) {
  const context = { params, go, changeLanguage };
  const [head, tail] = parts;
  if (!head) return { id: "overview", view: (target) => overview(target, context) };
  if (head === "connections") {
    if (tail === undefined) return { id: "connections", view: (target) => connections(target, context) };
    const name = tail === "new" ? undefined : tail;
    return { id: "connections", view: (target) => connectionEditor(target, { ...context, name }) };
  }
  const simple = { defaults, tools, audit, sessions, file, panel }[head];
  if (simple) return { id: head, view: (target) => simple(target, context) };
  return { id: "overview", view: (target) => overview(target, context) };
}

function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

async function onHashChange() {
  const target = location.hash || "#/";
  if (target === current.hash) return;
  // A link to a place inside the page (a section of a form) is not a route.
  if (!target.startsWith("#/")) {
    history.replaceState(null, "", current.hash || "#/");
    return;
  }
  if (hasUnsaved() && parse(target).parts.join("/") !== parse(current.hash).parts.join("/")) {
    history.replaceState(null, "", current.hash);
    const leave = await confirm({
      title: t("unsaved.title"),
      text: t("unsaved.text"),
      confirmLabel: t("unsaved.leave"),
      tone: "danger",
    });
    if (!leave) return;
    guardUnsaved(null);
    history.replaceState(null, "", target);
  }
  render();
}

function render() {
  if (!page) return;
  const hash = location.hash || "#/";
  const route = resolve(parse(hash));
  if (cleanup) cleanup();
  cleanup = null;
  guardUnsaved(null);
  current = { id: route.id, hash };
  document.querySelectorAll("[data-nav]").forEach((item) => {
    if (item.dataset.nav === route.id) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  const title = t(`nav.${route.id}`);
  document.title = `${title} · pgharbor`;
  const heading = document.getElementById("topbar-title");
  if (heading) heading.textContent = title;
  mount(page);
  cleanup = route.view(page) || null;
  window.scrollTo(0, 0);
  const pageTitle = page.querySelector("h1");
  pageTitle?.focus({ preventScroll: true });
  watchTitle(pageTitle);
}

/** The top bar repeats the page title only while the title itself is out of sight. */
let titleWatcher = null;
function watchTitle(title) {
  if (titleWatcher) titleWatcher.disconnect();
  const bar = document.querySelector(".topbar");
  if (!bar) return;
  bar.classList.remove("scrolled");
  if (!title) return;
  titleWatcher = new IntersectionObserver(
    ([entry]) => bar.classList.toggle("scrolled", !entry.isIntersecting),
    { rootMargin: "-56px 0px 0px 0px" },
  );
  titleWatcher.observe(title);
}

// ------------------------------------------------------------------------ shell

function navLink(item) {
  return h(
    "a",
    { class: "nav-link", href: `#/${item.path}`, data: { nav: item.id } },
    icon(item.icon),
    h("span", null, t(`nav.${item.id}`)),
    item.id === "sessions" ? h("span", { class: "count", id: "nav-sessions", hidden: true }) : null,
  );
}

function languagePicker(options = {}) {
  return dropdown(
    LANGUAGES.map((entry) => ({ value: entry.code, label: entry.name })),
    language(),
    (next) => changeLanguage(next),
    { label: t("panel.language"), ...options },
  );
}

function themeButton() {
  const mode = theme();
  const control = iconButton(themeIcon(mode), t("theme.switch", { mode: t(`theme.${mode}`) }), {
    onClick: () => setTheme(nextTheme()),
    attrs: { id: "theme-button" },
  });
  return control;
}

function pauseButton() {
  const pause = pauseState();
  if (pause.paused) {
    return button(t("pause.badge"), { icon: "pause", variant: "danger", small: true, onClick: openPauseDialog, attrs: { id: "pause-button" } });
  }
  return button(t("pause.short"), {
    icon: "pause",
    small: true,
    onClick: openPauseDialog,
    title: t("pause.title"),
    attrs: { id: "pause-button", disabled: readOnly() },
  });
}

function moreSheet() {
  const rest = NAV.filter((item) => !PRIMARY.includes(item.id));
  const dialog = openDialog({
    title: t("nav.more"),
    body: [
      h(
        "div",
        { class: "menu-sheet card flush" },
        h(
          "div",
          { class: "rows" },
          rest.map((item) =>
            h(
              "a",
              {
                href: `#/${item.path}`,
                on: { click: () => dialog.close() },
              },
              icon(item.icon),
              h("span", { class: "lead" }, t(`nav.${item.id}`)),
              icon("chevron-right"),
            ),
          ),
        ),
      ),
      h("div", { class: "labeled" }, t("panel.language"), languagePicker()),
      h(
        "div",
        { class: "labeled" },
        t("panel.theme"),
        segmented(
          ["system", "light", "dark"].map((value) => ({ value, label: t(`theme.${value}`) })),
          theme(),
          (next) => setTheme(next),
          { label: t("panel.theme") },
        ),
      ),
    ],
  });
}

function shell() {
  const groups = ["watch", "manage", "panel"].map((group) =>
    h(
      "div",
      { class: "nav-list" },
      h("div", { class: "nav-heading" }, t(`nav.group.${group}`)),
      NAV.filter((item) => item.group === group).map(navLink),
    ),
  );
  page = h("div", { class: "page" });
  banners = h("div", { class: "banners", id: "banners" });
  const app = h(
    "div",
    { class: "app" },
    h("a", { class: "skip-link", href: "#main", on: { click: skip } }, t("common.skip")),
    h(
      "nav",
      { class: "nav", "aria-label": t("nav.label") },
      h("a", { class: "brand", href: "#/" }, brandMark(), h("span", { class: "brand-text" }, "pgharbor", h("small", null, t("brand.tagline")))),
      groups,
      h(
        "div",
        { class: "nav-foot" },
        h("div", { class: "label-wide" }, languagePicker({ small: true })),
        h("div", { class: "label-wide tiny muted" }, `pgharbor ${store.meta.version}`),
      ),
    ),
    h(
      "header",
      { class: "topbar" },
      h("a", { class: "brand", href: "#/", "aria-label": "pgharbor" }, brandMark()),
      h("div", { class: "topbar-title truncate", id: "topbar-title", "aria-hidden": "true" }),
      h("div", { class: "topbar-status", id: "topbar-status" }),
      h(
        "div",
        { class: "topbar-actions", id: "topbar-actions" },
        readOnly() ? badge(t("readonly.badge"), "info", "eye") : null,
        pauseButton(),
        themeButton(),
      ),
    ),
    h(
      "main",
      { class: "main", id: "main", tabindex: "-1" },
      // The page column: what holds whatever the screen is, then the screen.
      h("div", { class: "stack gap-5" }, h("div", { class: "page" }, banners), page),
    ),
    h(
      "nav",
      { class: "bottomnav", "aria-label": t("nav.label") },
      NAV.filter((item) => PRIMARY.includes(item.id)).map((item) =>
        h("a", { href: `#/${item.path}`, data: { nav: item.id } }, icon(item.icon), h("span", null, t(`nav.${item.id}`))),
      ),
      h("button", { type: "button", "aria-haspopup": "dialog", on: { click: moreSheet } }, icon("more"), h("span", null, t("nav.more"))),
    ),
  );
  mount(root, app);
  drawBanners();
  drawCounts();
}

function skip(event) {
  event.preventDefault();
  document.getElementById("main").focus();
}

function drawTopbar() {
  const actions = document.getElementById("topbar-actions");
  if (!actions) return;
  mount(actions, readOnly() ? badge(t("readonly.badge"), "info", "eye") : null, pauseButton(), themeButton());
}

function drawCounts() {
  const count = document.getElementById("nav-sessions");
  if (!count) return;
  const active = (store.pulse && store.pulse.active_sessions) || 0;
  count.hidden = active === 0;
  count.textContent = String(active);
  drawStatus();
}

/** The state of the installation in two chips: who is connected, and is the file sound. */
function drawStatus() {
  const status = document.getElementById("topbar-status");
  if (!status) return;
  const active = (store.pulse && store.pulse.active_sessions) || 0;
  const config = store.config;
  const valid = config && config.valid && config.file.exists;
  mount(
    status,
    h(
      "a",
      { class: "status-chip", href: "#/sessions" },
      h("span", { class: ["dot", active ? "ok live" : null], "aria-hidden": "true" }),
      t("sessions.active", { n: active }),
    ),
    h(
      "a",
      { class: ["status-chip", valid ? "ok" : "danger"], href: "#/file" },
      icon(valid ? "check-circle" : "alert-triangle"),
      valid ? t("status.valid") : t("status.invalid"),
    ),
  );
}

/** Redraw what shows the pause, whoever changed it: this page or another one. */
let shownPause = "";
function syncPause() {
  const pause = pauseState();
  const key = `${pause.paused}/${pause.since || ""}/${(pause.connections || []).join(",")}/${pause.reason || ""}`;
  if (key === shownPause) return;
  shownPause = key;
  drawTopbar();
  drawBanners();
}

function drawBanners() {
  if (!banners) return;
  const config = store.config;
  const pause = pauseState();
  const out = [];
  if (pause.paused) {
    const resume = button(t("pause.resume"), { icon: "play", small: true, variant: "primary", onClick: () => resumeAccess(resume) });
    out.push(
      banner({
        tone: "danger",
        title: t("pause.banner", { scope: pauseScope(pause) }),
        text: pause.reason ? t("pause.bannerReason", { reason: pause.reason }) : t("pause.activeText"),
        actions: readOnly() ? [] : [resume],
      }),
    );
  }
  if (config && config.file.exists && !config.valid) {
    out.push(
      banner({
        tone: "danger",
        title: t("banner.invalid"),
        text: t("banner.invalidText"),
        actions: [link(t("banner.seeFile"), "#/file", { small: true })],
      }),
    );
  } else if (config && !config.file.exists) {
    out.push(
      banner({
        tone: "warn",
        title: t(`banner.${config.file.origin}`),
        text: t("banner.noFileText"),
        actions: [link(t("banner.seeFile"), "#/file", { small: true })],
      }),
    );
  }
  mount(banners, out);
}

// -------------------------------------------------------------------- heartbeat

async function pulse() {
  if (document.hidden || !alive) return;
  try {
    store.pulse = await api("/api/pulse");
    if (store.config && store.pulse.config_digest !== store.config.file.digest) await loadConfig();
    emit("pulse", store.pulse);
  } catch {
    // "offline" and "locked" are handled where they are raised
  }
}

function startPulse() {
  clearInterval(pulseTimer);
  pulseTimer = setInterval(pulse, PULSE_MS);
  document.addEventListener("visibilitychange", () => !document.hidden && pulse());
}

// ---------------------------------------------------------------- lock and boot

function lockScreen(message) {
  alive = false;
  clearInterval(pulseTimer);
  page = null;
  const input = h("input", {
    class: "input",
    id: "key",
    type: "password",
    autocomplete: "off",
    spellcheck: "false",
    autocapitalize: "none",
    placeholder: "http://127.0.0.1:7432/#token=…",
    "aria-describedby": "key-help",
  });
  const error = h("div", { class: "field-error", role: "alert", hidden: !message }, icon("alert-circle"), message || "");
  const open = button(t("lock.open"), {
    variant: "primary",
    icon: "key",
    onClick: () => {
      const key = tokenFromText(input.value);
      if (!key) {
        error.lastChild.textContent = t("lock.invalid");
        error.hidden = false;
        return;
      }
      rememberToken(key);
      boot();
    },
  });
  input.addEventListener("keydown", (event) => event.key === "Enter" && open.click());
  mount(
    root,
    h(
      "main",
      { class: "gate" },
      h(
        "div",
        { class: "gate-card" },
        brandMark(),
        h("h1", null, t("lock.title")),
        h("p", { class: "secondary" }, t("lock.text")),
        h("pre", { class: "code" }, "pgharbor ui"),
        h("label", { class: "labeled", for: "key" }, t("lock.label"), input),
        h("p", { class: "small secondary", id: "key-help" }, t("lock.help")),
        error,
        h("div", null, open),
      ),
    ),
  );
  document.title = "pgharbor";
}

async function changeLanguage(code) {
  await setLanguage(code);
  shell();
  render();
}

async function boot() {
  mount(root, h("div", { class: "gate" }, h("div", { class: "gate-card" }, loading())));
  try {
    await Promise.all([loadMeta(), loadConfig()]);
    store.pulse = await api("/api/pulse");
  } catch (error) {
    if (error.code === "token") return; // the lock screen is already up
    if (error.code === "network") {
      mount(root, stoppedScreen(boot));
      return;
    }
    lockScreen(errorText(error));
    return;
  }
  alive = true;
  shell();
  render();
  startPulse();
}

async function main() {
  initTooltips();
  await initLanguage();
  on("locked", () => lockScreen(t("lock.expired")));
  on("offline", () => {
    if (!alive) return;
    alive = false;
    clearInterval(pulseTimer);
    if (cleanup) cleanup();
    cleanup = null;
    page = null;
    mount(root, stoppedScreen(boot));
  });
  on("stopped", () => {
    alive = false;
    clearInterval(pulseTimer);
    page = null;
    mount(root, stoppedScreen(null));
  });
  on("config", () => {
    drawBanners();
    drawTopbar();
    drawStatus();
  });
  on("theme", drawTopbar);
  on("pulse", () => {
    drawCounts();
    syncPause();
  });
  window.addEventListener("hashchange", onHashChange);
  if (takeToken()) await boot();
  else lockScreen();
}

main();
