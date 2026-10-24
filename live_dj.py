#!/usr/bin/env python3
"""Live DJ auto-EQ — HDMI loopback → Equalizer APO, with an on-screen mixer.

Owner: Christopher Patrick Urias <bigcityrooster@gmail.com>.
Free and open source under the MIT License.

python live_dj.py        # mixer window (default)
python live_dj.py run    # headless
python live_dj.py stop
"""
from __future__ import annotations

__author__ = "Christopher Patrick Urias"
__email__ = "bigcityrooster@gmail.com"
__license__ = "MIT"
__version__ = "1.0.0"

import argparse
import ctypes
import json
import os
import queue
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Must land before numpy so BLAS can use every core.
_CORES = os.cpu_count() or 4
os.environ.setdefault("OMP_NUM_THREADS", str(_CORES))
os.environ.setdefault("MKL_NUM_THREADS", str(_CORES))
os.environ.setdefault("OPENBLAS_NUM_THREADS", str(_CORES))
os.environ.setdefault("NUMEXPR_NUM_THREADS", str(_CORES))

import comtypes  # before soundcard; comtypes 1.4 sets the COM apartment at import
import numpy as np
import soundcard as sc

_POOL: ThreadPoolExecutor | None = None


def cpu_pool() -> ThreadPoolExecutor:
    global _POOL
    if _POOL is None:
        workers = max(4, _CORES)
        _POOL = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dj")
    return _POOL

DIR = Path(__file__).resolve().parent
APO_CFG = Path(r"C:\Program Files\EqualizerAPO\config")
APO_READY = False
APO_RAM = APO_CFG / "ram"
PEACE = APO_CFG / "peace.txt"
PEACE_SEED = DIR / "presets" / "peace-ram-seed.txt"
STAMP = APO_CFG / "live.stamp"
STATUS = DIR / "live_dj.status.json"
STOP = DIR / "live_dj.stop"
BOARD = DIR / "live_dj.board.json"
PREFS = DIR / "live_dj.prefs.json"
PIDFILE = DIR / "live_dj.pid"
LOG = DIR / "live_dj.log"
ERRLOG = DIR / "live_dj.err.log"
_LOG_LOCK = threading.Lock()
_ERR_MAX = 512 * 1024
_ERR_INSTALLED = False
_ERR_LAST: dict[str, float] = {}
_PEACE_ON_RAM = False
_LAST_PEACE = ""
_LAST_STAMP = 0.0

# Windows cache hint: keep this file's pages in RAM; don't flush to NAND
# unless the standby list is under pressure. APO still sees a normal path.
_FILE_ATTRIBUTE_TEMPORARY = 0x00000100
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_kernel32 = ctypes.windll.kernel32
_kernel32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
_kernel32.SetFileAttributesW.restype = ctypes.c_int
_kernel32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
_kernel32.GetFileAttributesW.restype = ctypes.c_uint32


def _keep_in_ram(path: Path) -> None:
    try:
        _kernel32.SetFileAttributesW(str(path), _FILE_ATTRIBUTE_TEMPORARY)
    except OSError:
        pass


def _is_ram_mount(path: Path) -> bool:
    try:
        attrs = _kernel32.GetFileAttributesW(str(path))
    except OSError:
        return False
    if attrs == _INVALID_FILE_ATTRIBUTES:
        return False
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def attach_peace() -> None:
    """Prefer ImDisk folder-mount at config\\ram (true RAM). Else NVMe + TEMPORARY."""
    global PEACE, _PEACE_ON_RAM, APO_READY
    if not APO_CFG.is_dir():
        APO_READY = False
        log("Equalizer APO is not installed. Levels only. Playback device is left alone.")
        return
    APO_READY = True
    ram_ok = False
    if _is_ram_mount(APO_RAM):
        try:
            probe = APO_RAM / "peace.txt"
            probe.write_text("# probe\n", encoding="ascii")
            ram_ok = True
        except OSError:
            ram_ok = False
    if ram_ok:
        PEACE = APO_RAM / "peace.txt"
        _PEACE_ON_RAM = True
        cfg = APO_CFG / "config.txt"
        want = "Include: ram\\peace.txt\n"
        try:
            if not cfg.exists() or cfg.read_text(encoding="ascii", errors="replace") != want:
                cfg.write_text(want, encoding="ascii")
        except OSError as e:
            log_error("config.txt ram include", e)
        log(f"peace RAM {PEACE}")
        return
    PEACE = APO_CFG / "peace.txt"
    _PEACE_ON_RAM = False
    cfg = APO_CFG / "config.txt"
    want = "Include: peace.txt\n"
    try:
        cfg.write_text(want, encoding="ascii")
    except OSError as e:
        log_error("config.txt nvme include", e)
    log(f"peace NVMe {PEACE} (TEMPORARY)")


def _poke_apo() -> None:
    """APO watches config\\, not a subfolder. 1-byte stamp, coalesced."""
    global _LAST_STAMP
    if not _PEACE_ON_RAM:
        return
    now = time.perf_counter()
    if now - _LAST_STAMP < 0.2:
        return
    _LAST_STAMP = now
    try:
        STAMP.write_bytes(b".")
        _keep_in_ram(STAMP)
    except OSError:
        pass


def save_peace_seed() -> None:
    """One durable NVMe copy on exit — live writes stay on RAM/cache."""
    try:
        if not PEACE.exists():
            return
        PEACE_SEED.parent.mkdir(parents=True, exist_ok=True)
        PEACE_SEED.write_text(PEACE.read_text(encoding="ascii"), encoding="ascii")
    except OSError:
        pass

HDMI = "EP-HDMI-RX"
# FiberLake AMD HDMI bar. Nitro's old id was {a36adb8f-88ee-4616-92d7-034feef0a581}.
HDMI_ID = r"{0.0.0.00000000}.{dcb13164-03b7-41f4-bca4-5d27388cf70f}"
_PIN_LAST = 0.0
# pycaw and the WASAPI loopback open share one COM apartment path.
# Two threads in there at once has killed the mixer with no Python traceback.
_ENDPOINT_LOCK = threading.RLock()
SR = 48000
N = 8192


def _hdmi_device():
    with _ENDPOINT_LOCK:
        try:
            _co_init()
            from pycaw.pycaw import AudioUtilities
        except Exception:
            return None
        try:
            devices = AudioUtilities.GetAllDevices()
        except Exception:
            return None
        for d in devices:
            try:
                n = d.FriendlyName or ""
                if "EP-HDMI-RX" in n and "Active" in str(d.state):
                    return d
            except Exception:
                continue
        return None


def _co_init() -> None:
    try:
        import pythoncom

        pythoncom.CoInitialize()
    except Exception:
        pass


def default_is_hdmi() -> bool:
    """True if default render is the bar. COM blips are unknown, not a steal."""
    with _ENDPOINT_LOCK:
        try:
            _co_init()
            from pycaw.pycaw import AudioUtilities

            name = AudioUtilities.GetSpeakers().FriendlyName or ""
            if not name:
                return True
            return "EP-HDMI-RX" in name
        except Exception:
            return True


def pin_hdmi_default(*, force: bool = False) -> bool:
    """Keep Windows default on the bar. FxSound/Realtek steal it.

    Without Equalizer APO there is nothing to feed, so the Corsair headset
    and the other endpoints stay as Windows left them.
    """
    if not APO_READY:
        return default_is_hdmi()

    # force skips the 2.5s throttle. Never SetDefaultEndpoint if already HDMI.
    # Re-pinning an active endpoint tears WASAPI and the movie audio cuts.
    with _ENDPOINT_LOCK:
        return _pin_hdmi_locked(force)


