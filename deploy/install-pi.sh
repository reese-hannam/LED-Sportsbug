#!/usr/bin/env bash
# Set up a Raspberry Pi to drive the panel.
#
#   sudo bash deploy/install-pi.sh
#
# Run it once. If you lose the connection, or the terminal wedges, or you just
# want to see how far it got, run the SAME command again -- it reattaches to
# the running install, or picks up from the last finished stage.
#
# Three things make this survivable on a 512MB Pi Zero, all learned the hard
# way on one:
#
#   IT DETACHES.  The work runs under setsid, so it is not a child of your
#   shell. Closing the terminal, dropping Wi-Fi, or killing a frozen SSH
#   session no longer destroys twenty minutes of compiling.
#
#   IT MAKES SWAP FIRST.  Compiling the display bindings needs far more than
#   512MB. Without swap the kernel thrashes and the whole machine locks up --
#   not the build, the machine. That looks exactly like a hang, and it is
#   unrecoverable except by pulling the power.
#
#   IT RESUMES.  Every stage leaves a marker, so re-running skips what is
#   already done rather than starting the slowest step over.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MATRIX_SRC="${MATRIX_SRC:-/opt/rpi-rgb-led-matrix}"
LOG=/var/log/sportsbug-install.log
STATE=/var/lib/sportsbug/install
PIDFILE=/run/sportsbug-install.pid
SWAPFILE=/swapfile-sportsbug

say()  { printf '\n\033[1m== %s\033[0m\n' "$1"; }
info() { printf '   %s\n' "$1"; }
warn() { printf '\033[33m!! %s\033[0m\n' "$1"; }
die()  { printf '\033[31m!! %s\033[0m\n' "$1" >&2; exit 1; }

done_with() { [[ -f "$STATE/$1" ]]; }
mark()      { mkdir -p "$STATE"; touch "$STATE/$1"; }

[[ $EUID -eq 0 ]] || die "Run with sudo: sudo bash deploy/install-pi.sh"

# --- attach, or detach ----------------------------------------------------
# Already running? Don't start a second one -- just follow along. tail exits
# by itself when the install finishes, so this doubles as "is it done yet?".
if [[ -f $PIDFILE ]] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
  RUNNING_PID="$(cat "$PIDFILE")"
  echo "Install already running (pid $RUNNING_PID). Following the log."
  echo "Ctrl-C just stops watching -- the install keeps going."
  echo
  exec tail -n 40 -f --pid="$RUNNING_PID" "$LOG"
fi

# First invocation: relaunch detached and watch the log.
if [[ "${SPORTSBUG_DETACHED:-}" != "1" ]]; then
  mkdir -p "$(dirname "$LOG")" "$STATE"
  : > "$LOG"
  rm -f "$PIDFILE"
  # setsid puts the work in its own session, so a dying terminal can't take it
  # with it. nohup alone is nearly as good if setsid is somehow absent.
  if command -v setsid >/dev/null 2>&1; then
    SPORTSBUG_DETACHED=1 setsid nohup bash "$0" "$@" >>"$LOG" 2>&1 &
  else
    SPORTSBUG_DETACHED=1 nohup bash "$0" "$@" >>"$LOG" 2>&1 &
  fi
  # Waits for the CHILD to write its own pid rather than trusting $!. With
  # setsid, $! is setsid's pid and setsid exits the moment it has forked --
  # so the pid file would hold a corpse, every re-run would think nothing was
  # running, and you'd get two installers fighting over apt and the venv.
  for _ in $(seq 40); do [[ -s "$PIDFILE" ]] && break; sleep 0.25; done
  CHILD="$(cat "$PIDFILE" 2>/dev/null || true)"
  [[ -n "$CHILD" ]] || die "the installer failed to start; see $LOG"
  echo "Installing in the background (pid $CHILD)."
  echo "Safe to lose the connection -- reconnect and run this again to check."
  echo
  exec tail -n +1 -f --pid="$CHILD" "$LOG"
