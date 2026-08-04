#!/usr/bin/env python3
"""Measure this machine's capture path and write a comparable baseline.

Exists because a whole day of audio debugging was spent acting on numbers
that were not comparable: gain stages moved between measurements without
being recorded, and a frequency response was once "measured" with ambient
room noise, which is not an excitation. Every record here therefore carries
the full gain chain and the excitation used, or it is not a measurement.

Usage:
    audio_baseline.py                 measure and print
    audio_baseline.py --save NAME     also write baselines/NAME.json
    audio_baseline.py --compare FILE  diff against a previous baseline
"""
import argparse
import datetime
import json
import pathlib
import re
import subprocess
import sys
import wave

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib          # noqa: E402

Gst.init(None)
UNKNOWN = "unknown"


def sh(*args):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=4).stdout
    except Exception:
        return ""


def default_source():
    return sh("pactl", "get-default-source").strip() or UNKNOWN


def gain_stages(source):
    """Every stage that can multiply the signal, queried not inferred.

    Anything Linux does not expose is reported as unknown rather than
    filled in with a plausible number.
    """
    stages = {}
    stages["microphone_element"] = {
        "value": UNKNOWN, "note": "not exposed by ALSA or PipeWire"}

    card = source_card(source)
    for card in ([card] if card else []):
        for ctl in ("Internal Mic Boost", "Mic Boost", "Capture"):
            out = sh("amixer", "-c", card, "sget", ctl)
            if "Simple mixer control" not in out:
                continue
            db = re.search(r"(?:Front Left|Mono):.*?\[(-?[\d.]+)dB\]", out)
            pct = re.search(r"(?:Front Left|Mono):.*?\[(\d+)%\]", out)
            lim = re.search(r"Limits:.*?(\d+) - (\d+)", out)
            raw = re.search(r"(?:Front Left|Mono):\s*Capture (\d+)", out)
            stages[f"alsa_card{card}_{ctl.replace(' ', '_').lower()}"] = {
                "db": float(db.group(1)) if db else UNKNOWN,
                "percent": int(pct.group(1)) if pct else UNKNOWN,
                "raw": int(raw.group(1)) if raw else UNKNOWN,
                "range": [int(lim.group(1)), int(lim.group(2))] if lim else UNKNOWN,
            }

    vol = sh("pactl", "get-source-volume", source)
    m = re.search(r"/\s*(\d+)%\s*/\s*(-?[\d.]+) dB", vol)
    # PipeWire's volume is two stages at once: it attenuates in software
    # always, and only starts lowering the hardware control below roughly
    # 30 percent. Its dB figure is its own scale and does NOT match the
    # ALSA dB above. Recorded as-is, and labelled, rather than reconciled.
    stages["pipewire_source_volume"] = {
        "percent": int(m.group(1)) if m else UNKNOWN,
        "db_pulse_scale": float(m.group(2)) if m else UNKNOWN,
        "note": "software attenuation plus hardware control below ~30%; "
                "its dB scale is not the ALSA dB scale",
    }
    stages["pipewire_source_muted"] = (
        sh("pactl", "get-source-mute", source).replace("Mute:", "").strip()
        or UNKNOWN)

    try:
        cfg = json.loads((pathlib.Path.home() / ".config/lens/settings.json")
                         .read_text())
        stages["lens_application_gain"] = {
            "linear": cfg.get("mic_gain", UNKNOWN),
            "requested_rate": cfg.get("mic_rate") or "automatic",
            "muted": not cfg.get("mic_enabled", True),
        }
    except Exception:
        stages["lens_application_gain"] = UNKNOWN
    stages["encoder"] = {"codec": "aac", "bitrate": 128000, "gain": "none"}
    return stages


def source_card(source):
    """Which ALSA card the capture source actually lives on.

    Not the first card with a codec file. An early version of this tool
    reported the Nvidia HDMI codec as the microphone's, because it took
    card 0 and stopped: precisely the kind of plausible wrong value these
    rules exist to prevent.
    """
    block, seen = [], False
    for line in sh("pactl", "list", "sources").splitlines():
        if line.strip() == f"Name: {source}":
            seen = True
        elif seen and line.startswith("Source #"):
            break
        elif seen:
            block.append(line.strip())
    for line in block:
        m = re.match(r'alsa\.card = "(\d+)"', line)
        if m:
            return m.group(1)
    return None


def system_facts(source):
    f = {"source": source}
    info = sh("pactl", "info")
    for line in info.splitlines():
        if line.startswith("Server Name:"):
            f["backend"] = line.split(":", 1)[1].strip()
        if line.startswith("Server Version:"):
            f["backend_version"] = line.split(":", 1)[1].strip()
    card = source_card(source)
    if card is None:
        f["codec"] = UNKNOWN
        f["codec_note"] = "could not determine which ALSA card the source uses"
    else:
        f["alsa_card"] = card
        codec = pathlib.Path(f"/proc/asound/card{card}/codec#0")
        if codec.exists():
            for line in codec.read_text().splitlines()[:5]:
                if line.startswith("Codec:"):
                    f["codec"] = line.split(":", 1)[1].strip()
        hw = pathlib.Path(f"/proc/asound/card{card}/pcm0c/sub0/hw_params")
        if hw.exists():
            txt = hw.read_text()
            if "closed" not in txt:
                for key in ("format", "rate", "channels", "period_size",
                            "buffer_size"):
                    mm = re.search(rf"^{key}:\s*(\S+)", txt, re.M)
                    if mm:
                        f[f"alsa_{key}"] = mm.group(1)
    f.setdefault("codec", UNKNOWN)
    mods = [l for l in sh("pactl", "list", "short", "modules").splitlines()
            if any(k in l.lower()
                   for k in ("echo", "noise", "agc", "filter-chain"))]
    f["system_processing"] = [l.split()[1] for l in mods] if mods else []
    return f


