# Homelab with Docker Compose

This repository contains the setup for my personal homelab, powered by Docker Compose. It includes self-hosted services such as reverse proxies, photo management, media servers, dynamic DNS updates, and backups.

## Project Structure

```bash
.
├── 00_homelab-network.sh            # Script to create a shared Docker network
├── 01_docker-compose-up-all.sh      # Script to start all services
├── caddy-reverse-proxy/             # Reverse proxy with Caddy
│   ├── Caddyfile
│   ├── docker-compose.yml
│   ├── caddy_config/
│   ├── caddy_data/
│   └── site/
├── immich/                          # Self-hosted photo library with Immich
│   ├── docker-compose.yml
│   ├── example.env
│   ├── backup-borg-script-v2.sh
│   ├── backup-borg-setup.sh
│   ├── library/
│   └── postgres/
├── jellifyn/                        # Media server (like Jellyfin)
│   ├── docker-compose.yml
│   ├── cache/
│   └── config/
└── namecheap-ddns-updater/          # Dynamic DNS updater for Namecheap
    ├── docker-compose.yml
    ├── Dockerfile
    ├── entrypoint.sh
    └── example.env
