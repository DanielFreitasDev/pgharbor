// Charts, drawn as SVG with the DOM: no library, nothing fetched.
//
// The four outcome colors were validated as a set for color-vision deficiency in both
// themes, and color is never the only carrier: every chart has a legend, values on
// hover and on keyboard focus, and a table with the same numbers.

import { h, icon, mount, svg } from "./dom.js";
import { fmt, t } from "./i18n.js";
import { button } from "./ui.js";

export const SERIES = ["ok", "refused", "other", "error"]; // stacked bottom to top

export function seriesLabel(key) {
  return t(`series.${key}`);
}

export function legend(keys = SERIES) {
  return h(
    "div",
    { class: "legend" },
    keys.map((key) => h("span", null, h("i", { class: `series-${key}` }), seriesLabel(key))),
  );
}

function niceMax(value) {
  if (value <= 4) return 4;
  const power = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (value <= step * power) return step * power;
  }
  return 10 * power;
}

/** A rectangle whose top corners are rounded: the free end of a column. */
function roundedTop(x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height);
  return `M${x} ${y + height} V${y + r} Q${x} ${y} ${x + r} ${y} H${x + width - r} Q${x + width} ${y} ${x + width} ${y + r} V${y + height} Z`;
}

/**
 * Calls over time as stacked columns.
 * buckets: [{t, ok, refused, other, error}], bucketSeconds: width of a bucket in time.
 */
export function activityChart(buckets, bucketSeconds) {
  const box = h("div", { class: "stack gap-3" });
  const chart = h("div", { class: "chart" });
  const tip = h("div", { class: "tip", hidden: true });
  const tableBox = h("div", { class: "table-wrap", hidden: true });
  let asTable = false;

  const toggleView = button(t("chart.asTable"), {
    icon: "table",
    variant: "ghost",
    small: true,
    onClick: () => {
      asTable = !asTable;
      chart.hidden = asTable;
      tableBox.hidden = !asTable;
      toggleView.querySelector("span").textContent = asTable ? t("chart.asChart") : t("chart.asTable");
    },
  });

  function rangeLabel(bucket) {
    const end = bucket.t + bucketSeconds;
    return bucketSeconds >= 86400
      ? fmt.bucket(bucket.t, bucketSeconds)
      : `${fmt.bucket(bucket.t, bucketSeconds)} – ${fmt.bucket(end, bucketSeconds)}`;
  }

  function showTip(bucket, target) {
    const total = SERIES.reduce((sum, key) => sum + bucket[key], 0);
    mount(
      tip,
      h("div", { class: "tip-title" }, rangeLabel(bucket)),
      SERIES.map((key) =>
        h("div", { class: "tip-row" }, h("i", { class: `series-${key}` }), seriesLabel(key), h("b", null, fmt.number(bucket[key]))),
      ),
      h("div", { class: "tip-row secondary" }, t("chart.total"), h("b", null, fmt.number(total))),
    );
    tip.hidden = false;
    const area = chart.getBoundingClientRect();
    const at = target.getBoundingClientRect();
    const width = tip.offsetWidth;
    let left = at.left - area.left + at.width / 2 - width / 2;
    left = Math.max(0, Math.min(left, area.width - width));
    tip.style.setProperty("left", `${left}px`);
    tip.style.setProperty("top", "0px");
  }

  function draw() {
    const width = Math.max(260, Math.floor(chart.clientWidth || 600));
    const height = width < 480 ? 190 : 230;
    const margin = { top: 12, right: 6, bottom: 24, left: 38 };
    const innerW = width - margin.left - margin.right;
    const innerH = height - margin.top - margin.bottom;
    const totals = buckets.map((b) => SERIES.reduce((sum, key) => sum + b[key], 0));
    const top = niceMax(Math.max(1, ...totals));
    const band = innerW / buckets.length;
    const barW = Math.max(2, Math.min(24, band - 2));
    const y = (value) => margin.top + innerH - (value / top) * innerH;

    const root = svg("svg", {
      viewBox: `0 0 ${width} ${height}`,
      width,
      height,
      role: "group",
      "aria-label": t("chart.activity"),
    });

    for (let tick = 0; tick <= 4; tick += 1) {
      const value = (top / 4) * tick;
      const at = y(value);
      root.append(
        svg("line", {
          class: tick === 0 ? "axis-line" : "grid-line",
          x1: margin.left,
          x2: width - margin.right,
          y1: at,
          y2: at,
        }),
        svg("text", { x: margin.left - 6, y: at + 4, "text-anchor": "end" }, fmt.compact(value)),
      );
    }

    const labelEvery = Math.max(1, Math.ceil(buckets.length / Math.max(2, Math.floor(innerW / 64))));
    const hits = [];
    buckets.forEach((bucket, index) => {
      const x = margin.left + band * index + (band - barW) / 2;
      let base = y(0);
      const present = SERIES.filter((key) => bucket[key] > 0);
      present.forEach((key, position) => {
        const full = (bucket[key] / top) * innerH;
        const isTop = position === present.length - 1;
        // A 2px gap in the surface color separates stacked segments.
        const gap = position === 0 ? 0 : 2;
        const tall = Math.max(1, full - gap);
        const topY = base - full;
        if (isTop) {
          root.append(svg("path", { class: `series-${key}`, d: roundedTop(x, topY, barW, tall, 4) }));
        } else {
          root.append(svg("rect", { class: `series-${key}`, x, y: topY, width: barW, height: tall }));
        }
        base -= full;
      });
      if (index % labelEvery === 0) {
        root.append(
          svg("text", { x: margin.left + band * index + band / 2, y: height - 6, "text-anchor": "middle" }, fmt.bucket(bucket.t, bucketSeconds)),
        );
      }
      const hit = svg("rect", {
        class: "hit",
        x: margin.left + band * index,
        y: margin.top,
        width: band,
        height: innerH,
        tabindex: index === buckets.length - 1 ? "0" : "-1",
        role: "img",
        "aria-label": `${rangeLabel(bucket)}: ${SERIES.map((key) => `${seriesLabel(key)} ${bucket[key]}`).join(", ")}`,
      });
      hit.addEventListener("pointerenter", () => showTip(bucket, hit));
      hit.addEventListener("pointerleave", () => (tip.hidden = true));
      hit.addEventListener("focus", () => showTip(bucket, hit));
      hit.addEventListener("blur", () => (tip.hidden = true));
      hit.addEventListener("keydown", (event) => {
        const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
        if (!step) return;
        event.preventDefault();
        const next = hits[index + step];
        if (next) {
          hits.forEach((other) => other.setAttribute("tabindex", "-1"));
          next.setAttribute("tabindex", "0");
          next.focus();
        }
      });
      hits.push(hit);
    });
    root.append(...hits);
    mount(chart, root, tip);
  }

  mount(
    tableBox,
    h(
      "table",
      { class: "table" },
      h("caption", { class: "sr-only" }, t("chart.activity")),
      h(
        "thead",
        null,
        h("tr", null, h("th", { scope: "col" }, t("chart.when")), SERIES.map((key) => h("th", { scope: "col", class: "num" }, seriesLabel(key)))),
      ),
      h(
        "tbody",
        null,
        buckets
          .filter((bucket) => SERIES.some((key) => bucket[key]))
          .map((bucket) =>
            h("tr", null, h("td", null, rangeLabel(bucket)), SERIES.map((key) => h("td", { class: "num" }, fmt.number(bucket[key])))),
          ),
      ),
    ),
  );

  box.append(h("div", { class: "row spread" }, legend(), toggleView), chart, tableBox);
  const observer = new ResizeObserver(() => {
    if (!chart.hidden && chart.isConnected) draw();
  });
  observer.observe(chart);
  requestAnimationFrame(draw);
  return box;
}

