# Screenshot Home Assistant using Puppeteer

Experiment to easily create screenshots of your dashboards using Puppeteer. Allowing you to put them on e-ink screens or any other screen that can display images.

[![Open your Home Assistant instance and show the dashboard of an add-on.](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=0f1cc410_puppet&repository_url=https%3A%2F%2Fgithub.com%2Fballoob%2Fhome-assistant-addons)

You will need to create a long lived access token and add it as an add-on option.

_This is a prototype, there is NO security. Anyone can access the server and make screenshots of any Home Assistant page._

[![ESPHome device showing a screenshot of a Home Assistant dashboard](https://raw.githubusercontent.com/balloob/home-assistant-addons/main/puppet/example/screenshot.jpg)](./example/)

## Usage

Starting the add-on will launch a new server on port 10000. Any path you request will return a screenshot of that page. You will need to specify the viewport size you want.

For example, to get a 1000px x 1000px screenshot of your default dashboard, fetch:

```
http://homeassistant.local:10000/lovelace/0?viewport=1000x1000
```

To reduce the color palette for e-ink displays, you can add the `eink` parameter. The value represents the number of colors (including black) to use. For example, for a 2-color e-ink display:

```
http://homeassistant.local:10000/lovelace/0?viewport=1000x1000&eink=2
```

If you are using `eink=2`, you can also invert the colors by adding the `invert` parameter:

```
http://homeassistant.local:10000/lovelace/0?viewport=1000x1000&eink=2&invert
```

By default, on a cold start the server will wait for 2.5 extra seconds after the loading is considered done, to give things that are not tracked by loading spinners to load (ie icons, pictures). When the browser is active, it waits 750ms. You can control this wait time by adding a `wait` query parameter. For example, to wait 10 seconds:

```
http://homeassistant.local:10000/lovelace/0?viewport=1000x1000&wait=10000
```

You can control the zoom level of the page using the `zoom` query parameter. The default zoom level is 1. For example, to zoom in 1.3x:

```
http://homeassistant.local:10000/lovelace/0?viewport=1000x1000&zoom=1.3
```

By default, the output format is PNG. You can request a BMP image by adding the `format=bmp` query parameter:

```
http://homeassistant.local:10000/lovelace/0?viewport=1000x1000&format=bmp
```

By default, the output format is PNG. You can request a JPEG or WebP image by adding the `format=jpeg` or `format=webp` query parameter:

```
http://homeassistant.local:10000/lovelace/0?viewport=1000x1000&format=jpeg
```

```
http://homeassistant.local:10000/lovelace/0?viewport=1000x1000&format=webp
```

**Note:** If the `eink` parameter is specified, the output format will always be PNG, regardless of the `format` parameter.

You can rotate the screenshot by adding the `rotate` query parameter. Valid values are 90, 180, and 270.

```
http://homeassistant.local:10000/lovelace/0?viewport=1000x1000&rotate=90
```

## Speed (or lack thereof)

This add-on is slow. On a Home Assistant Green, on cold-start, it takes ~10s. The browser is kept alive for up to 30 seconds.

If the same page is requested, a screenshot is returned as fast as possible (0.6s on HA Green). If a different page is requested, it takes ~1.5s because it needs to navigate.
