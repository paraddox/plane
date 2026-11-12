import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../core/components/comments/comment-create.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
});

for (const projectId of ["project-id", undefined]) {
  test(`new comment uploads bind to the returned comment, not the task (${projectId ?? "workspace"})`, async () => {
    const calls = [];
    let submit;
    const exports = {};
    const mocks = {
      react: {
        useRef: () => ({ current: { isEditorReadyToDiscard: () => true, clearEditor: () => {} } }),
        useState: () => [["uploaded-image"], () => {}],
      },
      "mobx-react": { observer: (component) => component },
      "react-hook-form": {
        Controller: () => null,
        useForm: () => ({
          handleSubmit: (handler) => {
            submit = handler;
            return () => {};
          },
          watch: () => "<p>Feedback</p>",
          formState: {},
          reset: () => {},
        }),
      },
      "@plane/constants": {},
      "@plane/utils": { cn: (value) => value, isCommentEmpty: () => false },
      "@/components/editor/lite-text": { LiteTextEditor: () => null },
      "@/hooks/store/use-workspace": { useWorkspace: () => ({ getWorkspaceBySlug: () => ({ id: "workspace-id" }) }) },
      "@/services/file.service": {
        FileService: class {
          async updateBulkProjectAssetsUploadStatus(...args) {
            calls.push(args);
          }
          async updateBulkWorkspaceAssetsUploadStatus(...args) {
            calls.push(args);
          }
        },
      },
    };
    runInNewContext(outputText, { exports, console, require: (specifier) => mocks[specifier] ?? require(specifier) });
    const element = exports.CommentCreate({
      workspaceSlug: "workspace",
      entityId: "task-id",
      projectId,
      activityOperations: { createComment: async () => ({ id: "new-comment-id" }) },
    });
    element.props.onKeyDown({ key: "Enter" });
    await submit({ comment_html: "<p>Feedback</p>" });
    assert.equal(calls.length, 1);
    assert.equal(calls[0][projectId ? 2 : 1], "new-comment-id");
    assert.equal(calls[0].at(-1).asset_ids[0], "uploaded-image");
  });
}
