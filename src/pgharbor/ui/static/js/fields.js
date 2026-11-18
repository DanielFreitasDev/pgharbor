// Forms for the configuration.
//
// A Draft holds the keys a person actually wrote in one section of the file. A key
// that is absent is not empty: it is inherited (from [defaults], or from pgharbor's
// own default), and the form says so. Saving sends only what changed, one operation
// per key, so nothing else in the file moves.

import { h, icon, mount } from "./dom.js";
import { fmt, has, t } from "./i18n.js";
import { button, dropdown, iconButton, nextId, segmented, toggle } from "./ui.js";

export function equal(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

export function clone(value) {
  return value === undefined ? undefined : JSON.parse(JSON.stringify(value));
}

export class Draft {
  /**
   * @param original the section as written in the file, without credentials
   * @param secrets  names of the credentials that are set in the file
   */
  constructor(original, secrets = []) {
    this.original = clone(original || {});
    this.values = clone(original || {});
    this.existing = new Set(secrets);
    this.secretEdits = new Map();
    this.listeners = new Set();
  }

  has(key) {
    return Object.prototype.hasOwnProperty.call(this.values, key);
  }

  get(key) {
    return this.values[key];
  }

  set(key, value) {
    this.values[key] = value;
    this.changedNow();
  }

  unset(key) {
    delete this.values[key];
    this.changedNow();
  }

  /** A credential: set to a new value, removed, or left as it is in the file. */
  setSecret(key, value) {
    this.secretEdits.set(key, { action: "set", value });
    this.changedNow();
  }

  removeSecret(key) {
    if (this.existing.has(key)) this.secretEdits.set(key, { action: "remove" });
    else this.secretEdits.delete(key);
    this.changedNow();
  }

  keepSecret(key) {
    this.secretEdits.delete(key);
    this.changedNow();
  }

  secretState(key) {
    const edit = this.secretEdits.get(key);
    if (edit) return edit.action === "set" ? "new" : "removed";
    return this.existing.has(key) ? "set" : "unset";
  }

  isChanged(key) {
    return !equal(this.original[key], this.values[key]) || this.secretEdits.has(key);
  }

  get dirty() {
    return !equal(this.original, this.values) || this.secretEdits.size > 0;
  }

  get changeCount() {
    const keys = new Set([...Object.keys(this.original), ...Object.keys(this.values)]);
    let count = this.secretEdits.size;
    for (const key of keys) if (!equal(this.original[key], this.values[key])) count += 1;
    return count;
  }

  onChange(listener) {
    this.listeners.add(listener);
  }

  changedNow() {
    for (const listener of this.listeners) listener();
  }

  /**
   * The changes as operations under `base` (a path such as ["connections", "dev"]).
   * `nested` names keys that are tables edited one entry at a time.
   */
  ops(base, { nested = [] } = {}) {
    const ops = [];
    const keys = new Set([...Object.keys(this.original), ...Object.keys(this.values)]);
    for (const key of keys) {
      const before = this.original[key];
      const after = this.values[key];
      if (equal(before, after)) continue;
      if (nested.includes(key)) {
        const inner = new Set([...Object.keys(before || {}), ...Object.keys(after || {})]);
        for (const name of inner) {
          const was = (before || {})[name];
          const now = (after || {})[name];
          if (equal(was, now)) continue;
          if (now === undefined) ops.push({ op: "unset", path: [...base, key, name] });
          else ops.push({ op: "set", path: [...base, key, name], value: now });
        }
      } else if (after === undefined) {
        ops.push({ op: "unset", path: [...base, key] });
      } else {
        ops.push({ op: "set", path: [...base, key], value: after });
      }
    }
    for (const [key, edit] of this.secretEdits) {
      const path = [...base, ...key.split(".")];
      if (edit.action === "set") ops.push({ op: "set", path, value: edit.value });
      else ops.push({ op: "unset", path });
    }
    return ops;
  }
}

// ------------------------------------------------------------------- durations

const DURATION = /^\s*(\d+(?:\.\d+)?)\s*(ms|s|sec|min|m|h)?\s*$/i;
const FACTOR = { ms: 1, s: 1000, sec: 1000, min: 60000, m: 60000, h: 3600000 };

/** Milliseconds for `30000`, "30s", "2min"... exactly as the server reads them. */
export function durationMs(value) {
  if (typeof value === "number") return value;
  if (typeof value !== "string") return null;
  const match = DURATION.exec(value);
  if (!match) return null;
  return Math.trunc(parseFloat(match[1]) * FACTOR[(match[2] || "ms").toLowerCase()]);
}

// ------------------------------------------------------------------ validation

export function problem(spec, value) {
  if (value === undefined) return null;
  if (spec.kind === "int" || spec.kind === "float") {
    if (typeof value !== "number" || Number.isNaN(value)) return t("validation.number");
    if (spec.kind === "int" && !Number.isInteger(value)) return t("validation.integer");
    if (spec.min !== undefined && value < spec.min) return t("validation.min", { min: spec.min });
    if (spec.max !== undefined && value > spec.max) return t("validation.max", { max: spec.max });
    if (spec.above !== undefined && value <= spec.above) {
      return t("validation.above", { min: spec.above });
    }
  }
  if (spec.kind === "duration" && durationMs(value) === null) return t("validation.duration");
  if (spec.kind === "env" && value && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(value)) {
    return t("validation.env");
  }
  return null;
}

// --------------------------------------------------------------------- labels

export function fieldLabel(key) {
  return has(`field.${key}.label`) ? t(`field.${key}.label`) : key;
}

export function fieldHelp(key) {
  return has(`field.${key}.help`) ? t(`field.${key}.help`) : "";
}

export function optionLabel(key, value) {
  const id = `option.${key}.${value}`;
  return has(id) ? t(id) : String(value);
}

function show(spec, value) {
  if (value === null || value === undefined || value === "") return t("field.none");
  if (spec.kind === "bool") return value ? t("common.on") : t("common.off");
  if (spec.kind === "choice") return optionLabel(spec.key, value);
  if (spec.kind === "duration") {
    const ms = durationMs(value);
    return ms === 0 && has(`field.${spec.key}.zero`) ? t(`field.${spec.key}.zero`) : fmt.ms(ms);
  }
  if (spec.kind === "int" || spec.kind === "float") return fmt.number(value);
  if (Array.isArray(value)) {
    if (!value.length) return t("field.emptyList");
    return value.every((entry) => typeof entry !== "object") ? value.join(", ") : String(value.length);
  }
  if (typeof value === "object") {
    const names = Object.keys(value);
    return names.length ? names.join(", ") : t("field.emptyList");
  }
  return String(value);
}

// ------------------------------------------------------------------ list editor

/** A list of short texts as removable chips, with a box to add more. */
export function chipsEditor(values, onChange, { placeholder, label, readOnly, mono = true } = {}) {
  const box = h("div", { class: "stack gap-2" });
  const input = h("input", {
    class: "input",
    placeholder: placeholder || "",
    "aria-label": label || placeholder || "",
    autocomplete: "off",
    autocapitalize: "none",
    spellcheck: "false",
    enterkeyhint: "done",
  });
  let list = [...(values || [])];

  function add() {
    const fresh = input.value
      .split(/[\n,]/)
      .map((entry) => entry.trim())
      .filter((entry) => entry && !list.includes(entry));
    if (!fresh.length) return;
    list = [...list, ...fresh];
    input.value = "";
    onChange(list);
    draw();
    input.focus();
  }

  function draw() {
    const chips = h(
      "div",
      { class: "chips" },
      list.map((entry) =>
        h(
          "span",
          { class: ["chip", !mono && "plain"] },
          h("span", { class: "truncate" }, entry),
          readOnly
            ? null
            : h(
                "button",
                {
                  type: "button",
                  "aria-label": t("common.removeItem", { item: entry }),
                  on: {
                    click: () => {
                      list = list.filter((other) => other !== entry);
                      onChange(list);
                      draw();
                    },
                  },
                },
                icon("x"),
              ),
        ),
      ),
    );
    mount(
      box,
      list.length ? chips : h("div", { class: "small muted" }, t("field.emptyList")),
      readOnly
        ? null
        : h(
            "div",
            { class: "input-group" },
            input,
            button(t("common.add"), { icon: "plus", onClick: add }),
          ),
    );
  }

  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      add();
    }
  });
  input.addEventListener("blur", add);
  draw();
  return box;
}

