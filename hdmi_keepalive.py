#!/usr/bin/env python3
"""Hold a shared-mode WASAPI stream on EP-HDMI-RX so NVIDIA does not unplug HDMI audio.

Owner: Christopher Patrick Urias. MIT License.

Plays an inaudible 18 Hz tone well below the Q6T bar/sub. Restarts on device drop.
"""
from __future__ import annotations

import time
from pathlib import Path

LOG = Path(__file__).with_name("hdmi_keepalive.log")
HDMI = "EP-HDMI-RX"
SR = 48000


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def main() -> None:
    import numpy as np
    import soundcard as sc

    t = np.arange(SR, dtype=np.float32) / np.float32(SR)
    tone = (np.sin(np.float32(2.0 * np.pi * 18.0) * t) * np.float32(1e-4)).astype(
        np.float32
    )
    block = np.stack([tone, tone], axis=1)
    log("keepalive start")
    while True:
        try:
            spk = sc.get_speaker(HDMI)
            log(f"playing on {spk.name}")
            with spk.player(samplerate=SR, channels=2, blocksize=4096) as player:
                while True:
                    player.play(block)
        except Exception as e:
            log(f"drop {type(e).__name__}: {e}")
            time.sleep(1.5)


if __name__ == "__main__":
    main()
