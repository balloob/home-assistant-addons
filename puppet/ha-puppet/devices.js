import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// Cache for device configurations
let devicesConfig = null;

/**
 * Load device configurations from devices.json
 * @returns {Promise<Object>} The devices configuration
 */
export async function loadDevicesConfig() {
  if (devicesConfig) {
    return devicesConfig;
  }

  try {
    const devicesPath = join(__dirname, "devices.json");
    const devicesData = await readFile(devicesPath, "utf-8");
    devicesConfig = JSON.parse(devicesData);
    return devicesConfig;
  } catch (err) {
    console.error("Error loading devices config:", err);
    devicesConfig = { devices: {}, aliases: {} };
    return devicesConfig;
  }
}

/**
 * Resolve device name (handle aliases)
 * @param {string} deviceName - The device name or alias
 * @param {Object} config - The devices configuration
 * @returns {string} The resolved device name
 */
export function resolveDeviceName(deviceName, config) {
  if (config.aliases && config.aliases[deviceName]) {
    return config.aliases[deviceName];
  }
  return deviceName;
}

/**
 * Get device configuration
 * @param {string} deviceName - The device name or alias
 * @param {Object} config - The devices configuration
 * @returns {Object|null} The device configuration or null if not found
 */
export function getDeviceConfig(deviceName, config) {
  const resolvedName = resolveDeviceName(deviceName, config);
  return config.devices[resolvedName] || null;
}
