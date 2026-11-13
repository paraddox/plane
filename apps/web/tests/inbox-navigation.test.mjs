import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(
  new URL("../core/components/workspace-notifications/back-button.tsx", import.meta.url),
  "utf8"
);
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
});

function renderBackButton(state) {
  const exports = {};
  const navigations = [];
  let click;
  const mocks = {
    "react-router": {
      useNavigate:
        () =>
        (...args) =>
          navigations.push(args),
    },
    "@plane/i18n": { useTranslation: () => ({ t: () => "Go back" }) },
    "@plane/propel/tooltip": { Tooltip: ({ children }) => children },
    "@plane/propel/icon-button": {
      IconButton: ({ icon: Icon, size: _size, variant: _variant, ...props }) => {
        click = props.onClick;
        return createElement("button", props, createElement(Icon));
      },
    },
  };
  runInNewContext(outputText, {
    exports,
    require: (specifier) => mocks[specifier] ?? require(specifier),
    window: { history: { state } },
  });
  const html = renderToStaticMarkup(createElement(exports.InboxBackButton, { workspaceSlug: "workspace" }));
  return { html, navigations, click: () => click() };
}

test("Inbox has an accessible return control that uses in-app history", () => {
  const button = renderBackButton({ idx: 1 });
  assert.match(button.html, /aria-label="Go back"/);
  button.click();
  assert.deepEqual(button.navigations, [[-1]]);
});

for (const state of [{ idx: 0 }, null]) {
  test(`direct Inbox visits return to the workspace when history is ${JSON.stringify(state)}`, () => {
    const button = renderBackButton(state);
    button.click();
    assert.equal(button.navigations[0][0], "/workspace/");
    assert.equal(button.navigations[0][1].replace, true);
  });
}
