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

function MockCoordinationService() {
  return {};
}

function renderPanel(data) {
  const panelExports = {};
  const panelSource = readFileSync(new URL("../core/components/coordination/task-panel.tsx", import.meta.url), "utf8");
  const compiled = ts.transpileModule(panelSource, {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
  });
  const mocks = {
    swr: () => ({ data, error: new Error("Overview temporarily unavailable"), mutate: async () => {} }),
    "@plane/constants": { API_BASE_URL: "" },
    "@/services/coordination.service": { CoordinationService: MockCoordinationService },
    "@/hooks/use-task-write-state": { useTaskWriteState: () => ({ pending: false }) },
    "./helpers": { coordinationError: (error) => error.message },
    "./question-answer": exports,
    "@plane/propel/button": {
      Button: ({ children, variant: _variant, ...props }) => createElement("button", props, children),
    },
  };
  runInNewContext(compiled.outputText, {
    exports: panelExports,
    require: (specifier) => mocks[specifier] ?? require(specifier),
  });
  return renderToStaticMarkup(
    createElement(panelExports.TaskCoordinationPanel, {
      workspaceSlug: "workspace",
      projectId: "project",
      issueId: "task",
      actorId: "owner",
      creatorId: "owner",
      members: [],
      canAdmin: true,
      canWrite: true,
    })
  );
}

test("overview refresh errors retain questions and disable writes while allowing refresh", () => {
  const html = renderPanel({
    contract: { owner_ids: ["owner"], status: "awaiting_input", spec_revision: 1 },
    runs: [],
    reviewers: [],
    decisions: [],
    children: [],
    evidence: [],
    merges: [],
    questions: [{ id: "question", text: "Which audience?", answer: "", respondent: "owner", spec_revision: 1 }],
  });
  assert.match(html, /Which audience\?/);
  assert.match(html, /Overview temporarily unavailable/);
  assert.match(html, /<textarea[^>]*disabled/);
  const refresh = html.match(/<button[^>]*aria-label="Refresh workflow"[^>]*>/)?.[0];
  assert.ok(refresh);
  assert.doesNotMatch(refresh, /disabled/);
});

test("an initial overview failure without cached data still offers Retry", () => {
  const html = renderPanel(undefined);
  assert.match(html, /Overview temporarily unavailable/);
  assert.match(html, /Retry/);
  assert.doesNotMatch(html, /<textarea/);
});
