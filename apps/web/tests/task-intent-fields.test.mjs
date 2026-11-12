import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../core/components/coordination/task-intent-fields.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
});
const exports = {};
runInNewContext(outputText, {
  exports,
  crypto: { randomUUID },
  require: (specifier) =>
    specifier === "@plane/propel/button"
      ? { Button: ({ children, variant: _variant, ...props }) => createElement("button", props, children) }
      : require(specifier),
});

test("empty tasks expose requirements and an acceptance field without a configuration step", () => {
  const html = renderToStaticMarkup(
    createElement(exports.TaskIntentFields, {
      value: { requirements: "", definition_of_done: [] },
      onChange() {},
    })
  );
  assert.match(html, /Requirements/);
  assert.match(html, /Definition of done/);
  assert.match(html, /aria-label="Acceptance criterion 1"/);
  assert.match(html, /Add criterion/);
  assert.doesNotMatch(html, /Configure|Submit to agents|Human owner|Save specification/);
});

test("inherited and read-only scopes display the stored intent without edit controls", () => {
  const html = renderToStaticMarkup(
    createElement(exports.TaskIntentFields, {
      value: {
        requirements: "Standalone HTML",
        definition_of_done: [{ id: randomUUID(), description: "Works offline" }],
      },
      disabled: true,
      onChange() {},
    })
  );
  assert.match(html, /Standalone HTML/);
  assert.match(html, /<label[^>]*>Requirements<\/label>/);
  assert.match(html, /Works offline/);
  assert.match(html, /<fieldset[^>]*disabled/);
  assert.doesNotMatch(html, /Add criterion/);
});
