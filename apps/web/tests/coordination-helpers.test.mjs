import assert from "node:assert/strict";
import { test } from "node:test";
import { coordinationError, durationSeconds } from "../core/components/coordination/helpers.ts";

test("configured Django durations retain their exact numeric values", () => {
  assert.equal(durationSeconds(null), "");
  assert.equal(durationSeconds("00:05:00"), 300);
  assert.equal(durationSeconds("1 02:03:04.5"), 93784.5);
  assert.equal(durationSeconds("bad"), "");
});

test("workflow errors show the actual gate instead of losing the reason", () => {
  assert.equal(
    coordinationError({ response: { status: 409, data: { detail: "Every requested reviewer must approve." } } }),
    "Every requested reviewer must approve."
  );
  assert.equal(
    coordinationError({
      response: { status: 400, data: { owner: ["Select an active human."], requirements: ["Required."] } },
    }),
    "Select an active human. Required."
  );
  assert.equal(coordinationError(new Error("private transport details")), "The request failed. Try again.");
  assert.equal(coordinationError(null), "The request failed. Try again.");
});
