# Reference screen — 2026-10-23

Before a reference clip is saved, the preview shows a quality score and plain warnings. The screen reads the
processed clip that would be saved: after trimming, mono conversion, resampling and any noise-reduction preview.
It is advisory. It never blocks a save. The existing intake rules are unchanged: a clip silent or quieter than
−60 dBFS RMS is still refused, and the transcript and origin requirements stay as they were.

## What it checks

| Check | What it measures | Warns when | Penalty |
|---|---|---|---|
| Clipping | Samples at or above 0.999 of full scale | Any clipped sample. Over 0.1% of samples counts as heavy | 15, or 25 when heavy |
| Noise floor | The quietest 10% of 100 ms windows (overlapping, 10 ms apart), against the speech level | The floor is above −50 dBFS, or speech sits less than 20 dB above it | 15 |
| Reverb | How fast the level falls after each burst of speech, from 15 dB to 5 dB above the floor. Six 10 dB spans give an RT60 estimate | The estimate is 0.5 s or more | 15 |
| Background tones | Steady spectral peaks in the quieter parts: 15 dB above their neighbourhood and within 3 dB across frames | Any steady tone, such as hum, whine or music under the speech | 15 |
| Speakers | The speech split into up to four parts of at least 3 s. Each part is scored against the rest with the proxy speaker encoder | A part falls in the encoder's "likely different voice" band | 30 |

The score starts at 100 and subtracts the penalties of the warnings. The verdict is `good` with no warnings,
`review` when the score is 60 or more, and `poor` below 60. Each warning says what was measured, so the listener
knows what to listen for.

## Where it appears

- The preview line in the reference panel reads "Quality 85/100, review." and then each warning.
- The save response carries the same screen as `quality` on the reference. It is not written to the database, so a
  saved reference does not keep its score. Reopen it later and the score is gone.

## Checked on the development machine

- Synthetic speech-like clips with known answers. A dry clip's reverb estimate is 0.00 s and a 0.2 s room reads
  0.24 s. A 0.9 s room reads 0.87 s. A 1 kHz tone in the pauses is found at 1000 Hz. One clipped sample is counted.
  A clip under one second reports each acoustic check as not checked.
- The 23 reference versions in the development Cast library, read only, with the speaker check on:
  - Nine get a noise warning, with floors from −34 to −47 dBFS and speech 17 to 31 dB above them. Several versions
    share the same audio, so these are fewer distinct clips. Nobody listened to them.
  - None gets a reverb, tone or clipping warning. The largest reverb estimate is 0.36 s.
  - Five references with at least 6 s of speech scored 0.86 to 0.91 across their parts, in the "likely same voice"
    band. The others report the speaker check as not checked.

## Limits

- The thresholds are starting points. None is calibrated against listening tests, so a warning means listen before
  saving, not reject.
- The noise floor is the 10th percentile of 100 ms windows. Pauses of about 0.3 s or more give a fair floor. A clip
  with almost no pauses takes its floor from its quietest speech, so its noise can read higher than it is. Gaps of
  0.15 s sit on the edge of what this measures.
- The reverb estimate needs pauses long enough for the decay to reach the floor. When speech fills the gaps, the
  check reports that it found fewer than three clean decays. It is an estimate, not a measured RT60.
- Background tones catch steady sounds: hum, whine, or a sustained chord. Music that changes under continuous speech
  is not separated from the voice.
- The speaker check uses the proxy encoder and its two bands (`catalog/speaker_similarity.json`). The bands were measured
  on one target voice (af_heart) and whole clips. Parts are at least 3 s long, so a short part can score lower than the
  bands intend. When the encoder is not installed, the check says so.
- The score is not stored and is not a prediction of clone quality. Reference quality drives clone quality, but these
  checks only say what to listen for.

## Not done

- Storing the score on each reference version. That needs a schema change, so it is deferred.
- Listening samples, or any judgment of accent, likeness or acting.
