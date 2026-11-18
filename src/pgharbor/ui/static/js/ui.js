// The small pieces every screen is built from.

import { h, icon } from "./dom.js";
import { t } from "./i18n.js";

let uid = 0;
export function nextId(prefix = "id") {
  uid += 1;
  return `${prefix}-${uid}`;
}

export function button(label, { icon: name, variant, small, onClick, title, type = "button", attrs } = {}) {
  return h(
    "button",
    {
      type,
      class: ["btn", variant, small && "small"],
      "data-tip": title || null,
      on: { click: onClick },
      ...(attrs || {}),
    },
    name ? icon(name) : null,
    label ? h("span", null, label) : null,
  );
}

export function iconButton(name, label, { variant = "ghost", small, onClick, attrs } = {}) {
  return h(
    "button",
    {
      type: "button",
      class: ["btn", "icon", variant, small && "small"],
      "aria-label": label,
      "data-tip": label,
      on: { click: onClick },
      ...(attrs || {}),
    },
    icon(name),
  );
}

export function link(label, href, { icon: name, variant, small } = {}) {
  return h("a", { class: ["btn", variant, small && "small"], href }, name ? icon(name) : null, label);
}

export function badge(text, tone, iconName) {
  return h("span", { class: ["badge", tone] }, iconName ? icon(iconName) : null, h("span", null, text));
}

export function card({ title, subtitle, actions, flush, tight, tag = "section" } = {}, ...children) {
  const head =
    title || actions
      ? h(
          "div",
          { class: "card-head" },
          h("div", null, title ? h("h3", null, title) : null, subtitle ? h("p", null, subtitle) : null),
          actions || null,
        )
      : null;
  return h(tag, { class: ["card", flush && "flush", tight && "tight"] }, head, children);
}

const BANNER_ICON = { danger: "alert-triangle", warn: "alert-triangle", ok: "check-circle" };

export function banner({ tone, title, text, actions, plain } = {}, ...children) {
  return h(
    "div",
    { class: ["banner", tone, plain && "plain"], role: tone === "danger" ? "alert" : "note" },
    icon(BANNER_ICON[tone] || "info"),
    h(
      "div",
      { class: "banner-body" },
      title ? h("div", { class: "title" }, title) : null,
      text ? h("div", { class: "small" }, text) : null,
      children,
      actions && actions.length ? h("div", { class: "banner-actions" }, actions) : null,
    ),
  );
}

export function empty({ icon: name = "info", title, text, action } = {}) {
  return h(
    "div",
    { class: "empty" },
    icon(name),
    title ? h("h3", null, title) : null,
    text ? h("p", null, text) : null,
    action || null,
  );
}

export function skeleton(height = "6rem") {
  const block = h("div", { class: "skeleton", "aria-hidden": "true" });
  block.style.setProperty("height", height);
  return block;
}

export function loading() {
  return h(
    "div",
    { class: "stack", role: "status", "aria-label": t("common.loading") },
    skeleton("5rem"),
    skeleton("12rem"),
    skeleton("8rem"),
  );
}

/** A switch: a button that says whether it is on. `onChange(next)` may refuse by returning false. */
export function toggle(checked, { label, onChange, disabled, describedBy } = {}) {
  const control = h("button", {
    type: "button",
    class: "switch",
    role: "switch",
    "aria-checked": String(Boolean(checked)),
    "aria-label": label || null,
    "aria-describedby": describedBy || null,
    disabled: Boolean(disabled),
    on: {
      click: () => {
        const next = control.getAttribute("aria-checked") !== "true";
        if (onChange && onChange(next) === false) return;
        control.setAttribute("aria-checked", String(next));
      },
    },
  });
  return control;
}

/** A few options shown side by side; exactly one is chosen. */
export function segmented(options, value, onChange, { label } = {}) {
  const group = h("div", { class: "segmented", role: "radiogroup", "aria-label": label || null });
  const buttons = options.map((option) =>
    h(
      "button",
      {
        type: "button",
        role: "radio",
        "aria-checked": String(option.value === value),
        tabindex: option.value === value || (value === undefined && option === options[0]) ? "0" : "-1",
        on: {
          click: () => select(option.value, true),
          keydown: (event) => {
            const index = options.indexOf(option);
            const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
            if (!step) return;
            event.preventDefault();
            const next = options[(index + step + options.length) % options.length];
            select(next.value, true);
            buttons[options.indexOf(next)].focus();
          },
        },
      },
      option.label,
    ),
  );
  function select(next, notify) {
    buttons.forEach((element, index) => {
      const chosen = options[index].value === next;
      element.setAttribute("aria-checked", String(chosen));
      element.setAttribute("tabindex", chosen ? "0" : "-1");
    });
    if (notify) onChange(next);
  }
  group.append(...buttons);
  group.select = (next) => select(next, false);
  return group;
}

