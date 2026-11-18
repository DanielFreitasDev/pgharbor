// Pieces used by more than one screen.

import { h } from "../dom.js";
import { has, t } from "../i18n.js";
import { badge } from "../ui.js";

const OUTCOME = {
  ok: ["ok", "check"],
  refused: ["warn", "ban"],
  error: ["danger", "alert-circle"],
  bug: ["danger", "alert-circle"],
  approved: ["info", "shield-check"],
  "needs-approval": ["info", "lock"],
};

export function outcomeLabel(outcome) {
  return has(`outcome.${outcome}`) ? t(`outcome.${outcome}`) : String(outcome || "?");
}

export function outcomeBadge(outcome) {
  const [tone, iconName] = OUTCOME[outcome] || [null, null];
  return badge(outcomeLabel(outcome), tone, iconName);
}

export function accessBadge(access) {
  const tone = { "read-only": "ok", "read-write": "warn", admin: "danger" }[access];
  const iconName = access === "read-only" ? "eye" : "edit";
  return badge(has(`option.access.${access}`) ? t(`option.access.${access}`) : access, tone, iconName);
}

export function environmentBadge(connection) {
  const name = connection.effective.environment;
  return connection.is_production ? badge(name, "danger", "server") : badge(name, "outline", "server");
}

/** How a session is called on screen: what the client said it is, or just "session". */
export function sessionTitle(session) {
  if (session.id === "panel") return t("sessions.panel");
  const client = session.client;
  if (client && client.name) return client.version ? `${client.name} ${client.version}` : client.name;
  return t("sessions.unknownClient");
}

export function folderName(path) {
  if (!path) return "";
  const parts = String(path).split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

export function mono(text) {
  return h("span", { class: "mono" }, text);
}
