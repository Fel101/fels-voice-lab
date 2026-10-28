# Language screen

Recorded 2026-10-22 on the service device `cuda` (AMD Radeon AI PRO R9700). Generated from [evidence/language-matrix.json](evidence/language-matrix.json) by `python language_matrix.py report`.

Each cell is the median word error rate (character error rate for Chinese and Japanese) over the generations. The screen asks one question: after the engine speaks the sentence, does a speech recognizer write back the same words? It does not judge accent, voice quality, likeness or acting, and it is not a listening test.

| Engine | Device | English | British English | Spanish | French | German | Italian | Portuguese | Russian | Chinese* | Japanese* | Korean | Hindi | Arabic |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Kokoro 82M | cuda | 0.00 understood | 0.00 understood | 0.00 understood | 0.00 understood | — | unavailable | unavailable | — | — | — | — | unavailable | — |
| Chatterbox Turbo 350M | cuda | 0.00 understood | — | — | — | — | — | — | — | — | — | — | — | — |
| Chatterbox 500M | cuda | 0.00 understood | — | — | — | — | — | — | — | — | — | — | — | — |
| Breeze TTS 2 BF16 | cuda | 0.00 understood | — | — | — | — | — | — | — | 0.07 understood | — | — | — | — |
| Qwen3-TTS 1.7B VoiceDesign | cuda | 0.00 understood | — | 0.00 understood | 0.09 understood | 0.00 understood | 0.25 partial | 0.22 partial | 0.00 understood | 0.07 understood | 0.12 understood | 0.00 understood | — | — |
| Qwen3-TTS 1.7B Base | cuda | 0.00 understood | — | 0.00 understood | 0.00 understood | 0.00 understood | 0.00 understood | 0.67 not understood | 0.00 understood | 0.07 understood | 0.38 partial | 0.00 understood | — | — |
| Qwen3-TTS 0.6B Base | cuda | 0.00 understood | — | 0.00 understood | 0.00 understood | 0.00 understood | 0.00 understood | 0.56 not understood | 0.00 understood | 0.07 understood | 0.12 understood | 0.14 understood | — | — |
| Qwen3-TTS 1.7B CustomVoice | cuda | 0.00 understood | — | 0.00 understood | 0.27 partial | 0.11 understood | 0.25 partial | 0.22 partial | 0.00 understood | 0.14 understood | 0.38 partial | 0.14 understood | — | — |
| VoxCPM2 2B | cuda | 0.00 understood | — | 0.00 understood | 0.09 understood | 0.00 understood | 0.00 understood | 0.44 not understood | 0.00 understood | 0.00 understood | 0.38 partial | 0.29 partial | inconclusive | 0.00 understood |
| Fish Audio Drama 3 (35B MoE / Q4_K) | Vulkan1 | 0.00 understood | — | 0.09 understood | 0.09 understood | 0.00 understood | 0.00 understood | 0.11 understood | 0.00 understood | 0.07 understood | 0.38 partial | 0.43 not understood | inconclusive | 0.00 understood |

Cells: 56 understood, 10 partial, 4 not understood, 0 failed, 3 unavailable, 2 inconclusive.

Key: `0.04 understood` means the median error rate is at most 0.20; `partial` is at most 0.40; `not understood` is above that. `failed` means generation raised an error. `unavailable` means the installation, voice pack or checkpoint lacks the language. `inconclusive` means the recognizer wrote the speech in other letters, so no word error is measured.
 `(2/3)` means only some generations finished. `—` means the engine does not claim the language, so it was not run.

\* Scored by character error rate, because these scripts have no spaces between words.

## Per engine

- **Kokoro 82M** — devices: cuda.
  Scene picker: American English, Brazilian Portuguese, British English, English, French, Hindi, Italian, Spanish.
  Screened: English, British English, Spanish, French.
  Not run or not scored: Hindi: no Kokoro voice pack for this language in the installed snapshot; Italian: no Kokoro voice pack for this language in the installed snapshot; Portuguese: no Kokoro voice pack for this language in the installed snapshot.
- **Chatterbox Turbo 350M** — devices: cuda.
  Scene picker: English.
  Screened: English.
- **Chatterbox 500M** — devices: cuda.
  Scene picker: English.
  Screened: English.
