import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
function MockService() {}
const Conversation = () => null;
const source = readFileSync(new URL("../core/components/coordination/conversation.tsx", import.meta.url), "utf8");
function load(react = React) {
  const compiled = ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ESNext, jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS },
  });
  const exports = {};
  const mocks = {
    react,
    "@plane/constants": { API_BASE_URL: "" },
    "@plane/types": { EFileAssetType: { ISSUE_ATTACHMENT: "ISSUE_ATTACHMENT" } },
    "@plane/propel/button": { Button: ({ children }) => React.createElement("button", null, children) },
    "@plane/propel/dialog": {
      Dialog: Object.assign(({ children }) => children, { Panel: "div", Title: "h2" }),
      EDialogWidth: {},
    },
    "@/services/api.service": { APIService: MockService },
    "@/services/file.service": { FileService: MockService },
    "./helpers": { coordinationError: String },
  };
  runInNewContext(compiled.outputText + "\nexports.ConversationDialog = ConversationDialog;", {
    exports,
    require: (specifier) => mocks[specifier] ?? require(specifier),
  });
  return exports;
}

test("conversation starts collapsed with an accessible entry point", () => {
  const component = load();
  const html = renderToStaticMarkup(
    React.createElement(component.AgentConversation, {
      workspaceSlug: "test",
      projectId: "project",
      issueId: "issue",
      canWrite: true,
    })
  );
  assert.match(html, /Agent conversation/);
  assert.doesNotMatch(html, /<textarea|role="dialog"/);
});

test("scroll effect never returns the browser's scroll result as a React cleanup", () => {
  const effects = [];
  const component = load({
    ...React,
    useState: (initial) => [initial, () => {}],
    useRef: () => ({ current: { scrollIntoView: () => Promise.resolve() } }),
    useEffect: (effect) => effects.push(effect),
  });
  component.ConversationDialog({
    workspaceSlug: "test",
    projectId: "project",
    issueId: "issue",
    canWrite: true,
    onClose: () => {},
  });
  assert.equal(effects[1](), undefined);
});

test("switching work items remounts conversation state instead of carrying a draft to another task", () => {
  const wrapper = readFileSync(
    new URL("../core/components/issues/issue-detail/task-coordination.tsx", import.meta.url),
    "utf8"
  );
  const compiled = ts.transpileModule(wrapper, {
    compilerOptions: { target: ts.ScriptTarget.ESNext, jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS },
  });
  const exports = {};
  const mocks = {
    "mobx-react": { observer: (component) => component },
    "@plane/constants": { EUserPermissions: { ADMIN: "admin" }, EUserPermissionsLevel: { PROJECT: "project" } },
    "@/components/coordination/task-panel": { TaskCoordinationPanel: () => null },
    "@/components/coordination/task-intent-editor": { TaskIntentEditor: () => null },
    "@/components/coordination/conversation": { AgentConversation: Conversation },
    "@/hooks/store/use-issue-detail": { useIssueDetail: () => ({ issue: { getIssueById: () => ({}) } }) },
    "@/hooks/store/use-member": { useMember: () => ({ project: { getProjectMemberIds: () => [] } }) },
    "@/hooks/store/user": {
      useUser: () => ({ data: { id: "human" } }),
      useUserPermissions: () => ({ allowPermissions: () => true }),
    },
  };
  runInNewContext(compiled.outputText, { exports, require: (name) => mocks[name] ?? require(name) });
  for (const issueId of ["first", "second"]) {
    const element = exports.IssueTaskCoordination({ section: "execution", issueId });
    const conversation = React.Children.toArray(element.props.children).find((child) => child.type === Conversation);
    assert.equal(conversation.props.issueId, issueId);
    assert.ok(conversation.key.includes(issueId));
  }
});

for (const [state, label] of [
  ["offline", "Archive pending"],
  ["archived", "Archived conversation"],
]) {
  test(`closed ${state} sessions show ${label} without a composer`, () => {
    let index = 0;
    const component = load({
      ...React,
      useState: (initial) => [index++ === 0 ? { closed: true, state, messages: [], error: "" } : initial, () => {}],
      useRef: (initial) => ({ current: initial }),
      useEffect: () => {},
    });
    const html = renderToStaticMarkup(
      React.createElement(component.ConversationDialog, {
        workspaceSlug: "test",
        projectId: "project",
        issueId: "issue",
        canWrite: true,
        onClose: () => {},
      })
    );
    assert.ok(html.includes(label));
    assert.doesNotMatch(html, /<textarea|Message agent/);
  });
}
