#!/usr/bin/env python3
"""Watch the Q6T HDMI movie path. Owner: Christopher Patrick Urias. MIT License.

Stdout is ACTION_REQUIRED / FAILED only.

Details go to watch_hdmi_cut.log next to this script.
Catches: HDMI UNPLUGGED/NOTPRESENT, default stolen off EP-HDMI-RX,
WASAPI 0x88890004, loopback IndexError, FxSound/Boom respawn, live_dj death.
On a steal, pins HDMI immediately.
"""
from __future__ import annotations

import json
import sys
import time
import warnings
from pathlib import Path

DIR = Path(__file__).resolve().parent
LOG = DIR / "watch_hdmi_cut.log"
STATUS = Path(r"C:\Users\c\q6t-ctrl\live_dj.status.json")
DJLOG = Path(r"C:\Users\c\q6t-ctrl\live_dj.log")
PIDFILE = Path(r"C:\Users\c\q6t-ctrl\live_dj.pid")
HDMI_NEEDLE = "EP-HDMI-RX"
HDMI_ID = "{0.0.0.00000000}.{a36adb8f-88ee-4616-92d7-034feef0a581}"
POLL = 0.5

warnings.filterwarnings("ignore")
# Monitor wakes on every stderr line. Park pycaw COM noise in the log.
_err = open(LOG, "a", encoding="utf-8")
sys.stderr = _err

try:
    import pythoncom

    pythoncom.CoInitialize()
except Exception:
    pass


