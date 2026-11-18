// Building the page.
//
// Everything on screen is created through h(), which only ever sets text and
// attributes: no string is parsed as HTML anywhere in this application. That matters
// here more than usual, because much of what the panel shows (statements, error
// messages, client names) was written by an agent.

const SVG = "http://www.w3.org/2000/svg";

function append(parent, child) {
  if (child === null || child === undefined || child === false) return;
  if (Array.isArray(child)) {
    for (const item of child) append(parent, item);
  } else if (child instanceof Node) {
    parent.appendChild(child);
  } else {
    parent.appendChild(document.createTextNode(String(child)));
  }
}

function apply(element, props) {
  for (const [name, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") {
      element.setAttribute("class", Array.isArray(value) ? value.filter(Boolean).join(" ") : value);
    } else if (name === "on") {
      for (const [event, handler] of Object.entries(value)) {
        if (handler) element.addEventListener(event, handler);
      }
    } else if (name === "data") {
      for (const [key, entry] of Object.entries(value)) {
        if (entry !== null && entry !== undefined) element.dataset[key] = String(entry);
      }
    } else if (name === "value" && "value" in element) {
      element.value = value;
    } else if (value === true) {
      element.setAttribute(name, "");
    } else {
      element.setAttribute(name, String(value));
    }
  }
}

/** Create an element: h("button", { class: "btn", on: { click } }, "Save"). */
export function h(tag, props, ...children) {
  const element = document.createElement(tag);
  apply(element, props);
  append(element, children);
  return element;
}

export function svg(tag, props, ...children) {
  const element = document.createElementNS(SVG, tag);
  apply(element, props);
  append(element, children);
  return element;
}

export function clear(element) {
  element.replaceChildren();
  return element;
}

export function mount(element, ...children) {
  element.replaceChildren();
  append(element, children);
  return element;
}

/** A value that must be shown as an identifier (a name, a path, a rule). */
export function code(text) {
  return h("code", null, text);
}

