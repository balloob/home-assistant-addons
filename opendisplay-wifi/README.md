# OpenDisplay Wi-Fi

> **Experimental** - This add-on is a work in progress.

[![Open your Home Assistant instance and show the dashboard of an add-on.](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=0f1cc410_opendisplay-wifi&repository_url=https%3A%2F%2Fgithub.com%2Fballoob%2Fhome-assistant-addons)

Run an [OpenDisplay](https://opendisplay.org) Wi-Fi server as a Home Assistant add-on. E-paper displays on your network will automatically discover the server via mDNS and connect to receive images.

This add-on uses a source install of the [`wifi-server` branch of py-opendisplay](https://github.com/balloob/py-opendisplay/tree/wifi-server).

## Features

- Runs an OpenDisplay Wi-Fi protocol server on port 2446
- Web UI accessible via Home Assistant Ingress
- View connected screens with their dimensions and color support
- Assign images to screens:
  - **Upload a local image** - converted and sent to the display
  - **Provide a URL** - the server fetches it periodically at a configurable interval, updating the display when the image changes

## Installation

Add this repository to your Home Assistant add-on store:

```
https://github.com/balloob/home-assistant-addons
```

Then install the **OpenDisplay Wi-Fi** add-on.

## Usage

1. Start the add-on
2. Open the Web UI from the add-on page (via Ingress)
3. Power on your OpenDisplay e-paper screens - they will appear in the UI once connected
4. Upload an image or provide a URL and assign it to a screen

## Local development

You can also run the server outside Home Assistant:

```bash
cd opendisplay-wifi
mkdir -p dev-data
uv sync
uv run python server.py
```

When run locally, the server stores its data and config in `./dev-data`:

- `dev-data/assignments.json`
- `dev-data/albums.json`
- `dev-data/uploads/`
- `dev-data/thumbnails/`
- optional `dev-data/options-dev.json`

Inside the Home Assistant add-on, it continues using `/data` instead.

## Links

- [OpenDisplay](https://opendisplay.org)
- [py-opendisplay wifi-server branch](https://github.com/balloob/py-opendisplay/tree/wifi-server)
