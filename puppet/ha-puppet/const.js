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
export const isAddOn = optionsFile === "/data/options.json";
const options = JSON.parse(readFileSync(optionsFile));

export const hassUrl = isAddOn
  ? "http://homeassistant:8123"
  : options.home_assistant_url;
export const hassToken = options.access_token;
export const debug = false;

export const getChromePath = (is_addon) => {
  if (is_addon) {
    return "/usr/bin/chromium";
  } else if (process.platform === "darwin") {
    return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  } else {
    return "/usr/bin/google-chrome";
  }
};

if (!hassToken) {
  console.error("No access token found. Please configure the access token");
  process.exit(1);
}
