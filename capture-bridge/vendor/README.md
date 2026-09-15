# vendor/

Two runtime binaries belong in this folder (both gitignored — not committed):

- `node.exe` — Node.js 20 runtime. Get it from https://nodejs.org (or point
  `start.bat` / your own script at any Node 20+ install already on your PATH
  instead of a local copy here).
- `ffmpeg.exe` — used for the `/video.mjpeg` route. Get it from
  https://ffmpeg.org/download.html.

The original copies were pulled from the iDronam install
(`resources/app.asar.unpacked/node_modules/{node,ffmpeg-static}`), since both
are open-source binaries iDronam itself bundles rather than anything
proprietary to it.
