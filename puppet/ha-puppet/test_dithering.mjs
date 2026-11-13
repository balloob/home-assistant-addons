import assert from "node:assert/strict";
import test from "node:test";
import sharp from "sharp";
import { Browser } from "./screenshot.js";
import devices from "./devices.json" with { type: "json" };

const device = devices.devices["7.3in-spectra-e6"];
const colors = device.colors.split(",").map((color) => `#${color}`);
const paletteColors = device.palette_colors.split(",").map((color) => `#${color}`);

for (const dithering of ["none", "floyd-steinberg", "atkinson", "jarvis-judice-ninke", "stucki", "burkes", "sierra", "sierra-lite"]) {
  test(`${dithering} preserves exact device palette matches`, async () => {
    const browser = new Browser();
    for (let index = 0; index < colors.length; index++) {
      const source = await sharp({
        create: { width: 16, height: 16, channels: 3, background: paletteColors[index] },
      }).png().toBuffer();
      browser.page = { screenshot: async () => source };
      const { image } = await browser.screenshotPage({
        viewport: { width: 16, height: 16 }, zoom: 1, format: "png",
        colors, paletteColors, dithering,
      });
      const pixels = await sharp(image).removeAlpha().raw().toBuffer();
      const expected = Buffer.from(colors[index].slice(1), "hex");
      for (let offset = 0; offset < pixels.length; offset += 3) {
        assert.deepEqual(pixels.subarray(offset, offset + 3), expected);
      }
    }
  });
}