- **Breeze TTS 2 BF16** — devices: cuda.
  Scene picker: English.
  Documented: Chinese.
  Screened: English, Chinese.
- **Qwen3-TTS 1.7B VoiceDesign** — devices: cuda.
  Scene picker: Chinese, English, French, German, Italian, Japanese, Korean, Portuguese, Russian, Spanish.
  Screened: English, Spanish, French, German, Italian, Portuguese, Russian, Chinese, Japanese, Korean.
- **Qwen3-TTS 1.7B Base** — devices: cuda.
  Scene picker: Chinese, English, French, German, Italian, Japanese, Korean, Portuguese, Russian, Spanish.
  Screened: English, Spanish, French, German, Italian, Portuguese, Russian, Chinese, Japanese, Korean.
- **Qwen3-TTS 0.6B Base** — devices: cuda.
  Scene picker: Chinese, English, French, German, Italian, Japanese, Korean, Portuguese, Russian, Spanish.
  Screened: English, Spanish, French, German, Italian, Portuguese, Russian, Chinese, Japanese, Korean.
- **Qwen3-TTS 1.7B CustomVoice** — devices: cuda.
  Scene picker: Chinese, English, French, German, Italian, Japanese, Korean, Portuguese, Russian, Spanish.
  Screened: English, Spanish, French, German, Italian, Portuguese, Russian, Chinese, Japanese, Korean.
- **VoxCPM2 2B** — devices: cuda.
  Scene picker: English.
  Upstream card: 30 languages.
  Screened: English, Spanish, French, German, Italian, Portuguese, Russian, Chinese, Japanese, Korean, Arabic.
  Not screened (18): Burmese, Danish, Dutch, Finnish, Greek, Hebrew, Indonesian, Khmer, Lao, Malay, Norwegian, Polish, Swahili, Swedish, Tagalog, Thai, Turkish, Vietnamese.
  Not run or not scored: Hindi: the recognizer wrote latin letters for devanagari speech, so no word error is measured.
- **Fish Audio Drama 3 (35B MoE / Q4_K)** — devices: Vulkan1.
  Scene picker: Afrikaans, Albanian, Amharic, Arabic, Armenian, Assamese, Azerbaijani, Basque, Belarusian, Bengali, Bosnian, Breton, Bulgarian, Burmese, Catalan, Chinese, Croatian, Czech, Danish, Dutch, English, Estonian, Faroese, Filipino, Finnish, French, Galician, Georgian, German, Greek, Gujarati, Haitian Creole, Hebrew, Hindi, Hungarian, Icelandic, Indonesian, Italian, Japanese, Javanese, Kannada, Kazakh, Khmer, Korean, Latin, Latvian, Lithuanian, Malay, Malayalam, Maori, Marathi, Mongolian, Nepali, Norwegian, Norwegian Nynorsk, Pashto, Persian, Polish, Portuguese, Punjabi, Romanian, Russian, Sanskrit, Serbian, Shona, Sindhi, Sinhala, Slovak, Slovenian, Spanish, Swahili, Swedish, Tamil, Telugu, Thai, Tibetan, Turkish, Ukrainian, Urdu, Vietnamese, Welsh, Yiddish, Yoruba.
  Screened: English, Spanish, French, German, Italian, Portuguese, Russian, Chinese, Japanese, Korean, Arabic.
  Not screened (71): Afrikaans, Albanian, Amharic, Armenian, Assamese, Azerbaijani, Basque, Belarusian, Bengali, Bosnian, Breton, Bulgarian, Burmese, Catalan, Croatian, Czech, Danish, Dutch, Estonian, Faroese, Filipino, Finnish, Galician, Georgian, Greek, Gujarati, Haitian Creole, Hebrew, Hungarian, Icelandic, Indonesian, Javanese, Kannada, Kazakh, Khmer, Latin, Latvian, Lithuanian, Malay, Malayalam, Maori, Marathi, Mongolian, Nepali, Norwegian, Norwegian Nynorsk, Pashto, Persian, Polish, Punjabi, Romanian, Sanskrit, Serbian, Shona, Sindhi, Sinhala, Slovak, Slovenian, Swahili, Swedish, Tamil, Telugu, Thai, Tibetan, Turkish, Ukrainian, Urdu, Vietnamese, Welsh, Yiddish, Yoruba.
  Not run or not scored: Hindi: the recognizer wrote latin letters for devanagari speech, so no word error is measured.

