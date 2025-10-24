import { createCanvas, loadImage } from "canvas";
import {
  ditherImage,
  getDefaultPalettes,
  getDeviceColors,
  replaceColors,
} from "epdoptimize";

/**
 * Apply dithering to an image buffer using epdoptimize
 * @param {Buffer} imageBuffer - Input image buffer (PNG format)
 * @param {Object} options - Dithering options
 * @param {number} options.colors - Number of colors (2, 4, 7, etc.)
 * @param {string} options.algorithm - Dithering algorithm (default: 'floydSteinberg')
 * @param {string} options.displayType - Display type ('spectra6' or 'acep' or null for generic)
 * @param {boolean} options.serpentine - Use serpentine scanning (default: false)
 * @returns {Promise<Buffer>} - Dithered image as PNG buffer
 */
export async function applyDithering(imageBuffer, options = {}) {
  const {
    colors = 2,
    algorithm = "floydSteinberg",
    displayType = null,
    serpentine = false,
  } = options;

  // Load the image into a canvas
  const image = await loadImage(imageBuffer);
  const inputCanvas = createCanvas(image.width, image.height);
  const inputCtx = inputCanvas.getContext("2d");
  inputCtx.drawImage(image, 0, 0);

  // Create output canvases
  const ditheredCanvas = createCanvas(image.width, image.height);
  const finalCanvas = createCanvas(image.width, image.height);

  // Get palette based on display type or create a grayscale palette
  let palette;
  let deviceColors = null;

  if (displayType === "spectra6" || displayType === "acep") {
    // Use device-specific palette
    palette = getDefaultPalettes(displayType);
    deviceColors = getDeviceColors(displayType);
  } else {
    // Create a generic grayscale palette based on number of colors
    palette = createGrayscalePalette(colors);
  }

  // Configure dithering options
  const ditheringOptions = {
    ditheringType: "errorDiffusion",
    errorDiffusionMatrix: algorithm,
    serpentine: serpentine,
    palette: palette,
  };

  // Apply dithering
  ditherImage(inputCanvas, ditheredCanvas, ditheringOptions);

  // If using device-specific colors, replace with actual device colors
  if (deviceColors) {
    replaceColors(ditheredCanvas, finalCanvas, {
      originalColors: palette,
      replaceColors: deviceColors,
    });
  } else {
    // Use the dithered canvas as-is
    const finalCtx = finalCanvas.getContext("2d");
    finalCtx.drawImage(ditheredCanvas, 0, 0);
  }

  // Convert canvas to buffer
  return finalCanvas.toBuffer("image/png");
}

/**
 * Create a grayscale palette with specified number of colors
 * @param {number} colors - Number of colors
 * @returns {string[]} - Array of hex color strings
 */
function createGrayscalePalette(colors) {
  const palette = [];
  for (let i = 0; i < colors; i++) {
    const value = Math.round((i / (colors - 1)) * 255);
    const hex = value.toString(16).padStart(2, "0");
    palette.push(`#${hex}${hex}${hex}`);
  }
  return palette;
}

/**
 * Get available dithering algorithms
 * @returns {string[]} - List of algorithm names
 */
export function getAvailableAlgorithms() {
  return [
    "floydSteinberg",
    "falseFloydSteinberg",
    "jarvis",
    "stucki",
    "burkes",
    "sierra3",
    "sierra2",
    "sierra2-4a",
  ];
}

/**
 * Get available display types
 * @returns {string[]} - List of display types
 */
export function getAvailableDisplayTypes() {
  return ["spectra6", "acep"];
}
