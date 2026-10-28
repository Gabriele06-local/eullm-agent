#!/bin/sh
# One dated archive of the installation: the database (pg_dump, taken while
# everything runs) plus .env, core.yaml, compose.yaml and initdb/, enough to
# restore it here or on a new server. Guide: BACKUP.md.
#
#   BACKUP_DIR    where the archives go     (default: ./backup)
#   BACKUP_OWNER  user:group owning them    (default: unchanged)
#   BACKUP_KEEP   days an archive is kept   (default: 14)
#   BACKUP_NAME   start of the file name    (default: eullm)
set -eu
cd "$(dirname "$0")"
dir=${BACKUP_DIR:-$PWD/backup}
keep=${BACKUP_KEEP:-14}
name=${BACKUP_NAME:-eullm}
stamp=$(date +%F_%H%M)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
umask 077
mkdir -p "$dir"
docker compose exec -T postgres pg_dump -U postgres -Fc eullm > "$tmp/eullm.dump"
cp -a .env core.yaml compose.yaml initdb "$tmp/"
tar czf "$dir/$name-$stamp.tgz.part" -C "$tmp" .
mv "$dir/$name-$stamp.tgz.part" "$dir/$name-$stamp.tgz"
chmod 700 "$dir"; chmod 600 "$dir/$name-$stamp.tgz"
if [ -n "${BACKUP_OWNER:-}" ]; then chown "$BACKUP_OWNER" "$dir" "$dir/$name-$stamp.tgz"; fi
find "$dir" -name "$name-*.tgz" -mtime +"$keep" -delete
echo "$dir/$name-$stamp.tgz"
