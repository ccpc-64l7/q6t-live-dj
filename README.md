# DJ Lab

**Owner: Christopher Patrick Urias.** Free and open source under the [MIT License](LICENSE). Use it, copy it, change it, and share it at no cost.

Live mixer for a Samsung HW-Q60T soundbar. Stereo playback goes out the HDMI port. `live_dj.py` listens on the WASAPI loopback, and Equalizer APO applies the filters. The window is three channel strips: SUB, BAR, and MASTER.

Version 1.0.0. Release: https://github.com/ccpc-64l7/q6t-live-dj/releases/tag/v1.0.0

The math is NumPy on the CPU. This program does not use the graphics card. There is no CUDA, OpenCL, or ROCm path.

## Author

Christopher Patrick Urias wrote this program. He is a programmer and runs [CCPC Repair](https://ccpcrepair.com) in Tahlequah, Oklahoma.

| | |
|---|---|
| Email | [bigcityrooster@gmail.com](mailto:bigcityrooster@gmail.com) |
| GitHub | [ccpc-64l7](https://github.com/ccpc-64l7) |
| ORCID | [0009-0006-1011-8135](https://orcid.org/0009-0006-1011-8135) |

Certifications on the public record:

- [Google IT Support Professional Certificate](https://www.credly.com/badges/de2e05bb-26d8-4066-888d-adc95534a62e) (Google, 21 June 2026). ACE GOOG-0001, 16 recommended semester hours. Course work covers troubleshooting, customer service, networking, operating systems, system administration, and security.
- [Google Cybersecurity Professional Certificate](https://www.credly.com/badges/c6b35942-180b-4f22-803a-f05a1d0646ef) (Google, 21 June 2026). ACE GOOG-0007, 9 recommended semester hours. Course work covers Python, Linux, SQL, SIEM, and intrusion detection.
- [Credly profile](https://www.credly.com/users/christ469131321654)

## License

MIT. See [LICENSE](LICENSE). Copyright 2026 Christopher Patrick Urias.

## Hardware this build targets

Samsung HW-Q60T/ZA and its wireless 6.5 inch sub (PS-WR65BB). No rear-speaker kit. The bar receives stereo PCM at 48 kHz and upmixes it. The wireless sub hands off around 120 Hz. The port is tuned near 47 Hz, with in-room output falling off below the upper 40s. **31.5 Hz is never boosted.**

The author's playback endpoint is AMD HDMI, `EP-HDMI-RX`, id `{0.0.0.00000000}.{dcb13164-03b7-41f4-bca4-5d27388cf70f}`. That id is the HDMI audio device, not a GPU compute device. Set `HDMI_ID` in `live_dj.py` to your own endpoint before you rely on it. `import comtypes` stays above `import soundcard`.

## Install

Python 3.12 on Windows, plus:

```text
pip install -r requirements.txt
```

Equalizer APO has to be installed on the playback device you want to feed. The mixer writes `peace.txt` in the Equalizer APO config directory. `config.txt` should include that file. On a normal install the path is `C:\Program Files\EqualizerAPO\config\peace.txt`.

## Run

```powershell
.\start-live-dj.ps1
```

That starts `pythonw` with no console. From a terminal:

```powershell
python live_dj.py gui
python live_dj.py status
python live_dj.py stop
```

The window opens in HOLD. LIVE DJ auto-EQ stays off until you turn it on. Startup reads the Windows HDMI volume and does not write it. A saved `hdmi_vol` in the prefs file is a record only.

Runtime files (`live_dj.prefs.json`, `live_dj.log`, `live_dj.status.json`, the pid file) stay on the machine. They are not part of this repository.

## Mixer

| Strip | What you grab |
|---|---|
| **SUB** | Wireless sub. Bass, 63 Hz, punch, body, crossover, ceiling, kill low, gain, volume. |
| **BAR** | Front drivers and vocals. Mids, treble, body, voice, air, ceiling, kill mid, kill high, gain, volume. |
| **MASTER** | Whole bar. Width, balance, Windows output, boost, and the master volume fader. |

Gain and the volume fader on one strip are the same level. SUB VOL is −12 to +18 dB. BAR VOL is −12 to +12 dB. The bar path uses +14 dB of makeup and a safe cap of +26 dB, so a bar fader at +12 lands at +26 dB on that bus. MASTER volume is the whole mix, −24 to +24 dB. BAR OUT is the Windows endpoint percent, 0–100, and it moves only when you move that slider.

## Signal path

```text
player
  → Windows default playback (the HDMI bar)
    → Equalizer APO pre-mix (peace.txt)
      → HDMI stereo PCM 48 kHz
        → Samsung HW-Q60T HDMI IN
          → bar upmix
          → ~120 Hz handoff to the wireless sub
```

There is no virtual cable. Loopback capture is post-EQ, so the limiter hears what the bar is being sent. Capture is 8192 frames at 48 kHz.

`write_config()` copies L/R into two buses, low-passes the sub at the crossover (default 120 Hz), high-passes the bar at that same frequency, gives each bus its own preamp, then sums `L=SL+BL` and `R=SR+BR`. A short Haas copy sits at −18 dB. The master preamp is the last `Channel: L R` block.

A full-scale sub peak ducks the wireless sub only. The bar ducks only when energy at or above 300 Hz is full scale. LOCK SUB and LOCK BAR hold that path. 31.5 Hz is never boosted.

## Modes

| Control | What it does |
|---|---|
| **HOLD / NOW** | Opens here. Keeps your mix. |
| **LIVE DJ** | Classify the track and morph the curve. |
| **STEREO** | Dry auto curve. Knobs still apply. |
| **A / B** | A is current. B is the NOW freeze. |
| **BLAST** | Indoor hits. Does not boost 31 Hz. |
| **SHAKE** | Trailer rumble at 55/75 Hz. |
| **DEEP** | Port-depth peaks at 48 and 72 Hz. |
| **YARD** | Outdoor throw, 2.5 kHz presence. |
| **100** | Push path. Does not set the Windows volume slider. |
| **MOVIE** | Dialogue and FX on the bar. |
| **SAFE** | Caps how hot the staged preamps may sit. |

## Files

| Path | Role |
|---|---|
| `live_dj.py` | Engine and mixer window |
| `start-live-dj.ps1` | pythonw launch |
| `q6t.py` | Older HDMI and FxSound helper |
| `presets/` | Saved Equalizer APO graphs |
| `requirements.txt` | Python packages this build uses |
| `start-ramdisk.ps1` | Optional ImDisk layout from an older machine. The author does not use it. The live graph is `peace.txt` itself. |

## What this is not

- Not a GPU DSP port.
- Not a VST host, virtual cable, or Voicemeeter.
- Not a reader of track titles. It classifies the audio.
- Not a replacement for the bar's own crossover. The split is so the sub and the bar can be gained separately before the bar splits again.
- Not a remote for sound mode or woofer level. The HW-Q60T has no Wi-Fi, and HDMI-CEC from this GPU does not carry those commands.
