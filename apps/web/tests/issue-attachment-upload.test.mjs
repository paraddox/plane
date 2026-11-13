import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";

function APIService() {}
function FileUploadService() {}

for (const [detected, reported, expected] of [
  ["", "text/plain", "text/plain"],
  ["image/png", "text/plain", "image/png"],
]) {
  test(`work-item attachments use ${expected} when signature is ${JSON.stringify(detected)}`, async () => {
    const posts = [];
    APIService.prototype.post = async (path, data) => {
      posts.push({ path, data });
      return { data: { asset_id: "file", attachment: { id: "file" }, upload_data: { url: "storage", fields: {} } } };
    };
    APIService.prototype.patch = async () => ({ data: {} });
    FileUploadService.prototype.uploadFile = async () => {};
    const exports = {};
    const source = readFileSync(new URL("../core/services/issue/issue_attachment.service.ts", import.meta.url), "utf8");
    const result = ts.transpileModule(source, {
      compilerOptions: { target: ts.ScriptTarget.ESNext, module: ts.ModuleKind.CommonJS },
    });
    const mocks = {
      "@plane/constants": { API_BASE_URL: "" },
      "@plane/types": { EIssueServiceType: { ISSUES: "issues" } },
      "@plane/services": {
        getFileMetaDataForUpload: async () => ({ name: "feedback.txt", size: 10, type: detected }),
        generateFileUploadPayload: () => ({}),
      },
      "@/services/api.service": { APIService },
      "@/services/file-upload.service": { FileUploadService },
    };
    runInNewContext(result.outputText, { exports, require: (specifier) => mocks[specifier] });
    const service = new exports.IssueAttachmentService();
    const attachment = await service.uploadIssueAttachment("workspace", "project", "issue", { type: reported });
    assert.equal(attachment.id, "file");
    assert.equal(posts[0].data.type, expected);
    assert.match(posts[0].path, /\/issues\/issue\/attachments\/$/);
  });
}