def capture(source, seconds, path):
    """Record raw, with no processing of any kind in the path."""
    pipe = Gst.parse_launch(
        f"pulsesrc device={source} ! audioconvert ! "
        f"audio/x-raw,format=S16LE,channels=1,rate=48000 ! "
        f"wavenc ! filesink location={path}")
    pipe.set_state(Gst.State.PLAYING)
    bus = pipe.get_bus()
    deadline = GLib.get_monotonic_time() + int(seconds * 1e6)
    while GLib.get_monotonic_time() < deadline:
        bus.timed_pop_filtered(100 * Gst.MSECOND, Gst.MessageType.ERROR)
    pipe.send_event(Gst.Event.new_eos())
    bus.timed_pop_filtered(5 * Gst.SECOND, Gst.MessageType.EOS)
    pipe.set_state(Gst.State.NULL)


def analyse(path):
    import numpy as np
    with wave.open(str(path)) as w:
        rate = w.getframerate()
        d = np.frombuffer(w.readframes(w.getnframes()),
                          dtype=np.int16).astype(float) / 32768.0
    if len(d) < rate:
        return None

    def db(x):
        return 20 * np.log10(x) if x > 1e-12 else -120.0
    win = 4096
    frames = [d[i:i + win] for i in range(0, len(d) - win, win)]
    rms = np.array([np.sqrt((f ** 2).mean()) for f in frames])
    spec = np.zeros(win // 2 + 1)
    for f in frames:
        spec += np.abs(np.fft.rfft(f * np.hanning(win))) ** 2
    spec /= max(len(frames), 1)
    fr = np.fft.rfftfreq(win, 1.0 / rate)

    def band(lo, hi):
        m = (fr >= lo) & (fr < hi)
        return db(np.sqrt(spec[m].sum()) / win) if m.any() else -120.0
    total = db(np.sqrt(spec.sum()) / win)
    return {
        "noise_floor_dbfs": round(db(float(np.median(rms))), 2),
        "quietest_frame_dbfs": round(db(float(rms.min())), 2),
        "rms_dbfs": round(db(float(np.sqrt((d ** 2).mean()))), 2),
        "peak_dbfs": round(db(float(np.abs(d).max())), 2),
        "clipping_percent": round(100.0 * float((np.abs(d) >= 0.999).sum())
                                  / len(d), 4),
        "dc_offset": round(float(d.mean()), 6),
        "hum_50hz_rel_db": round(band(48, 52) - total, 2),
        "hum_60hz_rel_db": round(band(58, 62) - total, 2),
        "seconds": round(len(d) / rate, 2),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=5.0)
    ap.add_argument("--save")
    ap.add_argument("--compare")
    ap.add_argument("--excitation", default="silence",
                    help="what produced the signal; 'silence' means a quiet "
                         "room and is valid ONLY for noise measurements")
    args = ap.parse_args()

    try:
        import numpy  # noqa: F401
    except ImportError:
        sys.exit("needs python3-numpy")

    src = default_source()
    record = {
        "date": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "excitation": args.excitation,
        "excitation_valid_for": (
            ["noise_floor", "dc_offset", "hum", "clipping"]
            if args.excitation == "silence"
            else ["depends on excitation"]),
        "method": (f"{args.samples} independent captures of {args.seconds}s, "
                   f"raw pulsesrc with no processing elements, S16LE mono "
                   f"48kHz, median of 4096-sample frame RMS for noise floor"),
        "system": system_facts(src),
        "gain_stages": gain_stages(src),
        "samples": [],
    }
    tmp = pathlib.Path("/tmp") / f"lens-baseline-{id(record)}.wav"
    for i in range(args.samples):
        capture(src, args.seconds, tmp)
        got = analyse(tmp)
        if got is None:
            sys.exit("capture produced too little audio")
        record["samples"].append(got)
    tmp.unlink(missing_ok=True)

    import numpy as np
    stats = {}
    for key in record["samples"][0]:
        vals = [s[key] for s in record["samples"]]
        stats[key] = {
            "mean": round(float(np.mean(vals)), 3),
            "min": round(float(np.min(vals)), 3),
            "max": round(float(np.max(vals)), 3),
            "stdev": round(float(np.std(vals)), 3),
            "spread": round(float(np.max(vals) - np.min(vals)), 3),
        }
    record["statistics"] = stats

    print(json.dumps(record, indent=2))
    if args.save:
        out = pathlib.Path(__file__).resolve().parent.parent / "baselines"
        out.mkdir(exist_ok=True)
        p = out / f"{args.save}.json"
        p.write_text(json.dumps(record, indent=2) + "\n")
        print(f"\nwritten: {p}", file=sys.stderr)
    if args.compare:
        old = json.loads(pathlib.Path(args.compare).read_text())
        print("\n--- against %s (%s) ---" % (args.compare, old.get("date")),
              file=sys.stderr)
        for key in stats:
            a = old.get("statistics", {}).get(key, {}).get("mean")
            b = stats[key]["mean"]
            if a is None:
                continue
            delta = b - a
            spread = max(stats[key]["spread"],
                         old["statistics"][key].get("spread", 0))
            verdict = ("within sample spread, not significant"
                       if abs(delta) <= spread else "SIGNIFICANT")
            print(f"  {key:24s} {a:+9.2f} -> {b:+9.2f}  "
                  f"({delta:+.2f}, {verdict})", file=sys.stderr)


if __name__ == "__main__":
    main()