// ---------------------------------------------------------------------- dialogs

/**
 * A modal dialog built on the native element: the browser keeps focus inside it,
 * closes it on Escape and gives focus back to where it was.
 */
export function openDialog({ title, body, footer, wide, drawer, onClose, dismissible = true } = {}) {
  const titleId = nextId("dialog");
  const dialog = h("dialog", {
    class: [wide && "wide", drawer && "drawer"],
    "aria-labelledby": titleId,
  });
  const bodyElement = h("div", { class: "dialog-body" }, body);
  const footElement = footer ? h("div", { class: "dialog-foot" }, footer) : null;
  const closeButton = iconButton("x", t("common.close"), { onClick: () => dialog.close() });
  dialog.append(
    h("div", { class: "dialog-head" }, h("h2", { id: titleId }, title), dismissible ? closeButton : null),
    bodyElement,
  );
  if (footElement) dialog.append(footElement);
  dialog.addEventListener("cancel", (event) => {
    if (!dismissible) event.preventDefault();
  });
  dialog.addEventListener("click", (event) => {
    if (dismissible && event.target === dialog) dialog.close(); // a click on the backdrop
  });
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (onClose) onClose(dialog.returnValue);
  });
  document.body.append(dialog);
  dialog.showModal();
  return {
    element: dialog,
    body: bodyElement,
    close: (value) => dialog.close(value),
    setFooter: (...children) => {
      if (footElement) footElement.replaceChildren(...children.flat().filter(Boolean));
    },
  };
}

/**
 * Ask before something that cannot be taken back with one click. With `typed`, the
 * person must type that word: for the few actions where a slip would hurt.
 */
export function confirm({ title, text, confirmLabel, tone = "primary", typed, details } = {}) {
  return new Promise((resolve) => {
    let answer = false;
    const inputId = nextId("confirm");
    const input = typed
      ? h("input", {
          id: inputId,
          class: "input",
          autocomplete: "off",
          autocapitalize: "none",
          spellcheck: "false",
        })
      : null;
    const accept = button(confirmLabel || t("common.confirm"), {
      variant: tone,
      onClick: () => {
        answer = true;
        dialog.close();
      },
    });
    if (typed) {
      accept.disabled = true;
      input.addEventListener("input", () => {
        accept.disabled = input.value.trim() !== typed;
      });
    }
    const dialog = openDialog({
      title,
      body: [
        text ? h("p", null, text) : null,
        details || null,
        typed
          ? h(
              "label",
              { class: "labeled", for: inputId },
              t("common.typeToConfirm", { word: typed }),
              input,
            )
          : null,
      ],
      footer: [button(t("common.cancel"), { onClick: () => dialog.close() }), accept],
      onClose: () => resolve(answer),
    });
    (input || accept).focus();
  });
}

// ----------------------------------------------------------------------- toasts

export function toast(text, tone = "ok", { title, timeout = 6000 } = {}) {
  const region = document.getElementById("toasts");
  const iconName = tone === "ok" ? "check-circle" : "alert-triangle";
  const element = h(
    "div",
    { class: ["toast", tone === "ok" ? null : tone] },
    icon(iconName),
    h("div", { class: "grow" }, title ? h("div", { class: "strong" }, title) : null, h("div", { class: "small" }, text)),
    iconButton("x", t("common.close"), { small: true, onClick: () => element.remove() }),
  );
  region.append(element);
  if (timeout) setTimeout(() => element.remove(), tone === "ok" ? timeout : timeout * 2);
  return element;
}

// ------------------------------------------------------------------ small parts

export function kv(pairs) {
  const list = h("dl", { class: "kv" });
  for (const [label, value] of pairs) {
    if (value === null || value === undefined || value === "") continue;
    list.append(h("dt", null, label), h("dd", null, value));
  }
  return list;
}

export function pageHead({ title, text, crumbs, actions } = {}) {
  return h(
    "div",
    { class: "page-head" },
    h(
      "div",
      { class: "grow" },
      crumbs
        ? h(
            "nav",
            { class: "crumbs", "aria-label": t("common.breadcrumb") },
            crumbs.flatMap((crumb, index) => [
              index ? icon("chevron-right") : null,
              crumb.href ? h("a", { href: crumb.href }, crumb.label) : h("span", null, crumb.label),
            ]),
          )
        : null,
      h("h1", { tabindex: "-1", id: "page-title" }, title),
      text ? h("p", null, text) : null,
    ),
    actions ? h("div", { class: "row" }, actions) : null,
  );
}