// ------------------------------------------------------------------- field row

/**
 * One key of the configuration as a row: what it is, where its value comes from, the
 * control to change it and the way back to the inherited value.
 *
 * ctx: { draft, inherited(key) -> {value, source}, errorFor(key), readOnly, secretPath(key) }
 */
export function fieldRow(spec, ctx) {
  const row = h("div", { class: "field", data: { key: spec.key } });
  const helpId = nextId("help");
  const { draft } = ctx;

  function draw() {
    const set = draft.has(spec.key);
    const inherited = ctx.inherited ? ctx.inherited(spec.key) : null;
    const value = set ? draft.get(spec.key) : inherited ? inherited.value : undefined;
    const local = problem(spec, set ? draft.get(spec.key) : undefined);
    const remote = ctx.errorFor ? ctx.errorFor(spec.key) : null;
    const isSecret = spec.kind === "secret";

    let source = null;
    if (isSecret) {
      source = null;
    } else if (set) {
      source = h("span", { class: "source own" }, t("field.source.own"));
    } else if (inherited && inherited.source === "defaults") {
      source = h("span", { class: "source" }, t("field.source.defaults"));
    } else {
      source = h("span", { class: "source" }, t("field.source.builtin"));
    }

    const canReset = set && !isSecret && !spec.required && !ctx.readOnly;
    const side = h("div", { class: "field-side" });
    side.append(control(spec, ctx, { value, set, inherited, helpId, redraw: draw }));

    const notes = extraNotes(spec, ctx, { value, set, inherited });
    for (const note of notes) side.append(note);
    if (local || remote) {
      side.append(
        h("div", { class: "field-error", role: "alert" }, icon("alert-circle"), local || remote),
      );
    }
    if (canReset) {
      side.append(
        h(
          "div",
          null,
          button(
            t("field.reset", {
              value: inherited ? show(spec, inherited.value) : t("field.none"),
            }),
            {
              icon: "undo",
              variant: "ghost",
              small: true,
              onClick: () => {
                draft.unset(spec.key);
                draw();
              },
            },
          ),
        ),
      );
    }

    const changed = isSecret
      ? draft.secretEdits.has(ctx.secretPath ? ctx.secretPath(spec.key) : spec.key)
      : draft.isChanged(spec.key);
    row.classList.toggle("changed", changed);
    mount(
      row,
      h(
        "div",
        { class: "field-text" },
        h(
          "div",
          { class: "field-head" },
          h("span", { class: "field-label" }, fieldLabel(spec.key)),
          h("span", { class: "field-key" }, spec.key),
          source,
        ),
        h("div", { class: "field-help", id: helpId }, fieldHelp(spec.key)),
      ),
      side,
    );
  }

  draw();
  row.redraw = draw;
  return row;
}

