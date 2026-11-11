import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import axios, { AxiosError } from "axios";
import { APIService as SharedAPIService } from "../src/api.service.ts";
import { APIService as WebAPIService } from "../../../apps/web/core/services/api.service.ts";

const originalAdapter = axios.defaults.adapter;
afterEach(() => {
  axios.defaults.adapter = originalAdapter;
});

function mockAPI({ rejectCSRF = false, permissionDenied = false } = {}) {
  const state = { token: "initial-token", tokenFetches: 0, writes: 0, saves: 0 };
  axios.defaults.adapter = async (config) => {
    const response = { config, headers: {}, status: 200, statusText: "OK", data: {} };
    if (config.url.endsWith("/auth/get-csrf-token/")) {
      state.tokenFetches++;
      return { ...response, data: { csrf_token: state.token } };
    }

    state.writes++;
    if (permissionDenied || rejectCSRF || config.headers.get("X-CSRFTOKEN") !== state.token) {
      const detail = permissionDenied ? "Permission denied." : "CSRF Failed: CSRF token incorrect.";
      throw new AxiosError("Forbidden", "ERR_BAD_REQUEST", config, undefined, {
        ...response,
        status: 403,
        data: { detail },
      });
    }
    state.saves++;
    return response;
  };
  return state;
}

for (const [name, Service] of [
  ["shared", SharedAPIService],
  ["web", WebAPIService],
]) {
  test(`${name}: services recover after the CSRF cookie rotates`, async () => {
    const state = mockAPI();
    const first = new Service("https://plane.example");
    const second = new Service("https://plane.example");
    await first.patch("/api/test/");
    await second.patch("/api/test/");
    state.token = "rotated-token";

    assert.equal((await first.patch("/api/test/")).status, 200);
    assert.equal((await second.patch("/api/test/")).status, 200);
    await first.patch("/api/test/");
    assert.equal(state.tokenFetches, 4);
    assert.equal(state.saves, 5);
  });

  test(`${name}: stale explicit tokens also recover`, async () => {
    const state = mockAPI();
    const service = new Service("https://plane.example");
    await service.post("/api/test/", {}, { headers: { "X-CSRFTOKEN": "stale-token" } });
    assert.equal(state.tokenFetches, 1);
    assert.equal(state.saves, 1);
  });

  test(`${name}: concurrent CSRF failures share the refresh`, async () => {
    const state = mockAPI();
    const service = new Service("https://plane.example");
    await service.post("/api/test/");
    state.token = "rotated-token";

    await Promise.all([service.post("/api/test/"), service.patch("/api/test/")]);
    assert.equal(state.tokenFetches, 2);
    assert.equal(state.saves, 3);
  });

  test(`${name}: an unsuccessful refresh cannot loop`, async () => {
    const state = mockAPI({ rejectCSRF: true });
    const service = new Service("https://plane.example");
    await assert.rejects(service.post("/api/test/"), (error) => error.response.status === 403);
    assert.equal(state.tokenFetches, 2);
    assert.equal(state.writes, 2);
    assert.equal(state.saves, 0);
  });

  test(`${name}: ordinary permission errors are never replayed`, async () => {
    const state = mockAPI({ permissionDenied: true });
    const service = new Service("https://plane.example");
    await assert.rejects(service.post("/api/test/"), (error) => error.response.status === 403);
    assert.equal(state.tokenFetches, 1);
    assert.equal(state.writes, 1);
  });

  test(`${name}: external and credential-free requests do not refresh or replay`, async () => {
    const state = mockAPI({ rejectCSRF: true });
    const service = new Service("https://plane.example");
    await assert.rejects(service.post("https://uploads.example/file"));
    await assert.rejects(service.post("/file", {}, { withCredentials: false }));
    assert.equal(state.tokenFetches, 0);
    assert.equal(state.writes, 2);
  });
}
