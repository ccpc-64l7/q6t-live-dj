#!/usr/bin/env python3
"""Append one compact health line to diag.log every 10s. Stop: live_dj.stop-diag or diag.stop.

Owner: Christopher Patrick Urias. MIT License.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent
LOG = DIR / "diag.log"
STOP = DIR / "diag.stop"
STATUS = DIR / "live_dj.status.json"
ERR = DIR / "live_dj.err.log"
PEACE = Path(r"C:\Program Files\EqualizerAPO\config\peace.txt")
PIDFILE = DIR / "live_dj.pid"

HDMI = "EP-HDMI-RX"


def _tail_err() -> str:
    if not ERR.exists() or not ERR.stat().st_size:
        return "-"
    try:
        tail = ERR.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        for line in reversed(tail):
            if line.startswith("---") or not line.strip():
                continue
            return line[:90]
    except OSError:
        return "err-read"
    return "-"


def _line() -> str:
    import psutil

    t = time.strftime("%Y-%m-%d %H:%M:%S")
    vm = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=0.4)
    py = []
    audio_cpu = 0.0
    for p in psutil.process_iter(["name", "pid", "memory_info", "cpu_percent"]):
        n = (p.info.get("name") or "").lower()
        if n in ("pythonw.exe", "python.exe", "audiodg.exe"):
            rss = (p.info["memory_info"].rss / (1024 * 1024)) if p.info.get("memory_info") else 0
            c = p.info.get("cpu_percent") or 0.0
            py.append(f"{n}:{p.info['pid']}={rss:.0f}MB/{c:.0f}%")
            if n == "audiodg.exe":
                audio_cpu = c
    defn = "?"
    try:
        from pycaw.pycaw import AudioUtilities

        defn = AudioUtilities.GetSpeakers().FriendlyName
    except Exception as e:
        defn = f"err:{e}"
    hdmi = "HDMI" if HDMI in defn else "NOT-HDMI"
    st = "no-status"
    if STATUS.exists():
        try:
            j = json.loads(STATUS.read_text(encoding="utf-8"))
            st = (
                f"peak={j.get('peak', 0):.2f} rms={j.get('rms', 0):.3f} "
                f"sub={j.get('preamp')} bar={j.get('bar_preamp')} "
                f"subvol={j.get('sub_level', 0)} "
                f"err={str(j.get('error') or '-')[:40]}"
            )
        except Exception:
            st = "status-bad"
    peace_age = "-"
    if PEACE.exists():
        peace_age = f"{time.time() - PEACE.stat().st_mtime:.0f}s"
    pid = "-"
    if PIDFILE.exists():
        try:
            pid = PIDFILE.read_text(encoding="ascii").strip()
        except OSError:
            pass
    disk = ""
    try:
        dio = psutil.disk_io_counters()
        if dio:
            disk = f"disk_w={dio.write_bytes // (1024 * 1024)}MB"
    except Exception:
        pass
    return (
        f"{t} cpu={cpu:.0f}% ram={vm.percent:.0f}% free={vm.available/1e9:.1f}GB "
        f"out={hdmi} ({defn}) pid={pid} peace_age={peace_age} audiodg={audio_cpu:.0f}% "
        f"{disk} {' '.join(py)} {st} lasterr={_tail_err()}\n"
    )


def main() -> None:
    if STOP.exists():
        try:
            STOP.unlink()
        except OSError:
            pass
    LOG.open("a", encoding="utf-8").write(
        time.strftime("%Y-%m-%d %H:%M:%S ") + "diag_watch START\n"
    )
    while not STOP.exists():
        try:
            LOG.open("a", encoding="utf-8").write(_line())
        except Exception as e:
            try:
                LOG.open("a", encoding="utf-8").write(
                    time.strftime("%Y-%m-%d %H:%M:%S ") + f"diag_watch {e}\n"
                )
            except OSError:
                pass
        for _ in range(30):
            if STOP.exists():
                break
            time.sleep(1)
    LOG.open("a", encoding="utf-8").write(
        time.strftime("%Y-%m-%d %H:%M:%S ") + "diag_watch STOP\n"
    )


if __name__ == "__main__":
    main()