function extraNotes(spec, ctx, { value }) {
  const notes = [];
  const note = (text, tone) =>
    h("div", { class: ["field-note", tone] }, icon(tone === "warn" ? "alert-triangle" : "info"), text);
  if (spec.kind === "duration" && value !== undefined && durationMs(value) !== null) {
    const ms = durationMs(value);
    if (!(ms === 0 && has(`field.${spec.key}.zero`))) notes.push(note(`= ${fmt.ms(ms)}`));
    else notes.push(note(t(`field.${spec.key}.zero`)));
  }
  const warnKey = `field.${spec.key}.warn.${value}`;
  if ((typeof value === "string" || typeof value === "boolean") && has(warnKey)) {
    notes.push(note(t(warnKey), "warn"));
  }
  if (spec.kind === "env" && value && ctx.environment) {
    const known = ctx.environment(spec.key, value);
    if (known === true) notes.push(note(t("field.env.set", { name: value })));
    else if (known === false) notes.push(note(t("field.env.unset", { name: value }), "warn"));
  }
  if (spec.kind === "environment" && value && ctx.productionLabels) {
    if (ctx.productionLabels.includes(String(value).trim().toLowerCase())) {
      notes.push(note(t("field.environment.isProduction")));
    }
  }
  return notes;
}

function control(spec, ctx, state) {
  const { draft, readOnly } = ctx;
  const { value, set, helpId, redraw } = state;
  const key = spec.key;
  const label = fieldLabel(key);

  switch (spec.kind) {
    case "bool": {
      const control = toggle(Boolean(value), {
        label,
        describedBy: helpId,
        disabled: readOnly,
        onChange: (next) => {
          draft.set(key, next);
          redraw();
        },
      });
      return h(
        "div",
        { class: "switch-row" },
        control,
        h("span", { class: "strong" }, value ? t("common.on") : t("common.off")),
      );
    }

    case "choice": {
      const options = spec.options.map((option) => ({
        value: option,
        label: optionLabel(key, option),
      }));
      if (spec.optional) {
        options.unshift({ value: "", label: t(`option.${key}.auto`) });
      }
      if (options.length > 4) {
        return dropdown(
          options,
          value === null || value === undefined ? "" : String(value),
          (next) => {
            if (next === "") draft.unset(key);
            else draft.set(key, next);
            redraw();
          },
          { label, describedBy: helpId, disabled: readOnly },
        );
      }
      const chosen = value === null || value === undefined ? "" : value;
      const group = segmented(options, chosen, (next) => {
        if (next === "") draft.unset(key);
        else draft.set(key, next);
        redraw();
      }, { label });
      if (!set) group.querySelector('[aria-checked="true"]')?.classList.add("inherited");
      if (readOnly) group.querySelectorAll("button").forEach((b) => (b.disabled = true));
      return group;
    }

    case "int":
    case "float": {
      const input = h("input", {
        class: ["input", "short", !set && "inherited"],
        type: "text",
        inputmode: spec.kind === "int" ? "numeric" : "decimal",
        autocomplete: "off",
        "aria-label": label,
        "aria-describedby": helpId,
        placeholder: value === null || value === undefined ? t("field.none") : String(value),
        readonly: readOnly,
      });
      if (set) input.value = String(draft.get(key));
      input.addEventListener("change", () => {
        const text = input.value.trim().replace(",", ".");
        if (text === "") draft.unset(key);
        else draft.set(key, /^-?\d+(\.\d+)?$/.test(text) ? Number(text) : Number.NaN);
        redraw();
      });
      return input;
    }

    case "secret":
      return secretControl(spec, ctx, redraw);

    case "list": {
      if (!set) {
        const shown = value || [];
        return h(
          "div",
          { class: "stack gap-2" },
          shown.length
            ? h(
                "div",
                { class: "chips" },
                shown.map((entry) => h("span", { class: "chip inherited plain mono" }, entry)),
              )
            : h("div", { class: "small muted" }, t("field.emptyList")),
          readOnly
            ? null
            : h(
                "div",
                null,
                button(t("field.customize"), {
                  icon: "edit",
                  small: true,
                  onClick: () => {
                    draft.set(key, [...shown]);
                    redraw();
                  },
                }),
              ),
        );
      }
      return chipsEditor(draft.get(key), (next) => draft.set(key, next), {
        label,
        placeholder: has(`field.${key}.placeholder`) ? t(`field.${key}.placeholder`) : "",
        readOnly,
      });
    }

    case "protect":
      return protectEditor(spec, ctx);

    case "params":
      return paramsEditor(spec, ctx);

    default: {
      // text, env, path, duration, environment
      const input = h("input", {
        class: ["input", spec.kind === "duration" && "short", !set && "inherited"],
        type: "text",
        "aria-label": label,
        "aria-describedby": helpId,
        autocomplete: "off",
        autocapitalize: "none",
        spellcheck: "false",
        placeholder:
          value === null || value === undefined || value === ""
            ? has(`field.${key}.placeholder`)
              ? t(`field.${key}.placeholder`)
              : t("field.none")
            : String(value),
        readonly: readOnly,
      });
      if (set) input.value = String(draft.get(key));
      input.addEventListener("change", () => {
        const text = input.value.trim();
        if (text === "") {
          draft.unset(key);
        } else if (spec.kind === "duration" && /^\d+$/.test(text)) {
          draft.set(key, Number(text)); // a bare number is milliseconds, as in the file
        } else {
          draft.set(key, text);
        }
        redraw();
      });
      if (spec.kind !== "environment" || readOnly) return input;
      // The usual names, one tap away; any other name can still be typed.
      return h(
        "div",
        { class: "stack gap-2 grow" },
        input,
        h(
          "div",
          { class: "row gap-2" },
          ["development", "staging", "production"].map((entry) =>
            button(optionLabel("environment", entry), {
              small: true,
              variant: value === entry ? "primary" : null,
              onClick: () => {
                draft.set(key, entry);
                redraw();
              },
            }),
          ),
        ),
      );
    }
  }
}

