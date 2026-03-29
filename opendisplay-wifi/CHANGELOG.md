# Changelog

## 0.1.8

- Add image preprocessing timing logs for load, fit, dither, and cache generation
- Queue screen image preprocessing in the background so clients do not block on cold conversions
- Process cache work serially and prewarm only the current album image on startup

## 0.1.5

- Rework the image management UI around a unified image library
- Support adding URL-backed images with generated thumbnails and gallery actions
- Allow albums to pick from existing images and add new uploads or URLs inline

## 0.1.0

- Initial release
- OpenDisplay Wi-Fi server for e-paper displays
- Web UI via Ingress for managing screens and images
- Support for image URLs with configurable update intervals
- Support for local image uploads
