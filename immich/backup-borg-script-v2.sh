#!/bin/bash
set -eu  # Exit on error and undefined variable

# Paths
UPLOAD_LOCATION="/home/r-gmk/docker/immich/library"
BACKUP_PATH="/mnt/disk1/r-gmk/immich-backup"
LOG_DIR="/var/log/immich-backup"
LOG_FILE="$LOG_DIR/backup-$(date +'%Y-%m-%d_%H-%M-%S').log"

# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Redirect all output to log file
exec > "$LOG_FILE" 2>&1

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $*"
}

trap 'log "Backup script FAILED at line $LINENO"; exit 1' ERR

log "=== Immich backup script started ==="

# Dump PostgreSQL database
log "Dumping Immich database..."
docker exec -t immich_postgres pg_dumpall --clean --if-exists --username=postgres > "$UPLOAD_LOCATION/database-backup/immich-database.sql"
log "Database dump complete."

# Run Borg backup
log "Running Borg backup..."
borg create "$BACKUP_PATH/immich-borg::{now}" "$UPLOAD_LOCATION" \
    --exclude "$UPLOAD_LOCATION/thumbs/" \
    --exclude "$UPLOAD_LOCATION/encoded-video/"
log "Borg archive created."

# Prune old backups
log "Pruning old Borg backups..."
borg prune --keep-weekly=4 --keep-monthly=3 "$BACKUP_PATH/immich-borg"
log "Prune complete."

# Compact repository
log "Compacting Borg repository..."
borg compact "$BACKUP_PATH/immich-borg"
log "Compact complete."

log "=== Backup script completed successfully ==="
