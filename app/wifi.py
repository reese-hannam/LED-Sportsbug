"""Wi-Fi status, scanning, joining, and the fallback hotspot.

The panel is meant to be carried somewhere and plugged in -- a tailgate, a
friend's house -- where the network is one it has never seen. So when it can't
get online it becomes an access point of its own, says so on the panel, and
serves the same control center on that network. You join it from a phone, pick
the real network, and it swaps over.

Everything goes through `nmcli`. Raspberry Pi OS manages Wi-Fi with
NetworkManager, which already knows how to run a DHCP-serving hotspot on one
radio and how to store credentials across reboots -- reimplementing that with
hostapd and dnsmasq would be a lot of moving parts to get the same result.

The single radio in a Pi Zero 2 cannot be a client and an access point at once,
and -- the part that broke joining twice -- it cannot SCAN while it's an access
point either. So the hotspot is strictly a fallback, and a join has to:

  1. drop the hotspot,
  2. RESCAN until the chosen network actually shows up (right after an access
     point comes down the radio's list is empty, and `nmcli device wifi
     connect` fails with "No network with SSID found" -- exactly the error the
     panel kept hitting, looking from a phone like Join did nothing),
  3. connect, and wait for an address,
  4. on failure, bring the hotspot back and RECORD WHY, so the panel and the
     setup page can both say it. The phone doing the joining has been kicked
     off the hotspot by then; the only way it learns anything is from what's
     waiting when it gets back.

While a join is running, the watcher stands aside: seeing "offline and no
hotspot", it would otherwise raise the hotspot in the middle of the join.

Off a Pi -- a laptop, or any machine without nmcli -- every call reports
"unsupported" rather than failing, so the app runs normally in development.
"""

import shutil
import subprocess
import threading
import time

HOTSPOT_NAME = "sportsbug-setup"
HOTSPOT_SSID = "SPORTSBUG"
HOTSPOT_PASSWORD = "scoreboard"
# NetworkManager hands out 10.42.0.1 to itself for a shared connection.
HOTSPOT_URL = "http://10.42.0.1:8080"

# Long enough not to trip over a slow DHCP lease or a reboot with the router
# still coming up, short enough that you aren't left staring at a dark panel.
GRACE_SECONDS = 45

# After the hotspot drops, how long to keep rescanning for the chosen network.
SCAN_WAIT_SECONDS = 20
# After associating, how long to wait for the network to hand out an address.
ONLINE_WAIT_SECONDS = 20
# How long the outcome of a join stays on the panel and the setup page. Long
# enough to get back onto the hotspot from a phone and read it.
JOIN_REPORT_SECONDS = 600
# Saved networks outrank anything else NetworkManager might autoconnect to --
# in particular the setup hotspot, which must never win a boot.
KNOWN_PRIORITY = "10"

_TIMEOUT = 25

_joining = threading.Event()
_join_lock = threading.Lock()
_last_join: dict | None = None
# The last real scan. An access point can't scan, so while the hotspot is up
# this is what the setup page offers -- captured just before it came up.
_remembered: list = []


def available() -> bool:
    return shutil.which("nmcli") is not None


def _run(args, timeout=_TIMEOUT):
    """(ok, output). Never raises -- a network tool failing is a normal
    condition here, not an exception. Arguments go straight to the process,
    never through a shell, so a password with `$` or quotes arrives intact."""
    if not available():
        return False, "nmcli not installed"
    try:
        done = subprocess.run(["nmcli"] + args, capture_output=True, text=True,
                              timeout=timeout)
        return done.returncode == 0, (done.stdout or done.stderr).strip()
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"{type(e).__name__}: {e}"


def _fields(args, count):
    """Parse nmcli's terse output, honouring its backslash escaping."""
    ok, out = _run(args)
    if not ok:
        return []
    rows = []
    for line in out.splitlines():
        parts, cur, escaped = [], "", False
        for ch in line:
            if escaped:
                cur += ch
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == ":" and len(parts) < count - 1:
                parts.append(cur)
                cur = ""
            else:
                cur += ch
        parts.append(cur)
        if len(parts) == count:
            rows.append(parts)
    return rows


def _wifi_device() -> str:
    for dev, typ in _fields(["-t", "-f", "DEVICE,TYPE", "device"], 2):
        if typ == "wifi":
            return dev
    return "wlan0"


def last_join() -> dict | None:
    """The outcome of the most recent join, while it's still worth showing."""
    j = _last_join
    if j and time.time() - j["at"] < JOIN_REPORT_SECONDS:
        return dict(j)
    return None