/**
 * A ranked list: a name, a thin bar split by outcome, the value at its tip.
 * rows: [{name, calls, ok, refused, error}], href(row) makes a row a link.
 */
export function barList(rows, { href, emptyText, segments = ["ok", "refused", "error"] } = {}) {
  if (!rows.length) return h("div", { class: "small muted" }, emptyText || t("common.nothing"));
  const max = Math.max(...rows.map((row) => row.calls || row.count || 0), 1);
  return h(
    "div",
    { class: "bars" },
    rows.map((row) => {
      const value = row.calls ?? row.count ?? 0;
      const track = h("div", { class: "bar-track", "aria-hidden": "true" });
      const parts = row.calls === undefined ? [["accent", value]] : segments.map((key) => [key, row[key] || 0]);
      const shown = row.calls === undefined ? value : parts.reduce((sum, [, amount]) => sum + amount, 0);
      for (const [key, amount] of parts) {
        if (!amount) continue;
        const piece = h("i", { class: `series-${key}` });
        piece.style.setProperty("width", `${(amount / max) * 100}%`);
        track.append(piece);
      }
      if (row.calls !== undefined && shown < value) {
        const rest = h("i", { class: "series-other" });
        rest.style.setProperty("width", `${((value - shown) / max) * 100}%`);
        track.append(rest);
      }
      const detail = [];
      if (row.refused) detail.push(t("bars.refused", { n: row.refused }));
      if (row.error) detail.push(t("bars.error", { n: row.error }));
      if (row.avg_ms !== undefined && row.avg_ms !== null) detail.push(fmt.ms(row.avg_ms));
      const content = [
        h("span", { class: "name truncate" }, row.label || row.name),
        h("span", { class: "value" }, fmt.number(value), detail.length ? h("small", null, ` · ${detail.join(" · ")}`) : null),
        track,
      ];
      const link = href ? href(row) : null;
      return link ? h("a", { class: "bar-row", href: link }, content) : h("div", { class: "bar-row" }, content);
    }),
  );
}

/** When, in the day, pgharbor is used: one cell per hour, darker where busier. */
export function hoursStrip(hours) {
  const max = Math.max(...hours, 1);
  const cells = hours.map((count, hour) => {
    const cell = h("i", {
      role: "img",
      "data-tip": t("chart.hour", { hour: String(hour).padStart(2, "0"), n: count }),
      "aria-label": t("chart.hour", { hour: String(hour).padStart(2, "0"), n: count }),
    });
    if (count) {
      const share = 18 + Math.round((count / max) * 82);
      cell.style.setProperty("background", `color-mix(in srgb, var(--series-ok) ${share}%, var(--surface-3))`);
    }
    return cell;
  });
  return h(
    "div",
    null,
    h("div", { class: "heat" }, cells),
    h("div", { class: "heat-axis", "aria-hidden": "true" }, ["00", "06", "12", "18", "23"].map((hour) => h("span", null, hour))),
  );
}

export function delta(current, previous, { upIsGood = false } = {}) {
  if (!previous && !current) return null;
  if (!previous) return h("span", { class: "delta" }, t("tile.new"));
  const change = (current - previous) / previous;
  if (Math.abs(change) < 0.005) return h("span", { class: "delta" }, t("tile.same"));
  const up = change > 0;
  const good = up === upIsGood;
  return h(
    "span",
    { class: ["delta", upIsGood === null ? null : good ? "good" : "bad"] },
    icon(up ? "arrow-up" : "arrow-down"),
    fmt.number(Math.abs(change), { style: "percent", maximumFractionDigits: 0 }),
  );
}
