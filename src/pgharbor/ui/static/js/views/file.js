// The file: where the configuration lives, what it says, and the way back to any
// earlier version. Credentials are masked in everything shown here.

import { api, on } from "../api.js";
import { h, icon, mount } from "../dom.js";
import { fmt, t } from "../i18n.js";
import { diffView, errorText, reviewAndSave } from "../save.js";
import { readOnly, store, useConfig } from "../store.js";
import { badge, banner, button, busy, card, copyButton, empty, kv, loading, openDialog, pageHead, toast } from "../ui.js";

export function file(root) {
  const facts = h("div");
  const content = h("div", { class: "stack gap-3" }, loading());
  const history = h("div", { class: "stack gap-3" });

  async function create(control) {
    await busy(control, async () => {
      try {
        useConfig(await api("/api/config/create", { method: "POST" }));
        toast(t("file.created"));
      } catch (error) {
        toast(errorText(error), "danger", { title: t("save.failed") });
      }
    });
  }

  /** A key the validator does not know (a typo, usually) can be taken out from here. */
  function removable(error) {
    const parts = (error.loc || "").split(".");
    return (
      !readOnly() &&
      store.config.file.is_toml &&
      /extra inputs|unknown/i.test(error.message) &&
      parts.length >= 2 &&
      parts.length <= 3 &&
      parts.every((part) => /^[A-Za-z0-9_-]+$/.test(part))
    );
  }

  async function removeKey(loc) {
    await reviewAndSave({ ops: [{ op: "unset", path: loc.split(".") }] });
  }

  function drawFacts() {
    const config = store.config;
    const info = config.file;
    const createButton = button(t("file.create"), { icon: "plus", variant: "primary", onClick: () => create(createButton) });
    const origin = {
      file: null,
      none: banner({ tone: "warn", title: t("file.none"), text: t("file.noneText"), actions: readOnly() ? [] : [createButton] }),
      environment: banner({ title: t("file.environment"), text: t("file.environmentText"), actions: readOnly() ? [] : [createButton] }),
      missing: banner({ tone: "danger", title: t("file.missing"), text: t("file.missingText"), actions: readOnly() ? [] : [createButton] }),
    }[info.origin];
    mount(
      facts,
      h(
        "div",
        { class: "stack gap-4" },
        origin,
        card(
          {
            title: t("file.where"),
            actions: info.exists
              ? config.valid
                ? badge(t("file.valid"), "ok", "check-circle")
                : badge(t("file.invalid"), "danger", "alert-triangle")
              : null,
          },
          kv([
            [t("file.path"), h("span", { class: "path-row" }, h("span", { class: "mono" }, info.path), copyButton(info.path))],
            [t("file.mode"), info.mode ? h("span", { class: "mono" }, info.mode) : null],
            [t("file.modified"), info.modified ? fmt.dateTime(info.modified) : null],
            [t("file.digest"), info.digest ? h("span", { class: "mono" }, info.digest.slice(0, 16)) : null],
          ]),
          config.errors.length
            ? [
                banner({ tone: "danger", title: t("file.errors"), text: info.is_toml ? t("file.errorsText") : t("file.notToml") }),
                h(
                  "ul",
                  { class: "stack gap-2" },
                  config.errors.map((error) =>
                    h(
                      "li",
                      { class: "finding danger" },
                      icon("alert-circle"),
                      h("div", { class: "body" }, error.loc ? h("code", null, error.loc) : null, h("p", null, error.message)),
                      removable(error) ? button(t("file.removeKey"), { icon: "trash", small: true, onClick: () => removeKey(error.loc) }) : null,
                    ),
                  ),
                ),
              ]
            : null,
          config.warnings.length ? config.warnings.map((warning) => banner({ tone: "warn", text: warning })) : null,
        ),
      ),
    );
  }

  async function loadContent() {
    const config = store.config;
    if (!config.file.exists) {
      mount(content);
      mount(history);
      return;
    }
    try {
      const [text, backups] = await Promise.all([api("/api/config/text"), api("/api/backups")]);
      mount(
        content,
        card(
          { title: t("file.content"), subtitle: t("file.contentText") },
          h("pre", { class: "code nowrap-code", tabindex: "0", "aria-label": t("file.content") }, text.text),
        ),
      );
      drawHistory(backups.backups);
    } catch (error) {
      mount(content, banner({ tone: "danger", title: t("error.load"), text: errorText(error) }));
    }
  }

  function drawHistory(backups) {
    mount(
      history,
      card(
        { title: t("file.history"), subtitle: t("file.historyText"), flush: true },
        backups.length
          ? h(
              "div",
              { class: "rows" },
              backups.map((backup) =>
                h(
                  "button",
                  { type: "button", on: { click: () => showBackup(backup) } },
                  icon("history"),
                  h("div", { class: "lead" }, h("span", { class: "strong" }, fmt.dateTime(backup.modified)), h("span", { class: "small secondary mono truncate" }, backup.name)),
                  h("div", { class: "trail" }, fmt.bytes(backup.size), h("span", { class: "tiny" }, fmt.relative(backup.modified))),
                ),
              ),
            )
          : empty({ icon: "history", title: t("file.noHistory"), text: t("file.noHistoryText") }),
      ),
    );
  }

  async function showBackup(backup) {
    let shown;
    try {
      shown = await api(`/api/backups/${encodeURIComponent(backup.name)}`);
    } catch (error) {
      toast(errorText(error), "danger");
      return;
    }
    const same = !shown.diff.length;
    const restore = button(t("file.restore"), {
      icon: "undo",
      variant: "primary",
      onClick: async () => {
        dialog.close();
        const route = `/api/backups/${encodeURIComponent(backup.name)}`;
        let preview;
        try {
          preview = await api(`${route}/preview`, { method: "POST", body: { base: store.config.file.digest } });
        } catch (error) {
          toast(errorText(error), "danger", { title: t("save.failed") });
          return;
        }
        // The same review as any other change: what moves, and what it costs.
        const saved = await reviewAndSave({ ops: [], extra: { restore: `${route}/restore`, preview } });
        if (saved) loadContent();
      },
    });
    const dialog = openDialog({
      title: fmt.dateTime(backup.modified),
      wide: true,
      body: [
        same ? banner({ tone: "ok", title: t("file.same") }) : [h("h3", null, t("file.wouldChange")), h("p", { class: "small secondary" }, t("file.wouldChangeText")), diffView(shown.diff)],
        h("details", { class: "more" }, h("summary", null, icon("chevron-right"), t("file.fullText")), h("pre", { class: "code nowrap-code" }, shown.text)),
      ],
      footer: [button(t("common.close"), { onClick: () => dialog.close() }), same || readOnly() ? null : restore],
    });
  }

  mount(root, pageHead({ title: t("file.title"), text: t("file.text") }), facts, content, history);
  drawFacts();
  loadContent();
  return on("config", () => {
    drawFacts();
    loadContent();
  });
}
