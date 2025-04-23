import puppeteer from "puppeteer";
import sharp from "sharp"; // Import sharp
import { BMPEncoder } from "./bmp.js";
import { debug, isAddOn, chromiumExecutable } from "./const.js";
import { CannotOpenPageError } from "./error.js";

const HEADER_HEIGHT = 56;

// These are JSON stringified values
const hassLocalStorageDefaults = {
  dockedSidebar: `"always_hidden"`,
  selectedTheme: `{"dark": false}`,
};

// From https://www.bannerbear.com/blog/ways-to-speed-up-puppeteer-screenshots/
const puppeteerArgs = [
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
];
if (isAddOn) {
  puppeteerArgs.push("--enable-low-end-device-mode");
}

export class Browser {
  TIMEOUT = 30_000; // 30s

  constructor(homeAssistantUrl, token) {
    this.homeAssistantUrl = homeAssistantUrl;
    this.token = token;
    this.browser = undefined;
    this.page = undefined;
    this.lastAccess = new Date();
    this.busy = false;
    this.pending = [];

    // The last path we requested a screenshot for
    // We store this instead of using page.url() because panels can redirect
    // users, ie / -> /lovelace/0.
    this.lastRequestedPath = undefined;
  }

  async cleanup() {
    const diff = this.busy ? 0 : new Date() - this.lastAccess;

    // instance was used since scheduling cleanup, postpone
    if (diff < this.TIMEOUT) {
      setTimeout(() => this.cleanup(), this.TIMEOUT - diff + 1000);
      return;
    }

    this.busy = true;
    try {
      this.lastRequestedPath = undefined;
      if (this.page) {
        await this.page.close();
        this.page = undefined;
      }
      if (this.browser) {
        await this.browser.close();
        this.browser = undefined;
      }
      console.log("Closed browser");
    } finally {
      this.busy = false;
    }
  }

  async getPage() {
    if (this.page) {
      return this.page;
    }

    let browser;
    let page;

    try {
      console.log("Starting browser");
      browser = await puppeteer.launch({
        headless: "shell",
        executablePath: chromiumExecutable,
        args: puppeteerArgs,
      });
      setTimeout(() => this.cleanup(), this.TIMEOUT);
      page = await browser.newPage();

      // Route all log messages from browser to our add-on log
      // https://pptr.dev/api/puppeteer.pageevents
      page
        .on("framenavigated", (frame) =>
          // Why are we seeing so many frame navigated ??
          console.log("Frame navigated", frame.url()),
        )
        .on("console", (message) =>
          console.log(
            `CONSOLE ${message
              .type()
              .substr(0, 3)
              .toUpperCase()} ${message.text()}`,
          ),
        )
        .on("error", (err) => console.error("ERROR", err))
        .on("pageerror", ({ message }) => console.log("PAGE ERROR", message))
        .on("requestfailed", (request) =>
          console.log(
            `REQUEST-FAILED ${request.failure().errorText} ${request.url()}`,
          ),
        );
      if (debug)
        page.on("response", (response) =>
          console.log(
            `RESPONSE ${response.status()} ${response.url()} (cache: ${response.fromCache()})`,
          ),
        );
    } catch (err) {
      console.error("Error starting browser", err);
      if (page) {
        await page.close();
      }
      if (browser) {
        await browser.close();
      }
      throw new Error("Error starting browser");
    }

    this.browser = browser;
    this.page = page;
    return this.page;
  }

