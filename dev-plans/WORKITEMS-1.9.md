# Work Items — 1.9

Theme: **ESPHome dashboard parity.** Every feature the stock ESPHome UI has, this has too. Serial flashing, tail logs after OTA, adopt discovered devices. Also: remote compilation for cloud-based build workers.

(Minimal "new device" + "duplicate device" landed in 1.4.0 — see `WORKITEMS-1.4.md`. The full wizard with platform/board/WiFi selection remains here if we decide it adds value over the minimal stub + editor flow.)

## Web Serial Flashing

The right integration target is **[`esphome/esp-web-tools`](https://github.com/esphome/esp-web-tools)** (the same package `web.esphome.io` is built on), **not** `espressif/esptool-js` directly. `esp-web-tools` wraps `esptool-js: ^0.5.7` in a drop-in `<esp-web-install-button>` custom element that takes a `manifest` attribute pointing at a JSON manifest describing the firmware binaries and offsets. We get the Chrome Web Serial flashing + progress UI + error handling for free; our job is just the manifest endpoint.

**Prerequisite:** 1.4's firmware download work (CD section / 3.1a-c equivalents in 1.4) must land first — it's what produces the `.bin` files the manifest points at. Without it, there's nothing to flash.

- [ ] **3.2a.1 Firmware manifest endpoint** — `GET /ui/api/targets/{f}/manifest.json` returns an `esp-web-tools`-shaped manifest: `{name, version, home_assistant_domain, new_install_prompt_erase, builds: [{chipFamily, parts: [{path, offset}]}]}`. `path` points at the existing firmware download endpoint from 1.4. Chip family is read from the target's YAML (`esphome.platform` + board → chip family mapping). Document the manifest format version we target in a module-level constant.
- [ ] **3.2a.2 `<esp-web-install-button>` integration** — install `esp-web-tools` as a frontend dep. Drop the custom element into a new "Install via USB" modal (or the existing device row hamburger menu → "Flash via USB"). Wire its `manifest` attribute to the manifest endpoint from 3.2a.1. Handle the `state-changed` events it emits to surface progress in our own toast/log UI instead of its default rendering. Check bundle size impact — `esptool-js` + deps aren't tiny.
- [ ] **3.2a.3 Chrome/Edge detection + graceful fallback** — Web Serial is Chromium-only. If `navigator.serial` is undefined, disable the button and show a tooltip explaining why (matches the **Disable, don't fail** design judgment rule). Playwright e2e test asserts the button is disabled on Firefox/WebKit.
- [ ] **3.2a.4 E2E mocked test** — at minimum verify the button appears on device rows, opens the modal, and the manifest endpoint returns the correct shape. Actual flashing can't be e2e-tested without a real device — document this as a manual verification step in the release checklist.
- [ ] **3.2b Server serial flashing** — list ports on HA host, esptool.py flash endpoint. Alternative to Web Serial for the HA host itself (where Chrome isn't an option). Keep this separate from 3.2a — different code path, different security model.

## Web Serial Logs

- [ ] **4.1d Web Serial logs** — browser-side USB serial log viewer (Web Serial API)

## Live Log Tail After Update

- [ ] **4.5 Auto-connect device logs after OTA** — when viewing a job's log modal, automatically connect to the device's native API log stream after OTA completes, like `esphome run` does (compile → upload → tail logs)

## Thread / IPv6 Support

- [ ] **4.6 Thread device IP display** (GitHub #17) — Thread devices use IPv6 and don't show an IP address in the dashboard. Display IPv6 addresses and add a wifi/thread indicator to the device row.

## Device Adoption

- [ ] **2.4 Device adoption/import** — discover unconfigured devices, adopt with project URL

## Remote Compilation

Allow compiling on VPS servers not on the local network. Builds on 1.4's firmware download infrastructure — the remote worker compiles and sends firmware back to the server, then a local agent (the HA add-on itself or a local worker) handles OTA separately.

- [ ] **RC.1 Compile-only worker mode** — worker compiles and POSTs firmware binary to server instead of OTA-flashing; reuses 1.4's firmware storage
- [ ] **RC.2 Server-side OTA** — server runs `esphome upload` or OTA protocol against local devices using stored firmware
- [ ] **RC.3 Two-phase job lifecycle** — new job states for compile-complete-awaiting-OTA; UI shows firmware ready + OTA trigger button
- [ ] **RC.4 GitHub Actions integration** — optional: trigger builds via GitHub Actions workflow

## Open Bugs & Tweaks
