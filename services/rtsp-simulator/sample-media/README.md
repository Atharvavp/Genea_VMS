# Mounted source videos

Anything you drop in this directory is mounted **read-only** into the simulator
container at `/data/local-sources`, and can be selected as a camera source
without uploading it through the browser ("Use a mounted file" in the Add Camera
dialog, or `{"source": {"kind": "local", "local_path": "sample-320x240.mp4"}}`
in the API).

Only paths *inside* this directory are accepted. Absolute host paths and
`../` traversal are rejected, and the simulator never deletes files here -
deleting a camera only removes files it uploaded itself.

Supported extensions: `.mp4` `.mkv` `.mov` `.avi` `.m4v` `.webm`

`sample-320x240.mp4` is a 10-second FFmpeg test pattern, included so the
simulator can be tried out immediately. Generate your own with:

```bash
ffmpeg -f lavfi -i testsrc=size=640x480:rate=25:duration=10 \
       -c:v libx264 -pix_fmt yuv420p sample-media/my-clip.mp4
```