## Method

- Recognizer: faster-whisper tiny (multilingual, int8, CPU), the only Whisper model cached on this machine; language forced per clip. Its own error on each language is not measured here, so compare cells with each other, not against an absolute scale.
- Thresholds, fixed before the first recorded run: understood at most 0.20; partial at most 0.40.
- Devices: `cuda` is the name PyTorch gives the accelerator; on this build it is the AMD GPU through ROCm. `Vulkan1` is the Drama runtime's design device, reported by the worker.
- Generations: 3 per cell from suite `multilingual-lines-common` version 2. Engines that take a seed get seeds 43 to 45; Kokoro is deterministic, so its repeats match.
- Sentences contain no numerals: the recognizer writes numbers as digits, which would count as word errors.
- A transcript written in other letters than its sentence (for example Latin letters for Hindi) is inconclusive: no word error is measured for it.
- Sentences (translated by the project; no native speaker has checked them):

| Column | Suite label | Sentence |
|---|---|---|
| en | English | The next train is about to leave the station. |
| en-GB | British English | The next train is about to leave the station. |
| es | Spanish | El próximo tren está a punto de salir de la estación. |
| fr | French | Le prochain train est sur le point de quitter la gare. |
| de | German | Der nächste Zug fährt gleich aus dem Bahnhof ab. |
| it | Italian | Il prossimo treno sta per lasciare la stazione. |
| pt | Portuguese | O próximo trem está prestes a sair da estação. |
| ru | Russian | Следующий поезд скоро отправится от этой станции. |
| zh | Chinese | 下一班火车马上就要离开车站了。 |
| ja | Japanese | 次の電車はまもなく駅を出発します。 |
| ko | Korean | 다음 열차가 곧 이 역을 떠나려고 합니다. |
| hi | Hindi | अगली ट्रेन जल्दी ही स्टेशन से निकलने वाली है। |
| ar | Arabic | القطار التالي على وشك مغادرة المحطة. |

## Unscreened picker entries

The app keeps these scene-picker entries and marks each one `(unscreened)` in every language picker for its engine, because this screen did not clear it. Remove them or re-screen them before any claim that the model speaks the language.

- **Qwen3-TTS 1.7B VoiceDesign**: Italian (partial 0.25), Portuguese (partial 0.22).
- **Qwen3-TTS 1.7B Base**: Japanese (partial 0.38), Portuguese (not understood 0.67).
- **Qwen3-TTS 0.6B Base**: Portuguese (not understood 0.56).
- **Qwen3-TTS 1.7B CustomVoice**: French (partial 0.27), Italian (partial 0.25), Japanese (partial 0.38), Portuguese (partial 0.22).

- **Fish Audio Drama 3 (35B MoE / Q4_K)** labels are metadata: the speech runtime does not receive the label. At the text level, inconclusive: Hindi; not understood: Korean; partial: Japanese; understood: Arabic, Chinese, English, French, German, Italian, Portuguese, Russian, Spanish. 71 labels were not screened.

## Known confounds

- Base checkpoints (1.7B and 0.6B) clone the bundled English reference voice for every language, so their cells measure cross-lingual cloning, not a native reference.
- CustomVoice uses one preset speaker, the first the checkpoint reports, for every language.
- Japanese: the recognizer often writes katakana for kana and kanji words (マモナク for まもなく). The character metric counts that as errors, and several engines score the same clip the same way, which points at spelling rather than speech.
- Korean: spacing differences count as word errors.
- Portuguese: the recognizer often writes "trein" for "trem", so these cells are the most sensitive to recognizer quality.

## Limits

- This is a screen. A cell marked understood means the recognizer wrote back the sentence; it says nothing about accent, emotion, identity or whether a fluent speaker would accept the speech.
- One short sentence per language, without numbers or names. Longer text, dialects and mixed-language lines are not screened.
- The cells were generated on the service device above. CPU and other accelerators are not part of this matrix.
- Drama 3's language label is stored with each job, but neither Drama path sends it to the speech runtime. Perform and scenes lose it in `EnginePool.speak`, and the studio's `stream_drama3` never reads it. Its cells therefore measure the text alone.
- The scene picker lists what the app accepts for each engine, which can differ from what was screened. Only the cells marked understood are screened results; the others are untested, unavailable or failed.
