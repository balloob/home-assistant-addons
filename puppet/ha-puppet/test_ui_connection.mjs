import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("./ui.js", import.meta.url), "utf8");
const fetchFunction = source.slice(
  source.indexOf("async function fetchHomeAssistantData()"),
  source.indexOf("/**\n * Handle UI page request"),
);

for (const failure of [null, "connect", "frontend/get_themes", "network/url", "fetch", "json"]) {
  test(`UI connection cleanup after ${failure || "success"}`, async () => {
    let closed = 0;
    const context = vm.createContext({
      hassUrl: "http://ha.test", hassToken: "test",
      console: { error() {} },
      createLongLivedTokenAuth: () => ({}),
      createConnection: async () => {
        if (failure === "connect") throw new Error("Cannot connect");
        return {
          sendMessagePromise: async ({ type }) => {
            if (type === failure) throw new Error("Request failed");
            return {};
          },
          close: () => closed++,
        };
      },
      fetch: async () => {
        if (failure === "fetch") throw new Error("Fetch failed");
        return { ok: true, json: async () => {
          if (failure === "json") throw new Error("Invalid JSON");
          return {};
        } };
      },
    });
    vm.runInContext(fetchFunction, context);
    const result = await vm.runInContext("fetchHomeAssistantData()", context);
    assert.equal(closed, failure === "connect" ? 0 : 1);
    assert.equal(result.themes === null, failure !== null);
  });
}
