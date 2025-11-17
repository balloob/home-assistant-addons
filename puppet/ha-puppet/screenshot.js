import puppeteer from "puppeteer";
import sharp from "sharp"; // Import sharp
import { BMPEncoder } from "./bmp.js";
import { debug, isAddOn, chromiumExecutable } from "./const.js";
import { CannotOpenPageError } from "./error.js";

const HEADER_HEIGHT = 56;

// Dithering algorithms
function applyDithering(data, width, height, palette, channels = 4, algorithm = "atkinson") {
  // Convert hex colors to RGB
  const rgbPalette = palette.map(hex => {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return [r, g, b];
  });

  // Function to find the closest color in the palette
  function findClosestColor(r, g, b) {
    let minDistance = Infinity;
    let closestColor = rgbPalette[0];

    for (const color of rgbPalette) {
      const distance = Math.sqrt(
        Math.pow(r - color[0], 2) +
        Math.pow(g - color[1], 2) +
        Math.pow(b - color[2], 2)
      );

      if (distance < minDistance) {
        minDistance = distance;
        closestColor = color;
      }
    }

    return closestColor;
  }

  if (algorithm === "none") {
    // Simple nearest color mapping without dithering
    const result = new Uint8Array(data);
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const idx = (y * width + x) * channels;
        const [newR, newG, newB] = findClosestColor(result[idx], result[idx + 1], result[idx + 2]);
        result[idx] = newR;
        result[idx + 1] = newG;
        result[idx + 2] = newB;
      }
    }
    return result;
  }

  // Apply error diffusion dithering
  return applyErrorDiffusionDithering(data, width, height, rgbPalette, channels, algorithm, findClosestColor);
}

