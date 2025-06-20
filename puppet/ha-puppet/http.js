import http from "node:http";
import { Browser } from "./screenshot.js";
import { isAddOn, hassUrl, hassToken, keepBrowserOpen } from "./const.js";
import { CannotOpenPageError } from "./error.js";

// Maximum number of next requests to keep in memory
const MAX_NEXT_REQUESTS = 100;
const BROWSER_TIMEOUT = 30_000; // Timeout for browser inactivity in milliseconds

class RequestHandler {
  constructor(browser) {
    this.browser = browser;
    this.busy = false;

    // Pending web requests
    this.pending = [];

    // Request counter to identify requests
    this.requestCount = 0;

    // Timeout identifiers for next requests
    this.nextRequests = [];

    // Time it takes to navigate to a page
    this.navigationTime = 0;

    // Last time the browser was accessed
    this.lastAccess = new Date();
  }

  _runBrowserCleanupCheck = async () => {
    if (this.busy) {
      return;
    }

    const idleTime = Date.now() - this.lastAccess.getTime();

    if (idleTime < BROWSER_TIMEOUT) {
      // Not time to clean up yet. Reschedule for the remaining time.
      const remainingTime = BROWSER_TIMEOUT - idleTime;
      this.browserCleanupTimer = setTimeout(
        this._runBrowserCleanupCheck,
        remainingTime + 100,
      );
      return;
    }

    await this.browser.cleanup();
  };

  _markBrowserAccessed() {
    clearTimeout(this.browserCleanupTimer);
    this.lastAccess = new Date();
    if (keepBrowserOpen) {
      return;
    }
    this.browserCleanupTimer = setTimeout(
      this._runBrowserCleanupCheck,
      BROWSER_TIMEOUT + 100,
    );
  }

