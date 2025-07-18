# Requisite, have borg installed
# https://borgbackup.readthedocs.io/en/stable/installation.html

UPLOAD_LOCATION="/home/r-gmk/docker/immich/library"       # Immich database location, as set in your .env file
BACKUP_PATH="/mnt/disk1/r-gmk/immich-backup"

mkdir "$UPLOAD_LOCATION/database-backup"
borg init --encryption=none "$BACKUP_PATH/immich-borg"

# This is only setup, the backup script will do the actual backup
# RESTORING: https://immich.app/docs/guides/template-backup-script/#restoring
