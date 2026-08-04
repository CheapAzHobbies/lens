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
