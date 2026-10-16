# Homelab

A self-hosted homelab running on a dedicated mini PC in a home network, managed entirely with Docker Compose. The setup provides photo management, media streaming with automated downloads, network-level ad blocking, automatic TLS reverse proxying, dynamic DNS, a dashboard, log monitoring, an AI agent and a work-clock bot — all accessible from anywhere via a public domain or locally via `.local` hostnames.

## Network architecture

A router forwards only ports **80** and **443** to the mini PC. All public and local traffic enters through **Caddy**, which terminates TLS and routes requests to the appropriate service. Internal services communicate over a shared Docker bridge network (`homelab`) and are never directly reachable from outside.

```
Internet
    │
    │  :80 / :443
    ▼
 Router (port forward)
    │
    ▼
 Mini PC
    ├── Caddy (reverse proxy, TLS)
    │     ├── immich.homelab.example.com  ──► Immich
    │     ├── immich.local  (LAN only)   ──► Immich
    │     ├── jellyfin.local  (LAN only) ──► Jellyfin
    │     ├── adguard.local  (LAN only)  ──► AdGuard Home
    │     ├── homepage.local (LAN only)  ──► Homepage
    │     ├── grafana.local  (LAN only)  ──► Grafana
    │     └── hermes.local   (LAN only)  ──► Hermes Agent
    │
    ├── Immich           (photo library)
    ├── Jellyfin         (media server)
    ├── Arr stack        (Prowlarr, Sonarr, Radarr, qBittorrent, Seerr; LAN ports)
    ├── AdGuard Home     (DNS + ad blocking)
    ├── Homepage         (dashboard)
    ├── Monitoring       (Grafana + Loki + Alloy)
    ├── Hermes Agent     (AI agent, Telegram)
    ├── Sesame autoclock (work clock-in bot, Telegram)
    └── DDNS Updater     (keeps DNS record pointing to home IP)
```

Local-only services (anything under a `.local` hostname) reject connections from outside `192.168.1.0/24` at the Caddy level.

## Project structure

```
.
├── 00_homelab-network.sh           # Creates the shared Docker network (run once)
├── 01_docker-compose-up-all.sh     # Starts all services
├── 02_docker-compose-recreate-all.sh  # Pulls and force-recreates every container
│
├── caddy-reverse-proxy/
│   ├── Caddyfile                   # Route definitions and local-only access rules
│   └── docker-compose.yml
│
├── immich/
│   ├── docker-compose.yml          # Immich server, ML, Redis, PostgreSQL
│   ├── example.env                 # Template — copy to .env and fill in secrets
│   ├── backup-borg-setup.sh        # One-time Borg repository initialisation
│   └── backup-borg-script-v2.sh   # Incremental backup + prune (run via cron)
│
├── jellifyn/
│   └── docker-compose.yml          # Jellyfin with AMD GPU passthrough
│
├── arr-stack/
│   └── docker-compose.yml          # Prowlarr, Sonarr, Radarr, qBittorrent, Seerr
│
├── adguardhome/
│   └── docker-compose.yml          # AdGuard Home on host network
│
├── homepage/
│   ├── docker-compose.yml          # Exposed via Caddy at homepage.local
│   └── config/                     # services.yaml, widgets.yaml, bookmarks.yaml, ...
│
├── monitoring/                     # Grafana + Loki + Alloy (container, journald and backup logs)
│   ├── docker-compose.yml          # Exposed via Caddy at grafana.local
│   ├── config.alloy                # What Alloy collects and ships to Loki
│   ├── loki-config.yaml            # Single-node Loki, 30d retention
│   ├── grafana/provisioning/       # Loki datasource + "Homelab logs" dashboard
│   └── example.env                 # Template — copy to .env and set the Grafana admin password
│
├── hermes-agent/                   # See hermes-agent/README.md
│   ├── docker-compose.yml          # Exposed via Caddy at hermes.local
│   ├── backup-hermes.sh            # Daily backup to /mnt/backup (run via cron)
│   └── example.env                 # Template — copy to .env and set the dashboard login
│
├── sesame-autoclock/               # See sesame-autoclock/README.md
│   ├── Dockerfile                  # Python + Playwright Chromium
│   ├── app/                        # Bot, scheduler and Sesame automation
│   ├── docker-compose.yml
│   └── example.env                 # Template — copy to .env and fill in credentials
│
└── namecheap-ddns-updater/
    ├── Dockerfile                  # Minimal Alpine + bash + curl image
    ├── entrypoint.sh               # Polling loop that calls Namecheap DDNS API
    ├── docker-compose.yml
    └── example.env                 # Template — copy to .env and fill in secrets
```

## Services

### Caddy — reverse proxy & TLS