  async screenshotHomeAssistant({
    pagePath,
    viewport,
    extraWait,
    einkColors,
    invert,
    zoom,
    format,
    rotate,
  }) {
    let start = new Date();
    if (this.busy) {
      console.log("Busy, waiting in queue");
      await new Promise((resolve) => this.pending.push(resolve));
      const end = Date.now();
      console.log(`Wait time: ${end - start} ms`);
    }
    start = new Date();
    this.busy = true;

    try {
      const page = await this.getPage();

      // We add 56px to the height to account for the header
      // We'll cut that off from the screenshot
      const headerHeight = Math.round(HEADER_HEIGHT * zoom);
      viewport.height += headerHeight;

      const curViewport = page.viewport();

      if (
        !curViewport ||
        curViewport.width !== viewport.width ||
        curViewport.height !== viewport.height
      ) {
        await page.setViewport(viewport);
      }

      let defaultWait = isAddOn ? 750 : 500;
      let openedNewPage = false;

      // If we're still on about:blank, navigate to HA UI
      if (this.lastRequestedPath === undefined) {
        openedNewPage = true;

        // Ensure we have tokens when we open the UI
        const clientId = new URL("/", this.homeAssistantUrl).toString(); // http://homeassistant.local:8123/
        const hassUrl = clientId.substring(0, clientId.length - 1); // http://homeassistant.local:8123
        const evaluateIdentifier = await page.evaluateOnNewDocument(
          (hassUrl, clientId, token, hassLocalStorage) => {
            for (const [key, value] of Object.entries(hassLocalStorage)) {
              localStorage.setItem(key, value);
            }
            localStorage.setItem(
              "hassTokens",
              JSON.stringify({
                access_token: token,
                token_type: "Bearer",
                expires_in: 1800,
                hassUrl,
                clientId,
                expires: 9999999999999,
                refresh_token: "",
              }),
            );
          },
          hassUrl,
          clientId,
          this.token,
          hassLocalStorageDefaults,
        );

        // Open the HA UI
        const pageUrl = new URL(pagePath, this.homeAssistantUrl).toString();
        const response = await page.goto(pageUrl);
        if (!response.ok()) {
          throw new CannotOpenPageError(response.status(), pageUrl);
        }
        page.removeScriptToEvaluateOnNewDocument(evaluateIdentifier.identifier);

        // Launching browser is slow inside the add-on, give it extra time
        if (isAddOn) {
          defaultWait += 2000;
        }
      } else if (this.lastRequestedPath !== pagePath) {
        // mimick HA frontend navigation (no full reload)
        await page.evaluate((pagePath) => {
          history.replaceState(
            history.state?.root ? { root: true } : null,
            "",
            pagePath,
          );
          const event = new Event("location-changed");
          event.detail = { replace: true };
          window.dispatchEvent(event);
        }, pagePath);
      } else {
        // We are already on the correct page
        defaultWait = 0;
      }

      this.lastRequestedPath = pagePath;

      // Dismiss any dashboard update avaiable toasts
      if (
        !openedNewPage &&
        (await page.evaluate((zoomLevel) => {
          // Set zoom level
          document.body.style.zoom = zoomLevel;

          const haEl = document.querySelector("home-assistant");
          if (!haEl) return false;
          const notifyEl = haEl.shadowRoot?.querySelector(
            "notification-manager",
          );
          if (!notifyEl) return false;
          const actionEl = notifyEl.shadowRoot.querySelector(
            "ha-toast *[slot=action]",
          );
          if (!actionEl) return false;
          actionEl.click();
          return true;
        }, zoom))
      ) {
        // If we dismissed a toast, let's wait a bit longer
        defaultWait += 1000;
      } else {
        // Set zoom level
        await page.evaluate((zoomLevel) => {
          document.body.style.zoom = zoomLevel;
        }, zoom);
      }

      // Wait for the page to be loaded.
      try {
        await page.waitForFunction(
          () => {
            const haEl = document.querySelector("home-assistant");
            if (!haEl) return false;
            const mainEl = haEl.shadowRoot?.querySelector(
              "home-assistant-main",
            );
            if (!mainEl) return false;
            const panelResolver = mainEl.shadowRoot?.querySelector(
              "partial-panel-resolver",
            );
            if (!panelResolver || panelResolver._loading) {
              return false;
            }

            const panel = panelResolver.children[0];
            if (!panel) return false;

            return !("_loading" in panel) || !panel._loading;
          },
          {
            timeout: 10000,
            polling: 100,
          },
        );
      } catch (err) {
        console.log("Timeout waiting for HA to finish loading");
      }

      // wait for the work to be done.
      // Not sure yet how to decide that?
      if (extraWait === undefined) {
        extraWait = defaultWait;
      }
      if (extraWait) {
        await new Promise((resolve) => setTimeout(resolve, extraWait));
      }

      // If eink processing is requested, we need PNG input for sharp.
      // Otherwise, use the requested format.
      const screenshotType = (einkColors || format == "bmp") ? "png" : format;

      let image = await page.screenshot({
        type: screenshotType,
        clip: {
          x: 0,
          y: headerHeight,
          width: viewport.width,
          height: viewport.height - headerHeight,
        },
      });

      let sharpInstance = sharp(image);

      if (rotate) {
        sharpInstance = sharpInstance.rotate(rotate);
      }

      // Manually handle color conversion for 2 colors
      if (einkColors === 2) {
        sharpInstance = sharpInstance.threshold(220, {
          greyscale: true,
        });
        if (invert) {
          sharpInstance = sharpInstance.negate({
            alpha: false,
          });
        }
      }

      // If eink processing was requested, output PNG with specified colors
      if (einkColors) {
        if (format == "bmp") {
          if (einkColors === 2) {
            sharpInstance = sharpInstance.toColourspace("b-w");
          }
          sharpInstance = sharpInstance.raw();

          const {data, info } = await sharpInstance.toBuffer({ resolveWithObject: true });
          let bitsPerPixel = 8;
          if (einkColors === 2) {
            bitsPerPixel = 1;
          } else if (einkColors === 4) {
            bitsPerPixel = 2;
          } else if (einkColors === 16) {
            bitsPerPixel = 4;
          }
          const bmpEncoder = new BMPEncoder(info.width, info.height, bitsPerPixel);
          image = bmpEncoder.encode(data);
        } else {
          sharpInstance = sharpInstance.png({
            colours: einkColors,
          });
          image = await sharpInstance.toBuffer();
        }
      }
      // Otherwise, output in the requested format
      else if (format === "jpeg") {
        sharpInstance = sharpInstance.jpeg();
        image = await sharpInstance.toBuffer();
      } else if (format === "webp") {
        sharpInstance = sharpInstance.webp();
        image = await sharpInstance.toBuffer();
      } else if (format === "bmp") {
        sharpInstance = sharpInstance.raw();
        const {data, info } = await sharpInstance.toBuffer({ resolveWithObject: true });
        const bmpEncoder = new BMPEncoder(info.width, info.height, 24);
        image = bmpEncoder.encode(data);
      } else {
        sharpInstance = sharpInstance.png();
        image = await sharpInstance.toBuffer();
      }

      const end = Date.now();
      console.log(`Screenshot time: ${end - start} ms`);
      return image;
    } catch (err) {
      // trigger a full page navigation on next request
      this.lastRequestedPath = undefined;
      throw err;
    } finally {
      this.lastAccess = new Date();
      this.busy = false;
      const resolve = this.pending.shift();
      if (resolve) {
        resolve();
      }
    }
  }
}