function secretControl(spec, ctx, redraw) {
  const { draft, readOnly } = ctx;
  const path = ctx.secretPath ? ctx.secretPath(spec.key) : spec.key;
  const state = draft.secretState(path);
  const tone = { set: "info", new: "accent", removed: "warn", unset: null }[state];
  const status = h(
    "span",
    { class: ["badge", tone] },
    icon(state === "unset" || state === "removed" ? "x" : "lock"),
    t(`secret.${state}`),
  );
  if (readOnly) return status;

  const box = h("div", { class: "stack gap-2" });
  const input = h("input", {
    class: "input",
    type: "password",
    autocomplete: "new-password",
    "aria-label": t("secret.newValue", { name: fieldLabel(spec.key) }),
    placeholder: t("secret.placeholder"),
    spellcheck: "false",
    autocapitalize: "none",
  });
  const reveal = iconButton("eye", t("secret.show"), {
    onClick: () => {
      input.type = input.type === "password" ? "text" : "password";
    },
  });
  const apply = button(t("secret.use"), { variant: "primary", type: "submit" });
  // A form, so that Enter confirms; it is never sent anywhere.
  const editor = h(
    "form",
    {
      class: "input-group",
      hidden: true,
      autocomplete: "off",
      on: {
        submit: (event) => {
          event.preventDefault();
          if (!input.value) return;
          draft.setSecret(path, input.value);
          redraw();
        },
      },
    },
    input,
    reveal,
    apply,
  );
  const actions = h("div", { class: "row gap-2" });
  if (state === "new" || state === "removed") {
    actions.append(
      button(t("secret.keep"), {
        icon: "undo",
        small: true,
        onClick: () => {
          draft.keepSecret(path);
          redraw();
        },
      }),
    );
  } else {
    actions.append(
      button(state === "set" ? t("secret.replace") : t("secret.define"), {
        icon: "key",
        small: true,
        onClick: () => {
          editor.hidden = false;
          input.focus();
        },
      }),
    );
    if (state === "set") {
      actions.append(
        button(t("secret.remove"), {
          icon: "trash",
          small: true,
          variant: "ghost",
          onClick: () => {
            draft.removeSecret(path);
            redraw();
          },
        }),
      );
    }
  }
  box.append(h("div", { class: "row gap-2" }, status), actions, editor);
  return box;
}

