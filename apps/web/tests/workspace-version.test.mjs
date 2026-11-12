import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

test("the sidebar footer displays only the installed version without a promotional control", () => {
  const require = createRequire(import.meta.url);
  const packageJson = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  const source = readFileSync(new URL("../ce/components/workspace/edition-badge.tsx", import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
  });
  const exports = {};
  runInNewContext(outputText, {
    exports,
    require: (specifier) => (specifier === "package.json" ? packageJson : require(specifier)),
  });
  const html = renderToStaticMarkup(createElement(exports.WorkspaceEditionBadge));
  assert.match(html, new RegExp(`>v${packageJson.version.replaceAll(".", "\\.")}<`));
  assert.doesNotMatch(html, /<button|<a\b|Community|dialog/);
});
