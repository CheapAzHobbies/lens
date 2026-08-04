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
- `2026-08-04-working.json` — the configuration that was finally judged good
  by ear: analog +18 dB, PipeWire 20%, Lens +12 dB, RNNoise on at 5% dry.

Note when comparing those two: their gain stages are identical, so the
+10.4 dB peak difference between them is a transient in the room during one
capture, not a result. The noise floor difference of -1.4 dB is real and
reproducible. This is what the spread column is for.

These baselines measure the raw capture device, deliberately, with no
processing in the path. They describe what the microphone gives Lens, not
what Lens does with it.

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

## Traps found the hard way

**Pin the sample rate in every capture.** Leaving caps unpinned lets
PipeWire negotiate 44100 and resample from the device's native 48000, which
low-passes around 22 kHz and discards real noise energy above it. The same
quiet room measures -41.90 dBFS at 48 kHz and -44.44 at 44.1 kHz. Every
measurement here pins 48000; several experiments that did not were 2.5 dB
optimistic before this was found.

**The Capture control is not 0 to +30 dB.** It spans roughly -24 to +30 dB
across its 0-63 range, about 0.75 dB per step. An early gain-placement test
assumed the lower half was positive and compared four settings whose totals
were +18, +5.5, +12.2 and +6.5 dB while calling them matched.

## What the hardware turns out to be

Measured, all at matched total analog gain with the rate pinned:

- **Gain placement makes no difference.** Internal Mic Boost at 0, +20 and
  +30 dB, with Capture reduced to compensate, all land within 0.27 dB of
  each other. The noise is the microphone element, not either analog stage,
  so there is nothing left to win by rearranging them.
- **There is one working microphone.** The codec exposes two capture
  sources; `Internal Mic 1` produces digital silence (-120 dBFS).
- **No mains hum, no tonal content at all.**
- Noise floor with +18 dB analog gain: **-41.9 dBFS**, reproducible to
  0.04 dB.

## Where the noise actually is, and what that rules out

Measured per band on a real take (`Lens_VID_0029.mkv`), speech against the
silence in the same file, so both share every gain stage:

| band | share of noise energy | speech-to-noise |
|---|---|---|
| 60-300 Hz | 12.1% | 15-17 dB |
| 300-1200 Hz | **40.7%** | 6-12 dB |
| 1200-4800 Hz | **31.2%** | 4.5-5.4 dB |
| 4800-12000 Hz | 1.4% | 6-9 dB |
| above 12 kHz | 0.7% | 1-2 dB |

Nearly three quarters of the noise is inside the voice. That rules out
every approach based on frequency: a low-pass, a high-shelf, a band-pass or
a gate cannot remove it, because there is no region to cut that is not also
speech. Hours were spent on exactly those before this was measured.

Broadband RMS hides this. It reported 2 dB speech-to-noise on a recording
whose owner said the voice level was fine, because it lumps a wide quiet
band in with a narrow loud one. Always measure SNR per band.

RNNoise separates by what the signal is rather than where it sits. On that
same file: 300-600 Hz went from 11.6 dB SNR to 82.9, 1200-2400 Hz from 4.5
to 71.0, and the floor during silence fell 61.8 dB. That is why it is back
in Lens, and why it is the only processing there.