/** Mark a region as reloading without emptying it: the frame stays, a little dimmed. */
export function stale(element, on) {
  element.classList.toggle("stale", Boolean(on));
  element.setAttribute("aria-busy", String(Boolean(on)));
}

/** Run an action from a button, showing that it is working and stopping a second click. */
export async function busy(control, work) {
  if (control.classList.contains("busy")) return undefined;
  control.classList.add("busy");
  control.setAttribute("aria-busy", "true");
  try {
    return await work();
  } finally {
    control.classList.remove("busy");
    control.removeAttribute("aria-busy");
  }
}

export function copyButton(text, label) {
  const control = iconButton("copy", label || t("common.copy"), {
    small: true,
    onClick: async () => {
      try {
        await navigator.clipboard.writeText(text);
        toast(t("common.copied"));
      } catch {
        toast(t("common.copyFailed"), "warn");
      }
    },
  });
  return control;
}

// --------------------------------------------------------------------- dropdown

/**
 * The panel's own dropdown: one choice from a list.
 *
 * A button shows the current choice; the list opens under it (or above, when there is
 * no room) and is driven by mouse, touch or keyboard: arrows, Home/End, Enter or Space
 * to choose, Escape to leave, and typing a few letters jumps to a match. Focus stays on
 * the button, as the combobox pattern asks, so a screen reader hears each option.
 *
 * options: [{ value, label, hint? }]
 */
export function dropdown(options, value, onChange, { label, placeholder, small, auto, disabled, describedBy } = {}) {
  const listId = nextId("list");
  let current = value;
  let active = -1;
  let menu = null;
  let typed = "";
  let typedAt = 0;

  const text = h("span");
  const trigger = h(
    "button",
    {
      type: "button",
      class: ["input", "select", small && "small", auto && "auto"],
      role: "combobox",
      "aria-haspopup": "listbox",
      "aria-expanded": "false",
      "aria-controls": listId,
      "aria-label": label || null,
      "aria-describedby": describedBy || null,
      disabled: Boolean(disabled),
    },
    text,
    icon("chevron-down"),
  );

  function indexOf(target) {
    return options.findIndex((option) => option.value === target);
  }

  function paint() {
    const chosen = options[indexOf(current)];
    text.textContent = chosen ? chosen.label : placeholder || "";
    trigger.classList.toggle("placeholder", !chosen);
  }

  function place() {
    const box = trigger.getBoundingClientRect();
    const below = window.innerHeight - box.bottom - 8;
    const above = box.top - 8;
    const wanted = Math.min(menu.scrollHeight + 2, 320);
    const up = below < Math.min(wanted, 180) && above > below;
    menu.style.setProperty("max-height", `${Math.max(120, Math.min(wanted, up ? above : below))}px`);
    menu.style.setProperty("min-width", `${box.width}px`);
    const width = Math.max(box.width, menu.offsetWidth);
    // Under the button, from its left edge; from its right edge when that would not fit.
    const fits = box.left + width <= window.innerWidth - 8;
    const left = Math.max(8, fits ? box.left : box.right - width);
    menu.style.setProperty("left", `${left}px`);
    if (up) {
      menu.style.setProperty("top", "auto");
      menu.style.setProperty("bottom", `${window.innerHeight - box.top + 4}px`);
    } else {
      menu.style.setProperty("bottom", "auto");
      menu.style.setProperty("top", `${box.bottom + 4}px`);
    }
  }

  function highlight(index) {
    if (!menu || !options.length) return;
    active = (index + options.length) % options.length;
    [...menu.children].forEach((item, position) => item.classList.toggle("active", position === active));
    const item = menu.children[active];
    trigger.setAttribute("aria-activedescendant", item.id);
    item.scrollIntoView({ block: "nearest" });
  }

  function choose(index) {
    const option = options[index];
    close();
    if (!option || option.value === current) return;
    current = option.value;
    paint();
    onChange(current);
  }

  function outside(event) {
    if (menu && !menu.contains(event.target) && !trigger.contains(event.target)) close();
  }

  function open() {
    if (menu || trigger.disabled) return;
    menu = h(
      "ul",
      { class: "menu", id: listId, role: "listbox", "aria-label": label || null },
      options.map((option, index) =>
        h(
          "li",
          {
            id: `${listId}-${index}`,
            role: "option",
            "aria-selected": String(option.value === current),
            on: {
              click: () => choose(index),
              pointermove: () => {
                if (active !== index) highlight(index);
              },
            },
          },
          h("span", null, option.label, option.hint ? h("small", null, option.hint) : null),
          icon("check"),
        ),
      ),
    );
    // Inside a modal dialog the list must live in the dialog, or it would sit under it.
    (trigger.closest("dialog") || document.body).append(menu);
    trigger.setAttribute("aria-expanded", "true");
    place();
    highlight(Math.max(0, indexOf(current)));
    document.addEventListener("pointerdown", outside, true);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", reposition, true);
  }

  function reposition(event) {
    if (menu && event.target instanceof Node && menu.contains(event.target)) return;
    if (menu) place();
  }

  function close() {
    if (!menu) return;
    menu.remove();
    menu = null;
    active = -1;
    trigger.setAttribute("aria-expanded", "false");
    trigger.removeAttribute("aria-activedescendant");
    document.removeEventListener("pointerdown", outside, true);
    window.removeEventListener("resize", close);
    window.removeEventListener("scroll", reposition, true);
  }

  trigger.addEventListener("click", () => (menu ? close() : open()));
  trigger.addEventListener("blur", () => setTimeout(() => !trigger.matches(":focus") && close(), 120));
  trigger.addEventListener("keydown", (event) => {
    const { key } = event;
    if (!menu) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(key)) {
        event.preventDefault();
        open();
      }
      return;
    }
    if (key === "Escape") {
      event.preventDefault();
      event.stopPropagation(); // do not also close the dialog this dropdown is in
      close();
    } else if (key === "ArrowDown") {
      event.preventDefault();
      highlight(active + 1);
    } else if (key === "ArrowUp") {
      event.preventDefault();
      highlight(active - 1);
    } else if (key === "Home") {
      event.preventDefault();
      highlight(0);
    } else if (key === "End") {
      event.preventDefault();
      highlight(options.length - 1);
    } else if (key === "Enter" || key === " ") {
      event.preventDefault();
      choose(active);
    } else if (key === "Tab") {
      close();
    } else if (key.length === 1) {
      const now = Date.now();
      typed = now - typedAt > 700 ? key : typed + key;
      typedAt = now;
      const match = options.findIndex((option) =>
        String(option.label).toLowerCase().startsWith(typed.toLowerCase()),
      );
      if (match >= 0) highlight(match);
    }
  });

  paint();
  trigger.setValue = (next) => {
    current = next;
    paint();
  };
  trigger.getValue = () => current;
  return trigger;
}

