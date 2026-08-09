import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import { installShutdownHandlers } from "./shutdown.js";

function createHarness({ cleanup = async () => {} } = {}) {
  const signalSource = new EventEmitter();
  const calls = [];
  const timers = [];
  const server = { close: () => calls.push("server.close") };
  const browser = {
    cleanup: async () => {
      calls.push("browser.cleanup");
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

  const shutdown = installShutdownHandlers(server, browser, {
    signalSource,
    exit,
    logger,
    setTimer,
    clearTimer,
  });

  return { calls, shutdown, signalSource, timers };
}

test("SIGTERM closes the server and browser before exiting successfully", async () => {
  const harness = createHarness();

  harness.signalSource.emit("SIGTERM");
  await new Promise(setImmediate);

  assert.deepEqual(harness.calls.slice(0, 5), [
    ["log", "Received SIGTERM, shutting down"],
    "timer.unref",
    "server.close",
    "browser.cleanup",
    ["timer.clear", harness.timers[0]],
  ]);
  assert.deepEqual(harness.calls.slice(5), [
    ["log", "Shutdown complete"],
    ["exit", 0],
  ]);
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
    harness.calls.filter((call) => call === "browser.cleanup").length,
    1,
  );
  assert.deepEqual(harness.calls.at(-1), ["exit", 0]);
});

test("cleanup failures exit unsuccessfully", async () => {
  const error = new Error("cleanup failed");
  const harness = createHarness({
    cleanup: async () => {
      throw error;
    },
  });

  await harness.shutdown("SIGTERM");

  assert.deepEqual(harness.calls.at(-2), [
    "error",
    "Error during shutdown:",
    error,
  ]);
  assert.deepEqual(harness.calls.at(-1), ["exit", 1]);
});

test("shutdown timeout still exits cleanly before Supervisor kills the app", () => {
  const harness = createHarness({ cleanup: () => new Promise(() => {}) });

  void harness.shutdown("SIGTERM");
  harness.timers[0].callback();

  assert.deepEqual(harness.calls.at(-2), [
    "warn",
    "Shutdown timed out; exiting without browser cleanup",
  ]);
  assert.deepEqual(harness.calls.at(-1), ["exit", 0]);
});
