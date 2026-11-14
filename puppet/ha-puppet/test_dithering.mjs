import assert from "node:assert/strict";
import test from "node:test";
import sharp from "sharp";
import { Browser } from "./screenshot.js";
import devices from "./devices.json" with { type: "json" };

const device = devices.devices["7.3in-spectra-e6"];
const colors = device.colors.split(",").map((color) => `#${color}`);
const paletteColors = device.palette_colors.split(",").map((color) => `#${color}`);

test("Sierra dithering with a narrow matching palette preserves white backgrounds", async () => {
  const width = 64;
  const height = 64;
  const data = Buffer.alloc(width * height * 3, 255);
  for (let y = 24; y < 40; y++) {
    for (let x = 24; x < 40; x++) {
      data.fill(0, (y * width + x) * 3, (y * width + x) * 3 + 3);
    }
  }
  const source = await sharp(data, { raw: { width, height, channels: 3 } }).png().toBuffer();
  const browser = new Browser();
  browser.page = { screenshot: async () => source };
  const { image } = await browser.screenshotPage({
    viewport: { width, height }, zoom: 1, format: "png",
    colors: ["#000000", "#FFFFFF"], paletteColors: ["#EFEFEF", "#FFFFFF"],
    dithering: "sierra", invert: true,
  });
  const pixels = await sharp(image).removeAlpha().raw().toBuffer();
  for (let offset = 0; offset < pixels.length; offset++) {
    assert.equal(pixels[offset], 255 - data[offset], `channel ${offset}`);
  }
});

for (const dithering of ["none", "floyd-steinberg", "atkinson", "jarvis-judice-ninke", "stucki", "burkes", "sierra", "sierra-lite"]) {
  test(`${dithering} preserves exact ${dithering === "none" ? "device" : "output"} palette matches`, async () => {
    const browser = new Browser();
    const matchingColors = dithering === "none" ? paletteColors : colors;
    for (let index = 0; index < colors.length; index++) {
      const source = await sharp({
        create: { width: 16, height: 16, channels: 3, background: matchingColors[index] },
      }).png().toBuffer();
      browser.page = { screenshot: async () => source };
      const { image } = await browser.screenshotPage({
        viewport: { width: 16, height: 16 }, zoom: 1, format: "png",
        colors, paletteColors: matchingColors, dithering,
      });
      const pixels = await sharp(image).removeAlpha().raw().toBuffer();
      const expected = Buffer.from(colors[index].slice(1), "hex");
      for (let offset = 0; offset < pixels.length; offset += 3) {
        assert.deepEqual(pixels.subarray(offset, offset + 3), expected);
      }
    }
  });
}