  async handleRequest(request, response) {
    if (request.url === "/favicon.ico") {
      response.statusCode = 404;
      response.end();
      return;
    }

    const requestId = ++this.requestCount;
    console.debug(requestId, "Request", request.url);

    const start = new Date();
    if (this.busy) {
      console.log(requestId, "Busy, waiting in queue");
      await new Promise((resolve) => this.pending.push(resolve));
      const end = Date.now();
      console.log(requestId, `Wait time: ${end - start} ms`);
    }
    this.busy = true;

    try {
      console.debug(requestId, "Handling", request.url);
      const requestUrl = new URL(
        request.url,
        // We don't use this, but we need full URL for parsing.
        "http://localhost",
      );

      let extraWait = parseInt(requestUrl.searchParams.get("wait"));
      if (isNaN(extraWait)) {
        extraWait = undefined;
      }
      const viewportParams = (requestUrl.searchParams.get("viewport") || "")
        .split("x")
        .map((n) => parseInt(n));
      if (
        viewportParams.length != 2 ||
        !viewportParams.every((x) => !isNaN(x))
      ) {
        response.statusCode = 400;
        response.end();
        return;
      }

      let einkColors = parseInt(requestUrl.searchParams.get("eink"));
      if (isNaN(einkColors) || einkColors < 2) {
        einkColors = undefined;
      }

      let zoom = parseFloat(requestUrl.searchParams.get("zoom"));
      if (isNaN(zoom) || zoom <= 0) {
        zoom = 1;
      }

      const invert = requestUrl.searchParams.has("invert");

      let format = requestUrl.searchParams.get("format") || "png";
      if (!["png", "jpeg", "webp", "bmp"].includes(format)) {
        format = "png";
      }

      let rotate = parseInt(requestUrl.searchParams.get("rotate"));
      if (isNaN(rotate) || ![90, 180, 270].includes(rotate)) {
        rotate = undefined;
      }

      const lang = requestUrl.searchParams.get("lang") || undefined;
      const theme = requestUrl.searchParams.get("theme") || undefined;
      const dark = requestUrl.searchParams.has("dark");

      const requestParams = {
        pagePath: requestUrl.pathname,
        viewport: { width: viewportParams[0], height: viewportParams[1] },
        extraWait,
        einkColors,
        invert,
        zoom,
        format,
        rotate,
        lang,
        theme,
        dark,
      };

      // Extract next param and schedule if necessary
      const nextParam = requestUrl.searchParams.get("next");
      let next = parseInt(nextParam);
      if (isNaN(next) || next < 0) {
        next = undefined;
      }

      let image;
      try {
        const navigateResult = await this.browser.navigatePage(requestParams);
        console.debug(requestId, `Navigated in ${navigateResult.time} ms`);
        this.navigationTime = Math.max(
          this.navigationTime,
          navigateResult.time,
        );
        const screenshotResult = await this.browser.screenshotPage(
          requestParams,
        );
        console.debug(requestId, `Screenshot in ${screenshotResult.time} ms`);
        image = screenshotResult.image;
      } catch (err) {
        console.error(requestId, "Error generating screenshot", err);
        response.statusCode =
          err instanceof CannotOpenPageError ? err.status : 500;
        response.end();
        return;
      }

      // If eink processing happened, the format could be png or bmp
      const responseFormat = format;
      let contentType;
      if (responseFormat === "jpeg") {
        contentType = "image/jpeg";
      } else if (responseFormat === "webp") {
        contentType = "image/webp";
      } else if (responseFormat === "bmp") {
        contentType = "image/bmp";
      } else {
        contentType = "image/png";
      }

      response.writeHead(200, {
        "Content-Type": contentType,
        "Content-Length": image.length,
      });
      response.write(image);
      response.end();

      if (!next) {
        return;
      }

      // Adjust next based on time it took to process the request
      const end = new Date();
      const requestTime = end.getTime() - start.getTime();
      const nextWaitTime =
        // Convert to milliseconds
        next * 1000 -
        // We calculate next from the start of the request
        requestTime -
        // Start a bit earlier to account for the time browser warms up
        this.navigationTime -
        1000;

      if (nextWaitTime < 0) {
        return;
      }
      console.debug(requestId, `Next request in ${nextWaitTime} ms`);
      this.nextRequests.push(
        setTimeout(
          () => this.prepareNextRequest(requestId, requestParams),
          nextWaitTime,
        ),
      );
      if (this.nextRequests.length > MAX_NEXT_REQUESTS) {
        clearTimeout(this.nextRequests.shift());
      }
    } finally {
      this.busy = false;
      const resolve = this.pending.shift();
      if (resolve) {
        resolve();
      }
      this._markBrowserAccessed();
    }
  }

  async prepareNextRequest(requestId, requestParams) {
    if (this.busy) {
      console.log("Busy, skipping next request");
      return;
    }
    requestId = `${requestId}-next`;
    this.busy = true;
    console.log(requestId, "Preparing next request");
    try {
      const navigateResult = await this.browser.navigatePage({
        ...requestParams,
        // No unnecessary wait time, as we're just warming up
        extraWait: 0,
      });
      console.debug(requestId, `Navigated in ${navigateResult.time} ms`);
    } catch (err) {
      console.error(requestId, "Error preparing next request", err);
    } finally {
      this.busy = false;
      const resolve = this.pending.shift();
      if (resolve) {
        resolve();
      }
      this._markBrowserAccessed();
    }
  }
}

const browser = new Browser(hassUrl, hassToken);
const requestHandler = new RequestHandler(browser);
const port = 10000;
const server = http.createServer((request, response) =>
  requestHandler.handleRequest(request, response),
);
server.listen(port);
const now = new Date();
const serverUrl = isAddOn
  ? `http://homeassistant.local:${port}`
  : `http://localhost:${port}`;
console.log(
  `[${now.toLocaleTimeString()}] Visit server at ${serverUrl}/lovelace/0?viewport=1000x1000`,
);