fi

# Deliberately no trap removing this on exit. A finished re-run races itself
# otherwise: every stage is already marked, so the child writes its pid, does
# nothing, and deletes the file again before the parent's poll notices it --
# and the parent then reports a failure to start that never happened. The
# attach check above proves liveness with kill -0, so a stale pid is harmless,
# and /run is cleared on every boot anyway.
echo $$ > "$PIDFILE"
echo "sportsbug install -- $(date)"

# --- 0. sanity ------------------------------------------------------------
say "Checking the OS"
info "python3 is $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || die \
  "Needs Python 3.10+. Raspberry Pi OS Bookworm ships 3.11; Bullseye's 3.9 is too old."
info "$(free -m | awk '/^Mem:/{print $2" MB RAM"}')"

# Power, before anything slow. A Pi browning out under load doesn't fail
# cleanly: it resets mid-write, which is how the last SD card in this project
# died. Worth thirty seconds here to avoid losing a 25-minute compile -- and
# the panel makes it far worse later, since lighting 4096 LEDs is a much
# bigger current step than compiling.
power_report() {
  command -v vcgencmd >/dev/null 2>&1 || { info "vcgencmd not available -- skipping the power check"; return 0; }
  local t bits
  t="$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)" || return 0
  bits=$(( t ))
  if (( bits == 0 )); then
    info "power is clean (throttled=$t)"
    return 0
  fi
  # Written as `if` blocks, not `(( ... )) && warn`: under `set -e` an AND-list
  # whose test is false exits 1 and takes the whole install down with it, so
  # the clean-power case would abort the run it was meant to protect.
  #
  # bit 0 = under-voltage NOW, bit 16 = it has happened since boot.
  local said=0
  if (( bits & 0x1 ));     then warn "UNDER-VOLTAGE RIGHT NOW (throttled=$t) -- the 5V supply is sagging"; said=1; fi
  if (( bits & 0x10000 )); then warn "under-voltage has occurred since boot (throttled=$t)"; said=1; fi
  if (( bits & 0x2 ));     then warn "ARM frequency capped (throttled=$t)"; said=1; fi
  if (( bits & 0x4 ));     then warn "currently throttled (throttled=$t)"; said=1; fi
  if (( bits & 0x8 ));     then warn "soft temperature limit active (throttled=$t)"; said=1; fi
  if (( bits & 0x20000 )); then warn "ARM frequency has been capped since boot (throttled=$t)"; said=1; fi
  if (( bits & 0x40000 )); then warn "throttling has occurred since boot (throttled=$t)"; said=1; fi
  if (( bits & 0x80000 )); then warn "soft temperature limit has been hit since boot (throttled=$t)"; said=1; fi
  # Never go quiet on a non-zero value: an unrecognised bit still means the
  # hardware is unhappy, and silence here reads as "all good".
  if (( said == 0 )); then warn "unexpected throttle flags (throttled=$t)"; fi
  if (( bits & 0x1 )); then
    warn "Give the Pi its OWN 5V/2.5A supply on PWR IN rather than sharing the"
    warn "panel's. A bigger shared supply does not fix this -- it is voltage"
    warn "sag on a current step, not a lack of capacity."
  fi
  return 0
}
say "Checking power"
power_report

say "Checking the network"
if ping -c1 -W3 deb.debian.org >/dev/null 2>&1 || ping -c1 -W3 1.1.1.1 >/dev/null 2>&1; then
  info "online"
else
  die "No internet. Join a network first: sudo nmtui   (then run this again)"
fi

