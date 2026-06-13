# YAIL Wire Protocol

This documents the protocol spoken between the YAIL client (cc65/Atari,
via FujiNet `N:TCP://host:5556/`) and this server, as implemented by the
pre-2.0 server and preserved byte-for-byte by 2.0. Versioning quirks are
historical; do not "fix" them without coordinating a client release.

## Transport

Plain TCP, default port 5556. The client sends whitespace-separated text
commands; the server replies with binary YAI packets (images, errors) or,
for a few configuration commands, plain text lines.

Multiple commands may arrive in one TCP segment (e.g. `gfx 2 files`); the
server consumes them left to right.

Anything that looks like an HTTP request (`GET`, `POST`, `PUT`, `DELETE`,
`HEAD`) receives a fixed `HTTP/1.1 403 Forbidden` response with the body
`Not Allowed`, and the connection is closed.

## Client commands

| Command | Effect |
| --- | --- |
| `gfx <mode>` | Select graphics mode for subsequent images (see mode values). |
| `search "<terms>"` | Image search (DDGS); a random result is converted and streamed. |
| `gen "<prompt>"` | Generate an image with the server's configured model and stream it. |
| `gen <model> "<prompt>"` | Legacy form: generate with the named model (retired model names are served with the current default). |
| `gen-gemini "<prompt>"` | Generate with the default Gemini image model. |
| `showurl <url>` | Fetch a specific image URL and stream it. |
| `files` | Stream a random image from the server's `--paths` collection. |
| `video` | Stream one webcam frame (camera optional on the server). |
| `next` | Repeat the last mode: another search result / regeneration / file / frame. |
| `openai-config [param] [value]` | Get or set generation config (`model`, `size`, `quality`, `style`, `system_prompt`). Replies in text. |
| `quit` | End the session. |

Prompts/terms may be wrapped in double quotes; the quotes are not part of
the value. Unrecognized commands receive the text reply `OK: ACK!\r\n`.

## Graphics mode values

Sent by the client in `gfx <mode>` (decimal) and echoed in packet headers:

| Value | Mode | Native format |
| --- | --- | --- |
| 2 | Graphics 8 | ANTIC mode F: 320×220 here (custom display list), 1-bit pixels, 40 bytes/line, dithered |
| 4 | Graphics 9 | GTIA 16-luminance mode (PRIOR[7:6]=%01): 80×220, two 4-bit pixels per byte, 40 bytes/line |
| 6 | Graphics 15 | ANTIC mode E: 160×220, four 2-bit pixels per byte, 40 bytes/line; 4-level gray ramp via COLBK/PF0/PF1/PF2 |
| 8 | Graphics 11 | GTIA 16-hue mode (PRIOR[7:6]=%11): 80×220, two 4-bit hue indices per byte, 40 bytes/line; luminance from COLBK, pixel value 0 always black |
| 10 | Graphics 10 | GTIA 9-color mode (PRIOR[7:6]=%10): 80×220, two 4-bit pixels per byte; adaptive per-image palette sent in a v1.4 palette block (see below) |
| 16 | VBXE | 320×240, 8-bit palette indices, 256-color RGB palette |

Anything that is not 2, 4, 6, 8, or 10 is treated as VBXE by the server.

> Historical note: the legacy server defined `GRAPHICS_11 = 8` but never
> implemented it; the legacy client sent its internal defines raw
> (`GRAPHICS_10 = 0x08`, `GRAPHICS_11 = 0x10`), so Graphics 10/11
> selections landed in the server's VBXE path and broke. Since v2.x the
> client maps internal modes to wire values explicitly
> (`graphics_mode_to_wire`), and wire value 8 carries Graphics 11 data.
> A legacy client that picks Graphics 10 sends `gfx 8` and now receives
> a well-formed 8807-byte v1.1 packet (hue data shown through the wrong
> GTIA mode) instead of a VBXE packet it cannot parse.

Source: Graphics 8/9 mode facts verified against the Altirra Hardware
Reference Manual (GTIA mode 9, PRIOR[7:6]=%01, p.154) and the Atari
Assembly Language Programmer's Guide via the a8 MCP index.

## YAI image packet, version 1.1 (Graphics 8/9/11/15)

Total 8807 bytes:

| Offset | Size | Value |
| --- | --- | --- |
| 0 | 3 | Version `01 01 00` |
| 3 | 1 | Graphics mode (2, 4, 6, or 8) |
| 4 | 1 | Block token `0x03` |
| 5 | 2 | Payload size, little-endian u16 (`0x2260` = 8800) |
| 7 | 8800 | Framebuffer: 220 lines × 40 bytes |

The client reads the 4-byte header, then 3 more bytes (token + size), then
the framebuffer in chunks of 4080/4080/640 bytes to skip 4KB boundaries.

## YAI image packet, version 1.4 (VBXE)

| Offset | Size | Value |
| --- | --- | --- |
| 0 | 3 | Version `01 04 00` |
| 3 | 1 | Graphics mode (16) |
| 4 | 1 | Number of memory blocks |
| — | — | Per block: 1-byte type, little-endian u32 size, payload |

Block types: `0x04` DL, `0x05` XDL, `0x06` palette, `0x07` image data,
`0xFF` error. The standard VBXE image is two blocks: a 768-byte palette
(256 × RGB) then 76800 bytes of pixel indices. Palette entry 0 is forced
to black; pixel values are shifted up by one accordingly.

## YAI image packet, Graphics 10 (v1.4 framing)

Graphics 10 needs a per-image palette, so it reuses the v1.4 two-block
layout with `gfx_mode = 10`:

| Block | Size | Contents |
| --- | --- | --- |
| `0x06` palette | 9 | Atari color bytes (hue<<4 \| lum) for the GTIA mode 10 sources in register order P0–P3, PF0–PF3, BAK |
| `0x07` image | 8800 | Framebuffer, 220 lines × 40 bytes, two 4-bit pixels per byte |

Pixel values 0–3 select P0–P3, 4–7 select PF0–PF3, 8 selects BAK
(GTIA decodes %10xx as background). The palette is sorted darkest
first because GTIA renders the screen border in P0's color. Total
packet: 8824 bytes.

## Error packet

A version-1.4 packet with one `0xFF` block whose payload is the UTF-8
error message:

```
01 04 00 <gfx_mode> 01 FF <u32 length> <message bytes>
```

## Text responses (quirk)

Configuration commands (`openai-config`) and the unknown-command ACK reply
with plain text `OK: <message>\r\n` rather than a binary packet. This is
legacy behavior the client tolerates only outside of image streaming.