def _pin_hdmi_locked(force: bool) -> bool:
    global _PIN_LAST
    now = time.time()
    if not force and now - _PIN_LAST < 2.5:
        return default_is_hdmi()
    _PIN_LAST = now
    try:
        _co_init()
        try:
            from comtypes import (
                CLSCTX_ALL,
                COMMETHOD,
                GUID,
                HRESULT,
                IUnknown,
                CoCreateInstance,
            )
        except OSError:
            from comtypes import (
                CLSCTX_ALL,
                COMMETHOD,
                GUID,
                HRESULT,
                IUnknown,
                CoCreateInstance,
            )
        from ctypes import POINTER, c_int, c_longlong, c_void_p, c_wchar_p
        from pycaw.pycaw import AudioUtilities

        class IPolicyConfig(IUnknown):
            _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
            _methods_ = [
                COMMETHOD(
                    [],
                    HRESULT,
                    "GetMixFormat",
                    (["in"], c_wchar_p, "wszDeviceId"),
                    (["out"], POINTER(c_void_p), "ppFormat"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "GetDeviceFormat",
                    (["in"], c_wchar_p, "wszDeviceId"),
                    (["in"], c_int, "bDefault"),
                    (["out"], POINTER(c_void_p), "ppFormat"),
                ),
                COMMETHOD(
                    [], HRESULT, "ResetDeviceFormat", (["in"], c_wchar_p, "wszDeviceId")
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "SetDeviceFormat",
                    (["in"], c_wchar_p, "wszDeviceId"),
                    (["in"], c_void_p, "pEndpointFormat"),
                    (["in"], c_void_p, "pMixFormat"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "GetProcessingPeriod",
                    (["in"], c_wchar_p, "wszDeviceId"),
                    (["in"], c_int, "bDefault"),
                    (["out"], POINTER(c_longlong), "pmftDefaultPeriod"),
                    (["out"], POINTER(c_longlong), "pmftMinimumPeriod"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "SetProcessingPeriod",
                    (["in"], c_wchar_p, "wszDeviceId"),
                    (["in"], POINTER(c_longlong), "pmftPeriod"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "GetShareMode",
                    (["in"], c_wchar_p, "wszDeviceId"),
                    (["out"], POINTER(c_void_p), "pMode"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "SetShareMode",
                    (["in"], c_wchar_p, "wszDeviceId"),
                    (["in"], c_void_p, "mode"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "GetPropertyValue",
                    (["in"], c_wchar_p, "wszDeviceId"),
                    (["in"], c_void_p, "key"),
                    (["out"], POINTER(c_void_p), "pv"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "SetPropertyValue",
                    (["in"], c_wchar_p, "wszDeviceId"),
                    (["in"], c_void_p, "key"),
                    (["in"], c_void_p, "pv"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "SetDefaultEndpoint",
                    (["in"], c_wchar_p, "wszDeviceId"),
                    (["in"], c_int, "role"),
                ),
                COMMETHOD(
                    [],
                    HRESULT,
                    "SetEndpointVisibility",
                    (["in"], c_wchar_p, "wszDeviceId"),
                    (["in"], c_int, "bVisible"),
                ),
            ]

        hdmi = _hdmi_device()
        if hdmi is None:
            return False
        if default_is_hdmi():
            try:
                ev = hdmi.EndpointVolume
                if ev.GetMute():
                    ev.SetMute(0, None)
            except Exception:
                pass
            return True
        pc = CoCreateInstance(
            GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"),
            IPolicyConfig,
            CLSCTX_ALL,
        )
        for role in (0, 1, 2):
            pc.SetDefaultEndpoint(hdmi.id, role)
        try:
            ev = hdmi.EndpointVolume
            ev.SetMute(0, None)
        except Exception:
            pass
        log(f"pinned default HDMI  {hdmi.FriendlyName}")
        return True
    except Exception as e:
        log_error("pin HDMI default", e, throttle_s=15.0)
        return False


def hdmi_out_percent() -> float | None:
    """Windows volume of the HDMI soundbar, 0-100. None if the bar is gone."""
    with _ENDPOINT_LOCK:
        try:
            _co_init()
            dev = _hdmi_device()
            if dev is None:
                return None
            return round(float(dev.EndpointVolume.GetMasterVolumeLevelScalar()) * 100.0, 1)
        except Exception as e:
            log_error("hdmi out read", e, throttle_s=10.0)
            return None


def set_hdmi_out_percent(pct: float) -> None:
    """Soundbar Windows volume. Independent of SUB VOL and BAR VOL."""
    with _ENDPOINT_LOCK:
        try:
            _co_init()
            dev = _hdmi_device()
            if dev is None:
                return
            ev = dev.EndpointVolume
            ev.SetMasterVolumeLevelScalar(float(np.clip(pct, 0.0, 100.0)) / 100.0, None)
            if ev.GetMute():
                ev.SetMute(0, None)
        except Exception as e:
            log_error("hdmi out set", e, throttle_s=5.0)


def hdmi_loopback():
    """Match the bar by GUID first, then the EP-HDMI-RX name."""
    with _ENDPOINT_LOCK:
        try:
            return sc.get_microphone(HDMI_ID, include_loopback=True)
        except Exception:
            return sc.get_microphone(HDMI, include_loopback=True)


def _kill_audio_thieves() -> None:
    """FxSound / Boom3D steal the Windows default device off HDMI.

    Skipped until Equalizer APO is installed.
    """
    if not APO_READY:
        return

    # pythonw has no console. A bare tasklist/taskkill makes Windows Terminal
    # the default host and flashes a window over the movie.
    import subprocess

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for name in ("FxSound.exe", "Boom3D.exe"):
        try:
            listed = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                capture_output=True,
                text=True,
                timeout=2,
                creationflags=flags,
            )
            if name.lower() not in (listed.stdout or "").lower():
                continue
            r = subprocess.run(
                ["taskkill", "/IM", name, "/F", "/T"],
                capture_output=True,
                text=True,
                timeout=2,
                creationflags=flags,
            )
            if r.returncode == 0:
                log(f"killed {name} (was stealing default audio)")
        except Exception as e:
            log_error(f"kill {name}", e, throttle_s=30.0)


def bar_link_state() -> str:
    """ok, stolen (bar is up but not the default), or gone (HDMI endpoint missing)."""
    if _hdmi_device() is None:
        return "gone"
    if default_is_hdmi():
        return "ok"
    return "stolen"


_TOAST_PS = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
function Esc([string]$s) {
  if (-not $s) { return '' }
  return (($s -replace '&','&amp;') -replace '<','&lt;') -replace '>','&gt;'
}
$raw = "<toast scenario=`"urgent`"><visual><binding template=`"ToastGeneric`"><text>$(Esc $env:Q6T_TOAST_TITLE)</text><text>$(Esc $env:Q6T_TOAST_BODY)</text></binding></visual></toast>"
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($raw)
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Microsoft.Windows.Explorer').Show($toast)
"""

_BAR_COPY = {
    "ok": (
        "Sound bar connected",
        "HDMI audio is on the Samsung bar.",
        "DJ Lab — Q6T",
        "Sound bar connected",
    ),
    "stolen": (
        "Sound bar lost the audio",
        "Windows moved playback off the Samsung bar. Putting it back.",
        "SOUND BAR NOT DEFAULT — DJ Lab",
        "Sound bar is not the playback device",
    ),
    "gone": (
        "Sound bar unplugged",
        "The Samsung bar's HDMI audio is gone. Movie sound is on the PC speakers.",
        "SOUND BAR UNPLUGGED — DJ Lab",
        "SOUND BAR UNPLUGGED",
    ),
}


def notify_bar(link: str) -> None:
    """Toast without a console window. Windows Terminal is the default host."""
    title, body, _win, _tip = _BAR_COPY.get(link, _BAR_COPY["gone"])

    def run() -> None:
        import subprocess

        try:
            env = os.environ.copy()
            env["Q6T_TOAST_TITLE"] = title
            env["Q6T_TOAST_BODY"] = body
            subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-WindowStyle",
                    "Hidden",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    _TOAST_PS,
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            log_error("bar toast", e, throttle_s=30.0)

    threading.Thread(target=run, name="dj-toast", daemon=True).start()


class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("hWnd", ctypes.c_void_p),
        ("uID", ctypes.c_uint),
        ("uFlags", ctypes.c_uint),
        ("uCallbackMessage", ctypes.c_uint),
        ("hIcon", ctypes.c_void_p),
        ("szTip", ctypes.c_wchar * 128),
        ("dwState", ctypes.c_uint),
        ("dwStateMask", ctypes.c_uint),
        ("szInfo", ctypes.c_wchar * 256),
        ("uTimeoutOrVersion", ctypes.c_uint),
        ("szInfoTitle", ctypes.c_wchar * 64),
        ("dwInfoFlags", ctypes.c_uint),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", ctypes.c_void_p),
    ]


class BarTray:
    """Notification-area icon. Stays up for the life of the mixer."""

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        self._shell32 = ctypes.windll.shell32
        self._user32.LoadIconW.restype = ctypes.c_void_p
        self._user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._shell32.Shell_NotifyIconW.argtypes = [
            ctypes.c_uint,
            ctypes.POINTER(_NOTIFYICONDATAW),
        ]
        self._shell32.Shell_NotifyIconW.restype = ctypes.c_int
        self._hwnd = 0
        self._added = False

    def attach(self, hwnd: int) -> None:
        self._hwnd = int(hwnd)

    def update(self, link: str) -> None:
        if not self._hwnd:
            return
        title, _body, _win, tip = _BAR_COPY.get(link, _BAR_COPY["gone"])
        icon = {"ok": 32516, "stolen": 32515}.get(link, 32513)
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = 2 | 4  # NIF_ICON | NIF_TIP
        nid.hIcon = self._user32.LoadIconW(None, icon)
        nid.szTip = tip[:127]
        nid.szInfoTitle = title[:63]
        cmd = 1 if self._added else 0  # NIM_MODIFY / NIM_ADD
        ok = self._shell32.Shell_NotifyIconW(cmd, ctypes.byref(nid))
        if ok:
            self._added = True
        elif not self._added:
            log_error("tray icon", OSError("Shell_NotifyIcon add failed"), throttle_s=60.0)

    def close(self) -> None:
        if not (self._added and self._hwnd):
            return
        nid = _NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        self._shell32.Shell_NotifyIconW(2, ctypes.byref(nid))
        self._added = False


def flash_taskbar(hwnd: int, *, stop: bool) -> None:
    class FLASHWINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint),
            ("hwnd", ctypes.c_void_p),
            ("dwFlags", ctypes.c_uint),
            ("uCount", ctypes.c_uint),
            ("dwTimeout", ctypes.c_uint),
        ]

    info = FLASHWINFO()
    info.cbSize = ctypes.sizeof(FLASHWINFO)
    info.hwnd = int(hwnd)
    # Flash the taskbar button until the mixer is focused. Stop clears it.
    info.dwFlags = 0 if stop else (2 | 0xC)
    info.uCount = 0 if stop else 8
    info.dwTimeout = 0
    ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))


BANDS = np.array([31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000], dtype=np.float64)
LABELS = ["31", "63", "125", "250", "500", "1k", "2k", "4k", "8k", "16k"]

# Samsung HW-Q60T / Q6CT (Q6T-Series) — programmed from published specs
# + RTINGS: dark default profile, weak sub-58 Hz thump, HDMI IN = stereo PCM.
# HW-Q60T/ZA as installed here. No SWA-8500S rear kit, so there is no
# back-left or back-right channel to feed. HDMI from this PC is stereo
# PCM; the bar upmixes that to L / C / R plus the two Acoustic Beam drivers.
BAR = {
    "model": "Samsung HW-Q60T",
    "name": "Q6T-Series",
    "channels": "L C R + Acoustic Beam, no rear kit",
    "power_w": 360,
    "bar_w": 200,  # 6 front drivers × 30 W + 2 Acoustic Beam × 10 W
    "front_drivers": "6 × 30 W: L/R 85×40 mm woofers, 39 mm tweeters, center",
    "front_ohm": 6.0,
    "beam_w": 10,  # each up-firing Acoustic Beam driver
    "sub_w": 160,
    "sub_model": "wireless",
    "sub_driver_in": 6.5,
    "sub_ohm": 3.0,
    "sub_align": "bass-reflex, side-firing, rear port",
    "sub_f3_hz": 58,  # in-room; marketing says 42 Hz
    "sub_fb_hz": 47,  # port tune — useful deep, not 31 Hz
    "high_ext_hz": 19900,
    "max_spl_db": 92.8,
    "drivers": 8,  # in the bar. The sub is separate. Rears are not installed.
    "rears": "none",  # SWA-8500S is optional and not on this system
    "hdmi_in": "PCM 2.0 @ 48 kHz (this PC) — bar upmixes",
    "hdmi_formats": "Dolby Digital 5.1, DTS 5.1, stereo PCM (no Atmos / no eARC)",
    "voicing": "dark — vocals on the center, bass on the 6.5 in, beam for width",
    "crossover_hz": 120,  # bar/sub handoff
    "width_default": 1.00,  # wider than this hollows the center
}
HEADROOM_DB = 1.5  # true-peak pad; 3 dB was burying the mix at −12
PEAK_ALLOW = 1.145  # 114.50% — ride through 100, dump only here
PEAK_PULL = 1.10  # start easing before the ceiling

# Nearfield-studio target on the Q6T: flatter than club, still no 20 Hz rumble.
TARGET_RAW = np.array(
    [
        -5.0,  # 31.5  below sub F3
        0.3,  # 63    tight, not thumpy
        0.8,  # 125   controlled punch
        -1.8,  # 250   desk/bar box
        -0.4,  # 500
        0.2,  # 1k    monitors / center
        0.9,  # 2k    presence, not hype
        1.2,  # 4k    open the dark top
        1.0,  # 8k
        0.2,  # 16k
    ]
)
RING_WANT_GB = 6.0
STUDIO_SECONDS = 2.5
STUDIO_WIN = 16384
_MID = slice(4, 7)
TARGET = TARGET_RAW - TARGET_RAW[_MID].mean()
DEADBAND = 0.35  # don't twitch the faders under this (dB)

def _bias(arr: list[float]) -> np.ndarray:
    a = np.array(arr, dtype=np.float64)
    return a - a[_MID].mean()


# Extra target on top of Q6T studio curve, picked by content class.
CLASS_BIAS = {
    "quiet": _bias([0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    "mix": _bias([0, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
    "voice": _bias([0, -1.0, -0.6, -1.0, 0.2, 0.7, 1.0, 0.3, 0.0, -0.5]),
    "vocal": _bias([0, -0.6, -0.4, -0.8, 0.2, 0.5, 0.7, 0.2, 0.0, -0.3]),
    # already bass-heavy: tighten mud, do NOT add more 63/125
    "bass": _bias([0, -0.4, -0.2, -1.6, -0.8, 0.2, 0.4, 0.5, 0.3, 0.0]),
    "thin": _bias([0, 0.8, 1.0, -0.6, -0.2, 0.0, 0.2, 0.4, 0.3, 0.0]),
    "bright": _bias([0, 0.0, 0.0, -0.3, 0.0, 0.0, -0.4, -1.1, -1.5, -1.8]),
    "dark": _bias([0, 0.2, 0.3, -0.5, 0.0, 0.2, 0.5, 0.8, 0.9, 0.2]),
}


def stereo_corr(x: np.ndarray) -> float:
    if x.ndim < 2 or x.shape[1] < 2:
        return 1.0
    l = x[:, 0] - float(x[:, 0].mean())
    r = x[:, 1] - float(x[:, 1].mean())
    d = float(np.sqrt(np.mean(l * l) * np.mean(r * r))) + 1e-12
    return float(np.clip(np.mean(l * r) / d, -1.0, 1.0))


def classify(shape: np.ndarray, rms: float, peak: float, corr: float) -> str:
    if rms < 0.015:
        return "quiet"
    crest = peak / (rms + 1e-12)
    bass = float(shape[1:3].mean())
    voice = float(shape[5:7].mean())
    air = float(shape[7:10].mean())
    mud = float(shape[3])
    if voice > 2.0 and bass < 1.2 and crest > 3.4:
        return "voice"
    if corr > 0.91 and voice > 1.0 and bass < 2.0:
        return "vocal"
    if bass > 3.2:
        return "bass"
    if bass < -2.0 and air < 1.0:
        return "thin"
    if air > 3.0:
        return "bright"
    if air < -3.2:
        return "dark"
    return "mix"


MAX_GAIN = 2.5  # auto curve only — user trims sit on top, then preamp pays
MAX_LOW = 0.0  # never boost 31.5 Hz (sub F3 ~58 Hz)
# Wireless sub is slower than the bar — longer tau on low bands
# Sub is wireless — bass a bit slower. Mids/highs snap so skips don't lag.
TAU = np.array([4.0, 3.2, 2.4, 1.5, 1.1, 0.95, 0.80, 0.70, 0.65, 0.75])
SILENCE = 1e-4
WRITE_EPS = 0.12
WIN = np.hanning(N)
FREQS = np.fft.rfftfreq(N, 1.0 / SR)
BAND_SLICES = []
for _fc in BANDS:
    _lo, _hi = _fc / np.sqrt(2), _fc * np.sqrt(2)
    BAND_SLICES.append((FREQS >= _lo) & (FREQS < _hi))


def _append_log(path: Path, text: str) -> None:
    if not text.endswith("\n"):
        text += "\n"
    with _LOG_LOCK:
        try:
            if path.exists() and path.stat().st_size > _ERR_MAX:
                tail = path.read_bytes()[-262144:]
                path.write_bytes(b"...truncated...\n" + tail)
            with path.open("a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass


def log(msg: str) -> None:
    line = (
        time.strftime("%Y-%m-%d %H:%M:%S ")
        + f"[{threading.current_thread().name}] {msg}"
    )
    _append_log(LOG, line)


def log_error(
    msg: str,
    exc: BaseException | None = None,
    *,
    throttle_s: float = 0.0,
) -> None:
    """Always hits live_dj.err.log (pythonw has no console)."""
    if throttle_s > 0:
        key = msg[:80]
        now = time.time()
        with _LOG_LOCK:
            last = _ERR_LAST.get(key, 0.0)
            if now - last < throttle_s:
                return
            _ERR_LAST[key] = now
    ts = time.strftime("%Y-%m-%d %H:%M:%S ")
    thr = threading.current_thread().name
    block = f"{ts}[{thr}] ERROR {msg}\n"
    tb = ""
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    elif sys.exc_info()[0] is not None:
        tb = traceback.format_exc()
    if tb:
        block += tb
        if not block.endswith("\n"):
            block += "\n"
        block += "---\n"
    _append_log(ERRLOG, block)
    _append_log(LOG, block.rstrip("\n"))


class _ErrStream:
    encoding = "utf-8"

    def write(self, s: str) -> int:
        if s and str(s).strip():
            t = str(s).strip()
            key = "stderr:" + t[:80]
            now = time.time()
            with _LOG_LOCK:
                last = _ERR_LAST.get(key, 0.0)
                if now - last < 10.0:
                    return len(s)
                _ERR_LAST[key] = now
            _append_log(
                ERRLOG,
                time.strftime("%Y-%m-%d %H:%M:%S [stderr] ") + t + "\n",
            )
        return len(s) if s else 0

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False


def install_error_logging() -> None:
    global _ERR_INSTALLED
    if _ERR_INSTALLED:
        return
    _ERR_INSTALLED = True

    def _hook(exc_type, exc, tb) -> None:
        log_error(
            f"uncaught {exc_type.__name__}: {exc}",
            exc if isinstance(exc, BaseException) else None,
        )
        if tb is not None and exc is not None:
            _append_log(ERRLOG, "".join(traceback.format_exception(exc_type, exc, tb)))

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        log_error(
            f"thread {args.thread.name if args.thread else '?'} "
            f"{args.exc_type.__name__}: {args.exc_value}",
            args.exc_value if isinstance(args.exc_value, BaseException) else None,
        )

    sys.excepthook = _hook
    threading.excepthook = _thread_hook
    sys.stderr = _ErrStream()
    log(f"error log {ERRLOG}")


def _channel_mag(ch: np.ndarray) -> np.ndarray:
    spec = np.fft.rfft(ch * WIN) / (N * 0.5)
    return np.abs(spec)


def _mag_to_bands(mag: np.ndarray) -> np.ndarray:
    abs_db = np.empty(len(BANDS), dtype=np.float64)
    for i, mask in enumerate(BAND_SLICES):
        if not np.any(mask):
            m = float(mag[np.argmin(np.abs(FREQS - BANDS[i]))])
        else:
            m = float(np.sqrt(np.mean(mag[mask] ** 2)))
        abs_db[i] = 20.0 * np.log10(m + 1e-12)
    return abs_db


def analyze(x: np.ndarray, pool: ThreadPoolExecutor | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Shape vs mids + band dBFS. L/R FFTs run on two threads when pooled."""
    x = np.asarray(x, dtype=np.float64)
    pool = pool or cpu_pool()
    if x.ndim == 2 and x.shape[1] >= 2:
        fl = pool.submit(_channel_mag, x[:, 0])
        fr = pool.submit(_channel_mag, x[:, 1])
        mag = 0.5 * (fl.result() + fr.result())
    else:
        mag = _channel_mag(x.reshape(-1) if x.ndim > 1 else x)
    abs_db = _mag_to_bands(mag)
    return abs_db - abs_db[_MID].mean(), abs_db


def alloc_ring(want_gb: float = RING_WANT_GB) -> tuple[np.ndarray, float]:
    """Reserve a stereo float32 ring. Try 6 GB, then step down."""
    frame = 8  # 2 ch * float32
    for gb in (want_gb, 4.0, 2.0, 1.0, 0.5):
        n = int(gb * 1024**3) // frame
        n = max(N * 8, (n // N) * N)
        try:
            buf = np.empty((n, 2), dtype=np.float32)
            log(f"ring {buf.nbytes / 1024**3:.2f} GB  {n} frames")
            return buf, buf.nbytes / 1024**3
        except MemoryError:
            log(f"ring {gb} GB MemoryError")
    n = SR * 60
    buf = np.empty((n, 2), dtype=np.float32)
    log(f"ring fallback {buf.nbytes / 1024**3:.2f} GB")
    return buf, buf.nbytes / 1024**3


def welch_shape(x: np.ndarray, pool: ThreadPoolExecutor) -> np.ndarray:
    """Long-window averaged spectrum from the ring (studio ears)."""
    if x.shape[0] < STUDIO_WIN:
        s, _ = analyze(x[:N] if x.shape[0] >= N else np.pad(x, ((0, N - x.shape[0]), (0, 0))), pool)
        return s
    hop = STUDIO_WIN // 2
    win = np.hanning(STUDIO_WIN)
    acc = None
    nacc = 0
    i = 0
    while i + STUDIO_WIN <= x.shape[0]:
        sl = x[i : i + STUDIO_WIN]
        fl = pool.submit(_channel_mag_n, sl[:, 0], win, STUDIO_WIN)
        fr = pool.submit(_channel_mag_n, sl[:, 1], win, STUDIO_WIN)
        mag = 0.5 * (fl.result() + fr.result())
        acc = mag if acc is None else acc + mag
        nacc += 1
        i += hop
        if nacc >= 12:
            break
    mag = acc / max(nacc, 1)
    freqs = np.fft.rfftfreq(STUDIO_WIN, 1.0 / SR)
    abs_db = np.empty(len(BANDS), dtype=np.float64)
    for bi, fc in enumerate(BANDS):
        lo, hi = fc / np.sqrt(2), fc * np.sqrt(2)
        mask = (freqs >= lo) & (freqs < hi)
        if not np.any(mask):
            m = float(mag[np.argmin(np.abs(freqs - fc))])
        else:
            m = float(np.sqrt(np.mean(mag[mask] ** 2)))
        abs_db[bi] = 20.0 * np.log10(m + 1e-12)
    return abs_db - abs_db[_MID].mean()


def _channel_mag_n(ch: np.ndarray, win: np.ndarray, n: int) -> np.ndarray:
    spec = np.fft.rfft(ch.astype(np.float64) * win) / (n * 0.5)
    return np.abs(spec)


def split_peaks(x: np.ndarray) -> tuple[float, float, float, float]:
    """Full, sub (<120), bar (>120), and vocal (>300 Hz) peaks.

    Vocal is the part of the bar that must stay up. Bass under 300 Hz,
    including the sub, is not allowed to count as a bar overload.
    """
    x = np.asarray(x, dtype=np.float64)
    peak = float(np.max(np.abs(x)))
    m = 0.5 * (x[:, 0] + x[:, 1]) if x.ndim == 2 and x.shape[1] >= 2 else x.reshape(-1)
    n = int(m.shape[0])
    spec = np.fft.rfft(m)
    f = np.fft.rfftfreq(n, 1.0 / SR)
    xo = float(BAR["crossover_hz"])
    low = np.fft.irfft(np.where(f <= xo, spec, 0.0), n=n)
    high = np.fft.irfft(np.where(f > xo, spec, 0.0), n=n)
    voice = np.fft.irfft(np.where(f >= 300.0, spec, 0.0), n=n)
    return (
        peak,
        float(np.max(np.abs(low))),
        float(np.max(np.abs(high))),
        float(np.max(np.abs(voice))),
    )


def write_config(
    eq: np.ndarray,
    preamp: float,
    width: float = 1.0,
    balance: float = 0.0,
    note: str = "",
    bass: float = 0.0,
    sub: float = 0.0,
    mid: float = 0.0,
    treble: float = 0.0,
    kills: tuple | list | None = None,
    vol_boost: float = 0.0,
    shake: bool = False,
    yard: bool = False,
    deep: bool = False,
    movie: bool = False,
    bar_preamp: float = 0.0,
    sub_xo: float = 120.0,
    sub_punch: float = 0.0,
    sub_body: float = 0.0,
    bar_voice: float = 0.0,
    bar_body: float = 0.0,
    bar_air: float = 0.0,
    master: float = 0.0,
) -> None:
    """Crossover split: sub path (≤120 Hz) vs bar path (vocals).

    808 / kick peaks pay on the sub. Bar drivers keep vocals up.
    AutoEq PK Q=1.41 + Mixxx isolator on the matching path.
    Extra sub/bar knobs default to a no-op so a saved mix stays put.
    """
    if not APO_READY:
        return
    w = float(np.clip(width, 0.0, 2.0))
    bal = float(np.clip(balance, -1.0, 1.0)) * 6.0
    kills = kills or (False, False, False)
    xo = float(np.clip(sub_xo, 80.0, 160.0))
    vb = float(np.clip(vol_boost, 0.0, 12.0))
    sub_pre = float(preamp)
    bar_pre = float(bar_preamp)
    if kills[0]:
        sub_pre = -40.0
    lines = [
        f"# AutoEq PK + Mixxx  split  {note}",
        "Preamp: 0.00 dB",
        "Copy: M=0.5*L+0.5*R S=0.5*L+-0.5*R",
        f"Copy: L=1.0*M+{w:.3f}*S R=1.0*M+{-w:.3f}*S",
        "Channel: L",
        f"Preamp: {bal:.2f} dB",
        "Channel: R",
        f"Preamp: {-bal:.2f} dB",
        "Copy: SL=L SR=R BL=L BR=R",
        "Channel: SL SR",
        f"Filter: ON LPQ Fc {xo:g} Hz Q 0.707",
        f"Filter: ON LPQ Fc {xo:g} Hz Q 0.707",
        f"Preamp: {sub_pre:.2f} dB",
        "Filter: ON LSC Fc 105 Hz Gain {:.2f} dB Q 0.70".format(
            0.0 if (shake or deep) else float(np.clip(eq[0], -12, 0))
        ),
    ]
    for fc, g in zip(BANDS[1:3], eq[1:3]):
        gg = float(g)
        if shake or deep:
            gg = min(gg, 0.0)
        lines.append(f"Filter: ON PK Fc {fc:g} Hz Gain {gg:.2f} dB Q 1.41")
    if deep:
        # Ported 6.5" PS-WR65BB: Fb ~47 Hz, unloads below ~38 Hz.
        # One deep peak at the port, one kick peak above — not stacked 63s.
        lines.extend(
            [
                "Filter: ON HPQ Fc 38 Hz Q 0.80",
                f"Filter: ON PK Fc 48 Hz Gain {float(np.clip(sub, 0, 8)) + 4.0:.2f} dB Q 1.35",
                f"Filter: ON PK Fc 72 Hz Gain {float(np.clip(bass, 0, 5)) + 1.5:.2f} dB Q 1.20",
            ]
        )
    elif shake:
        lines.extend(
            [
                "Filter: ON HPQ Fc 42 Hz Q 0.80",
                f"Filter: ON PK Fc 55 Hz Gain {float(np.clip(sub, 0, 6)) + 3.5:.2f} dB Q 1.60",
                f"Filter: ON PK Fc 75 Hz Gain {float(np.clip(bass, 0, 5)) + 2.5:.2f} dB Q 1.40",
            ]
        )
    else:
        lines.append(f"Filter: ON LSC Fc 250 Hz Gain {float(bass):.2f} dB Q 0.71")
        # 6.5" wireless sub speaks around 50 Hz. 63 Hz alone stays thin.
        lines.append(
            "Filter: ON PK Fc 50 Hz Gain {:.2f} dB Q 1.10".format(
                float(np.clip(sub, 0.0, 8.0))
            )
        )
        lines.append(f"Filter: ON PK Fc 63 Hz Gain {float(sub):.2f} dB Q 1.20")
    # PUNCH / BODY sit on the wireless sub only. 0 dB writes nothing.
    punch = float(np.clip(sub_punch, -8.0, 10.0))
    body = float(np.clip(sub_body, -8.0, 10.0))
    if abs(punch) >= 0.05:
        lines.append(f"Filter: ON PK Fc 52 Hz Gain {punch:.2f} dB Q 1.50")
    if abs(body) >= 0.05:
        lines.append(f"Filter: ON PK Fc 78 Hz Gain {body:.2f} dB Q 1.25")
    if vb > 0.15 and not shake and not deep:
        shelf = vb * (0.18 if yard else 0.28)
        lines.append(f"Filter: ON LSC Fc 80 Hz Gain {shelf:.2f} dB Q 0.70")
    lines.extend(
        [
            "Channel: BL BR",
            f"Filter: ON HPQ Fc {xo:g} Hz Q 0.707",
            f"Filter: ON HPQ Fc {xo:g} Hz Q 0.707",
            f"Preamp: {bar_pre:.2f} dB",
        ]
    )
    for fc, g in zip(BANDS[3:], eq[3:]):
        lines.append(f"Filter: ON PK Fc {fc:g} Hz Gain {float(g):.2f} dB Q 1.41")
    lines.append(
        "Filter: ON HSC Fc 10000 Hz Gain {:.2f} dB Q 0.70".format(float(eq[-1]))
    )
    if shake or deep:
        lines.append("Filter: ON PK Fc 250 Hz Gain -4.00 dB Q 1.10")
    lines.append(f"Filter: ON PK Fc 1000 Hz Gain {float(mid):.2f} dB Q 0.71")
    # VOICE 0 keeps the existing 2 dB presence (4 dB in MOVIE).
    voice = (4.0 if movie else 2.0) + float(np.clip(bar_voice, -8.0, 8.0))
    lines.append(
        "Filter: ON PK Fc 2200 Hz Gain {:.2f} dB Q 1.10".format(voice)
    )
    bar_body_g = float(np.clip(bar_body, -8.0, 8.0))
    if abs(bar_body_g) >= 0.05:
        lines.append(f"Filter: ON PK Fc 300 Hz Gain {bar_body_g:.2f} dB Q 0.90")
    bar_air_g = float(np.clip(bar_air, -8.0, 8.0))
    if abs(bar_air_g) >= 0.05:
        lines.append(f"Filter: ON HSC Fc 9000 Hz Gain {bar_air_g:.2f} dB Q 0.70")
    if movie:
        lines.append("Filter: ON PK Fc 1200 Hz Gain 2.50 dB Q 1.00")
        lines.append("Filter: ON PK Fc 3500 Hz Gain 1.80 dB Q 1.10")
    lines.append(f"Filter: ON HSC Fc 4000 Hz Gain {float(treble):.2f} dB Q 0.71")
    if vb > 0.15 and not shake:
        if yard:
            lines.append(f"Filter: ON PK Fc 2500 Hz Gain {vb * 0.24:.2f} dB Q 0.90")
            lines.append(f"Filter: ON HSC Fc 8000 Hz Gain {vb * 0.08:.2f} dB Q 0.70")
        else:
            lines.append(f"Filter: ON HSC Fc 8000 Hz Gain {vb * 0.20:.2f} dB Q 0.70")
    if kills[1]:
        lines.append("Filter: ON PK Fc 1000 Hz Gain -40.00 dB Q 0.71")
    if kills[2]:
        lines.append("Filter: ON HSC Fc 4000 Hz Gain -40.00 dB Q 0.71")
    lines.extend(
        [
            "Channel: all",
            "Copy: L=SL+BL R=SR+BR",
        ]
    )
    if "STEREO" not in note:
        lines.extend(
            [
                "Copy: LE=L RE=R",
                "Channel: LE RE",
                "Preamp: -18 dB",
                "Delay: 5.8 ms",
                "Channel: all",
                "Copy: L=L+LE R=R+RE",
            ]
        )
    # Whole-mix volume. 0 dB leaves the bar and sub where the other faders set them.
    master_db = float(np.clip(master, -24.0, 24.0))
    lines.extend(
        [
            "Channel: L R",
            f"Preamp: {master_db:.2f} dB",
        ]
    )
    text = "\n".join(lines) + "\n"
    global _LAST_PEACE
    if text == _LAST_PEACE:
        return
    last_err = None
    for _ in range(8):
        try:
            PEACE.write_text(text, encoding="ascii")
            _keep_in_ram(PEACE)
            _LAST_PEACE = text
            _poke_apo()
            return
        except OSError as e:
            last_err = e
            time.sleep(0.05)
    if last_err:
        log_error("peace.txt write failed", last_err)
        raise last_err


def write_eq(eq: np.ndarray, preamp: float) -> None:
    write_config(eq, preamp)


class Engine:
    def __init__(self) -> None:
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.eq = np.zeros(len(BANDS))
        self.meas = np.zeros(len(BANDS))
        self.abs_db = np.full(len(BANDS), -80.0)
        self.preamp = 0.0
        self.preamp_auto = True
        self.safe_gain = True
        self.vol_boost = 10.0
        self.shake = False
        self.yard = False
        self.deep = True  # 6.5" port-tune ~48 Hz
        self.push100 = False
        self.movie = True  # dialogue / FX on the bar — louder than music 100
        self.clip_trim = 0.0
        self.clip_trim_bar = 0.0
        self.bar_preamp = 2.0
        self.rms = 0.0
        self.peak = 0.0
        self.peak_sub = 0.0
        self.peak_bar = 0.0
        self.peak_ceil_sub = 114.5
        self.peak_ceil_bar = 114.5
        self.lock_sub = False
        self.lock_bar = False
        self.hdmi_vol = None  # Windows % of the HDMI bar; None until read
        self.track_flash = 0.0
        self.alive = False
        self.error = ""
        self.listen = "HDMI → bar (after EQ)"
        self.workers = max(4, _CORES)
        self.mode = "hold"  # auto | stereo | hold — never start LIVE DJ cold
        self.auto_warm = 0.0
        self.board_sync = 0
        self.ab = "A"
        self.width = float(BAR["width_default"])
        self.balance = 0.0
        self.kill = [False, False, False]  # lo mid hi
        self.now = None  # saved "how it sounds now"
        self.bass_trim = 2.0
        self.sub_trim = 5.0
        self.sub_level = 2.0  # wireless 6.5" volume, independent of bar
        self.sub_xo = 120.0  # handoff; 120 matches the saved mix
        self.sub_punch = 0.0  # extra dB at 52 Hz, sub only
        self.sub_body = 0.0  # extra dB at 78 Hz, sub only
        self.bar_level = 8.0  # bar volume, independent of sub
        self.bar_voice = 0.0  # extra dB at 2.2 kHz on top of the 2 dB presence
        self.bar_body = 0.0  # extra dB at 300 Hz, bar only
        self.bar_air = 0.0  # extra dB at 9 kHz, bar only
        self.master_db = 0.0  # whole mix, on top of bar and sub
        self.mid_trim = 4.0
        self.treble_trim = 2.0
        self.gen = 0
        self.studio_meas = None
        self.ring_gb = 0.0
        self.smart_class = "mix"
        self.smart_crest = 0.0
        self.smart_corr = 1.0
        self._last_status_t = 0.0
        self._prefs_gen = -1
        self.bar_link = "unknown"  # ok | stolen | gone | unknown
        self._load_prefs()

    def _prefs_dict(self) -> dict:
        return {
            "mode": self.mode if self.mode in ("hold", "stereo") else "hold",
            "eq": [round(float(x), 3) for x in self.eq],
            "bass_trim": self.bass_trim,
            "sub_trim": self.sub_trim,
            "sub_level": self.sub_level,
            "sub_xo": self.sub_xo,
            "sub_punch": self.sub_punch,
            "sub_body": self.sub_body,
            "bar_level": self.bar_level,
            "bar_voice": self.bar_voice,
            "bar_body": self.bar_body,
            "bar_air": self.bar_air,
            "master": self.master_db,
            "mid_trim": self.mid_trim,
            "treble_trim": self.treble_trim,
            "vol_boost": self.vol_boost,
            "width": self.width,
            "balance": self.balance,
            "deep": self.deep,
            "movie": self.movie,
            "push100": self.push100,
            "shake": self.shake,
            "yard": self.yard,
            "safe_gain": self.safe_gain,
            "preamp_auto": self.preamp_auto,
            "peak_ceil_sub": self.peak_ceil_sub,
            "peak_ceil_bar": self.peak_ceil_bar,
            "lock_sub": self.lock_sub,
            "lock_bar": self.lock_bar,
            "hdmi_vol": None if self.hdmi_vol is None else round(float(self.hdmi_vol), 1),
        }

    def _load_prefs(self) -> None:
        if not PREFS.exists():
            return
        try:
            raw = json.loads(PREFS.read_text(encoding="utf-8"))
        except Exception as e:
            log_error("prefs load", e)
            return
        if not isinstance(raw, dict):
            return
        if "eq" in raw:
            try:
                arr = np.array(raw["eq"], dtype=np.float64).reshape(-1)
                if arr.size == len(BANDS):
                    arr[0] = min(float(arr[0]), 0.0)
                    self.eq = np.clip(arr, -MAX_GAIN, MAX_GAIN)
            except (TypeError, ValueError):
                pass
        for name, lo, hi in (
            ("bass_trim", -6.0, 12.0),
            ("sub_trim", -6.0, 12.0),
            ("sub_level", -12.0, 18.0),
            ("sub_xo", 80.0, 160.0),
            ("sub_punch", -8.0, 10.0),
            ("sub_body", -8.0, 10.0),
            ("bar_level", -12.0, 12.0),
            ("bar_voice", -8.0, 8.0),
            ("bar_body", -8.0, 8.0),
            ("bar_air", -8.0, 8.0),
            ("master", -24.0, 24.0),
            ("mid_trim", -6.0, 8.0),
            ("treble_trim", -6.0, 12.0),
            ("vol_boost", 0.0, 12.0),
            ("width", 0.0, 2.0),
            ("balance", -1.0, 1.0),
            ("peak_ceil_sub", 80.0, 130.0),
            ("peak_ceil_bar", 80.0, 130.0),
        ):
            if name in raw:
                attr = "master_db" if name == "master" else name
                setattr(self, attr, float(np.clip(raw[name], lo, hi)))
        for name in (
            "deep",
            "movie",
            "push100",
            "shake",
            "yard",
            "safe_gain",
            "preamp_auto",
            "lock_sub",
            "lock_bar",
        ):
            if name in raw:
                setattr(self, name, bool(raw[name]))
        m = str(raw.get("mode") or "hold")
        self.mode = m if m in ("hold", "stereo", "auto") else "hold"
        if self.mode == "auto":
            self.mode = "hold"
        # hdmi_vol in the file is a record of the last slider position.
        # Startup must not write it back. The Windows percent is live, and
        # putting a saved 100 over the user's ~23% blows the bar up.
        self.gen += 1
        log("prefs loaded (HOLD — LIVE DJ off)")

    def save_prefs(self) -> None:
        try:
            with self.lock:
                if self.gen == self._prefs_gen:
                    return
                self._prefs_gen = self.gen
                data = self._prefs_dict()
            PREFS.write_text(json.dumps(data, indent=2), encoding="utf-8")
            _keep_in_ram(PREFS)
        except OSError as e:
            log_error("prefs save", e)

    def snapshot(self) -> dict:
        with self.lock:
            d = {
                "eq": self.eq.copy(),
                "meas": self.meas.copy(),
                "abs_db": self.abs_db.copy(),
                "preamp": self.preamp,
                "bar_preamp": self.bar_preamp,
                "preamp_auto": self.preamp_auto,
                "safe_gain": self.safe_gain,
                "vol_boost": self.vol_boost,
                "rms": self.rms,
                "peak": self.peak,
                "peak_sub": self.peak_sub,
                "peak_bar": self.peak_bar,
                "peak_ceil_sub": self.peak_ceil_sub,
                "peak_ceil_bar": self.peak_ceil_bar,
                "lock_sub": self.lock_sub,
                "lock_bar": self.lock_bar,
                "bar_level": self.bar_level,
                "movie": self.movie,
                "track_flash": self.track_flash,
                "alive": self.alive,
                "error": self.error,
                "listen": self.listen,
                "mode": self.mode,
                "ab": self.ab,
                "width": self.width,
                "balance": self.balance,
                "kill": list(self.kill),
                "has_now": self.now is not None,
                "workers": self.workers,
                "ring_gb": self.ring_gb,
                "smart_class": self.smart_class,
                "smart_crest": self.smart_crest,
                "smart_corr": self.smart_corr,
                "bass_trim": self.bass_trim,
                "sub_trim": self.sub_trim,
                "sub_level": self.sub_level,
                "sub_xo": self.sub_xo,
                "sub_punch": self.sub_punch,
                "sub_body": self.sub_body,
                "bar_voice": self.bar_voice,
                "bar_body": self.bar_body,
                "bar_air": self.bar_air,
                "master": self.master_db,
                "mid_trim": self.mid_trim,
                "treble_trim": self.treble_trim,
                "shake": self.shake,
                "yard": self.yard,
                "deep": self.deep,
                "push100": self.push100,
                "board_sync": self.board_sync,
                "bar_link": self.bar_link,
            }
            if self.ab == "B" and self.now:
                n = self.now
                eq = np.array(n["eq"], dtype=np.float64)
                d["eq"] = eq
                d["preamp"] = self._stage_preamp(eq, n)
                d["bar_preamp"] = self._stage_bar_preamp(n)
                d["width"] = float(n["width"])
                d["balance"] = float(n["balance"])
                d["bass_trim"] = float(n["bass"])
                d["sub_trim"] = float(n["sub"])
                d["sub_level"] = float(n.get("sub_level", 0.0))
                d["sub_xo"] = float(n.get("sub_xo", 120.0))
                d["sub_punch"] = float(n.get("sub_punch", 0.0))
                d["sub_body"] = float(n.get("sub_body", 0.0))
                d["bar_voice"] = float(n.get("bar_voice", 0.0))
                d["bar_body"] = float(n.get("bar_body", 0.0))
                d["bar_air"] = float(n.get("bar_air", 0.0))
                d["master"] = float(n.get("master", 0.0))
                d["mid_trim"] = float(n["mid"])
                d["treble_trim"] = float(n["treble"])
                d["kill"] = list(n.get("kills") or [False, False, False])
                d["vol_boost"] = float(n["vol_boost"])
                d["shake"] = bool(n["shake"])
                d["yard"] = bool(n["yard"])
                d["deep"] = bool(n.get("deep", False))
                d["push100"] = bool(n.get("push100", False))
                d["movie"] = bool(n.get("movie", False))
                d["bar_level"] = float(n.get("bar_level", 0.0))
            return d

    def _live_mix(self) -> dict:
        return {
            "eq": self.eq.copy(),
            "width": self.width,
            "balance": self.balance,
            "bass": self.bass_trim,
            "sub": self.sub_trim,
            "sub_level": self.sub_level,
            "sub_xo": self.sub_xo,
            "sub_punch": self.sub_punch,
            "sub_body": self.sub_body,
            "bar_voice": self.bar_voice,
            "bar_body": self.bar_body,
            "bar_air": self.bar_air,
            "master": self.master_db,
            "mid": self.mid_trim,
            "treble": self.treble_trim,
            "kills": list(self.kill),
            "vol_boost": self.vol_boost,
            "shake": self.shake,
            "yard": self.yard,
            "deep": self.deep,
            "push100": self.push100,
            "movie": self.movie,
            "bar_level": self.bar_level,
        }

    def _job(self, m: dict, note: str):
        eq = np.array(m["eq"], dtype=np.float64)
        return (
            eq,
            self._stage_preamp(eq, m),
            float(m["width"]),
            float(m["balance"]),
            note,
            float(m["bass"]),
            float(m["sub"]),
            float(m["mid"]),
            float(m["treble"]),
            tuple(m.get("kills") or (False, False, False)),
            float(m["vol_boost"]),
            bool(m["shake"]),
            bool(m["yard"]),
            bool(m.get("deep", False)),
            bool(m.get("movie", False)),
            self._stage_bar_preamp(m),
            float(m.get("sub_xo", 120.0)),
            float(m.get("sub_punch", 0.0)),
            float(m.get("sub_body", 0.0)),
            float(m.get("bar_voice", 0.0)),
            float(m.get("bar_body", 0.0)),
            float(m.get("bar_air", 0.0)),
            float(m.get("master", 0.0)),
        )

    def audible(self):
        """Auto 10-band + Mixxx knobs currently sent to the bar."""
        with self.lock:
            if self.ab == "B" and self.now:
                # Restage like A so B isn't stuck at a limiter dip.
                return self._job(self.now, "B NOW")
            m = self._live_mix()
            if self.mode == "stereo":
                m["eq"] = np.zeros(len(BANDS))
            note = self.mode.upper()
            if m["yard"]:
                note += " YARD"
            if m.get("push100"):
                note += " 100"
            if m.get("deep"):
                note += " DEEP"
            if m.get("movie"):
                note += " MOVIE"
            return self._job(m, note)

    def _eq_cost(self, eq: np.ndarray, m: dict) -> float:
        """Worst-case stacked boost on the SUB path only (≤120 Hz)."""
        g63 = float(eq[1]) + float(m["sub"])
        g63 += float(m["bass"]) * 0.55
        if m.get("deep"):
            # PK 48 + overlap from PK 72 — destacked, not two 63s
            g63 = float(m["sub"]) + 4.0 + 0.40 * (float(m["bass"]) + 1.5)
        elif m["shake"]:
            g63 += 3.5 + 2.5  # 55 Hz + 75 Hz SHAKE peaks
        g63 += max(0.0, float(m.get("sub_punch", 0.0)))
        g63 += 0.6 * max(0.0, float(m.get("sub_body", 0.0)))
        vb = float(m["vol_boost"])
        if vb > 0.15 and not m["shake"] and not m.get("deep"):
            g63 += vb * (0.18 if m["yard"] else 0.28)
        return max(g63, float(eq[1]), float(m["sub"]), 0.0)

    def _stage_preamp(self, eq: np.ndarray, m: dict) -> float:
        """Pay for every stacked boost. VOL BOOST only fills leftover headroom."""
        cost = self._eq_cost(eq, m)
        ceiling = -cost - HEADROOM_DB
        ceiling = float(np.clip(ceiling, -24.0, 0.0))
        if self.preamp_auto:
            base = ceiling
        elif self.safe_gain:
            base = float(min(self.preamp, ceiling))
        else:
            base = float(np.clip(self.preamp, -24.0, 12.0))
        user_sub = float(m.get("sub_level", 0.0))
        # SUB VOL is the wireless-sub level. Do not let the vocal
        # headroom bill cancel it. clip_trim is the limiter.
        boosted = user_sub + float(self.clip_trim)
        if self.safe_gain:
            # Indoor: never 0 dBFS on the SUB path unless the user
            # SUB VOL fader asks for more. Limiter is still the clip stop.
            # YARD / 100 let leftover VOL BOOST through.
            if m["yard"]:
                cap = 6.0
            elif m.get("deep"):
                cap = max(2.0, -0.5 + user_sub)
            elif m.get("push100"):
                cap = max(1.5, -0.5 + user_sub)
            else:
                cap = max(-0.5, -0.5 + user_sub)
            # Q60T wireless 6.5" needs the SUB fader to actually arrive.
            # 14.5 dB was still thin once the bar drivers were already loud.
            cap = min(18.0, max(cap, user_sub))
            return float(np.clip(boosted, -24.0, cap))
        return float(np.clip(boosted, -24.0, 18.0))

    def _stage_bar_preamp(self, m: dict) -> float:
        """Bar path is vocals/mids — do not pay for 808 boosts."""
        user_bar = float(m.get("bar_level", 0.0))
        if m.get("movie"):
            makeup = 12.0
        elif m.get("push100"):
            makeup = 6.5
        elif m["yard"]:
            makeup = 5.5
        else:
            # Front drivers were still quiet with the fader up and Windows at full.
            makeup = 14.0
        g = makeup + float(self.clip_trim_bar) + user_bar
        if self.safe_gain:
            if m.get("movie"):
                cap = 14.5
            elif m.get("push100"):
                cap = 8.5
            elif m["yard"]:
                cap = 7.0
            else:
                cap = 14.0
            cap = min(26.0, cap + max(0.0, user_bar))
            return float(np.clip(g, -12.0, cap))
        return float(np.clip(g, -12.0, 26.0))

    def _apply_trims(self, eq: np.ndarray) -> np.ndarray:
        out = eq.copy()
        # 6.5" sub F3 ~58 Hz: put punch at 63/125, never boost 31.5
        out[1] += self.bass_trim + self.sub_trim
        out[2] += self.bass_trim * 0.85 + self.sub_trim * 0.45
        out[0] = min(out[0], 0.0)
        out[3:7] += self.mid_trim
        out[7:10] += self.treble_trim
        return np.clip(out, -12.0, 12.0)

    def _apply_kills(self, eq: np.ndarray) -> np.ndarray:
        out = eq.copy()
        if self.kill[0]:
            out[0:3] = -12.0
        if self.kill[1]:
            out[3:7] = -12.0
        if self.kill[2]:
            out[7:10] = -12.0
        return out

    def pull_board(self) -> None:
        """Take the console: live_dj.board.json sets EQ/knobs and HOLD."""
        if not BOARD.exists():
            return
        try:
            raw = json.loads(BOARD.read_text(encoding="utf-8"))
        except Exception as e:
            log_error("board json", e)
            return
        try:
            BOARD.unlink()
        except OSError:
            pass
        if not isinstance(raw, dict):
            return
        with self.lock:
            if "eq" in raw:
                arr = np.array(raw["eq"], dtype=np.float64).reshape(-1)
                if arr.size == len(BANDS):
                    arr[0] = min(float(arr[0]), 0.0)
                    self.eq = np.clip(arr, -MAX_GAIN, 6.0)
            if "bass" in raw:
                self.bass_trim = float(raw["bass"])
            if "sub" in raw:
                self.sub_trim = float(raw["sub"])
            if "sub_level" in raw:
                self.sub_level = float(np.clip(raw["sub_level"], -12.0, 18.0))
            if "sub_xo" in raw:
                self.sub_xo = float(np.clip(raw["sub_xo"], 80.0, 160.0))
            if "sub_punch" in raw:
                self.sub_punch = float(np.clip(raw["sub_punch"], -8.0, 10.0))
            if "sub_body" in raw:
                self.sub_body = float(np.clip(raw["sub_body"], -8.0, 10.0))
            if "bar_voice" in raw:
                self.bar_voice = float(np.clip(raw["bar_voice"], -8.0, 8.0))
            if "bar_body" in raw:
                self.bar_body = float(np.clip(raw["bar_body"], -8.0, 8.0))
            if "bar_air" in raw:
                self.bar_air = float(np.clip(raw["bar_air"], -8.0, 8.0))
            if "master" in raw:
                self.master_db = float(np.clip(raw["master"], -24.0, 24.0))
            if "bar_level" in raw:
                self.bar_level = float(np.clip(raw["bar_level"], -12.0, 12.0))
            if "movie" in raw:
                self.movie = bool(raw["movie"])
            if "mid" in raw:
                self.mid_trim = float(raw["mid"])
            if "treble" in raw:
                self.treble_trim = float(raw["treble"])
            if "vol_boost" in raw:
                self.vol_boost = float(np.clip(raw["vol_boost"], 0.0, 12.0))
            if "width" in raw:
                self.width = float(np.clip(raw["width"], 0.0, 2.0))
            if "yard" in raw:
                self.yard = bool(raw["yard"])
            if "shake" in raw:
                self.shake = bool(raw["shake"])
            if "deep" in raw:
                self.deep = bool(raw["deep"])
            if "push100" in raw:
                self.push100 = bool(raw["push100"])
            if "peak_ceil_sub" in raw:
                self.peak_ceil_sub = float(np.clip(raw["peak_ceil_sub"], 80.0, 130.0))
            if "peak_ceil_bar" in raw:
                self.peak_ceil_bar = float(np.clip(raw["peak_ceil_bar"], 80.0, 130.0))
            if "lock_sub" in raw:
                self.lock_sub = bool(raw["lock_sub"])
            if "lock_bar" in raw:
                self.lock_bar = bool(raw["lock_bar"])
            if "safe" in raw:
                self.safe_gain = bool(raw["safe"])
            if "preamp_auto" in raw:
                self.preamp_auto = bool(raw["preamp_auto"])
            self.mode = str(raw.get("mode") or "hold")
            self.ab = "A"
            self.board_sync += 1
            self.gen += 1
        log(f"board take  mode={self.mode}  bass={self.bass_trim}  mid={self.mid_trim}")

    def save_now(self) -> None:
        with self.lock:
            m = self._live_mix()
            m["eq"] = np.array(m["eq"], dtype=np.float64).tolist()
            self.now = m
            self.mode = "hold"
            self.ab = "A"

    def run(self) -> None:
        if STOP.exists():
            try:
                STOP.unlink()
            except OSError:
                pass
        PIDFILE.write_text(str(os.getpid()), encoding="ascii")
        pool = cpu_pool()
        self.workers = pool._max_workers
        audio_q: queue.Queue = queue.Queue(maxsize=4)
        write_q: queue.Queue = queue.Queue(maxsize=8)
        ring, gb = alloc_ring(RING_WANT_GB)
        self.ring_gb = gb
        nring = ring.shape[0]
        ring_lock = threading.Lock()
        ring_state = {"pos": 0, "filled": 0}
        self.listen = f"HDMI → bar · studio dual · {gb:.1f} GB"
        attach_peace()
        _keep_in_ram(STATUS)
        _keep_in_ram(LOG)
        _keep_in_ram(ERRLOG)
        try:
            write_config(*self.audible())
        except Exception as e:
            log_error("initial peace write", e)
        log(f"live DJ start  cores={_CORES}  pool={self.workers}  ring={gb:.2f} GB")
        _kill_audio_thieves()
        pin_hdmi_default(force=True)
        try:
            mic = hdmi_loopback()
        except Exception as e:
            log_error("HDMI loopback failed", e)
            pin_hdmi_default(force=True)
            mic = hdmi_loopback()
        log(f"loopback {mic}")
        self.alive = True

        def capture() -> None:
            # HDMI unplug / Windows jumping to Realtek invalidates WASAPI
            # (AUDCLNT_E_DEVICE_INVALIDATED 0x88890004). Retry. Do NOT send
            # None into audio_q on a drop — that kills the process thread.
            backoff = 0.5
            while not self.stop_event.is_set() and not STOP.exists():
                try:
                    try:
                        pin_hdmi_default()
                        mic_now = hdmi_loopback()
                    except Exception as e:
                        log_error(
                            "HDMI loopback missing (bar/default device?)",
                            e,
                            throttle_s=20.0,
                        )
                        pin_hdmi_default(force=True)
                        time.sleep(backoff)
                        backoff = min(6.0, backoff * 1.6)
                        continue
                    log(f"loopback {mic_now}")
                    with self.lock:
                        self.error = ""
                        self.listen = f"HDMI → bar · studio dual · {gb:.1f} GB"
                    last_pin = time.time()
                    with mic_now.recorder(samplerate=SR, channels=2, blocksize=N) as rec:
                        backoff = 0.5
                        while not self.stop_event.is_set() and not STOP.exists():
                            nowp = time.time()
                            if nowp - last_pin > 3.0:
                                last_pin = nowp
                                if not default_is_hdmi():
                                    log("default jumped off HDMI — pinning bar")
                                    pin_hdmi_default(force=True)
                            x = rec.record(numframes=N)
                            x32 = np.ascontiguousarray(x, dtype=np.float32)
                            if x32.ndim == 1:
                                x32 = np.stack([x32, x32], axis=1)
                            nfr = int(x32.shape[0])
                            with ring_lock:
                                pos = ring_state["pos"]
                                end = pos + nfr
                                if end <= nring:
                                    ring[pos:end] = x32
                                else:
                                    k = nring - pos
                                    ring[pos:] = x32[:k]
                                    ring[: nfr - k] = x32[k:]
                                ring_state["pos"] = end % nring
                                ring_state["filled"] = min(
                                    ring_state["filled"] + nfr, nring
                                )
                            try:
                                audio_q.put(x, timeout=0.05)
                            except queue.Full:
                                try:
                                    audio_q.get_nowait()
                                except queue.Empty:
                                    pass
                                try:
                                    audio_q.put_nowait(x)
                                except queue.Full:
                                    pass
                except Exception as e:
                    self.error = traceback.format_exc()
                    log_error(
                        "capture drop — retrying HDMI loopback",
                        e,
                        throttle_s=20.0,
                    )
                    time.sleep(backoff)
                    backoff = min(6.0, backoff * 1.6)
            try:
                audio_q.put_nowait(None)
            except queue.Full:
                pass

        def writer() -> None:
            pending = None
            last = 0.0
            min_dt = 0.125  # 8 Hz ceiling — SN750 does not need every twitch
            while True:
                try:
                    job = write_q.get(timeout=0.05)
                except queue.Empty:
                    job = "tick"
                if job is None:
                    if pending is not None:
                        try:
                            write_config(*pending)
                            self._status()
                        except Exception as e:
                            log_error("peace.txt write", e)
                    break
                if job != "tick":
                    pending = job
                now = time.perf_counter()
                if pending is not None and (now - last) >= min_dt:
                    try:
                        write_config(*pending)
                        self._status()
                    except Exception as e:
                        log_error("peace.txt write", e)
                    pending = None
                    last = now

        def studio() -> None:
            try:
                _studio_loop()
            except Exception as e:
                self.error = traceback.format_exc()
                log_error("studio thread", e)

        def _studio_loop() -> None:
            while not self.stop_event.is_set() and not STOP.exists():
                time.sleep(0.32)
                with ring_lock:
                    filled = ring_state["filled"]
                    pos = ring_state["pos"]
                    if filled < STUDIO_WIN:
                        continue
                    take = min(int(STUDIO_SECONDS * SR), filled)
                    if pos >= take:
                        chunk = ring[pos - take : pos].copy()
                    else:
                        chunk = np.concatenate(
                            [ring[nring - (take - pos) :], ring[:pos]], axis=0
                        )
                try:
                    meas = welch_shape(chunk, pool)
                except Exception as e:
                    log_error("studio fft", e)
                    continue
                with self.lock:
                    self.studio_meas = meas

        def process() -> None:
            try:
                _process_loop()
            except Exception as e:
                self.error = traceback.format_exc()
                log_error("process thread", e)

        def _process_loop() -> None:
            last_write = self.eq.copy()
            last_pre = self.preamp
            last_bar = self.bar_preamp
            last_gen = -1
            last_meas = None
            last_rms = None
            catchup = 0.0
            refractory = 0.0
            flux_hits = 0
            votes: list[str] = []
            bias = CLASS_BIAS["mix"].copy()
            t0 = time.perf_counter()
            while True:
                x = audio_q.get()
                if x is None or self.stop_event.is_set() or STOP.exists():
                    break
                self.pull_board()
                now = time.perf_counter()
                dt = max(0.02, min(0.5, now - t0))
                t0 = now
                rms = float(np.sqrt(np.mean(x ** 2)))
                peak, psub, pbar, pvoice = split_peaks(x)
                with self.lock:
                    # Independent ceilings. Lock freezes LIVE DJ on that path.
                    push = self.push100
                    allow_s = float(self.peak_ceil_sub) / 100.0
                    allow_b = float(self.peak_ceil_bar) / 100.0
                    pull_s = allow_s * (PEAK_PULL / PEAK_ALLOW)
                    pull_b = allow_b * (PEAK_PULL / PEAK_ALLOW)
                    ls, lb = self.lock_sub, self.lock_bar
                    # A sub hit ducks the wireless sub only. The bar keeps
                    # vocals and its own bass (above the 120 Hz split).
                    sub_hit = psub >= 0.9 and psub >= pvoice
                    if (not ls) and psub >= 1.0:
                        self.clip_trim -= 3.0 * dt
                    elif not ls:
                        if self.clip_trim < 0.0:
                            self.clip_trim = min(0.0, self.clip_trim + 8.0 * dt)
                        elif self.clip_trim > 0.0 and (
                            not (self.yard or push) or psub >= 1.00
                        ):
                            self.clip_trim = max(0.0, self.clip_trim - 2.5 * dt)
                    if (not lb) and pvoice >= 1.0 and not sub_hit:
                        self.clip_trim_bar -= 3.0 * dt
                    elif not lb:
                        # Let a sub-caused duck go. Vocals should be back
                        # before the next kick, not several seconds later.
                        rec = 12.0 if sub_hit else (4.0 if self.movie else 2.5)
                        if self.clip_trim_bar < 0.0:
                            self.clip_trim_bar = min(
                                0.0, self.clip_trim_bar + rec * dt
                            )
                        elif self.clip_trim_bar > 0.0 and (
                            not (push or self.movie) or pbar >= 1.05
                        ):
                            self.clip_trim_bar = max(
                                0.0, self.clip_trim_bar - rec * dt
                            )
                    self.clip_trim = float(
                        np.clip(
                            self.clip_trim,
                            -24.0,
                            6.0 if (self.yard or push) else 0.0,
                        )
                    )
                    self.clip_trim_bar = float(
                        np.clip(
                            self.clip_trim_bar,
                            -6.0 if self.movie else -12.0,
                            6.0 if (self.movie or push) else 0.0,
                        )
                    )
                    self.rms, self.peak = rms, peak
                    self.peak_sub, self.peak_bar = psub, pbar
                    self.track_flash = max(0.0, self.track_flash - dt)
                if rms < SILENCE:
                    last_rms = rms
                    self._status()
                    continue
                meas_fast, abs_db = analyze(x, pool)
                new_track = False
                refractory = max(0.0, refractory - dt)
                if last_meas is not None:
                    flux = float(np.mean(np.abs(meas_fast - last_meas)))
                    gap = (
                        last_rms is not None
                        and last_rms < 0.012
                        and rms > 0.04
                    )
                    if flux > 8.5:
                        flux_hits += 1
                    else:
                        flux_hits = 0
                    # Only a real gap is a skip. Kick-drum flux is not NEW TRACK.
                    if refractory <= 0 and gap:
                        new_track = True
                        catchup = 2.4
                        refractory = 5.0
                        flux_hits = 0
                    elif flux > 4.5 and catchup <= 0:
                        catchup = max(catchup, 0.4)
                last_meas, last_rms = meas_fast, rms
                with self.lock:
                    sm = self.studio_meas
                if sm is not None:
                    meas = (
                        0.82 * meas_fast + 0.18 * sm
                        if new_track
                        else 0.28 * meas_fast + 0.72 * sm
                    )
                else:
                    meas = meas_fast
                if catchup > 0:
                    catchup = max(0.0, catchup - dt)
                with self.lock:
                    mode = self.mode
                    ab = self.ab
                    p_auto = self.preamp_auto
                    p_user = self.preamp
                    yard = self.yard
                if mode == "auto":
                    if self.auto_warm > 0:
                        self.auto_warm = max(0.0, self.auto_warm - dt)
                        tau_scale = 6.0
                    elif new_track:
                        tau_scale = 0.10
                    elif catchup > 0:
                        tau_scale = 0.28
                    else:
                        tau_scale = 1.0
                    tau = TAU * tau_scale
                    corr = stereo_corr(x)
                    cls = classify(meas, rms, peak, corr)
                    votes.append(cls)
                    if len(votes) > 10:
                        votes.pop(0)
                    stable = max(set(votes), key=votes.count)
                    bias = 0.90 * bias + 0.10 * CLASS_BIAS[stable]
                    tgt = TARGET + bias
                    residual = tgt - meas
                    residual[np.abs(residual) < DEADBAND] = 0.0
                    hot = meas > 2.0
                    residual = np.where(hot & (residual > 0.0), residual * 0.2, residual)
                    residual[0] = min(float(residual[0]), 0.0)
                    crest = peak / (rms + 1e-12)
                    if crest < 2.7:
                        residual[1:3] = np.minimum(residual[1:3], residual[1:3] * 0.35)
                    alpha = 1.0 - np.exp(-dt / tau)
                    eq = self.eq + alpha * residual
                    eq = np.clip(eq, -MAX_GAIN, MAX_GAIN)
                    eq[0] = float(np.clip(eq[0], -MAX_GAIN, MAX_LOW))
                    if stable == "bass":
                        eq[1] = min(float(eq[1]), 0.0)
                        eq[2] = min(float(eq[2]), 0.4)
                    hi = eq[6:10].copy()
                    hi_pos = np.maximum(hi, 0.0)
                    hi_sum = float(hi_pos.sum())
                    if hi_sum > 4.5:
                        eq[6:10] = hi * (4.5 / hi_sum)
                    if yard:
                        # 8–16 kHz dies outdoors; don't spend headroom on air
                        eq[8] = min(float(eq[8]), 0.5)
                        eq[9] = min(float(eq[9]), 0.0)
                    with self.lock:
                        if self.lock_sub:
                            eq[0:3] = self.eq[0:3]
                        if self.lock_bar:
                            eq[3:] = self.eq[3:]
                    preamp = p_user
                    with self.lock:
                        self.smart_class = stable
                        self.smart_crest = float(crest)
                        self.smart_corr = float(corr)
                elif mode == "stereo":
                    eq = np.zeros(len(BANDS))
                    preamp = p_user
                else:
                    eq, preamp = self.eq, p_user
                with self.lock:
                    self.eq = eq
                    mix = self._live_mix()
                    mix["eq"] = eq
                    preamp = self._stage_preamp(eq, mix)
                    bar_pre = self._stage_bar_preamp(mix)
                with self.lock:
                    self.meas, self.abs_db = meas, abs_db
                    self.preamp = preamp
                    self.bar_preamp = bar_pre
                    if new_track:
                        self.track_flash = 1.6
                job = self.audible()
                out_eq, out_pre = job[0], job[1]
                # bar preamp stays at index 15. Extra sub/bar knobs are appended after it.
                out_bar = float(job[15]) if len(job) > 15 else bar_pre
                with self.lock:
                    gen = self.gen
                if (
                    gen != last_gen
                    or np.max(np.abs(out_eq - last_write)) >= WRITE_EPS
                    or abs(out_pre - last_pre) >= 0.2
                    or abs(out_bar - last_bar) >= 0.2
                ):
                    last_write, last_pre, last_gen = out_eq.copy(), out_pre, gen
                    last_bar = out_bar
                    try:
                        write_q.put_nowait(job)
                    except queue.Full:
                        pass

        def pin_loop() -> None:
            seen = None
            seen_n = 0
            while not self.stop_event.is_set() and not STOP.exists():
                link = "gone"
                try:
                    link = bar_link_state()
                    # No bar endpoint (HDMI unplugged): killing FxSound only
                    # flashes consoles and chops PC-speaker movie audio.
                    if link == "stolen":
                        pin_hdmi_default(force=True)
                        _kill_audio_thieves()
                        if not default_is_hdmi():
                            pin_hdmi_default(force=True)
                        link = "ok" if default_is_hdmi() else "stolen"
                except Exception as e:
                    log_error("hdmi pin loop", e, throttle_s=20.0)
                    link = "gone"
                if link == seen:
                    seen_n += 1
                else:
                    seen = link
                    seen_n = 1
                # One pass when it returns. Two passes (~4s) before calling it gone,
                # so a one-off device blip does not toast over a movie.
                if seen_n >= (1 if link == "ok" else 2):
                    with self.lock:
                        self.bar_link = link
                for _ in range(20):
                    if self.stop_event.is_set() or STOP.exists():
                        break
                    time.sleep(0.1)

        try:
            threads = [
                threading.Thread(target=capture, name="dj-capture", daemon=True),
                threading.Thread(target=process, name="dj-process", daemon=True),
                threading.Thread(target=writer, name="dj-write", daemon=True),
                threading.Thread(target=studio, name="dj-studio", daemon=True),
                threading.Thread(target=pin_loop, name="dj-hdmi-pin", daemon=True),
            ]
            for t in threads:
                t.start()
            while not self.stop_event.is_set() and not STOP.exists():
                time.sleep(0.15)
            self.stop_event.set()
            try:
                audio_q.put_nowait(None)
            except queue.Full:
                pass
            try:
                write_q.put_nowait(None)
            except queue.Full:
                pass
            for t in threads:
                t.join(timeout=1.5)
        except Exception as e:
            self.error = traceback.format_exc()
            log_error("engine run", e)
        finally:
            self.alive = False
            if STOP.exists():
                try:
                    STOP.unlink()
                except OSError:
                    pass
            if PIDFILE.exists():
                try:
                    PIDFILE.unlink()
                except OSError:
                    pass
            save_peace_seed()
            self.save_prefs()
            log("live DJ exit")

    def _status(self) -> None:
        now = time.perf_counter()
        if now - self._last_status_t < 0.5:
            return
        self._last_status_t = now
        try:
            STATUS.write_text(
                json.dumps(
                    {
                        "running": True,
                        "rms": round(self.rms, 5),
                        "peak": round(self.peak, 4),
                        "peak_sub": round(self.peak_sub, 4),
                        "peak_bar": round(self.peak_bar, 4),
                        "peak_ceil_sub": round(self.peak_ceil_sub, 1),
                        "peak_ceil_bar": round(self.peak_ceil_bar, 1),
                        "lock_sub": bool(self.lock_sub),
                        "lock_bar": bool(self.lock_bar),
                        "preamp": round(self.preamp, 2),
                        "bar_preamp": round(self.bar_preamp, 2),
                        "sub_level": round(self.sub_level, 2),
                        "sub_xo": round(self.sub_xo, 1),
                        "sub_punch": round(self.sub_punch, 2),
                        "sub_body": round(self.sub_body, 2),
                        "bar_level": round(self.bar_level, 2),
                        "bar_voice": round(self.bar_voice, 2),
                        "bar_body": round(self.bar_body, 2),
                        "bar_air": round(self.bar_air, 2),
                        "master": round(self.master_db, 2),
                        "movie": bool(self.movie),
                        "push100": bool(self.push100),
                        "bar_link": self.bar_link,
                        "bands": {
                            f"{fc:g}": round(float(g), 2)
                            for fc, g in zip(BANDS, self.eq)
                        },
                        "ts": time.time(),
                        "error": (self.error or "")[:240],
                    }
                ),
                encoding="utf-8",
            )
            _keep_in_ram(STATUS)
        except OSError as e:
            log_error("status.json write", e)


def _pid_command_line(pid: int) -> str:
    """Empty when the pid is already gone. Used so a stale pidfile cannot kill a reused pid."""
    try:
        import subprocess

        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=4,
            creationflags=flags,
        )
        return r.stdout or ""
    except Exception:
        return ""


def stop_other() -> None:
    STOP.write_text("1", encoding="ascii")
    if PIDFILE.exists():
        try:
            pid = int(PIDFILE.read_text().strip())
            if pid != os.getpid() and "live_dj.py" in _pid_command_line(pid):
                os.kill(pid, 15)
        except (OSError, ValueError):
            pass
    time.sleep(0.3)


def gui() -> None:
    import tkinter as tk

    install_error_logging()
    stop_other()
    engine = Engine()
    # Audio thread starts after the strips exist. Opening WASAPI while
    # tk is still building the window has exited pythonw with no traceback.
    thread = threading.Thread(target=engine.run, daemon=False)

    root = tk.Tk()
    root.title("DJ Lab — Q6T")
    root.configure(bg="#0e0e10")
    root.attributes("-topmost", False)
    root.resizable(True, True)
    root.minsize(1180, 900)
    root.geometry("1320x940+60+20")

    BG, INK, DIM = "#0e0e10", "#e8e8ea", "#7a7a82"
    ACC, CUT, OFF = "#3dff9a", "#ff4d6d", "#2a2a30"
    STRIP = "#141418"
    # Scale.set() during build fires command. Stay quiet until the
    # sliders are copied from the engine so a relaunch cannot rewrite the mix.
    _ui_live = {"on": False}

    def bump() -> None:
        with engine.lock:
            engine.gen += 1

    def set_mode(mode: str) -> None:
        with engine.lock:
            engine.mode = mode
            engine.ab = "A"
            if mode == "auto":
                engine.auto_warm = 8.0
            engine.gen += 1
            engine.save_prefs()
        paint_modes()

    def save_now() -> None:
        engine.save_now()
        bump()
        paint_modes()

    def toggle_ab() -> None:
        with engine.lock:
            if engine.now is None:
                return
            engine.ab = "B" if engine.ab == "A" else "A"
            engine.gen += 1
        paint_modes()

    def toggle_kill(i: int) -> None:
        with engine.lock:
            engine.kill[i] = not engine.kill[i]
            engine.gen += 1
        paint_kills()

    top = tk.Frame(root, bg=BG)
    top.pack(fill="x", padx=14, pady=(12, 6))
    live = tk.Label(top, text="● LIVE", fg=ACC, bg=BG, font=("Segoe UI", 12, "bold"))
    live.pack(side="left")
    listen_l = tk.Label(top, text="HDMI → bar", fg=DIM, bg=BG, font=("Segoe UI", 9))
    listen_l.pack(side="left", padx=10)
    flash = tk.Label(top, text="", fg=ACC, bg=BG, font=("Segoe UI", 9, "bold"))
    flash.pack(side="right")

    modes = tk.Frame(root, bg=BG)
    modes.pack(fill="x", padx=14, pady=4)
    btns = {}

    def mkbtn(parent, text, cmd, w=10):
        b = tk.Button(
            parent,
            text=text,
            command=cmd,
            bg=OFF,
            fg=INK,
            activebackground="#333",
            activeforeground=INK,
            relief="flat",
            font=("Segoe UI", 9, "bold"),
            width=w,
            padx=6,
            pady=6,
        )
        b.pack(side="left", padx=3)
        return b

    btns["auto"] = mkbtn(modes, "LIVE DJ", lambda: set_mode("auto"))
    btns["stereo"] = mkbtn(modes, "STEREO", lambda: set_mode("stereo"))
    btns["now"] = mkbtn(modes, "NOW", save_now)
    btns["ab"] = mkbtn(modes, "A / B", toggle_ab)

    def paint_modes() -> None:
        snap = engine.snapshot()
        for k, b in btns.items():
            b.configure(bg=OFF, fg=INK)
        if snap["ab"] == "B":
            btns["ab"].configure(bg=ACC, fg="#111")
        elif snap["mode"] == "auto":
            btns["auto"].configure(bg=ACC, fg="#111")
        elif snap["mode"] == "stereo":
            btns["stereo"].configure(bg=ACC, fg="#111")
        elif snap["mode"] == "hold":
            btns["now"].configure(bg=ACC, fg="#111")
        if snap["has_now"]:
            btns["now"].configure(text="NOW ✓")
        else:
            btns["now"].configure(text="NOW")

    stats = tk.Frame(root, bg=BG)
    stats.pack(fill="x", padx=14, pady=2)
    def _pk_fg(pct: float, ceil: float) -> str:
        if pct >= ceil:
            return CUT
        if pct >= min(100.0, ceil):
            return "#ffe566"
        return INK

    peak_l = tk.Label(stats, text="sub pk  —", fg=INK, bg=BG, font=("Consolas", 10))
    peak_l.pack(side="left")
    peak_bar_l = tk.Label(stats, text="bar pk  —", fg=INK, bg=BG, font=("Consolas", 10))
    peak_bar_l.pack(side="left", padx=12)
    pre_l = tk.Label(stats, text="preamp  —", fg=INK, bg=BG, font=("Consolas", 10))
    pre_l.pack(side="left", padx=16)
    rms_l = tk.Label(stats, text="level  —", fg=INK, bg=BG, font=("Consolas", 10))
    rms_l.pack(side="left")
    err_l = tk.Label(stats, text="", fg=CUT, bg=BG, font=("Consolas", 8))
    err_l.pack(side="left", padx=10)
    mode_l = tk.Label(stats, text="", fg=DIM, bg=BG, font=("Consolas", 10))
    mode_l.pack(side="right")

    canvas = tk.Canvas(root, width=652, height=120, bg="#16161a", highlightthickness=0)
    canvas.pack(padx=14, pady=4, fill="x")
    n = len(BANDS)

    def draw(snap: dict) -> None:
        cw = max(400, int(canvas.winfo_width() or 652))
        ch = max(90, int(canvas.winfo_height() or 120))
        left, right, top_y, bot = 28, cw - 16, 16, ch - 30
        slot = (right - left) / n
        canvas.delete("all")
        canvas.create_line(left, (top_y + bot) / 2, right, (top_y + bot) / 2, fill="#2a2a30")
        eq, meas = snap["eq"], snap["meas"]
        if snap["ab"] == "B" and engine.now:
            eq = np.array(engine.now["eq"])
        elif snap["mode"] == "stereo":
            eq = np.zeros(len(BANDS))
        else:
            with engine.lock:
                eq = engine._apply_kills(engine._apply_trims(eq))
        peak, pre, rms = snap["peak"], snap["preamp"], snap["rms"]
        for i, lab in enumerate(LABELS):
            x0 = left + i * slot + 6
            x1 = x0 + slot - 12
            nrg = float(np.clip((meas[i] + 10.0) / 20.0, 0.0, 1.0))
            y_n = bot - nrg * (bot - top_y)
            canvas.create_rectangle(x0, y_n, x1, bot, fill="#1e2a33", outline="")
            mid = (top_y + bot) / 2
            g = float(np.clip(eq[i] / max(MAX_GAIN, 12.0) * MAX_GAIN / MAX_GAIN, -1.2, 1.2))
            g = float(np.clip(eq[i] / 6.0, -1.0, 1.0))
            y_g = mid - g * (bot - top_y) * 0.42
            color = ACC if eq[i] >= 0 else CUT
            canvas.create_rectangle(x0 + 4, y_g, x1 - 4, mid, fill=color, outline="")
            canvas.create_text((x0 + x1) / 2, bot + 12, text=lab, fill=DIM, font=("Segoe UI", 8))
            canvas.create_text(
                (x0 + x1) / 2,
                y_g - 8 if eq[i] >= 0 else y_g + 8,
                text=f"{eq[i]:+.1f}",
                fill=INK,
                font=("Consolas", 7),
            )
        sp = float(snap.get("peak_sub", peak)) * 100.0
        bp = float(snap.get("peak_bar", 0.0)) * 100.0
        cs = float(snap.get("peak_ceil_sub", 114.5))
        cb = float(snap.get("peak_ceil_bar", 114.5))
        peak_l.config(text=f"sub pk {sp:6.1f}% / {cs:.1f}", fg=_pk_fg(sp, cs))
        peak_bar_l.config(text=f"bar pk {bp:6.1f}% / {cb:.1f}", fg=_pk_fg(bp, cb))
        bar_p = float(snap.get("bar_preamp", 0.0))
        sl = float(snap.get("sub_level", 0.0))
        bl = float(snap.get("bar_level", 0.0))
        pre_l.config(
            text=f"sub {pre:+.1f}   bar {bar_p:+.1f}   subvol {sl:+.1f}   barvol {bl:+.1f}"
        )
        rms_l.config(text=f"level  {20*np.log10(max(rms,1e-9)):+.1f} dBFS")
        sub_pk_l.config(text=f"peak {sp:5.1f}%", fg=_pk_fg(sp, cs))
        bar_pk_l.config(text=f"peak {bp:5.1f}%", fg=_pk_fg(bp, cb))
        mst_lv_l.config(text=f"level {20*np.log10(max(rms,1e-9)):+.1f} dB")
        hear = "B NOW" if snap["ab"] == "B" else snap["mode"].upper()
        sc = str(snap.get("smart_class", "mix")).upper()
        lk = []
        if snap.get("lock_sub"):
            lk.append("SUB LOCK")
        if snap.get("lock_bar"):
            lk.append("BAR LOCK")
        lock_txt = ("   " + " ".join(lk)) if lk else ""
        mode_l.config(text=f"hearing  {hear}   smart {sc}{lock_txt}")
        listen_l.config(
            text=(
                f"{snap.get('listen', 'HDMI → bar')}  ·  "
                f"{snap.get('workers', _CORES)} threads"
            )
        )
        if snap["track_flash"] > 0 and snap["mode"] == "auto" and snap["ab"] == "A":
            flash.config(text="NEW TRACK")
            live.config(fg="#ffe566")
        else:
            flash.config(text="")
            live.config(fg=ACC if snap["alive"] else CUT)

    kill_btns = []

    def paint_kills() -> None:
        snap = engine.snapshot()
        for i, b in enumerate(kill_btns):
            if snap["kill"][i]:
                b.configure(bg=CUT, fg="#fff")
            else:
                b.configure(bg=OFF, fg=INK)

    gain = tk.Frame(root, bg=BG)
    gain.pack(fill="x", padx=14, pady=(0, 6))
    tk.Label(gain, text="SUB AUTO", fg=DIM, bg=BG, font=("Segoe UI", 8, "bold")).pack(side="left")
    pre_s = tk.Scale(
        gain,
        from_=-12,
        to=12,
        resolution=0.1,
        orient="horizontal",
        length=280,
        bg=BG,
        fg=INK,
        troughcolor="#222",
        highlightthickness=0,
        showvalue=1,
        command=lambda v: _set_pre(float(v)),
    )
    pre_s.set(0.0)
    pre_s.pack(side="left", padx=8)
    tk.Label(gain, text="dB", fg=DIM, bg=BG, font=("Segoe UI", 8)).pack(side="left")
    pre_auto_btn = tk.Button(
        gain,
        text="AUTO",
        command=lambda: _toggle_pre_auto(),
        bg=ACC,
        fg="#111",
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        width=6,
        pady=4,
    )
    pre_auto_btn.pack(side="left", padx=10)
    _pre_from_code = {"on": False}

    def _set_w(v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            engine.width = v
            engine.gen += 1

    def _set_b(v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            engine.balance = v
            engine.gen += 1

    def _set_pre(v: float) -> None:
        if not _ui_live["on"] or _pre_from_code["on"]:
            return
        with engine.lock:
            engine.preamp = v
            engine.preamp_auto = False
            engine.gen += 1
        pre_auto_btn.configure(bg=OFF, fg=INK)

    def _toggle_pre_auto() -> None:
        with engine.lock:
            engine.preamp_auto = not engine.preamp_auto
            engine.gen += 1
            on = engine.preamp_auto
        pre_auto_btn.configure(bg=ACC if on else OFF, fg="#111" if on else INK)

    def _toggle_safe() -> None:
        with engine.lock:
            engine.safe_gain = not engine.safe_gain
            engine.gen += 1
            on = engine.safe_gain
        safe_btn.configure(bg=ACC if on else OFF, fg="#111" if on else INK)

    safe_btn = tk.Button(
        gain,
        text="SAFE",
        command=_toggle_safe,
        bg=ACC,
        fg="#111",
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        width=6,
        pady=4,
    )
    safe_btn.pack(side="left", padx=4)

    def _set_trim(name: str, v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            setattr(engine, name, v)
            engine.gen += 1
        if name == "bar_level":
            _sync_bar_ui(float(v))
        elif name == "sub_level":
            _sync_sub_ui(float(v))

    def _blast() -> None:
        # Punch 63/125, never 31 Hz. Preamp AUTO pays for the boosts.
        with engine.lock:
            engine.preamp_auto = True
            engine.safe_gain = True
            engine.yard = False
            engine.shake = False
            engine.bass_trim = 4.0
            engine.sub_trim = 3.0
            engine.mid_trim = 2.0
            engine.treble_trim = 0.0
            engine.vol_boost = 8.0
            engine.push100 = False
            engine.deep = False
            engine.gen += 1
        bass_s.set(4.0)
        sub_s.set(3.0)
        mid_s.set(2.0)
        treble_s.set(0.0)
        pre_auto_btn.configure(bg=ACC, fg="#111")
        safe_btn.configure(bg=ACC, fg="#111")
        vol_s.set(8.0)
        yard_btn.configure(bg=OFF, fg=INK)
        shake_btn.configure(bg=OFF, fg=INK)
        push100_btn.configure(bg=OFF, fg=INK)
        deep_btn.configure(bg=OFF, fg=INK)

    blast_btn = tk.Button(
        gain,
        text="BLAST",
        command=_blast,
        bg=CUT,
        fg="#fff",
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        width=7,
        pady=4,
    )
    blast_btn.pack(side="left", padx=6)

    def _toggle_shake() -> None:
        with engine.lock:
            engine.shake = not engine.shake
            on = engine.shake
            if on:
                engine.yard = False
                engine.push100 = False
                engine.deep = False
                engine.bass_trim = 3.0
                engine.sub_trim = 5.0
                engine.mid_trim = -2.0
                engine.vol_boost = min(engine.vol_boost, 4.0)
                engine.preamp_auto = True
                engine.safe_gain = True
            engine.gen += 1
        shake_btn.configure(bg="#c9a227" if on else OFF, fg="#111" if on else INK)
        yard_btn.configure(bg=OFF, fg=INK)
        push100_btn.configure(bg=OFF, fg=INK)
        deep_btn.configure(bg=OFF, fg=INK)
        if on:
            bass_s.set(3.0)
            sub_s.set(5.0)
            mid_s.set(-2.0)
            pre_auto_btn.configure(bg=ACC, fg="#111")
            safe_btn.configure(bg=ACC, fg="#111")

    shake_btn = tk.Button(
        gain,
        text="SHAKE",
        command=_toggle_shake,
        bg=OFF,
        fg=INK,
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        width=7,
        pady=4,
    )
    shake_btn.pack(side="left", padx=6)

    def _toggle_deep(force=None) -> None:
        # Port-tune the 6.5" (Fb ~47 Hz). Not 31 Hz — that unloads the port.
        with engine.lock:
            engine.deep = (not engine.deep) if force is None else bool(force)
            on = engine.deep
            if on:
                engine.shake = False
                engine.preamp_auto = True
                engine.safe_gain = True
                engine.bass_trim = 2.0
                engine.sub_trim = 5.0
                engine.sub_level = max(float(engine.sub_level), 2.0)
                engine.vol_boost = float(np.clip(engine.vol_boost, 6.0, 10.0))
            engine.gen += 1
            sl = float(engine.sub_level)
            vb = float(engine.vol_boost)
        deep_btn.configure(bg="#5ad0ff" if on else OFF, fg="#111" if on else INK)
        if on:
            shake_btn.configure(bg=OFF, fg=INK)
            bass_s.set(2.0)
            sub_s.set(5.0)
            sub_vol.set(sl)
            sub_gain_s.set(sl)
            vol_s.set(vb)
            pre_auto_btn.configure(bg=ACC, fg="#111")
            safe_btn.configure(bg=ACC, fg="#111")

    deep_btn = tk.Button(
        gain,
        text="DEEP",
        command=_toggle_deep,
        bg=OFF,
        fg=INK,
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        width=7,
        pady=4,
    )
    deep_btn.pack(side="left", padx=6)

    def _toggle_yard() -> None:
        # Outdoor throw: spend unused peak on 2.5 kHz presence, not 31 Hz.
        with engine.lock:
            engine.yard = not engine.yard
            on = engine.yard
            if on:
                engine.shake = False
                engine.push100 = False
                engine.deep = False
                engine.bass_trim = 3.0
                engine.sub_trim = 2.0
                engine.mid_trim = 2.0
                engine.treble_trim = 1.0
                engine.vol_boost = 8.0
                engine.preamp_auto = True
                engine.safe_gain = True
            else:
                engine.bass_trim = 4.0
                engine.sub_trim = 3.0
                engine.mid_trim = 2.0
                engine.treble_trim = 0.0
                engine.vol_boost = 8.0
            engine.gen += 1
        yard_btn.configure(bg="#7cff6b" if on else OFF, fg="#111" if on else INK)
        shake_btn.configure(bg=OFF, fg=INK)
        push100_btn.configure(bg=OFF, fg=INK)
        deep_btn.configure(bg=OFF, fg=INK)
        bass_s.set(3.0 if on else 4.0)
        sub_s.set(2.0 if on else 3.0)
        mid_s.set(2.0)
        treble_s.set(1.0 if on else 0.0)
        vol_s.set(8.0)
        pre_auto_btn.configure(bg=ACC, fg="#111")
        safe_btn.configure(bg=ACC, fg="#111")

    yard_btn = tk.Button(
        gain,
        text="YARD",
        command=_toggle_yard,
        bg=OFF,
        fg=INK,
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        width=7,
        pady=4,
    )
    yard_btn.pack(side="left", padx=6)

    def _hit_100(force=None) -> None:
        # Ride the ceiling for loud rock (In The End). Chester on the bar.
        with engine.lock:
            engine.push100 = (not engine.push100) if force is None else bool(force)
            on = engine.push100
            if on:
                engine.yard = False
                engine.shake = False
                engine.preamp_auto = True
                engine.safe_gain = True
                engine.bass_trim = 2.0
                engine.sub_trim = 1.5
                engine.mid_trim = 3.5
                engine.treble_trim = 1.5
                engine.vol_boost = 10.0
                engine.ab = "A"
            engine.gen += 1
        push100_btn.configure(bg="#ff2d55" if on else OFF, fg="#fff" if on else INK)
        if on:
            yard_btn.configure(bg=OFF, fg=INK)
            shake_btn.configure(bg=OFF, fg=INK)
            bass_s.set(2.0)
            sub_s.set(1.5)
            mid_s.set(3.5)
            treble_s.set(1.5)
            vol_s.set(10.0)
            pre_auto_btn.configure(bg=ACC, fg="#111")
            safe_btn.configure(bg=ACC, fg="#111")

    push100_btn = tk.Button(
        gain,
        text="100",
        command=_hit_100,
        bg=OFF,
        fg=INK,
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        width=7,
        pady=4,
    )
    push100_btn.pack(side="left", padx=6)

    def _toggle_movie(force=None) -> None:
        with engine.lock:
            engine.movie = (not engine.movie) if force is None else bool(force)
            on = engine.movie
            if on:
                engine.yard = False
                engine.preamp_auto = True
                engine.safe_gain = True
                engine.mid_trim = 4.0
                engine.treble_trim = 2.0
                engine.bar_level = max(float(engine.bar_level), 8.0)
                engine.ab = "A"
            engine.gen += 1
            bl = float(engine.bar_level)
        movie_btn.configure(bg="#7cff6b" if on else OFF, fg="#111" if on else INK)
        if on:
            yard_btn.configure(bg=OFF, fg=INK)
            mid_s.set(4.0)
            treble_s.set(2.0)
            bar_vol.set(bl)
            bar_s.set(bl)
            pre_auto_btn.configure(bg=ACC, fg="#111")
            safe_btn.configure(bg=ACC, fg="#111")

    movie_btn = tk.Button(
        gain,
        text="MOVIE",
        command=_toggle_movie,
        bg=OFF,
        fg=INK,
        relief="flat",
        font=("Segoe UI", 9, "bold"),
        width=7,
        pady=4,
    )
    movie_btn.pack(side="left", padx=6)

    def _set_vol(v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            engine.vol_boost = v
            engine.gen += 1

    _sub_from_code = {"on": False}
    _sub_knob = {"s": None}

    def _sync_sub_ui(v: float) -> None:
        if _sub_from_code["on"]:
            return
        _sub_from_code["on"] = True
        try:
            v = float(np.clip(v, -12.0, 18.0))
            if abs(float(sub_vol.get()) - v) > 0.05:
                sub_vol.set(v)
            k = _sub_knob["s"]
            if k is not None and abs(float(k.get()) - v) > 0.05:
                k.set(v)
        finally:
            _sub_from_code["on"] = False

    def _set_sub_level(v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            engine.sub_level = float(np.clip(v, -12.0, 18.0))
            engine.gen += 1
        _sync_sub_ui(float(v))

    _bar_from_code = {"on": False}
    _bar_knob = {"s": None}

    def _sync_bar_ui(v: float) -> None:
        if _bar_from_code["on"]:
            return
        _bar_from_code["on"] = True
        try:
            v = float(np.clip(v, -12.0, 12.0))
            if abs(float(bar_vol.get()) - v) > 0.05:
                bar_vol.set(v)
            k = _bar_knob["s"]
            if k is not None and abs(float(k.get()) - v) > 0.05:
                k.set(v)
        finally:
            _bar_from_code["on"] = False

    def _set_bar_level(v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            engine.bar_level = float(np.clip(v, -12.0, 12.0))
            engine.gen += 1
        _sync_bar_ui(float(v))

    mixer = tk.Frame(root, bg=BG)

    def _strip(title, fg):
        col = tk.Frame(
            mixer, bg=STRIP, highlightbackground=fg, highlightthickness=2, padx=8, pady=6
        )
        col.pack(side="left", fill="both", expand=True, padx=5)
        tk.Label(col, text=title, fg=fg, bg=STRIP, font=("Segoe UI", 14, "bold")).pack()
        return col

    def _vknob(parent, label, lo, hi, init, command, fg, trough, length=108, res=0.5, wide=14):
        box = tk.Frame(parent, bg=STRIP)
        box.pack(side="left", padx=2)
        tk.Label(box, text=label, fg=fg, bg=STRIP, font=("Segoe UI", 7, "bold")).pack()
        s = tk.Scale(
            box,
            from_=hi,
            to=lo,
            resolution=res,
            orient="vertical",
            length=length,
            width=wide,
            bg=STRIP,
            fg=fg,
            troughcolor=trough,
            highlightthickness=0,
            showvalue=1,
            command=command,
        )
        s.set(init)
        s.pack()
        return s

    def _hslider(parent, label, lo, hi, init, command, fg, trough, length=170, res=0.5):
        row = tk.Frame(parent, bg=STRIP)
        row.pack(fill="x", pady=1)
        tk.Label(
            row, text=label, fg=fg, bg=STRIP, font=("Segoe UI", 8, "bold"), width=8, anchor="w"
        ).pack(side="left")
        s = tk.Scale(
            row,
            from_=lo,
            to=hi,
            resolution=res,
            orient="horizontal",
            length=length,
            bg=STRIP,
            fg=fg,
            troughcolor=trough,
            highlightthickness=0,
            showvalue=1,
            command=command,
        )
        s.set(init)
        s.pack(side="left", padx=4)
        return s, row

    def _kill_btn(parent, index, name):
        b = tk.Button(
            parent,
            text=name,
            command=lambda i=index: toggle_kill(i),
            bg=OFF,
            fg=INK,
            relief="flat",
            font=("Segoe UI", 8, "bold"),
            width=8,
            pady=2,
        )
        b.pack(side="left", padx=3)
        kill_btns.append(b)
        return b

    def _set_sub_xo(v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            engine.sub_xo = float(np.clip(v, 80.0, 160.0))
            engine.gen += 1

    def _set_sub_punch(v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            engine.sub_punch = float(np.clip(v, -8.0, 10.0))
            engine.gen += 1

    def _set_sub_body(v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            engine.sub_body = float(np.clip(v, -8.0, 10.0))
            engine.gen += 1

    def _set_bar_voice(v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            engine.bar_voice = float(np.clip(v, -8.0, 8.0))
            engine.gen += 1

    def _set_bar_body(v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            engine.bar_body = float(np.clip(v, -8.0, 8.0))
            engine.gen += 1

    def _set_bar_air(v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            engine.bar_air = float(np.clip(v, -8.0, 8.0))
            engine.gen += 1

    # prog: Scale.set() fires command. hold: ignore device polls while dragging.
    hdmi_ui = {"prog": False, "hold": 0.0, "ticks": 0}

    def _set_hdmi_out(v: float) -> None:
        if not _ui_live["on"] or hdmi_ui["prog"]:
            return
        pct = float(np.clip(round(float(v)), 0.0, 100.0))
        hdmi_ui["hold"] = time.time() + 0.9
        with engine.lock:
            engine.hdmi_vol = pct
        # Endpoint COM stays off the tk thread. A read here during
        # widget setup has exited the process with no traceback.
        threading.Thread(
            target=set_hdmi_out_percent, args=(pct,), name="dj-hdmi-set", daemon=True
        ).start()

    def _show_hdmi(pct: float) -> None:
        hdmi_ui["prog"] = True
        try:
            bar_out.set(int(round(pct)))
        finally:
            hdmi_ui["prog"] = False

    def _poll_hdmi() -> None:
        if hdmi_ui.get("busy"):
            return
        hdmi_ui["busy"] = True

        def work() -> None:
            pct = hdmi_out_percent()

            def apply() -> None:
                hdmi_ui["busy"] = False
                if pct is None:
                    return
                try:
                    if abs(float(bar_out.get()) - pct) >= 1.0:
                        _show_hdmi(pct)
                        with engine.lock:
                            engine.hdmi_vol = pct
                except tk.TclError:
                    pass

            try:
                root.after(0, apply)
            except tk.TclError:
                hdmi_ui["busy"] = False

        threading.Thread(target=work, name="dj-hdmi-read", daemon=True).start()

    def _set_ceil(which: str, v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            if which == "sub":
                engine.peak_ceil_sub = float(np.clip(v, 80.0, 130.0))
            else:
                engine.peak_ceil_bar = float(np.clip(v, 80.0, 130.0))
            engine.gen += 1

    def _toggle_lock(which: str) -> None:
        with engine.lock:
            if which == "sub":
                engine.lock_sub = not engine.lock_sub
                on = engine.lock_sub
            else:
                engine.lock_bar = not engine.lock_bar
                on = engine.lock_bar
            engine.gen += 1
        btn = lock_sub_btn if which == "sub" else lock_bar_btn
        btn.configure(bg="#ff2d55" if on else OFF, fg="#fff" if on else INK)

    def _set_master(v: float) -> None:
        if not _ui_live["on"]:
            return
        with engine.lock:
            engine.master_db = float(np.clip(v, -24.0, 24.0))
            engine.gen += 1

    def _loud() -> None:
        with engine.lock:
            engine.vol_boost = 12.0
            engine.gen += 1
        vol_s.set(12.0)

    sub_col = _strip("SUB", "#c9a227")
    tk.Label(
        sub_col, text="wireless 6.5 in  ·  bass", fg=DIM, bg=STRIP, font=("Segoe UI", 8)
    ).pack()
    sub_pk_l = tk.Label(sub_col, text="peak —", fg=INK, bg=STRIP, font=("Consolas", 9))
    sub_pk_l.pack(pady=(1, 4))
    tk.Label(sub_col, text="EQ", fg="#c9a227", bg=STRIP, font=("Segoe UI", 8, "bold")).pack(anchor="w")
    sub_eq = tk.Frame(sub_col, bg=STRIP)
    sub_eq.pack(fill="x")
    bass_s = _vknob(
        sub_eq, "BASS", -6, 12, 0.0, lambda v: _set_trim("bass_trim", float(v)),
        "#c9a227", "#3a2e10",
    )
    sub_s = _vknob(
        sub_eq, "63", -6, 12, 0.0, lambda v: _set_trim("sub_trim", float(v)),
        "#c9a227", "#3a2e10",
    )
    sub_punch_s = _vknob(
        sub_eq, "PUNCH", -8, 10, 0.0, lambda v: _set_sub_punch(float(v)),
        "#c9a227", "#3a2e10",
    )
    sub_body_s = _vknob(
        sub_eq, "BODY", -8, 10, 0.0, lambda v: _set_sub_body(float(v)),
        "#c9a227", "#3a2e10",
    )
    sub_xo_s, _xo_row = _hslider(
        sub_col, "XO", 80, 160, 120.0, lambda v: _set_sub_xo(float(v)),
        "#c9a227", "#3a2e10", res=2, length=180,
    )
    sub_ceil_s, ceil_row = _hslider(
        sub_col, "CEIL", 80, 130, 114.5, lambda v: _set_ceil("sub", float(v)),
        "#c9a227", "#3a2e10", length=120,
    )
    tk.Label(ceil_row, text="%", fg=DIM, bg=STRIP, font=("Segoe UI", 8)).pack(side="left")
    lock_sub_btn = tk.Button(
        ceil_row, text="LOCK", command=lambda: _toggle_lock("sub"),
        bg=OFF, fg=INK, relief="flat", font=("Segoe UI", 8, "bold"), width=5, pady=2,
    )
    lock_sub_btn.pack(side="left", padx=4)
    sub_kills = tk.Frame(sub_col, bg=STRIP)
    sub_kills.pack(fill="x", pady=(4, 0))
    tk.Label(sub_kills, text="KILL", fg=DIM, bg=STRIP, font=("Segoe UI", 8, "bold")).pack(side="left")
    _kill_btn(sub_kills, 0, "LOW")
    sub_faders = tk.Frame(sub_col, bg=STRIP)
    sub_faders.pack(pady=(6, 0))
    sub_gain_s = _vknob(
        sub_faders, "GAIN", -12, 18, 0.0, lambda v: _set_sub_level(float(v)),
        "#c9a227", "#3a2e10", length=120, wide=16,
    )
    _sub_knob["s"] = sub_gain_s
    sub_vol = _vknob(
        sub_faders, "VOL", -12, 18, 0.0, lambda v: _set_sub_level(float(v)),
        "#c9a227", "#3a2e10", length=210, wide=24,
    )

    bar_col = _strip("BAR", ACC)
    tk.Label(
        bar_col, text="front drivers  ·  vocals", fg=DIM, bg=STRIP, font=("Segoe UI", 8)
    ).pack()
    bar_pk_l = tk.Label(bar_col, text="peak —", fg=INK, bg=STRIP, font=("Consolas", 9))
    bar_pk_l.pack(pady=(1, 4))
    tk.Label(bar_col, text="EQ", fg=ACC, bg=STRIP, font=("Segoe UI", 8, "bold")).pack(anchor="w")
    bar_eq = tk.Frame(bar_col, bg=STRIP)
    bar_eq.pack(fill="x")
    mid_s = _vknob(
        bar_eq, "MIDS", -6, 8, 0.0, lambda v: _set_trim("mid_trim", float(v)),
        ACC, "#1e2a33",
    )
    treble_s = _vknob(
        bar_eq, "TREB", -6, 12, 0.0, lambda v: _set_trim("treble_trim", float(v)),
        ACC, "#1e2a33",
    )
    bar_body_s = _vknob(
        bar_eq, "BODY", -8, 8, 0.0, lambda v: _set_bar_body(float(v)),
        ACC, "#1e2a33",
    )
    bar_voice_s = _vknob(
        bar_eq, "VOICE", -8, 8, 0.0, lambda v: _set_bar_voice(float(v)),
        ACC, "#1e2a33",
    )
    bar_air_s = _vknob(
        bar_eq, "AIR", -8, 8, 0.0, lambda v: _set_bar_air(float(v)),
        ACC, "#1e2a33",
    )
    tk.Label(
        bar_col, text="crossover follows SUB XO", fg=DIM, bg=STRIP, font=("Segoe UI", 7)
    ).pack(anchor="w", pady=(3, 0))
    bar_ceil_s, bar_ceil_row = _hslider(
        bar_col, "CEIL", 80, 130, 114.5, lambda v: _set_ceil("bar", float(v)),
        ACC, "#1e2a33", length=120,
    )
    tk.Label(bar_ceil_row, text="%", fg=DIM, bg=STRIP, font=("Segoe UI", 8)).pack(side="left")
    lock_bar_btn = tk.Button(
        bar_ceil_row, text="LOCK", command=lambda: _toggle_lock("bar"),
        bg=OFF, fg=INK, relief="flat", font=("Segoe UI", 8, "bold"), width=5, pady=2,
    )
    lock_bar_btn.pack(side="left", padx=4)
    bar_kills = tk.Frame(bar_col, bg=STRIP)
    bar_kills.pack(fill="x", pady=(4, 0))
    tk.Label(bar_kills, text="KILL", fg=DIM, bg=STRIP, font=("Segoe UI", 8, "bold")).pack(side="left")
    _kill_btn(bar_kills, 1, "MID")
    _kill_btn(bar_kills, 2, "HIGH")
    bar_faders = tk.Frame(bar_col, bg=STRIP)
    bar_faders.pack(pady=(6, 0))
    bar_s = _vknob(
        bar_faders, "GAIN", -12, 12, 0.0, lambda v: _set_bar_level(float(v)),
        ACC, "#1e2a33", length=120, wide=16,
    )
    _bar_knob["s"] = bar_s
    bar_vol = _vknob(
        bar_faders, "VOL", -12, 12, 0.0, lambda v: _set_bar_level(float(v)),
        ACC, "#1e2a33", length=210, wide=24,
    )

    mst_col = _strip("MASTER", "#ffe566")
    tk.Label(
        mst_col, text="whole soundbar", fg=DIM, bg=STRIP, font=("Segoe UI", 8)
    ).pack()
    mst_lv_l = tk.Label(mst_col, text="level —", fg=INK, bg=STRIP, font=("Consolas", 9))
    mst_lv_l.pack(pady=(1, 4))
    width, _wrow = _hslider(
        mst_col, "WIDTH", 0, 2, 1.0, lambda v: _set_w(float(v)),
        INK, "#222", res=0.05, length=180,
    )
    bal, _brow = _hslider(
        mst_col, "BAL", -1, 1, 0.0, lambda v: _set_b(float(v)),
        INK, "#222", res=0.05, length=180,
    )
    bar_out, _out_row = _hslider(
        mst_col, "OUT", 0, 100, 0.0, lambda v: _set_hdmi_out(float(v)),
        "#7CFFB2", "#143028", length=180, res=1,
    )
    vol_s, boost_row = _hslider(
        mst_col, "BOOST", 0, 12, 0.0, lambda v: _set_vol(float(v)),
        INK, "#222", length=140,
    )
    tk.Button(
        boost_row, text="LOUD", command=_loud,
        bg="#ff8c42", fg="#111", relief="flat",
        font=("Segoe UI", 8, "bold"), width=6, pady=2,
    ).pack(side="left", padx=4)
    mst_faders = tk.Frame(mst_col, bg=STRIP)
    mst_faders.pack(pady=(6, 0))
    master_s = _vknob(
        mst_faders, "VOLUME", -24, 24, 0.0, lambda v: _set_master(float(v)),
        "#ffe566", "#2a2410", length=210, wide=26,
    )

    def _seed_mixer() -> None:
        snap = engine.snapshot()
        bass_s.set(float(snap["bass_trim"]))
        sub_s.set(float(snap["sub_trim"]))
        sub_punch_s.set(float(snap["sub_punch"]))
        sub_body_s.set(float(snap["sub_body"]))
        sub_xo_s.set(float(snap["sub_xo"]))
        sub_ceil_s.set(float(snap["peak_ceil_sub"]))
        sub_gain_s.set(float(snap["sub_level"]))
        sub_vol.set(float(snap["sub_level"]))
        mid_s.set(float(snap["mid_trim"]))
        treble_s.set(float(snap["treble_trim"]))
        bar_body_s.set(float(snap["bar_body"]))
        bar_voice_s.set(float(snap["bar_voice"]))
        bar_air_s.set(float(snap["bar_air"]))
        bar_ceil_s.set(float(snap["peak_ceil_bar"]))
        bar_s.set(float(snap["bar_level"]))
        bar_vol.set(float(snap["bar_level"]))
        master_s.set(float(snap["master"]))
        vol_s.set(float(snap["vol_boost"]))
        width.set(float(snap["width"]))
        bal.set(float(snap["balance"]))
        _pre_from_code["on"] = True
        try:
            pre_s.set(round(float(snap["preamp"]), 1))
        finally:
            _pre_from_code["on"] = False

    _seed_mixer()
    _ui_live["on"] = True
    with engine.lock:
        engine.board_sync += 1

    tk.Label(
        root,
        text=(
            f"{BAR['model']}  ·  {BAR['channels']}  ·  {BAR['power_w']} W  ·  "
            f"{BAR['sub_model']} {BAR['sub_driver_in']}\" {BAR['sub_w']}W "
            f"ported (Fb ~{BAR['sub_fb_hz']} Hz, F3 ~{BAR['sub_f3_hz']} Hz)  ·  "
            f"HDMI IN {BAR['hdmi_in']}"
        ),
        fg="#5a5a62",
        bg=BG,
        font=("Segoe UI", 7),
    ).pack(side="bottom", pady=(0, 6))
    tk.Label(
        root,
        text="Three strips: SUB, BAR, MASTER. Gain and VOL on a strip move together. Opens in HOLD.",
        fg=DIM,
        bg=BG,
        font=("Segoe UI", 8),
    ).pack(side="bottom", pady=(2, 0))
    mixer.pack(fill="both", expand=True, padx=8, pady=(4, 2))

    last_board = {"n": -1}
    tray = BarTray()
    shown_link = {"v": None}

    def paint_bar(link: str) -> None:
        _title, _body, window, tip = _BAR_COPY.get(link, _BAR_COPY["gone"])
        if root.title() != window:
            root.title(window)
        if link == "ok":
            live.config(text="● LIVE", fg=ACC)
            listen_l.config(text=tip, fg=ACC)
        elif link == "stolen":
            live.config(text="● BAR LOST", fg=CUT)
            listen_l.config(text=tip, fg=CUT)
        else:
            live.config(text="● NO BAR", fg=CUT)
            listen_l.config(text=tip, fg=CUT)

    def show_bar(link: str) -> None:
        paint_bar(link)
        try:
            hwnd = int(root.winfo_id())
        except Exception:
            hwnd = 0
        if hwnd:
            tray.attach(hwnd)
            tray.update(link)
            flash_taskbar(hwnd, stop=(link == "ok"))
        notify_bar(link)

    def tick() -> None:
        try:
            snap = engine.snapshot()
            draw(snap)
            paint_modes()
            paint_kills()
            if snap.get("preamp_auto") or snap.get("safe_gain"):
                if abs(float(pre_s.get()) - snap["preamp"]) > 0.2:
                    _pre_from_code["on"] = True
                    pre_s.set(round(snap["preamp"], 1))
                    _pre_from_code["on"] = False
            if snap.get("preamp_auto"):
                pre_auto_btn.configure(bg=ACC, fg="#111")
            bs = int(snap.get("board_sync") or 0)
            if bs != last_board["n"]:
                last_board["n"] = bs
                bass_s.set(float(snap["bass_trim"]))
                sub_s.set(float(snap["sub_trim"]))
                sub_vol.set(float(snap.get("sub_level", 0.0)))
                sub_gain_s.set(float(snap.get("sub_level", 0.0)))
                sub_xo_s.set(float(snap.get("sub_xo", 120.0)))
                sub_punch_s.set(float(snap.get("sub_punch", 0.0)))
                sub_body_s.set(float(snap.get("sub_body", 0.0)))
                bar_body_s.set(float(snap.get("bar_body", 0.0)))
                bar_voice_s.set(float(snap.get("bar_voice", 0.0)))
                bar_air_s.set(float(snap.get("bar_air", 0.0)))
                master_s.set(float(snap.get("master", 0.0)))
                mid_s.set(float(snap["mid_trim"]))
                treble_s.set(float(snap["treble_trim"]))
                vol_s.set(float(snap["vol_boost"]))
                width.set(float(snap["width"]))
                shake_btn.configure(
                    bg="#c9a227" if snap["shake"] else OFF,
                    fg="#111" if snap["shake"] else INK,
                )
                yard_btn.configure(
                    bg="#7cff6b" if snap["yard"] else OFF,
                    fg="#111" if snap["yard"] else INK,
                )
                push100_btn.configure(
                    bg="#ff2d55" if snap.get("push100") else OFF,
                    fg="#fff" if snap.get("push100") else INK,
                )
                deep_btn.configure(
                    bg="#5ad0ff" if snap.get("deep") else OFF,
                    fg="#111" if snap.get("deep") else INK,
                )
                movie_btn.configure(
                    bg="#7cff6b" if snap.get("movie") else OFF,
                    fg="#111" if snap.get("movie") else INK,
                )
                bar_vol.set(float(snap.get("bar_level", 0.0)))
                bar_s.set(float(snap.get("bar_level", 0.0)))
                safe_btn.configure(
                    bg=ACC if snap["safe_gain"] else OFF,
                    fg="#111" if snap["safe_gain"] else INK,
                )
                sub_ceil_s.set(float(snap.get("peak_ceil_sub", 114.5)))
                bar_ceil_s.set(float(snap.get("peak_ceil_bar", 114.5)))
                lock_sub_btn.configure(
                    bg="#ff2d55" if snap.get("lock_sub") else OFF,
                    fg="#fff" if snap.get("lock_sub") else INK,
                )
                lock_bar_btn.configure(
                    bg="#ff2d55" if snap.get("lock_bar") else OFF,
                    fg="#fff" if snap.get("lock_bar") else INK,
                )
            err = (snap.get("error") or "").strip()
            if err:
                one = err.splitlines()[-1][:72]
                err_l.configure(text=one)
            else:
                err_l.configure(text="")
            hdmi_ui["ticks"] += 1
            if hdmi_ui["ticks"] >= 6 and time.time() >= hdmi_ui["hold"]:
                hdmi_ui["ticks"] = 0
                _poll_hdmi()
            link = str(snap.get("bar_link") or "unknown")
            if link in ("ok", "stolen", "gone"):
                if link != shown_link["v"]:
                    shown_link["v"] = link
                    show_bar(link)
                else:
                    paint_bar(link)
        except Exception as e:
            log_error("gui tick", e)
            err_l.configure(text=f"ERR {type(e).__name__}")
        root.after(80, tick)

    def on_close() -> None:
        log("window close")
        tray.close()
        engine.save_prefs()
        engine.stop_event.set()
        STOP.write_text("1", encoding="ascii")
        root.destroy()

    paint_modes()
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.update_idletasks()
    log("gui widgets ready")
    thread.start()
    log(f"gui mainloop {root.winfo_width()}x{root.winfo_height()} at {root.winfo_x()},{root.winfo_y()}")
    tick()
    try:
        root.mainloop()
    except Exception as e:
        log_error("gui mainloop", e)
    log("gui mainloop end")
    engine.stop_event.set()


def main() -> int:
    install_error_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="gui", choices=["gui", "run", "stop", "status"])
    args = ap.parse_args()
    if args.cmd == "stop":
        stop_other()
        print("stopping")
        return 0
    if args.cmd == "status":
        print(STATUS.read_text(encoding="utf-8") if STATUS.exists() else "not running")
        return 0
    if args.cmd == "gui":
        try:
            gui()
        except Exception as e:
            log_error("gui", e)
            raise
        return 0
    try:
        Engine().run()
    except KeyboardInterrupt:
        log("interrupt")
    except Exception as e:
        log_error("crash", e)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
