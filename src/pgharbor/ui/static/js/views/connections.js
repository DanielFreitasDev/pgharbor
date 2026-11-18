// Connections: the list, and the editor of one connection.

import { api, on } from "../api.js";
import { h, icon, mount } from "../dom.js";
import { Draft, fieldGroup, fieldRow } from "../fields.js";
import { fmt, has, t } from "../i18n.js";
import { errorText, findingRow, guardUnsaved, reviewAndSave, saveBar } from "../save.js";
import { connectionNames, errorsUnder, inheritedFor, readOnly, schema, store } from "../store.js";
import {
  badge,
  banner,
  button,
  busy,
  card,
  confirm,
  dropdown,
  empty,
  iconButton,
  kv,
  link,
  nextId,
  openDialog,
  pageHead,
  toast,
} from "../ui.js";
import { accessBadge, environmentBadge } from "./shared.js";

const GROUP_ICON = {
  identity: "info",
  where: "server",
  access: "shield",
  functions: "code",
  data: "lock",
  limits: "clock",
  pool: "layers",
};

function validName(name) {
  return new RegExp(schema().connection_name_pattern).test(name);
}

// ------------------------------------------------------------------- the test

export async function testConnection(name, control) {
  const connection = (store.config.connections || {})[name];
  if (connection && connection.needs_approval) {
    const go = await confirm({
      title: t("test.productionTitle"),
      text: t("test.productionText", { name }),
      confirmLabel: t("test.run"),
    });
    if (!go) return;
  }
  await busy(control, async () => {
    try {
      const result = await api(`/api/connections/${encodeURIComponent(name)}/test`, { method: "POST" });
      showTestResult(name, result);
    } catch (error) {
      toast(errorText(error), "danger", { title: t("test.failed") });
    }
  });
}

function showTestResult(name, result) {
  const body = result.ok
    ? [
        banner({ tone: "ok", title: t("test.ok"), text: t("test.okText", { ms: fmt.ms(result.ms) }) }),
        kv([
          [t("test.server"), `PostgreSQL ${result.postgresql}`],
          [t("test.database"), result.database],
          [t("test.user"), result.user],
          [t("test.role"), result.superuser ? t("test.superuser") : t("test.regular")],
          [t("test.replica"), result.in_recovery ? t("common.yes") : t("common.no")],
        ]),
        result.superuser ? banner({ tone: "warn", title: t("test.superuserTitle"), text: t("test.superuserText") }) : null,
      ]
    : [
        banner({ tone: "danger", title: t("test.failed") }),
        h("pre", { class: "code" }, result.error),
        h("p", { class: "small secondary" }, t("test.environmentHint")),
      ];
  const dialog = openDialog({
    title: t("test.title", { name }),
    body,
    footer: [button(t("common.close"), { variant: "primary", onClick: () => dialog.close() })],
  });
}

// -------------------------------------------------------------------- the list

