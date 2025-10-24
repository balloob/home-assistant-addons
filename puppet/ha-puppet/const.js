import { readFileSync, existsSync } from "fs";

// load first file that exists
const optionsFile = ["./options-dev.json", "/data/options.json"].find(
  existsSync,
);
if (!optionsFile) {
  console.error(
    "No options file found. Please copy options-dev.json.sample to options-dev.json",
  );
  process.exit(1);
}
// If `homeassistant_api: true` is removed from config.yaml then this detection will stop working
export const isAddOn = process.env.SUPERVISOR_TOKEN !== undefined;
const options = JSON.parse(readFileSync(optionsFile));

export const hassUrl = isAddOn
  ? (options.home_assistant_url || "http://homeassistant:8123")
  : (options.home_assistant_url || "http://localhost:8123");
export const hassToken = options.access_token;
export const debug = false;

export const chromiumExecutable = isAddOn ? "/usr/bin/chromium" : (options.chromium_executable || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome");

export const keepBrowserOpen = options.keep_browser_open || false;

if (!hassToken) {
  console.error("No access token found. Please configure the access token");
  process.exit(1);
}
