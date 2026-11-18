// Actions that belong to the whole panel rather than to one screen: the theme, the
// emergency pause and stopping the panel.

import { api, emit } from "./api.js";
import { h, icon } from "./dom.js";
import { fmt, t } from "./i18n.js";
import { errorText } from "./save.js";
import { connectionNames, readOnly, store } from "./store.js";
import { banner, button, busy, checkbox, confirm, kv, nextId, openDialog, segmented, toast } from "./ui.js";

const THEME_KEY = "pgharbor.theme";

export function theme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    return saved === "light" || saved === "dark" ? saved : "system";
  } catch {
    return "system";
  }
}

export function setTheme(mode) {
  const root = document.documentElement;
  if (mode === "light" || mode === "dark") root.setAttribute("data-theme", mode);
  else root.removeAttribute("data-theme");
  try {
    if (mode === "system") localStorage.removeItem(THEME_KEY);
    else localStorage.setItem(THEME_KEY, mode);
  } catch {
    // applied for this visit only
  }
  emit("theme", mode);
}

export function themeIcon(mode = theme()) {
  return { system: "monitor", light: "sun", dark: "moon" }[mode];
}

export function nextTheme() {
  const order = ["system", "light", "dark"];
  return order[(order.indexOf(theme()) + 1) % order.length];
}

// ------------------------------------------------------------------------ pause

export function pauseState() {
  return (store.pulse && store.pulse.pause) || { paused: false };
}

function usePause(pause) {
  store.pulse = { ...(store.pulse || {}), pause };
  emit("pulse", store.pulse);
}

export function pauseScope(pause) {
  return pause.connections && pause.connections.length ? pause.connections.join(", ") : t("pause.everything");
}

export async function resumeAccess(control) {
  await busy(control, async () => {
    try {
      usePause(await api("/api/pause", { method: "POST", body: { paused: false } }));
      toast(t("pause.lifted"));
    } catch (error) {
      toast(errorText(error), "danger");
    }
  });
}

/** The emergency pause: stop database access now, for everything or for some connections. */
export function openPauseDialog() {
  const pause = pauseState();
  if (pause.paused) {
    const resume = button(t("pause.resume"), {
      variant: "primary",
      icon: "play",
      onClick: async () => {
        await resumeAccess(resume);
        dialog.close();
      },
    });
    const dialog = openDialog({
      title: t("pause.activeTitle"),
      body: [
        banner({ tone: "danger", title: t("pause.active"), text: t("pause.activeText") }),
        kv([
          [t("pause.scope"), pauseScope(pause)],
          [t("pause.since"), pause.since ? fmt.dateTime(pause.since) : null],
          [t("pause.reason"), pause.reason || null],
        ]),
      ],
      footer: [button(t("common.close"), { onClick: () => dialog.close() }), readOnly() ? null : resume],
    });
    return;
  }

  let scope = "all";
  const chosen = new Set();
  const names = connectionNames();
  const reasonId = nextId("reason");
  const reason = h("input", { id: reasonId, class: "input", maxlength: "300", placeholder: t("pause.reasonPlaceholder"), autocomplete: "off" });
  const picks = h(
    "div",
    { class: "stack gap-1", hidden: true },
    names.map((name) =>
      checkbox(name, false, (checked) => {
        if (checked) chosen.add(name);
        else chosen.delete(name);
      }),
    ),
  );
  const start = button(t("pause.start"), {
    variant: "danger",
    icon: "pause",
    onClick: () =>
      busy(start, async () => {
        if (scope === "some" && !chosen.size) {
          toast(t("pause.pickOne"), "warn");
          return;
        }
        try {
          usePause(
            await api("/api/pause", {
              method: "POST",
              body: { paused: true, reason: reason.value, connections: scope === "some" ? [...chosen] : [] },
            }),
          );
          toast(t("pause.started"), "warn");
          dialog.close();
        } catch (error) {
          toast(errorText(error), "danger");
        }
      }),
  });
  const dialog = openDialog({
    title: t("pause.title"),
    body: [
      h("p", null, t("pause.text")),
      banner({ plain: true, title: t("pause.howTitle"), text: t("pause.how") }),
      banner({
        tone: store.pulse && store.pulse.active_sessions ? null : "warn",
        plain: true,
        text: t("pause.reach", { n: (store.pulse && store.pulse.active_sessions) || 0 }),
      }),
      h(
        "div",
        { class: "labeled" },
        t("pause.scope"),
        names.length
          ? segmented(
              [
                { value: "all", label: t("pause.everything") },
                { value: "some", label: t("pause.some") },
              ],
              scope,
              (next) => {
                scope = next;
                picks.hidden = next !== "some";
              },
              { label: t("pause.scope") },
            )
          : h("span", { class: "strong" }, t("pause.everything")),
        picks,
      ),
      h("label", { class: "labeled", for: reasonId }, t("pause.reason"), reason),
    ],
    footer: [button(t("common.cancel"), { onClick: () => dialog.close() }), readOnly() ? null : start],
  });
}

// ------------------------------------------------------------------------- stop

export async function stopPanel() {
  const sure = await confirm({
    title: t("panel.stopTitle"),
    text: t("panel.stopText"),
    confirmLabel: t("panel.stop"),
    tone: "danger",
  });
  if (!sure) return;
  try {
    await api("/api/shutdown", { method: "POST" });
    emit("stopped");
  } catch (error) {
    toast(errorText(error), "danger");
  }
}

export function stoppedScreen(retry) {
  return h(
    "div",
    { class: "gate" },
    h(
      "div",
      { class: "gate-card" },
      h("span", { class: "avatar neutral" }, icon("power")),
      h("h1", null, t("panel.stopped")),
      h("p", { class: "secondary" }, t("panel.stoppedText")),
      h("pre", { class: "code" }, "pgharbor ui"),
      retry ? h("div", null, button(t("common.retry"), { icon: "refresh", onClick: retry })) : null,
    ),
  );
}
