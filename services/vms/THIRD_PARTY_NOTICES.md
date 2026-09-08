# Third-party notices

## MediaMTX WebRTC reader

`app/static/vendor/mediamtx-reader.js` is a verbatim copy of
`internal/servers/webrtc/reader.js` from MediaMTX v1.20.1, with a provenance
header prepended. It is the browser-side WHEP client that MediaMTX itself
serves, so the VMS uses the officially supported integration rather than
inventing custom signalling.

- Project: MediaMTX — https://github.com/bluenviron/mediamtx
- Version: v1.20.1 (the version this stack pins in `docker-compose.yml`)
- License: MIT

```
MIT License

Copyright (c) 2019 Alessandro Ros

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## MediaMTX server

The `bluenviron/mediamtx:1.20.1` container image is used unmodified as the
VMS ingest/distribution server. Same project, same MIT license.