def _record(ssid: str, ok: bool, detail: str) -> None:
    global _last_join
    _last_join = {"ssid": ssid, "ok": ok, "detail": detail, "at": time.time()}


def status() -> dict:
    """What the panel and the control center both ask.

    `online` means associated with a network AND holding an address -- being
    associated but stuck without DHCP is exactly the state that should still
    raise the setup hotspot. `joining` and `last_join` ride along on every
    report, so the watcher's ten-second refresh can't wipe a failure reason
    before anyone has seen it.
    """
    if not available():
        return {"supported": False, "online": False, "hotspot": False,
                "ssid": None, "ip": None, "joining": False, "last_join": None}

    hotspot = False
    ssid = None
    for name, typ, _dev in _fields(["-t", "-f", "NAME,TYPE,DEVICE", "connection", "show",
                                    "--active"], 3):
        if name == HOTSPOT_NAME:
            hotspot = True
        elif typ == "802-11-wireless":
            ssid = name

    ip = None
    for dev, _typ, state, addr in _fields(
            ["-t", "-f", "DEVICE,TYPE,STATE,IP4-ADDRESS", "device", "status"], 4) or []:
        if dev.startswith("wl") and state == "connected" and addr:
            ip = addr.split("/")[0]
    if ip is None:
        # IP4-ADDRESS isn't offered by every nmcli build; ask the device directly.
        for line in _fields(["-t", "-f", "IP4.ADDRESS", "device", "show"], 2):
            if line[1]:
                ip = line[1].split("/")[0]
                break

    return {"supported": True,
            "online": bool(ssid and ip) and not hotspot,
            "hotspot": hotspot,
            "ssid": ssid,
            "ip": ip,
            "joining": _joining.is_set(),
            "last_join": last_join()}


