# Rel Zone (Python)

    pip install -r requirements.txt
    python app.py            # then open http://localhost:5000

Put your Groq key in `.env` as `GROQ_API_KEY=...` (server-side only, never sent to the browser).

## How the web view works
* **Full proxy** (Scramjet + a built-in Wisp server at `/wisp/`): JavaScript, cookies, WebSockets and
  games work. Needs a *secure context*: open the site at `http://localhost:5000` / `http://127.0.0.1:5000`, or over **https**.
* **Basic proxy** (fallback): used automatically on plain `http://` at a non-localhost address (for example
  `http://192.168.x.x:5000`), because browsers refuse service workers there. JavaScript and cookies are OFF in this
  mode, so games won't run. The status line above the web view tells you which mode you're in.

## Global chat
The **chat** tab is one shared room for everyone connected to *your* server, live over a WebSocket (`/ws/chat`).
* It shows the last 100 messages, who's online, and an unread dot on the nav when someone talks while you're elsewhere.
* Messages are plain text (never HTML), max 300 characters, 5 per 10 seconds, no repeats, 6 connections per address.
* Each name gets a short `#tag` derived from the sender's address, so nobody can pretend to be someone else.
* History is saved to `chat_history.json` (nicknames and messages only) so a restart doesn't wipe the room.
* **"Global" means everyone who can reach your server.** On your own computer at `localhost` that's only you. For friends
  to join you have to host it somewhere they can reach. There are no moderator tools yet (mute/ban/delete).

## Calls
The **calls** tab makes voice and video calls between people on your server (WebRTC: audio and video go directly
between browsers, your server only passes the setup messages).
* **Rooms:** type a room name (default `lobby`) and join. Up to 8 people per room; everyone gets a tile, a mute button,
  a camera button and a highlight when they're talking.
* **Ring someone:** switch on **Available for calls** (OFF by default, so nobody can see or ring you until you choose to).
  People who are available show up in the list with a call button. The other person gets a ringing popup on any page
  and can accept or decline; you get a private room that nobody else can join. Unanswered calls stop after 30 seconds.
* **Needs** a microphone and the site opened at `http://localhost:5000` or over https (same rule as the full proxy).
* Browsers find each other with free public STUN servers. Some networks (many school and mobile ones) block direct
  connections and need a **TURN** server too. Set it with `ICE_SERVERS`, a JSON list, for example:
  `ICE_SERVERS='[{"urls":"turn:your.server:3478","username":"u","credential":"p"}]' python app.py`
  (`ICE_SERVERS='[]'` means LAN only.)
* Every extra person adds an upload stream for everyone (full mesh), which is why rooms stop at 8.
* Known quirk: if someone's camera is already on when another person joins, their picture occasionally freezes for the
  other person. The page watches for this: if no video frames are being sent a few seconds after someone connects, it first
  re-attaches the camera, then, if still frozen, gets a fresh camera stream and swaps it in (you may see the camera light
  blink once). Turning the camera off and on by hand always works too.

## Virtual machine
The **vm** tab runs an old PC inside your browser with the open-source v86 emulator. It runs on the visitor's own computer
(nothing runs on your server), only uses CPU while the tab is on screen, and pauses when you leave it.
* **Rel Zone does not include Windows or any other operating system.** Bring a disk image you have the right to use.
* **It starts by itself.** Drop a disk image in `vm_images` named `windows95.img` or `windows98.img` (or any name containing win95 / win98;
  or just one image of any name) and the first time you open the **vm** tab it picks the right preset and boots it, no clicking.
  It remembers your last machine. Untick "Start automatically" to turn this off.
  Presets: Windows 95 = 128 MB RAM + 8 MB video, Windows 98 = 256 MB + 16 MB, both boot from the hard disk.
  Anything in `vm_images` shows up in the tab: `.iso` = CD-ROM, files up to 2.88 MB = floppy, everything else = hard disk.
* **Disks are checked before starting.** The tab reads the first bytes of each image and tells you what it is. It refuses to start a hard disk
  that can never boot, and says why: an installer ISO (use the CD-ROM slot), a qcow2 / vmdk / vhd / vdi file (convert it), a zip / 7z / rar
  (unpack it), or an empty disk. If the BIOS still ends on "No bootable device", the page explains it in plain words.
* Or skip the folder: **browse** picks a file from your own computer (it is read in your browser and never uploaded).
* Disk images must be **raw** (convert qcow2/vhd/vmdk with `qemu-img convert -O raw in.qcow2 out.img`).
  To install from a CD, choose a "new empty disk", the CD image, and a boot floppy, and set "boot from" to floppy or CD first.
* Old systems start slowly in a browser (Windows 95 can take a few minutes). **save state** downloads the whole running machine
  (memory and, for disks loaded fully into the browser, your changes); **load state** brings it back instantly.
  Disks over 300 MB load on demand and a saved state does not include changes made to them.
* Click the screen to give the VM your keyboard and mouse (Esc frees the mouse, clicking outside gives the keyboard back).
  The fog animation stops while a VM is on screen so the emulator gets the CPU.
* No internet or sound is set up inside the VM.

## Files
* `app.py` Flask site, AI chat (`/api/chat`), AI check (`/api/health`), basic proxy (`/proxy`), runs under uvicorn
* `chat.py` the global chat room
* `calls.py` call signalling (rooms, ringing, relaying WebRTC setup messages)
* `vm_images/` your disk images for the VM tab (the tab lists whatever is in here)
* `vendor/v86/` the PC emulator (BSD-2) and its BIOS files (LGPL)
* `wisp.py` the Wisp server (TCP over WebSocket). Blocks private/internal addresses and mail ports
* `static/` the site (`index.html`) and the proxy service worker (`sw.js`)
* `vendor/` Scramjet, bare-mux and the libcurl transport (see their LICENSE files; they are AGPL-3.0 licensed,
  so if you host this publicly for other people you must offer them the source). The libcurl transport has one small
  patch (waits for its WebAssembly before the first request).

## Options (environment variables)
`HOST` (use `0.0.0.0` for other devices), `PORT`, `GROQ_MODEL`, `LOG_LEVEL=info`, `WISP_DEBUG=1`,
`ISOLATE=1` (cross-origin isolation; off by default because it can blank proxied pages in some browsers),
`CHAT_FILE` (where chat history is saved), `VM_DIR` (folder for VM disk images), `ICE_SERVERS` (STUN/TURN for calls), `RING_SECONDS`, `TRUST_PROXY=1` (use when hosting behind Cloudflare/nginx/a tunnel, so each
visitor's real address is used for rate limits and chat tags instead of the proxy's).

## Safety
The proxy fetches pages for anyone who can reach the server. Keep it on your own machine, or put a login in front of it
before exposing it to the internet. Some sites (those with bot checks, logins or heavy ads) may still not work.
