// Saving is always two steps: see what will change and what it costs, then confirm.
//
// The server works out both: the difference in the file (credentials masked) and every
// point where the new configuration protects less than the current one. A change with
// such a point cannot be saved without saying so explicitly; the server refuses it
// otherwise, whatever this page sends.

import { api, ApiError } from "./api.js";
import { h, icon } from "./dom.js";
import { fmt, has, t } from "./i18n.js";
import { store, useConfig } from "./store.js";
import { badge, banner, button, busy, checkbox, openDialog, toast } from "./ui.js";

const LEVEL_ICON = { danger: "shield-alert", warn: "alert-triangle", note: "info", ok: "check-circle" };

// Which setting the values of a finding belong to, so that they are shown with the
// same words as in the forms ("Read-only", not "read-only").
const SETTING = {
  "access-raised": "access",
  "connection-added": "access",
  "new-connection-writes": "access",
  "production-writes": "access",
  "write-confirmation-weakened": "write_confirmation",
  "writes-without-dialog": "write_confirmation",
  "function-policy-weakened": "function_policy",
};

function describe(value, setting) {
  if (Array.isArray(value)) return value.join(", ");
  if (value === null || value === undefined) return t("field.none");
  if (setting && has(`option.${setting}.${value}`)) return t(`option.${setting}.${value}`);
  if (typeof value === "number") return fmt.number(value);
  return String(value);
}

/** One finding (a risk of a change, or a point of the posture) as a sentence. */
export function findingText(prefix, finding) {
  const data = finding.data || {};
  const values = { connection: finding.connection || "" };
  for (const [key, value] of Object.entries(data)) values[key] = describe(value, SETTING[finding.code]);
  if (finding.code === "limit-raised") {
    const timed = String(data.key).includes("timeout");
    values.key = has(`field.${data.key}.label`) ? t(`field.${data.key}.label`) : data.key;
    values.before = timed ? fmt.ms(data.before) : fmt.number(data.before);
    values.after = timed && data.after === 0 ? t("risk.unlimited") : timed ? fmt.ms(data.after) : fmt.number(data.after);
  }
  if (typeof data.count === "number") values.count = data.count; // kept a number: it picks the plural
  const key = `${prefix}.${finding.code}`;
  return has(key) ? t(key, values) : finding.code;
}

export function findingRow(prefix, finding, action, { showConnection = true } = {}) {
  const helpKey = `${prefix}.${finding.code}.help`;
  return h(
    "div",
    { class: ["finding", finding.level] },
    icon(LEVEL_ICON[finding.level] || "info", t(`level.${finding.level}`)),
    h(
      "div",
      { class: "body" },
      h(
        "div",
        { class: "row gap-2" },
        h("span", { class: "strong" }, findingText(prefix, finding)),
        finding.connection && showConnection ? badge(finding.connection, "outline", "database") : null,
      ),
      has(helpKey) ? h("p", null, t(helpKey)) : null,
    ),
    action || null,
  );
}

export function diffView(lines) {
  const box = h("div", { class: "diff", tabindex: "0", role: "region", "aria-label": t("save.diff") });
  for (const line of lines) {
    if (line.startsWith("+++") || line.startsWith("---")) continue;
    const kind = line.startsWith("@@")
      ? "hunk"
      : line.startsWith("+")
        ? "add"
        : line.startsWith("-")
          ? "del"
          : null;
    box.append(h("div", { class: kind }, line || " "));
  }
  return box;
}

function explain(error) {
  if (error instanceof ApiError) {
    if (error.code === "network") return t("error.network");
    if (has(`error.${error.code}`)) return t(`error.${error.code}`);
    return error.message || t("error.generic");
  }
  return t("error.generic");
}

export { explain as errorText };

/**
 * Show what `ops` would do and, if the person agrees, save.
 * Resolves to the new configuration, or to null when nothing was saved.
 */