export function connections(root) {
  function draw() {
    const config = store.config;
    const names = connectionNames();
    const canEdit = !readOnly() && config.file.exists && config.file.is_toml;
    const cards = names.map((name) => {
      const info = (config.connections || {})[name];
      const raw = (config.raw.connections || {})[name] || {};
      const findings = (config.posture || []).filter(
        (finding) => finding.connection === name && finding.level !== "note",
      );
      const testButton = button(t("test.button"), { icon: "plug", small: true, onClick: () => testConnection(name, testButton) });
      if (!info) testButton.disabled = true;
      return h(
        "article",
        { class: "card" },
        h(
          "div",
          { class: "card-head" },
          h(
            "div",
            null,
            h(
              "h3",
              { class: "row gap-2" },
              h("a", { href: `#/connections/${encodeURIComponent(name)}` }, name),
              config.default_connection === name ? badge(t("connections.default"), "accent") : null,
            ),
            h("p", null, raw.description || (info ? info.effective.description : "") || t("connections.noDescription")),
          ),
          info ? h("div", { class: "row gap-2" }, environmentBadge(info), accessBadge(info.effective.access)) : badge(t("connections.invalid"), "danger"),
        ),
        info
          ? kv([
              [t("connections.server"), h("span", { class: "mono" }, info.target)],
              [t("connections.approval"), info.needs_approval ? t("connections.approvalNeeded") : t("connections.approvalFree")],
              [
                t("connections.limits"),
                t("connections.limitsValue", {
                  rows: fmt.number(info.effective.max_rows),
                  timeout: fmt.ms(info.effective.statement_timeout),
                }),
              ],
              [
                t("connections.protected"),
                info.effective.protect.length
                  ? info.effective.protect.map((rule) => rule.table).join(", ")
                  : t("connections.none"),
              ],
            ])
          : null,
        findings.length
          ? h("div", null, findings.slice(0, 3).map((finding) => findingRow("posture", finding, null, { showConnection: false })))
          : null,
        h(
          "div",
          { class: "row gap-2" },
          link(canEdit ? t("common.edit") : t("common.view"), `#/connections/${encodeURIComponent(name)}`, {
            icon: canEdit ? "edit" : "eye",
            small: true,
            variant: "primary",
          }),
          testButton,
        ),
      );
    });

    const choice = dropdown(
      [{ value: "", label: t("connections.noDefault") }, ...names.map((name) => ({ value: name, label: name }))],
      config.raw.default_connection || "",
      async (next) => {
        const ops = next
          ? [{ op: "set", path: ["default_connection"], value: next }]
          : [{ op: "unset", path: ["default_connection"] }];
        const saved = await reviewAndSave({ ops });
        if (!saved) choice.setValue(store.config.raw.default_connection || "");
      },
      { label: t("connections.defaultLabel"), auto: true, disabled: !canEdit },
    );

    mount(
      root,
      pageHead({
        title: t("connections.title"),
        text: t("connections.text"),
        actions: canEdit ? link(t("connections.new"), "#/connections/new", { icon: "plus", variant: "primary" }) : null,
      }),
      names.length
        ? [
            h(
              "div",
              { class: "card tight" },
              h(
                "div",
                { class: "row" },
                h(
                  "div",
                  { class: "grow" },
                  h("div", { class: "strong" }, t("connections.defaultLabel")),
                  h("div", { class: "small secondary" }, t("connections.defaultHelp")),
                ),
                choice,
              ),
            ),
            h("div", { class: "grid" }, cards),
          ]
        : card(
            {},
            empty({
              icon: "database",
              title: t("connections.empty"),
              text: config.file.exists ? t("connections.emptyText") : t("connections.noFile"),
              action: canEdit ? link(t("connections.new"), "#/connections/new", { icon: "plus", variant: "primary" }) : null,
            }),
          ),
    );
  }

  draw();
  return on("config", draw);
}

// ------------------------------------------------------------------ the editor

