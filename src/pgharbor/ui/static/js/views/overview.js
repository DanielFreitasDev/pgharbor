// Overview: the state of the installation at a glance, then the numbers behind it.

import { api, on, query } from "../api.js";
import { activityChart, barList, delta, hoursStrip } from "../charts.js";
import { h, icon, mount } from "../dom.js";
import { fmt, t } from "../i18n.js";
import { errorText, findingRow } from "../save.js";
import { store } from "../store.js";
import { badge, banner, card, empty, link, loading, pageHead, segmented, stale } from "../ui.js";
import { outcomeBadge, sessionTitle } from "./shared.js";

const RANGES = ["1h", "24h", "7d", "30d"];

function tile({ label, iconName, value, unit, note, href }) {
  const content = [
    h("div", { class: "tile-label" }, iconName ? icon(iconName) : null, label),
    h("div", { class: "tile-value" }, value, unit ? h("small", null, ` ${unit}`) : null),
    h("div", { class: "tile-note" }, note || null),
  ];
  return href ? h("a", { class: "tile", href }, content) : h("div", { class: "tile" }, content);
}

export function overview(root) {
  let range = "24h";
  try {
    const saved = sessionStorage.getItem("pgharbor.range");
    if (RANGES.includes(saved)) range = saved;
  } catch {
    // default range
  }
  const body = h("div", { class: "stack gap-5" }, loading());
  const picker = segmented(
    RANGES.map((value) => ({ value, label: t(`range.${value}`) })),
    range,
    (next) => {
      range = next;
      try {
        sessionStorage.setItem("pgharbor.range", next);
      } catch {
        // not remembered
      }
      load(true);
    },
    { label: t("overview.period") },
  );
  mount(
    root,
    pageHead({ title: t("overview.title"), text: t("overview.text") }),
    h("div", { class: "filters" }, picker),
    body,
  );

  let loaded = false;
  let lastSeen = null;

  async function load(showStale) {
    if (showStale && loaded) stale(body, true);
    try {
      const tz = -new Date().getTimezoneOffset();
      const [stats, sessions] = await Promise.all([
        api(`/api/audit/stats${query({ range, tz })}`),
        api("/api/sessions"),
      ]);
      loaded = true;
      lastSeen = store.pulse ? store.pulse.audit_last : null;
      draw(stats, sessions);
    } catch (error) {
      if (!loaded) mount(body, banner({ tone: "danger", title: t("error.load"), text: errorText(error) }));
    } finally {
      stale(body, false);
    }
  }

  function draw(stats, sessionData) {
    const config = store.config;
    const totals = stats.totals;
    const previous = stats.previous;
    const calls = totals.calls || 0;
    const active = sessionData.sessions.filter((session) => session.state === "active");
    const outdated = active.filter((session) => session.outdated);
    const connections = Object.values(config.connections || {});
    const production = connections.filter((connection) => connection.is_production).length;
    const versus = t(`range.${range}.previous`);

    const tiles = h(
      "div",
      { class: "tiles six" },
      tile({
        label: t("tile.calls"),
        iconName: "activity",
        value: fmt.compact(calls),
        note: [delta(calls, previous.calls || 0, { upIsGood: null }), versus],
        href: "#/audit",
      }),
      tile({
        label: t("tile.refused"),
        iconName: "ban",
        value: fmt.compact(totals.refused || 0),
        note: [
          delta(totals.refused || 0, previous.refused || 0, { upIsGood: false }),
          calls ? t("tile.share", { share: fmt.percent(totals.refused || 0, calls) }) : versus,
        ],
        href: "#/audit?outcome=refused",
      }),
      tile({
        label: t("tile.errors"),
        iconName: "alert-circle",
        value: fmt.compact(totals.error || 0),
        note: [
          delta(totals.error || 0, previous.error || 0, { upIsGood: false }),
          calls ? t("tile.share", { share: fmt.percent(totals.error || 0, calls) }) : versus,
        ],
        href: "#/audit?outcome=error",
      }),
      tile({
        label: t("tile.latency"),
        iconName: "clock",
        value: stats.latency.p50 === null ? "–" : fmt.ms(stats.latency.p50),
        note: stats.latency.p95 === null ? t("tile.noData") : t("tile.p95", { value: fmt.ms(stats.latency.p95) }),
      }),
      tile({
        label: t("tile.sessions"),
        iconName: "users",
        value: fmt.number(active.length),
        note: outdated.length ? t("tile.outdated", { n: outdated.length }) : t("tile.sessionsNote"),
        href: "#/sessions",
      }),
      tile({
        label: t("tile.connections"),
        iconName: "database",
        value: fmt.number(connections.length),
        note: t("tile.production", { n: production }),
        href: "#/connections",
      }),
    );

    const activity = card(
      { title: t("overview.activity"), subtitle: t("overview.activityText") },
      calls
        ? activityChart(stats.buckets, stats.bucket_seconds)
        : empty({ icon: "chart", title: t("overview.quiet"), text: t("overview.quietText") }),
    );

    const tools = card(
      { title: t("overview.tools"), subtitle: t("overview.toolsText") },
      barList(stats.by_tool.slice(0, 8), {
        href: (row) => `#/audit${query({ tool: row.name })}`,
        emptyText: t("overview.noCalls"),
      }),
    );
    const byConnection = card(
      { title: t("overview.connections"), subtitle: t("overview.connectionsText") },
      barList(stats.by_connection, {
        href: (row) => `#/audit${query({ connection: row.name })}`,
        emptyText: t("overview.noCalls"),
      }),
    );
    const rules = card(
      { title: t("overview.rules"), subtitle: t("overview.rulesText") },
      barList(stats.by_rule, {
        href: (row) => `#/audit${query({ outcome: "refused", rule: row.name })}`,
        emptyText: t("overview.noRefusals"),
      }),
    );
    const hours = card(
      { title: t("overview.hours"), subtitle: t("overview.hoursText") },
      calls ? hoursStrip(stats.by_hour) : h("div", { class: "small muted" }, t("overview.noCalls")),
    );

    const findings = config.posture || [];
    const counts = { danger: 0, warn: 0, note: 0 };
    for (const finding of findings) if (finding.level in counts) counts[finding.level] += 1;
    const postureCard = card(
      {
        title: t("posture.title"),
        subtitle: t("posture.text"),
        actions: h(
          "div",
          { class: "row gap-2" },
          counts.danger ? badge(t("posture.count.danger", { n: counts.danger }), "danger") : null,
          counts.warn ? badge(t("posture.count.warn", { n: counts.warn }), "warn") : null,
          !counts.danger && !counts.warn ? badge(t("posture.clean"), "ok", "shield-check") : null,
        ),
      },
      config.valid
        ? h(
            "div",
            null,
            findings.map((finding) =>
              findingRow(
                "posture",
                finding,
                finding.connection
                  ? link(t("common.open"), `#/connections/${encodeURIComponent(finding.connection)}`, {
                      small: true,
                      variant: "ghost",
                    })
                  : null,
              ),
            ),
          )
        : h("div", { class: "small muted" }, t("posture.invalid")),
    );

    const recent = (title, entries, emptyText, filter) =>
      card(
        {
          title,
          flush: true,
          actions: link(t("common.seeAll"), `#/audit${query(filter)}`, { small: true, variant: "ghost" }),
        },
        entries.length
          ? h(
              "div",
              { class: "rows" },
              entries.map((entry) =>
                h(
                  "a",
                  { href: `#/audit${query({ ...filter, focus: entry.n })}` },
                  h(
                    "div",
                    { class: "lead" },
                    h("div", { class: "row gap-2" }, h("span", { class: "strong" }, entry.tool), entry.rule ? badge(entry.rule, "outline") : null),
                    h("div", { class: "small secondary truncate" }, entry.detail || entry.sql || ""),
                  ),
                  h("div", { class: "trail" }, entry.connection || "", h("span", { class: "tiny" }, fmt.relative(entry.ts))),
                ),
              ),
            )
          : h("div", { class: "empty" }, h("p", null, emptyText)),
      );

    const sessionsCard = card(
      {
        title: t("overview.sessions"),
        flush: true,
        actions: link(t("common.seeAll"), "#/sessions", { small: true, variant: "ghost" }),
      },
      active.length
        ? h(
            "div",
            { class: "rows" },
            active.slice(0, 6).map((session) =>
              h(
                "a",
                { href: "#/sessions" },
                h("span", { class: "dot ok live", "aria-hidden": "true" }),
                h(
                  "div",
                  { class: "lead" },
                  h("span", { class: "strong" }, sessionTitle(session)),
                  h("span", { class: "small secondary truncate" }, session.cwd || ""),
                ),
                h(
                  "div",
                  { class: "trail" },
                  t("sessions.calls", { n: session.activity ? session.activity.calls : 0 }),
                  session.outdated ? badge(t("sessions.outdated"), "warn") : null,
                ),
              ),
            ),
          )
        : h("div", { class: "empty" }, h("p", null, t("overview.noSessions"))),
    );

    mount(
      body,
      tiles,
      activity,
      h("div", { class: "split" }, tools, byConnection),
      h("div", { class: "split" }, rules, hours),
      h("div", { class: "split main-aside" }, postureCard, sessionsCard),
      h(
        "div",
        { class: "split" },
        recent(t("overview.recentRefusals"), stats.recent_refusals, t("overview.noRefusals"), { outcome: "refused" }),
        recent(t("overview.recentFailures"), stats.recent_failures, t("overview.noFailures"), { outcome: "error" }),
      ),
    );
    void outcomeBadge;
  }

  const offPulse = on("pulse", (pulse) => {
    if (loaded && pulse.audit_last !== lastSeen) load(true);
  });
  const offConfig = on("config", () => loaded && load(true));
  load(false);
  return () => {
    offPulse();
    offConfig();
  };
}
