// Talking to the panel's server.
//
// The key of this run arrives once, in the fragment of the address printed by
// `pgharbor ui`. It is kept for this tab only (sessionStorage is separate for every
// port of 127.0.0.1, unlike a cookie), removed from the address at once, and sent as an
// Authorization header, which no other site can make a browser send.

const TOKEN_KEY = "pgharbor.token";
let token = null;
const listeners = new Map();

export function on(event, handler) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(handler);
  return () => listeners.get(event).delete(handler);
}

export function emit(event, detail) {
  for (const handler of listeners.get(event) || []) handler(detail);
}

/** Read the key from the address (once) or from this tab's storage. */
export function takeToken() {
  const match = /(?:^#|[#&])token=([A-Za-z0-9_-]{20,})/.exec(location.hash);
  if (match) {
    rememberToken(match[1]);
    // Out of the address bar and out of the history, before anything else happens.
    history.replaceState(null, "", `${location.pathname}#/`);
  } else {
    try {
      token = sessionStorage.getItem(TOKEN_KEY);
    } catch {
      token = null;
    }
  }
  return Boolean(token);
}

export function rememberToken(value) {
  token = value;
  try {
    sessionStorage.setItem(TOKEN_KEY, value);
  } catch {
    // kept in memory only: a reload will ask for the address again
  }
}

/** Accepts the key alone or the whole address copied from the terminal. */
export function tokenFromText(text) {
  const trimmed = (text || "").trim();
  const match = /token=([A-Za-z0-9_-]{20,})/.exec(trimmed);
  if (match) return match[1];
  return /^[A-Za-z0-9_-]{20,}$/.test(trimmed) ? trimmed : null;
}

export function forgetToken() {
  token = null;
  try {
    sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    // nothing stored
  }
}

export class ApiError extends Error {
  constructor(status, code, message, data) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = data || {};
  }
}

export async function api(path, { method = "GET", body, signal } = {}) {
  const headers = { Authorization: `Bearer ${token || ""}` };
  const options = { method, headers, signal, cache: "no-store", credentials: "omit" };
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body ?? {});
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    emit("offline");
    throw new ApiError(0, "network", "network");
  }
  emit("online");
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  if (response.ok) return data;
  const problem = (data && data.error) || {};
  if (response.status === 401) {
    forgetToken();
    emit("locked");
  }
  throw new ApiError(response.status, problem.code || "error", problem.message || "", problem);
}

export function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}