// ------------------------------------------------------------- protected tables

function protectEditor(spec, ctx) {
  const { draft, readOnly } = ctx;
  const box = h("div", { class: "stack gap-3" });

  function rules() {
    return clone(draft.get("protect") || []);
  }

  function update(index, change) {
    const list = rules();
    change(list[index]);
    for (const key of Object.keys(list[index])) {
      const entry = list[index][key];
      if (entry === "" || entry === null || (Array.isArray(entry) && !entry.length)) {
        delete list[index][key];
      }
    }
    draft.set("protect", list);
  }

  function draw() {
    const list = rules();
    mount(
      box,
      list.length
        ? list.map((rule, index) => ruleCard(rule, index))
        : h("div", { class: "small muted" }, t("protect.none")),
      readOnly
        ? null
        : h(
            "div",
            null,
            button(t("protect.add"), {
              icon: "plus",
              onClick: () => {
                draft.set("protect", [...rules(), { table: "" }]);
                draw();
                box.querySelector(".subcard:last-of-type input")?.focus();
              },
            }),
          ),
    );
  }

  function ruleCard(rule, index) {
    const error = ctx.errorFor ? ctx.errorFor(`protect.${index}`) : null;
    const tableId = nextId("table");
    const rowsId = nextId("rows");
    const noteId = nextId("note");
    const table = h("input", {
      id: tableId,
      class: "input",
      value: rule.table || "",
      placeholder: "schema.table",
      autocomplete: "off",
      autocapitalize: "none",
      spellcheck: "false",
      readonly: readOnly,
      on: { change: () => update(index, (r) => (r.table = table.value.trim())) },
    });
    const maxRows = h("input", {
      id: rowsId,
      class: "input short",
      type: "text",
      inputmode: "numeric",
      autocomplete: "off",
      value: rule.max_rows ?? "",
      placeholder: t("field.none"),
      readonly: readOnly,
      on: {
        change: () =>
          update(index, (r) => {
            if (maxRows.value.trim() === "") delete r.max_rows;
            else r.max_rows = Number(maxRows.value);
          }),
      },
    });
    const note = h("input", {
      id: noteId,
      class: "input",
      value: rule.note || "",
      placeholder: t("protect.notePlaceholder"),
      readonly: readOnly,
      on: { change: () => update(index, (r) => (r.note = note.value.trim())) },
    });
    return h(
      "div",
      { class: "subcard" },
      h(
        "div",
        { class: "row nowrap-row" },
        h("label", { class: "labeled grow", for: tableId }, fieldLabel("table"), table),
        readOnly
          ? null
          : iconButton("trash", t("protect.remove"), {
              onClick: () => {
                const list = rules();
                list.splice(index, 1);
                if (list.length) draft.set("protect", list);
                else draft.unset("protect");
                draw();
              },
            }),
      ),
      h("div", { class: "small secondary" }, fieldHelp("table")),
      h(
        "div",
        { class: "labeled" },
        fieldLabel("deny_columns"),
        chipsEditor(rule.deny_columns || [], (next) => update(index, (r) => (r.deny_columns = next)), {
          label: fieldLabel("deny_columns"),
          placeholder: t("protect.columnPlaceholder"),
          readOnly,
        }),
        h("span", { class: "small secondary" }, fieldHelp("deny_columns")),
      ),
      h(
        "div",
        { class: "switch-row" },
        toggle(Boolean(rule.require_index_filter), {
          label: fieldLabel("require_index_filter"),
          disabled: readOnly,
          onChange: (next) =>
            update(index, (r) => {
              if (next) r.require_index_filter = true;
              else delete r.require_index_filter;
            }),
        }),
        h(
          "div",
          { class: "grow" },
          h("div", { class: "strong small" }, fieldLabel("require_index_filter")),
          h("div", { class: "small secondary" }, fieldHelp("require_index_filter")),
        ),
      ),
      h("label", { class: "labeled", for: rowsId }, fieldLabel("protect_max_rows"), maxRows),
      h(
        "label",
        { class: "labeled", for: noteId },
        fieldLabel("note"),
        note,
        h("span", { class: "small secondary" }, fieldHelp("note")),
      ),
      error ? h("div", { class: "field-error", role: "alert" }, icon("alert-circle"), error) : null,
    );
  }

  draw();
  return box;
}

