# Regression check — 2026-10-23

`evaluation_regression.py` speaks one evaluation suite through one engine, using the app's worker pool, and compares
the result with a saved baseline. Run it before a dependency or checkpoint pin changes: save a baseline, make the
change, then check again. CI installs core requirements only and runs no real engine, so this is the check that
exercises them.

```bash
.venv\Scripts\python.exe evaluation_regression.py save-baseline --suite neutral-clone-three-line --engine kokoro
.venv\Scripts\python.exe evaluation_regression.py check --suite neutral-clone-three-line --engine kokoro
```

Options: `--repeats N` (default: the suite's repeat count) and `--out-dir` (default
`out/regression/runs/<engine>-<suite>-<time>`).

## What it records for each clip

- Whether the engine spoke it: `ok`, `failed` with the reason, or `unavailable` (not installed, or a language the
  engine does not claim).
- The word error rate. The recognizer is the one the language screen uses (faster-whisper tiny, language forced).
  Chinese and Japanese are scored by character.
- The speaker proxy against `reference/af_heart.wav`, for the clone engines only (`qwen3-tts-1.7b-base` and
  `qwen3-tts-0.6b-base`).
- The clip's length.

## How a check compares with the baseline

Only clips that both runs spoke are compared.

| Change | Regression when |
|---|---|
| A clip stops speaking | It spoke in the baseline and now fails or is unavailable |
| Word error | The mean rises by more than 0.10 |
| Speaker proxy | The mean falls by more than 0.05 |
| Length | A clip changes by more than 25% |
| Suite changed | The set of clips differs. Save a new baseline |

The margins are starting points, set before any run. They have not been measured against repeated runs of one engine,
so a single run can move without a real change. The means cover the whole suite, so one bad clip can be diluted; the
per-clip rows in `result.json` show each one.

Exit codes: `0` nothing beyond the margins, `3` a regression, `2` no baseline or a usage problem. A check with no
baseline stops before speaking anything.

Direction and cues are not sent. The check covers the words, the voice and the length, not acting. Kokoro clips use
the first installed voice pack for each language.

## Checked on the development machine

- `neutral-clone-three-line` on Kokoro: 9 clips spoken, mean word error 0.000, mean length 3.25 s. The check against
  its baseline passed.
- `neutral-clone-three-line` on Qwen3-TTS 0.6B Base: 9 clips spoken, mean word error 0.000, mean speaker proxy 0.895
  (the calibrated "likely same voice" band starts at 0.847), mean length 2.8 s. The check against its baseline passed.

A first Kokoro run wrote every clip of the suite to one file name, because all three cases share a language. The
recognizer then heard the wrong sentence for two cases, which showed as word errors of 0.89 and 1.0. Clips are now
named by case. The language screen keeps its own names.

Faults such as a broken engine, a dropped word or a changed length are covered by tests that inject them. They were
not produced by breaking a real dependency.

## Where the results go

- `out/regression/runs/<engine>-<suite>-<time>/`: the clips, the recognizer's manifest and `result.json`, with each
  clip's heard text.
- `out/regression/baselines/<engine>__<suite>.json`: the saved baseline. These are local files under `out/`.

## Not included

- The in-app benchmark runner. The suites are served at `/api/evaluations/suites`, but nothing runs them across
  engines from the app yet.
- Any judgment of listening quality, likeness or acting.
- CI runs. No real engine runs there.