[Caddy](https://caddyserver.com) is the single entry point for all traffic. It automatically obtains and renews TLS certificates via Let's Encrypt for public domains, and issues its own local CA certificate for `.local` hostnames. The `Caddyfile` defines a reusable `local_only` snippet that aborts any connection not originating from the home LAN subnet, applied to every `.local` site block.

### Immich — photo library

[Immich](https://immich.app) is a self-hosted Google Photos alternative with automatic mobile backup, facial recognition, object detection, and an albums/timeline UI. The stack consists of four containers:

| Container | Role |
|---|---|
| `immich-server` | Main API and web UI |
| `immich-machine-learning` | Face and object recognition inference |
| `redis` (Valkey) | Job queue and caching |
| `database` (PostgreSQL + pgvecto-rs) | Persistent storage with vector search for smart albums |

Configuration is done via a `.env` file (see `example.env`). The most important variables are `UPLOAD_LOCATION` (where photos are stored on disk) and `DB_DATA_LOCATION` (PostgreSQL data directory).

#### Backup

Immich photos and database are backed up with [Borg](https://borgbackup.readthedocs.io):

1. **One-time setup** — initialise the Borg repository:
   ```bash
   ./immich/backup-borg-setup.sh
   ```
2. **Incremental backup** — dump the database and archive the library:
   ```bash
   ./immich/backup-borg-script-v2.sh
   ```
   Runs from root's crontab (see [Host setup](#host-setup)). Thumbnails and transcoded video are excluded (they are regenerable). Archives are pruned to keep the last 4 weekly and 3 monthly snapshots.

For restore instructions see the [Immich backup guide](https://immich.app/docs/guides/template-backup-script/#restoring).

### Jellyfin — media server

[Jellyfin](https://jellyfin.org) is a self-hosted media system for movies, TV shows, and music. The container is configured with AMD GPU passthrough (`/dev/dri`) and the `radeonsi` VAAPI driver for hardware-accelerated video transcoding. Media files are read from `/mnt/backup/media/jellyfinmedia` on the host, plus the arr-stack library at `/mnt/backup/media/data/media` (read-only).

### Arr stack — automated downloads

Feeds the Jellyfin library. All containers use the linuxserver images with `PUID`/`PGID` 1000, so their config dirs stay owned by `r-gmk`.

| Container | Port | Role |
|---|---|---|
| `prowlarr` | 9696 | Indexer manager, shared with Sonarr/Radarr |
| `sonarr` | 8989 | TV shows |
| `radarr` | 7878 | Movies |
| `qbittorrent` | 8080 (UI), 6881 (torrent) | Download client |
| `seerr` | 5055 | Request UI on top of Jellyfin (successor of Jellyseerr) |

Sonarr, Radarr and qBittorrent share one `/data` mount (`/mnt/backup/media/data`) so hardlinks and atomic moves work: qBittorrent downloads to `torrents/{movies,tv}`, Sonarr/Radarr place the final files in `media/{movies,tv}`. The UIs are reached by `IP:port` on the LAN, not through Caddy. App config dirs (`arr-stack/*/`) hold API keys and are gitignored.

### Homepage — dashboard

[Homepage](https://gethomepage.dev) is a start page linking every service, at `homepage.local`. Its configuration is the versioned `homepage/config/*.yaml`. It reads the Docker socket (read-only) to show container status.

### AdGuard Home — DNS & ad blocking

[AdGuard Home](https://github.com/AdguardTeam/AdGuardHome) acts as the network's DNS resolver. All LAN devices point to the mini PC as their DNS server. AdGuard Home blocks ad and tracker domains before they ever reach the browser, and also resolves the `.local` hostnames to the mini PC's LAN IP so that local Caddy routes work on any device without manual `/etc/hosts` entries.

It runs with `network_mode: host` so it can bind to port 53 on the LAN interface. The web UI is on port 81, accessible via `adguard.local` through Caddy.

Its whole config (DNS rewrites, blocklists, admin password hash) is `config/AdGuardHome.yaml`. It is not versioned because it holds the password hash; root's crontab copies it daily to `/mnt/backup/adguardhome/config/` instead (see [Host setup](#host-setup)).

### Monitoring — Grafana, Loki, Alloy

Centralised logs at `grafana.local`. [Alloy](https://grafana.com/docs/alloy/) collects every container's logs (via the Docker socket), the host journal (including the Hermes backup cron) and the Immich backup logs in `/var/log/immich-backup`, and ships them to [Loki](https://grafana.com/oss/loki/) (30-day retention). [Grafana](https://grafana.com/oss/grafana/) is provisioned from the repo with the Loki datasource and the "Homelab logs" dashboard. Data lives in named Docker volumes, since Loki and Grafana run as non-root users. Set the Grafana admin password in `monitoring/.env`.

### Hermes Agent — AI agent

[Hermes Agent](https://hermes-agent.nousresearch.com) gateway reachable over Telegram, with a web dashboard at `hermes.local`. It uses the Claude subscription (via the `claude` CLI) instead of an API key. Everything, including credentials, lives in `hermes-agent/data/` (gitignored) and is backed up daily to `/mnt/backup/hermes-backup/`, keeping 14 days. Setup steps are in [hermes-agent/README.md](hermes-agent/README.md).

### Sesame autoclock — work clock-in bot

Telegram bot that clocks in and out on Sesame HR through a headless Chromium (Playwright): it plans each workday, skips holidays and absences, clocks automatically on remote days and sends reminders on office days. Its state and browser profile live in `sesame-autoclock/data/`. Details are in [sesame-autoclock/README.md](sesame-autoclock/README.md).

### Namecheap DDNS updater

A lightweight custom service (Alpine + bash + curl) that keeps a Namecheap DNS record updated when the home IP changes. Every 10 minutes it fetches the current public IP from `checkip.amazonaws.com` and calls the Namecheap DDNS API for each configured hostname.

Configure via `.env` (see `example.env`):

| Variable | Description |
|---|---|
| `DDNS_PASSWORD` | Namecheap DDNS password (found in the domain's DNS management page) |
| `DOMAIN_NAME` | Root domain, e.g. `example.com` |
| `HOSTNAME_ENTRIES` | Comma-separated subdomains to update, e.g. `@,immich,vpn` |

## Getting started

### Prerequisites

- Docker and Docker Compose installed on the host
- Borg installed on the host (for Immich backups)
- A Namecheap domain with DDNS enabled
- Router port forwarding: **80** and **443** → mini PC

### Host setup

Everything here lives outside the repo, on the host itself.

- **User:** `r-gmk` must be uid/gid **1000** (the arr-stack `PUID`/`PGID` assume it) and in the `docker` group:
  ```bash
  sudo usermod -aG docker r-gmk
  ```
- **Static LAN IP:** the mini PC must be `192.168.1.2` on `192.168.1.0/24`. The Caddyfile hardcodes both (local-only allowlist and the AdGuard upstream), and LAN devices use it as their DNS server. Set it with a DHCP reservation on the router or in netplan.
- **Backup disk** mounted at `/mnt/backup`, in `/etc/fstab` (get the UUID with `sudo blkid`; `nofail` keeps the host booting without it):
  ```
  UUID=b805fdcd-5d8f-444f-81d2-3c8d3e82c3c1 /mnt/backup ext4 defaults,nofail,x-systemd.device-timeout=10 0 2
  ```
- **Media paths:** `/mnt/backup/media/...` (Jellyfin, arr-stack) live on the external USB disk mounted at `/mnt/backup` (see `/etc/fstab`), owned by `r-gmk`.
- **Tailscale** as an exit node (also approve it in the Tailscale admin console):
  ```bash
  curl -fsSL https://tailscale.com/install.sh | sh
  echo 'net.ipv4.ip_forward = 1' | sudo tee /etc/sysctl.d/99-tailscale.conf
  echo 'net.ipv6.conf.all.forwarding = 1' | sudo tee -a /etc/sysctl.d/99-tailscale.conf
  sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
  sudo tailscale up --advertise-exit-node
  ```
- **Cron jobs.** Root's crontab (`sudo crontab -e`) needs root because the Immich library, the Borg repo and `AdGuardHome.yaml` are root-owned:
  ```
  0 3 * * 0,3 /home/r-gmk/docker/immich/backup-borg-script-v2.sh
  0 3 * * *   mountpoint -q /mnt/backup && install -D -m 600 /home/r-gmk/docker/adguardhome/config/AdGuardHome.yaml /mnt/backup/adguardhome/config/AdGuardHome.yaml
  ```
  `r-gmk`'s crontab (`crontab -e`):
  ```
  30 3 * * * /home/r-gmk/docker/hermes-agent/backup-hermes.sh 2>&1 | systemd-cat -t hermes-backup
  ```

### First-time setup

```bash
# 1. Create the shared Docker network
./00_homelab-network.sh

# 2. Create every .env from its example and fill in the secrets
#    (immich, namecheap-ddns-updater, monitoring, sesame-autoclock, hermes-agent)
for d in */; do [ -f "$d/example.env" ] && [ ! -f "$d/.env" ] && cp "$d/example.env" "$d/.env"; done
# immich/.env: DB_PASSWORD, UPLOAD_LOCATION, DB_DATA_LOCATION
# namecheap-ddns-updater/.env: DDNS_PASSWORD, DOMAIN_NAME, HOSTNAME_ENTRIES

# 3. Initialise Borg backup repository (skip if restoring from an existing /mnt/backup)
./immich/backup-borg-setup.sh

# 4. Restore AdGuard config, if a backup exists
sudo install -D -m 600 /mnt/backup/adguardhome/config/AdGuardHome.yaml adguardhome/config/AdGuardHome.yaml

# 5. Start all services
./01_docker-compose-up-all.sh
```

Keep a copy of the `.env` files outside this host (e.g. a password manager); they are neither versioned nor backed up.

### Starting / stopping individual services

```bash
cd <service-dir>
docker compose up -d       # start
docker compose down        # stop
docker compose logs -f     # follow logs
```
