# Audio baselines

Committed measurements of the capture path, so a change can be compared
against a recorded number instead of a memory of how something sounded.

Each file records every gain stage, the excitation used, and the method.
A measurement without those is not comparable and should not be used to
justify a change.

## Taking one

    tools/audio_baseline.py --samples 3 --seconds 5 \
        --excitation "silence (quiet room)" --save NAME

## Comparing

    tools/audio_baseline.py --compare baselines/NAME.json

The comparison marks a difference significant only when it exceeds the
spread across samples in either run. Anything smaller is noise in the
measurement, not a result.

## Excitation

`silence` is valid for noise floor, DC offset, hum and clipping only.
It is **not** valid for frequency response: a quiet room is not flat, and
treating it as one produced a confident, entirely fictional 18 dB treble
rolloff earlier in this project. Frequency response needs a known signal.

## Files

- `2026-08-04-as-found.json` — the state after the preamp was reduced from
  +30 dB to +18 dB. Realtek ALC285, PipeWire 1.0.5, no system processing
  loaded.

## Findings so far

**There is no mains hum.** Measured at 0.18 Hz resolution, exactly 60.00 Hz
sits at -90 dBFS and 120 Hz at -103 dBFS. An earlier metric reported "60 Hz
hum at -25 dB" because it measured the energy in a 4 Hz band relative to the
whole spectrum, which detects broadband noise rather than a tone.

**Tone detection needed three attempts to become trustworthy**, and each
failure is why the current version looks the way it does:

1. Band energy relative to total. Detects noise, not tones. Reported hum
   that was not there.
2. Peak-to-median in the band. The maximum of a dozen noise bins sits
   several dB above their median by chance, so it read 7-10 dB at both 50
   and 60 Hz on a spectrum with no line at either.
3. Mean-to-median with a fixed threshold, from a single FFT window. One
   window leaves every bin with full chi-squared variance, so it found
   between zero and three tones in the same room at frequencies varying by
   22 Hz.

The version in the tool averages ten overlapping windows, compares the mean
in band against the median of the neighbourhood, and derives its detection
threshold from control frequencies measured in the same capture. Controls
read within half a decibel of zero, which is what says the number can be
believed.

**Charger state.** Unplugging lowered the noise floor by 0.79 dB, which is
significant against a sample spread of 0.04 dB but too small to be worth
changing how you work. It does not affect any tonal content, because there
is none.

**Not yet known:** the microphone element's own contribution, which neither
ALSA nor PipeWire exposes; and the frequency response of the capture path,
which needs a known excitation played through the speakers and has only been
measured once, roughly.
