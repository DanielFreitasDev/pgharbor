// Defaults: the rules and limits every connection gets unless it says otherwise.

import { on } from "../api.js";
import { h, mount } from "../dom.js";
import { Draft, fieldGroup, fieldRow } from "../fields.js";
import { t } from "../i18n.js";
import { guardUnsaved, saveBar } from "../save.js";
import { errorsUnder, readOnly, schema, store } from "../store.js";
import { banner, pageHead } from "../ui.js";

export function defaults(root) {
  let draft = null;
  let serverErrors = {};
  let saving = false;

  function build() {
    draft = new Draft(store.config.raw.defaults || {});
    serverErrors = errorsUnder("defaults", store.config.errors);
    draw();
  }

  function draw() {
    const config = store.config;
    const editable = !readOnly() && config.file.exists && config.file.is_toml;
    const specs = schema().connection.filter((spec) => spec.in_defaults);
    const ctx = {
      draft,
      readOnly: !editable,
      inherited: (key) => ({ value: specs.find((spec) => spec.key === key).default, source: "builtin" }),
      errorFor: (key) => serverErrors[key] || null,
      libpqParams: schema().libpq_params,
      productionLabels: schema().production_labels,
    };
    const groups = schema()
      .connection_groups.map((group) => {
        const rows = specs.filter((spec) => spec.group === group).map((spec) => fieldRow(spec, ctx));
        return rows.length ? fieldGroup(`group-${group}`, t(`group.${group}`), t(`group.${group}.text`), rows) : null;
      })
      .filter(Boolean);

    const bar = saveBar({
      count: () => draft.changeCount,
      collect: () => draft.ops(["defaults"], { nested: ["params"] }),
      onErrors: (errors) => {
        serverErrors = errorsUnder("defaults", errors);
        root.querySelectorAll(".field").forEach((row) => row.redraw && row.redraw());
      },
      onSaved: () => {
        saving = true;
        build();
        saving = false;
      },
      onDiscard: build,
    });
    draft.onChange(() => bar.update());
    guardUnsaved(() => !saving && draft.dirty);

    const users = Object.keys(config.raw.connections || {}).length;
    mount(
      root,
      pageHead({ title: t("defaults.title"), text: t("defaults.text") }),
      !editable ? banner({ title: t("readonly.title"), text: readOnly() ? t("readonly.text") : t("readonly.noFile") }) : null,
      banner({ plain: true, title: t("defaults.howTitle"), text: t("defaults.how", { n: users }) }),
      h("div", { class: "stack gap-5" }, groups),
      bar,
    );
  }

  build();
  const off = on("config", () => {
    if (!draft || !draft.dirty) build();
  });
  return () => {
    off();
    guardUnsaved(null);
  };
}
