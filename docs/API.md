# HTTP API

The Voice Lab is one small web service on your own computer. Its pages talk to it, and so can a script or another program.
This page says how. [`openapi.json`](../openapi.json) describes the same calls for tools that read OpenAPI, and a running
service answers `GET /api/openapi.json` with it.

[The basics](#the-basics) · [Quick start](#quick-start) · [Speak text](#speak-text) ·
[Speak as a Cast voice](#speak-as-a-cast-voice) · [Command-line tool](#command-line-tool) · [Reference](#reference) ·
[Keeping this page true](#keeping-this-page-true)

## The basics

**Address.** `http://127.0.0.1:8765`, the known port: the Lab's window, `serve.bat` and `serve.py` all listen there. When
another program has that port, the Lab takes a free one and says so on **System & backups**, under **Connect other apps**;
`serve.py --port 8775` asks for another port, and `--port 0` for any free one. Every running Lab service also writes its
address to the address file, `%LOCALAPPDATA%\FelsVoiceLabService\service.json` (`url`, `version`, `pid`, `root` and
`started_at`), and removes it when it stops; a file whose process has gone is stale and is ignored. When several Labs run, it
names the one that started first. It also writes `apps\voice-lab.json` in the suite folder that the apps of Fel's Studio
share ([Connect other apps](CONNECT.md#the-other-apps-of-fels-studio)). A program should look for the Lab in this order, as
the command-line tool does: an address it was given, the `VOICE_LAB_URL` environment variable, the suite's address file, the
Lab's address file, then `http://127.0.0.1:8765`. `GET /api/health` answers with `"app": "Fel's Voice Lab"` and
`"fels": {"contract": 1, "app": "voice-lab", ...}`, so a program can tell the Lab from something else on the port.

**Local only.** The service listens on 127.0.0.1. A request whose `Host` is not `127.0.0.1` or `localhost`, or that a web page
from another site made (it carries a foreign `Origin`, or a `Sec-Fetch-Site` other than `same-origin` or `none`), gets `403`;
another site may only open the Lab's pages in a tab. A script sends neither header and is fine. There is no
sign-in and no key: anything on this computer can call the service, so do not forward or proxy its port.

**JSON.** Send a JSON object with `Content-Type: application/json`. A body may be up to 64 KiB; scene calls take 2 MiB and
reference recordings 32 MiB. Answers are JSON, except where a call returns audio, CSV or a file.

**Errors.** A failure is a JSON object, `{"error": "what went wrong", "code": "busy"}`, with a status. `error` is a sentence
for a person; `code` is for a program, which can act on it without reading the sentence:

| Status | `code` | Means |
|---|---|---|
| `400` | `invalid_request` | The request was refused; the message says why. |
| `403` | `not_local` | The request did not come from this computer, or came from another website's page. |
| `404` | `not_found` | Nothing there: no such route, take, scene or voice. |
| `409` | `busy` | The model worker, or an install, backup or export, is busy. Try again, or send `wait_seconds`. |
| `409` | `already_exists`, `conflict` | Where a route says so: a name that is taken, a manifest that changed. |
| `413` | `too_large` | The body is over its limit. |
| `499` | `cancelled` | Cancelled while it waited or ran. |
| `500` | `failed` | The service or an installed model failed. |
| `502` | `upstream_failed` | GitHub did not answer an update check. |
| `503` | `model_not_installed` | The model is not installed; the message says where to install it. |
| `504` | `timeout` | A model did not answer in time. |

Version 0.3.0 answered `500` for a model that was not installed, and sent no `code`. A body over its limit is read
through before the `413` goes out, so a client that is still sending gets the answer; one that declares more than 512 MiB is
not read, and its client may see the connection reset instead.

**One generation at a time.** The model worker does one load, clip, take, test or install at a time. A request that needs
it while it is busy gets `409` at once, unless its JSON body has `wait_seconds`: then it waits in line for up to that many
seconds (at most 3600) and runs when its turn comes. `GET /api/queue` shows what runs and what waits, and
`POST /api/queue/{request_id}/cancel` takes a request out of the line or stops it while it runs; the request then answers
`499`. A scene batch's lines wait behind other requests. Send your own `X-Request-Id` so you know which id to cancel.

**Request ids.** Every answer carries `X-Request-Id`. Send your own (letters, digits, `.`, `_` and `-`, up to 64 characters)
and the same id is in the service log, so your logs and the Lab's can be matched.

**Parts of a recording.** A `GET` that answers with audio, such as `GET /api/takes/{take_id}/audio`, says
`Accept-Ranges: bytes` and reads one `Range: bytes=first-last` (or `first-`, or `-count`): the answer is `206` with that part
and a `Content-Range`, or `416` when the part lies past the end. This is how an `<audio>` player seeks. Several ranges at
once, or an `If-Range`, get the whole file with `200`. A recording made by a `POST` is always sent whole.

**Header text.** A header value that is not plain ASCII, such as a character's name, is percent-encoded as UTF-8, and
`X-Metadata-Encoding` lists which headers were.

**Which routes you can rely on.** The routes in the [reference](#reference) are *documented*: they change only with a new
version and a line in the [changelog](../CHANGELOG.md). The running version is `version` in `GET /api/health`. The
[internal routes](#internal-routes) serve the Lab's own pages and can change at any time.

## Quick start

Check that the service is up:

```powershell
curl.exe -s http://127.0.0.1:8765/api/health
```

Speak a sentence with a built-in voice and keep the file. From PowerShell:

```powershell
$body = @{ text = "Hello from the Lab." } | ConvertTo-Json
Invoke-WebRequest -Uri http://127.0.0.1:8765/api/speak -Method Post -ContentType "application/json" `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -OutFile hello.wav
```

From Python, with nothing installed:

```python
import json
import urllib.request

request = urllib.request.Request(
    "http://127.0.0.1:8765/api/speak",
    data=json.dumps({"text": "Hello from the Lab."}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(request, timeout=600) as response:
    with open("hello.wav", "wb") as output:
        output.write(response.read())
```

Or with the [command-line tool](#command-line-tool):

```powershell
.venv\Scripts\python.exe tools\lab_cli.py speak --text "Hello from the Lab." --out hello.wav
```

## Speak text

[`POST /api/speak`](#post-api-speak) is the plain call: text in, one WAV file out.

- It is **blocking**. The service sends nothing until the clip is finished, then sends the whole 16-bit WAV file in one
  response with a `Content-Length`. There are no chunks, no partial file and nothing to poll. Set your client's timeout for
  the longest clip you expect: the first call for an engine also loads its model, which can take a minute or more.
- `text` (1 to 10,000 characters) is the one field every engine needs. Text over 1,200 characters is split where a scene
  splits a long line, spoken part by part in the same call and joined with a quarter-second pause: still one WAV, and
  `X-Parts` says how many parts. The call can be cancelled between parts.
- `voice` names a voice from [`GET /api/voices`](#get-api-voices): `{"voice": "Nell", "text": "..."}` speaks as the Cast voice
  Nell and keeps nothing, and `{"voice": "af_heart", "text": "..."}` speaks with that Kokoro voice. Or name the model:
  `engine` picks it (default `kokoro`); the rest are that engine's own controls. The reference lists the common ones,
  `GET /api/catalog` lists what each engine supports, and `GET /api/performance/settings` describes its performance controls.
- The answer's headers say how long it took (`X-Gen-Seconds`), how long the clip is (`X-Audio-Seconds`) and which checkpoint
  made it.
- Nothing is saved except what you ask for with `save_voice_as`. To keep a take with its provenance, use the next call.
- [`POST /api/stream`](#post-api-stream) is the separate call for audio as it is made, for Qwen3-TTS. `/api/speak` stays a
  blocking whole-file call; streaming never changes it.

## Speak as a Cast voice

[`POST /api/performance`](#post-api-performance) speaks one line with a voice from your Cast and saves the result as a take,
with its text, settings, model and where the voice came from. Like `/api/speak` it answers with the WAV in one blocking
response.

**1. Find the voice.** [`GET /api/voices`](#get-api-voices) lists every voice that can speak, with the `name` to send back as
`voice`: a character's name for its only voice, or `Character (Label)`. For ids, `GET /api/cast` lists the characters, their
voices (identities) and each voice's recordings; send the identity's `id` as `identity_id`. A voice that clones a recording
must be `approved`, or you must name a `reference_version_id`; a Kokoro voice or a preset voice needs neither.

**A clip without a take.** With `"keep_take": false` nothing is saved in the library: a program that talks all day does not
fill it. The text may then run to 10,000 characters, and `line_id` and `idempotency_key` are not used. The clip is labelled
as AI-generated speech all the same, and `X-Take-Kept: false` says nothing was kept.

**2. Ask for the line.**

```json
{
  "identity_id": "0123456789abcdef0123456789abcdef",
  "line_id": "scene1-001",
  "text": "We made it home.",
  "idempotency_key": "scene1-001-attempt-1"
}
```

`line_id` is your own label, 1 to 100 characters. Takes for the same voice and line are grouped under it, which is how the
Perform page and the scenes find alternatives. The other fields are optional: `reference_version_id`, `language`,
`direction`, `settings`, `use_pronunciations`.

**3. Use the answer.** The body is the WAV. `X-Take-Id` names the saved take: read it with `GET /api/takes/{take_id}`,
download it again from `GET /api/takes/{take_id}/audio`, mark it with `POST /api/takes/{take_id}/update`, or choose it for a
scene line. The other headers are `X-Cast-Character`, `X-Cast-Identity`, `X-Cast-Line-Id`, `X-Audio-Seconds`,
`X-Gen-Seconds`, `X-Voice-Backend`, `X-Model-Checkpoint`, `X-Checkpoint-Revision` and `X-Generation-Mode`.

**4. Retry safely.** `idempotency_key` is any text you choose, up to 200 characters. If the connection drops after the
service saved the take, send the same call with the same key and you get that take back, with `X-Idempotent-Replay: true`,
instead of a second generation. A key used again with different inputs is refused with `400`.

**5. Expect `409` when it is busy.** One generation runs at a time, so a call that arrives during another is refused. Wait a
moment and send it again, with the same `idempotency_key`.

From the command line, the same call by name:

```powershell
.venv\Scripts\python.exe tools\lab_cli.py perform --character Nell --text "We made it home." --line-id scene1-001 --out nell.wav
```

## Edit a saved take

A take can be trimmed, cleared of the silence at its ends, faded, and made louder or quieter, and the result saved as a new
take. The original is never changed.

**1. Look at the take.** [`GET /api/takes/{take_id}/waveform`](#get-api-takes-take_id-waveform) answers the take's peaks, its
levels and where the speech is: enough to draw it or to choose a trim.

**2. Hear the edit.** [`POST /api/takes/{take_id}/edit-preview`](#post-api-takes-take_id-edit-preview) takes the edit as JSON,
for example `{"start_seconds": 0.3, "end_seconds": 1.8, "fade_out_ms": 40, "gain_db": -2}`, and answers the edited WAV.
Nothing is saved. Fields you leave out take their defaults, and a field the service does not know is refused, so a misspelt
one is never ignored silently.

**3. Save it.** [`POST /api/takes/{take_id}/edit`](#post-api-takes-take_id-edit) takes the same JSON, with an optional
`idempotency_key`, and answers `201` with the new take. Its `parent_take_id` is the take you edited and its `edit` says what
was done. It is a `candidate` for the same line with the same text, voice, settings and model; its loudness and checks are
measured again on the new audio, and its word and phoneme times, when the original has them, move with the trim. If the
connection drops, send the call again with the same key and you get that take back, with `200` and `replayed: true`.

The steps run in a fixed order: trim, then auto-trim, then the fades, then the gain. A trim alone copies the samples exactly.
An edit of an edit is allowed. Retiring a take retires its edits, and purging it deletes them. None of these calls reaches an
engine, so they work while the service is generating.

## Command-line tool

`tools/lab_cli.py` calls the service from a terminal or a script. It needs only Python 3.8 or later, not the Lab's packages.

```text
lab_cli.py [--url URL] [--timeout SECONDS] health | speak | perform | voices | cast
```

| Command | What it does |
|---|---|
| `health [--wait SECONDS] [--json]` | Ask `GET /api/health`. With `--wait` it keeps trying until the service answers, for a script that has just started it. |
| `speak --text TEXT --out FILE` | `POST /api/speak`. Also `--text-file FILE`, `--engine`, `--voice` (a name from `voices`, or with `--engine kokoro` a Kokoro voice), `--speed`, `--language`, `--set KEY=VALUE` (repeatable; the value is read as JSON when it can be, else as text), `--request-file FILE` (a whole request body) and `--play`. |
| `perform --text TEXT --out FILE` | `POST /api/performance`. Name the voice with `--character NAME` (and `--voice LABEL` when the character has more than one), or give `--identity-id`. Also `--line-id` (default `cli`), `--reference-version-id`, `--direction`, `--language`, `--setting KEY=VALUE`, `--pronunciations`, `--idempotency-key`, `--no-keep` (keep no take) and `--play`. |
| `voices` | List the voices a program can speak with, by the name to send (`--json` for the raw answer). |
| `cast` | List the characters and voices with their ids (`--json` for the raw answer). |

`--url` is where the service listens (default the `VOICE_LAB_URL` environment variable, else the address file, else
`http://127.0.0.1:8765`; see [the basics](#the-basics)). With neither `--url` nor `VOICE_LAB_URL`, a Lab that is not running is
said plainly, with how to open it.
`--timeout` is how long to wait for an answer: 10 seconds for `health`, `voices` and `cast`, 900 for `speak` and `perform`.
`--json` prints what the call returned as one JSON object instead of a sentence. The WAV is written under a temporary name and
moved into place only when it has arrived whole, so a failed call leaves no half file. `--play` then plays it on this
computer's speakers and waits until it ends (Windows' own player; `afplay`, `paplay` or `aplay` elsewhere).

The exit status is `0` when the call worked, `1` when the service refused it or failed (the message is the service's own,
on standard error), `2` for a mistake in the command line, and `3` when nothing answered.

## Reference

Every documented route, by group, then the internal routes. `{name}` in a path is a value you fill in.

<!-- BEGIN GENERATED REFERENCE: edit api_spec.py, then run tools/build_api_docs.py -->

### Service

Is it running, what is loaded, which engines exist. None of these makes audio, except an engine check.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/health`](#get-api-health) | Say whether the service is ready, and which version it is. |
| GET | [`/api/info`](#get-api-info) | Name the device, the loaded engines, the Kokoro and Phonon voices found and the free memory. |
| GET | [`/api/catalog`](#get-api-catalog) | List every engine the Lab knows, with its support level, pinned checkpoint, capabilities and license. |
| GET | [`/api/engines/status`](#get-api-engines-status) | Say which engines are installed and usable here, and what each check measured. Reads files and stored results only. |
| POST | [`/api/engines/{engine_id}/check`](#post-api-engines-engine_id-check) | Check that one engine can speak now: confirm its files, load it and speak one short clip. |
| POST | [`/api/engines/{engine_id}/install`](#post-api-engines-engine_id-install) | Install an engine with its guided installer (every engine in the catalog has one): read its plan, or run the installer. |
| POST | [`/api/engines/{engine_id}/install/stop`](#post-api-engines-engine_id-install-stop) | Stop a guided model install. An active package command finishes first. Resumable model downloads are kept. |
| POST | [`/api/engines/{engine_id}/uninstall`](#post-api-engines-engine_id-uninstall) | Uninstall a model: read what it would delete, or delete its files from the Lab's folder. |
| POST | [`/api/engines/{engine_id}/device`](#post-api-engines-engine_id-device) | Run one engine on the graphics card or on the CPU from its next load, as the switch on its Models & features card does. |
| POST | [`/api/load`](#post-api-load) | Load an engine into memory ahead of its first use. |
| POST | [`/api/unload`](#post-api-unload) | Unload every engine and free the GPU. |
| GET | [`/api/gpu`](#get-api-gpu) | Say who holds the graphics card: the GPU lock the apps of Fel's Studio share. |
| POST | [`/api/prepare-reference`](#post-api-prepare-reference) | Prepare a reference recording for an engine so its first clone is quick. |
| GET | [`/api/performance/settings`](#get-api-performance-settings) | Describe the settings each engine's performance takes, and the languages its picker offers. |
| GET | [`/api/queue`](#get-api-queue) | Say what the model worker is doing, and what waits for it, in order. |
| POST | [`/api/queue/{request_id}/cancel`](#post-api-queue-request_id-cancel) | Take a request out of the line, or stop it while it runs. |
| GET | [`/api/openapi.json`](#get-api-openapi-json) | Return this API as an OpenAPI 3.1 document. |

<a id="get-api-health"></a>

#### `GET /api/health`

Say whether the service is ready, and which version it is.

**Answer:** `200`, JSON. `status` (`ready`, or `busy` while a model loads or generates), `busy`, `device`, `version`, `workers` (one entry per loaded engine), `last_worker_error`, `last_unload`, `app` (`Fel's Voice Lab`, so a program can tell the Lab from something else on the port), `lab_id` (the same for every service of one Lab folder and library) and `fels` (`contract`, `app` (`voice-lab`), `version` and `api`: how the other apps of Fel's Studio recognise the Lab, fels_suite.py).

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer field | Type |
|---|---|
| `status` | string |
| `busy` | boolean |
| `version` | string |
| `app` | string |
| `lab_id` | string |
| `fels.contract` | integer |
| `fels.app` | string |
| `fels.version` | string |
| `fels.api` | integer |

<a id="get-api-info"></a>

#### `GET /api/info`

Name the device, the loaded engines, the Kokoro and Phonon voices found and the free memory.

**Answer:** `200`, JSON. `device`, `loaded` (the engines in memory), `voices` (Kokoro voice names), `kokoro_languages`, `phonon_voices` (the installed Phonon voices, each with `name`, `group`, `character`, `gender`, `age`, `locale` and `description`) and `free_ram_gb`.

<a id="get-api-catalog"></a>

#### `GET /api/catalog`

List every engine the Lab knows, with its support level, pinned checkpoint, capabilities and license.

**Answer:** `200`, JSON. The engine catalog. An engine's `id` is the `engine` of the speaking calls; only engines whose `integration.service_api` is true answer them. `support` is `supported` (tested for every release on the Lab's test GPU and on the CPU where it runs), `works` (tested when it changes) or `experimental` (may break or have limits, described in `support_note`). Each engine has a `licence` rating and reason, and can have a separate `preset_licence` for its voice catalog. Each engine also has `facts`, what its card on the Models page says, from the catalog, its adapter, its installer and the language screen: `first_model` (marked as a good first model), and `runs` (`cpu`, `gpu`, `gpu_memory_gb`), `voices` (`clones`, `presets`, `designs`, `converts`), `direction` (`takes`, `acts_tags`), `languages` (`offered`, each `label`, `language` and its `screen` result; `filter`, the languages the page's question matches) and `output` (`rating`, the model's licence rating; `preset_rating`, its voice catalog's where that has its own; and `status`, `restricted` or `unknown` as in an export's licence profile), each with its `text` in plain words.

<a id="get-api-engines-status"></a>

#### `GET /api/engines/status`

Say which engines are installed and usable here, and what each check measured. Reads files and stored results only.

**Answer:** `200`, JSON. `device` (the service's: `cuda` for a graphics card, or `cpu`), `busy`, `engines` (one row per catalog engine: `id`, `name`, `integrated`, `installation`, `state`, `check`, `check_outdated`, `min_free_ram_gb`, `device` and, for an integrated engine, `switch`, `device_choice` and `gpu`), `ram`, `install`, `speed` and `store_error`. `installation` holds `installed`, `missing` (the files not found), `environment`, `checkpoint` and, while files are missing from an engine that has a guided installer, `installer`: the launcher in the lab folder that installs it. `min_free_ram_gb` is the free RAM in GB the Lab wants before it loads the engine (`null` when none is set), and `ram` is this computer's `total_gb` and `free_gb`. An engine's `device` is the one its worker is asked for, and `check` is its last check there. `switch` is what its GPU/CPU switch shows (`gpu` or `cpu`), `device_choice` the choice kept for it (`null` when it follows the service), and `gpu` holds `choosable` (GPU can be picked), `reason` (why not), `build` (the PyTorch build in its environment: `cuda`, `rocm`, `cpu` or `null`) and `install` (Install puts in its GPU build). An engine that is not installed and has a guided installer also has `install_size`: `download_bytes` and `disk_bytes` for a first install of the build its Install puts in, from the installer's own numbers (its plan gives the exact figure), or `null` when they cannot be read. `install` is the latest install started from the Models page, or `null` when none has run since the service started: `id`, `engine`, `name`, `nvidia` (requests a GPU build), `state` (`running`, `stopping`, `stopped`, `finished` or `failed`), `phase` (`preparing`, `environment`, `packages`, `downloading`, `unpacking`, `checking`, `finishing`, `waiting` or `null`), `started_at`, `ended_at`, `seconds`, `exit_code`, `lines` (the end of its output) and `log` (all of it, a path under the lab folder). A card that can retry an interrupted install has `install_resume` with its `nvidia` choice and the `phase` where it was interrupted.

<a id="post-api-engines-engine_id-check"></a>

#### `POST /api/engines/{engine_id}/check`

Check that one engine can speak now: confirm its files, load it and speak one short clip.

| Path parameter | Meaning |
|---|---|
| `engine_id` | An engine id from `GET /api/catalog`. |

| Request field | Type | Meaning |
|---|---|---|
| `wait_seconds` | number | Wait in line up to this many seconds (at most 3600) while the model worker is busy, instead of the 409 answered at once without it. See `GET /api/queue`. |
| `wait_priority` | string | `normal` (the default), or `low` to wait behind the others, as a scene batch's lines do. |

**Answer:** `200`, JSON. `check`: `status` (`passed`, `failed` or `not_installed`), `device`, `ran_on`, `checked_at`, `reason`, `load_seconds`, `total_s`, `audio_seconds`, `real_time_factor` and, for a missing install, `missing`. Takes seconds to minutes; the result is kept and shows in `GET /api/engines/status`.

| Also answers | When |
|---|---|
| `400` | The engine is unknown, or is not a speech engine. |
| `409` | The model worker is busy and the request sent no `wait_seconds`, waited that long, or found 20 requests already waiting. Or, with `code` `gpu_busy`, the model would load on the graphics card while another app of Fel's Studio has it to itself (NPC Forge's One model at a time): refused at once, even with `wait_seconds`, and `holder` names that app, what it is doing and since when. |
| `499` | The request was cancelled with `POST /api/queue/{request_id}/cancel`: while it waited, or while it ran. |

<a id="post-api-engines-engine_id-install"></a>

#### `POST /api/engines/{engine_id}/install`

Install an engine with its guided installer (every engine in the catalog has one): read its plan, or run the installer.

| Path parameter | Meaning |
|---|---|
| `engine_id` | An engine id from `GET /api/catalog`. |

| Request field | Type | Meaning |
|---|---|---|
| `dry_run` | boolean | Only read the plan: what the installer would do and whether this computer can take it. Nothing is changed. Default false, which starts the install. |
| `nvidia` | boolean | Install the engine's GPU build for this computer's graphics card (its installer's `--gpu`): NVIDIA's CUDA build, or AMD's ROCm build on a computer whose card is an AMD Radeon. An engine installed with the CPU build of PyTorch can get it afterwards. The Qwen3-TTS models, VoxCPM2 and Breeze TTS 2 run only on a graphics card and always get the GPU build, whatever this says. Default false. |

**Answer:** `200`, JSON. With `dry_run`, `plan`: `engine`, `nvidia`, `ok`, `paragraphs` (the plan, one string for each paragraph or numbered step) and `problems` (why it cannot go ahead; empty when `ok`). Otherwise `install`: the job that started, as in `GET /api/engines/status`, which is the route to poll for its progress. The job runs the script the engine's launcher runs, with `--yes`. While it runs the service refuses every other load, check and clip.

| Also answers | When |
|---|---|
| `400` | The engine is unknown, has no installer (or no GPU build) in the app, or is installed already; or `dry_run` or `nvidia` is not true or false. |
| `409` | An install is already running, or the model worker is busy. |

<a id="post-api-engines-engine_id-install-stop"></a>

#### `POST /api/engines/{engine_id}/install/stop`

Stop a guided model install. An active package command finishes first. Resumable model downloads are kept.

| Path parameter | Meaning |
|---|---|
| `engine_id` | An engine id from `GET /api/catalog`. |

| Request field | Type | Meaning |
|---|---|---|
| `job_id` (required) | string | The install's `id` from its progress. |

**Answer:** `200`, JSON. `install`: the matching job, as in `GET /api/engines/status`. Poll until its state is final.

| Also answers | When |
|---|---|
| `400` | The job id is missing or invalid. |
| `409` | The current install does not match this job. |

<a id="post-api-engines-engine_id-uninstall"></a>

#### `POST /api/engines/{engine_id}/uninstall`

Uninstall a model: read what it would delete, or delete its files from the Lab's folder.

| Path parameter | Meaning |
|---|---|
| `engine_id` | An engine id from `GET /api/catalog`. |

| Request field | Type | Meaning |
|---|---|---|
| `dry_run` | boolean | Only read the plan: what would be deleted and what stays. Nothing is changed. Default false, which deletes. |

**Answer:** `200`, JSON. With `dry_run`, `plan`: `engine`, `name`, `remove` (each folder's `path` in the Lab's folder, its `bytes`, and `link` when it is a link or junction, which is removed as a link and never followed), `keep` (each folder that stays, with `why`), `bytes`, `ok`, `paragraphs` and `problems`. Otherwise `uninstall`: the plan with `removed`, `failed` (each `path` with its `reason`, such as a file another program has open) and `freed` bytes.

| Also answers | When |
|---|---|
| `400` | The engine is unknown, or has no files of its own to delete (another installed model uses them all), or `dry_run` is not true or false. |
| `409` | An install is running, or the model worker is busy. |

A model's files are its checkpoint in `hf-cache`, with its preset voices' repository; the files it keeps elsewhere (Breeze in `breeze/models`, Drama 3 in `out/drama3`); its worker's source folders (VoxCPM2 in `out/voxcpm2`); and its own Python environment (`.venv-kokoro`). What another installed model uses stays: the Chatterbox models share a checkpoint and `.venv-chatterbox`, the Qwen3-TTS models `.venv-fqwen`, which VoxCPM2 also borrows from. The service's own `.venv` and the library are never touched. Every model is unloaded first, and the model worker refuses other work until the files are gone. Install on the model's card puts it back.

<a id="post-api-engines-engine_id-device"></a>

#### `POST /api/engines/{engine_id}/device`

Run one engine on the graphics card or on the CPU from its next load, as the switch on its Models & features card does.

| Path parameter | Meaning |
|---|---|
| `engine_id` | An engine id from `GET /api/catalog`. |

| Request field | Type | Meaning |
|---|---|---|
| `device` (required) | string | `gpu` or `cpu`. |

**Answer:** `200`, JSON. `device`: `engine`, `choice`, `device` (the one its worker is now asked for, `cuda` or `cpu`) and `unloaded` (true when the engine was loaded, so its next request loads it again). The choice is kept in `data/engines/devices.json`. An engine whose environment holds the CPU build of PyTorch runs on the CPU whatever is chosen, until its GPU build is installed.

| Also answers | When |
|---|---|
| `400` | The engine is unknown or runs only on the CPU (Phonon, Drama 3), `device` is not `gpu` or `cpu`, or `gpu` was asked of a service that runs on the CPU. |
| `409` | The model worker is busy. |

<a id="post-api-load"></a>

#### `POST /api/load`

Load an engine into memory ahead of its first use.

| Request field | Type | Meaning |
|---|---|---|
| `engine` | string | An engine id. Default `kokoro`. |
| `profile` | string | Breeze only: its decode profile. Default `eager`. |
| `wait_seconds` | number | Wait in line up to this many seconds (at most 3600) while the model worker is busy, instead of the 409 answered at once without it. See `GET /api/queue`. |
| `wait_priority` | string | `normal` (the default), or `low` to wait behind the others, as a scene batch's lines do. |

**Answer:** `200`, JSON. `engine`, `loaded`, `load_seconds`, `sample_rate`, `memory`, `backend`, `checkpoint`, `checkpoint_revision`, `supported_speakers` and `supported_languages`.

| Also answers | When |
|---|---|
| `500` | The model is installed but could not load or run; `error` says what it reported. |
| `503` | The model is not installed (`code` `model_not_installed`). `error` begins by saying where to install it: Install on its Models & features card, or its launcher in the lab folder with the arguments that choose the model: `tools\setup-kokoro.bat`, `tools\setup-phonon.bat`, `tools\setup-chatterbox.bat` (Chatterbox Turbo, or with `--original` Chatterbox 500M and Voice Conversion), `tools\setup-qwen.bat` with `--model` and the Qwen3-TTS model, `tools\setup-voxcpm.bat`, `tools\setup-breeze.bat` or `tools\setup-drama3.bat`. |
| `409` | The model worker is busy and the request sent no `wait_seconds`, waited that long, or found 20 requests already waiting. Or, with `code` `gpu_busy`, the model would load on the graphics card while another app of Fel's Studio has it to itself (NPC Forge's One model at a time): refused at once, even with `wait_seconds`, and `holder` names that app, what it is doing and since when. |
| `499` | The request was cancelled with `POST /api/queue/{request_id}/cancel`: while it waited, or while it ran. |

<a id="post-api-unload"></a>

#### `POST /api/unload`

Unload every engine and free the GPU.

| Request field | Type | Meaning |
|---|---|---|
| `wait_seconds` | number | Wait in line up to this many seconds (at most 3600) while the model worker is busy, instead of the 409 answered at once without it. See `GET /api/queue`. |
| `wait_priority` | string | `normal` (the default), or `low` to wait behind the others, as a scene batch's lines do. |

**Answer:** `200`, JSON. `{"ok": true}`.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer field | Type |
|---|---|
| `ok` | boolean |

| Also answers | When |
|---|---|
| `409` | A scene batch is being spoken with a model on the graphics card, from its first line to its last, or the worker has other work: `doing` says what the Lab is doing, as the end of "Voice Lab is ..." (`speaking a scene, 12 of 40 lines`), and `code` is `busy`; or the model worker is busy and the request sent no `wait_seconds`, waited that long, or found 20 requests already waiting. Or, with `code` `gpu_busy`, the model would load on the graphics card while another app of Fel's Studio has it to itself (NPC Forge's One model at a time): refused at once, even with `wait_seconds`, and `holder` names that app, what it is doing and since when. |
| `499` | The request was cancelled with `POST /api/queue/{request_id}/cancel`: while it waited, or while it ran. |

The other apps of Fel's Studio call this before NPC Forge takes the graphics card to itself (its One model at a time switch). Without `wait_seconds` a busy Lab answers 409 at once; with it, the request waits its turn in line, but a scene being spoken is refused at once, since it would wait for the whole scene. After the unload the Lab no longer holds the GPU lock of Fel's Studio (`GET /api/gpu`). docs/CONNECT.md says more.

<a id="get-api-gpu"></a>

#### `GET /api/gpu`

Say who holds the graphics card: the GPU lock the apps of Fel's Studio share.

**Answer:** `200`, JSON. `device` (this service's, `cuda` or `cpu`), `held` (whether a model of this service is on the graphics card now), `holders` (each app holding the lock, oldest first: `app`, `name`, `pid`, `what` (the model or the work), `since`, `alone` and `this_service`) and `alone` (the app that has the card to itself or is clearing it for itself, with the same fields, or null). For showing only: a model load decides by the lock itself.

<a id="post-api-prepare-reference"></a>

#### `POST /api/prepare-reference`

Prepare a reference recording for an engine so its first clone is quick.

| Request field | Type | Meaning |
|---|---|---|
| `engine` (required) | string | An engine id that clones from a recording. |
| `path` (required) | string | A recording inside the Lab's reference storage. |
| `reference_text` | string | The exact transcript, which Breeze, Qwen Base and Drama 3 need. |
| `exaggeration` | number | Chatterbox: how strongly to act. Default 0.5. |
| `profile` | string | Breeze only: its decode profile. |
| `xvec_only` | boolean | Qwen Base only: use the speaker embedding alone, without a transcript. |
| `wait_seconds` | number | Wait in line up to this many seconds (at most 3600) while the model worker is busy, instead of the 409 answered at once without it. See `GET /api/queue`. |
| `wait_priority` | string | `normal` (the default), or `low` to wait behind the others, as a scene batch's lines do. |

**Answer:** `200`, JSON. What the engine reports about the prepared reference.

| Also answers | When |
|---|---|
| `409` | The model worker is busy and the request sent no `wait_seconds`, waited that long, or found 20 requests already waiting. Or, with `code` `gpu_busy`, the model would load on the graphics card while another app of Fel's Studio has it to itself (NPC Forge's One model at a time): refused at once, even with `wait_seconds`, and `holder` names that app, what it is doing and since when. |
| `499` | The request was cancelled with `POST /api/queue/{request_id}/cancel`: while it waited, or while it ran. |

<a id="get-api-performance-settings"></a>

#### `GET /api/performance/settings`

Describe the settings each engine's performance takes, and the languages its picker offers.

**Answer:** `200`, JSON. `engines`: for each engine its controls (name, type, range, default), whether it takes a direction, and its `languages`; and `unscreened_languages`: per engine, the languages the language screen did not clear.

<a id="get-api-queue"></a>

#### `GET /api/queue`

Say what the model worker is doing, and what waits for it, in order.

**Answer:** `200`, JSON. `running`: null, or the request using the worker; `waiting`: the requests in line, first first; and `max_waiting`. Each request has `id` (its `X-Request-Id`), `what` (such as `Take`, `Clip`, `Model test` or `Install`), `engine` (null when it is not one model's work), `priority` (`normal`, or `low` for a scene batch), `since` (when it started running, or joined the line), `seconds` (how long ago), `can_stop` and, for an install, `detail` (what it is installing).

The worker does one load, clip, take, test or install at a time. A request that needs it while it is busy is refused with 409 at once, unless it sent `wait_seconds` in its JSON body: then it waits in line, at most that many seconds (up to 3600). Requests with `wait_priority` `low`, as a scene batch's lines send, wait behind the others, and each priority keeps its order. At most 20 wait. A waiting request leaves the line when it is cancelled, when its caller closes the connection, or when its time is up (409).

<a id="post-api-queue-request_id-cancel"></a>

#### `POST /api/queue/{request_id}/cancel`

Take a request out of the line, or stop it while it runs.

| Path parameter | Meaning |
|---|---|
| `request_id` | A request's id: the `X-Request-Id` it sent, or the one its answer names; an entry of `GET /api/queue`. |

**Answer:** `200`, JSON. `cancelled`: `waiting` (it left the line, and answers 499 at once) or `running` (its model's process was ended: it answers 499 within moments, and the next request loads the model again).

| Also answers | When |
|---|---|
| `400` | The running request cannot be stopped: an install, or an unload. |
| `404` | No request with that id is waiting or running. |

Send your own `X-Request-Id` with the request you may want to stop, so you know its id before it answers.

<a id="get-api-openapi-json"></a>

#### `GET /api/openapi.json`

Return this API as an OpenAPI 3.1 document.

**Answer:** `200`, JSON. The same document as `openapi.json` in the program folder, built from the route table.

### Speaking

Turn text into audio. `speak` and `performance` answer with the finished WAV file in one response.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/voices`](#get-api-voices) | List every voice a program can speak with by name. |
| POST | [`/api/speak`](#post-api-speak) | Speak text with one engine and answer with the finished WAV file. |
| POST | [`/api/stream`](#post-api-stream) | Speak text with a Qwen3-TTS checkpoint and stream the audio as it is made. |
| POST | [`/api/performance`](#post-api-performance) | Speak a line as a Cast voice, and save it as a take unless asked not to. |
| POST | [`/api/kokoro/phonemize`](#post-api-kokoro-phonemize) | Show the phonemes Kokoro would speak for a text. |

<a id="get-api-voices"></a>

#### `GET /api/voices`

List every voice a program can speak with by name.

**Answer:** `200`, JSON. `voices`: the Cast's voices first, then the preset voices of the models this service runs. Each has `name`, the one name to send back as `voice`; `aliases`, other names that mean it; `kind`, `cast` or `preset`; `model` and `model_name`; `label`; `languages`; `takes_direction`, whether its model uses an acting `direction`; `ready`, and `why_not`, a sentence saying what to do when it is not ready. `licence` has a `rating` (`yes`, `conditions`, `unclear` or `no`) and a `reason` for it. A Cast voice also has `character`, `identity_id` and `status`; a preset voice has `voice_id`, its model's own name for it.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer field | Type |
|---|---|
| `voices[].name` | string |
| `voices[].aliases` | string[] |
| `voices[].kind` | string |
| `voices[].model` | string |
| `voices[].model_name` | string |
| `voices[].label` | string |
| `voices[].languages` | string[] |
| `voices[].takes_direction` | boolean |
| `voices[].ready` | boolean |
| `voices[].why_not` | string\|null |
| `voices[].character` | string |
| `voices[].identity_id` | string |
| `voices[].status` | string |
| `voices[].voice_id` | string |
| `voices[].licence.rating` | string |
| `voices[].licence.reason` | string |

A Cast voice is named after its character when it is the character's only voice, or its only approved one; any Cast voice is also `Character (Label)`. Retired and rejected voices are left out. A preset voice keeps its model's name (`af_heart`, `Harper`), or is `model:voice` when a Cast voice or another model has that name. Kokoro's and Phonon's voices are those installed; a Qwen CustomVoice checkpoint's speakers are listed while it is loaded, since only the loaded checkpoint reports them. Nothing is loaded to answer.

<a id="post-api-speak"></a>

#### `POST /api/speak`

Speak text with one engine and answer with the finished WAV file.

| Request field | Type | Meaning |
|---|---|---|
| `text` (required) | string | What to say, 1 to 10,000 characters. Text over 1,200 characters is split where a scene splits a long line, spoken part by part in the same call and joined with a quarter-second pause; the answer is still one WAV, and `X-Parts` says how many parts it had. The call can be cancelled between parts. A voice invented from a description (design mode) is refused for such a text, since it would change from one part to the next. |
| `engine` | string | An engine id from `GET /api/catalog` whose `integration.service_api` is true. Default `kokoro`. |
| `voice` | string | Without `engine`: a voice's `name` from `GET /api/voices`. A Cast voice speaks as that voice and nothing is kept (as `POST /api/performance` with `keep_take` false: `language`, `direction`, `settings` and `use_pronunciations` go with it); a preset voice speaks with its model. With `engine` `kokoro`, or no engine: a Kokoro voice id, or several joined with commas to blend them. Default `af_heart`. Phonon: one voice name from `phonon_voices` of `GET /api/info`. Default `Harper`. |
| `speed` | number | Kokoro: speaking rate. Default 1.0. |
| `language` | string | Kokoro and Qwen: the language. Default `English`, or for a Kokoro voice named without `engine`, the voice's own language. Phonon speaks English only. |
| `voice_description` | string | Qwen VoiceDesign, VoxCPM2 and Breeze design mode: the voice to invent, in words. |
| `mode` | string | For engines with several modes: `design`, `clone`, `continuation`, `custom_voice` or, for Breeze, `direction`. A checkpoint accepts the modes it supports. |
| `reference_path` | string | Cloning: a recording inside the Lab's reference storage. |
| `reference_text` | string | Cloning: the exact transcript of that recording. |
| `direction` | string | Engines with a direction control: how to deliver the line, up to 1000 characters. |
| `seed` | integer | Engines that take one: the random seed. Default 42. |
| `temperature` | number | Phonon: how much the delivery varies, 0 to 1. Default 0.3. The other engines that take a temperature list theirs under `capabilities` in `GET /api/catalog`. |
| `duration_delta` | number | Phonon: shift the pace, from -0.5 (faster) to 0.5 (slower). Default 0. |
| `save_voice_as` | string | Design mode only: keep the invented voice as a reusable reference under this name, letters, digits, `_` and `-`, up to two folders deep. |
| `wait_seconds` | number | Wait in line up to this many seconds (at most 3600) while the model worker is busy, instead of the 409 answered at once without it. See `GET /api/queue`. |
| `wait_priority` | string | `normal` (the default), or `low` to wait behind the others, as a scene batch's lines do. |

**Answer:** `200`, `audio/wav`. The whole 16-bit WAV file, in one response with a `Content-Length`. The call blocks until the clip is finished; there is no streaming and no partial answer. Other engine-specific fields are listed under each engine's `capabilities` in `GET /api/catalog`.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer header | Meaning |
|---|---|
| `X-Gen-Seconds` | Seconds the engine took. |
| `X-Audio-Seconds` | Seconds of audio in the file. |
| `X-First-Chunk` | Seconds to the first audio, when the engine reports it; otherwise empty. |
| `X-Generation-Mode` | The mode the engine ran in, when it reports one. |
| `X-Voice-Backend` | The backend, when reported. |
| `X-Model-Checkpoint` | The checkpoint, when reported. |
| `X-Checkpoint-Revision` | The checkpoint's pinned revision, when reported. |
| `X-Voice-Asset-Path` | With `save_voice_as`: the file the voice was saved to. |
| `X-Parts` | How many parts a text over 1,200 characters was spoken in. |

| Also answers | When |
|---|---|
| `404` | `voice` names no voice in `GET /api/voices` and is not a Kokoro voice id. |
| `413` | The request is over 64 KiB. |
| `500` | The model is installed but could not load or run; `error` says what it reported. |
| `503` | The model is not installed (`code` `model_not_installed`). `error` begins by saying where to install it: Install on its Models & features card, or its launcher in the lab folder with the arguments that choose the model: `tools\setup-kokoro.bat`, `tools\setup-phonon.bat`, `tools\setup-chatterbox.bat` (Chatterbox Turbo, or with `--original` Chatterbox 500M and Voice Conversion), `tools\setup-qwen.bat` with `--model` and the Qwen3-TTS model, `tools\setup-voxcpm.bat`, `tools\setup-breeze.bat` or `tools\setup-drama3.bat`. |
| `504` | The engine did not answer in time. |
| `409` | `save_voice_as` names a voice that already exists (`code` `already_exists`); or the model worker is busy and the request sent no `wait_seconds`, waited that long, or found 20 requests already waiting. Or, with `code` `gpu_busy`, the model would load on the graphics card while another app of Fel's Studio has it to itself (NPC Forge's One model at a time): refused at once, even with `wait_seconds`, and `holder` names that app, what it is doing and since when. |
| `499` | The request was cancelled with `POST /api/queue/{request_id}/cancel`: while it waited, or while it ran. |

Send no query string: `/api/speak?x=1` is a 404. Qwen, VoxCPM2 and Drama 3 refuse a field they do not take and name it in the 400. To keep what you generate as a Cast take, use [`POST /api/performance`](#post-api-performance).

<a id="post-api-stream"></a>

#### `POST /api/stream`

Speak text with a Qwen3-TTS checkpoint and stream the audio as it is made.

| Request field | Type | Meaning |
|---|---|---|
| `text` (required) | string | What to say, 1 to 1200 characters. |
| `engine` (required) | string | A Qwen3-TTS engine id. |
| `mode` | string | `design`, `clone` or `custom_voice`, as the checkpoint supports. |
| `voice_description` | string | VoiceDesign: the voice to invent, in words. |
| `reference_path` | string | Base (clone): a recording inside the Lab's reference storage. |
| `reference_text` | string | Base (clone): the exact transcript. |
| `speaker` | string | CustomVoice: a speaker the loaded checkpoint reports. |
| `language` | string | The language. Default `English`. |
| `save_voice_as` | string | VoiceDesign only: keep the voice as a reusable reference under this name. |
| `wait_seconds` | number | Wait in line up to this many seconds (at most 3600) while the model worker is busy, instead of the 409 answered at once without it. See `GET /api/queue`. |
| `wait_priority` | string | `normal` (the default), or `low` to wait behind the others, as a scene batch's lines do. |

**Answer:** `200`, `application/x-ndjson`. Newline-delimited JSON: one `start` line, `audio` lines (`sequence`, `sample_start`, `sample_count`, `sample_rate`, `sample_format` `pcm_s16le` and the PCM as base64 in `data`) and a closing `complete` line with the timings. A failure after the first line arrives as an `error` line, not a status code.

| Also answers | When |
|---|---|
| `409` | The model worker is busy and the request sent no `wait_seconds`, waited that long, or found 20 requests already waiting. Or, with `code` `gpu_busy`, the model would load on the graphics card while another app of Fel's Studio has it to itself (NPC Forge's One model at a time): refused at once, even with `wait_seconds`, and `holder` names that app, what it is doing and since when. |
| `499` | The request was cancelled with `POST /api/queue/{request_id}/cancel`: while it waited, or while it ran. |

Use `POST /api/speak` when you want a file.

<a id="post-api-performance"></a>

#### `POST /api/performance`

Speak a line as a Cast voice, and save it as a take unless asked not to.

| Request field | Type | Meaning |
|---|---|---|
| `identity_id` | string | The voice identity to speak with, from `GET /api/cast`. Give this or `voice`. |
| `voice` | string | A Cast voice's `name` from `GET /api/voices`, instead of `identity_id`. |
| `text` (required) | string | The line, 1 to 1200 characters; with `keep_take` false, up to 10,000. Text over 1,200 characters is split where a scene splits a long line, spoken part by part in the same call and joined with a quarter-second pause; the answer is still one WAV, and `X-Parts` says how many parts it had. The call can be cancelled between parts. A voice invented from a description (design mode) is refused for such a text, since it would change from one part to the next. |
| `keep_take` | boolean | Default true: the clip is saved as a take. False saves nothing in the library, for a program that speaks all day; the clip is still labelled as AI-generated speech. |
| `line_id` | string | Your own label for the line, 1 to 100 characters. Takes are grouped by voice and line. Needed when the take is kept. |
| `reference_version_id` | string | The recording to clone from. Default: the identity's approved recording. Refused for a fixed or preset voice. |
| `language` | string | Default: the voice's own language, else `English`. |
| `direction` | string | How to deliver the line, up to 1000 characters. An engine with no verified direction control refuses a non-empty one. |
| `settings` | object | The engine's performance settings; see `GET /api/performance/settings`. |
| `use_pronunciations` | boolean | Apply the character's pronunciation dictionary to the spoken text. The take keeps both texts. Default false. |
| `idempotency_key` | string | 1 to 200 characters. Repeating a call with the same key and the same inputs returns that take's audio without generating again. Only with a kept take. |
| `identity_snapshot` | object | Used by scene batches: the voice's `identity_type`, `engine_id` and `engine_options` as the scene saved them. The call is refused if the voice has changed since. |
| `wait_seconds` | number | Wait in line up to this many seconds (at most 3600) while the model worker is busy, instead of the 409 answered at once without it. See `GET /api/queue`. |
| `wait_priority` | string | `normal` (the default), or `low` to wait behind the others, as a scene batch's lines do. |

**Answer:** `200`, `audio/wav`. The clip's 16-bit WAV file, in one response. The call blocks until the clip is finished, and saved when kept.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer header | Meaning |
|---|---|
| `X-Take-Id` | The saved take's id; absent when it was not kept. |
| `X-Take-Kept` | `false` when `keep_take` was false. |
| `X-Parts` | How many parts a text over 1,200 characters was spoken in. |
| `X-Cast-Character` | The character's name. |
| `X-Cast-Identity` | The voice's label. |
| `X-Cast-Line-Id` | The `line_id` you sent. |
| `X-Gen-Seconds` | Seconds the engine took. |
| `X-Audio-Seconds` | Seconds of audio in the file. |
| `X-Voice-Backend` | The backend. |
| `X-Model-Checkpoint` | The checkpoint. |
| `X-Checkpoint-Revision` | The checkpoint's pinned revision. |
| `X-Generation-Mode` | `clone`, `direction`, `custom_voice` or `fixed_voice`. |
| `X-Idempotent-Replay` | `true` when the answer is an earlier take for the same key; then only `X-Take-Id` and the three `X-Cast-*` headers come with it. |

| Also answers | When |
|---|---|
| `400` | The identity is rejected or retired, has no usable recording, the text or direction is too long, the settings do not fit the engine, `voice` names a preset voice, or `idempotency_key` was used before with different inputs or for an edit of a take, or sent with `keep_take` false. |
| `404` | The identity, the recording or the voice named does not exist. |
| `503` | The model is not installed (`code` `model_not_installed`). `error` begins by saying where to install it: Install on its Models & features card, or its launcher in the lab folder with the arguments that choose the model: `tools\setup-kokoro.bat`, `tools\setup-phonon.bat`, `tools\setup-chatterbox.bat` (Chatterbox Turbo, or with `--original` Chatterbox 500M and Voice Conversion), `tools\setup-qwen.bat` with `--model` and the Qwen3-TTS model, `tools\setup-voxcpm.bat`, `tools\setup-breeze.bat` or `tools\setup-drama3.bat`. |
| `504` | The engine did not answer in time. |
| `409` | The model worker is busy and the request sent no `wait_seconds`, waited that long, or found 20 requests already waiting. Or, with `code` `gpu_busy`, the model would load on the graphics card while another app of Fel's Studio has it to itself (NPC Forge's One model at a time): refused at once, even with `wait_seconds`, and `holder` names that app, what it is doing and since when. |
| `499` | The request was cancelled with `POST /api/queue/{request_id}/cancel`: while it waited, or while it ran. |

See [Speak as a Cast voice](#speak-as-a-cast-voice) for how to find the ids and what a retry does.

<a id="post-api-kokoro-phonemize"></a>

#### `POST /api/kokoro/phonemize`

Show the phonemes Kokoro would speak for a text.

| Request field | Type | Meaning |
|---|---|---|
| `text` (required) | string | 1 to 1200 characters. |
| `language` | string | Default `English`. |
| `wait_seconds` | number | Wait in line up to this many seconds (at most 3600) while the model worker is busy, instead of the 409 answered at once without it. See `GET /api/queue`. |
| `wait_priority` | string | `normal` (the default), or `low` to wait behind the others, as a scene batch's lines do. |

**Answer:** `200`, JSON. `language`, `language_name` and `segments`, each with its `graphemes` and `phonemes`.

| Also answers | When |
|---|---|
| `409` | The model worker is busy and the request sent no `wait_seconds`, waited that long, or found 20 requests already waiting. Or, with `code` `gpu_busy`, the model would load on the graphics card while another app of Fel's Studio has it to itself (NPC Forge's One model at a time): refused at once, even with `wait_seconds`, and `holder` names that app, what it is doing and since when. |
| `499` | The request was cancelled with `POST /api/queue/{request_id}/cancel`: while it waited, or while it ran. |

Kokoro's language rules live in its own environment, so its worker answers: Kokoro is loaded first when it is not loaded already, as for speaking, and the model loaded before it is unloaded. A text or a language it cannot take is refused with a 400 before anything loads.

### OpenAI-style

For apps that can send text to any server that answers like OpenAI's speech API: give them `http://127.0.0.1:8765/v1` as the base address and any key. The Lab's voices, by the names `GET /api/voices` gives; nothing is kept in the library. Errors take OpenAI's shape: `{"error": {"message", "type", "param", "code"}}`, with the Lab's `code`.

| Method | Path | What it does |
|---|---|---|
| POST | [`/v1/audio/speech`](#post-v1-audio-speech) | Speak text as OpenAI's speech API does, in one of the Lab's voices. |
| POST | [`/v1/audio/transcriptions`](#post-v1-audio-transcriptions) | Transcribe a local audio file. |
| POST | [`/v1/audio/translations`](#post-v1-audio-translations) | Translate local speech to English. |
| GET | [`/v1/models`](#get-v1-models) | List the models that have a voice ready to speak, as OpenAI lists models. |
| GET | [`/v1/audio/models`](#get-v1-audio-models) | List the same models in the shape some apps ask for. |
| GET | [`/v1/audio/voices`](#get-v1-audio-voices) | List the voices ready to speak, for apps that offer a choice of voices. |

<a id="post-v1-audio-speech"></a>

#### `POST /v1/audio/speech`

Speak text as OpenAI's speech API does, in one of the Lab's voices.

| Request field | Type | Meaning |
|---|---|---|
| `input` (required) | string | What to say, 1 to 10,000 characters (OpenAI's own limit is 4,096). Text over 1,200 characters is split where a scene splits a long line, spoken part by part in the same call and joined with a quarter-second pause; the answer is still one WAV, and `X-Parts` says how many parts it had. The call can be cancelled between parts. A voice invented from a description (design mode) is refused for such a text, since it would change from one part to the next. |
| `voice` (required) | string | A voice's name from `GET /v1/audio/voices`, the same names as `GET /api/voices`. `{"id": "Mira"}` works too. OpenAI's own voices (alloy, nova...) are not the Lab's. |
| `model` | string | A model's id from `GET /v1/models`, which must be the voice's own. Any other name, such as `tts-1`, means the voice's own model. |
| `response_format` | string | `mp3` (the default, as OpenAI's), `opus` (in Ogg), `aac` (ADTS), `flac`, `wav` or `pcm` (24 kHz, 16-bit signed little-endian, mono, no header). MP3, Opus and AAC are made by FFmpeg. |
| `speed` | number | 0.25 to 4.0, default 1. Only a model with a speed control takes another value than 1, and only in its own range (Kokoro 82M: 0.5 to 2.0). |
| `instructions` | string | How to say it, for a voice whose model takes an acting direction (`takes_direction` in `GET /api/voices`). Other voices speak without it. |
| `stream_format` | string | Only `audio`, the default: the answer is the whole file. |
| `wait_seconds` | number | How long to wait in line while the model worker is busy, at most 3600. Default 600 here, since apps send a reply sentence by sentence. |

**Answer:** `200`, audio. The clip in the format asked for: `audio/mpeg`, `audio/ogg`, `audio/aac`, `audio/flac`, `audio/wav` or `audio/pcm`. Every format with room for it carries the AI-generated label; `pcm` and `aac` have none. No take is kept.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer header | Meaning |
|---|---|
| `X-Audio-Seconds` | The clip's length in seconds. |
| `X-Parts` | How many parts a long text was spoken in, when more than one. |

| Also answers | When |
|---|---|
| `404` | No voice has that name (`not_found`). |
| `409` | Twenty requests already wait for the model worker, or this one waited `wait_seconds` (`busy`). |
| `499` | The request was cancelled with `POST /api/queue/{request_id}/cancel` (`cancelled`). |
| `503` | The voice's model is not installed (`model_not_installed`), or the format is made by FFmpeg, which is not installed or cannot make it (`ffmpeg_not_installed`). |

Any `Authorization` header is accepted and ignored: like every route, this one answers programs on this computer only. A cancel needs the request's own `X-Request-Id`, as on the Lab's other routes.

<a id="post-v1-audio-transcriptions"></a>

#### `POST /v1/audio/transcriptions`

Transcribe a local audio file.

**Request:** a `multipart/form-data` file upload.

| Request field | Type | Meaning |
|---|---|---|
| `file` (required) | binary | A local audio upload, up to 25 MiB. |
| `model` (required) | string | Use whisper-1 or select a configured faster-whisper-tiny or faster-whisper-small model. |
| `language` | string | Optional ISO language code. |
| `prompt` | string | Optional spelling prompt. |
| `temperature` | number | Decoding temperature from 0 to 1. |
| `response_format` | string | json, verbose_json, text, srt or vtt. |
| `timestamp_granularities[]` | string[] | Optional word and segment timestamp granularity. |

**Answer:** `200`, JSON or caption text. Transcript text, optionally with timestamps or formatted as captions.

<a id="post-v1-audio-translations"></a>

#### `POST /v1/audio/translations`

Translate local speech to English.

**Request:** a `multipart/form-data` file upload.

| Request field | Type | Meaning |
|---|---|---|
| `file` (required) | binary | A local audio upload, up to 25 MiB. |
| `model` (required) | string | Use whisper-1 or select a configured faster-whisper-tiny or faster-whisper-small model. |
| `prompt` | string | Optional English spelling prompt. |
| `temperature` | number | Decoding temperature from 0 to 1. |
| `response_format` | string | json, verbose_json, text, srt or vtt. |

**Answer:** `200`, JSON or caption text. English transcript text, optionally with timestamps or as captions.

<a id="get-v1-models"></a>

#### `GET /v1/models`

List the models that have a voice ready to speak, as OpenAI lists models.

**Answer:** `200`, JSON. `object` (`list`) and `data`: each model's `id`, `object` (`model`), `created` (0), `owned_by` and `name`.

<a id="get-v1-audio-models"></a>

#### `GET /v1/audio/models`

List the same models in the shape some apps ask for.

**Answer:** `200`, JSON. `models`: each model's `id` and `name`.

<a id="get-v1-audio-voices"></a>

#### `GET /v1/audio/voices`

List the voices ready to speak, for apps that offer a choice of voices.

**Answer:** `200`, JSON. `voices`: each voice's `id` (its name, to send as `voice`) and `name` (what it is and its model, to show).

Open WebUI reads this list, and `GET /v1/audio/models`, when its speech engine is OpenAI with another base address.

### Cast

Characters, the voices (identities) they speak with, the recordings that clone those voices, and pronunciations. Retiring blocks new use of an item and everything made from it; purging deletes it.

| Method | Path | What it does |
|---|---|---|
| POST | [`/api/cast/slate-clip/preview`](#post-api-cast-slate-clip-preview) | Check a Voice Slate handoff file without saving it. |
| POST | [`/api/cast/slate-clip/import`](#post-api-cast-slate-clip-import) | Import a kept Voice Slate clip as a candidate. |
| GET | [`/api/inbox`](#get-api-inbox) | List the clips Voice Slate handed over, waiting in the inbox. |
| POST | [`/api/inbox/{inbox_file}/import`](#post-api-inbox-inbox_file-import) | Import a waiting clip as a candidate, then clear it from the inbox. |
| POST | [`/api/inbox/{inbox_file}/dismiss`](#post-api-inbox-inbox_file-dismiss) | Send a waiting file to the Recycle Bin without importing it. |
| GET | [`/api/cast`](#get-api-cast) | List every character with its voices and their recordings. |
| POST | [`/api/characters`](#post-api-characters) | Create a character. |
| POST | [`/api/characters/from-design`](#post-api-characters-from-design) | Design a voice from words and keep it as a new character's voice, ready to speak by the character's name. |
| POST | [`/api/characters/{character_id}`](#post-api-characters-character_id) | Rename a character or change its description. |
| POST | [`/api/characters/{character_id}/identities`](#post-api-characters-character_id-identities) | Add a voice to a character. |
| POST | [`/api/identities/{identity_id}/update`](#post-api-identities-identity_id-update) | Update a voice, approve or reject it, or rate its recording's licence. |
| POST | [`/api/identities/{identity_id}/references`](#post-api-identities-identity_id-references) | Upload a recording for a cloning voice and save it as a voice recording. |
| POST | [`/api/identities/{identity_id}/references/preview`](#post-api-identities-identity_id-references-preview) | Process a recording the way an upload would, and return the result without saving anything. |
| POST | [`/api/identities/{identity_id}/references/from-take`](#post-api-identities-identity_id-references-from-take) | Save a take as a new reference recording for a cloning voice. |
| POST | [`/api/references/{reference_id}/versions`](#post-api-references-reference_id-versions) | Save a new version of a reference recording with a different transcript or trim. |
| GET | [`/api/references/{reference_id}/audio`](#get-api-references-reference_id-audio) | Download a voice recording's processed WAV. |
| GET | [`/api/references/{reference_id}/source`](#get-api-references-reference_id-source) | Download the file a voice recording was made from, as it was uploaded. |
| POST | [`/api/references/{reference_id}/transcript-suggestion`](#post-api-references-reference_id-transcript-suggestion) | Draft a transcript of a reference recording. Nothing is saved. |
| GET | [`/api/pronunciations`](#get-api-pronunciations) | List pronunciation entries. |
| POST | [`/api/pronunciations`](#post-api-pronunciations) | Add a pronunciation, or change the one with the same term. |
| POST | [`/api/retirements`](#post-api-retirements) | Retire a character, voice, recording or take: block new use of it and of everything made from it. Nothing is deleted. |
| POST | [`/api/retirements/restore`](#post-api-retirements-restore) | Lift a retirement. |
| GET | [`/api/purges/preview`](#get-api-purges-preview) | Show what purging a retired item would delete. |
| POST | [`/api/purges`](#post-api-purges) | Permanently delete a retired item, everything made from it, and their audio. |

<a id="post-api-cast-slate-clip-preview"></a>

#### `POST /api/cast/slate-clip/preview`

Check a Voice Slate handoff file without saving it.

**Request:** file bytes (`application/octet-stream`).

**Answer:** `200`, JSON. `clip` (transcript, origin, allowed use, hashes and warnings) and `engines`.

| Also answers | When |
|---|---|
| `400` | The bundle or source file is invalid. |
| `413` | The bundle exceeds the upload limit. |

<a id="post-api-cast-slate-clip-import"></a>

#### `POST /api/cast/slate-clip/import`

Import a kept Voice Slate clip as a candidate.

| Query parameter | Type | Meaning |
|---|---|---|
| `character` (required) | string | The Cast character name or ID. |
| `engine_id` (required) | string | A reference-cloning engine from the preview. |
| `create_character` | string | Send 1 to create the character when the name is new. |
| `accept_draft` | string | Send 1 only after explicitly accepting an unchecked transcript draft. |
| `sidecar_sha256` | string | Optional preview hash. Refuse a clip whose sidecar differs from the one reviewed. |

**Request:** file bytes (`application/octet-stream`).

**Answer:** `201`, JSON. `status`, Cast character, identity and reference IDs, and `warnings`.

| Also answers | When |
|---|---|
| `400` | The clip or requested assignment is invalid. |
| `413` | The bundle exceeds the upload limit. |

The bundle contains the kept WAV and its exact sidecar. The existing recorder bridge validates and copies them. A master recording stays in Slate and an imported reference still needs Cast approval.

<a id="get-api-inbox"></a>

#### `GET /api/inbox`

List the clips Voice Slate handed over, waiting in the inbox.

**Answer:** `200`, JSON. `files`, oldest first, at most 200: each file's `name`, `sender` (an app id such as `voice-slate`, or null), `sender_name`, `sent` (UTC, from the file name, or null), `size` in bytes and `state`. `ready` comes with `clip`, the same summary as `POST /api/cast/slate-clip/preview`; `checking` means not checked yet; `refused` comes with `code` (`kind`, `size`, `clip` or `unreadable`) and `problem`, a sentence. Also `engines` (the models that can use a recording) and `folder` (where the inbox is).

The inbox is `inbox\voice-lab\` in the folder of Fel's Studio (`%LOCALAPPDATA%\Fels\`, or `FELS_SUITE_DIR`). The service makes it when it starts; Voice Slate offers Send to Voice Lab only when it exists. Each file is checked once as the file box checks a picked one, and again when it changes. One listing checks new files for about 1.5 s, and the next one goes on. Nothing is imported until a caller asks.

<a id="post-api-inbox-inbox_file-import"></a>

#### `POST /api/inbox/{inbox_file}/import`

Import a waiting clip as a candidate, then clear it from the inbox.

| Path parameter | Meaning |
|---|---|
| `inbox_file` | A file's `name` from `GET /api/inbox`, percent-encoded. |

| Request field | Type | Meaning |
|---|---|---|
| `character` (required) | string | The Cast character name or ID. |
| `engine_id` (required) | string | A model from the listing's `engines`. |
| `create_character` | boolean | Create the character when the name is new. |
| `accept_draft` | boolean | Send true only after the person accepted an unchecked transcript draft. |
| `sidecar_sha256` | string | The `clip.sidecar_sha256` the person reviewed. A clip whose sidecar differs is refused. |

**Answer:** `201`, JSON. `status`, Cast character, identity and reference IDs, `warnings`, and `cleared`: whether the file went to the Recycle Bin.

| Also answers | When |
|---|---|
| `400` | The clip, its kind or size, or the requested assignment is invalid. |
| `404` | The file is no longer in the inbox. |

The same check and import as `POST /api/cast/slate-clip/import`. After the import the file goes to the Recycle Bin (it is deleted outside Windows). If that fails, the clip is still in Cast, `cleared` is false and a warning says why; importing it again changes nothing.

<a id="post-api-inbox-inbox_file-dismiss"></a>

#### `POST /api/inbox/{inbox_file}/dismiss`

Send a waiting file to the Recycle Bin without importing it.

| Path parameter | Meaning |
|---|---|
| `inbox_file` | A file's `name` from `GET /api/inbox`, percent-encoded. |

**Answer:** `200`, JSON. `{"ok": true}`.

| Also answers | When |
|---|---|
| `404` | The file is no longer in the inbox. |
| `500` | Windows could not move it to the Recycle Bin; the sentence says why. |

Any listed file can be dismissed, refused ones too. It is deleted outside Windows.

<a id="get-api-cast"></a>

#### `GET /api/cast`

List every character with its voices and their recordings.

**Answer:** `200`, JSON. `schema_version` and `characters`. Each character has `id`, `name`, `description`, `tags` and `identities`; each identity has `id`, `engine_id`, `label`, `status` (`candidate`, `approved` or `rejected`), `kind` (`reference`, `fixed_voice` or `preset_voice`), `engine_options`, `approved_reference_id`, `notes` and `references` (the recordings, newest version first, each with `id`, `version`, `transcript`, `origin`, `allowed_use`, `duration_seconds`, `audio_url` and `licence_rating`, null until one is chosen). Each voice has a `licence` with its effective rating and reason. A voice can also report `voice_languages` and whether its `approved_recording_available` file is present. Retired items say so.

<a id="post-api-characters"></a>

#### `POST /api/characters`

Create a character.

| Request field | Type | Meaning |
|---|---|---|
| `name` (required) | string | 1 to 80 characters. |
| `description` | string | Up to 2000 characters. |
| `tags` | string[] | Up to 40 characters each. |

**Answer:** `201`, JSON. `character`, as in `GET /api/cast`.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer field | Type |
|---|---|
| `character.id` | string |
| `character.name` | string |
| `character.description` | string |
| `character.tags` | string[] |
| `character.identities` | array |

<a id="post-api-characters-from-design"></a>

#### `POST /api/characters/from-design`

Design a voice from words and keep it as a new character's voice, ready to speak by the character's name.

| Request field | Type | Meaning |
|---|---|---|
| `name` (required) | string | The new character's name, 1 to 80 characters. The voice is called by it. |
| `voice_description` (required) | string | The voice to invent, in words, up to 1000 characters: `A warm, low voice, unhurried and a little hoarse.` |
| `text` | string | The sample line the voice is designed with, up to 400 characters, which becomes its voice recording with these words as the transcript. Default: a short English line. |
| `engine` | string | The model that designs the voice: `qwen3-tts-1.7b-voice-design` (the default), `voxcpm2` or `breeze-tts-2`. |
| `clone_engine` | string | The model that speaks the voice afterwards, from its recording. Default, for a Qwen design: the first installed of Qwen3-TTS 1.7B Base, 0.6B Base, Chatterbox Turbo, VoxCPM2 and Chatterbox 500M; for the others, the design model itself. |
| `language` | string | Qwen VoiceDesign: the language of the sample line. Default `English`. |
| `seed` | integer | The design's random seed. Default 42. |
| `wait_seconds` | number | Wait in line up to this many seconds (at most 3600) while the model worker is busy, instead of the 409 answered at once without it. See `GET /api/queue`. |
| `wait_priority` | string | `normal` (the default), or `low` to wait behind the others, as a scene batch's lines do. |

**Answer:** `201`, JSON. `voice`: the new voice as `GET /api/voices` lists it, with `ready` true and `name`, the character's name; `character_id`; `reference_id`, its recording; `engine`, the model that designed it; and `clone_engine`, the one that speaks it.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer field | Type |
|---|---|
| `voice.name` | string |
| `voice.aliases` | string[] |
| `voice.kind` | string |
| `voice.model` | string |
| `voice.character` | string |
| `voice.identity_id` | string |
| `voice.status` | string |
| `voice.ready` | boolean |
| `voice.why_not` | string\|null |
| `voice.licence.rating` | string |
| `voice.licence.reason` | string |
| `character_id` | string |
| `reference_id` | string |
| `engine` | string |
| `clone_engine` | string |

| Also answers | When |
|---|---|
| `500` | The model is installed but could not load or run; `error` says what it reported. |
| `503` | The design model, or the model that speaks the voice afterwards, is not installed (`model_not_installed`): `error` says which. |
| `409` | A character of that name is in the Cast already (`already_exists`); or the model worker is busy and the request sent no `wait_seconds`, waited that long, or found 20 requests already waiting. Or, with `code` `gpu_busy`, the model would load on the graphics card while another app of Fel's Studio has it to itself (NPC Forge's One model at a time): refused at once, even with `wait_seconds`, and `holder` names that app, what it is doing and since when. |
| `499` | The request was cancelled with `POST /api/queue/{request_id}/cancel`: while it waited, or while it ran. |

One call does what the page does when a designed clip is kept as a Cast voice: the design model speaks the sample line, the clip is kept as the voice's recording with its origin and allowed use (the design model's terms, as for `save_voice_as`), and the voice is approved. It then sounds the same in every clip, through `POST /v1/audio/speech`, `POST /api/speak` or `POST /api/performance`, by its name. Nothing is saved when the design fails. The first call loads the design model, which can take minutes: send `wait_seconds`. A voice saved with `save_voice_as` of `POST /api/speak` is a recording under `data/voices`, not a Cast voice.

<a id="post-api-characters-character_id"></a>

#### `POST /api/characters/{character_id}`

Rename a character or change its description.

| Path parameter | Meaning |
|---|---|
| `character_id` | A character's id, from `GET /api/cast`. |

| Request field | Type | Meaning |
|---|---|---|
| `name` | string | 1 to 80 characters. Leave out to keep it. |
| `description` | string | Up to 2000 characters. Leave out to keep it. |

**Answer:** `200`, JSON. `character`.

<a id="post-api-characters-character_id-identities"></a>

#### `POST /api/characters/{character_id}/identities`

Add a voice to a character.

| Path parameter | Meaning |
|---|---|
| `character_id` | A character's id, from `GET /api/cast`. |

| Request field | Type | Meaning |
|---|---|---|
| `engine_id` (required) | string | A recording-cloning engine, `kokoro`, `phonon-31m-en` or the Qwen CustomVoice engine that the service exposes. |
| `label` (required) | string | 1 to 80 characters. |
| `notes` | string | Up to 2000 characters. |
| `voice` | string | Kokoro: a voice name, or several joined with commas to blend them. Default `af_heart`. |
| `speaker` | string | CustomVoice: a speaker the loaded checkpoint reports. Phonon: a voice name from `phonon_voices` of `GET /api/info`. |
| `language` | string | CustomVoice: a language the loaded checkpoint reports. Phonon: `English`, the default. |
| `default_direction` | string | CustomVoice: the delivery instruction to use when a line gives none. |

**Answer:** `201`, JSON. `identity`. A new voice is a `candidate` until it is approved.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer field | Type |
|---|---|
| `identity.id` | string |
| `identity.engine_id` | string |
| `identity.label` | string |
| `identity.status` | string |
| `identity.kind` | string |
| `identity.references` | array |

A cloning voice has no recording yet: upload one with [`POST /api/identities/{identity_id}/references`](#post-api-identities-identity_id-references).

<a id="post-api-identities-identity_id-update"></a>

#### `POST /api/identities/{identity_id}/update`

Update a voice, approve or reject it, or rate its recording's licence.

| Path parameter | Meaning |
|---|---|
| `identity_id` | A voice identity's id, from `GET /api/cast`. |

| Request field | Type | Meaning |
|---|---|---|
| `label` | string | 1 to 80 characters. |
| `notes` | string | Up to 2000 characters. |
| `status` | string | `candidate`, `approved` or `rejected`. |
| `reference_version_id` | string | The recording to approve; needed to approve a cloning voice. |
| `licence_rating` | string\|null | Recording use: `yes`, `conditions`, `unclear` or `no`. Use null to clear it. |
| `licence_reference_id` | string | The saved recording to rate. Defaults to the approved recording. |
| `engine_options` | object | Kokoro: `{"voice": "af_heart"}`. Preset voices cannot change. |

**Answer:** `200`, JSON. `identity`.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer field | Type |
|---|---|
| `identity.id` | string |
| `identity.status` | string |
| `identity.approved_reference_id` | string\|null |
| `identity.references[].id` | string |
| `identity.references[].licence_rating` | string\|null |
| `identity.licence.rating` | string |
| `identity.licence.reason` | string |

Approving a cloning voice needs a recording with its exact transcript. Approving a voice sends another approved voice of the same character and engine back to `candidate`.

<a id="post-api-identities-identity_id-references"></a>

#### `POST /api/identities/{identity_id}/references`

Upload a recording for a cloning voice and save it as a voice recording.

| Path parameter | Meaning |
|---|---|
| `identity_id` | A voice identity's id, from `GET /api/cast`. |

**Request:** the recording's bytes, and an `X-Reference-Metadata` header.

| `X-Reference-Metadata` field | Type | Meaning |
|---|---|---|
| `filename` | string | The recording's file name; its ending says the format. Default `reference.wav`. |
| `transcript` | string | The exact words spoken in it, up to 1200 characters. Models that clone with the words need them. |
| `origin` (required) | string | Where the recording came from, up to 1000 characters. |
| `allowed_use` (required) | string | Who may use it, and for what, up to 1000 characters. |
| `edits` | object | Trim, sample rate, channel and noise reduction, as `POST /api/references/{reference_id}/versions` takes them. |

**Answer:** `201`, JSON. `reference`: the saved version with its quality readings and `audio_url`.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer field | Type |
|---|---|
| `reference.id` | string |
| `reference.version` | integer |
| `reference.transcript` | string |
| `reference.origin` | string |
| `reference.allowed_use` | string |
| `reference.duration_seconds` | number |
| `reference.audio_url` | string |

| Also answers | When |
|---|---|
| `413` | The recording is over 32 MiB. |

Send the recording's bytes as the body (WAV, FLAC, Ogg, MP3 or M4A) and, in the `X-Reference-Metadata` header, base64url-encoded JSON: `{"filename": "nell.wav", "transcript": "...", "origin": "...", "allowed_use": "...", "edits": {"start_seconds": 0.5, "end_seconds": 9.0, "target_sample_rate": 24000, "channel": "mono", "noise_reduction": {"enabled": false}}}`. `filename` sets the format; `edits` is optional. The saved reference is 0.5 to 60 seconds.

<a id="post-api-identities-identity_id-references-preview"></a>

#### `POST /api/identities/{identity_id}/references/preview`

Process a recording the way an upload would, and return the result without saving anything.

| Path parameter | Meaning |
|---|---|
| `identity_id` | A voice identity's id, from `GET /api/cast`. |

**Request:** the recording's bytes, and an `X-Reference-Metadata` header.

**Answer:** `200`, `audio/wav`. The processed WAV. Its quality readings are in the `X-Reference-*` headers.

| Answer header | Meaning |
|---|---|
| `X-Reference-Duration` | Seconds. |
| `X-Reference-Sample-Rate` | Hertz. |
| `X-Reference-RMS` | dBFS. |
| `X-Reference-Quality` | Base64url-encoded JSON of the quality assessment. |

| Also answers | When |
|---|---|
| `413` | The recording is over 32 MiB. |

Same body and `X-Reference-Metadata` header as the upload.

<a id="post-api-identities-identity_id-references-from-take"></a>

#### `POST /api/identities/{identity_id}/references/from-take`

Save a take as a new reference recording for a cloning voice.

| Path parameter | Meaning |
|---|---|
| `identity_id` | A voice identity's id, from `GET /api/cast`. |

| Request field | Type | Meaning |
|---|---|---|
| `take_id` (required) | string | The take to copy. |
| `transcript` | string | Default: the take's spoken text. |
| `edits` | object | Trim, rate and channel edits, as for an upload. |
| `origin` | string | Default: a note naming the take. |
| `allowed_use` | string | Added to the take's own terms, which always carry over. |

**Answer:** `201`, JSON. `reference`. It keeps the take's licence rating (`licence_rating`), unless `allowed_use` is given or the take's own recording has no rating yet.

<a id="post-api-references-reference_id-versions"></a>

#### `POST /api/references/{reference_id}/versions`

Save a new version of a reference recording with a different transcript or trim.

| Path parameter | Meaning |
|---|---|
| `reference_id` | A reference recording version's id, from `GET /api/cast`. |

| Request field | Type | Meaning |
|---|---|---|
| `transcript` | string | Up to 1200 characters. Default: the current one. |
| `origin` | string | Cannot be blank. |
| `allowed_use` | string | Cannot be blank. |
| `start_seconds` | number | Trim start in the source recording. |
| `end_seconds` | number | Trim end. |
| `target_sample_rate` | integer | 8000 to 192000. |
| `channel` | string | `mono`, `left` or `right`. |
| `noise_reduction` | object | `enabled`, `profile_start_seconds`, `profile_end_seconds`, `strength`. |

**Answer:** `201`, JSON. `reference`: the new version. The earlier one is kept as it was.

<a id="get-api-references-reference_id-audio"></a>

#### `GET /api/references/{reference_id}/audio`

Download a voice recording's processed WAV.

| Path parameter | Meaning |
|---|---|
| `reference_id` | A reference recording version's id, from `GET /api/cast`. |

**Answer:** `200`, `audio/wav`. The WAV.

| Answer header | Meaning |
|---|---|
| `X-Reference-Version` | The version's id. |

<a id="get-api-references-reference_id-source"></a>

#### `GET /api/references/{reference_id}/source`

Download the file a voice recording was made from, as it was uploaded.

| Path parameter | Meaning |
|---|---|
| `reference_id` | A reference recording version's id, from `GET /api/cast`. |

**Answer:** `200`, audio. The recording as it was uploaded.

<a id="post-api-references-reference_id-transcript-suggestion"></a>

#### `POST /api/references/{reference_id}/transcript-suggestion`

Draft a transcript of a reference recording. Nothing is saved.

| Path parameter | Meaning |
|---|---|
| `reference_id` | A reference recording version's id, from `GET /api/cast`. |

**Answer:** `200`, JSON. `suggestion`: the drafted text and how it was made. Check it before saving it with a new version.

| Also answers | When |
|---|---|
| `409` | A transcription is already running. |
| `500` | The transcription failed. |
| `503` | No transcription model is installed. |

<a id="get-api-pronunciations"></a>

#### `GET /api/pronunciations`

List pronunciation entries.

| Query parameter | Type | Meaning |
|---|---|---|
| `character_id` | string | A character's entries together with the shared ones. Default: only the shared ones. |

**Answer:** `200`, JSON. `pronunciations`: `id`, `character_id` (null when shared), `source`, `replacement`, `notes`.

<a id="post-api-pronunciations"></a>

#### `POST /api/pronunciations`

Add a pronunciation, or change the one with the same term.

| Request field | Type | Meaning |
|---|---|---|
| `source` (required) | string | The written term, 1 to 100 characters. |
| `replacement` (required) | string | How to spell it for the engine, 1 to 200 characters. |
| `character_id` | string | Omit for a shared entry. |
| `notes` | string | Up to 500 characters. |

**Answer:** `200`, JSON. `pronunciation`.

<a id="post-api-retirements"></a>

#### `POST /api/retirements`

Retire a character, voice, recording or take: block new use of it and of everything made from it. Nothing is deleted.

| Request field | Type | Meaning |
|---|---|---|
| `kind` (required) | string | `character`, `identity`, `reference` or `take`. |
| `id` (required) | string | The item's id. |
| `reason` | string | Up to 300 characters. |

**Answer:** `201`, JSON. `retirement` and `affected`: how many items of each kind it reaches.

<a id="post-api-retirements-restore"></a>

#### `POST /api/retirements/restore`

Lift a retirement.

| Request field | Type | Meaning |
|---|---|---|
| `kind` (required) | string | As for retiring. |
| `id` (required) | string | The item's id. |

**Answer:** `200`, JSON. `{"restored": true}`. Something retired because it came from a retired source stays blocked until that source is restored.

<a id="get-api-purges-preview"></a>

#### `GET /api/purges/preview`

Show what purging a retired item would delete.

| Query parameter | Type | Meaning |
|---|---|---|
| `kind` (required) | string | `character`, `identity`, `reference` or `take`. |
| `id` (required) | string | The item's id. |

**Answer:** `200`, JSON. `would_purge` (counts by kind), `media_files` and `external_copies`: copies of the audio outside Cast storage, which a purge lists but never deletes.

| Also answers | When |
|---|---|
| `400` | The item is not retired. |

<a id="post-api-purges"></a>

#### `POST /api/purges`

Permanently delete a retired item, everything made from it, and their audio.

| Request field | Type | Meaning |
|---|---|---|
| `kind` (required) | string | As for retiring. |
| `id` (required) | string | The item's id. |
| `reason` | string | Up to 300 characters. |
| `confirm` (required) | boolean | Must be `true`; anything else is refused. |

**Answer:** `200`, JSON. `batch_id`, `purged` (counts by kind), `media_files` and `external_copies`.

| Also answers | When |
|---|---|
| `400` | The item is not retired, `confirm` is not `true`, or a file could not be removed (nothing is purged then, so the call can be repeated). |

### Takes

Saved performances. Every `performance` call and every scene render saves a take with its text, settings, model and provenance. An edit of a take (a trim, a cut of the silence, fades, a gain) is saved as a new take that names its original.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/takes`](#get-api-takes) | List takes, oldest first. |
| GET | [`/api/takes/{take_id}`](#get-api-takes-take_id) | Read one take. |
| GET | [`/api/takes/{take_id}/audio`](#get-api-takes-take_id-audio) | Download a take's WAV. |
| GET | [`/api/takes/{take_id}/waveform`](#get-api-takes-take_id-waveform) | Draw a take: its peaks, its level and where the speech is. |
| POST | [`/api/takes/{take_id}/edit-preview`](#post-api-takes-take_id-edit-preview) | Hear an edit of a take without saving anything. |
| POST | [`/api/takes/{take_id}/edit`](#post-api-takes-take_id-edit) | Save an edit of a take as a new take. |
| POST | [`/api/takes/{take_id}/update`](#post-api-takes-take_id-update) | Mark a take selected, rejected or candidate, rate it, or note it. |
| GET | [`/api/takes/{take_id}/voice-match`](#get-api-takes-take_id-voice-match) | Score how closely a take's voice matches its reference recording, with the optional speaker model. |

<a id="get-api-takes"></a>

#### `GET /api/takes`

List takes, oldest first.

| Query parameter | Type | Meaning |
|---|---|---|
| `identity_id` | string | Only this voice's takes. |
| `line_id` | string | Only takes for this line. |

**Answer:** `200`, JSON. `takes`: each with `id`, `character_id`, `character_name`, `identity_id`, `identity_label`, `engine_id`, `reference_version_id`, `line_id`, `text`, `spoken_text`, `language`, `direction`, `settings`, `model`, `provenance`, `sample_rate`, `duration_seconds`, `status`, `rating`, `notes`, `created_at`, `loudness`, `waveform_checks`, `licence` (the rating and reason for the specific recording and model used), `has_word_timings`, `has_phoneme_timings`, `parent_take_id` and `edit` (null unless the take is an edit of another take), `audio_url` and its retirement state.

<a id="get-api-takes-take_id"></a>

#### `GET /api/takes/{take_id}`

Read one take.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

**Answer:** `200`, JSON. `take`, as in `GET /api/takes`.

<a id="get-api-takes-take_id-audio"></a>

#### `GET /api/takes/{take_id}/audio`

Download a take's WAV.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

**Answer:** `200`, `audio/wav`. The WAV.

| Answer header | Meaning |
|---|---|
| `X-Take-Id` | The take's id. |

<a id="get-api-takes-take_id-waveform"></a>

#### `GET /api/takes/{take_id}/waveform`

Draw a take: its peaks, its level and where the speech is.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

| Query parameter | Type | Meaning |
|---|---|---|
| `points` | integer | How many slices to summarise the take in, 10 to 2000. Default 600. |

**Answer:** `200`, JSON. `duration_seconds`, `sample_rate`, `points`, `peaks` (a `[min, max]` pair from -1 to 1 for each slice), `peak_dbfs` and `rms_dbfs` (null for silence), and `speech`: the part louder than -50 dB, with its `start_seconds` and `end_seconds`, or null when nothing is.

| Also answers | When |
|---|---|
| `400` | `points` is not a whole number from 10 to 2000, or the stored audio is missing or has changed. |

<a id="post-api-takes-take_id-edit-preview"></a>

#### `POST /api/takes/{take_id}/edit-preview`

Hear an edit of a take without saving anything.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

| Request field | Type | Meaning |
|---|---|---|
| `start_seconds` | number | Where the kept part starts, in seconds from the start of the take. Default 0. |
| `end_seconds` | number | Where the kept part ends. Default the end of the take. It must be after `start_seconds`. |
| `auto_trim` | boolean | Cut the quiet before and after the speech inside that range. Default false. |
| `silence_threshold_db` | number | Auto-trim: anything quieter than this counts as silence, from -90 to -10 dB. Default -50. |
| `keep_silence_ms` | number | Auto-trim: how much of the silence to leave at each end, 0 to 2000 ms. Default 80. |
| `fade_in_ms` | number | A fade in over the start of the result, 0 to 5000 ms. Default 0. |
| `fade_out_ms` | number | A fade out over the end of the result, 0 to 5000 ms. Default 0. |
| `gain_db` | number | Make the result louder or quieter, -60 to 24 dB. A gain that would clip is refused. Default 0. |

**Answer:** `200`, `audio/wav`. The edited audio as a 16-bit mono WAV. Nothing is saved, and the take is not changed.

| Answer header | Meaning |
|---|---|
| `X-Audio-Seconds` | Seconds of audio in the edited clip. |
| `X-Edit-Start-Seconds` | Where in the take the kept part starts. |
| `X-Edit-End-Seconds` | Where in the take the kept part ends. |
| `X-Peak-Dbfs` | The loudest sample of the edited clip in dBFS; empty for silence. |

| Also answers | When |
|---|---|
| `400` | A field is unknown, not a number (or not true or false) or out of range; `end_seconds` is not after `start_seconds`; the edit changes nothing; the result would be under 0.05 seconds; the two fades are longer than the result; auto-trim finds nothing louder than the threshold; a gain would clip the loudest sample (the answer says how much the take can take); the take is retired; or its stored audio is missing or no longer matches its checksum. |

An edit never changes the take it starts from. The steps run in this order: trim to `start_seconds` to `end_seconds`; auto-trim inside that range (quiet inside the speech is never cut); the two fades, on a raised-cosine curve; then the gain. A trim alone copies the samples exactly; a fade or a gain recalculates them and rounds back to 16 bits. The new take keeps the parent's text, voice, settings and model, starts as a `candidate`, and has its loudness and checks measured again. Its word and phoneme times, when the parent has them, move with the trim, and a word the trim cuts away leaves them. An edit of an edit is allowed. Retiring a take retires its edits, and purging it deletes them.

<a id="post-api-takes-take_id-edit"></a>

#### `POST /api/takes/{take_id}/edit`

Save an edit of a take as a new take.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

| Request field | Type | Meaning |
|---|---|---|
| `start_seconds` | number | Where the kept part starts, in seconds from the start of the take. Default 0. |
| `end_seconds` | number | Where the kept part ends. Default the end of the take. It must be after `start_seconds`. |
| `auto_trim` | boolean | Cut the quiet before and after the speech inside that range. Default false. |
| `silence_threshold_db` | number | Auto-trim: anything quieter than this counts as silence, from -90 to -10 dB. Default -50. |
| `keep_silence_ms` | number | Auto-trim: how much of the silence to leave at each end, 0 to 2000 ms. Default 80. |
| `fade_in_ms` | number | A fade in over the start of the result, 0 to 5000 ms. Default 0. |
| `fade_out_ms` | number | A fade out over the end of the result, 0 to 5000 ms. Default 0. |
| `gain_db` | number | Make the result louder or quieter, -60 to 24 dB. A gain that would clip is refused. Default 0. |
| `idempotency_key` | string | 1 to 200 characters. Repeating the call with the same key and the same edit returns the take that was made instead of making another. |

**Answer:** `201`, JSON. `take`, the new take as in `GET /api/takes` (its `parent_take_id` is this take and its `edit` says what was done), and `replayed`. A repeat with the same `idempotency_key` answers `200` with `replayed: true` and the take made the first time.

| Also answers | When |
|---|---|
| `400` | A field is unknown, not a number (or not true or false) or out of range; `end_seconds` is not after `start_seconds`; the edit changes nothing; the result would be under 0.05 seconds; the two fades are longer than the result; auto-trim finds nothing louder than the threshold; a gain would clip the loudest sample (the answer says how much the take can take); the take is retired; or its stored audio is missing or no longer matches its checksum. An `idempotency_key` used before for a different edit, another take or a performance is refused too. |

An edit never changes the take it starts from. The steps run in this order: trim to `start_seconds` to `end_seconds`; auto-trim inside that range (quiet inside the speech is never cut); the two fades, on a raised-cosine curve; then the gain. A trim alone copies the samples exactly; a fade or a gain recalculates them and rounds back to 16 bits. The new take keeps the parent's text, voice, settings and model, starts as a `candidate`, and has its loudness and checks measured again. Its word and phoneme times, when the parent has them, move with the trim, and a word the trim cuts away leaves them. An edit of an edit is allowed. Retiring a take retires its edits, and purging it deletes them.

<a id="post-api-takes-take_id-update"></a>

#### `POST /api/takes/{take_id}/update`

Mark a take selected, rejected or candidate, rate it, or note it.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

| Request field | Type | Meaning |
|---|---|---|
| `status` | string | `candidate`, `selected` or `rejected`. Selecting a take returns the line's other selected take to `candidate`. |
| `rating` | integer | 1 to 5. |
| `notes` | string | Up to 2000 characters. |

**Answer:** `200`, JSON. `take`.

| Also answers | When |
|---|---|
| `400` | The take is retired. |

<a id="get-api-takes-take_id-voice-match"></a>

#### `GET /api/takes/{take_id}/voice-match`

Score how closely a take's voice matches its reference recording, with the optional speaker model.

| Path parameter | Meaning |
|---|---|
| `take_id` | A take's id: the `X-Take-Id` header of a performance, or an entry of `GET /api/takes`. |

**Answer:** `200`, JSON. `voice_match`: `available`; when true, a `score`, a `band` (`likely_same`, `uncertain` or `likely_different`), the `reference_version_id` and `proxy: true`; when false, a `reason` (a preset voice has no recording to compare with, or the speaker model is not installed). A proxy for review, not an acceptance result.

### Scenes

Ordered scripts rendered line by line through the same performance call. The manifest's fields are in [scene-schema-v1.md](scenes/scene-schema-v1.md).

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/scenes`](#get-api-scenes) | List scenes. |
| POST | [`/api/scenes`](#post-api-scenes) | Create a scene from a manifest. |
| GET | [`/api/scenes/template.json`](#get-api-scenes-template-json) | Download a scene manifest template. |
| GET | [`/api/scenes/template.csv`](#get-api-scenes-template-csv) | Download the scene CSV header and one sample row. |
| POST | [`/api/scenes/import/preview`](#post-api-scenes-import-preview) | Read a script without creating anything. |
| POST | [`/api/scenes/import`](#post-api-scenes-import) | Create a scene from JSON, CSV, a script or timed subtitles. |
| GET | [`/api/sample`](#get-api-sample) | Say whether the sample scene exists, and whether it can be made here. |
| POST | [`/api/sample`](#post-api-sample) | Make the sample scene: two characters with Kokoro preset voices and a nine-line scene written for the Lab, ready to render. |
| GET | [`/api/sample/remove/preview`](#get-api-sample-remove-preview) | Show what removing the sample scene would delete. |
| POST | [`/api/sample/remove`](#post-api-sample-remove) | Delete everything the sample scene made, which leaves the library as it was. |
| GET | [`/api/scenes/{scene_id}`](#get-api-scenes-scene_id) | Read a scene. |
| POST | [`/api/scenes/{scene_id}`](#post-api-scenes-scene_id) | Replace a scene's name and lines. |
| GET | [`/api/scenes/{scene_id}/export.json`](#get-api-scenes-scene_id-export-json) | Download a scene as a manifest. |
| GET | [`/api/scenes/{scene_id}/export.csv`](#get-api-scenes-scene_id-export-csv) | Download a scene as CSV. |
| GET | [`/api/scenes/{scene_id}/segments`](#get-api-scenes-scene_id-segments) | Show how each line splits into synthesis segments. |
| GET | [`/api/scenes/{scene_id}/jobs`](#get-api-scenes-scene_id-jobs) | List a scene's batch jobs, newest first. |
| POST | [`/api/scenes/{scene_id}/jobs`](#post-api-scenes-scene_id-jobs) | Queue a batch that renders the scene's lines as takes. |
| GET | [`/api/scenes/jobs/{job_id}`](#get-api-scenes-jobs-job_id) | Read a job's progress. |
| GET | [`/api/scenes/jobs/{job_id}/manifest.json`](#get-api-scenes-jobs-job_id-manifest-json) | Download a finished job's result manifest. |
| POST | [`/api/scenes/jobs/{job_id}/pause`](#post-api-scenes-jobs-job_id-pause) | Pause a job after the take in progress. |
| POST | [`/api/scenes/jobs/{job_id}/resume`](#post-api-scenes-jobs-job_id-resume) | Resume a paused job. |
| POST | [`/api/scenes/jobs/{job_id}/cancel`](#post-api-scenes-jobs-job_id-cancel) | Cancel a job. A take already being made is not undone. |
| POST | [`/api/scenes/jobs/{job_id}/retry`](#post-api-scenes-jobs-job_id-retry) | Queue the failed rows of a job again. |
| POST | [`/api/scenes/{scene_id}/select`](#post-api-scenes-scene_id-select) | Choose which take a line, or one of its segments, uses. |
| GET | [`/api/scenes/{scene_id}/lines`](#get-api-scenes-scene_id-lines) | Say where every line of a scene stands, in one answer. |
| POST | [`/api/scenes/{scene_id}/choose-newest`](#post-api-scenes-scene_id-choose-newest) | Choose the newest take that fits for every segment without one. |
| POST | [`/api/scenes/{scene_id}/preview`](#post-api-scenes-scene_id-preview) | Assemble the selected takes into one preview. |
| GET | [`/api/scenes/{scene_id}/previews/{preview_file}.wav`](#get-api-scenes-scene_id-previews-preview_file-wav) | Download a preview's WAV. |
| GET | [`/api/dub-queue`](#get-api-dub-queue) | Read durable dubbing jobs and watch folders. |
| POST | [`/api/dub-queue`](#post-api-dub-queue) | Queue one or more dubbing deliveries. |
| POST | [`/api/dub-queue/jobs/{job_id}/retry`](#post-api-dub-queue-jobs-job_id-retry) | Retry a terminal dubbing job. |
| POST | [`/api/dub-queue/jobs/{job_id}/cancel`](#post-api-dub-queue-jobs-job_id-cancel) | Cancel a queued delivery or its pending render. |
| POST | [`/api/dub-queue/watches`](#post-api-dub-queue-watches) | Watch a selected local folder for dubbing manifests. |
| POST | [`/api/dub-queue/watches/{job_id}/disable`](#post-api-dub-queue-watches-job_id-disable) | Turn off a dubbing watch folder. |
| GET | [`/api/scenes/{scene_id}/dub`](#get-api-scenes-scene_id-dub) | Read the source, background stem and most recent dubbing delivery. |
| POST | [`/api/scenes/{scene_id}/dub/source`](#post-api-scenes-scene_id-dub-source) | Attach a local audio or video file to a timed scene. |
| POST | [`/api/scenes/{scene_id}/dub/background`](#post-api-scenes-scene_id-dub-background) | Attach an aligned audio-only background stem. |
| GET | [`/api/scenes/{scene_id}/dub/captions/{dub_file}`](#get-api-scenes-scene_id-dub-captions-dub_file) | Export review text or captions on the cue clock. |
| POST | [`/api/scenes/{scene_id}/dub/translate`](#post-api-scenes-scene_id-dub-translate) | Draft one line using an approved provider. |
| POST | [`/api/scenes/{scene_id}/dub/exports`](#post-api-scenes-scene_id-dub-exports) | Publish dubbed audio and optional video for selected takes. |
| POST | [`/api/scenes/{scene_id}/dub/exports/{delivery_id}/verify`](#post-api-scenes-scene_id-dub-exports-delivery_id-verify) | Verify a delivery manifest and files. |
| GET | [`/api/scenes/{scene_id}/dub/source/{dub_file}`](#get-api-scenes-scene_id-dub-source-dub_file) | Stream the source preview with byte ranges. |
| GET | [`/api/scenes/{scene_id}/dub/exports/{delivery_id}/{dub_file}`](#get-api-scenes-scene_id-dub-exports-delivery_id-dub_file) | Stream a file declared by a dubbing delivery. |

<a id="get-api-scenes"></a>

#### `GET /api/scenes`

List scenes.

**Answer:** `200`, JSON. `schema_version` and `scenes`: every scene in full, the most recently changed first. When a saved book owns a scene, `chapter_books` maps its scene ID to the book ID, title and chapter position (starting at 1).

<a id="post-api-scenes"></a>

#### `POST /api/scenes`

Create a scene from a manifest.

| Request field | Type | Meaning |
|---|---|---|
| `name` | string | 1 to 120 characters. Default `Untitled scene`. |
| `lines` (required) | object[] | 1 to 500 lines, in order; see the scene schema. |
| `schema_version` | integer | Default 1. |

**Answer:** `201`, JSON. `scene`, with its `scene_id`, `revision`, `checksum` and computed `segments`.

| Also answers | When |
|---|---|
| `413` | The request is over 2 MiB. |

Get a template from `GET /api/scenes/template.json` and fill in ids from `GET /api/cast`.

<a id="get-api-scenes-template-json"></a>

#### `GET /api/scenes/template.json`

Download a scene manifest template.

**Answer:** `200`, JSON. A manifest with placeholder ids.

<a id="get-api-scenes-template-csv"></a>

#### `GET /api/scenes/template.csv`

Download the scene CSV header and one sample row.

**Answer:** `200`, `text/csv`. The CSV.

<a id="post-api-scenes-import-preview"></a>

#### `POST /api/scenes/import/preview`

Read a script without creating anything.

| Request field | Type | Meaning |
|---|---|---|
| `format` (required) | string | `text` (one `NAME: words` line per speech), `fountain`, `srt`, `vtt` or `transcript`. |
| `content` (required) | string | The script, up to 2 MiB. |

**Answer:** `200`, JSON. `script`: `title` (a Fountain title page's Title, or empty), `lines` (each with `number`, its line in the script, `speaker`, null for narration, `kind`, `text` and `direction`), `speakers` (each `name` and how many `lines`), `narration` (how many lines have no speaker) and `unread` (each `number`, `text` and `reason`). Subtitle previews also include `duration_ms`, planned `sections`, overlap `warnings`, and the original cue times.

| Also answers | When |
|---|---|
| `400` | The format or subtitle header is invalid. |
| `413` | The request is over 2 MiB. |

The import below reads the script the same way. Plain text: a direction in brackets or parentheses at the start of the words, or after the name, is the line's acting direction, and a line with no name is narration. Fountain: a parenthetical is the direction of the words after it; extensions such as (V.O.) and (CONT'D) are taken off names; scene headings, transitions, notes and boneyard are left out; action is narration.

<a id="post-api-scenes-import"></a>

#### `POST /api/scenes/import`

Create a scene from JSON, CSV, a script or timed subtitles.

| Request field | Type | Meaning |
|---|---|---|
| `format` (required) | string | `json`, `csv`, `text`, `fountain`, `srt`, `vtt` or `transcript`. |
| `content` (required) | string | The text, up to 2 MiB. |
| `speakers` | object | For scripts and subtitles, every named speaker mapped to `{"identity_id": ...}` or to null to leave their lines out. A speaker missing from the map is refused. |
| `narrator` | object | For scripts and subtitles, `{"identity_id": ...}` for the lines with no speaker. Without it they are left out. |
| `name` | string | The scene's name for scripts and subtitles. Defaults to the Fountain title or a name for the format. |

**Answer:** `201`, JSON. `scene`; `scenes` also lists the saved scenes for a multi-section subtitle file. For scripts, `report` includes `lines` (how many were imported), `left_out` and `directions_left_out` (each with the script's line `number`, `speaker` and `text`) and `unread`, as the preview gives it.

| Also answers | When |
|---|---|
| `400` | A subtitle cue is malformed, a voice cannot be used or is unassigned, or every line is left out. |
| `413` | The request is over 2 MiB. |

A script's lines get the defaults a new line gets on the page: the voice's default acting direction when the script gives none, 250 ms after the line, one take, and the model's own settings. A direction for a model that takes none is left out and listed in `directions_left_out`. Subtitle scenes preserve their cue clock and split long files into scenes of up to 30 minutes and 500 cues, with video offsets. `transcript` accepts the Slate `fels-voice-transcript-v1` JSON and preserves its source media identity and original annotations.

<a id="get-api-sample"></a>

#### `GET /api/sample`

Say whether the sample scene exists, and whether it can be made here.

**Answer:** `200`, JSON. `sample`: null, or its `scene_id`, `scene_name`, `characters` (each `id` and `name`) and `created_at`; `can_make` (Kokoro 82M is installed) and `why_not`.

<a id="post-api-sample"></a>

#### `POST /api/sample`

Make the sample scene: two characters with Kokoro preset voices and a nine-line scene written for the Lab, ready to render.

**Answer:** `201`, JSON. `made` and `sample`, as `GET /api/sample` gives it. When the sample exists already, it is answered with 200 and nothing is made.

| Also answers | When |
|---|---|
| `503` | Kokoro 82M is not installed (`model_not_installed`). |

The characters are named as samples and tagged `sample`, and their voices are approved. The scene comes through the same script import as `POST /api/scenes/import`. What it made is recorded in `data/scenes/sample.json`, so a library backup carries it.

<a id="get-api-sample-remove-preview"></a>

#### `GET /api/sample/remove/preview`

Show what removing the sample scene would delete.

**Answer:** `200`, JSON. `scene` (null, or its `scene_id`, `name` and how many batch `jobs`), `would_remove` (counts of `character`, `identity`, `reference` and `take`), `used_elsewhere` (the names of other scenes with a line in a sample character's voice, which stop the removal) and `external_copies` (copies of the takes under `out/`, such as export packages, which are listed and never deleted). With no sample, `scene` is null and nothing is counted.

<a id="post-api-sample-remove"></a>

#### `POST /api/sample/remove`

Delete everything the sample scene made, which leaves the library as it was.

| Request field | Type | Meaning |
|---|---|---|
| `confirm` (required) | boolean | Must be `true`; anything else is refused. |

**Answer:** `200`, JSON. `removed` (counts of `scene`, `jobs`, `character`, `identity`, `reference` and `take`) and `external_copies`.

| Also answers | When |
|---|---|
| `400` | `confirm` is not `true`, or another scene uses a sample character. |
| `404` | There is no sample scene. |
| `409` | A batch of the sample is making a line: it was cancelled; remove the sample again in a moment (`busy`). |

The scene goes with its batches and built previews; each character goes with its voices, any recording added to it and every take made with them, and no record of the removal is kept. A take made with a sample voice outside the sample scene, in Perform for example, goes too.

<a id="get-api-scenes-scene_id"></a>

#### `GET /api/scenes/{scene_id}`

Read a scene.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `200`, JSON. `scene`.

<a id="post-api-scenes-scene_id"></a>

#### `POST /api/scenes/{scene_id}`

Replace a scene's name and lines.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Request field | Type | Meaning |
|---|---|---|
| `name` | string | 1 to 120 characters. |
| `lines` (required) | object[] | The full list of lines. |
| `expected_revision` | integer | The revision you last read. A different one is refused, so a stale edit cannot overwrite a newer one. |

**Answer:** `200`, JSON. `scene` at its next revision.

| Also answers | When |
|---|---|
| `400` | `expected_revision` is stale, or the manifest is invalid. |
| `413` | The request is over 2 MiB. |

There is no PUT: a scene is updated with this POST.

<a id="get-api-scenes-scene_id-export-json"></a>

#### `GET /api/scenes/{scene_id}/export.json`

Download a scene as a manifest.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `200`, a file. The manifest as a JSON file.

<a id="get-api-scenes-scene_id-export-csv"></a>

#### `GET /api/scenes/{scene_id}/export.csv`

Download a scene as CSV.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `200`, `text/csv`. The CSV file.

<a id="get-api-scenes-scene_id-segments"></a>

#### `GET /api/scenes/{scene_id}/segments`

Show how each line splits into synthesis segments.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Query parameter | Type | Meaning |
|---|---|---|
| `line_id` | string | Only this line. |

**Answer:** `200`, JSON. `segments`: `seg-001`, `seg-002` and so on, per line.

<a id="get-api-scenes-scene_id-jobs"></a>

#### `GET /api/scenes/{scene_id}/jobs`

List a scene's batch jobs, newest first.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `200`, JSON. `jobs`, each as `GET /api/scenes/jobs/{job_id}` gives it.

<a id="post-api-scenes-scene_id-jobs"></a>

#### `POST /api/scenes/{scene_id}/jobs`

Queue a batch that renders the scene's lines as takes.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Request field | Type | Meaning |
|---|---|---|
| `line_ids` | string[] | Only these lines. Default: every line. |
| `segment_ids` | string[] | Only these segments, as `line-id/seg-001`. |

**Answer:** `202`, JSON. `job`. Jobs run one take at a time; poll `GET /api/scenes/jobs/{job_id}`.

<a id="get-api-scenes-jobs-job_id"></a>

#### `GET /api/scenes/jobs/{job_id}`

Read a job's progress.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. `job`: its `status` (`queued`, `running`, `paused`, `completed`, `failed` or `cancelled`), the counts `total`, `queued`, `running`, `completed`, `failed` and `cancelled`, `progress` from 0 to 1, and `rows`: one per segment to render, with its `status`, `take_id`, `attempts`, `error` and `engine_id`, in the order they render. A batch renders all of one model's rows before the next model loads: `models` lists them in that order, each with `engine_id`, `name`, `total` and `done` (rows finished, failed or cancelled).

<a id="get-api-scenes-jobs-job_id-manifest-json"></a>

#### `GET /api/scenes/jobs/{job_id}/manifest.json`

Download a finished job's result manifest.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, a file. The manifest, only when its checksum verifies.

| Also answers | When |
|---|---|
| `409` | The manifest's checksum does not match (`code` `conflict`). |

<a id="post-api-scenes-jobs-job_id-pause"></a>

#### `POST /api/scenes/jobs/{job_id}/pause`

Pause a job after the take in progress.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. `job`.

<a id="post-api-scenes-jobs-job_id-resume"></a>

#### `POST /api/scenes/jobs/{job_id}/resume`

Resume a paused job.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. `job`.

<a id="post-api-scenes-jobs-job_id-cancel"></a>

#### `POST /api/scenes/jobs/{job_id}/cancel`

Cancel a job. A take already being made is not undone.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

| Request field | Type | Meaning |
|---|---|---|
| `stop_current` | boolean | True also stops the line the job is rendering now, by ending its model's process, instead of letting it finish; that line is marked cancelled, and the next request loads the model again. |

**Answer:** `200`, JSON. `job`.

<a id="post-api-scenes-jobs-job_id-retry"></a>

#### `POST /api/scenes/jobs/{job_id}/retry`

Queue the failed rows of a job again.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. `job`.

<a id="post-api-scenes-scene_id-select"></a>

#### `POST /api/scenes/{scene_id}/select`

Choose which take a line, or one of its segments, uses.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Request field | Type | Meaning |
|---|---|---|
| `line_id` (required) | string | The line. |
| `take_id` (required) | string | The take. |
| `segment_id` | string | For a line with several segments. |

**Answer:** `200`, JSON. `scene`.

<a id="get-api-scenes-scene_id-lines"></a>

#### `GET /api/scenes/{scene_id}/lines`

Say where every line of a scene stands, in one answer.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `200`, JSON. `scene_id`, `revision` and `lines`, in script order, each with its `line_id`, `status`, `reason`, `segments_done` and `segments`. A status is `not_rendered`, `waiting` (a batch will reach it), `rendering`, `failed` (the latest batch to try its current text failed; `reason` says why), `choose_take` (a take fits its current text, voice and settings, and none is chosen), `out_of_date` (its chosen take, or every take it has, was made before its text, voice or settings changed; `reason` says what changed) or `done` (its chosen take still fits). A line has the first of `rendering`, `waiting`, `failed`, `out_of_date`, `not_rendered`, `choose_take` and `done` that one of its segments has. Each segment has its `segment_id`, `status`, `reason`, `chosen_take_id` and `takes`, newest first and without retired takes that are not chosen, each with `id`, `created_at`, `duration_seconds`, `audio_url`, `waveform_checks`, `chosen`, `fits` and `reason`.

<a id="post-api-scenes-scene_id-choose-newest"></a>

#### `POST /api/scenes/{scene_id}/choose-newest`

Choose the newest take that fits for every segment without one.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Request field | Type | Meaning |
|---|---|---|
| `line_ids` | string[] | Only these lines. Default: every line. |

**Answer:** `200`, JSON. `scene`, at a new revision when it chose any, `chosen` (how many segments got a take) and `lines` (how many lines they are in).

<a id="post-api-scenes-scene_id-preview"></a>

#### `POST /api/scenes/{scene_id}/preview`

Assemble the selected takes into one preview.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `201`, JSON. `preview`: `audio_url` for the raw assembly, `mastered_audio_url` for the loudness-normalized copy, and the measurements.

| Also answers | When |
|---|---|
| `400` | A line has no selected take, or its audio is missing. |

<a id="get-api-scenes-scene_id-previews-preview_file-wav"></a>

#### `GET /api/scenes/{scene_id}/previews/{preview_file}.wav`

Download a preview's WAV.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |
| `preview_file` | The file name in a preview answer's `audio_url`, without its `.wav`. |

**Answer:** `200`, `audio/wav`. The WAV named by a preview answer's `audio_url` or `mastered_audio_url`.

<a id="get-api-dub-queue"></a>

#### `GET /api/dub-queue`

Read durable dubbing jobs and watch folders.

**Answer:** `200`, JSON. The latest jobs and the configured local watch folders.

<a id="post-api-dub-queue"></a>

#### `POST /api/dub-queue`

Queue one or more dubbing deliveries.

| Request field | Type | Meaning |
|---|---|---|
| `items` (required) | object[] | Scene IDs, export options and optional render_missing flag. |

**Answer:** `202`, JSON. The durable jobs and their current status.

<a id="post-api-dub-queue-jobs-job_id-retry"></a>

#### `POST /api/dub-queue/jobs/{job_id}/retry`

Retry a terminal dubbing job.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. The updated job.

<a id="post-api-dub-queue-jobs-job_id-cancel"></a>

#### `POST /api/dub-queue/jobs/{job_id}/cancel`

Cancel a queued delivery or its pending render.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. The cancelled job.

<a id="post-api-dub-queue-watches"></a>

#### `POST /api/dub-queue/watches`

Watch a selected local folder for dubbing manifests.

| Request field | Type | Meaning |
|---|---|---|
| `path` (required) | string | Existing local absolute folder for .dubjob.json manifests. |

**Answer:** `201`, JSON. The watch.

<a id="post-api-dub-queue-watches-job_id-disable"></a>

#### `POST /api/dub-queue/watches/{job_id}/disable`

Turn off a dubbing watch folder.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. The disabled watch.

<a id="get-api-scenes-scene_id-dub"></a>

#### `GET /api/scenes/{scene_id}/dub`

Read the source, background stem and most recent dubbing delivery.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `200`, JSON. The attached assets, current scene revision and newest published delivery.

<a id="post-api-scenes-scene_id-dub-source"></a>

#### `POST /api/scenes/{scene_id}/dub/source`

Attach a local audio or video file to a timed scene.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Query parameter | Type | Meaning |
|---|---|---|
| `name` (required) | string | Local media file name and extension. |

**Request:** file bytes (`application/octet-stream`).

**Answer:** `201`, JSON. The SHA-256 and browser preview of the preserved source file. Send one supported media file up to 512 MiB.

| Also answers | When |
|---|---|
| `413` | The media is larger than 512 MiB. |

<a id="post-api-scenes-scene_id-dub-background"></a>

#### `POST /api/scenes/{scene_id}/dub/background`

Attach an aligned audio-only background stem.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Query parameter | Type | Meaning |
|---|---|---|
| `name` (required) | string | Local media file name and extension. |

**Request:** file bytes (`application/octet-stream`).

**Answer:** `201`, JSON. The SHA-256 and preview metadata for the preserved background source.

<a id="get-api-scenes-scene_id-dub-captions-dub_file"></a>

#### `GET /api/scenes/{scene_id}/dub/captions/{dub_file}`

Export review text or captions on the cue clock.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |
| `dub_file` | A file name listed in the dubbing delivery, captions or source preview. |

**Answer:** `200`, a file. Target TXT, source/target/bilingual SRT and VTT, or target/bilingual ASS.

<a id="post-api-scenes-scene_id-dub-translate"></a>

#### `POST /api/scenes/{scene_id}/dub/translate`

Draft one line using an approved provider.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Request field | Type | Meaning |
|---|---|---|
| `expected_revision` (required) | integer | Saved scene revision. |
| `line_id` (required) | string | The line to translate. |
| `source_language` (required) | string | Source language code. |
| `target_language` (required) | string | Target language code. |
| `provider` (required) | string | Selected provider. |
| `approve_provider_revision` (required) | string | Provider revision approved after showing the destination and fields. |

**Answer:** `200`, JSON. A draft for review and the unchanged scene revision.

<a id="post-api-scenes-scene_id-dub-exports"></a>

#### `POST /api/scenes/{scene_id}/dub/exports`

Publish dubbed audio and optional video for selected takes.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |

| Request field | Type | Meaning |
|---|---|---|
| `expected_revision` (required) | integer | Saved scene revision. |
| `target_language` (required) | string | Target language tag. |
| `keep_original` | boolean | Include the original as an alternate MP4 audio track. |
| `background` | boolean | Mix the attached background stem. |
| `background_gain_db` | number | Background gain from −30 to +6 dB. |
| `duck` | boolean | Duck the background during dialogue. |
| `burn` | string | Optional target, bilingual or karaoke burned captions. |
| `additional_deliveries` | string[] | Previous language deliveries for this source. |

**Answer:** `201`, JSON. Delivery ID, source checksum, selected takes, timing adjustments, stream metadata and file hashes.

<a id="post-api-scenes-scene_id-dub-exports-delivery_id-verify"></a>

#### `POST /api/scenes/{scene_id}/dub/exports/{delivery_id}/verify`

Verify a delivery manifest and files.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |
| `delivery_id` | A saved delivery or audition ID. |

**Answer:** `200`, JSON. Delivery ID and number of verified files.

<a id="get-api-scenes-scene_id-dub-source-dub_file"></a>

#### `GET /api/scenes/{scene_id}/dub/source/{dub_file}`

Stream the source preview with byte ranges.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |
| `dub_file` | A file name listed in the dubbing delivery, captions or source preview. |

**Answer:** `200`, a file. Browser-ready original.mp4 or original.wav.

| Also answers | When |
|---|---|
| `416` | The range is outside the file. |

<a id="get-api-scenes-scene_id-dub-exports-delivery_id-dub_file"></a>

#### `GET /api/scenes/{scene_id}/dub/exports/{delivery_id}/{dub_file}`

Stream a file declared by a dubbing delivery.

| Path parameter | Meaning |
|---|---|
| `scene_id` | A scene's id, from `GET /api/scenes`. |
| `delivery_id` | A saved delivery or audition ID. |
| `dub_file` | A file name listed in the dubbing delivery, captions or source preview. |

**Answer:** `200`, a file. The requested manifest, video, audio stem or caption file.

| Also answers | When |
|---|---|
| `416` | The range is outside the file. |

### Live speech

Short-lived sessions with ordered local audio input and speech replies.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/live`](#get-api-live) | Discover local live speech capabilities. |
| POST | [`/api/live`](#post-api-live) | Start a short-lived audio session. |
| GET | [`/api/live/{session_id}`](#get-api-live-session_id) | Replay a live session's events. |
| POST | [`/api/live/{session_id}/audio`](#post-api-live-session_id-audio) | Append ordered 16 kHz signed PCM samples. |
| POST | [`/api/live/{session_id}/partial`](#post-api-live-session_id-partial) | Recognize the audio received so far. |
| POST | [`/api/live/{session_id}/final`](#post-api-live-session_id-final) | Finish the audio turn. |
| POST | [`/api/live/{session_id}/cancel`](#post-api-live-session_id-cancel) | Cancel recognition or speech and clear the audio. |
| POST | [`/api/live/{session_id}/speak`](#post-api-live-session_id-speak) | Stream reply audio from a local model. |

<a id="get-api-live"></a>

#### `GET /api/live`

Discover local live speech capabilities.

**Answer:** `200`, JSON. ASR readiness, input format and speech engines with native streaming.

<a id="post-api-live"></a>

#### `POST /api/live`

Start a short-lived audio session.

| Request field | Type | Meaning |
|---|---|---|
| `language` | string | Optional input language code. |

**Answer:** `201`, JSON. Session ID, input format and cursor.

<a id="get-api-live-session_id"></a>

#### `GET /api/live/{session_id}`

Replay a live session's events.

| Path parameter | Meaning |
|---|---|
| `session_id` | A blind listening session's id, from `GET /api/evaluations/blind-sessions`. |

| Query parameter | Type | Meaning |
|---|---|---|
| `after` | integer | Return events after this cursor. |

**Answer:** `200`, JSON. Text events, state and next sample offset.

<a id="post-api-live-session_id-audio"></a>

#### `POST /api/live/{session_id}/audio`

Append ordered 16 kHz signed PCM samples.

| Path parameter | Meaning |
|---|---|
| `session_id` | A blind listening session's id, from `GET /api/evaluations/blind-sessions`. |

| Query parameter | Type | Meaning |
|---|---|---|
| `offset` (required) | integer | Sample offset for this chunk. |

**Request:** file bytes (`application/octet-stream`).

**Answer:** `200`, JSON. The next sample offset. Repeated identical chunks are accepted.

<a id="post-api-live-session_id-partial"></a>

#### `POST /api/live/{session_id}/partial`

Recognize the audio received so far.

| Path parameter | Meaning |
|---|---|
| `session_id` | A blind listening session's id, from `GET /api/evaluations/blind-sessions`. |

**Answer:** `200`, JSON. A partial transcript and its event cursor.

<a id="post-api-live-session_id-final"></a>

#### `POST /api/live/{session_id}/final`

Finish the audio turn.

| Path parameter | Meaning |
|---|---|
| `session_id` | A blind listening session's id, from `GET /api/evaluations/blind-sessions`. |

**Answer:** `200`, JSON. Final recognized text and its event cursor.

<a id="post-api-live-session_id-cancel"></a>

#### `POST /api/live/{session_id}/cancel`

Cancel recognition or speech and clear the audio.

| Path parameter | Meaning |
|---|---|
| `session_id` | A blind listening session's id, from `GET /api/evaluations/blind-sessions`. |

**Answer:** `200`, JSON. Cancelled session status and event.

<a id="post-api-live-session_id-speak"></a>

#### `POST /api/live/{session_id}/speak`

Stream reply audio from a local model.

| Path parameter | Meaning |
|---|---|
| `session_id` | A blind listening session's id, from `GET /api/evaluations/blind-sessions`. |

| Request field | Type | Meaning |
|---|---|---|
| `text` (required) | string | Reply text, up to 500 characters. |
| `voice` | string | Named voice for buffered playback. |
| `engine` | string | Engine for native speech streaming. |
| `voice_description` | string | Engine-supported voice design description. |
| `language` | string | Optional native speech language. |
| `speaker` | string | Native preset ID. |
| `mode` | string | Native engine mode. |

**Answer:** `200`, `application/x-ndjson`. Start, PCM audio and completion events.

### Automation

Saved local recipes, provider profiles and resumable runs.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/automation/providers`](#get-api-automation-providers) | List versioned provider destinations and capabilities. |
| POST | [`/api/automation/providers`](#post-api-automation-providers) | Save a provider profile on this computer. |
| POST | [`/api/automation/providers/check`](#post-api-automation-providers-check) | Check the selected provider's models without sending media. |
| GET | [`/api/automation/recipes`](#get-api-automation-recipes) | List the versioned recipes in this library. |
| POST | [`/api/automation/recipes`](#post-api-automation-recipes) | Create or update a named recipe. |
| POST | [`/api/automation/preview`](#post-api-automation-preview) | Check recipe inputs and preview providers and output destinations. |
| POST | [`/api/automation/assets`](#post-api-automation-assets) | Upload a local audio asset for automation. |
| GET | [`/api/automation/assets/{asset_sha}`](#get-api-automation-assets-asset_sha) | Read a saved automation file. |
| GET | [`/api/automation/runs`](#get-api-automation-runs) | Read recent run statuses. |
| POST | [`/api/automation/runs`](#post-api-automation-runs) | Queue a saved recipe on one or more inputs. |
| GET | [`/api/automation/runs/{job_id}`](#get-api-automation-runs-job_id) | Read a recipe run. |
| POST | [`/api/automation/runs/{job_id}/cancel`](#post-api-automation-runs-job_id-cancel) | Cancel unfinished work for a recipe run. |

<a id="get-api-automation-providers"></a>

#### `GET /api/automation/providers`

List versioned provider destinations and capabilities.

**Answer:** `200`, JSON. Public provider profiles with their revisions.

<a id="post-api-automation-providers"></a>

#### `POST /api/automation/providers`

Save a provider profile on this computer.

| Request field | Type | Meaning |
|---|---|---|
| `profile` (required) | object | The provider ID, base URL, model and capabilities. |
| `key` | string | Optional key kept in memory until the Lab closes. |

**Answer:** `201`, JSON. Public provider details with its current revision.

<a id="post-api-automation-providers-check"></a>

#### `POST /api/automation/providers/check`

Check the selected provider's models without sending media.

| Request field | Type | Meaning |
|---|---|---|
| `provider` (required) | string | Configured provider ID. |
| `approve_provider_revision` | string | Explicitly approved revision for an external destination. |

**Answer:** `200`, JSON. Connectivity and presence of the configured model.

<a id="get-api-automation-recipes"></a>

#### `GET /api/automation/recipes`

List the versioned recipes in this library.

**Answer:** `200`, JSON. Saved recipes.

<a id="post-api-automation-recipes"></a>

#### `POST /api/automation/recipes`

Create or update a named recipe.

| Request field | Type | Meaning |
|---|---|---|
| `schema_version` (required) | integer | Recipe schema version 1. |
| `name` (required) | string | Recipe name. |
| `input_kind` (required) | string | text, audio or scene. |
| `steps` (required) | object[] | Ordered operations. |
| `id` | string | Saved recipe ID when updating. |
| `revision` | integer | Saved revision when updating. |

**Answer:** `201`, JSON. Validated recipe with its ID and revision.

<a id="post-api-automation-preview"></a>

#### `POST /api/automation/preview`

Check recipe inputs and preview providers and output destinations.

| Request field | Type | Meaning |
|---|---|---|
| `recipe` | object | Unstored recipe schema. |
| `recipe_id` | string | Saved recipe ID. |
| `inputs` (required) | string[] | Text values, asset IDs or scene IDs. |

**Answer:** `200`, JSON. Execution plan without starting model work.

<a id="post-api-automation-assets"></a>

#### `POST /api/automation/assets`

Upload a local audio asset for automation.

| Query parameter | Type | Meaning |
|---|---|---|
| `filename` (required) | string | File name with extension. |

**Request:** file bytes (`application/octet-stream`).

**Answer:** `201`, JSON. A content-addressed audio ID.

<a id="get-api-automation-assets-asset_sha"></a>

#### `GET /api/automation/assets/{asset_sha}`

Read a saved automation file.

| Path parameter | Meaning |
|---|---|
| `asset_sha` | An automation audio asset SHA-256. |

**Answer:** `200`, audio. The local audio file.

<a id="get-api-automation-runs"></a>

#### `GET /api/automation/runs`

Read recent run statuses.

**Answer:** `200`, JSON. Recent runs with per-item progress.

<a id="post-api-automation-runs"></a>

#### `POST /api/automation/runs`

Queue a saved recipe on one or more inputs.

| Request field | Type | Meaning |
|---|---|---|
| `recipe_id` (required) | string | Saved recipe ID. |
| `revision` | integer | Require this recipe revision. |
| `approve_provider_revisions` | string[] | Externally routed revisions explicitly approved after preview. |
| `inputs` (required) | string[] | Text, local audio asset IDs or saved scene IDs. |

**Answer:** `202`, JSON. The queued run and its input statuses.

<a id="get-api-automation-runs-job_id"></a>

#### `GET /api/automation/runs/{job_id}`

Read a recipe run.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. A saved run and per-item status.

<a id="post-api-automation-runs-job_id-cancel"></a>

#### `POST /api/automation/runs/{job_id}/cancel`

Cancel unfinished work for a recipe run.

| Path parameter | Meaning |
|---|---|
| `job_id` | A job's id, from the call that started it. |

**Answer:** `200`, JSON. The updated run status.

### Production

Search saved projects and share local project or voice files.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/projects`](#get-api-projects) | Search local books, timed scenes and scenes. |
| POST | [`/api/portable/export`](#post-api-portable-export) | Create a portable archive. |
| POST | [`/api/portable/preview`](#post-api-portable-preview) | Verify a portable ZIP without importing it. |
| POST | [`/api/portable/voice-preview`](#post-api-portable-voice-preview) | Play a verified archive voice sample without importing. |
| POST | [`/api/portable/import`](#post-api-portable-import) | Import the exact portable ZIP previewed locally. |
| GET | [`/api/portable/{delivery_id}.zip`](#get-api-portable-delivery_id-zip) | Read a portable ZIP. |
| POST | [`/api/audio-tools/sound`](#post-api-audio-tools-sound) | Create a seeded procedural sound effect. |
| POST | [`/api/audio-tools/master`](#post-api-audio-tools-master) | Master a local WAV or FLAC to a separate WAV. |
| GET | [`/api/audio-tools/{delivery_id}/audio.wav`](#get-api-audio-tools-delivery_id-audio-wav) | Stream a saved audio tool WAV. |
| GET | [`/api/audio-tools/{delivery_id}/receipt.json`](#get-api-audio-tools-delivery_id-receipt-json) | Read an audio tool receipt. |
| POST | [`/api/audio-tools/{delivery_id}/dub-background`](#post-api-audio-tools-delivery_id-dub-background) | Attach a generated effect as the dub background. |

<a id="get-api-projects"></a>

#### `GET /api/projects`

Search local books, timed scenes and scenes.

| Query parameter | Type | Meaning |
|---|---|---|
| `kind` | string | `scene`, `dub` (timed scene), or `book`. |
| `q` | string | Search titles and script. |

**Answer:** `200`, JSON. Project IDs, title, type, voices, takes and job status.

<a id="post-api-portable-export"></a>

#### `POST /api/portable/export`

Create a portable archive.

| Request field | Type | Meaning |
|---|---|---|
| `kind` (required) | string | `persona` (a voice file), `scene`, `dub` (timed scene), or `book`. |
| `id` | string | Project ID. |
| `identity_id` | string | The Cast voice to export. |
| `include_media` | boolean | Include the book cover. |
| `include_voices` | boolean | Include approved project references. |
| `author` | string | Optional name of the voice-file author. |
| `permission` | boolean | Confirm permission to share a cloned voice and its recording. |

**Answer:** `201`, JSON. `archive`: its `id`, `kind`, `files` (how many it holds) and `download`, the path of `GET /api/portable/{delivery_id}.zip` that answers it.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer field | Type |
|---|---|
| `archive.id` | string |
| `archive.kind` | string |
| `archive.files` | integer |
| `archive.download` | string |

A voice file (`persona`) carries the voice's approved recording and a short take of it as an audition: make one with `POST /api/performance` first, or the export says so. A voice designed from words needs no `permission`; one cloned from a person's recording does.

<a id="post-api-portable-preview"></a>

#### `POST /api/portable/preview`

Verify a portable ZIP without importing it.

**Request:** file bytes (`application/octet-stream`).

**Answer:** `200`, JSON. `archive`: its `kind`, `title`, `author`, `voices` (each with `label`, `engine_id`, `kind`, `model_installed`, and for a voice with a recording its `sample`: `source_type` (`designed` or `recording`), `origin`, `allowed_use` and `transcript`), `assets` and `archive_sha256`, which the import needs.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer field | Type |
|---|---|
| `archive.kind` | string |
| `archive.title` | string |
| `archive.voices[].label` | string |
| `archive.voices[].engine_id` | string |
| `archive.voices[].kind` | string |
| `archive.voices[].model_installed` | boolean |
| `archive.voices[].sample.source_type` | string |
| `archive.voices[].sample.origin` | string |
| `archive.voices[].sample.allowed_use` | string |
| `archive.archive_sha256` | string |

<a id="post-api-portable-voice-preview"></a>

#### `POST /api/portable/voice-preview`

Play a verified archive voice sample without importing.

| Query parameter | Type | Meaning |
|---|---|---|
| `asset` (required) | string | Source voice sample asset. |

**Request:** file bytes (`application/octet-stream`).

**Answer:** `200`, `audio/wav`. The verified WAV in the supplied archive.

<a id="post-api-portable-import"></a>

#### `POST /api/portable/import`

Import the exact portable ZIP previewed locally.

**Request:** file bytes (`application/octet-stream`), and an `X-Reference-Metadata` header.

| `X-Reference-Metadata` field | Type | Meaning |
|---|---|---|
| `archive_sha256` (required) | string | The `archive_sha256` of `POST /api/portable/preview` for these exact bytes. |
| `permission` | boolean | True once the person has seen the recording's origin and allowed use; needed for a voice that carries a recording. |
| `character_id` | string | A voice file: add the voice to this character instead of a new one. |
| `archive_voice_id` | string | A project archive: import only this one of its voices. |
| `voices` | object | A project archive: which Cast voice speaks for each of its voices. |
| `include_media` | boolean | A project archive: also import its cover and other files. |
| `import_voice_samples` | boolean | A project archive: also import its voices' recordings. |

**Answer:** `201`, JSON. `imported`: for a voice file, `kind` (`persona`) and `identity`, the new voice as in `GET /api/cast`, a `candidate` until it is approved; for a project, the new project's ids.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

| Answer field | Type |
|---|---|
| `imported.kind` | string |
| `imported.identity.id` | string |
| `imported.identity.status` | string |
| `imported.identity.references[].id` | string |

Send the file's bytes as the body, and the fields below as base64url-encoded JSON in the `X-Reference-Metadata` header.

<a id="get-api-portable-delivery_id-zip"></a>

#### `GET /api/portable/{delivery_id}.zip`

Read a portable ZIP.

| Path parameter | Meaning |
|---|---|
| `delivery_id` | A saved delivery or audition ID. |

**Answer:** `200`, a file. The generated local archive.

**Suite API 1:** the other apps of Fel's Studio rely on this route. Its fields keep their names and types until the suite API number goes up ([For Fel's other apps](CONNECT.md#for-fels-other-apps)).

<a id="post-api-audio-tools-sound"></a>

#### `POST /api/audio-tools/sound`

Create a seeded procedural sound effect.

| Request field | Type | Meaning |
|---|---|---|
| `description` (required) | string | Description naming one supported sound. |
| `seconds` | number | Length in seconds. |
| `seed` | integer | Repeatable sound seed. |

**Answer:** `201`, JSON. Audio and sound receipt.

<a id="post-api-audio-tools-master"></a>

#### `POST /api/audio-tools/master`

Master a local WAV or FLAC to a separate WAV.

**Request:** file bytes (`application/octet-stream`).

**Answer:** `201`, JSON. Audio and measured output receipt. Send X-Reference-Metadata with base64url JSON settings.

<a id="get-api-audio-tools-delivery_id-audio-wav"></a>

#### `GET /api/audio-tools/{delivery_id}/audio.wav`

Stream a saved audio tool WAV.

| Path parameter | Meaning |
|---|---|
| `delivery_id` | A saved delivery or audition ID. |

**Answer:** `200`, `audio/wav`. The saved WAV.

<a id="get-api-audio-tools-delivery_id-receipt-json"></a>

#### `GET /api/audio-tools/{delivery_id}/receipt.json`

Read an audio tool receipt.

| Path parameter | Meaning |
|---|---|
| `delivery_id` | A saved delivery or audition ID. |

**Answer:** `200`, JSON. Provenance and readings from the saved file.

<a id="post-api-audio-tools-delivery_id-dub-background"></a>

#### `POST /api/audio-tools/{delivery_id}/dub-background`

Attach a generated effect as the dub background.

| Path parameter | Meaning |
|---|---|
| `delivery_id` | A saved delivery or audition ID. |

| Request field | Type | Meaning |
|---|---|---|
| `scene_id` (required) | string | Timed scene ID. |

**Answer:** `201`, JSON. The prepared background stem.

### Books

Local manuscript import, editable chapter scenes, chapter auditions and audiobook packages.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/books`](#get-api-books) | List saved books. |
| POST | [`/api/books/import/preview`](#post-api-books-import-preview) | Preview an uploaded manuscript without creating a book. |
| POST | [`/api/books`](#post-api-books) | Create a book and its editable chapter scenes. |
| GET | [`/api/books/{book_id}`](#get-api-books-book_id) | Open a saved book and its latest published delivery. |
| POST | [`/api/books/{book_id}`](#post-api-books-book_id) | Save book metadata, chapter order or pronunciation rules. |
| POST | [`/api/books/{book_id}/cover`](#post-api-books-book_id-cover) | Attach a local JPEG or PNG cover. |
| GET | [`/api/books/{book_id}/cover.jpg`](#get-api-books-book_id-cover-jpg) | Read the saved book cover. |
| POST | [`/api/books/{book_id}/queue`](#post-api-books-book_id-queue) | Queue missing or explicitly selected chapter renders. |
| POST | [`/api/books/{book_id}/chapters/{scene_id}/choose`](#post-api-books-book_id-chapters-scene_id-choose) | Select current finished chapter takes. |
| POST | [`/api/books/{book_id}/chapters/{scene_id}/audition`](#post-api-books-book_id-chapters-scene_id-audition) | Assemble a chapter audition from selected takes. |
| POST | [`/api/books/{book_id}/exports`](#post-api-books-book_id-exports) | Create a verified audiobook package. |
| POST | [`/api/books/{book_id}/exports/{delivery_id}/verify`](#post-api-books-book_id-exports-delivery_id-verify) | Verify an audiobook package. |
| GET | [`/api/books/{book_id}/exports/{delivery_id}/{book_file}`](#get-api-books-book_id-exports-delivery_id-book_file) | Stream a saved audiobook delivery file. |
| GET | [`/api/books/{book_id}/auditions/{delivery_id}/{book_file}`](#get-api-books-book_id-auditions-delivery_id-book_file) | Stream a saved chapter audition. |

<a id="get-api-books"></a>

#### `GET /api/books`

List saved books.

**Answer:** `200`, JSON. Book IDs, titles and chapter counts.

<a id="post-api-books-import-preview"></a>

#### `POST /api/books/import/preview`

Preview an uploaded manuscript without creating a book.

| Query parameter | Type | Meaning |
|---|---|---|
| `name` (required) | string | TXT or Markdown file name. |
| `split` | string | sentence, paragraph or chapter. |

**Request:** file bytes (`application/octet-stream`).

**Answer:** `200`, JSON. Extracted chapters, passages, speakers, warnings, source SHA-256 and locally measured model speed.

| Also answers | When |
|---|---|
| `413` | The manuscript exceeds 24 MiB. |

<a id="post-api-books"></a>

#### `POST /api/books`

Create a book and its editable chapter scenes.

| Request field | Type | Meaning |
|---|---|---|
| `metadata` (required) | object | Title and optional author, narrator, genre, description and year. |
| `chapters` (required) | object[] | Reviewed chapter and passage records from manuscript preview. |
| `voices` (required) | object | Named speakers and optional empty narrator label mapped to approved Cast IDs. |
| `source` | object | Optional manuscript file name and SHA-256. |
| `paragraph_pause_ms` | integer | Default pause after a paragraph. |
| `gap_ms` | integer | Silence between chapters. |
| `pronunciations` | object | Project phrases mapped to spoken replacements. |

**Answer:** `201`, JSON. Saved book, chapter scene IDs and cast warnings.

<a id="get-api-books-book_id"></a>

#### `GET /api/books/{book_id}`

Open a saved book and its latest published delivery.

| Path parameter | Meaning |
|---|---|
| `book_id` | A book project ID returned from `GET /api/books`. |

**Answer:** `200`, JSON. Book details, chapter statuses, estimated duration and latest delivery.

<a id="post-api-books-book_id"></a>

#### `POST /api/books/{book_id}`

Save book metadata, chapter order or pronunciation rules.

| Path parameter | Meaning |
|---|---|
| `book_id` | A book project ID returned from `GET /api/books`. |

| Request field | Type | Meaning |
|---|---|---|
| `expected_revision` (required) | integer | Current book revision. |
| `metadata` | object | Book metadata. |
| `gap_ms` | integer | Silence between chapters. |
| `chapters` | string[] | Existing chapter IDs in their new order. |
| `pronunciations` | object | Project phrases mapped to spoken replacements. |

**Answer:** `200`, JSON. Revised book and chapter statuses.

<a id="post-api-books-book_id-cover"></a>

#### `POST /api/books/{book_id}/cover`

Attach a local JPEG or PNG cover.

| Path parameter | Meaning |
|---|---|
| `book_id` | A book project ID returned from `GET /api/books`. |

| Query parameter | Type | Meaning |
|---|---|---|
| `revision` (required) | integer | Current book revision. |

**Request:** file bytes (`application/octet-stream`).

**Answer:** `201`, JSON. Book with its normalized JPEG cover identity.

| Also answers | When |
|---|---|
| `413` | The image exceeds 5 MiB. |

<a id="get-api-books-book_id-cover-jpg"></a>

#### `GET /api/books/{book_id}/cover.jpg`

Read the saved book cover.

| Path parameter | Meaning |
|---|---|
| `book_id` | A book project ID returned from `GET /api/books`. |

**Answer:** `200`, a file. The normalized and verified JPEG cover.

<a id="post-api-books-book_id-queue"></a>

#### `POST /api/books/{book_id}/queue`

Queue missing or explicitly selected chapter renders.

| Path parameter | Meaning |
|---|---|
| `book_id` | A book project ID returned from `GET /api/books`. |

| Request field | Type | Meaning |
|---|---|---|
| `chapters` | string[] | Chapter scene IDs. Omit to queue the book. |
| `rerender` | boolean | Generate new takes for every segment instead of missing segments only. |

**Answer:** `202`, JSON. Persistent scene job IDs and updated chapter status. Resume and cancel use the scene job endpoints.

<a id="post-api-books-book_id-chapters-scene_id-choose"></a>

#### `POST /api/books/{book_id}/chapters/{scene_id}/choose`

Select current finished chapter takes.

| Path parameter | Meaning |
|---|---|
| `book_id` | A book project ID returned from `GET /api/books`. |
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `200`, JSON. Chapter scene and chosen takes.

<a id="post-api-books-book_id-chapters-scene_id-audition"></a>

#### `POST /api/books/{book_id}/chapters/{scene_id}/audition`

Assemble a chapter audition from selected takes.

| Path parameter | Meaning |
|---|---|
| `book_id` | A book project ID returned from `GET /api/books`. |
| `scene_id` | A scene's id, from `GET /api/scenes`. |

**Answer:** `201`, JSON. Checked chapter mix and its local audio URL.

<a id="post-api-books-book_id-exports"></a>

#### `POST /api/books/{book_id}/exports`

Create a verified audiobook package.

| Path parameter | Meaning |
|---|---|
| `book_id` | A book project ID returned from `GET /api/books`. |

| Request field | Type | Meaning |
|---|---|---|
| `expected_revision` (required) | integer | Current book revision. |
| `formats` | string[] | WAV, FLAC, MP3 and M4B, in lowercase. Omit for all formats. |

**Answer:** `201`, JSON. Delivery ID, chapter offsets, audio levels, files and selected-take provenance.

<a id="post-api-books-book_id-exports-delivery_id-verify"></a>

#### `POST /api/books/{book_id}/exports/{delivery_id}/verify`

Verify an audiobook package.

| Path parameter | Meaning |
|---|---|
| `book_id` | A book project ID returned from `GET /api/books`. |
| `delivery_id` | A saved delivery or audition ID. |

**Answer:** `200`, JSON. Verification result and number of checked files and chapters.

<a id="get-api-books-book_id-exports-delivery_id-book_file"></a>

#### `GET /api/books/{book_id}/exports/{delivery_id}/{book_file}`

Stream a saved audiobook delivery file.

| Path parameter | Meaning |
|---|---|
| `book_id` | A book project ID returned from `GET /api/books`. |
| `delivery_id` | A saved delivery or audition ID. |
| `book_file` | A verified chapter or audiobook file name. |

**Answer:** `200`, a file. A declared file from the delivery.

| Also answers | When |
|---|---|
| `416` | The requested range is outside the file. |

<a id="get-api-books-book_id-auditions-delivery_id-book_file"></a>

#### `GET /api/books/{book_id}/auditions/{delivery_id}/{book_file}`

Stream a saved chapter audition.

| Path parameter | Meaning |
|---|---|
| `book_id` | A book project ID returned from `GET /api/books`. |
| `delivery_id` | A saved delivery or audition ID. |
| `book_file` | A verified chapter or audiobook file name. |

**Answer:** `200`, a file. The chapter WAV or its manifest.

| Also answers | When |
|---|---|
| `416` | The requested range is outside the file. |

### Exports

Delivery packages: a scene's selected takes, converted, checked and written to `out/exports`.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/exports`](#get-api-exports) | List export packages and say which formats FFmpeg can write. |
| GET | [`/api/ffmpeg/install`](#get-api-ffmpeg-install) | Say how the latest FFmpeg install is getting on. |
| POST | [`/api/ffmpeg/install`](#post-api-ffmpeg-install) | Install FFmpeg into the Lab's own folder, the build `offer` names: read the plan, or run the installer. |
| POST | [`/api/exports/preview`](#post-api-exports-preview) | Check an export request and report what it would write, writing nothing. |
| POST | [`/api/exports`](#post-api-exports) | Write an export package. An existing package is never overwritten. |
| POST | [`/api/exports/{export_id}/verify`](#post-api-exports-export_id-verify) | Check a written package against its manifest. |

<a id="get-api-exports"></a>

#### `GET /api/exports`

List export packages and say which formats FFmpeg can write.

| Query parameter | Type | Meaning |
|---|---|---|
| `recheck` | string | `1` asks FFmpeg again, after an install. |

**Answer:** `200`, JSON. `exports` (newest first, each with its `name`, `status` and, when complete, its scene, formats and counts) and `ffmpeg` (`found`, `ready`, `version`, `formats`, a `message`, `own` (it is the Lab's own, which Install FFmpeg put in) and `offer` (the FFmpeg `POST /api/ffmpeg/install` puts in: `version`, `builder`, `license`, `download_bytes`, `disk_bytes`; `null` when the FFmpeg in use writes every format or is the Lab's own)).

<a id="get-api-ffmpeg-install"></a>

#### `GET /api/ffmpeg/install`

Say how the latest FFmpeg install is getting on.

**Answer:** `200`, JSON. `install`: the latest FFmpeg install since the service started, or `null`: `engine` (`ffmpeg`), `name`, `state` (`running`, `finished` or `failed`), `started_at`, `ended_at`, `seconds`, `exit_code`, `lines` (the end of its output) and `log`.

<a id="post-api-ffmpeg-install"></a>

#### `POST /api/ffmpeg/install`

Install FFmpeg into the Lab's own folder, the build `offer` names: read the plan, or run the installer.

| Request field | Type | Meaning |
|---|---|---|
| `dry_run` | boolean | Only read the plan; nothing is changed. Default false, which starts the install. |

**Answer:** `200`, JSON. With `dry_run`, `plan`: `engine`, `ok`, `paragraphs` and `problems`, as for an engine. Otherwise `install`: the job that started, as `GET /api/ffmpeg/install` gives it, which is the route to poll. It runs `tools/install_ffmpeg.py --yes`, which downloads the build, checks its SHA-256 and unpacks `ffmpeg.exe` and `ffprobe.exe` into `ffmpeg/`; from then on the Lab uses that FFmpeg. Speech is not held up while it runs.

| Also answers | When |
|---|---|
| `400` | `dry_run` is not true or false. |
| `409` | An install, of FFmpeg or of a model, is already running. |

<a id="post-api-exports-preview"></a>

#### `POST /api/exports/preview`

Check an export request and report what it would write, writing nothing.

| Request field | Type | Meaning |
|---|---|---|
| `scene_id` (required) | string | The scene to export. |
| `formats` | string[] | `wav`, `flac`, `ogg`, `mp3`, `m4a`. Default `["wav"]`. |
| `wav_precision` | string | PCM_16, PCM_24 or FLOAT for WAV. Default PCM_16. |
| `filename_template` | string | Default `{line_id}`. |
| `name` | string | The package folder's name. |
| `output_rate` | integer | Sample rate in hertz. |
| `trim_start_seconds` | number | Cut from the start of each clip. |
| `trim_end_seconds` | number | Cut from the end of each clip. |
| `fade_in_ms` | integer | Fade in. |
| `fade_out_ms` | integer | Fade out. |
| `loudness_lufs` | number | Normalize each clip to this loudness. |
| `loudness_true_peak` | number | True-peak ceiling with `loudness_lufs`. Default -1. |
| `limiter_dbfs` | number | Peak limiter ceiling. |

**Answer:** `200`, JSON. `dry_run` and `log`: the plan, line by line.

<a id="post-api-exports"></a>

#### `POST /api/exports`

Write an export package. An existing package is never overwritten.

| Request field | Type | Meaning |
|---|---|---|
| `scene_id` (required) | string | The scene to export. |
| `formats` | string[] | `wav`, `flac`, `ogg`, `mp3`, `m4a`. Default `["wav"]`. |
| `wav_precision` | string | PCM_16, PCM_24 or FLOAT for WAV. Default PCM_16. |
| `filename_template` | string | Default `{line_id}`. |
| `name` | string | The package folder's name. |
| `output_rate` | integer | Sample rate in hertz. |
| `trim_start_seconds` | number | Cut from the start of each clip. |
| `trim_end_seconds` | number | Cut from the end of each clip. |
| `fade_in_ms` | integer | Fade in. |
| `fade_out_ms` | integer | Fade out. |
| `loudness_lufs` | number | Normalize each clip to this loudness. |
| `loudness_true_peak` | number | True-peak ceiling with `loudness_lufs`. Default -1. |
| `limiter_dbfs` | number | Peak limiter ceiling. |

**Answer:** `201`, JSON. `ok`, `export` (the package's summary) and `log`.

| Also answers | When |
|---|---|
| `409` | Another package is still being written. |

<a id="post-api-exports-export_id-verify"></a>

#### `POST /api/exports/{export_id}/verify`

Check a written package against its manifest.

| Path parameter | Meaning |
|---|---|
| `export_id` | An export package's folder name under `out/exports`, from `GET /api/exports`. |

**Answer:** `200`, JSON. `ok`, `name` and `log`.

| Also answers | When |
|---|---|
| `400` | The package is missing or does not verify. |

### Comparisons

Listening comparisons and blind A/B sessions. See [evaluation-schema-v1.md](evaluation/evaluation-schema-v1.md).

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/evaluations/suites`](#get-api-evaluations-suites) | List the named test suites. |
| GET | [`/api/evaluations`](#get-api-evaluations) | List registered runs. |
| GET | [`/api/evaluations/runs/{run_id}`](#get-api-evaluations-runs-run_id) | Read a run with its samples, unblinded. |
| POST | [`/api/evaluations/runs/import`](#post-api-evaluations-runs-import) | Register a checksummed run manifest from `out/comparisons`. |
| POST | [`/api/evaluations/cast-import`](#post-api-evaluations-cast-import) | Make a run from saved Cast takes, to compare them. |
| GET | [`/api/evaluations/blind-sessions`](#get-api-evaluations-blind-sessions) | List blind listening sessions. |
| POST | [`/api/evaluations/blind-sessions`](#post-api-evaluations-blind-sessions) | Start a blind A/B session over a run. |
| GET | [`/api/evaluations/blind-sessions/{session_id}`](#get-api-evaluations-blind-sessions-session_id) | Read a session. Models stay hidden until reveal. |
| POST | [`/api/evaluations/blind-sessions/{session_id}/ratings`](#post-api-evaluations-blind-sessions-session_id-ratings) | Save or revise the scores for a trial. |
| POST | [`/api/evaluations/blind-sessions/{session_id}/reveal`](#post-api-evaluations-blind-sessions-session_id-reveal) | Reveal which engine made which sample, once every trial is scored on both sides. |
| GET | [`/api/evaluations/blind-audio/{item_id}`](#get-api-evaluations-blind-audio-item_id) | Play one concealed audition copy. |
| GET | [`/api/evaluations/items/{item_id}`](#get-api-evaluations-items-item_id) | Read the diagnostics for a sample, unblinded. |
| POST | [`/api/evaluations/items/{item_id}/transcript`](#post-api-evaluations-items-item_id-transcript) | Attach a heard transcript to a sample. |

<a id="get-api-evaluations-suites"></a>

#### `GET /api/evaluations/suites`

List the named test suites.

**Answer:** `200`, JSON. The suite definitions.

<a id="get-api-evaluations"></a>

#### `GET /api/evaluations`

List registered runs.

**Answer:** `200`, JSON. `runs`.

<a id="get-api-evaluations-runs-run_id"></a>

#### `GET /api/evaluations/runs/{run_id}`

Read a run with its samples, unblinded.

| Path parameter | Meaning |
|---|---|
| `run_id` | A registered comparison run's id, from `GET /api/evaluations`. |

**Answer:** `200`, JSON. `run`.

<a id="post-api-evaluations-runs-import"></a>

#### `POST /api/evaluations/runs/import`

Register a checksummed run manifest from `out/comparisons`.

| Request field | Type | Meaning |
|---|---|---|
| `manifest_path` (required) | string | The manifest's path. |

**Answer:** `201`, JSON. `run`.

<a id="post-api-evaluations-cast-import"></a>

#### `POST /api/evaluations/cast-import`

Make a run from saved Cast takes, to compare them.

| Request field | Type | Meaning |
|---|---|---|
| `take_ids` (required) | string[] | The takes to compare. |
| `title` | string | Default `Saved voice comparison`. |

**Answer:** `201`, JSON. `run`.

<a id="get-api-evaluations-blind-sessions"></a>

#### `GET /api/evaluations/blind-sessions`

List blind listening sessions.

**Answer:** `200`, JSON. `sessions`.

<a id="post-api-evaluations-blind-sessions"></a>

#### `POST /api/evaluations/blind-sessions`

Start a blind A/B session over a run.

| Request field | Type | Meaning |
|---|---|---|
| `run_id` (required) | string | The run to compare. |
| `evaluator` | string | Who is listening. Default `local listener`. |

**Answer:** `201`, JSON. `session`: trials with A and B labels and opaque audio URLs only.

<a id="get-api-evaluations-blind-sessions-session_id"></a>

#### `GET /api/evaluations/blind-sessions/{session_id}`

Read a session. Models stay hidden until reveal.

| Path parameter | Meaning |
|---|---|
| `session_id` | A blind listening session's id, from `GET /api/evaluations/blind-sessions`. |

**Answer:** `200`, JSON. `session`.

<a id="post-api-evaluations-blind-sessions-session_id-ratings"></a>

#### `POST /api/evaluations/blind-sessions/{session_id}/ratings`

Save or revise the scores for a trial.

| Path parameter | Meaning |
|---|---|
| `session_id` | A blind listening session's id, from `GET /api/evaluations/blind-sessions`. |

| Request field | Type | Meaning |
|---|---|---|
| `trial_id` (required) | string | The trial. |
| `ratings` (required) | object | `left` and `right`, each with integer scores 1 to 5 for `intelligibility`, `identity_consistency`, `acting_fit`, `naturalness` and `artifacts`; and an optional `preference` of `A`, `B`, `tie` or null. |
| `notes` | string | Free text. |

**Answer:** `200`, JSON. `session`.

<a id="post-api-evaluations-blind-sessions-session_id-reveal"></a>

#### `POST /api/evaluations/blind-sessions/{session_id}/reveal`

Reveal which engine made which sample, once every trial is scored on both sides.

| Path parameter | Meaning |
|---|---|
| `session_id` | A blind listening session's id, from `GET /api/evaluations/blind-sessions`. |

**Answer:** `200`, JSON. `session`, now revealed and locked.

<a id="get-api-evaluations-blind-audio-item_id"></a>

#### `GET /api/evaluations/blind-audio/{item_id}`

Play one concealed audition copy.

| Path parameter | Meaning |
|---|---|
| `item_id` | A compared sample's id. |

**Answer:** `200`, `audio/wav`. The WAV.

<a id="get-api-evaluations-items-item_id"></a>

#### `GET /api/evaluations/items/{item_id}`

Read the diagnostics for a sample, unblinded.

| Path parameter | Meaning |
|---|---|
| `item_id` | A compared sample's id. |

**Answer:** `200`, JSON. `item`.

<a id="post-api-evaluations-items-item_id-transcript"></a>

#### `POST /api/evaluations/items/{item_id}/transcript`

Attach a heard transcript to a sample.

| Path parameter | Meaning |
|---|---|
| `item_id` | A compared sample's id. |

| Request field | Type | Meaning |
|---|---|---|
| `heard_text` (required) | string | What was heard. |
| `evaluator` (required) | object | Who or what transcribed it: its `name` and `version`. |

**Answer:** `200`, JSON. `item_id` and `diagnostics`, including the comparison with the expected text: word and character error rates and the differences.

### Conversion

Voice conversion: say a recording again in another voice.

| Method | Path | What it does |
|---|---|---|
| POST | [`/api/convert`](#post-api-convert) | Say a recording again in another voice, with Chatterbox voice conversion. |
| POST | [`/api/conversion/uploads`](#post-api-conversion-uploads) | Keep a recording from this computer as a source performance or a target voice for voice conversion. |

<a id="post-api-convert"></a>

#### `POST /api/convert`

Say a recording again in another voice, with Chatterbox voice conversion.

| Request field | Type | Meaning |
|---|---|---|
| `source_path` (required) | string | The performance to convert: a WAV, FLAC or Ogg file of up to 60 seconds in the Lab's reference, saved-voice, Cast, scene or conversion folders, such as a file kept with `POST /api/conversion/uploads`. |
| `target_reference_path` (required) | string | The voice to convert to: a WAV, FLAC or Ogg recording of up to 10 seconds in the reference, saved-voice, Cast reference or conversion folders. A file kept with `POST /api/conversion/uploads` must have been sent as a target. |
| `target_reference_id` | string | A Cast voice recording; its file must be the target file. |
| `wait_seconds` | number | Wait in line up to this many seconds (at most 3600) while the model worker is busy, instead of the 409 answered at once without it. See `GET /api/queue`. |
| `wait_priority` | string | `normal` (the default), or `low` to wait behind the others, as a scene batch's lines do. |

**Answer:** `201`, JSON. `conversion`: `conversion_id`, `audio_url`, `audio_path`, `manifest_path` and the manifest, which names both inputs by checksum, and for a file chosen from this computer its original name and, for the target, the origin and allowed use it was given.

| Also answers | When |
|---|---|
| `409` | The model worker is busy and the request sent no `wait_seconds`, waited that long, or found 20 requests already waiting. Or, with `code` `gpu_busy`, the model would load on the graphics card while another app of Fel's Studio has it to itself (NPC Forge's One model at a time): refused at once, even with `wait_seconds`, and `holder` names that app, what it is doing and since when. |
| `499` | The request was cancelled with `POST /api/queue/{request_id}/cancel`: while it waited, or while it ran. |

<a id="post-api-conversion-uploads"></a>

#### `POST /api/conversion/uploads`

Keep a recording from this computer as a source performance or a target voice for voice conversion.

**Request:** the recording's bytes, and an `X-Reference-Metadata` header.

**Answer:** `201`, JSON. `upload`: `path` (for `POST /api/convert`), `role`, `original_name`, `original_sha256`, `uploaded_at`, `duration_seconds`, `sample_rate_hz` and, for a target, `permission`, `origin` and `allowed_use`.

| Also answers | When |
|---|---|
| `413` | The recording is over 32 MiB. |

Send the recording's bytes as the body (WAV, FLAC, Ogg, MP3 or M4A) and, in the `X-Reference-Metadata` header, base64url-encoded JSON: `{"filename": "take.mp3", "role": "source"}` for the performance, or `{"filename": "voice.wav", "role": "target", "permission": true, "origin": "...", "allowed_use": "..."}` for the voice to convert to, which is refused without all three. `filename` sets the format and the label, and names nothing on disk: the recording is kept as a mono WAV named by the SHA-256 of the bytes sent, in `out/conversions/uploads/source` or `.../target`, so the same file sent twice is kept once, with a JSON of the rest beside it. A source may be 0.5 to 60 seconds long and a target 0.5 to 10.

### System

The service's report, folder sizes, library backups and housekeeping.

| Method | Path | What it does |
|---|---|---|
| GET | [`/api/asr/install`](#get-api-asr-install) | Check local speech recognition and its install progress. |
| POST | [`/api/asr/install`](#post-api-asr-install) | Plan or start the speech recognition install. |
| GET | [`/api/system`](#get-api-system) | Report the version, the system, the library, FFmpeg, disk space and engine states. |
| GET | [`/api/system/storage`](#get-api-system-storage) | Measure the library's folders and what a cleanup would remove. |
| GET | [`/api/system/backups`](#get-api-system-backups) | List backups, what a new one would include, and the running job. |
| POST | [`/api/system/backups`](#post-api-system-backups) | Back up the library to one ZIP, in the background. |
| POST | [`/api/system/backups/verify`](#post-api-system-backups-verify) | Read a backup back in full and check every file, in the background. |
| POST | [`/api/system/backups/restore`](#post-api-system-backups-restore) | Restore a backup into a new folder inside the library, in the background. |
| GET | [`/api/system/task`](#get-api-system-task) | Say how the running backup, check or restore is getting on. |
| POST | [`/api/system/cleanup`](#post-api-system-cleanup) | Remove old worker scratch folders, desktop sessions and service logs. |

<a id="get-api-asr-install"></a>

#### `GET /api/asr/install`

Check local speech recognition and its install progress.

**Answer:** `200`, JSON. `speech_recognition` reports installed model files. `install` contains the latest job.

<a id="post-api-asr-install"></a>

#### `POST /api/asr/install`

Plan or start the speech recognition install.

| Request field | Type | Meaning |
|---|---|---|
| `dry_run` | boolean | Return the plan without downloading anything. |

**Answer:** `202`, JSON. The install plan or the progress snapshot.

| Also answers | When |
|---|---|
| `409` | An install is running. |

<a id="get-api-system"></a>

#### `GET /api/system`

Report the version, the system, the library, FFmpeg, disk space and engine states.

| Query parameter | Type | Meaning |
|---|---|---|
| `recheck` | string | `1` asks FFmpeg again. |

**Answer:** `200`, JSON. `generated_at`, `app`, `system`, `library`, `ffmpeg` (as `GET /api/exports` gives it), `disk`, `engines`, `log`, `backups`, `setup` (states for connections, provider profiles and speech recognition), and `diagnostics`, a plain-text report with workspace counts and setup states. It omits scripts, transcripts and names.

<a id="get-api-system-storage"></a>

#### `GET /api/system/storage`

Measure the library's folders and what a cleanup would remove.

**Answer:** `200`, JSON. `library`, `other` and `cleanup`. Never walks the Drama model folder.

<a id="get-api-system-backups"></a>

#### `GET /api/system/backups`

List backups, what a new one would include, and the running job.

**Answer:** `200`, JSON. `folder`, `backups`, `plan`, `task` and `suggested_folder`.

<a id="post-api-system-backups"></a>

#### `POST /api/system/backups`

Back up the library to one ZIP, in the background.

| Request field | Type | Meaning |
|---|---|---|
| `include` | string[] | The optional sections to add: `voices`, `exports`, `drama`, `dubs` and `audio-tools`. Default: all of them. |

**Answer:** `202`, JSON. `task`: poll `GET /api/system/task`.

| Also answers | When |
|---|---|
| `409` | A backup, check or restore is already running. |

<a id="post-api-system-backups-verify"></a>

#### `POST /api/system/backups/verify`

Read a backup back in full and check every file, in the background.

| Request field | Type | Meaning |
|---|---|---|
| `name` (required) | string | The backup's file name, from the list. |

**Answer:** `202`, JSON. `task`.

| Also answers | When |
|---|---|
| `409` | A backup, check or restore is already running. |

<a id="post-api-system-backups-restore"></a>

#### `POST /api/system/backups/restore`

Restore a backup into a new folder inside the library, in the background.

| Request field | Type | Meaning |
|---|---|---|
| `name` (required) | string | The backup's file name. |
| `folder` (required) | string | A new folder's name: letters, digits, spaces, dots, dashes and underscores. |

**Answer:** `202`, JSON. `task`. The library in use is never touched.

| Also answers | When |
|---|---|
| `409` | A backup, check or restore is already running. |

<a id="get-api-system-task"></a>

#### `GET /api/system/task`

Say how the running backup, check or restore is getting on.

**Answer:** `200`, JSON. `task`: its kind, state and progress, or null.

<a id="post-api-system-cleanup"></a>

#### `POST /api/system/cleanup`

Remove old worker scratch folders, desktop sessions and service logs.

**Answer:** `200`, JSON. What was removed, and anything that could not be.

### Internal routes

These serve the Lab's own pages. They can change at any time and are not part of the API.

| Method | Path | Used for |
|---|---|---|
| GET | `/api/projects/recent` | List the five most recently changed projects. |
| GET | `/api/scenes/{scene_id}/preview` | Open the latest mix for this scene revision. |
| GET | `/` | The Lab's main page. |
| GET | `/index.html` | The Lab's main page. |
| GET | `/compare` | The Compare page. |
| GET | `/compare.html` | The Compare page. |
| GET | `/theme.js` | The appearance script. |
| GET | `/voice_lab_i18n.js` | The page's language helper. |
| GET | `/voice_lab_queue.js` | A page script. |
| GET | `/voice_workflows.js` | A page script. |
| GET | `/voice_library.js` | A page script. |
| GET | `/drama3-studio.js` | The Drama studio script. |
| GET | `/voice_lab_studio.js` | A page script. |
| GET | `/voice_lab_cast.js` | A page script. |
| GET | `/voice_lab_scenes.js` | A page script. |
| GET | `/voice_lab_books.js` | A page script. |
| GET | `/voice_lab_projects.js` | A page script. |
| GET | `/voice_lab_audio.js` | A page script. |
| GET | `/voice_lab_automation.js` | A page script. |
| GET | `/voice_lab_cast_bindings.js` | A page script. |
| GET | `/voice_lab_take_edit.js` | A page script. |
| GET | `/voice_lab_generate.js` | A page script. |
| GET | `/voice_lab_system.js` | A page script. |
| GET | `/voice_lab_app.js` | A page script. |
| GET | `/voice_lab_drama.js` | A page script. |
| GET | `/out/*` | A file from the program's `out` folder, such as a finished voice conversion and its manifest. |
| GET | `/api/preferences` | Read the saved theme, page language and update preference. |
| POST | `/api/preferences` | Save each preference supplied, keeping the saved values of the others. |
| GET | `/api/reference-audio` | Play a recording from the reference screen by path. |
| GET | `/api/references` | List the recordings the reference screen offers. |
| GET | `/api/voice-library` | List the saved voices the voice picker offers. |
| GET | `/api/conversion/sources` | List the performances the conversion picker offers. |
| GET | `/api/conversion/targets` | List the voices the conversion picker offers. |
| POST | `/api/system/use-library` | Point the desktop app at a restored library the next time it starts. |
| GET | `/api/updates` | Say which version this is and what the last check for a newer release found, without asking GitHub. |
| POST | `/api/updates/check` | Ask GitHub which release is the newest and whether it is newer than this version. |
| POST | `/api/update/plan` | Say what Update now would do: the plan of `update.bat --dry-run`. |
| POST | `/api/update/start` | Close the Lab and update it: the updater runs in a window of its own and opens the Lab again. |
| GET | `/api/update/notice` | What the last update, undo or Update now left for the page to say. |
| POST | `/api/update/notice/dismiss` | Forget the notice once the page has shown it. |
| POST | `/api/system/open-folder` | Show the Lab's folder in File Explorer. |
| GET | `/api/system/connect` | Say where other programs reach this service, for Connected apps. |
| GET | `/api/system/connected-apps` | Find NPC Forge and Altera in the order Fel's Studio uses. |
| POST | `/api/system/connect/port` | Choose the port the desktop window's service asks for, from the next time the window opens. |
| GET | `/api/drama3/*` | Everything below `/api/drama3/` that is read: `info`, and `jobs/{id}` with its `audio`, `metadata` and `package`. |
| GET | `/api/drama3/info` | The Drama runtime's capabilities, sources and jobs. |
| GET | `/api/drama3/jobs/{job_id}` | A Drama job, from an offset into its log. |
| GET | `/api/drama3/jobs/{job_id}/audio` | A Drama job's audio. |
| GET | `/api/drama3/jobs/{job_id}/metadata` | A Drama job's metadata file. |
| GET | `/api/drama3/jobs/{job_id}/package` | A Drama job's package. |
| POST | `/api/drama3/jobs` | Start a Drama job. |
| POST | `/api/drama3/jobs/{job_id}/cancel` | Cancel a Drama job. |

<!-- END GENERATED REFERENCE -->

## Keeping this page true

Every route `serve.py` answers is a row in `api_spec.py`, either documented or marked `internal=True`, and the service's
dispatch is built from that table (`routes.py`): `serve.py` registers one function for each row and will not start with a row
that no function answers, or a function for a route with no row. A path parameter is matched with the pattern the table gives
it. `tests/test_api_docs.py` checks the same, that a request made from each row's examples reaches that row, and that
`openapi.json` and the reference above are what the table gives. The routes marked "Suite API 1" also keep their fields in
`tests/suite_api_1.json`, and `tests/test_suite_api.py` fails when one changes
([For Fel's other apps](CONNECT.md#for-fels-other-apps)). After you change the table, write them again:

```powershell
.venv\Scripts\python.exe tools\build_api_docs.py
```

Only the reference between the two marker lines is generated; the rest of this page is written by hand.