def _list_networks(rescan: bool) -> list[dict]:
    """Networks the radio can see, strongest first, one entry per SSID."""
    args = ["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
    if rescan:
        args += ["--rescan", "yes"]
    seen: dict = {}
    for ssid, signal, security in _fields(args, 3):
        if not ssid or ssid == HOTSPOT_SSID:
            continue
        try:
            strength = int(signal)
        except ValueError:
            strength = 0
        current = seen.get(ssid)
        if current is None or strength > current["signal"]:
            seen[ssid] = {"ssid": ssid, "signal": strength,
                          "secured": bool(security and security != "--")}
    return sorted(seen.values(), key=lambda n: -n["signal"])


def scan() -> list[dict]:
    """Nearby networks for the setup page.

    While the hotspot is up the radio can't scan, and what NetworkManager still
    lists from before ages out within minutes -- so the page would slowly empty
    until there was nothing left to tap. It falls back to the scan taken just
    before the hotspot came up.
    """
    global _remembered
    nets = _list_networks(rescan=True)
    if nets:
        _remembered = nets
        return nets
    return list(_remembered) if status()["hotspot"] else nets


def known() -> list[str]:
    """Networks already saved, so the UI can show what it will rejoin."""
    return [name for name, typ in _fields(["-t", "-f", "NAME,TYPE", "connection", "show"], 2)
            if typ == "802-11-wireless" and name != HOTSPOT_NAME]


def _profiles_for(ssid: str) -> list[str]:
    """Saved profiles for this SSID, matched on the SSID itself, not the name.

    Profiles are named however they were made -- "Home WiFi", "Home WiFi 1",
    "netplan-wlan0-Home WiFi" from the OS installer -- so the name alone can't
    be trusted to find them.
    """
    out = []
    for name in known():
        ok, got = _run(["-g", "802-11-wireless.ssid", "connection", "show", name])
        if ok and got.strip() == ssid:
            out.append(name)
    return out


def _await_network(ssid: str, timeout: float = SCAN_WAIT_SECONDS) -> bool:
    """Rescan until `ssid` is visible, or give up."""
    deadline = time.time() + timeout
    while True:
        if any(n["ssid"] == ssid for n in _list_networks(rescan=True)):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(2)


def _wait_online(timeout: float = ONLINE_WAIT_SECONDS) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if status()["online"]:
            return True
        time.sleep(1)
    return status()["online"]


def connect(ssid: str, password: str | None = None) -> tuple[bool, str]:
    """Join a network. Returns (ok, a sentence a person can act on).

    One join at a time, and the watcher stands aside while it runs. The outcome
    is recorded either way -- see last_join().
    """
    with _join_lock:
        _joining.set()
        try:
            ok, detail = _connect(ssid, password)
        finally:
            _joining.clear()
        _record(ssid, ok, detail)
        return ok, detail


def _connect(ssid: str, password: str | None) -> tuple[bool, str]:
    dev = _wifi_device()
    stop_hotspot()

    if not _await_network(ssid):
        start_hotspot()
        return False, f"Couldn't see {ssid} from here — is it in range?"

    existing = _profiles_for(ssid)
    if password:
        # Fresh credentials REPLACE whatever was saved. An old profile with a
        # stale password would otherwise be tried first, fail, and look exactly
        # like the new password being wrong.
        for name in existing:
            _run(["connection", "delete", "id", name])
        existing = []

    if existing:
        ok, out = _run(["connection", "up", "id", existing[0]], timeout=45)
    else:
        args = ["device", "wifi", "connect", ssid, "ifname", dev]
        if password:
            args += ["password", password]
        ok, out = _run(args, timeout=45)

    if ok and _wait_online():
        name = status()["ssid"] or ssid
        # Remembered, and preferred over anything else on every boot from now.
        _run(["connection", "modify", "id", name, "connection.autoconnect", "yes",
              "connection.autoconnect-priority", KNOWN_PRIORITY])
        return True, f"Joined {ssid}."

    # It didn't work. A profile saved with a wrong password would be retried on
    # every boot and fail every time, so a failed fresh attempt leaves nothing.
    if password:
        for name in _profiles_for(ssid):
            _run(["connection", "delete", "id", name])
    start_hotspot()

    lowered = (out or "").lower()
    if ok:
        return False, f"Joined {ssid} but it never gave the panel an address."
    if any(w in lowered for w in ("secrets", "password", "802-1x", "psk", "authentication")):
        return False, f"The password for {ssid} was rejected."
    if "no network" in lowered or "not found" in lowered:
        return False, f"Couldn't see {ssid} from here — is it in range?"
    return False, f"Couldn't join {ssid}: {out.splitlines()[-1] if out else 'no reason given'}"


def forget(ssid: str) -> bool:
    return _run(["connection", "delete", ssid])[0]


def start_hotspot() -> tuple[bool, str]:
    global _remembered
    if not available():
        return False, "nmcli not installed"
    if status()["hotspot"]:
        return True, "already up"
    # Look around BEFORE becoming an access point. Once it is one the radio
    # can't scan, and the setup page needs something to offer.
    nets = _list_networks(rescan=True)
    if nets:
        _remembered = nets
    ok, out = _run(["device", "wifi", "hotspot", "con-name", HOTSPOT_NAME,
                    "ssid", HOTSPOT_SSID, "password", HOTSPOT_PASSWORD], timeout=45)
    if ok:
        # Stop it coming back BY ITSELF on the next boot. nmcli saves the
        # hotspot as an ordinary profile, and profiles autoconnect by default,
        # so a panel that raised it once would boot into it forever after.
        _run(["connection", "modify", HOTSPOT_NAME, "connection.autoconnect", "no"])
    return ok, out


def stop_hotspot() -> bool:
    if not status()["hotspot"]:
        return True
    return _run(["connection", "down", HOTSPOT_NAME])[0]


def watch(state, stop_event, grace=GRACE_SECONDS, interval=10.0):
    """Keep `state.network` current, and raise the hotspot when adrift.

    Runs in its own thread. The grace period matters on a cold boot: the radio
    associates several seconds after the app starts, and flipping straight to
    an access point would stop it ever finding the network it already knows.
    """
    if not available():
        state.set_network(status())
        return

    # Repair a hotspot profile saved before autoconnect was switched off.
    # Harmless when the profile doesn't exist.
    _run(["connection", "modify", HOTSPOT_NAME, "connection.autoconnect", "no"])

    # Woke up ON the hotspot with a real network saved: that's the old
    # boot-time autoconnect, not a deliberate rescue. Let NM try the real one.
    if status()["hotspot"] and known():
        stop_hotspot()

    offline_since = None
    while not stop_event.is_set():
        current = status()
        # `known` rides along so the control center can render the whole Wi-Fi
        # panel from the state snapshot it already polls.
        state.set_network({**current, "known": known()})

        if _joining.is_set():
            # A join owns the radio. It has dropped the hotspot on purpose, and
            # raising it again now -- "offline, no hotspot, past the grace" --
            # would abort the join halfway. The join restores it on failure.
            offline_since = None
        elif current["online"]:
            offline_since = None
        elif not current["hotspot"]:
            now = time.time()
            offline_since = offline_since or now
            if now - offline_since >= grace:
                start_hotspot()
                state.set_network({**status(), "known": known()})
                offline_since = None

        # Faster while a join is running, so the page can show its progress.
        stop_event.wait(2.0 if _joining.is_set() else interval)
