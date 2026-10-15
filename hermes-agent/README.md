# Hermes Agent

[Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/docker) gateway (Telegram etc.), using the
[Claude Subscription DirectSDK](https://hermes-agent.nousresearch.com/docs/plugins/claude-subscription-directsdk)
plugin so turns are billed to the Claude Pro/Max subscription instead of an API key.

Everything lives in `./data` (mounted at `/opt/data`, the hermes user's `$HOME`), including the `claude` CLI
(`.local/bin/claude`) and its login (`.claude/`). It is gitignored because it holds credentials.

## First-time setup

```bash
# 0. Dashboard login (compose refuses to start without .env)
cp example.env .env   # set a password

# 1. claude CLI into the data volume (survives image upgrades)
docker compose run --rm hermes sh -c 'curl -fsSL https://claude.ai/install.sh | bash'

# 2. Plugin + model
docker compose run --rm hermes plugins install claude-subscription-directsdk
docker compose run --rm hermes plugins enable claude-subscription-directsdk-experimental
docker compose run --rm hermes config set model.provider claude-subscription-directsdk-experimental
docker compose run --rm hermes config set model.default sonnet
docker compose run --rm hermes config set providers.claude-subscription-directsdk-experimental.stale_timeout_seconds 900

# 3. Log the claude CLI into the subscription (interactive: open URL, paste code back)
docker compose up -d
docker exec -it -u hermes -e HOME=/opt/data hermes claude auth login

# 4. Messaging: set TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_USERS in data/.env, then
docker compose restart
```

## Web UI (https://hermes.local)

Dashboard on port 9119, proxied by Caddy (LAN only), login with the credentials from `.env`.
Tell it it sits behind Caddy (`172.19.0.0/16` is the `homelab` docker network) so session cookies are marked secure:

```bash
docker compose run --rm hermes config set dashboard.public_url https://hermes.local
docker compose run --rm hermes config set dashboard.trusted_proxies '["172.19.0.0/16"]'
```

Make `hermes.local` resolve to the homelab the same way `grafana.local` does (AdGuard DNS rewrite), then reload Caddy:
`docker exec caddy-reverse-proxy-caddy-1 caddy reload --config /etc/caddy/Caddyfile`.

Quick test from the CLI: `docker exec -it hermes hermes chat -q "hello"`.

## Backup

`backup-hermes.sh` runs `hermes backup` (config, `.env`, memories, sessions, skills, cron, state DB; ~120 MB, skips
reinstallable deps/caches) and moves the zip to `/mnt/backup/hermes-backup/`, keeping 14 days.

```bash
sudo mkdir /mnt/backup/hermes-backup && sudo chown r-gmk: /mnt/backup/hermes-backup   # once
crontab -e   # add:
30 3 * * * /home/r-gmk/docker/hermes-agent/backup-hermes.sh 2>&1 | systemd-cat -t hermes-backup
```

Output goes to the journal, which Alloy ships to Loki: see "Backup runs" / "Backup failures" on the Grafana
Homelab logs dashboard, or `journalctl -t hermes-backup`.

Restore: copy a zip into `data/`, then `docker exec hermes hermes import /opt/data/<zip>` and `docker compose restart`.

## Notes

- `docker exec hermes hermes ...` drops to the hermes user automatically; anything else (e.g. `claude`) needs
  `-u hermes -e HOME=/opt/data`, otherwise files land root-owned in `data/`.
- `claude` CLI too old for a model ("does not support this model"): `docker exec -u hermes -e HOME=/opt/data hermes claude update`.
- Don't set `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` in `data/.env`: the plugin refuses to run with them.
- Never run two gateway containers on the same `data/`.
