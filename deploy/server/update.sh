#!/bin/sh
# Updates asked from the console (/admin › Aggiornamenti). The backoffice
# has no Docker socket and no root: it only leaves updates/request.json with
# the version to install. This script, started by systemd as soon as the
# request is written (eullm-update.path) and every minute by root's cron as
# a fallback, takes the request, backs up (backup.sh), pulls the images of
# that version, writes it in .env, restarts and checks that the dashboard
# answers. How it went, with a percentage, goes to updates/status.json and
# updates/update.log, which the console shows. After an update it replaces
# itself with the script of the installed version. Guide: README.md,
# "Updating from the console".
#
#   ./update.sh install   once, as root: the shared folder, systemd and cron
#   ./update.sh trigger   as root: only the systemd units (run by itself)
#   ./update.sh           what systemd and cron run: nothing to do without a request
set -eu
cd "$(dirname "$0")"
here=$PWD
dir=$here/updates
EDITOR_UID=999   # the user of the editor image (editor-mode/Dockerfile)
REPO=https://raw.githubusercontent.com/eullm/eullm-agent

# The request starts the update at once: a systemd path unit watches the
# folder. Without systemd the cron job alone takes it within a minute.
trigger() {
  [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1 || return 0
  cat > /etc/systemd/system/eullm-update.service <<EOF
[Unit]
Description=Redazia update asked from the console
[Service]
Type=oneshot
ExecStart=$here/update.sh
TimeoutStartSec=0
EOF
  cat > /etc/systemd/system/eullm-update.path <<EOF
[Unit]
Description=Redazia: start an update as soon as the console asks
[Path]
PathChanged=$dir
Unit=eullm-update.service
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now eullm-update.path >/dev/null 2>&1 && echo "systemd: eullm-update.path enabled"
}

if [ "${1:-}" = "trigger" ]; then
  [ "$(id -u)" = 0 ] || { echo "run it as root: sudo $0 trigger"; exit 1; }
  trigger
  exit 0
fi

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
  trigger
  docker compose up -d
  echo "done: /admin › Aggiornamenti now updates this server"
  exit 0
fi

# Files of the shared folder are written by the dashboard's user: this
# script, run by root, never writes through them (a link placed there would
# point root elsewhere). It writes in its own folder and moves the result in:
# a rename replaces a link, it does not follow it.
work=$here/.work
mkdir -p "$work"
chmod 700 "$work"
exec 9>"$work/lock"
flock -n 9 || exit 0

publish() {  # file in $work -> same name in the shared folder
  cp "$work/$1" "$work/$1.pub" && chmod 644 "$work/$1.pub" && mv -f "$work/$1.pub" "$dir/$1"
}

# ---------------------------------------------------------------- backups
# Settings from the console (backup.conf), checked again here: the folder
# is written by root, so only plain paths under the allowed roots and never
# a folder a web server publishes.
conf() { sed -n "s/^$1=//p" "$dir/backup.conf" 2>/dev/null | tail -n 1; }
num() { case $1 in ''|*[!0-9]*) return 1 ;; esac; }
safe_dir() {
  printf '%s' "$1" | grep -Eq '^/[A-Za-z0-9._/-]+$' || return 1
  case "$1/" in *//*|*/./*|*/../*) return 1 ;; esac
  case "$1/" in /home/?*|/var/backups/?*|/backup/?*|/backups/?*|/mnt/?*|/media/?*|/srv/?*|/opt/?*) ;; *) return 1 ;; esac
  ! printf '%s' "$1" | grep -Eiq '/(public_html|www|htdocs|html|web|httpdocs|wwwroot)(/|$)'
}
settings() {
  b_on=$(conf ENABLED); b_freq=$(conf FREQUENCY); b_hour=$(conf HOUR); b_min=$(conf MINUTE)
  b_wday=$(conf WEEKDAY); b_dir=$(conf DIR); b_keep=$(conf KEEP); b_owner=$(conf OWNER)
  b_name=$(conf NAME); b_remote=$(conf REMOTE); b_target=$(conf TARGET); b_port=$(conf PORT)
  b_bad=
  num "${b_hour:-3}" && num "${b_min:-30}" && num "${b_wday:-7}" && num "${b_keep:-14}" && num "${b_port:-22}" || b_bad=numbers
  [ -z "$b_dir" ] || safe_dir "$b_dir" || b_bad=folder
  [ -z "$b_owner" ] || { printf '%s' "$b_owner" | grep -Eq '^[a-z_][a-z0-9_-]*(:[a-z_][a-z0-9_-]*)?$' && id -u "${b_owner%%:*}" >/dev/null 2>&1; } || b_bad=owner
  printf '%s' "${b_name:-redazia}" | grep -Eq '^[A-Za-z0-9_-]{1,40}$' || b_bad=name
  [ -z "$b_target" ] || printf '%s' "$b_target" | grep -Eq '^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/~-]+$' || b_bad=target
  [ -n "$b_bad" ] && b_on=0
  b_name=${b_name:-redazia}
  bdir=${b_dir:-$here/backup}
  # A folder that is a link counts where it leads.
  [ -z "$b_dir" ] || [ ! -e "$bdir" ] || { real=$(readlink -f "$bdir") && safe_dir "$real"; } || { b_bad=folder; b_on=0; }
}
run_backup() {  # one archive with these settings; its path on the last line of the log
  BACKUP_DIR=$bdir BACKUP_KEEP=${b_keep:-14} BACKUP_NAME=$b_name BACKUP_OWNER=$b_owner ./backup.sh
}
due() {  # the time set has come and no backup ran since
  [ "$b_on" = 1 ] || return 1
  now=$(date +%s)
  slot=$(date -d "$(date +%F) ${b_hour:-3}:${b_min:-30}" +%s) || return 1
  [ "$now" -ge "$slot" ] || return 1
  [ "$b_freq" != weekly ] || [ "$(date +%u)" = "${b_wday:-7}" ] || return 1
  last=$(cat "$work/backup-last" 2>/dev/null || echo 0)
  num "$last" || last=0
  [ "$last" -lt "$slot" ]
}
key=/root/.ssh/redazia_backup
remote_copy() {
  command -v rsync >/dev/null 2>&1 || { echo "rsync is not installed on this server (apt install rsync)"; return 1; }
  rsync -a --delete --include="$b_name-*.tgz" --exclude='*' \
    -e "ssh -i $key -p ${b_port:-22} -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20" \
    "$bdir/" "$b_target/"
}
backup_status() {  # state: what the console shows; written only when it changes
  pub=
  if [ -n "$b_target" ] || [ "$b_remote" = 1 ]; then
    [ -f "$key" ] || { mkdir -p -m 700 /root/.ssh && ssh-keygen -q -t ed25519 -N '' -C "redazia-backup@$(hostname)" -f "$key"; } || true
    pub=$(cat "$key.pub" 2>/dev/null)
  fi
  list=$(find "$bdir" -maxdepth 1 -type f -name "$b_name-*.tgz" -printf '{"name": "%f", "size": %s, "at": "%TY-%Tm-%TdT%TH:%TM"}\n' 2>/dev/null | sort -r | paste -sd, -)
  printf '{"state": "%s", "result": %s, "bad": "%s", "dir": "%s", "archives": [%s], "pubkey": "%s"}\n' \
    "$1" "$(cat "$work/backup-result" 2>/dev/null || echo '{}')" "$b_bad" "$bdir" "$list" "$pub" > "$work/backup-status.json"
  cmp -s "$work/backup-status.json" "$dir/backup-status.json" || publish backup-status.json
}
backups() {
  settings
  # Archives to delete, asked from the console: only names of this folder.
  if [ -f "$dir/backup-delete.json" ]; then
    names=$(grep -o '[A-Za-z0-9_-]*-[0-9]\{4\}-[0-9]\{2\}-[0-9]\{2\}_[0-9]\{4\}\.tgz' "$dir/backup-delete.json" | head -n 500)
    rm -f "$dir/backup-delete.json"
    for f in $names; do
      case $f in "$b_name"-*) [ -f "$bdir/$f" ] && rm -f "$bdir/$f" ;; esac
    done
  fi
  asked=
  [ -f "$dir/backup-now.json" ] && { asked=1; rm -f "$dir/backup-now.json"; }
  if [ -n "$asked" ] && [ -n "$b_bad" ]; then
    printf '{"at": "%s", "ok": false, "why": "settings"}\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$work/backup-result"
    asked=
  fi
  if [ -n "$asked" ] || due; then
    date +%s > "$work/backup-last"
    backup_status running
    blog=$work/backup.log
    echo "$(date -u) backup into $bdir" > "$blog"
    publish backup.log
    ok=true; remote=off
    run_backup >> "$blog" 2>&1 || ok=false
    file=$(tail -n 1 "$blog" | sed -n 's|.*/\([A-Za-z0-9_-]*-[0-9-]*_[0-9]*\.tgz\)$|\1|p')
    if $ok && [ "$b_remote" = 1 ] && [ -n "$b_target" ]; then
      backup_status running
      echo "copy to $b_target" >> "$blog"
      if remote_copy >> "$blog" 2>&1; then remote=done; else remote=failed; fi
    fi
    size=0; [ -n "$file" ] && size=$(stat -c %s "$bdir/$file" 2>/dev/null || echo 0)
    printf '{"at": "%s", "ok": %s, "file": "%s", "size": %s, "remote": "%s"}\n' \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$ok" "$file" "$size" "$remote" > "$work/backup-result"
    publish backup.log
  fi
  backup_status idle
}
# A backup that goes wrong never stops an update.
backups || true

