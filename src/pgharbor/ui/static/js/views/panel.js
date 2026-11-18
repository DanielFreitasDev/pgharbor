// The panel itself: language and theme, facts about this run, how it is protected.

import { on } from "../api.js";
import { h, icon, mount } from "../dom.js";
import { fmt, LANGUAGES, language, t } from "../i18n.js";
import { openPauseDialog, pauseScope, pauseState, setTheme, stopPanel, theme } from "../shell.js";
import { readOnly, store } from "../store.js";
import { badge, banner, button, card, copyButton, dropdown, kv, pageHead, segmented } from "../ui.js";

const DOCS = "https://github.com/DanielFreitasDev/pgharbor/blob/main/docs";

export function panel(root, { changeLanguage }) {
  function path(value) {
    return h("span", { class: "path-row" }, h("span", { class: "mono" }, value), copyButton(value));
  }

  let shownPaused = null;
  let themeControl = null;

  function draw() {
    const meta = store.meta;
    const pause = pauseState();
    shownPaused = pause.paused;
    const protections = ["local", "key", "sites", "review", "backup", "limit"].map((name) =>
      h(
        "div",
        { class: ["finding", name === "limit" ? "warn" : "ok"] },
        icon(name === "limit" ? "alert-triangle" : "shield-check"),
        h("div", { class: "body" }, h("span", { class: "strong" }, t(`guard.${name}.title`)), h("p", null, t(`guard.${name}.text`))),
      ),
    );

    mount(
      root,
      pageHead({ title: t("panel.title"), text: t("panel.text") }),
      h(
        "div",
        { class: "split" },
        card(
          { title: t("panel.appearance") },
          h(
            "div",
            { class: "labeled" },
            t("panel.language"),
            dropdown(
              LANGUAGES.map((entry) => ({ value: entry.code, label: entry.name })),
              language(),
              (next) => changeLanguage(next),
              { label: t("panel.language") },
            ),
          ),
          h(
            "div",
            { class: "labeled" },
            t("panel.theme"),
            (themeControl = segmented(
              ["system", "light", "dark"].map((value) => ({ value, label: t(`theme.${value}`) })),
              theme(),
              (next) => setTheme(next),
              { label: t("panel.theme") },
            )),
            h("span", { class: "small secondary" }, t("panel.themeHelp")),
          ),
        ),
        card(
          {
            title: t("pause.title"),
            subtitle: t("pause.cardText"),
            actions: pause.paused ? badge(t("pause.badge"), "danger", "pause") : badge(t("pause.off"), "ok", "play"),
          },
          pause.paused
            ? kv([
                [t("pause.scope"), pauseScope(pause)],
                [t("pause.since"), pause.since ? fmt.dateTime(pause.since) : null],
                [t("pause.reason"), pause.reason || null],
              ])
            : h("p", { class: "small secondary" }, t("pause.how")),
          h(
            "div",
            null,
            button(pause.paused ? t("pause.manage") : t("pause.start"), {
              icon: "pause",
              variant: pause.paused ? "primary" : "danger",
              onClick: openPauseDialog,
              attrs: { disabled: readOnly() && !pause.paused },
            }),
          ),
        ),
      ),
      card(
        {
          title: t("panel.run"),
          subtitle: t("panel.runText"),
          actions: readOnly() ? badge(t("readonly.badge"), "info", "eye") : badge(t("panel.editing"), "accent", "edit"),
        },
        kv([
          [t("panel.version"), `pgharbor ${meta.version} · Python ${meta.python} · ${meta.platform}`],
          [t("panel.started"), `${fmt.dateTime(meta.started)} (${fmt.relative(meta.started)})`],
          [t("panel.idle"), meta.idle_minutes ? t("panel.idleValue", { n: meta.idle_minutes }) : t("panel.idleNever")],
          [t("panel.configFile"), path(meta.paths.config)],
          [t("panel.auditFile"), path(meta.paths.audit)],
          [t("panel.sessionFiles"), path(meta.paths.sessions)],
        ]),
        h("div", null, button(t("panel.stop"), { icon: "power", variant: "subtle-danger", onClick: stopPanel })),
      ),
      card({ title: t("guard.title"), subtitle: t("guard.text") }, h("div", null, protections)),
      card(
        { title: t("panel.docs") },
        h(
          "div",
          { class: "row gap-2" },
          ["ui", "security", "configuration", "tools"].map((page) =>
            h(
              "a",
              { class: "btn small", href: `${DOCS}/${page}.md`, target: "_blank", rel: "noopener noreferrer" },
              icon("external"),
              t(`docs.${page}`),
            ),
          ),
        ),
        banner({ plain: true, text: t("panel.docsNote") }),
      ),
    );
  }

  draw();
  const offPulse = on("pulse", () => {
    if (pauseState().paused !== shownPaused) draw();
  });
  // The theme may be changed from the top bar too: follow it without redrawing, so
  // that a person moving through the options with the keyboard keeps the focus.
  const offTheme = on("theme", (mode) => themeControl && themeControl.select(mode));
  return () => {
    offPulse();
    offTheme();
  };
}
