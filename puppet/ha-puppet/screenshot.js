import puppeteer from "puppeteer";
import { debug, isAddOn, getChromePath } from "./const.js";

const HEADER_HEIGHT = 56;

// These are JSON stringified values
const getHassLocalStorageDefaults = (darkMode) => ({
  dockedSidebar: `"always_hidden"`,
  selectedTheme: `{"dark": ${darkMode === true}}`,
});

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
  browser = undefined;
  page = undefined;
  lastAccess = new Date();
  TIMEOUT = 30_000; // 30s
  currentDarkMode = undefined;

  constructor(homeAssistantUrl, token) {
    this.homeAssistantUrl = homeAssistantUrl;
    this.token = token;
    this.busy = false;
    this.pending = [];
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
      if (this.page) {
        await this.page.close();
        this.page = undefined;
      }
      if (this.browser) {
        await this.browser.close();
        this.browser = undefined;
      }
      this.currentDarkMode = undefined;
      console.log("Closed browser");
    } finally {
      this.busy = false;
    }
  }

  async getPage(darkMode) {
    if (this.page && this.currentDarkMode === darkMode) {
      return this.page;
    }

    if (this.page && this.currentDarkMode !== darkMode) {
      console.log(`Updating theme to dark mode = ${darkMode}`);
      await this.updateTheme(darkMode);
      this.currentDarkMode = darkMode;
      return this.page;
    }

    let browser;
    let page;

    try {
      console.log("Starting browser");
      browser = await puppeteer.launch({
        headless: "shell",
        executablePath: getChromePath(isAddOn),
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

      const clientId = new URL("/", this.homeAssistantUrl).toString(); // http://homeassistant.local:8123/
      const hassUrl = clientId.substring(0, clientId.length - 1); // http://homeassistant.local:8123

      // Open a lightweight page to set local storage
      await page.goto(`${hassUrl}/robots.txt`);

      await this.initializeLocalStorage(page, hassUrl, clientId, darkMode);
      this.currentDarkMode = darkMode;
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

  async initializeLocalStorage(page, hassUrl, clientId, darkMode) {
    const hassLocalStorage = getHassLocalStorageDefaults(darkMode);

    await page.evaluate(
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
      hassLocalStorage,
    );
  }

  async updateTheme(darkMode) {
    if (!this.page) return;

    await this.page.evaluate((darkMode) => {
      localStorage.setItem("selectedTheme", `{"dark": ${darkMode === true}}`);
      const event = new CustomEvent("settheme", {
        bubbles: true,
        composed: true,
        detail: { dark: darkMode === true }
      });
      const homeAssistant = document.querySelector("home-assistant");
      if (homeAssistant) {
        homeAssistant.dispatchEvent(event);
      } else {
        window.dispatchEvent(event);
      }
    }, darkMode);

    // Give the theme a moment to apply
    await new Promise(resolve => setTimeout(resolve, 300));
  }

  async screenshotHomeAssistant({ pagePath, viewport, extraWait, darkMode }) {
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
      const page = await this.getPage(darkMode);

      // We add 56px to the height to account for the header
      // We'll cut that off from the screenshot
      const fullViewport = {
        width: viewport.width,
        height: viewport.height + HEADER_HEIGHT
      };

      const curViewport = page.viewport();

      if (
        !curViewport ||
        curViewport.width !== fullViewport.width ||
        curViewport.height !== fullViewport.height
      ) {
        await page.setViewport(fullViewport);
      }

      let defaultWait = isAddOn ? 750 : 500;

      // If we're still on robots.txt, navigate to HA UI
      if (page.url().endsWith("/robots.txt")) {
        const pageUrl = new URL(pagePath, this.homeAssistantUrl).toString();
        await page.goto(pageUrl);

        // Launching browser is slow inside the add-on, give it extra time
        if (isAddOn) {
          defaultWait += 2000;
        }
      } else {
        // mimic HA frontend navigation (no full reload)
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
      }

      try {
        // Wait for the page to be loaded.
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
            // noinspection JSUnresolvedReference
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

        // Wait for all images and Lovelace cards to load
        await page.waitForFunction(
          () => {
            // Check if all images are loaded
            const images = Array.from(document.querySelectorAll("img"));
            const allImagesLoaded = images.every(img => img.complete);

            // Check if Lovelace UI is loaded (if we're on a Lovelace page)
            const haEl = document.querySelector("home-assistant");
            const mainEl = haEl?.shadowRoot?.querySelector("home-assistant-main");
            const panelResolver = mainEl?.shadowRoot?.querySelector("partial-panel-resolver");
            const panel = panelResolver?.children[0];

            // Check if it's a Lovelace panel
            if (panel && panel.tagName === "HUI-ROOT") {
              const lovelaceView = panel.shadowRoot?.querySelector("hui-view");
              if (!lovelaceView) return false;

              // Check if cards are still loading
              const cards = lovelaceView.shadowRoot?.querySelectorAll("hui-card-element-editor, hui-card");
              if (!cards || cards.length === 0) return false;

              // Check if any card is still loading
              for (const card of cards) {
                if (card._config === undefined || card._hass === undefined) {
                  return false;
                }
              }
            }

            return allImagesLoaded;
          },
          { timeout: 20000, polling: 200 }
        );

      } catch (err) {
        console.log("Timeout waiting for HA to finish loading");
      }

      // wait for the work to be done.
      if (extraWait === undefined) {
        extraWait = defaultWait;
      }
      if (extraWait) {
        await new Promise((resolve) => setTimeout(resolve, extraWait));
      }

      const image = await page.screenshot({
        clip: {
          x: 0,
          y: HEADER_HEIGHT,
          width: viewport.width,
          height: viewport.height,
        },
      });

      const end = Date.now();
      console.log(`Screenshot time: ${end - start} ms`);

      return image;
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