# ---------------------------------------------------------------- updates
req=$dir/request.json
[ -f "$req" ] || exit 0
version=$(sed -n 's/.*"version": *"\([0-9]\{1,4\}\.[0-9]\{1,4\}\.[0-9]\{1,4\}\)".*/\1/p' "$req" | head -n 1)
rm -f "$req"
old=$(sed -n 's/^EULLM_VERSION=//p' .env | tail -n 1)
log=$work/update.log
step=start

status() {  # state [percent]
  printf '{"state": "%s", "step": "%s", "percent": %s, "version": "%s", "from": "%s", "at": "%s"}\n' \
    "$1" "$step" "${2:-0}" "$version" "$old" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$work/status.json"
  publish status.json
  publish update.log
}
fail() {
  echo "failed during: $step" >> "$log"
  status failed
  exit 1
}

# The download of the images, with its percentage: compose tells the
# progress of each layer as JSON; the average of the layers goes from 10 to
# 80 on the console's bar. Other lines go to the log. A compose that does
# not know --progress json pulls the plain way.
pull() {
  rm -f "$work/pull-rc"
  { EULLM_VERSION=$version docker compose --progress json pull 2>&1; echo "RC $?"; } | awk '
    function field(k,   r) { if (match($0, "\"" k "\":\"[^\"]*\"")) { r = substr($0, RSTART, RLENGTH); sub("^\"" k "\":\"", "", r); sub("\"$", "", r); return r } return "" }
    /^RC [0-9]+$/ { print; next }
    /^\{/ {
      parent = field("parent_id"); text = field("text")
      if (field("status") == "Error") { print "LOG " $0; next }
      if (parent == "") next
      key = parent "/" field("id")
      if (!(key in pct)) { pct[key] = 0; n++ }
      if (text ~ /complete|Already exists/) pct[key] = 100
      else if (text == "Downloading" && match($0, /"percent":[0-9]+/)) pct[key] = substr($0, RSTART + 10, RLENGTH - 10) + 0
      sum = 0; for (k in pct) sum += pct[k]
      p = int(10 + 70 * sum / (n * 100))
      if (p > last) { print "PCT " p; last = p; fflush() }
      next
    }
    { print "LOG " $0 }' | while read -r kind rest; do
      case $kind in
        PCT) status running "$rest" ;;
        RC) echo "$rest" > "$work/pull-rc" ;;
        *) echo "$rest" >> "$log" ;;
      esac
    done
  [ "$(cat "$work/pull-rc" 2>/dev/null)" = 0 ] && return 0
  grep -q -- '--progress' "$log" && EULLM_VERSION=$version docker compose pull >> "$log" 2>&1
}

