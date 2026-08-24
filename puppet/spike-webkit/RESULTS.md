# Spike: Playwright WebKit vs Chromium headless shell

Question: would WebKit lower the memory footprint of the puppet add-on
compared to the current Puppeteer + Chromium headless shell setup?

**Answer: no. WebKit used ~1.9x more memory and was ~5x slower to
navigate. Keep Chromium.**

## Results

Rendering the Home Assistant demo dashboard (demo.home-assistant.io) at
1000x1000, 3 runs per engine. Memory is PSS summed over the browser's
whole process tree, read from `/proc/<pid>/smaps_rollup`.

| Metric                    | Chromium headless shell | Playwright WebKit |
| ------------------------- | ----------------------- | ----------------- |
| PSS idle, page open       | 251-252 MB              | 473-474 MB        |
| PSS right after screenshot| 250-252 MB              | 482-484 MB        |
| Navigation (cached)       | 159-174 ms              | 752-839 ms        |
| Screenshot                | 81-88 ms                | 241-316 ms        |

Per-process breakdown (idle):

- Chromium: renderer 126 MB, browser 51 MB, GPU (swiftshader) 48 MB,
  network service 25 MB
- WebKit: WPEWebProcess 347 MB, MiniBrowser (UI process) 77 MB,
  WPENetworkProcess 51 MB

Rendering fidelity was fine in both engines: the dashboard screenshots
are visually identical (`out/chromium-shell.png`, `out/webkit.png`).

## Method

- Playwright 1.62.1. Chromium 141 headless shell launched with the same
  flags the add-on passes to Puppeteer (`screenshot.js`), including
  `--enable-low-end-device-mode`. WebKit launched with Playwright
  defaults (WPE-based build 2336).
- `mirror.js` records every network response for the demo dashboard into
  `mirror.json` (117 responses, ~7 MB). `measure.js` replays them via
  Playwright request interception, so both engines receive identical
  bytes and network never touches the numbers. One extra JS chunk had to
  be added by hand because Chromium requests a chunk WebKit does not.
- Environment: x86_64 container, 16 GB RAM, no GPU. Not a HA Green
  (aarch64, 4 GB), but the comparison is relative and the gap is large.

## Reproduce

```bash
npm install
node mirror.js       # record demo.home-assistant.io into mirror.json
node measure.js chromium-shell https://demo.home-assistant.io/
node measure.js webkit https://demo.home-assistant.io/
```

## Caveats

- "WebKit" here is Playwright's WPE-based Linux build. A hand-tuned WPE
  WebKit embedder could behave differently, but this is the build the
  add-on would actually ship with the Playwright route.
- The demo dashboard mocks its backend in JS, so it is somewhat heavier
  than a typical dashboard. Both engines rendered the same content.
- Numbers are steady-state with the page held open (the add-on's
  `keep_browser_open` scenario). Cold-launch overhead was small for both
  engines here because all assets came from the local replay.

## Conclusion

The "WebKit is lighter" reputation comes from sharing an already-running
system webview on desktop/mobile. As a standalone headless engine it is
heavier than Chromium's headless shell, which drops the GPU/audio/UI
machinery and, with `--enable-low-end-device-mode`, is tuned for exactly
the add-on's use case. There is no memory win available by switching the
puppet add-on to WebKit.
