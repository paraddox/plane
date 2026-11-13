import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../core/components/coordination/project-agents.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
});
const exports = {};
const mocks = {
  swr: () => ({ data: [], mutate: async () => [] }),
  "@/services/coordination.service": { CoordinationService: function () {} },
  "./helpers": { coordinationError: String },
  "@plane/propel/dialog": { Dialog: () => null, EDialogWidth: {} },
  "@plane/propel/button": {
    Button: ({ children, variant: _variant, ...props }) => createElement("button", props, children),
  },
};
runInNewContext(outputText, { exports, require: (specifier) => mocks[specifier] ?? require(specifier) });

function render(canAdmin, agents = []) {
  return renderToStaticMarkup(
    createElement(exports.ProjectAgents, {
      workspaceSlug: "workspace",
      projectId: "project",
      canAdmin,
      agents,
      onRefresh: async () => [],
    })
  );
}

test("project admins can name and create a main agent without an email or terminal command", () => {
  const html = render(true);
  assert.match(html, /Main agent name/);
  assert.match(html, /Create main agent/);
  assert.doesNotMatch(html, /type="email"|Main agent API key/);
});

test("read-only members cannot create agents or manage their keys", () => {
  const html = render(false, [
    { id: "access", agent: "identity", name: "Planner", role: "dispatcher", is_active: true },
  ]);
  assert.match(html, /Planner/);
  assert.doesNotMatch(html, /<form|<button|Main agent name|Create main agent|Manage API keys/);
});

test("key management is available for main agents but not worker identities", () => {
  const html = render(true, [
    { id: "main", agent: "identity", name: "Planner", role: "dispatcher", is_active: true },
    { id: "worker", agent: "worker-identity", name: "Builder", role: "worker", is_active: true },
  ]);
  assert.match(html, /Manage API keys for Planner/);
  assert.doesNotMatch(html, /Manage API keys for Builder|Main agent API key/);
  assert.match(html, /Main agent/);
  assert.match(html, /Worker/);
});