# The scripts of the version just installed take these ones' place (a new
# file: the shell running this one keeps reading the old), and with them the
# way the script is started.
renew() {
  for f in update.sh backup.sh; do
    curl -fsSL "$REPO/v$version/deploy/server/$f" -o "$work/$f.new" 2>> "$log" || continue
    if [ "$(head -n 1 "$work/$f.new")" = "#!/bin/sh" ] && sh -n "$work/$f.new" && ! cmp -s "$work/$f.new" "$here/$f"; then
      chmod 755 "$work/$f.new"
      mv "$work/$f.new" "$here/$f"
      echo "$f renewed from v$version" >> "$log"
    fi
    rm -f "$work/$f.new"
  done
  "$here/update.sh" trigger >> "$log" 2>&1 || true
  publish update.log
}

: > "$log"
if [ -z "$version" ]; then step=request; echo "no valid version in the request" >> "$log"; fail; fi
echo "$(date -u) update from ${old:-latest} to $version" >> "$log"

# The backup before the update goes where the console's backups go.
step=backup; status running 2
run_backup >> "$log" 2>&1 || fail

# The images of a new release appear a few minutes after its tag: try for 10 minutes.
step=pull; status running 10
tries=0
until pull; do
  tries=$((tries + 1))
  [ "$tries" -lt 20 ] || fail
  sleep 30
done

step=restart; status running 80
if grep -q '^EULLM_VERSION=' .env; then
  sed -i "s/^EULLM_VERSION=.*/EULLM_VERSION=$version/" .env
else
  echo "EULLM_VERSION=$version" >> .env
fi
docker compose up -d >> "$log" 2>&1 || fail

step=check; status running 90
port=$(sed -n 's/^EDITOR_PORT=//p' .env | tail -n 1)
tries=0
until curl -fsS "http://127.0.0.1:${port:-8090}/api/health" >> "$log" 2>&1; do
  tries=$((tries + 1))
  [ "$tries" -lt 36 ] || fail
  sleep 5
done
echo >> "$log"
docker compose ps >> "$log" 2>&1 || true
step=done; status done 100
renew
