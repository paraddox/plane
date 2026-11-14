import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../core/components/coordination/project-settings.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
});
const exports = {};
const mocks = {
  swr: ([key]) => ({
    data: key === "coordination-policy" ? { enabled: true } : [],
  }),
  "@/services/coordination.service": { CoordinationService: function () {} },
  "./helpers": { coordinationError: String },
  "./project-agents": { ProjectAgents: () => null },
  "@plane/propel/button": {
    Button: ({ children, variant: _variant, ...props }) => createElement("button", props, children),
  },
};
runInNewContext(outputText, { exports, require: (specifier) => mocks[specifier] ?? require(specifier) });

test("project policy has no repository setup or timed worker authority", () => {
  const html = renderToStaticMarkup(
    createElement(exports.CoordinationProjectSettings, {
      workspaceSlug: "workspace",
      projectId: "project",
      canAdmin: true,
    })
  );
  assert.match(html, /Agent execution enabled/);
  assert.match(html, /Create release/);
  assert.doesNotMatch(
    html,
    /Repositories|Repository \(owner\/name\)|Base branch|Credential lifetime|Execution lease|Add repository|Required CI checks|Context references/
  );
});
