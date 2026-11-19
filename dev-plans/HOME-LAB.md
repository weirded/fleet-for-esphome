# Home Lab

The physical infrastructure used to integration-test Fleet for ESPHome. This is a developer-only reference — not surfaced in user docs.

## Network

- **CIDR:** `192.168.224.0/22` — one flat subnet.
- The development laptop, every ESPHome device under test, and every host listed below all live on it.
- ESPHome devices are discovered via mDNS (`_esphomelib._tcp`) on this network, and OTA upload is a direct TCP push from a worker to a device. Anything that runs a worker needs IP reachability to the targets — so the flat-network assumption is load-bearing for the end-to-end test path, not incidental.

## Hosts

All hosts below are reachable over SSH with friendly aliases configured in `~/.ssh/config` on the development laptop, so `ssh hass-4`, `ssh docker-pve-1`, etc. work without explicit host / user flags.

The Docker VMs are named after the Proxmox node they run on (`docker-pve-N` on `pve-N`). Their build-worker containers keep their original hostnames (`docker-pve`, `docker-optiplex-5`) so existing worker rows stay matched — see `scripts/refresh-workers.sh`.

| Alias | Role |
|-------|------|
| `hass-4` | Production Home Assistant install at `192.168.225.112` — Debian 13 + Supervised HA. Target of `./push-to-hass-4.sh` and every `e2e-hass-4` Playwright run. The canonical "real HA" target. |
| `pve-1` (also `pve`) | Proxmox node 1, OptiPlex 5055 Ryzen, `192.168.225.128`. |
| `docker-pve-1` | Docker VM (VMID 105) on `pve-1`, `192.168.227.90`. Standalone-Docker (non-HAOS) server/worker test target; `scripts/test-matrix.py`'s `standalone-pve` target. Formerly `docker-pve`. |
| `pve-2` | Proxmox node 2, OptiPlex 3070, `192.168.227.227`. No guests. |
| `pve-3` (also `optiplex-5`) | Proxmox node 3, OptiPlex 7040, `192.168.225.227`. |
| `docker-pve-3` | Docker VM (VMID 202) on `pve-3`, `192.168.225.194`. Second standalone-Docker target — lets us exercise server-on-one-host / worker-on-another topologies on the standalone path. Formerly `docker-optiplex-5`. |
| `haos-pve` | Throwaway HAOS VM at `192.168.226.135` (SSH on port 22). SSH is provided by the Advanced SSH & Web Terminal add-on (`a0d7b954_ssh`), installed by `scripts/haos/onboard.sh` — which authorizes the public keys in `~/.config/distributed-esphome/haos-authorized-keys` and pins the addon's host port to 22. SSH lands you inside the add-on container, not on HAOS itself; from there `ha …` and `docker exec …` reach the rest of the system. |

## SSH

If the key isn't already loaded in the agent:

```bash
ssh-add ~/.ssh/id_ed25519
```

`~/.ssh/id_ed25519` is unencrypted on disk (developer-laptop convenience), so `ssh-add` runs non-interactively — Claude can re-arm the agent itself without waiting on the user when SSH starts failing mid-session (the macOS launchd agent occasionally drops identities; symptoms are `agent refused operation` or `Too many authentication failures` in deploy logs).

The 1Password / macOS-keychain SSH agent ALSO advertises a few unrelated identities ("SSH Key (abstract GitHub)", "Github (stefanzierpersonal)", "Personal SSH Key 2022") via the same `SSH_AUTH_SOCK`. They get offered before `id_ed25519` and burn the per-connection auth-attempt budget, so a clean `ssh <alias>` can hit `Permission denied; Too many authentication failures` even though `id_ed25519` itself is fine. When that happens, force the right key explicitly:

```bash
ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes <alias> ...
```

`id_ed25519`'s public key (`SHA256:szoIC2jm7xpjuJAXFuzd0NwFwEfwuhQGd1x236sOrr0 stefan@m3pro-6.local`) is the one authorized in `~root/.ssh/authorized_keys` on every host above. After that, any `ssh <alias>` / `scp` / `rsync` against the hosts above works passwordless. The end-of-turn log tail (`ssh root@hass-4.local "ha addons logs local_esphome_dist_server"`) and `./push-to-hass-4.sh` both depend on this.

Additional dev laptops (e.g. the AI MacBook) keep their own `id_ed25519`. Their public keys are appended to each host's `authorized_keys`. The canonical list for HAOS VMs lives in `~/.config/distributed-esphome/haos-authorized-keys` on the laptop running `scripts/haos/onboard.sh` — that file is what the script feeds into the Advanced SSH add-on. To authorize a new laptop on a fresh `haos-pve`, drop the new public key into that file before running onboard.

## Lab add-on bearer token

The Fleet for ESPHome add-on on `hass-4` is pinned to the server token:

```
2416d179b5d41bca62091f681065bca9
```

This is the value in `/data/settings.json → server_token` inside the add-on container (host path `/usr/share/hassio/addons/data/local_esphome_dist_server/settings.json`). Hard-code the same token in every lab build worker's `SERVER_TOKEN` env var so a clean reinstall of the add-on (which would otherwise auto-generate a fresh token and invalidate every worker) doesn't force a fleet-wide re-registration.

**When you do a clean install of the add-on on hass-4**, re-pin this value after the install: stop the add-on, edit the host-side `settings.json` (`server_token` key), start the add-on. `push-to-hass-4.sh` reads it from `~/.config/distributed-esphome/hass4-token` for downstream smoke probes — keep that file in sync with the pinned value. Lab-only; not a production secret — workers on a private `192.168.224.0/22` LAN are the only consumers.

## Typical use

- **`hass-4`** — every turn deploys here; the `e2e-hass-4` Playwright suite (real compile + OTA to `cyd-office-info`) runs against it. This is what the end-of-turn smoke step exercises.
- **`docker-pve-1`, `docker-pve-3`** — where the standalone Docker install path is exercised (anything touching `Dockerfile.standalone` or the non-HAOS auth flow). Two of them so we can run server-on-one / worker-on-another topologies without co-locating.
- **`pve-1`, `pve-2`, `pve-3`** — the hypervisors themselves (one Proxmox cluster). Rarely touched directly by a turn; typically only used to reboot / snapshot / reprovision their Docker VMs.