export function connectionEditor(root, { name: routeName, go }) {
  const isNew = routeName === undefined;
  let name = routeName;
  let draft = null;
  let serverErrors = {};
  let bar = null;
  let newName = "";
  let saving = false;

  function build() {
    const config = store.config;
    const raw = isNew ? {} : (config.raw.connections || {})[name];
    if (!isNew && raw === undefined) {
      guardUnsaved(null);
      mount(
        root,
        pageHead({ title: name, crumbs: [{ label: t("connections.title"), href: "#/connections" }, { label: name }] }),
        card({}, empty({ icon: "database", title: t("connections.gone"), action: link(t("connections.back"), "#/connections", { variant: "primary" }) })),
      );
      return;
    }
    const prefix = `connections.${name}.`;
    const secrets = config.secrets.filter((path) => path.startsWith(prefix)).map((path) => path.slice(prefix.length));
    draft = new Draft(raw, isNew ? [] : secrets);
    serverErrors = isNew ? {} : errorsUnder(`connections.${name}`, config.errors);
    draw();
  }

  function draw() {
    const config = store.config;
    const info = isNew ? null : (config.connections || {})[name];
    const defaults = config.raw.defaults || {};
    const specs = schema().connection;
    const editable = !readOnly() && config.file.exists && config.file.is_toml;

    const ctx = {
      draft,
      readOnly: !editable,
      inherited: (key) => inheritedFor(specs.find((spec) => spec.key === key), defaults),
      errorFor: (key) => serverErrors[key] || null,
      secretPath: (key) => key,
      libpqParams: schema().libpq_params,
      productionLabels: schema().production_labels,
      environment: (key, value) => {
        const known = info && info.environment_variables[key];
        return known && known.name === value ? known.set : null;
      },
    };

    const groups = schema().connection_groups.map((group) => {
      const rows = specs.filter((spec) => spec.group === group).map((spec) => fieldRow(spec, ctx));
      return fieldGroup(`group-${group}`, t(`group.${group}`), t(`group.${group}.text`), rows);
    });

    const nameId = nextId("name");
    const nameInput = h("input", {
      id: nameId,
      class: "input medium",
      value: isNew ? newName : name,
      placeholder: "dev",
      autocomplete: "off",
      autocapitalize: "none",
      spellcheck: "false",
      "aria-describedby": `${nameId}-help`,
      on: {
        input: () => {
          newName = nameInput.value.trim();
          nameError.hidden = !newName || validName(newName);
          bar.update();
        },
      },
    });
    const nameError = h("div", { class: "field-error", hidden: true, role: "alert" }, icon("alert-circle"), t("connections.nameInvalid"));
    const nameCard = isNew
      ? card(
          { title: t("connections.nameTitle"), subtitle: t("connections.nameHelp") },
          h("label", { class: "sr-only", for: nameId }, t("connections.nameTitle")),
          nameInput,
          nameError,
        )
      : null;

    bar = saveBar({
      count: () => (isNew ? (newName ? 1 : 0) + draft.changeCount : draft.changeCount),
      collect: () =>
        isNew
          ? [{ op: "set", path: ["connections", newName], value: { ...draft.values, ...secretValues() } }]
          : draft.ops(["connections", name], { nested: ["params"] }),
      onErrors: (errors) => {
        serverErrors = errorsUnder(`connections.${isNew ? newName : name}`, errors);
        root.querySelectorAll(".field").forEach((row) => row.redraw && row.redraw());
      },
      onSaved: () => {
        saving = true;
        guardUnsaved(null);
        if (isNew) go(`#/connections/${encodeURIComponent(newName)}`);
        else build();
        saving = false;
      },
      onDiscard: () => {
        newName = "";
        build();
      },
    });
    draft.onChange(() => bar.update());
    guardUnsaved(() => !saving && (draft.dirty || (isNew && Boolean(newName))));

    const testButton = isNew ? null : button(t("test.button"), { icon: "plug", onClick: () => testConnection(name, testButton) });
    const actions = isNew || !editable
      ? testButton
      : [
          testButton,
          iconButton("edit", t("connections.rename"), { variant: null, onClick: rename }),
          iconButton("copy", t("connections.duplicate"), { variant: null, onClick: duplicate }),
          iconButton("trash", t("connections.delete"), { variant: "subtle-danger", onClick: remove }),
        ];

    const toc = h(
      "nav",
      { class: "toc", "aria-label": t("connections.sections") },
      schema().connection_groups.map((group) =>
        h("a", { href: `#group-${group}`, on: { click: (event) => jump(event, `group-${group}`) } }, icon(GROUP_ICON[group]), t(`group.${group}`)),
      ),
    );

    spy(toc);
    const posture = isNew ? [] : (config.posture || []).filter((finding) => finding.connection === name);
    mount(
      root,
      pageHead({
        title: isNew ? t("connections.newTitle") : name,
        text: isNew ? t("connections.newText") : info ? info.target : null,
        crumbs: [{ label: t("connections.title"), href: "#/connections" }, { label: isNew ? t("connections.new") : name }],
        actions,
      }),
      !editable ? banner({ title: t("readonly.title"), text: readOnly() ? t("readonly.text") : t("readonly.noFile") }) : null,
      serverErrors[""] ? banner({ tone: "danger", title: t("save.invalid"), text: serverErrors[""] }) : null,
      info ? h("div", { class: "row gap-2" }, environmentBadge(info), accessBadge(info.effective.access), info.needs_approval ? badge(t("connections.approvalNeeded"), "info", "lock") : null) : null,
      posture.length ? card({ title: t("posture.forConnection"), tight: true }, h("div", null, posture.map((finding) => findingRow("posture", finding, null, { showConnection: false })))) : null,
      h("div", { class: "split aside" }, toc, h("div", { class: "stack gap-5" }, nameCard, groups)),
      bar,
    );
    void has;
  }

  /** Credentials typed for a connection that does not exist yet go in its first save. */
  function secretValues() {
    const values = {};
    for (const [key, edit] of draft.secretEdits) {
      if (edit.action !== "set") continue;
      if (key.startsWith("params.")) {
        values.params = { ...(draft.values.params || {}), [key.slice(7)]: edit.value };
      } else {
        values[key] = edit.value;
      }
    }
    return values;
  }

  /** Mark, in the list of sections, the one being read. */
  let observer = null;
  function spy(toc) {
    if (observer) observer.disconnect();
    const visible = new Set();
    observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) visible.add(entry.target.id);
          else visible.delete(entry.target.id);
        }
        // Two sections share the top of the screen while one gives way to the next:
        // the one being read is the later of them.
        const reading = schema()
          .connection_groups.map((group) => `group-${group}`)
          .filter((id) => visible.has(id))
          .pop();
        toc.querySelectorAll("a").forEach((item) => {
          if (item.getAttribute("href") === `#${reading}`) item.setAttribute("aria-current", "true");
          else item.removeAttribute("aria-current");
        });
      },
      { rootMargin: "-80px 0px -55% 0px" },
    );
    requestAnimationFrame(() => root.querySelectorAll("section[id^='group-']").forEach((section) => observer.observe(section)));
  }

  function jump(event, id) {
    event.preventDefault(); // the address belongs to the router: scroll without changing it
    const target = document.getElementById(id);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    target.querySelector("h3")?.setAttribute("tabindex", "-1");
    target.querySelector("h3")?.focus({ preventScroll: true });
  }

  async function askName(title, text, initial) {
    return new Promise((resolve) => {
      let answer = null;
      const id = nextId("rename");
      const input = h("input", { id, class: "input", value: initial, autocomplete: "off", autocapitalize: "none", spellcheck: "false" });
      const error = h("div", { class: "field-error", hidden: true, role: "alert" }, icon("alert-circle"), "");
      const accept = button(t("common.continue"), {
        variant: "primary",
        onClick: () => {
          const value = input.value.trim();
          const problem = !validName(value)
            ? t("connections.nameInvalid")
            : connectionNames().includes(value)
              ? t("connections.nameTaken")
              : null;
          if (problem) {
            error.lastChild.textContent = problem;
            error.hidden = false;
            return;
          }
          answer = value;
          dialog.close();
        },
      });
      const dialog = openDialog({
        title,
        body: [h("p", null, text), h("label", { class: "labeled", for: id }, t("connections.nameTitle"), input), error],
        footer: [button(t("common.cancel"), { onClick: () => dialog.close() }), accept],
        onClose: () => resolve(answer),
      });
      input.addEventListener("keydown", (event) => event.key === "Enter" && accept.click());
      input.focus();
      input.select();
    });
  }

  async function rename() {
    if (draft.dirty) return toast(t("connections.saveFirst"), "warn");
    const next = await askName(t("connections.rename"), t("connections.renameText"), name);
    if (!next) return undefined;
    const saved = await reviewAndSave({ ops: [{ op: "rename", path: ["connections", name], to: next }] });
    if (saved) go(`#/connections/${encodeURIComponent(next)}`);
    return undefined;
  }

  async function duplicate() {
    if (draft.dirty) return toast(t("connections.saveFirst"), "warn");
    const next = await askName(t("connections.duplicate"), t("connections.duplicateText"), `${name}-copy`.slice(0, 32));
    if (!next) return undefined;
    const saved = await reviewAndSave({
      ops: [{ op: "set", path: ["connections", next], value: store.config.raw.connections[name] }],
    });
    if (saved) go(`#/connections/${encodeURIComponent(next)}`);
    return undefined;
  }

  async function remove() {
    const config = store.config;
    const usedBy = Object.entries(config.raw.custom_tools || {})
      .filter(([, tool]) => (tool.connections || []).includes(name))
      .map(([tool]) => tool);
    const isDefault = config.raw.default_connection === name;
    if (usedBy.length || isDefault) {
      const dialog = openDialog({
        title: t("connections.delete"),
        body: [
          banner({ tone: "warn", title: t("connections.inUse") }),
          h(
            "ul",
            { class: "stack gap-2" },
            isDefault ? h("li", null, t("connections.inUseDefault")) : null,
            usedBy.map((tool) => h("li", null, t("connections.inUseTool", { tool }))),
          ),
        ],
        footer: [button(t("common.close"), { variant: "primary", onClick: () => dialog.close() })],
      });
      return;
    }
    const sure = await confirm({
      title: t("connections.delete"),
      text: t("connections.deleteText", { name }),
      confirmLabel: t("common.delete"),
      tone: "danger",
      typed: name,
    });
    if (!sure) return;
    guardUnsaved(null);
    const saved = await reviewAndSave({ ops: [{ op: "unset", path: ["connections", name] }] });
    if (saved) go("#/connections");
  }

  build();
  const off = on("config", () => {
    if (!draft || !draft.dirty) build();
  });
  return () => {
    off();
    if (observer) observer.disconnect();
    guardUnsaved(null);
  };
}