function applyErrorDiffusionDithering(data, width, height, rgbPalette, channels, algorithm, findClosestColor) {
  // Create a copy of the data to work with
  const result = new Uint8Array(data);

  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const idx = (y * width + x) * channels;

      const oldR = result[idx];
      const oldG = result[idx + 1];
      const oldB = result[idx + 2];

      const [newR, newG, newB] = findClosestColor(oldR, oldG, oldB);

      result[idx] = newR;
      result[idx + 1] = newG;
      result[idx + 2] = newB;
      // Keep alpha channel unchanged if it exists
      if (channels === 4) {
        // Alpha channel remains the same
      }

      const errorR = oldR - newR;
      const errorG = oldG - newG;
      const errorB = oldB - newB;

      // Distribute error to neighboring pixels
      const distribute = (dx, dy, factor, dampening = 1.0) => {
        const newX = x + dx;
        const newY = y + dy;

        if (newX >= 0 && newX < width && newY >= 0 && newY < height) {
          const newIdx = (newY * width + newX) * channels;
          const dampedErrorR = errorR * dampening;
          const dampedErrorG = errorG * dampening;
          const dampedErrorB = errorB * dampening;
          result[newIdx] = Math.max(0, Math.min(255, result[newIdx] + dampedErrorR * factor));
          result[newIdx + 1] = Math.max(0, Math.min(255, result[newIdx + 1] + dampedErrorG * factor));
          result[newIdx + 2] = Math.max(0, Math.min(255, result[newIdx + 2] + dampedErrorB * factor));
          // Don't modify alpha channel
        }
      };

      // Apply different error distribution patterns based on algorithm
      if (algorithm === "floyd-steinberg") {
        // Floyd-Steinberg error distribution (classic, sharper)
        distribute(1, 0, 7/16);  // Right
        distribute(-1, 1, 3/16); // Bottom-left
        distribute(0, 1, 5/16);  // Bottom
        distribute(1, 1, 1/16);  // Bottom-right
      } else if (algorithm === "atkinson") {
        // Atkinson dithering error distribution (softer)
        const errorDampening = 0.75; // Reduce error intensity for softer dithering
        distribute(1, 0, 1/8, errorDampening);   // Right
        distribute(2, 0, 1/8, errorDampening);   // Right + 1
        distribute(-1, 1, 1/8, errorDampening);  // Bottom-left
        distribute(0, 1, 1/8, errorDampening);   // Bottom
        distribute(1, 1, 1/8, errorDampening);   // Bottom-right
        distribute(0, 2, 1/8, errorDampening);   // Bottom + 1
      }
    }
  }

  return result;
}

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
  constructor(homeAssistantUrl, token) {
    this.homeAssistantUrl = homeAssistantUrl;
    this.token = token;
    this.browser = undefined;
    this.page = undefined;
    this.busy = false;

    // The last path we requested a screenshot for
    // We store this instead of using page.url() because panels can redirect
    // users, ie / -> /lovelace/0.
    this.lastRequestedPath = undefined;
    this.lastRequestedLang = undefined;
    this.lastRequestedTheme = undefined;
    this.lastRequestedDarkMode = undefined;
  }

  async cleanup() {
    const { browser, page } = this;

    if (!this.browser && !this.page) {
      return;
    }

    this.page = undefined;
    this.browser = undefined;
    this.lastRequestedPath = undefined;
    this.lastRequestedLang = undefined;
    this.lastRequestedTheme = undefined;
    this.lastRequestedDarkMode = undefined;

    try {
      if (page) {
        await page.close();
      }
    } catch (err) {
      console.error("Error closing page during cleanup:", err);
    }

    try {
      if (browser) {
        await browser.close();
      }
    } catch (err) {
      console.error("Error closing browser during cleanup:", err);
    }

    console.log("Closed browser");
  }

  async getPage() {
    if (this.page) {
      return this.page;
    }

    console.log("Starting browser");
    // We don't catch these errors on purpose, as we're
    // not able to recover once the app fails to start.
    const browser = await puppeteer.launch({
      headless: "shell",
      executablePath: chromiumExecutable,
      args: puppeteerArgs,
    });
    const page = await browser.newPage();

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

    this.browser = browser;
    this.page = page;
    return this.page;
  }

  async navigatePage({
    pagePath,
    viewport,
    extraWait,
    zoom,
    lang,
    theme,
    dark,
  }) {
    let start = new Date();
    if (this.busy) {
      throw new Error("Browser is busy");
    }
    start = new Date();
    this.busy = true;
    const headerHeight = Math.round(HEADER_HEIGHT * zoom);

    try {
      const page = await this.getPage();

      // We add 56px to the height to account for the header
      // We'll cut that off from the screenshot
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
        const browserLocalStorage = {
          ...hassLocalStorageDefaults,
          hassTokens: JSON.stringify({
            access_token: this.token,
            token_type: "Bearer",
            expires_in: 1800,
            hassUrl,
            clientId,
            expires: 9999999999999,
            refresh_token: "",
          }),
        };
        const evaluateIdentifier = await page.evaluateOnNewDocument(
          (hassLocalStorage) => {
            for (const [key, value] of Object.entries(hassLocalStorage)) {
              localStorage.setItem(key, value);
            }
          },
          browserLocalStorage,
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

      // Update language
      // Should really be done via localStorage.selectedLanguage
      // but that doesn't seem to work
      if (lang !== this.lastRequestedLang) {
        await page.evaluate((newLang) => {
          document
            .querySelector("home-assistant")
            ._selectLanguage(newLang, false);
        }, lang || "en");
        this.lastRequestedLang = lang;
        defaultWait += 1000;
      }

      // Update theme and dark mode
      if (
        theme !== this.lastRequestedTheme ||
        dark !== this.lastRequestedDarkMode
      ) {
        await page.evaluate(
          ({ theme, dark }) => {
            document.querySelector("home-assistant").dispatchEvent(
              new CustomEvent("settheme", {
                detail: { theme, dark },
              }),
            );
          },
          { theme: theme || "", dark },
        );
        this.lastRequestedTheme = theme;
        this.lastRequestedDarkMode = dark;
        defaultWait += 500;
      }

      // wait for the work to be done.
      // Not sure yet how to decide that?
      if (extraWait === undefined) {
        extraWait = defaultWait;
      }
      if (extraWait) {
        await new Promise((resolve) => setTimeout(resolve, extraWait));
      }

      const end = Date.now();
      return { time: end - start };
    } finally {
      this.busy = false;
    }
  }

  async screenshotPage({ viewport, einkColors, colors, dithering, invert, zoom, format, rotate }) {
    let start = new Date();
    if (this.busy) {
      throw new Error("Browser is busy");
    }
    start = new Date();
    this.busy = true;
    const headerHeight = Math.round(HEADER_HEIGHT * zoom);

    try {
      const page = await this.getPage();

      // If eink processing is requested, we need PNG input for sharp.
      // Otherwise, use the requested format.
      const screenshotType = einkColors || format == "bmp" ? "png" : format;

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

      // Apply custom color dithering if colors parameter is provided
      if (colors && colors.length > 0) {
        // Convert to raw pixel data for custom dithering
        sharpInstance = sharpInstance.ensureAlpha().raw();
        const { data, info } = await sharpInstance.toBuffer({
          resolveWithObject: true,
        });

        // Apply dithering with the specified colors and algorithm
        const ditheredData = applyDithering(data, info.width, info.height, colors, info.channels, dithering);

        // Create new sharp instance from dithered data
        sharpInstance = sharp(ditheredData, {
          raw: {
            width: info.width,
            height: info.height,
            channels: info.channels,
          },
        });
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
        if (einkColors === 2) {
          sharpInstance = sharpInstance.toColourspace("b-w");
        }
        if (format == "bmp") {
          sharpInstance = sharpInstance.raw();

          const { data, info } = await sharpInstance.toBuffer({
            resolveWithObject: true,
          });
          let bitsPerPixel = 8;
          if (einkColors === 2) {
            bitsPerPixel = 1;
          } else if (einkColors === 4) {
            bitsPerPixel = 2;
          } else if (einkColors === 16) {
            bitsPerPixel = 4;
          }
          const bmpEncoder = new BMPEncoder(
            info.width,
            info.height,
            bitsPerPixel,
          );
          image = bmpEncoder.encode(data);
        } else if (format === "jpeg") {
          sharpInstance = sharpInstance.jpeg();
          image = await sharpInstance.toBuffer();
        } else if (format === "webp") {
          sharpInstance = sharpInstance.webp();
          image = await sharpInstance.toBuffer();
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
        const { data, info } = await sharpInstance.toBuffer({
          resolveWithObject: true,
        });
        const bmpEncoder = new BMPEncoder(info.width, info.height, 24);
        image = bmpEncoder.encode(data);
      } else {
        sharpInstance = sharpInstance.png();
        image = await sharpInstance.toBuffer();
      }

      const end = Date.now();
      return {
        image,
        time: end - start,
      };
    } catch (err) {
      // trigger a full page navigation on next request
      this.lastRequestedPath = undefined;
      throw err;
    } finally {
      this.busy = false;
    }
  }
}
