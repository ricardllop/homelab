#!/bin/bash
# Daily Hermes backup: built-in `hermes backup` (consistent SQLite snapshot), then moved to the backup disk.
# The backup disk is never mounted into the container, so the agent can't touch it.
set -eu

DATA_DIR="/home/r-gmk/docker/hermes-agent/data"
BACKUP_PATH="/mnt/backup/hermes-backup"
KEEP_DAYS=14

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') - $*"; }
trap 'log "Backup FAILED at line $LINENO"' ERR

# Abort if the backup disk is not mounted (avoid writing to the root fs)
mountpoint -q /mnt/backup || { log "Backup disk not mounted at /mnt/backup"; exit 1; }

name="hermes-backup-$(date +%Y-%m-%d_%H-%M-%S).zip"
log "Creating $name..."
docker exec hermes hermes backup -o "/opt/data/backups/$name" -k 0 >/dev/null
mv "$DATA_DIR/backups/$name" "$BACKUP_PATH/"

find "$BACKUP_PATH" -name 'hermes-backup-*.zip' -mtime +"$KEEP_DAYS" -delete
log "Done: $(du -h "$BACKUP_PATH/$name" | cut -f1), $(ls "$BACKUP_PATH" | wc -l) backups kept."
