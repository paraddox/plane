import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const require = createRequire(import.meta.url);

function loadSource(path) {
  const source = readFileSync(new URL(path, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, esModuleInterop: true },
  });
  const exports = {};
  runInNewContext(outputText, { exports, require });
  return exports;
}

test("workspace navigation and command search exclude billing while keeping other settings", () => {
  const { WORKSPACE_SETTINGS, GROUPED_WORKSPACE_SETTINGS, WORKSPACE_SETTINGS_ACCESS } = loadSource(
    "../../../packages/constants/src/settings/workspace.ts"
  );
  assert.deepEqual(Object.keys(WORKSPACE_SETTINGS), ["general", "members", "export", "webhooks"]);
  const navigationKeys = Object.values(GROUPED_WORKSPACE_SETTINGS).flatMap((items) => items.map((item) => item.key));
  assert.deepEqual(navigationKeys, Object.keys(WORKSPACE_SETTINGS));
  assert.equal(WORKSPACE_SETTINGS_ACCESS["/settings/billing"], undefined);
});

test("billing has no route while workspace and project settings remain available", () => {
  const { coreRoutes } = loadSource("../app/routes/core.ts");
  const paths = [];
  function collect(entries) {
    for (const entry of entries) {
      if (entry.path) paths.push(entry.path);
      if (entry.children) collect(entry.children);
    }
  }
  collect(coreRoutes);
  assert.equal(paths.includes(":workspaceSlug/settings/billing"), false);
  assert.ok(paths.includes(":workspaceSlug/settings"));
  assert.ok(paths.includes(":workspaceSlug/settings/members"));
  assert.ok(paths.includes(":workspaceSlug/settings/projects/:projectId/coordination"));
});