# --- 1. swap --------------------------------------------------------------
# Before anything else, because the compile below is what needs it.
if ! done_with swap; then
  say "Making swap for the compiler"
  RAM_MB="$(free -m | awk '/^Mem:/{print $2}')"
  if (( RAM_MB >= 1800 )); then
    info "${RAM_MB}MB RAM is plenty -- skipping"
  elif swapon --show=NAME --noheadings | grep -q "$SWAPFILE"; then
    info "already active"
  else
    info "only ${RAM_MB}MB RAM -- adding 2GB of swap so the build can't lock the Pi up"
    fallocate -l 2G "$SWAPFILE" 2>/dev/null || dd if=/dev/zero of="$SWAPFILE" bs=1M count=2048 status=none
    chmod 600 "$SWAPFILE"
    mkswap "$SWAPFILE" >/dev/null
    swapon "$SWAPFILE"
    grep -q "$SWAPFILE" /etc/fstab || echo "$SWAPFILE none swap sw 0 0" >> /etc/fstab
    info "$(free -m | awk '/^Swap:/{print $2" MB swap active"}')"
  fi
  mark swap
fi

# --- 2. packages ----------------------------------------------------------
if ! done_with apt; then
  say "Installing apt packages"
  apt-get update -qq
  # cmake/ninja: upstream builds the Python bindings with scikit-build-core now.
  # avahi-daemon: makes sportsbug.local resolve from a phone on any network.
  apt-get install -y --no-install-recommends \
    git build-essential python3-dev python3-venv python3-pip cython3 \
    cmake ninja-build avahi-daemon network-manager
  mark apt
fi

# --- 3. name and discovery ------------------------------------------------
if ! done_with hostname; then
  say "Setting the hostname to 'sportsbug'"
  if [[ "$(hostnamectl --static 2>/dev/null || hostname)" != "sportsbug" ]]; then
    hostnamectl set-hostname sportsbug 2>/dev/null || echo sportsbug > /etc/hostname
    # 127.0.1.1 must track the hostname or sudo warns on every command.
    if grep -q '^127.0.1.1' /etc/hosts; then
      sed -i "s/^127.0.1.1.*/127.0.1.1\tsportsbug/" /etc/hosts
    else
      printf '127.0.1.1\tsportsbug\n' >> /etc/hosts
    fi
    info "renamed -> http://sportsbug.local:8080"
  else
    info "already sportsbug"
  fi
  systemctl enable --now avahi-daemon >/dev/null 2>&1 || true
  systemctl is-enabled NetworkManager >/dev/null 2>&1 || systemctl enable --now NetworkManager || true
  mark hostname
fi

# --- 4. the app's venv ----------------------------------------------------
VENV_PY="$REPO_DIR/.venv/bin/python"
if ! done_with venv; then
  say "Creating the virtualenv"
  # --system-site-packages is NOT for rgbmatrix any more -- stage 5 pip-installs
  # that straight into the venv. It stays because it lets the venv see a
  # system-wide rgbmatrix from an older `sudo make install-python`, which makes
  # the import check in stage 5 pass and skips a 25-minute rebuild.
  [[ -x "$VENV_PY" ]] || python3 -m venv --system-site-packages "$REPO_DIR/.venv"
  info "installing the app's dependencies -- a few minutes, progress below"
  "$VENV_PY" -m pip install --upgrade pip setuptools wheel
  "$VENV_PY" -m pip install -r "$REPO_DIR/requirements.txt"
  info "ready: $("$VENV_PY" --version)"
  mark venv
fi

# --- 4b. team logos -------------------------------------------------------
# Not in the repo -- they're the leagues' artwork, not this project's to
# redistribute -- so they're downloaded once here and pre-shrunk to panel
# size. Under a minute; after this the panel never needs the network for them.
if ! done_with logos; then
  say "Downloading team logos"
  if "$VENV_PY" "$REPO_DIR/tools/fetch_logos.py"; then
    mark logos
  else
    warn "logo download failed -- the panel falls back to team colours. Re-run this installer to retry."
  fi
fi

