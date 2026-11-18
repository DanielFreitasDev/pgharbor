// Audit: every call the agents made, searchable, with the detail of each one.
//
// Statements and messages in the log were written with an agent's help. They are shown
// as text, in fixed-width blocks, and never interpreted.

import { api, on, query } from "../api.js";
import { h, icon, mount } from "../dom.js";
import { Draft, fieldRow } from "../fields.js";
import { fmt, t } from "../i18n.js";
import { errorText, guardUnsaved, saveBar } from "../save.js";
import { errorsUnder, readOnly, schema, store } from "../store.js";
import {
  badge,
  banner,
  button,
  card,
  copyButton,
  dropdown,
  empty,
  iconButton,
  kv,
  link,
  loading,
  openDialog,
  pageHead,
  stale,
  toggle,
} from "../ui.js";
import { outcomeBadge, outcomeLabel } from "./shared.js";

const OUTCOMES = ["ok", "refused", "error", "bug", "approved", "needs-approval"];
const PERIODS = { all: null, "1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000 };
const PAGE = 100;

export function audit(root, { params, go }) {
  const filters = {
    period: PERIODS[params.get("period")] !== undefined ? params.get("period") : "all",
    connection: params.get("connection") || "",
    outcome: params.get("outcome") || "",
    tool: params.get("tool") || "",
    rule: params.get("rule") || "",
    session: params.get("session") || "",
    q: params.get("q") || "",
  };
  const focus = Number(params.get("focus")) || null;
  let entries = [];
  let facets = { connection: [], tool: [], rule: [] };
  let hasMore = false;
  let last = 0;
  let file = null;
  let enabled = true;
  let live = true;
  let loaded = false;
  let typing = null;

  const list = h("div", { class: "stack gap-3" }, loading());
  const filterRow = h("div", { class: "filters" });
  const settings = h("div");

  function remember() {
    const shown = { ...filters, period: filters.period === "all" ? "" : filters.period };
    history.replaceState(null, "", `#/audit${query(shown)}`);
  }

  function request(extra) {
    const span = PERIODS[filters.period];
    return api(
      `/api/audit${query({
        limit: PAGE,
        connection: filters.connection,
        outcome: filters.outcome,
        tool: filters.tool,
        rule: filters.rule,
        session: filters.session,
        q: filters.q,
        since: span ? Date.now() / 1000 - span : null,
        ...extra,
      })}`,
    );
  }

  async function load() {
    if (loaded) stale(list, true);
    try {
      const page = await request({});
      entries = page.entries;
      facets = page.facets;
      hasMore = page.has_more;
      last = page.last;
      file = page.file;
      enabled = page.enabled;
      loaded = true;
      drawFilters();
      drawList();
      if (focus) {
        const entry = entries.find((candidate) => candidate.n === focus);
        if (entry) showEntry(entry);
      }
    } catch (error) {
      mount(list, banner({ tone: "danger", title: t("error.load"), text: errorText(error) }));
    } finally {
      stale(list, false);
    }
  }

  async function more() {
    const page = await request({ before: entries[entries.length - 1].n });
    entries = [...entries, ...page.entries];
    hasMore = page.has_more;
    drawList();
  }

  async function newer() {
    if (!loaded || !live) return;
    try {
      const page = await request({ after: last });
      if (page.entries.length) {
        entries = [...page.entries, ...entries];
        drawList(page.entries.length);
      }
      last = page.last;
    } catch {
      // the next heartbeat tries again
    }
  }

  function set(key, value) {
    filters[key] = value;
    remember();
    load();
  }

  function drawFilters() {
    const options = (values, all) => [{ value: "", label: all }, ...values.map((value) => ({ value, label: value }))];
    const search = h("input", {
      class: "input",
      value: filters.q,
      placeholder: t("audit.search"),
      "aria-label": t("audit.search"),
      autocomplete: "off",
      spellcheck: "false",
      enterkeyhint: "search",
      on: {
        input: () => {
          clearTimeout(typing);
          typing = setTimeout(() => set("q", search.value.trim()), 300);
        },
      },
    });
    const active = Object.entries(filters).filter(([key, value]) => value && !(key === "period" && value === "all")).length;
    mount(
      filterRow,
      h("div", { class: "search" }, icon("search"), search),
      dropdown(
        Object.keys(PERIODS).map((value) => ({ value, label: t(`audit.period.${value}`) })),
        filters.period,
        (next) => set("period", next),
        { label: t("audit.period"), auto: true },
      ),
      dropdown(
        [{ value: "", label: t("audit.anyOutcome") }, ...OUTCOMES.map((value) => ({ value, label: outcomeLabel(value) }))],
        filters.outcome,
        (next) => set("outcome", next),
        { label: t("audit.outcome"), auto: true },
      ),
      dropdown(options(facets.connection, t("audit.anyConnection")), filters.connection, (next) => set("connection", next), {
        label: t("audit.connection"),
        auto: true,
      }),
      dropdown(options(facets.tool, t("audit.anyTool")), filters.tool, (next) => set("tool", next), {
        label: t("audit.tool"),
        auto: true,
      }),
      facets.rule.length || filters.rule
        ? dropdown(options(facets.rule, t("audit.anyRule")), filters.rule, (next) => set("rule", next), {
            label: t("audit.rule"),
            auto: true,
          })
        : null,
      filters.session
        ? h(
            "span",
            { class: "chip plain" },
            icon("users"),
            t("audit.session", { id: filters.session }),
            h("button", { type: "button", "aria-label": t("audit.clearSession"), on: { click: () => set("session", "") } }, icon("x")),
          )
        : null,
      active
        ? button(t("audit.clear"), {
            variant: "ghost",
            small: true,
            icon: "x",
            onClick: () => {
              for (const key of Object.keys(filters)) filters[key] = key === "period" ? "all" : "";
              remember();
              load();
            },
          })
        : null,
      h(
        "div",
        { class: "switch-row end" },
        toggle(live, { label: t("audit.live"), onChange: (next) => (live = next) }),
        h("span", { class: "small strong" }, t("audit.live")),
      ),
    );
  }

  function summary(entry) {
    return entry.sql || entry.detail || "";
  }

  function drawList(fresh = 0) {
    if (!entries.length) {
      mount(
        list,
        card(
          {},
          empty({
            icon: "list",
            title: enabled ? t("audit.empty") : t("audit.disabled"),
            text: enabled ? t("audit.emptyText") : t("audit.disabledText"),
          }),
        ),
      );
      return;
    }
    const rows = entries.map((entry, index) =>
      h(
        "button",
        {
          type: "button",
          class: ["log-row", index < fresh && "fresh"],
          on: { click: () => showEntry(entry) },
        },
        h("span", { class: "log-time nums", "data-tip": fmt.dateTime(entry.ts) }, fmt.relative(entry.ts)),
        h("span", { class: "log-outcome" }, outcomeBadge(entry.outcome)),
        h("span", { class: "log-tool mono strong truncate" }, entry.tool),
        h("span", { class: "log-connection truncate" }, entry.connection || "–"),
        h("span", { class: "log-ms nums" }, entry.ms === undefined ? "" : fmt.ms(entry.ms)),
        h("span", { class: "log-text mono truncate secondary" }, entry.rule ? `${entry.rule} · ${summary(entry)}` : summary(entry)),
      ),
    );
    mount(
      list,
      h(
        "div",
        { class: "card flush" },
        h(
          "div",
          { class: "log-head", "aria-hidden": "true" },
          h("span", null, t("audit.col.when")),
          h("span", null, t("audit.col.outcome")),
          h("span", null, t("audit.col.tool")),
          h("span", null, t("audit.col.connection")),
          h("span", null, t("audit.col.time")),
          h("span", null, t("audit.col.what")),
        ),
        h("div", { class: "log" }, rows),
      ),
      h(
        "div",
        { class: "row spread" },
        h("span", { class: "small muted" }, t("audit.shown", { n: entries.length }), file ? ` · ${file.path} (${fmt.bytes(file.size)})` : ""),
        hasMore ? button(t("audit.more"), { icon: "arrow-down", onClick: more }) : null,
      ),
    );
  }

  function showEntry(entry) {
    const session = entry.session;
    const dialog = openDialog({
      title: entry.tool,
      drawer: true,
      body: [
        h("div", { class: "row gap-2" }, outcomeBadge(entry.outcome), entry.rule ? badge(entry.rule, "outline", "shield") : null),
        kv([
          [t("audit.col.when"), fmt.dateTime(entry.ts)],
          [t("audit.col.connection"), entry.connection],
          [t("audit.col.time"), entry.ms === undefined ? null : fmt.ms(entry.ms)],
          [t("audit.rows"), entry.rows === undefined ? null : fmt.number(entry.rows)],
          [
            t("audit.sessionLabel"),
            session
              ? h(
                  "a",
                  {
                    href: `#/audit${query({ session })}`,
                    on: {
                      click: (event) => {
                        event.preventDefault();
                        dialog.close();
                        go(`#/audit${query({ session })}`);
                      },
                    },
                  },
                  session,
                )
              : null,
          ],
        ]),
        entry.detail ? [h("h3", null, t("audit.detail")), h("pre", { class: "code" }, entry.detail)] : null,
        entry.sql
          ? [
              h("div", { class: "row spread" }, h("h3", null, t("audit.statement")), copyButton(entry.sql)),
              h("pre", { class: "code", tabindex: "0" }, entry.sql),
            ]
          : h("p", { class: "small muted" }, t("audit.noStatement")),
        entry.params !== undefined
          ? [h("h3", null, t("audit.params")), h("pre", { class: "code" }, JSON.stringify(entry.params, null, 2))]
          : null,
        banner({ plain: true, text: t("audit.dataNotice") }),
      ],
      footer: [button(t("common.close"), { variant: "primary", onClick: () => dialog.close() })],
    });
  }

  // -------------------------------------------------------------- log settings

  function drawSettings() {
    const config = store.config;
    const editable = !readOnly() && config.file.exists && config.file.is_toml;
    const draft = new Draft(config.raw.audit || {});
    let errors = errorsUnder("audit", config.errors);
    const ctx = {
      draft,
      readOnly: !editable,
      inherited: (key) => ({ value: schema().audit.find((spec) => spec.key === key).default, source: "builtin" }),
      errorFor: (key) => errors[key] || null,
    };
    const rows = schema().audit.map((spec) => fieldRow({ ...spec, key: spec.key }, ctx));
    const bar = saveBar({
      count: () => draft.changeCount,
      collect: () => draft.ops(["audit"]),
      onErrors: (found) => {
        errors = errorsUnder("audit", found);
        rows.forEach((row) => row.redraw());
      },
      onSaved: () => {
        drawSettings();
        load();
      },
      onDiscard: drawSettings,
    });
    draft.onChange(() => bar.update());
    guardUnsaved(() => draft.dirty);
    mount(
      settings,
      h(
        "details",
        { class: "more card" },
        h("summary", null, icon("chevron-right"), h("span", { class: "grow" }, t("audit.settings")), badge(config.raw.audit && config.raw.audit.enabled === false ? t("common.off") : t("common.on"), config.raw.audit && config.raw.audit.enabled === false ? "danger" : "ok")),
        h("p", { class: "small secondary" }, t("audit.settingsText")),
        h("div", null, rows),
        bar,
      ),
    );
  }

  mount(
    root,
    pageHead({
      title: t("audit.title"),
      text: t("audit.text"),
      actions: iconButton("refresh", t("common.refresh"), { variant: null, onClick: load }),
    }),
    filterRow,
    list,
    settings,
  );
  drawSettings();
  load();
  void link;
  const offPulse = on("pulse", (pulse) => {
    if (pulse.audit_last !== last) newer();
  });
  return () => {
    offPulse();
    clearTimeout(typing);
    guardUnsaved(null);
  };
}