/** The panel's own checkbox, with its label. */
export function checkbox(label, checked, onChange, { hint, disabled } = {}) {
  const id = nextId("check");
  const input = h("input", { type: "checkbox", id, disabled: Boolean(disabled) });
  input.checked = Boolean(checked);
  input.addEventListener("change", () => onChange(input.checked));
  const row = h(
    "label",
    { class: "check", for: id },
    input,
    h("span", null, h("span", { class: "strong" }, label), hint ? h("span", { class: "small secondary" }, ` ${hint}`) : null),
  );
  row.input = input;
  return row;
}

/** A text area that grows with what is typed in it: no resize handle. */
export function autosize(area) {
  const fit = () => {
    area.style.setProperty("height", "auto");
    area.style.setProperty("height", `${area.scrollHeight + 2}px`);
  };
  area.addEventListener("input", fit);
  requestAnimationFrame(fit);
  return area;
}

// --------------------------------------------------------------------- tooltips

/**
 * The panel's own tooltip: any element with a `data-tip` attribute shows it on hover
 * (where there is a pointer that hovers) and on keyboard focus. One floating element is
 * reused; it is kept inside the window, above the target when there is room.
 */
export function initTooltips() {
  const bubble = h("div", { class: "tooltip", role: "tooltip", hidden: true });
  let owner = null;

  function show(target) {
    const tip = target.getAttribute("data-tip");
    if (!tip) return;
    owner = target;
    bubble.textContent = tip;
    (target.closest("dialog") || document.body).append(bubble);
    bubble.hidden = false;
    const box = target.getBoundingClientRect();
    const width = bubble.offsetWidth;
    const height = bubble.offsetHeight;
    const left = Math.max(6, Math.min(box.left + box.width / 2 - width / 2, window.innerWidth - width - 6));
    const above = box.top - height - 6;
    bubble.style.setProperty("left", `${left}px`);
    bubble.style.setProperty("top", `${above >= 6 ? above : box.bottom + 6}px`);
  }

  function hide() {
    owner = null;
    bubble.hidden = true;
  }

  const hovers = window.matchMedia("(hover: hover)");
  document.addEventListener("pointerover", (event) => {
    if (!hovers.matches || event.pointerType === "touch") return;
    const target = event.target instanceof Element ? event.target.closest("[data-tip]") : null;
    if (target && target !== owner) show(target);
    else if (!target && owner) hide();
  });
  document.addEventListener("focusin", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-tip]") : null;
    if (target && target.matches(":focus-visible")) show(target);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("pointerdown", hide, true);
  document.addEventListener("keydown", (event) => event.key === "Escape" && hide());
  window.addEventListener("scroll", hide, true);
}
