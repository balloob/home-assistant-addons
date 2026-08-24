// Spike: compare memory of Playwright WebKit vs Chromium headless shell
// rendering a Home Assistant dashboard (public HA demo).
//
// Usage: node measure.js <engine> <url> [outDir]
//   engine: webkit | chromium-shell | chromium-full
//
// Prints one JSON object to stdout with timings and PSS memory of the
// browser process tree (sum of /proc/<pid>/smaps_rollup Pss for all
// descendants of this process, excluding node itself).

import { chromium, webkit } from "playwright";
import { readFileSync, readdirSync, writeFileSync, mkdirSync } from "fs";

const HEADLESS_SHELL =
  "/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell";
const CHROME_FULL = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome";

// Same flags the add-on passes to Puppeteer (screenshot.js)
const addonArgs = [
  "--autoplay-policy=user-gesture-required",
  "--disable-background-networking",
  "--disable-background-timer-throttling",
  "--disable-backgrounding-occluded-windows",
  "--disable-breakpad",
  "--disable-client-side-phishing-detection",
  "--disable-component-update",
  "--disable-default-apps",
  "--disable-dev-shm-usage",
  "--disable-domain-reliability",
  "--disable-extensions",
  "--disable-features=AudioServiceOutOfProcess",
  "--disable-hang-monitor",
  "--disable-ipc-flooding-protection",
  "--disable-notifications",
  "--disable-offer-store-unmasked-wallet-cards",
  "--disable-popup-blocking",
  "--disable-print-preview",
  "--disable-prompt-on-repost",
  "--disable-renderer-backgrounding",
  "--disable-setuid-sandbox",
  "--disable-speech-api",
  "--disable-sync",
  "--hide-scrollbars",
  "--ignore-gpu-blacklist",
  "--metrics-recording-only",
  "--mute-audio",
  "--no-default-browser-check",
  "--no-first-run",
  "--no-pings",
  "--no-sandbox",
  "--no-zygote",
  "--password-store=basic",
  "--use-gl=swiftshader",
  "--use-mock-keychain",
  "--enable-low-end-device-mode",
];

const [engine, url, outDir = "out"] = process.argv.slice(2);

function descendantsOf(rootPid) {
  const children = new Map();
  for (const dir of readdirSync("/proc")) {
    if (!/^\d+$/.test(dir)) continue;
    let stat;
    try {
      stat = readFileSync(`/proc/${dir}/stat`, "utf8");
    } catch {
      continue;
    }
    const afterComm = stat.slice(stat.lastIndexOf(")") + 2);
    const ppid = Number(afterComm.split(" ")[1]);
    if (!children.has(ppid)) children.set(ppid, []);
    children.get(ppid).push(Number(dir));
  }
  const result = [];
  const queue = [rootPid];
  while (queue.length) {
    const pid = queue.shift();
    for (const child of children.get(pid) ?? []) {
      result.push(child);
      queue.push(child);
    }
  }
  return result;
}

function pssKb(pid) {
  try {
    const rollup = readFileSync(`/proc/${pid}/smaps_rollup`, "utf8");
    return Number(rollup.match(/^Pss:\s+(\d+) kB/m)[1]);
  } catch {
    return 0;
  }
}

function procName(pid) {
  try {
    return readFileSync(`/proc/${pid}/cmdline`, "utf8")
      .split("\0")[0]
      .split("/")
      .pop();
  } catch {
    return "?";
  }
}

function measureTree() {
  const pids = descendantsOf(process.pid);
  const perProc = pids
    .map((pid) => ({ pid, name: procName(pid), pssKb: pssKb(pid) }))
    .filter((p) => p.pssKb > 0);
  const totalKb = perProc.reduce((sum, p) => sum + p.pssKb, 0);
  return { totalMb: Math.round(totalKb / 1024), perProc };
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const proxy = process.env.HTTPS_PROXY
    ? { server: process.env.HTTPS_PROXY }
    : undefined;

  const t0 = Date.now();
  let browser;
  if (engine === "webkit") {
    browser = await webkit.launch({ proxy });
  } else if (engine === "chromium-shell") {
    browser = await chromium.launch({
      executablePath: HEADLESS_SHELL,
      args: addonArgs,
      proxy,
    });
  } else if (engine === "chromium-full") {
    browser = await chromium.launch({
      executablePath: CHROME_FULL,
      args: addonArgs,
      proxy,
    });
  } else {
    throw new Error(`Unknown engine: ${engine}`);
  }
  const tLaunch = Date.now();

  const context = await browser.newContext({
    viewport: { width: 1000, height: 1000 },
    reducedMotion: "reduce",
    ignoreHTTPSErrors: false,
  });
  const page = await context.newPage();

  // Replay pre-recorded responses (mirror.json) so both engines get
  // identical bytes and no engine touches the network.
  const mirror = JSON.parse(readFileSync("mirror.json", "utf8"));
  await page.route("**/*", (route) => {
    const entry = mirror[route.request().url()];
    if (entry) {
      route.fulfill({
        status: entry.status,
        contentType: entry.contentType,
        body: Buffer.from(entry.bodyBase64, "base64"),
      });
    } else {
      console.error("MISS", route.request().url());
      route.fulfill({ status: 404, body: "" });
    }
  });

  await page.goto(url, { waitUntil: "load", timeout: 60000 });
  const tLoad = Date.now();

  // Settle: HA frontend keeps rendering after load (charts, icons, images)
  await sleep(8000);
  const tSettle = Date.now();

  mkdirSync(outDir, { recursive: true });
  const shot = await page.screenshot({ type: "png" });
  writeFileSync(`${outDir}/${engine}.png`, shot);
  const tShot = Date.now();

  const afterScreenshot = measureTree();
  await sleep(10000);
  const idle = measureTree();

  console.log(
    JSON.stringify(
      {
        engine,
        url,
        launchMs: tLaunch - t0,
        gotoMs: tLoad - tLaunch,
        screenshotMs: tShot - tSettle,
        pssAfterScreenshotMb: afterScreenshot.totalMb,
        pssIdleMb: idle.totalMb,
        procs: idle.perProc
          .sort((a, b) => b.pssKb - a.pssKb)
          .map((p) => `${p.name}:${Math.round(p.pssKb / 1024)}MB`),
      },
      null,
      2,
    ),
  );

  await browser.close();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
