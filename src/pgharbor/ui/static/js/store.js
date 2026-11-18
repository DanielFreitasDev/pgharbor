// What every screen shares: facts about this run of the panel, the configuration as the
// file has it now, and the last heartbeat from the server.

import { api, emit } from "./api.js";

export const store = {
  meta: null,
  config: null,
  pulse: null,
};

export async function loadMeta() {
  store.meta = await api("/api/meta");
  return store.meta;
}

export async function loadConfig() {
  store.config = await api("/api/config");
  emit("config", store.config);
  return store.config;
}

export function useConfig(config) {
  store.config = config;
  emit("config", config);
}

export function readOnly() {
  return Boolean(store.meta && store.meta.read_only);
}

export function schema() {
  return store.meta.schema;
}

/** The value a connection gets for a key it does not set, and where it comes from. */
export function inheritedFor(spec, defaults) {
  if (spec.in_defaults && defaults && Object.prototype.hasOwnProperty.call(defaults, spec.key)) {
    return { value: defaults[spec.key], source: "defaults" };
  }
  return { value: spec.default, source: "builtin" };
}

export function connectionNames() {
  const raw = (store.config && store.config.raw && store.config.raw.connections) || {};
  const names = new Set([...Object.keys(raw), ...Object.keys((store.config || {}).connections || {})]);
  return [...names];
}

/** Errors the validator reported for one part of the file, by key. */
export function errorsUnder(prefix, errors) {
  const found = {};
  for (const error of errors || []) {
    if (error.loc === prefix) found[""] = error.message;
    else if (error.loc.startsWith(`${prefix}.`)) found[error.loc.slice(prefix.length + 1)] = error.message;
  }
  return found;
}
