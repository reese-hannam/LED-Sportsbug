#!/usr/bin/env bash
# Push this checkout to the Pi and restart the panel.
#
#   ./deploy/push.sh              code + assets, then restart
#   ./deploy/push.sh --code       skip assets (faster; use while iterating)
#   ./deploy/push.sh --no-restart just copy, leave the service alone
#   ./deploy/push.sh --logs       copy, restart, then follow the log
#
# RUN THIS ON THE LAPTOP, not on the Pi.
set -euo pipefail

PI="${PI:-sportsbug@sportsbug.local}"
DEST="${DEST:-led-sports-bug}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

restart=1; assets=1; follow=0
for a in "$@"; do
  case "$a" in
    --code)       assets=0 ;;
    --no-restart) restart=0 ;;
    --logs)       follow=1 ;;
    *) echo "unknown option: $a" >&2; exit 2 ;;
  esac
done

# Running this ON the Pi is the single easiest mistake to make -- the paths are
# a laptop's, so it fails with a confusing "No such file or directory" on cd.
# Catch it and say what actually went wrong.
if [[ -r /proc/device-tree/model ]] && grep -qi raspberry /proc/device-tree/model 2>/dev/null; then
  echo "This is the Pi. push.sh copies FROM the laptop TO the Pi -- run it there." >&2
  exit 1
fi

PATHS=(app tools deploy requirements.txt start.sh)
(( assets )) && PATHS+=(assets)

echo "==> $PI:~/$DEST"
echo "    sending: ${PATHS[*]}"

# COPYFILE_DISABLE + --no-xattrs stop macOS shipping AppleDouble "._" sidecars
# and printing a screenful of xattr warnings. The "._" files matter: the logo
# loader globs *.png and would otherwise pick up hundreds of 4KB junk files.
cd "$REPO"
COPYFILE_DISABLE=1 tar --no-xattrs \
    --exclude '.venv' --exclude '.git' --exclude '__pycache__' \
    --exclude '*.pyc' --exclude '._*' \
    -czf - "${PATHS[@]}" \
  | ssh "$PI" "mkdir -p ~/$DEST && tar xzf - -C ~/$DEST && find ~/$DEST -name '._*' -delete"

echo "    copied."

if (( restart )); then
  echo "==> restarting the panel"
  # -t for a tty so sudo can prompt for the password.
  ssh -t "$PI" "sudo systemctl restart $DEST 2>/dev/null || sudo systemctl restart sportsbug; sleep 6; systemctl is-active sportsbug && journalctl -u sportsbug -n 15 --no-pager"
fi

if (( follow )); then
  echo "==> following the log (Ctrl-C to stop watching; the panel keeps running)"
  ssh -t "$PI" "journalctl -u sportsbug -f"
fi

echo "done -- http://sportsbug.local:8080"
