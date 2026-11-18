// Languages. The dictionaries are plain JSON files, one per language; English is
// always loaded as well and fills in for any key a translation does not have.

export const LANGUAGES = [
  { code: "en", name: "English" },
  { code: "pt-BR", name: "Português (Brasil)" },
  { code: "es", name: "Español" },
];

const STORAGE_KEY = "pgharbor.language";
let current = "en";
let dictionary = {};
let fallback = {};

function preferred() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (LANGUAGES.some((language) => language.code === saved)) return saved;
  } catch {
    // no storage: follow the browser
  }
  for (const tag of navigator.languages || [navigator.language || "en"]) {
    const lower = tag.toLowerCase();
    if (lower.startsWith("pt")) return "pt-BR";
    if (lower.startsWith("es")) return "es";
    if (lower.startsWith("en")) return "en";
  }
  return "en";
}

async function fetchDictionary(code) {
  const response = await fetch(`/i18n/${code}.json`);
  if (!response.ok) throw new Error(`language file ${code}: ${response.status}`);
  return response.json();
}

export async function initLanguage() {
  fallback = await fetchDictionary("en");
  await setLanguage(preferred(), { remember: false });
}

export async function setLanguage(code, { remember = true } = {}) {
  dictionary = code === "en" ? fallback : await fetchDictionary(code).catch(() => fallback);
  current = code;
  document.documentElement.setAttribute("lang", code);
  if (remember) {
    try {
      localStorage.setItem(STORAGE_KEY, code);
    } catch {
      // not remembered, still applied
    }
  }
}

export function language() {
  return current;
}

export function has(key) {
  return key in dictionary || key in fallback;
}

/**
 * The text for a key. `{name}` is replaced from `values`; a text written as
 * "one|many" picks a side from `values.n`.
 */
export function t(key, values) {
  let text = dictionary[key] ?? fallback[key];
  if (text === undefined) return key;
  if (text.includes("|") && values) {
    // The amount decides: `n`, or `count` when the text was written around that name.
    const amount = Number(values.n ?? values.count);
    if (!Number.isNaN(amount)) {
      const [one, many] = text.split("|");
      text = amount === 1 ? one : many;
    }
  }
  if (!values) return text;
  return text.replace(/\{(\w+)\}/g, (match, name) =>
    values[name] === undefined || values[name] === null ? match : String(values[name]),
  );
}

/** The text for the first of several keys that exists (specific before generic). */
export function tAny(keys, values) {
  for (const key of keys) {
    if (has(key)) return t(key, values);
  }
  return keys[keys.length - 1];
}

// ------------------------------------------------------------------ formatting

function seconds(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "number") return value;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? null : parsed / 1000;
}

export const fmt = {
  number(value, options) {
    if (value === null || value === undefined) return "–";
    return new Intl.NumberFormat(current, options).format(value);
  },

  compact(value) {
    if (value === null || value === undefined) return "–";
    if (Math.abs(value) < 10000) return new Intl.NumberFormat(current).format(value);
    return new Intl.NumberFormat(current, {
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(value);
  },

  percent(part, whole) {
    if (!whole) return "–";
    return new Intl.NumberFormat(current, { style: "percent", maximumFractionDigits: 1 }).format(
      part / whole,
    );
  },

  /** A duration given in milliseconds, in the unit a person would say it. */
  ms(value) {
    if (value === null || value === undefined) return "–";
    if (value < 1000) return `${fmt.number(Math.round(value))} ms`;
    if (value < 60000) return `${fmt.number(value / 1000, { maximumFractionDigits: 1 })} s`;
    if (value < 3600000) return `${fmt.number(value / 60000, { maximumFractionDigits: 1 })} min`;
    return `${fmt.number(value / 3600000, { maximumFractionDigits: 1 })} h`;
  },

  bytes(value) {
    if (value === null || value === undefined) return "–";
    const units = ["B", "KB", "MB", "GB"];
    let size = value;
    let unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit += 1;
    }
    return `${fmt.number(size, { maximumFractionDigits: unit ? 1 : 0 })} ${units[unit]}`;
  },

  dateTime(value) {
    const at = seconds(value);
    if (at === null) return "–";
    return new Intl.DateTimeFormat(current, { dateStyle: "medium", timeStyle: "medium" }).format(
      at * 1000,
    );
  },

  time(value) {
    const at = seconds(value);
    if (at === null) return "–";
    return new Intl.DateTimeFormat(current, { timeStyle: "medium" }).format(at * 1000);
  },

  /** A short label for one bar of a chart, by the width of the bar in time. */
  bucket(value, bucketSeconds) {
    const at = seconds(value);
    if (at === null) return "";
    const options =
      bucketSeconds >= 86400
        ? { day: "2-digit", month: "short" }
        : { hour: "2-digit", minute: "2-digit" };
    return new Intl.DateTimeFormat(current, options).format(at * 1000);
  },

  /** "3 minutes ago", in the current language. */
  relative(value, now = Date.now() / 1000) {
    const at = seconds(value);
    if (at === null) return "–";
    const delta = at - now;
    const steps = [
      ["second", 60],
      ["minute", 60],
      ["hour", 24],
      ["day", 30],
      ["month", 12],
      ["year", Infinity],
    ];
    let amount = delta;
    for (const [unit, size] of steps) {
      if (Math.abs(amount) < size) {
        if (unit === "second" && Math.abs(amount) < 5) return t("time.now");
        return new Intl.RelativeTimeFormat(current, { numeric: "auto" }).format(
          Math.round(amount),
          unit,
        );
      }
      amount /= size;
    }
    return "–";
  },
};

export { seconds as toSeconds };