// -------------------------------------------------------- connection parameters

function paramsEditor(spec, ctx) {
  const { draft, readOnly } = ctx;
  const box = h("div", { class: "stack gap-2" });
  const known = ctx.libpqParams || [];

  function current() {
    return clone(draft.get("params") || {});
  }

  function commit(params) {
    if (Object.keys(params).length) draft.set("params", params);
    else draft.unset("params");
  }

  function draw() {
    const params = current();
    const names = Object.keys(params);
    const free = known.filter((name) => !names.includes(name) && name !== "sslpassword");
    let picked = "";
    const picker = dropdown(
      free.map((name) => ({ value: name, label: name })),
      "",
      (next) => {
        picked = next;
      },
      { label: t("params.add"), placeholder: t("params.choose") },
    );
    mount(
      box,
      names.length
        ? names.map((name) => {
            const input = h("input", {
              class: "input",
              value: String(params[name]),
              "aria-label": name,
              autocomplete: "off",
              autocapitalize: "none",
              spellcheck: "false",
              readonly: readOnly,
              on: {
                change: () => {
                  const next = current();
                  next[name] = input.value.trim();
                  commit(next);
                },
              },
            });
            return h(
              "div",
              { class: "input-group" },
              h("code", { class: "nowrap" }, name),
              input,
              readOnly
                ? null
                : iconButton("trash", t("common.removeItem", { item: name }), {
                    onClick: () => {
                      const next = current();
                      delete next[name];
                      commit(next);
                      draw();
                    },
                  }),
            );
          })
        : h("div", { class: "small muted" }, t("params.none")),
      readOnly
        ? null
        : h(
            "div",
            { class: "input-group" },
            picker,
            button(t("common.add"), {
              icon: "plus",
              onClick: () => {
                if (!picked) return;
                const next = current();
                next[picked] = "";
                commit(next);
                draw();
                box.querySelector(".input-group:nth-last-of-type(2) input")?.focus();
              },
            }),
          ),
      secretRow(),
    );
  }

  function secretRow() {
    const path = "params.sslpassword";
    const state = draft.secretState(path);
    if (state === "unset" && readOnly) return null;
    return h(
      "div",
      { class: "subcard" },
      h("div", { class: "row gap-2" }, h("code", null, "sslpassword"), h("span", { class: "small secondary" }, t("params.sslpassword"))),
      secretControl({ key: "sslpassword" }, { ...ctx, secretPath: () => path }, draw),
    );
  }

  draw();
  return box;
}

/** A titled group of rows, with an anchor so that a list of sections can link to it. */
export function fieldGroup(id, title, text, rows) {
  return h(
    "section",
    { class: "card", id, "aria-labelledby": `${id}-title` },
    h(
      "div",
      { class: "card-head" },
      h("div", null, h("h3", { id: `${id}-title` }, title), text ? h("p", null, text) : null),
    ),
    h("div", null, rows),
  );
}
