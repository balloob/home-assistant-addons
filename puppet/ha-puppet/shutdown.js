export function installShutdownHandlers(
  server,
  browser,
  {
    signalSource = process,
    exit = (code) => process.exit(code),
    logger = console,
    setTimer = setTimeout,
    clearTimer = clearTimeout,
    timeoutMs = 8_000,
  } = {},
) {
  let shuttingDown = false;

  const closeServer = () =>
    new Promise((resolve, reject) => {
      server.close((err) => {
        if (err) {
          reject(err);
          return;
        }
        resolve();
      });
    });

  const shutdown = async (signal) => {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    logger.log(`Received ${signal}, shutting down`);

    const forceExitTimer = setTimer(() => {
      logger.warn("Shutdown timed out; exiting without browser cleanup");
      exit(0);
    }, timeoutMs);
    forceExitTimer.unref?.();

    try {
      await closeServer();
      await browser.cleanup({ throwOnError: true });
    } catch (err) {
      clearTimer(forceExitTimer);
      logger.error("Error during shutdown:", err);
      exit(1);
      return;
    }

    clearTimer(forceExitTimer);
    logger.log("Shutdown complete");
    exit(0);
  };

  signalSource.once("SIGTERM", () => void shutdown("SIGTERM"));
  signalSource.once("SIGINT", () => void shutdown("SIGINT"));

  return shutdown;
}