# --- 5. the display bindings ---------------------------------------------
# The slow one: 10-25 minutes on a Zero, and the reason for the swap above.
if ! done_with bindings; then
  say "Building the display bindings"
  if [[ -d "$MATRIX_SRC/.git" ]]; then
    git -C "$MATRIX_SRC" pull --ff-only >/dev/null 2>&1 || warn "using the existing checkout"
  else
    git clone --depth 1 https://github.com/hzeller/rpi-rgb-led-matrix "$MATRIX_SRC"
  fi

  if "$VENV_PY" -c 'import rgbmatrix' 2>/dev/null; then
    info "already installed"
  else
    # Built from the REPO ROOT, where the pyproject.toml is. Upstream moved
    # this to scikit-build-core and cmake; the `make build-python` inside
    # bindings/python that every older guide describes no longer exists.
    [[ -f "$MATRIX_SRC/pyproject.toml" ]] || die \
      "No pyproject.toml in $MATRIX_SRC -- upstream layout changed again."

    # Their CMakeLists compiles bindings/python/rgbmatrix/shims/pillow.c
    # unconditionally, and that file does #include "Imaging.h" -- a Pillow C
    # header they never put on the include path. It exists ONLY in Pillow's
    # source tarball; no wheel carries it, so having Pillow installed doesn't
    # help. Nothing here uses the shim (we never call SetImage) but it isn't
    # optional in their build, so it has to compile: fetch the header and
    # point the C compiler at it.
    PIL_INC=""
    if grep -q 'shims/pillow\.c' "$MATRIX_SRC/CMakeLists.txt" 2>/dev/null; then
      PIL_SRC=/opt/pillow-src
      if ! find "$PIL_SRC" -name Imaging.h -print -quit 2>/dev/null | grep -q .; then
        info "fetching Pillow's C headers (their build needs Imaging.h)"
        mkdir -p "$PIL_SRC"
        PIL_URL="$("$VENV_PY" -c "import json,urllib.request;d=json.load(urllib.request.urlopen('https://pypi.org/pypi/pillow/json',timeout=30));print(next(f['url'] for f in d['urls'] if f['packagetype']=='sdist'))")"
        curl -fsSL "$PIL_URL" -o /tmp/pillow-src.tar.gz
        tar -xzf /tmp/pillow-src.tar.gz -C "$PIL_SRC" --strip-components=1
        rm -f /tmp/pillow-src.tar.gz
      fi
      PIL_INC="$(dirname "$(find "$PIL_SRC" -name Imaging.h -print -quit)")"
      [[ -n "$PIL_INC" ]] || die "couldn't find Imaging.h in Pillow's source"
      info "using headers from $PIL_INC"
    fi

    # ONE compiler job at a time. Each g++ on the generated Cython source can
    # take hundreds of megabytes; running several at once is what turns a slow
    # build into a locked-up machine, swap or no swap.
    export CMAKE_BUILD_PARALLEL_LEVEL=1
    export MAKEFLAGS=-j1
    export CFLAGS="${CFLAGS:-} ${PIL_INC:+-I$PIL_INC}"
    info "compiling with cmake, one job at a time -- 10-25 minutes, be patient"
    info "(this is the slow bit; the log will look quiet while it works)"
    "$VENV_PY" -m pip install --no-cache-dir "$MATRIX_SRC"
  fi

  "$VENV_PY" -c 'import rgbmatrix' 2>/dev/null || die \
    "The bindings did not install. Send me the log: $LOG"
  info "the venv can drive the panel"
  mark bindings
fi

# --- 6. free the PWM ------------------------------------------------------
if ! done_with sound; then
  say "Freeing the PWM (disabling onboard sound)"
  BLACKLIST=/etc/modprobe.d/blacklist-rgb-matrix.conf
  if [[ ! -f "$BLACKLIST" ]]; then
    echo "blacklist snd_bcm2835" > "$BLACKLIST"
    update-initramfs -u >/dev/null 2>&1 || true
    info "sound disabled (takes effect after the reboot)"
  else
    info "already done"
  fi
  for CONFIG in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f "$CONFIG" ]] || continue
    sed -i 's/^\s*dtparam=audio=on/dtparam=audio=off/' "$CONFIG"
    # Belt and braces: a config.txt without the line at all (some images ship
    # it commented) still needs audio explicitly off, or the library refuses
    # to start and the panel never lights.
    grep -q '^dtparam=audio=off' "$CONFIG" || echo 'dtparam=audio=off' >> "$CONFIG"
    break
  done
  mark sound