export async function reviewAndSave({ ops, onErrors, route = "/api/config", extra = {} }) {
  if (!ops.length && !extra.restore) {
    toast(t("save.nothing"), "warn");
    return null;
  }
  const base = store.config.file.digest;
  let preview;
  try {
    preview = extra.restore
      ? extra.preview
      : await api(`${route}/preview`, { method: "POST", body: { base, ops } });
  } catch (error) {
    toast(explain(error), "danger", { title: t("save.failed") });
    return null;
  }
  if (!extra.restore && !preview.changed) {
    toast(t("save.nothing"), "warn");
    return null;
  }

  return new Promise((resolve) => {
    let saved = null;
    const body = [];

    if (!preview.valid) {
      if (onErrors) onErrors(preview.errors);
      body.push(
        banner({ tone: "danger", title: t("save.invalid"), text: t("save.invalidHelp") }),
        h(
          "ul",
          { class: "stack gap-2" },
          preview.errors.map((error) =>
            h(
              "li",
              { class: "finding danger" },
              icon("alert-circle"),
              h(
                "div",
                { class: "body" },
                error.loc ? h("code", null, error.loc) : null,
                h("p", null, error.message),
              ),
            ),
          ),
        ),
      );
      const dialog = openDialog({
        title: t("save.review"),
        body,
        footer: [button(t("common.back"), { variant: "primary", onClick: () => dialog.close() })],
        onClose: () => resolve(null),
      });
      return;
    }

    const serious = preview.risks.filter((risk) => risk.level !== "note");
    const notes = preview.risks.filter((risk) => risk.level === "note");
    if (serious.length) {
      body.push(
        banner({ tone: "danger", title: t("save.weakens"), text: t("save.weakensHelp") }),
        h("div", null, serious.map((risk) => findingRow("risk", risk))),
      );
    } else {
      body.push(banner({ tone: "ok", title: t("save.safe"), text: t("save.safeHelp") }));
    }
    if (notes.length) {
      body.push(
        h(
          "details",
          { class: "more", open: !serious.length && notes.length <= 3 },
          h("summary", null, icon("chevron-right"), t("save.notes", { n: notes.length })),
          h("div", null, notes.map((risk) => findingRow("risk", risk))),
        ),
      );
    }
    body.push(
      h("h3", null, t("save.diff")),
      h("p", { class: "small secondary" }, t("save.diffHelp")),
      diffView(preview.diff),
    );
    const active = (store.pulse && store.pulse.active_sessions) || 0;
    body.push(
      banner({
        plain: true,
        title: t("save.whenTitle"),
        text: active ? t("save.whenActive", { n: active }) : t("save.when"),
      }),
    );

    let acknowledge = null;
    const save = button(extra.restore ? t("file.restore") : t("save.confirm"), {
      variant: serious.length ? "danger" : "primary",
      icon: "check",
      onClick: () =>
        busy(save, async () => {
          try {
            const body = { base, acknowledged: Boolean(acknowledge && acknowledge.checked) };
            const result = extra.restore
              ? await api(extra.restore, { method: "POST", body })
              : await api(`${route}/apply`, { method: "POST", body: { ...body, ops } });
            saved = result;
            useConfig(result);
            toast(
              result.saved && result.saved.backup
                ? t("save.doneBackup", { name: result.saved.backup })
                : t("save.done"),
            );
            dialog.close();
          } catch (error) {
            if (error.code === "invalid" && error.data.errors && onErrors) onErrors(error.data.errors);
            toast(explain(error), "danger", { title: t("save.failed") });
          }
        }),
    });
    if (preview.needs_acknowledgement) {
      save.disabled = true;
      const row = checkbox(t("save.acknowledge"), false, (checked) => {
        save.disabled = !checked;
      });
      acknowledge = row.input;
      body.push(row);
    }
    const dialog = openDialog({
      title: t("save.review"),
      wide: true,
      body,
      footer: [button(t("common.cancel"), { onClick: () => dialog.close() }), save],
      onClose: () => resolve(saved),
    });
  });
}

/**
 * The bar that follows a form with unsaved changes. `collect()` returns the operations;
 * `onSaved(config)` runs after a save; `onDiscard()` puts the form back.
 */
export function saveBar({ count, collect, onSaved, onDiscard, onErrors }) {
  const label = h("div", { class: "grow", role: "status" });
  const discard = button(t("common.discard"), { variant: "ghost", onClick: onDiscard });
  const review = button(t("save.reviewButton"), {
    variant: "primary",
    icon: "shield-check",
    onClick: async () => {
      if (review.disabled) return;
      review.disabled = true; // one review at a time, without a spinner behind the dialog
      try {
        const result = await reviewAndSave({ ops: collect(), onErrors });
        if (result) onSaved(result);
      } finally {
        review.disabled = false;
      }
    },
  });
  const bar = h("div", { class: "savebar", hidden: true }, label, discard, review);
  bar.update = () => {
    const changes = count();
    bar.hidden = changes === 0;
    label.textContent = t("save.pending", { n: changes });
  };
  bar.update();
  return bar;
}

// Unsaved changes are guarded inside the application (see the router in app.js), with
// the panel's own dialog. The browser's built-in "leave this page?" prompt is not used:
// nothing on this page is drawn by the browser.
let guard = null;

export function guardUnsaved(isDirty) {
  guard = isDirty;
}

export function hasUnsaved() {
  return Boolean(guard && guard());
}
