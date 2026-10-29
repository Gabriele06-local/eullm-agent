#!/bin/sh
# Updates asked from the console (/admin › Aggiornamenti). The backoffice
# has no Docker socket and no root: it only leaves updates/request.json with
# the version to install. This script, run every minute by root's cron,
# takes the request, backs up (backup.sh), pulls the images of that version,
# writes it in .env, restarts and checks that the dashboard answers. How it
# went goes to updates/status.json and updates/update.log, which the console
# shows. Guide: README.md, "Updating from the console".
#
#   ./update.sh install   once, as root: the shared folder and the cron job
#   ./update.sh           what cron runs: nothing to do without a request
set -eu
cd "$(dirname "$0")"
here=$PWD
dir=$here/updates
EDITOR_UID=999   # the user of the editor image (editor-mode/Dockerfile)

if [ "${1:-}" = "install" ]; then
  [ "$(id -u)" = 0 ] || { echo "run it as root: sudo $0 install"; exit 1; }
  install -d -m 0770 -o "$EDITOR_UID" -g "$EDITOR_UID" "$dir"
  chmod +x "$here/update.sh" "$here/backup.sh"
  # Installations made before this script: their compose.yaml has no shared
  # folder, so it is added by an override file that compose reads by itself.
  if ! grep -q EDITOR_UPDATES_DIR compose.yaml && [ ! -e compose.override.yaml ]; then
    cat > compose.override.yaml <<'EOF'
# Written by update.sh install: the folder of the updates from the console.
services:
  editor-web:
    environment: {EDITOR_UPDATES_DIR: /var/lib/editor/updates}
    volumes: ["./updates:/var/lib/editor/updates"]
  editor-worker:
    environment: {EDITOR_UPDATES_DIR: /var/lib/editor/updates}
    volumes: ["./updates:/var/lib/editor/updates"]
EOF
    echo "written: compose.override.yaml"
  fi
  echo "* * * * * root $here/update.sh >/dev/null 2>&1" > /etc/cron.d/eullm-update
  chmod 644 /etc/cron.d/eullm-update
  docker compose up -d
  echo "done: /admin › Aggiornamenti now updates this server"
  exit 0
fi

req=$dir/request.json
[ -f "$req" ] || exit 0
exec 9>"$dir/.lock"
flock -n 9 || exit 0

version=$(sed -n 's/.*"version": *"\([0-9]\{1,4\}\.[0-9]\{1,4\}\.[0-9]\{1,4\}\)".*/\1/p' "$req" | head -n 1)
rm -f "$req"
old=$(sed -n 's/^EULLM_VERSION=//p' .env | tail -n 1)
log=$dir/update.log
step=start

status() {  # state
  printf '{"state": "%s", "step": "%s", "version": "%s", "from": "%s", "at": "%s"}\n' \
    "$1" "$step" "$version" "$old" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$dir/status.json.part"
  chmod 644 "$dir/status.json.part"
  mv "$dir/status.json.part" "$dir/status.json"
}
fail() {
  echo "failed during: $step" >> "$log"
  status failed
  exit 1
}

: > "$log"
chmod 644 "$log"
if [ -z "$version" ]; then step=request; echo "no valid version in the request" >> "$log"; fail; fi
echo "$(date -u) update from ${old:-latest} to $version" >> "$log"

step=backup; status running
./backup.sh >> "$log" 2>&1 || fail

# The images of a new release appear a few minutes after its tag: try for 10 minutes.
step=pull; status running
tries=0
until EULLM_VERSION=$version docker compose pull >> "$log" 2>&1; do
  tries=$((tries + 1))
  [ "$tries" -lt 20 ] || fail
  sleep 30
done

step=restart; status running
if grep -q '^EULLM_VERSION=' .env; then
  sed -i "s/^EULLM_VERSION=.*/EULLM_VERSION=$version/" .env
else
  echo "EULLM_VERSION=$version" >> .env
fi
docker compose up -d >> "$log" 2>&1 || fail

step=check; status running
port=$(sed -n 's/^EDITOR_PORT=//p' .env | tail -n 1)
tries=0
until curl -fsS "http://127.0.0.1:${port:-8090}/api/health" >> "$log" 2>&1; do
  tries=$((tries + 1))
  [ "$tries" -lt 36 ] || fail
  sleep 5
done
echo >> "$log"
docker compose ps >> "$log" 2>&1 || true
step=done; status done
