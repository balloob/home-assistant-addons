import http from "node:http";
import { Browser } from "./screenshot.js";
import { isAddOn, hassUrl, hassToken } from "./const.js";

const parseViewport = (viewportParam) => {
  const dimensions = (viewportParam || "").split("x").map(n => parseInt(n));
  if (dimensions.length !== 2 || !dimensions.every(x => !isNaN(x))) {
    return null;
  }
  return { width: dimensions[0], height: dimensions[1] };
};

const parseExtraWait = (waitParam) => {
  const wait = parseInt(waitParam);
  return isNaN(wait) ? undefined : wait;
};

const parseDarkMode = (darkParam) => {
  if (darkParam === null || darkParam === undefined) return undefined;
  return darkParam === "true" || darkParam === "1";
};

const handler = async (request, response, { browser }) => {
  console.debug("Handling", request.url);
  if (request.url === "/favicon.ico") {
    response.statusCode = 404;
    response.end();
    return;
  }

  const requestUrl = new URL(request.url, "http://localhost");
  const params = requestUrl.searchParams;

  // Parse viewport parameter
  const viewport = parseViewport(params.get("viewport"));
  if (!viewport) {
    response.statusCode = 400;
    response.write("Invalid viewport parameter. Format should be {WIDTH}x{HEIGHT}");
    response.end();
    return;
  }

  const extraWait = parseExtraWait(params.get("wait"));
  const darkMode = parseDarkMode(params.get("dark"));

  let image;
  try {
    image = await browser.screenshotHomeAssistant({
      pagePath: requestUrl.pathname,
      viewport,
      extraWait,
      darkMode,
    });
  } catch (err) {
    console.error("Error generating screenshot", err);
    response.statusCode = 500;
    response.write("Error generating screenshot");
    response.end();
    return;
  }

  response.writeHead(200, {
    "Content-Type": "image/png",
    "Content-Length": image.length,
  });
  response.write(image);
  response.end();
};

const startServer = async () => {
  const browser = new Browser(hassUrl, hassToken);
  const port = 10000;

  const server = http.createServer((request, response) =>
    handler(request, response, { browser })
  );

  server.listen(port, () => {
    const now = new Date();
    // noinspection HttpUrlsUsage
    const serverUrl = isAddOn
      ? `http://homeassistant.local:${port}`
      : `http://localhost:${port}`;
    console.log(
      `[${now.toLocaleTimeString()}] Server running at ${serverUrl}`
    );
    console.log(`Example: ${serverUrl}/lovelace/0?viewport=1000x1000`);
    console.log(`Dark mode: ${serverUrl}/lovelace/0?viewport=1000x1000&dark=true`);
  });
};

startServer().catch(err => {
  console.error("Failed to start server:", err);
  process.exit(1);
});
