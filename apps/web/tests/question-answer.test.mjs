import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../core/components/coordination/question-answer.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
});
const exports = {};
runInNewContext(outputText, {
  exports,
  require: (specifier) =>
    specifier === "@plane/propel/button"
      ? { Button: ({ children, variant: _variant, ...props }) => createElement("button", props, children) }
      : require(specifier),
});

function render(props = {}) {
  return renderToStaticMarkup(
    createElement(exports.QuestionAnswer, {
      question: "Which audience?",
      answer: "",
      canAnswer: true,
      busy: false,
      onSubmit: async () => true,
      ...props,
    })
  );
}

test("unanswered questions show a posting form", () => {
  const html = render();
  assert.match(html, /<textarea/);
  assert.match(html, /Post answer/);
  assert.doesNotMatch(html, /Edit answer/);
});

test("posted answers are collapsed and offer explicit editing", () => {
  const html = render({ answer: "Prospective customers." });
  assert.match(html, /Prospective customers\./);
  assert.match(html, /Edit answer/);
  assert.doesNotMatch(html, /<textarea|Post answer|Save answer/);
});

test("read-only and resolved questions have no editor or edit action", () => {
  const html = render({ answer: "Prospective customers.", canAnswer: false });
  assert.match(html, /Prospective customers\./);
  assert.doesNotMatch(html, /<textarea|<button/);
  assert.equal(render({ canAnswer: false }), "");
});

test("pending operations disable answer editing", () => {
  assert.match(render({ answer: "Saved answer", busy: true }), /<button[^>]*disabled/);
  assert.match(render({ busy: true }), /<textarea[^>]*disabled/);
});
