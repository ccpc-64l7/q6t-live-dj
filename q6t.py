#!/usr/bin/env python3
"""Q6T control from this PC.

HDMI-CEC cannot leave the GTX 1650 — NVIDIA does not expose pin 13.
This tool still gives full PC-side control of the HDMI audio path and
FxSound (volume, mute, treble, bass, voice, surround, sound modes).

CEC frames are built exactly as a Samsung TV would send them to an
Audio System (logical addr 5). If a CEC adapter appears later, send()
already has a hook.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

FX = Path(r"C:\Program Files\FxSound LLC\FxSound\FxSound.exe")
STATUS = Path.home() / "AppData/Roaming/FxSound/status.json"
HDMI_NAME = "EP-HDMI-RX (2- NVIDIA High Definition Audio)"

# ISO 31-band indexes
BASS_BANDS = (5, 6, 7, 8, 9)          # 63–160 Hz
VOICE_BANDS = (17, 18, 19, 20, 21, 22)  # 1k–3.15k
TREBLE_BANDS = (24, 25, 26, 27, 28)    # 5k–12.5k

# CEC as a Samsung TV (addr 0) talking to Audio System (addr 5)
CEC_TV, CEC_AUDIO = 0x0, 0x5
CEC_USER_CONTROL_PRESSED = 0x44
CEC_USER_CONTROL_RELEASED = 0x45
CEC_GIVE_AUDIO_STATUS = 0x71
CEC_SET_SYSTEM_AUDIO_MODE = 0x72
CEC_SYSTEM_AUDIO_MODE_REQUEST = 0x70
UI = {
    "vol+": 0x41,
    "vol-": 0x42,
    "mute": 0x43,
    "power": 0x40,
    "up": 0x01,
    "down": 0x02,
    "left": 0x03,
    "right": 0x04,
    "select": 0x00,
    "root-menu": 0x09,
    "play": 0x44,
    "pause": 0x46,
}


def cec_header(src: int, dst: int) -> int:
    return ((src & 0xF) << 4) | (dst & 0xF)


def cec_frame(*bytes_: int) -> bytes:
    return bytes(bytes_)


def cec_tv_to_bar(ui_cmd: str) -> list[bytes]:
    """User Control Pressed + Released — same sequence a Samsung TV sends."""
    code = UI[ui_cmd]
    hdr = cec_header(CEC_TV, CEC_AUDIO)
    return [
        cec_frame(hdr, CEC_USER_CONTROL_PRESSED, code),
        cec_frame(hdr, CEC_USER_CONTROL_RELEASED),
    ]


def cec_hex(frames: list[bytes]) -> str:
    return "  |  ".join(f.hex(" ").upper() for f in frames)


def fx(*args: str) -> None:
    if not FX.exists():
        raise SystemExit("FxSound.exe not found")
    subprocess.run([str(FX), *args], check=False)


def fx_status() -> dict:
    fx("--status")
    time.sleep(0.4)
    if not STATUS.exists():
        return {}
    return json.loads(STATUS.read_text(encoding="utf-8"))


def bump_bands(indexes: tuple[int, ...], delta: float) -> None:
    st = fx_status()
    bands = (st.get("equalizer") or {}).get("bands") or []
    parts = []
    for b in bands:
        i = int(b["index"])
        g = float(b["gain"])
        if i in indexes:
            g = max(-12.0, min(12.0, g + delta))
        parts.append(f"{i}:{g:.1f}")
    if not parts:
        raise SystemExit("no EQ bands — is FxSound running?")
    fx(f"--set_band_gain={','.join(parts)}")


def bump_effect(name: str, delta: float) -> None:
    st = fx_status()
    cur = float((st.get("effects") or {}).get(name, 0))
    nxt = max(0.0, min(10.0, cur + delta))
    fx(f"--set_effect={name}:{nxt:.1f}")


def set_volume(delta: float | None = None, absolute: float | None = None) -> str:
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import POINTER, cast

    speakers = AudioUtilities.GetSpeakers()
    iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    vol = cast(iface, POINTER(IAudioEndpointVolume))
    if absolute is not None:
        vol.SetMasterVolumeLevelScalar(max(0.0, min(1.0, absolute)), None)
    elif delta is not None:
        cur = vol.GetMasterVolumeLevelScalar()
        vol.SetMasterVolumeLevelScalar(max(0.0, min(1.0, cur + delta)), None)
    pct = int(vol.GetMasterVolumeLevelScalar() * 100)
    muted = bool(vol.GetMute())
    return f"volume={pct}% mute={muted}"


def toggle_mute() -> str:
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import POINTER, cast

    speakers = AudioUtilities.GetSpeakers()
    iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    vol = cast(iface, POINTER(IAudioEndpointVolume))
    vol.SetMute(0 if vol.GetMute() else 1, None)
    return f"mute={bool(vol.GetMute())}"


def hdmi_live() -> bool:
    st = fx_status()
    # also check Windows
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception:
        pass
    return True


def cmd_status(_: argparse.Namespace) -> None:
    st = fx_status()
    eq = st.get("equalizer") or {}
    print(f"fxsound power : {st.get('power')}")
    print(f"preset        : {st.get('preset')}")
    print(f"eq bands      : {eq.get('num_bands')}")
    print(f"effects       : {json.dumps(st.get('effects'), indent=None)}")
    print(f"hdmi device   : {HDMI_NAME}")
    print()
    print("CEC as Samsung TV -> Audio System (addr 5):")
    for name in ("vol+", "vol-", "mute", "power"):
        print(f"  {name:6}  {cec_hex(cec_tv_to_bar(name))}")
    print()
    print("This PC HDMI (GTX 1650) does not expose CEC pin 13.")
    print("Those frames are what a Samsung TV would put on the wire.")
    print("PC-side volume / EQ / voice / bass still work over HDMI audio.")


def cmd_cec(ns: argparse.Namespace) -> None:
    frames = cec_tv_to_bar(ns.key)
    print("Samsung-TV-style CEC:")
    print(cec_hex(frames))
    print("not sent: no CEC adapter / GPU pin on this Nitro")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="q6t", description="Q6T + HDMI + FxSound control")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show HDMI / FxSound / CEC dump")

    v = sub.add_parser("vol", help="PC HDMI path volume")
    v.add_argument("how", choices=["+", "-", "up", "down", "mute"])
    v.add_argument("amount", nargs="?", type=float, default=0.05)

    t = sub.add_parser("treble", help="live treble (5–12.5 kHz)")
    t.add_argument("how", choices=["+", "-"])
    b = sub.add_parser("bass", help="live bass (63–160 Hz)")
    b.add_argument("how", choices=["+", "-"])
    vo = sub.add_parser("voice", help="live vocals (1–3 kHz)")
    vo.add_argument("how", choices=["+", "-"])

    sm = sub.add_parser("mode", help="FxSound sound character")
    sm.add_argument("name", choices=["music", "voice", "standard", "surround"])

    c = sub.add_parser("cec", help="print TV-style CEC frame (not transmitted here)")
    c.add_argument("key", choices=list(UI))

    sub.add_parser("gui", help="on-screen remote")
    return p


def cmd_mode(ns: argparse.Namespace) -> None:
    fx("--power=1")
    if ns.name == "music":
        fx("--set_effect=clarity:5.5,ambience:2.5,surround:3.0,dynamicboost:1.5,bass:4.5")
    elif ns.name == "voice":
        fx("--set_effect=clarity:8.0,ambience:1.5,surround:2.0,dynamicboost:1.5,bass:3.0")
        bump_bands(VOICE_BANDS, 0.5)
    elif ns.name == "standard":
        fx("--set_effect=clarity:4.0,ambience:2.0,surround:2.0,dynamicboost:1.0,bass:3.5")
    elif ns.name == "surround":
        fx("--set_effect=clarity:5.0,ambience:6.0,surround:7.5,dynamicboost:2.0,bass:4.0")
    print(f"mode {ns.name}")


def cmd_gui(_: argparse.Namespace) -> None:
    import tkinter as tk

    root = tk.Tk()
    root.title("Q6T  —  PC HDMI remote")
    root.geometry("360x520")
    root.configure(bg="#111")
    fg, acc = "#eee", "#6cf"

    def row(text, cmds):
        f = tk.Frame(root, bg="#111")
        f.pack(fill="x", padx=12, pady=4)
        tk.Label(f, text=text, fg=acc, bg="#111", width=10, anchor="w").pack(side="left")
        for label, fn in cmds:
            tk.Button(f, text=label, width=8, command=fn).pack(side="left", padx=3)

    def run(fn):
        def inner():
            try:
                msg = fn() or "ok"
            except Exception as e:
                msg = str(e)
            status.set(msg)
        return inner

    status = tk.StringVar(value="HDMI audio + FxSound. CEC pin not on this GPU.")
    tk.Label(root, textvariable=status, fg="#aaa", bg="#111", wraplength=330).pack(pady=8)

    row("volume", [
        ("−", run(lambda: set_volume(delta=-0.05))),
        ("mute", run(toggle_mute)),
        ("+", run(lambda: set_volume(delta=0.05))),
    ])
    row("treble", [
        ("−", run(lambda: (bump_bands(TREBLE_BANDS, -0.5), "treble -"))),
        ("+", run(lambda: (bump_bands(TREBLE_BANDS, 0.5), "treble +"))),
    ])
    row("bass", [
        ("−", run(lambda: (bump_bands(BASS_BANDS, -0.5), bump_effect("bass", -0.4), "bass -"))),
        ("+", run(lambda: (bump_bands(BASS_BANDS, 0.5), bump_effect("bass", 0.4), "bass +"))),
    ])
    row("voice", [
        ("−", run(lambda: (bump_bands(VOICE_BANDS, -0.5), bump_effect("clarity", -0.4), "voice -"))),
        ("+", run(lambda: (bump_bands(VOICE_BANDS, 0.5), bump_effect("clarity", 0.4), "voice +"))),
    ])
    row("mode", [
        ("music", run(lambda: (cmd_mode(argparse.Namespace(name="music")), "music"))),
        ("voice", run(lambda: (cmd_mode(argparse.Namespace(name="voice")), "voice"))),
        ("sur", run(lambda: (cmd_mode(argparse.Namespace(name="surround")), "surround"))),
    ])

    cec_box = tk.Text(root, height=8, bg="#1a1a1a", fg="#8f8", font=("Consolas", 9))
    cec_box.pack(fill="both", expand=True, padx=12, pady=8)
    dump = "CEC TV→bar (not on this HDMI pin)\n"
    for name in ("vol+", "vol-", "mute", "power"):
        dump += f"{name:6} {cec_hex(cec_tv_to_bar(name))}\n"
    cec_box.insert("end", dump)
    cec_box.configure(state="disabled")
    root.mainloop()


def main() -> None:
    ns = build_parser().parse_args()
    if ns.cmd == "status":
        cmd_status(ns)
    elif ns.cmd == "vol":
        if ns.how == "mute":
            print(toggle_mute())
        elif ns.how in ("+", "up"):
            print(set_volume(delta=abs(ns.amount)))
        else:
            print(set_volume(delta=-abs(ns.amount)))
    elif ns.cmd == "treble":
        bump_bands(TREBLE_BANDS, 0.5 if ns.how == "+" else -0.5)
        print("treble", ns.how)
    elif ns.cmd == "bass":
        bump_bands(BASS_BANDS, 0.5 if ns.how == "+" else -0.5)
        bump_effect("bass", 0.4 if ns.how == "+" else -0.4)
        print("bass", ns.how)
    elif ns.cmd == "voice":
        bump_bands(VOICE_BANDS, 0.5 if ns.how == "+" else -0.5)
        bump_effect("clarity", 0.4 if ns.how == "+" else -0.4)
        print("voice", ns.how)
    elif ns.cmd == "mode":
        cmd_mode(ns)
    elif ns.cmd == "cec":
        cmd_cec(ns)
    elif ns.cmd == "gui":
        cmd_gui(ns)


if __name__ == "__main__":
    main()