// Icons: 24x24 outlines, drawn here so that the page loads nothing from anywhere else.
const ICONS = {
  overview: "M4 4h6v6H4z M14 4h6v6h-6z M4 14h6v6H4z M14 14h6v6h-6z",
  database:
    "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6 M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6",
  sliders: "M4 6h9 M17 6h3 M4 12h3 M11 12h9 M4 18h11 M19 18h1 M15 4v4 M9 10v4 M17 16v4",
  toolbox: "M3 9h18v10H3z M8 9V6h8v3 M3 14h18 M10 13v3h4v-3",
  list: "M9 6h11 M9 12h11 M9 18h11 M4.5 6h.01 M4.5 12h.01 M4.5 18h.01",
  users:
    "M16 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 18.5V20 M10 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z M20 20v-1.5a3.5 3.5 0 0 0-2.5-3.3 M15.5 4.2a3.5 3.5 0 0 1 0 6.6",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M5 20a7 7 0 0 1 14 0",
  file: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z M14 3v5h5 M9 13h6 M9 17h6",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 11v5 M12 8h.01",
  shield: "M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6z",
  "shield-check": "M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6z M9 12l2 2 4-4",
  "shield-alert": "M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6z M12 8v4 M12 15.5h.01",
  lock: "M6 11h12v9H6z M8.5 11V8a3.5 3.5 0 0 1 7 0v3",
  key: "M8 15a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M12 11h9 M18 11v3 M15 11v2",
  pause: "M8 5v14 M16 5v14",
  play: "M7 5l12 7-12 7z",
  plus: "M12 5v14 M5 12h14",
  trash: "M4 7h16 M9 7V4h6v3 M6 7l1 13h10l1-13 M10 11v6 M14 11v6",
  edit: "M4 20h4L19 9l-4-4L4 16z M14 6l4 4",
  copy: "M9 9h11v11H9z M5 15H4V4h11v1",
  check: "M5 12.5l4.5 4.5L19 7.5",
  x: "M6 6l12 12 M18 6L6 18",
  "alert-triangle": "M12 4l9.5 16h-19z M12 10v4 M12 17h.01",
  "alert-circle": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 8v5 M12 16h.01",
  "check-circle": "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M8.5 12.5l2.5 2.5 4.5-5",
  ban: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M5.6 5.6l12.8 12.8",
  "chevron-right": "M9 6l6 6-6 6",
  "chevron-left": "M15 6l-6 6 6 6",
  "chevron-down": "M6 9l6 6 6-6",
  "arrow-up": "M12 19V5 M6 11l6-6 6 6",
  "arrow-down": "M12 5v14 M6 13l6 6 6-6",
  search: "M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14z M20 20l-4-4",
  refresh: "M20 11a8 8 0 0 0-14.3-4.3L4 8 M4 4v4h4 M4 13a8 8 0 0 0 14.3 4.3L20 16 M20 20v-4h-4",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M12 7v5l3 2",
  activity: "M3 12h4l3-8 4 16 3-8h4",
  terminal: "M4 5h16v14H4z M7.5 9.5l3 2.5-3 2.5 M13 15h3.5",
  folder: "M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z",
  plug: "M9 3v5 M15 3v5 M6 8h12v3a6 6 0 0 1-12 0z M12 17v4",
  power: "M12 3v9 M6.3 6.8a8 8 0 1 0 11.4 0",
  sun: "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M12 2v2 M12 20v2 M4 12H2 M22 12h-2 M5 5l1.5 1.5 M17.5 17.5L19 19 M5 19l1.5-1.5 M17.5 6.5L19 5",
  moon: "M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z",
  monitor: "M3 5h18v11H3z M8 20h8 M12 16v4",
  globe:
    "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M3 12h18 M12 3c3 3.2 3 14.8 0 18 M12 3c-3 3.2-3 14.8 0 18",
  more: "M5 12h.01 M12 12h.01 M19 12h.01",
  history: "M4 12a8 8 0 1 0 2.4-5.7L4 8.5 M4 4v4.5h4.5 M12 8v4.5l3 1.5",
  undo: "M9 14L4 9l5-5 M4 9h10a6 6 0 0 1 0 12h-3",
  code: "M8 8l-4 4 4 4 M16 8l4 4-4 4 M13.5 5l-3 14",
  table: "M4 5h16v14H4z M4 10h16 M10 5v14",
  chart: "M4 20V4 M4 20h16 M8 16v-4 M12 16V8 M16 16v-6",
  eye: "M2.5 12s3.5-6.5 9.5-6.5 9.5 6.5 9.5 6.5-3.5 6.5-9.5 6.5S2.5 12 2.5 12z M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  external: "M14 4h6v6 M20 4l-9 9 M18 14v5H5V6h5",
  bolt: "M13 3L5 13.5h6L10 21l8-10.5h-6z",
  server: "M4 5h16v6H4z M4 13h16v6H4z M7.5 8h.01 M7.5 16h.01",
  download: "M12 4v11 M7 11l5 5 5-5 M5 20h14",
  layers: "M12 3l9 5-9 5-9-5z M3 13l9 5 9-5 M3 17.5l9 4.5 9-4.5",
};

export function icon(name, label) {
  const path = ICONS[name] || ICONS.info;
  return svg(
    "svg",
    {
      viewBox: "0 0 24 24",
      width: "20",
      height: "20",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "1.8",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      role: label ? "img" : null,
      "aria-label": label || null,
      "aria-hidden": label ? null : "true",
      focusable: "false",
    },
    svg("path", { d: path }),
  );
}

/** The pgharbor mark, the same drawing as the favicon. */
export function brandMark() {
  const id = `mark-${Math.random().toString(36).slice(2, 8)}`;
  return svg(
    "svg",
    { class: "brand-mark", viewBox: "0 0 152 152", "aria-hidden": "true", focusable: "false" },
    svg(
      "defs",
      null,
      svg(
        "linearGradient",
        { id, x1: "0", y1: "0", x2: "1", y2: "1" },
        svg("stop", { offset: "0", "stop-color": "#2dd4bf" }),
        svg("stop", { offset: "1", "stop-color": "#6ea8e0" }),
      ),
    ),
    svg("rect", { width: "152", height: "152", rx: "33.44", fill: `url(#${id})` }),
    svg("path", {
      d: "M30.4 42.56 A 51.68 51.68 0 1 0 121.6 42.56",
      fill: "none",
      stroke: "#06121f",
      "stroke-width": "10.64",
      "stroke-linecap": "round",
    }),
    svg("path", { d: "M53.2 51.68 V97.28 A 22.8 8.36 0 0 0 98.8 97.28 V51.68", fill: "#06121f" }),
    svg("ellipse", { cx: "76", cy: "51.68", rx: "22.8", ry: "8.36", fill: "#06121f" }),
    svg("path", {
      d: "M53.2 74.48 A 22.8 8.36 0 0 0 98.8 74.48",
      fill: "none",
      stroke: `url(#${id})`,
      "stroke-width": "3.95",
    }),
  );
}
