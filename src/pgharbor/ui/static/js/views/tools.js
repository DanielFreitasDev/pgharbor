// Tools: what the agent is offered, switched on and off, and the custom tools.

import { api, on } from "../api.js";
import { h, icon, mount } from "../dom.js";
import { chipsEditor, clone, equal, fieldHelp, fieldLabel } from "../fields.js";
import { has, t } from "../i18n.js";
import { errorText, guardUnsaved, reviewAndSave, saveBar } from "../save.js";
import { connectionNames, readOnly, schema, store } from "../store.js";
import {
  autosize,
  badge,
  banner,
  button,
  card,
  checkbox,
  confirm,
  dropdown,
  empty,
  iconButton,
  loading,
  nextId,
  openDialog,
  pageHead,
  toggle,
} from "../ui.js";

const GROUP_ICON = {
  core: "bolt",
  schema: "table",
  data: "database",
  performance: "activity",
  health: "shield-check",
  replication: "layers",
  security: "lock",
  extensions: "plug",
  admin: "toolbox",
  custom: "code",
};

export function tools(root) {
  let listed = null;
  let disabled = [];
  let disabledGroups = [];
  let original = { disabled: [], groups: [] };
  let search = "";
  let saving = false;
  const body = h("div", { class: "stack gap-5" }, loading());
  let bar = null;

  function editable() {
    const config = store.config;
    return !readOnly() && config.file.exists && config.file.is_toml;
  }

  async function load() {
    try {
      listed = (await api("/api/tools")).tools;
      const raw = store.config.raw.tools || {};
      disabled = [...(raw.disabled || [])];
      disabledGroups = [...(raw.disabled_groups || [])];
      original = { disabled: [...disabled], groups: [...disabledGroups] };
      draw();
    } catch (error) {
      mount(body, banner({ tone: "danger", title: t("error.load"), text: errorText(error) }));
    }
  }

  function dirty() {
    return !equal([...disabled].sort(), [...original.disabled].sort()) || !equal([...disabledGroups].sort(), [...original.groups].sort());
  }

  function changeCount() {
    const diff = (a, b) => a.filter((entry) => !b.includes(entry)).length + b.filter((entry) => !a.includes(entry)).length;
    return diff(disabled, original.disabled) + diff(disabledGroups, original.groups);
  }

  function ops() {
    const out = [];
    const put = (key, list, before) => {
      if (equal([...list].sort(), [...before].sort())) return;
      if (list.length) out.push({ op: "set", path: ["tools", key], value: list });
      else out.push({ op: "unset", path: ["tools", key] });
    };
    put("disabled", disabled, original.disabled);
    put("disabled_groups", disabledGroups, original.groups);
    return out;
  }

  function stateOf(tool) {
    if (disabledGroups.includes(tool.group)) return ["off", "group-disabled"];
    if (disabled.includes(tool.name)) return ["off", "disabled"];
    if (tool.reason === "no-writable-connection" || tool.reason === "no-admin-connection") return ["hidden", tool.reason];
    return ["on", null];
  }

  function draw() {
    const canEdit = editable();
    const needle = search.trim().toLowerCase();
    const groups = schema().tool_groups.filter((group) => listed.some((tool) => tool.group === group));
    const on = listed.filter((tool) => stateOf(tool)[0] === "on").length;

    const groupCards = groups.map((group) => {
      const members = listed.filter(
        (tool) =>
          tool.group === group &&
          (!needle || tool.name.toLowerCase().includes(needle) || tool.description.toLowerCase().includes(needle) || tool.title.toLowerCase().includes(needle)),
      );
      if (!members.length) return null;
      const groupOff = disabledGroups.includes(group);
      const rows = members.map((tool) => {
        const [state, reason] = stateOf(tool);
        const first = tool.description.split("\n")[0];
        const rest = tool.description.split("\n").slice(1).join("\n").trim();
        return h(
          "div",
          { class: "field" },
          h(
            "div",
            { class: "field-text" },
            h(
              "div",
              { class: "field-head" },
              h("span", { class: "field-label mono" }, tool.name),
              tool.writes ? badge(t("tools.writes"), "warn", "edit") : null,
              tool.destructive ? badge(t("tools.destructive"), "danger", "alert-triangle") : null,
            ),
            h("div", { class: "field-help" }, first),
            state === "hidden" ? h("div", { class: "field-note" }, icon("eye"), t(`tools.reason.${reason}`)) : null,
            rest
              ? h("details", { class: "more" }, h("summary", { class: "small" }, icon("chevron-right"), t("tools.more")), h("pre", { class: "code" }, rest))
              : null,
          ),
          h(
            "div",
            { class: "field-side" },
            h(
              "div",
              { class: "switch-row" },
              toggle(state !== "off", {
                label: t("tools.switch", { name: tool.name }),
                disabled: !canEdit || groupOff,
                onChange: (next) => {
                  disabled = next ? disabled.filter((name) => name !== tool.name) : [...disabled, tool.name];
                  draw();
                },
              }),
              h("span", { class: "strong" }, state === "off" ? t("common.off") : t("common.on")),
              groupOff ? h("span", { class: "small muted" }, t("tools.byGroup")) : null,
            ),
          ),
        );
      });
      const shown = members.filter((tool) => stateOf(tool)[0] === "on").length;
      return h(
        "section",
        { class: "card" },
        h(
          "div",
          { class: "card-head" },
          h(
            "div",
            { class: "row nowrap-row" },
            h("span", { class: ["avatar", groupOff && "neutral"] }, icon(GROUP_ICON[group] || "toolbox")),
            h(
              "div",
              { class: "grow" },
              h("h3", null, has(`toolgroup.${group}`) ? t(`toolgroup.${group}`) : group),
              h("p", null, t("tools.groupCount", { on: shown, total: members.length })),
            ),
          ),
          h(
            "div",
            { class: "switch-row" },
            h("span", { class: "small secondary" }, t("tools.wholeGroup")),
            toggle(!groupOff, {
              label: t("tools.groupSwitch", { name: group }),
              disabled: !canEdit,
              onChange: (next) => {
                disabledGroups = next ? disabledGroups.filter((name) => name !== group) : [...disabledGroups, group];
                draw();
              },
            }),
          ),
        ),
        h("div", null, rows),
      );
    });

    const searchId = nextId("search");
    const input = h("input", {
      id: searchId,
      class: "input",
      value: search,
      placeholder: t("tools.search"),
      "aria-label": t("tools.search"),
      autocomplete: "off",
      spellcheck: "false",
      on: {
        input: () => {
          search = input.value;
          draw();
          const again = document.getElementById(searchId);
          again.focus();
          again.setSelectionRange(search.length, search.length);
        },
      },
    });

    bar = saveBar({
      count: changeCount,
      collect: ops,
      onSaved: () => {
        saving = true;
        load();
        saving = false;
      },
      onDiscard: () => {
        disabled = [...original.disabled];
        disabledGroups = [...original.groups];
        draw();
      },
    });
    guardUnsaved(() => !saving && dirty());

    mount(
      body,
      !canEdit ? banner({ title: t("readonly.title"), text: readOnly() ? t("readonly.text") : t("readonly.noFile") }) : null,
      h(
        "div",
        { class: "filters" },
        h("div", { class: "search" }, icon("search"), input),
        badge(t("tools.summary", { on, total: listed.length }), "accent"),
      ),
      customCard(canEdit),
      groupCards,
      groupCards.every((entry) => entry === null) ? card({}, empty({ icon: "search", title: t("tools.noMatch") })) : null,
      bar,
    );
    bar.update();
  }

  // ---------------------------------------------------------------- custom tools

  function customCard(canEdit) {
    const custom = store.config.raw.custom_tools || {};
    const names = Object.keys(custom);
    return card(
      {
        title: t("custom.title"),
        subtitle: t("custom.text"),
        actions: canEdit ? button(t("custom.new"), { icon: "plus", variant: "primary", small: true, onClick: () => editCustom(null) }) : null,
      },
      names.length
        ? h(
            "div",
            null,
            names.map((name) =>
              h(
                "div",
                { class: "finding note" },
                icon("code"),
                h(
                  "div",
                  { class: "body" },
                  h("span", { class: "strong mono" }, `pgx_${name}`),
                  h("p", null, custom[name].description || ""),
                  h(
                    "div",
                    { class: "row gap-2" },
                    (custom[name].connections || []).length
                      ? custom[name].connections.map((connection) => badge(connection, "outline", "database"))
                      : badge(t("custom.everyConnection"), "outline", "database"),
                  ),
                ),
                canEdit
                  ? h(
                      "div",
                      { class: "row gap-1 nowrap-row" },
                      iconButton("edit", t("common.edit"), { onClick: () => editCustom(name) }),
                      iconButton("trash", t("common.delete"), { variant: "ghost subtle-danger", onClick: () => removeCustom(name) }),
                    )
                  : iconButton("eye", t("common.view"), { onClick: () => editCustom(name) }),
              ),
            ),
          )
        : h("div", { class: "small muted" }, t("custom.none")),
    );
  }

  async function removeCustom(name) {
    const sure = await confirm({
      title: t("custom.delete"),
      text: t("custom.deleteText", { name: `pgx_${name}` }),
      confirmLabel: t("common.delete"),
      tone: "danger",
    });
    if (!sure) return;
    const saved = await reviewAndSave({ ops: [{ op: "unset", path: ["custom_tools", name] }] });
    if (saved) load();
  }

  function editCustom(existing) {
    const canEdit = editable();
    const before = existing ? clone(store.config.raw.custom_tools[existing]) : { description: "", sql: "", params: [] };
    const tool = clone(before);
    tool.params = tool.params || [];
    tool.connections = tool.connections || [];
    let name = existing || "";
    const pattern = new RegExp(schema().tool_name_pattern);
    const ids = { name: nextId("f"), description: nextId("f"), sql: nextId("f"), rows: nextId("f") };
    const error = h("div", { class: "field-error", hidden: true, role: "alert" }, icon("alert-circle"), "");

    const nameInput = h("input", {
      id: ids.name,
      class: "input",
      value: name,
      placeholder: "orders_of_customer",
      readonly: !canEdit,
      autocomplete: "off",
      autocapitalize: "none",
      spellcheck: "false",
      on: { input: () => (name = nameInput.value.trim()) },
    });
    const description = autosize(
      h("textarea", {
        id: ids.description,
        class: "textarea",
        readonly: !canEdit,
        on: { input: () => (tool.description = description.value) },
      }),
    );
    description.value = tool.description || "";
    const sql = autosize(
      h("textarea", {
        id: ids.sql,
        class: "textarea code",
        readonly: !canEdit,
        spellcheck: "false",
        autocapitalize: "none",
        autocomplete: "off",
        placeholder: "SELECT id, status\n  FROM shop.orders\n WHERE customer_id = $1",
        on: { input: () => (tool.sql = sql.value) },
      }),
    );
    sql.value = tool.sql || "";
    const maxRows = h("input", {
      id: ids.rows,
      class: "input short",
      type: "text",
      inputmode: "numeric",
      value: tool.max_rows ?? "",
      placeholder: t("field.none"),
      readonly: !canEdit,
      on: {
        change: () => {
          const text = maxRows.value.trim();
          if (text === "") delete tool.max_rows;
          else tool.max_rows = Number(text);
        },
      },
    });

    const paramsBox = h("div", { class: "stack gap-3" });
    function drawParams() {
      mount(
        paramsBox,
        tool.params.length
          ? tool.params.map((param, index) => {
              const nameId = nextId("p");
              const textId = nextId("p");
              const paramName = h("input", {
                id: nameId,
                class: "input",
                value: param.name || "",
                placeholder: "customer_id",
                readonly: !canEdit,
                autocomplete: "off",
                autocapitalize: "none",
                spellcheck: "false",
                on: { input: () => (param.name = paramName.value.trim()) },
              });
              const paramText = h("input", {
                id: textId,
                class: "input",
                value: param.description || "",
                readonly: !canEdit,
                on: {
                  input: () => {
                    if (paramText.value.trim()) param.description = paramText.value.trim();
                    else delete param.description;
                  },
                },
              });
              return h(
                "div",
                { class: "subcard" },
                h(
                  "div",
                  { class: "row nowrap-row" },
                  h("span", { class: "badge accent mono" }, `$${index + 1}`),
                  h("span", { class: "grow small secondary" }, t("custom.paramOrder")),
                  canEdit
                    ? iconButton("trash", t("common.removeItem", { item: param.name || `$${index + 1}` }), {
                        onClick: () => {
                          tool.params.splice(index, 1);
                          drawParams();
                        },
                      })
                    : null,
                ),
                h(
                  "div",
                  { class: "pair" },
                  h("label", { class: "labeled", for: nameId }, fieldLabel("param_name"), paramName),
                  h(
                    "div",
                    { class: "labeled" },
                    fieldLabel("type"),
                    dropdown(
                      ["text", "integer", "number", "boolean"].map((value) => ({ value, label: t(`option.type.${value}`) })),
                      param.type || "text",
                      (next) => (param.type = next),
                      { label: fieldLabel("type"), disabled: !canEdit },
                    ),
                  ),
                ),
                h("label", { class: "labeled", for: textId }, fieldLabel("param_description"), paramText),
                checkbox(
                  fieldLabel("required"),
                  param.required !== false,
                  (checked) => {
                    if (checked) delete param.required;
                    else param.required = false;
                  },
                  { hint: fieldHelp("required"), disabled: !canEdit },
                ),
              );
            })
          : h("div", { class: "small muted" }, t("custom.noParams")),
        canEdit
          ? h(
              "div",
              null,
              button(t("custom.addParam"), {
                icon: "plus",
                onClick: () => {
                  tool.params.push({ name: "", type: "text" });
                  drawParams();
                },
              }),
            )
          : null,
      );
    }
    drawParams();

    const scope = h(
      "div",
      { class: "stack gap-1" },
      connectionNames().map((connection) =>
        checkbox(
          connection,
          tool.connections.includes(connection),
          (checked) => {
            tool.connections = checked
              ? [...tool.connections, connection]
              : tool.connections.filter((other) => other !== connection);
          },
          { disabled: !canEdit },
        ),
      ),
    );

    const save = button(t("save.reviewButton"), {
      variant: "primary",
      icon: "shield-check",
      onClick: async () => {
        error.hidden = true;
        const problem = !pattern.test(name)
          ? t("custom.nameInvalid")
          : !existing && Object.keys(store.config.raw.custom_tools || {}).includes(name)
            ? t("custom.nameTaken")
            : !tool.description.trim()
              ? t("custom.needDescription")
              : !tool.sql.trim()
                ? t("custom.needSql")
                : null;
        if (problem) {
          error.lastChild.textContent = problem;
          error.hidden = false;
          return;
        }
        const value = clone(tool);
        if (!value.params.length) delete value.params;
        if (!value.connections.length) delete value.connections;
        const changes = [];
        if (existing && existing !== name) changes.push({ op: "rename", path: ["custom_tools", existing], to: name });
        if (!existing || !equal(before, value) || existing !== name) changes.push({ op: "set", path: ["custom_tools", name], value });
        const saved = await reviewAndSave({
          ops: changes,
          onErrors: (errors) => {
            const first = errors.find((entry) => entry.loc.startsWith(`custom_tools.${name}`)) || errors[0];
            if (first) {
              error.lastChild.textContent = `${first.loc ? `${first.loc}: ` : ""}${first.message}`;
              error.hidden = false;
            }
          },
        });
        if (saved) {
          dialog.close();
          load();
        }
      },
    });

    const dialog = openDialog({
      title: existing ? `pgx_${existing}` : t("custom.new"),
      wide: true,
      body: [
        banner({ plain: true, title: t("custom.whatTitle"), text: t("custom.what") }),
        h("label", { class: "labeled", for: ids.name }, fieldLabel("tool_name"), nameInput, h("span", { class: "small secondary" }, fieldHelp("tool_name"))),
        h("label", { class: "labeled", for: ids.description }, fieldLabel("tool_description"), description, h("span", { class: "small secondary" }, fieldHelp("tool_description"))),
        h("label", { class: "labeled", for: ids.sql }, fieldLabel("sql"), sql, h("span", { class: "small secondary" }, fieldHelp("sql"))),
        h("div", { class: "labeled" }, fieldLabel("tool_params"), h("span", { class: "small secondary" }, fieldHelp("tool_params")), paramsBox),
        h("div", { class: "labeled" }, fieldLabel("tool_connections"), h("span", { class: "small secondary" }, fieldHelp("tool_connections")), scope),
        h("label", { class: "labeled", for: ids.rows }, fieldLabel("tool_max_rows"), maxRows, h("span", { class: "small secondary" }, fieldHelp("tool_max_rows"))),
        error,
      ],
      footer: canEdit ? [button(t("common.cancel"), { onClick: () => dialog.close() }), save] : [button(t("common.close"), { variant: "primary", onClick: () => dialog.close() })],
    });
    void chipsEditor;
  }

  mount(root, pageHead({ title: t("tools.title"), text: t("tools.text") }), body);
  load();
  const off = on("config", () => {
    if (!dirty()) load();
  });
  return () => {
    off();
    guardUnsaved(null);
  };
}