def dbg(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def hdmi_state() -> tuple[str, str]:
    """Return (default_name, hdmi_state). Unknown is ('', 'UNKNOWN'), never a fake steal."""
    try:
        from pycaw.pycaw import AudioUtilities

        sp = AudioUtilities.GetSpeakers()
        default = sp.FriendlyName or ""
        st = "MISSING"
        try:
            enum = AudioUtilities.GetDeviceEnumerator()
            dev = enum.GetDevice(HDMI_ID)
            # eState: 1=ACTIVE 2=DISABLED 4=NOTPRESENT 8=UNPLUGGED
            raw = int(dev.GetState())
            st = {1: "Active", 2: "Disabled", 4: "NotPresent", 8: "Unplugged"}.get(
                raw, str(raw)
            )
        except Exception:
            st = "MISSING"
        return default, st
    except Exception as e:
        dbg(f"hdmi_state unknown: {e}")
        return "", "UNKNOWN"


def status_snap() -> dict:
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def pid_alive() -> bool:
    try:
        pid = int(PIDFILE.read_text(encoding="ascii").strip())
    except Exception:
        return False
    try:
        import ctypes

        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def thieves() -> list[str]:
    found = []
    try:
        import subprocess

        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq FxSound.exe", "/FI", "IMAGENAME eq Boom3D.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        # tasklist ANDs multiple /FI — check each
    except Exception:
        r = None
    for name in ("FxSound.exe", "Boom3D.exe"):
        try:
            import subprocess

            r = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if name.lower() in (r.stdout or "").lower() and "No tasks" not in (r.stdout or ""):
                found.append(name)
        except Exception:
            pass
    return found


def gpu_reset() -> str:
    """Win+Ctrl+Shift+B — WDDM reset re-handshakes NVIDIA HDMI audio when the
    endpoint is phantom/NotPresent but the monitor is still attached."""
    import ctypes

    user32 = ctypes.windll.user32
    VK_LWIN, VK_LCONTROL, VK_LSHIFT, VK_B = 0x5B, 0xA2, 0xA0, 0x42
    KEYUP = 0x0002
    for vk in (VK_LWIN, VK_LCONTROL, VK_LSHIFT, VK_B):
        user32.keybd_event(vk, 0, 0, 0)
    for vk in (VK_B, VK_LSHIFT, VK_LCONTROL, VK_LWIN):
        user32.keybd_event(vk, 0, KEYUP, 0)
    dbg("WDDM reset Win+Ctrl+Shift+B")
    return "wddm-reset"


def catch_pin() -> str:
    """Pin in a child process so this watcher never imports live_dj/soundcard COM."""
    import subprocess

    try:
        r = subprocess.run(
            [
                sys.executable,
                "-c",
                "from live_dj import pin_hdmi_default, _kill_audio_thieves\n"
                "pin_hdmi_default(force=True)\n"
                "_kill_audio_thieves()\n"
                "pin_hdmi_default(force=True)\n"
                "print('ok')\n",
            ],
            cwd=r"C:\Users\c\q6t-ctrl",
            capture_output=True,
            text=True,
            timeout=8,
        )
        if r.returncode == 0:
            return "pinned"
        err = (r.stderr or r.stdout or "")[-160:].replace("\n", " ")
        return f"pin-fail {err}"
    except Exception as e:
        return f"pin-fail {e}"


def new_log_hits(offset: int) -> tuple[int, list[str]]:
    hits = []
    try:
        data = DJLOG.read_bytes()
    except OSError:
        return offset, hits
    n = len(data)
    if offset > n:
        offset = 0
    chunk = data[offset:].decode("utf-8", errors="replace")
    keys = (
        "0x88890004",
        "capture drop",
        "jumped off HDMI",
        "no device with id",
        "HDMI loopback missing",
        "pinned default HDMI",
        "killed ",
        "DEVICE_INVALIDATED",
    )
    for line in chunk.splitlines():
        if any(k in line for k in keys):
            hits.append(line.strip()[:180])
    return n, hits


def main() -> int:
    dbg("WATCH START")
    default, state = hdmi_state()
    st = status_snap()
    log_off = 0
    try:
        log_off = DJLOG.stat().st_size
    except OSError:
        pass
    incident = False
    last_fire = 0.0
    did_wddm = False
    healthy_note = (
        f"start default={default!r} hdmi_state={state} "
        f"rms={st.get('rms')} peak={st.get('peak')} pid={pid_alive()}"
    )
    dbg(healthy_note)

    while True:
        time.sleep(POLL)
        default, state = hdmi_state()
        st = status_snap()
        alive = pid_alive()
        th = thieves()
        log_off, hits = new_log_hits(log_off)
        rms = float(st.get("rms") or 0.0)
        peak = float(st.get("peak") or 0.0)
        reasons = []
        if not alive:
            reasons.append("live_dj_dead")
        # Empty / UNKNOWN is a COM miss, not a steal. Need a real other device name.
        if default and HDMI_NEEDLE not in default:
            reasons.append(f"default={default}")
        st_up = state.upper()
        if st_up in ("UNPLUGGED", "NOTPRESENT", "DISABLED", "MISSING"):
            reasons.append(f"hdmi_state={state}")
        if th:
            reasons.append("thieves=" + ",".join(th))
        hard_log = [
            h
            for h in hits
            if any(
                k in h
                for k in (
                    "0x88890004",
                    "capture drop",
                    "jumped off HDMI",
                    "no device with id",
                    "HDMI loopback missing",
                )
            )
        ]
        if hard_log:
            reasons.append("log=" + hard_log[-1][:80])

        if reasons:
            now = time.time()
            st_up = state.upper()
            gone = st_up in ("UNPLUGGED", "NOTPRESENT", "DISABLED", "MISSING")
            if gone and (not did_wddm):
                did_wddm = True
                gpu_reset()
            if (not incident) or (now - last_fire > 20.0):
                incident = True
                last_fire = now
                pin_msg = ""
                if (not gone) and ((HDMI_NEEDLE not in default) or th):
                    pin_msg = catch_pin()
                if gone and did_wddm:
                    pin_msg = (pin_msg + " wddm-reset").strip()
                snap = (
                    f"CUT {' | '.join(reasons)} rms={rms:.4f} peak={peak:.3f} "
                    f"state={state} default={default} {pin_msg}"
                )
                dbg(snap)
                if not alive:
                    print(f"FAILED: {snap}", flush=True)
                    return 1
                print(f"ACTION_REQUIRED: {snap}", flush=True)
        else:
            if incident:
                dbg(
                    f"RECOVERED default={default!r} state={state} "
                    f"rms={rms:.4f} peak={peak:.3f}"
                )
                print(
                    f"ACTION_REQUIRED: RECOVERED HDMI {state} default={default} "
                    f"rms={rms:.4f} peak={peak:.3f}",
                    flush=True,
                )
            incident = False
            did_wddm = False


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        dbg("WATCH STOP")
        print("CANCELLED: watcher interrupted", flush=True)
        raise SystemExit(0)