fi

# --- 6b. give the panel a core -------------------------------------------
# Pairs with CPUAffinity=3 in the service file. Without isolcpus the kernel
# schedules other work on core 3 too, and the panel visibly stutters whenever
# the poller or web server does anything.
if ! done_with isolcpus; then
  say "Reserving a CPU core for the panel"
  for CMDLINE in /boot/firmware/cmdline.txt /boot/cmdline.txt; do
    [[ -f "$CMDLINE" ]] || continue
    if grep -q 'isolcpus=' "$CMDLINE"; then
      info "already set"
    else
      # MUST stay a single line -- a stray newline in cmdline.txt stops the Pi
      # booting at all, so this appends in place rather than echoing a new one.
      sed -i 's/[[:space:]]*$//' "$CMDLINE"
      sed -i '1s/$/ isolcpus=3/' "$CMDLINE"
      info "added isolcpus=3 (takes effect after the reboot)"
    fi
    LINES="$(wc -l < "$CMDLINE")"
    (( LINES <= 1 )) || warn "$CMDLINE has $LINES lines -- it must be ONE. Check it before rebooting."
    break
  done
  mark isolcpus
fi

# --- 7. verify ------------------------------------------------------------
say "Self-check"
"$VENV_PY" "$REPO_DIR/tools/selfcheck.py" --quick || warn "self-check reported problems"

# --- 8. start on boot -----------------------------------------------------
say "Installing the panel settings"
# Never overwritten: this is where someone's hand-tuned slowdown or a swapped
# adapter board lives, and clobbering it on every re-run would silently undo
# their fix. Re-running the installer is meant to be safe.
if [[ -f /etc/default/sportsbug ]]; then
  info "/etc/default/sportsbug already exists -- leaving it alone"
else
  install -m 644 "$REPO_DIR/deploy/sportsbug.env" /etc/default/sportsbug
  info "wrote /etc/default/sportsbug (panel type, mapping, slowdown)"
fi

say "Installing the boot service"
sed "s#^WorkingDirectory=.*#WorkingDirectory=$REPO_DIR#; \
     s#^ExecStart=.*#ExecStart=$REPO_DIR/.venv/bin/python -m app.main#" \
  "$REPO_DIR/deploy/sportsbug.service" > /etc/systemd/system/sportsbug.service
systemctl daemon-reload
systemctl enable sportsbug >/dev/null
info "enabled -- it starts itself on every power-up"
mark service

say "Power, one more time"
power_report

cat <<EOF

$(printf '\033[1m== Done. Two commands left\033[0m')

    sudo reboot

  The reboot is REQUIRED, not tidiness: disabling the onboard sound and
  reserving a CPU core both only take effect on a fresh boot, and the panel
  will not start while the sound module still holds the PWM hardware.

  Then prove the panel actually lights up:

    sudo $REPO_DIR/.venv/bin/python $REPO_DIR/tools/paneltest.py

  You should see red, green, blue, white, a clean border, then OK. That is
  the only check that proves anything is lit -- the service can report
  perfect health while the panel stays black.

  After that the panel runs on its own, every power-up, no login needed.
  From your phone on the same Wi-Fi:   http://sportsbug.local:8080

  If it can't find a network it makes its own: join SPORTSBUG (password
  scoreboard) and open http://10.42.0.1:8080

  Useful later:
    journalctl -u sportsbug -f          what it's doing
    sudo systemctl restart sportsbug    restart it
    sudo nano /etc/default/sportsbug    panel type, mapping, slowdown
    vcgencmd get_throttled              0x0 means the 5V supply is clean
EOF
