import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { after, before, test } from "node:test";
import puppeteer from "puppeteer";
import devices from "./devices.json" with { type: "json" };

const html = (await readFile(new URL("./html/index.html", import.meta.url), "utf8"))
  .replace("</head>", `<script>window.devices=${JSON.stringify(devices)};window.hass={network:{internal:"https://ha.test:8123"}};</script></head>`);
let browser;
before(async () => {
  browser = await puppeteer.launch({
    executablePath: process.env.PUPPETEER_EXECUTABLE_PATH,
    headless: true,
    args: ["--no-sandbox"],
  });
});
after(async () => { await browser?.close(); });

async function openPage(t) {
  const page = await browser.newPage();
  t.after(() => page.close());
  await page.setRequestInterception(true);
  page.on("request", (request) => request.respond({
    contentType: "text/html",
    body: new URL(request.url()).pathname === "/" ? html : "",
  }));
  await page.goto("http://puppet.test:10000/");
  await page.evaluate(() => {
    window.requests = [];
    window.fetch = async (url) => {
      requests.push(url);
      return { ok: true, blob: async () => new Blob(["image"]) };
    };
  });
  return page;
}

test("generated URLs use the Puppet endpoint even when HA uses HTTPS", async (t) => {
  const page = await openPage(t);
  assert.equal(new URL(await page.evaluate(() => buildUrl())).origin, "http://puppet.test:10000");
});

test("selecting a device previews its applied settings", async (t) => {
  const page = await openPage(t);
  await page.select("#device", "7.3in-spectra-e6");
  const result = await page.evaluate(() => ({ request: requests.at(-1), url: buildUrl() }));
  const url = new URL(result.url);
  assert.equal(result.request, url.pathname + url.search);
  assert.equal(url.searchParams.get("device"), "7.3in-spectra-e6");
  assert.equal(url.searchParams.has("viewport"), false);
});

test("empty device palette overrides survive URL reload", async (t) => {
  const page = await openPage(t);
  await page.select("#device", "7.3in-spectra-e6");
  await page.evaluate(() => {
    document.getElementById("colors").value = "";
    document.getElementById("palette_colors").value = "";
    updateBrowserUrl();
  });
  await page.reload();
  const params = new URL(await page.evaluate(() => buildUrl())).searchParams;
  assert.equal(params.get("colors"), "");
  assert.equal(params.get("palette_colors"), "");
});

test("import clears settings omitted from a custom screenshot URL", async (t) => {
  const page = await openPage(t);
  await page.select("#device", "7.3in-spectra-e6");
  const result = await page.evaluate(() => {
    document.getElementById("format").value = "bmp";
    document.getElementById("rotate").value = "90";
    document.getElementById("auto_height").checked = true;
    document.getElementById("height").disabled = true;
    document.getElementById("importUrlInput").value = "http://puppet.test/dashboard/test?viewport=640x480";
    importUrl();
    return { url: buildUrl(), disabled: document.getElementById("height").disabled };
  });
  const url = new URL(result.url);
  assert.equal(url.pathname, "/dashboard/test");
  assert.equal(url.searchParams.get("viewport"), "640x480");
  for (const key of ["device", "format", "rotate", "colors", "palette_colors", "dithering"]) {
    assert.equal(url.searchParams.has(key), false, key);
  }
  assert.equal(result.disabled, false);
});

test("import applies device defaults before explicit overrides", async (t) => {
  const page = await openPage(t);
  const result = await page.evaluate(() => {
    document.getElementById("importUrlInput").value = "/dashboard/test?device=seeed-reterminal-e1002&colors=&palette_colors=&viewport=640xauto&dithering=none";
    importUrl();
    return buildUrl();
  });
  const params = new URL(result).searchParams;
  assert.equal(params.get("device"), "seeed-reterminal-e1002");
  assert.equal(params.get("colors"), "");
  assert.equal(params.get("palette_colors"), "");
  assert.equal(params.get("viewport"), "640xauto");
  assert.equal(params.get("dithering"), "none");
});
