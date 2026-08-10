import assert from "node:assert/strict";
import http from "node:http";
import { EventEmitter, once } from "node:events";
import test from "node:test";

import { Browser } from "./screenshot.js";
import { installShutdownHandlers } from "./shutdown.js";

function createHarness({ browser, cleanup = async () => {}, server } = {}) {
  const signalSource = new EventEmitter();
  const calls = [];
  const timers = [];
  const testServer =
    server ??
    {
      close: (callback) => {
        calls.push("server.close");
        callback();
      },
    };
  const testBrowser =
    browser ??
    {
      cleanup: async (options) => {
        calls.push(["browser.cleanup", options]);
        await cleanup();
      },
    };
  const logger = {
    log: (message) => calls.push(["log", message]),
    warn: (message) => calls.push(["warn", message]),
    error: (...args) => calls.push(["error", ...args]),
  };
  const setTimer = (callback, timeout) => {
    const timer = { callback, timeout, unref: () => calls.push("timer.unref") };
    timers.push(timer);
    return timer;
  };
  const clearTimer = (timer) => calls.push(["timer.clear", timer]);
  const exit = (code) => calls.push(["exit", code]);

  const shutdown = installShutdownHandlers(testServer, testBrowser, {
    signalSource,
    exit,
    logger,
    setTimer,
    clearTimer,
  });

  return { calls, shutdown, signalSource, timers };
}

test("SIGTERM awaits the server before cleaning up the browser", async () => {
  const harness = createHarness();

  harness.signalSource.emit("SIGTERM");
  await new Promise(setImmediate);

  assert.deepEqual(harness.calls.slice(0, 6), [
    ["log", "Received SIGTERM, shutting down"],
    "timer.unref",
    "server.close",
    ["browser.cleanup", { throwOnError: true }],
    ["timer.clear", harness.timers[0]],
    ["log", "Shutdown complete"],
  ]);
  assert.deepEqual(harness.calls.at(-1), ["exit", 0]);
});

test("shutdown lets an in-flight HTTP response finish before cleanup", async () => {
  let releaseResponse;
  const responseGate = new Promise((resolve) => {
    releaseResponse = resolve;
  });
  const server = http.createServer(async (_request, response) => {
    await responseGate;
    response.end("ok");
  });
  const requestStarted = once(server, "request");
  server.listen(0, "127.0.0.1");
  await once(server, "listening");

  const calls = [];
  const browser = {
    cleanup: async () => calls.push("browser.cleanup"),
  };
  const shutdown = installShutdownHandlers(server, browser, {
    signalSource: new EventEmitter(),
    exit: (code) => calls.push(["exit", code]),
    logger: { log: () => {}, warn: () => {}, error: () => {} },
  });

  const { port } = server.address();
  const responsePromise = fetch(`http://127.0.0.1:${port}/`);
  await requestStarted;
  const shutdownPromise = shutdown("SIGTERM");
  await new Promise(setImmediate);

  assert.deepEqual(calls, []);
  releaseResponse();
  assert.equal(await (await responsePromise).text(), "ok");
  await shutdownPromise;
  assert.deepEqual(calls, ["browser.cleanup", ["exit", 0]]);
});

test("repeated signals start only one shutdown", async () => {
  let releaseCleanup;
  const cleanupWait = new Promise((resolve) => {
    releaseCleanup = resolve;
  });
  const harness = createHarness({ cleanup: () => cleanupWait });

  harness.signalSource.emit("SIGTERM");
  harness.signalSource.emit("SIGINT");
  releaseCleanup();
  await new Promise(setImmediate);

  assert.equal(
    harness.calls.filter((call) => call === "server.close").length,
    1,
  );
  assert.equal(
    harness.calls.filter(
      (call) => Array.isArray(call) && call[0] === "browser.cleanup",
    ).length,
    1,
  );
  assert.deepEqual(harness.calls.at(-1), ["exit", 0]);
});

test("real Browser cleanup failures make shutdown exit unsuccessfully", async (t) => {
  t.mock.method(console, "error", () => {});
  t.mock.method(console, "log", () => {});
  const pageError = new Error("page close failed");
  const browserError = new Error("browser close failed");
  const browser = new Browser("http://homeassistant:8123", "test-token");
  browser.page = {
    close: async () => {
      throw pageError;
    },
  };
  browser.browser = {
    close: async () => {
      throw browserError;
    },
  };
  const harness = createHarness({ browser });

  await harness.shutdown("SIGTERM");

  const shutdownError = harness.calls.at(-2)[2];
  assert.equal(shutdownError instanceof AggregateError, true);
  assert.deepEqual(shutdownError.errors, [pageError, browserError]);
  assert.deepEqual(harness.calls.at(-1), ["exit", 1]);
});

test("server close failures make shutdown exit unsuccessfully", async () => {
  const error = new Error("server close failed");
  const harness = createHarness({
    server: { close: (callback) => callback(error) },
  });

  await harness.shutdown("SIGTERM");

  assert.deepEqual(harness.calls.at(-2), [
    "error",
    "Error during shutdown:",
    error,
  ]);
  assert.deepEqual(harness.calls.at(-1), ["exit", 1]);
});

test("shutdown timeout exits before Supervisor kills the app", () => {
  const harness = createHarness({ cleanup: () => new Promise(() => {}) });

  void harness.shutdown("SIGTERM");
  harness.timers[0].callback();

  assert.deepEqual(harness.calls.at(-2), [
    "warn",
    "Shutdown timed out; exiting without browser cleanup",
  ]);
  assert.deepEqual(harness.calls.at(-1), ["exit", 0]);
});
