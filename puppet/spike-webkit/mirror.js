// Record all network responses for the HA demo into mirror.json so that
// measure.js can replay them identically to both engines without touching
// the network.
import { webkit } from "playwright";
import { writeFileSync } from "fs";

const URL = "https://demo.home-assistant.io/";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const proxy = process.env.HTTPS_PROXY
  ? { server: process.env.HTTPS_PROXY }
  : undefined;

const browser = await webkit.launch({ proxy });
const context = await browser.newContext({
  viewport: { width: 1000, height: 1000 },
});
const page = await context.newPage();

const responses = {};
page.on("response", async (response) => {
  try {
    const body = await response.body();
    responses[response.url()] = {
      status: response.status(),
      contentType: response.headers()["content-type"] ?? "",
      bodyBase64: body.toString("base64"),
    };
  } catch {
    // ignore bodies we cannot read (redirects, cancelled)
  }
});

await page.goto(URL, { waitUntil: "load", timeout: 60000 });
await sleep(10000);
// Scroll to force below-the-fold assets to load
await page.mouse.wheel(0, 2000);
await sleep(3000);

writeFileSync("mirror.json", JSON.stringify(responses));
console.log(`Captured ${Object.keys(responses).length} responses`);
await browser.close();
