// Sessions: who is using pgharbor right now, and who was.
//
// Each client session runs its own pgharbor process, and each process keeps a small
// file while it runs. What a client says about itself (its name, its version) is shown
// as it came: it identifies, it does not prove.

import { api, on, query } from "../api.js";
import { h, icon, mount } from "../dom.js";
import { fmt, t } from "../i18n.js";
import { errorText } from "../save.js";
import { badge, banner, card, empty, kv, link, loading, pageHead, stale } from "../ui.js";
import { folderName, sessionTitle } from "./shared.js";

export function sessions(root) {
  const body = h("div", { class: "stack gap-5" }, loading());
  let loaded = false;
  let pastOpen = false;
  let seen = "";

  async function load() {
    if (loaded) stale(body, true);
    try {
      draw(await api("/api/sessions"));
      loaded = true;
    } catch (error) {
      if (!loaded) mount(body, banner({ tone: "danger", title: t("error.load"), text: errorText(error) }));
    } finally {
      stale(body, false);
    }
  }

  function approvals(session, now) {
    const granted = Object.entries(session.approvals || {});
    if (!granted.length) return t("sessions.noApprovals");
    return h(
      "div",
      { class: "row gap-2" },
      granted.map(([name, approval]) => {
        const until = approval.expires ? Date.parse(approval.expires) / 1000 : null;
        if (until !== null && until < now) return badge(t("sessions.approvalExpired", { name }), "outline", "lock");
        return badge(
          until === null ? t("sessions.approvalForever", { name }) : t("sessions.approvalUntil", { name, when: fmt.time(until) }),
          "warn",
          "shield-check",
        );
      }),
    );
  }

  function sessionCard(session, now) {
    const activity = session.activity || { calls: 0, refused: 0, error: 0, connections: [] };
    const state = {
      active: badge(t("sessions.state.active"), "ok"),
      ended: badge(t("sessions.state.ended"), null),
      lost: badge(t("sessions.state.lost"), "warn"),
    }[session.state];
    return h(
      "article",
      { class: "card" },
      h(
        "div",
        { class: "card-head" },
        h(
          "div",
          { class: "row nowrap-row" },
          h("span", { class: ["avatar", session.state !== "active" && "neutral"] }, icon("terminal")),
          h(
            "div",
            { class: "grow" },
            h("h3", null, sessionTitle(session)),
            h("p", { class: "truncate" }, session.cwd ? t("sessions.project", { name: folderName(session.cwd) }) : t("sessions.noFolder")),
          ),
        ),
        h(
          "div",
          { class: "row gap-2" },
          session.state === "active" ? h("span", { class: "dot ok live", "aria-hidden": "true" }) : null,
          state,
          session.outdated ? badge(t("sessions.outdated"), "warn", "history") : null,
        ),
      ),
      h(
        "div",
        { class: "tiles compact" },
        h("div", { class: "tile" }, h("div", { class: "tile-label" }, t("tile.calls")), h("div", { class: "tile-value" }, fmt.compact(activity.calls))),
        h("div", { class: "tile" }, h("div", { class: "tile-label" }, t("tile.refused")), h("div", { class: "tile-value" }, fmt.compact(activity.refused))),
        h("div", { class: "tile" }, h("div", { class: "tile-label" }, t("tile.errors")), h("div", { class: "tile-value" }, fmt.compact(activity.error))),
      ),
      kv([
        [t("sessions.folder"), session.cwd ? h("span", { class: "mono" }, session.cwd) : null],
        [t("sessions.started"), h("span", { "data-tip": fmt.dateTime(session.started) }, fmt.relative(session.started, now))],
        [t("sessions.lastCall"), activity.last ? `${fmt.relative(activity.last, now)} · ${activity.last_tool || ""}` : t("sessions.noCalls")],
        [t("sessions.ended"), session.ended ? fmt.dateTime(session.ended) : null],
        [t("sessions.connections"), activity.connections.length ? activity.connections.join(", ") : t("connections.none")],
        [t("sessions.approvals"), session.state === "active" ? approvals(session, now) : null],
        [t("sessions.id"), h("span", { class: "mono" }, `${session.id} · pid ${session.pid}`)],
        [t("sessions.protocol"), session.protocol ? `MCP ${session.protocol} · pgharbor ${session.version}` : `pgharbor ${session.version}`],
      ]),
      h("div", null, link(t("sessions.seeCalls"), `#/audit${query({ session: session.id })}`, { icon: "list", small: true })),
    );
  }

  function draw(data) {
    const now = data.now;
    const active = data.sessions.filter((session) => session.state === "active");
    const past = data.sessions.filter((session) => session.state !== "active");
    const outdated = active.filter((session) => session.outdated).length;
    mount(
      body,
      !data.recording ? banner({ tone: "warn", title: t("sessions.notRecording"), text: t("sessions.notRecordingText") }) : null,
      outdated ? banner({ tone: "warn", title: t("sessions.someOutdated", { n: outdated }), text: t("sessions.outdatedText") }) : null,
      h("h2", null, t("sessions.active", { n: active.length })),
      active.length
        ? h("div", { class: "grid", "data-min": "wide" }, active.map((session) => sessionCard(session, now)))
        : card({}, empty({ icon: "users", title: t("sessions.none"), text: t("sessions.noneText") })),
      past.length
        ? h(
            "details",
            { class: "more", open: pastOpen, on: { toggle: (event) => (pastOpen = event.target.open) } },
            h("summary", null, icon("chevron-right"), t("sessions.past", { n: past.length })),
            h("div", { class: "grid", "data-min": "wide" }, past.map((session) => sessionCard(session, now))),
          )
        : null,
      banner({ plain: true, title: t("sessions.howTitle"), text: t("sessions.how") }),
    );
  }

  mount(root, pageHead({ title: t("sessions.title"), text: t("sessions.text") }), body);
  load();
  // Reload only when something a session card shows may have changed.
  const off = on("pulse", (pulse) => {
    const now = `${pulse.active_sessions}/${pulse.audit_last}/${pulse.config_digest}`;
    if (loaded && now !== seen) load();
    seen = now;
  });
  return off;
}
