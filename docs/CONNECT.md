# Connect other apps

Fel's Voice Lab can be the voice of other programs on this computer: an assistant such as Claude Code, any other app that
uses MCP, apps that can use OpenAI's speech API with another server, the command-line tool, and your own scripts. Everything stays on this computer. Programs on other computers cannot
reach the Lab, and a web page from another site cannot use it.

In the Lab, **System & backups** has **Connect other apps**. It shows the lines on this page with this computer's real paths,
each with a Copy button, so nothing needs typing.

[Claude Code](#claude-code) · [Other MCP apps](#other-mcp-apps) · [What the tools do](#what-the-tools-do) ·
[When the Lab is closed](#when-the-lab-is-closed) · [Apps that use OpenAI's speech API](#apps-that-use-openais-speech-api) ·
[The command-line tool](#the-command-line-tool) · [Your own programs](#your-own-programs) ·
[The other apps of Fel's Studio](#the-other-apps-of-fels-studio)

## Claude Code

1. In the Lab, open **System & backups** and press **Copy the Claude Code line** under **Connect other apps**.
2. Paste it into a command window (PowerShell or Command Prompt) and press Enter. It looks like this, with your paths:

   ```powershell
   claude mcp add --scope user --transport stdio voice-lab -- "C:\Fel's Voice Lab\.venv\Scripts\python.exe" "C:\Fel's Voice Lab\tools\mcp_server.py"
   ```

3. Start a new Claude Code session and ask it to say something aloud, for example "Say hello with the Voice Lab". Claude
   Code may ask you first whether it can use the tool.

`--scope user` adds the Lab to all your projects. Without it the Lab is added only to the project you are in. `claude mcp
list` shows whether Claude Code reaches it, `/mcp` inside a session shows its tools, and `claude mcp remove
voice-lab` takes it away again. Claude Code shows the tools as `mcp__voice-lab__speak` and so on, which is also the name to use in
its permission rules.

The line runs the server with the Lab's own Python, so nothing else needs installing. Move or reinstall the Lab in another
folder, and the line needs copying again.

## Other MCP apps

Most apps that use MCP read a settings file with an `mcpServers` section. **Copy the settings for other MCP apps** copies the
Lab's entry with your paths:

```json
{
  "mcpServers": {
    "voice-lab": {
      "command": "C:\\Fel's Voice Lab\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Fel's Voice Lab\\tools\\mcp_server.py"]
    }
  }
}
```

Where that file is, and whether the app wants the whole block or only the `voice-lab` entry inside `mcpServers`, is in the
app's own help. Open the app again after changing it.

The server speaks MCP over standard input and output (the stdio transport): protocol 2026-07-28, where each request names
its version, and the `initialize` handshake of 2025-11-25, 2025-06-18, 2025-03-26 and 2024-11-05. It needs no network and
no key. It is `tools/mcp_server.py` in the Lab's folder, and it uses only
Python's standard library, so any Python 3.8 or later runs it.

## What the tools do

| Tool | What it does |
|---|---|
| `lab_status` | Says whether the Lab is running, which models are installed and loaded, and what its queue is doing. It never starts the Lab. |
| `list_voices` | Lists the voices that can speak now: your Cast's voices and the preset voices of the installed models. Each has the name to use, its model, its languages, and whether it takes an acting direction. It can keep to one language, and list the voices that are not ready yet with the reason. |
| `speak` | Says text aloud on this computer's speakers and saves it as a WAV file, then answers with the file's path and length. Up to 10,000 characters; longer text is made in parts and joined. It takes a voice's name (else the Lab's default voice, Kokoro's `af_heart`), an acting direction for voices that take one, a language, `play: false` to save without playing, and a file to keep it in. |
| `stop_speaking` | Stops the clip that is playing, and cancels the clips being made or waiting. |
| `set_voice` | Chooses the voice `speak` uses for this connection when it names none. Other apps keep their own. |
| `describe_voice` | Says which model a voice uses, its languages, whether it takes an acting direction, and whether it can speak now. |
| `transcribe` | Writes down the words in an audio file from the connection's workspace, on this computer, with the Lab's speech recognition. It can give the words in English instead. |
| `preview_voice_candidate` | Shows where a Voice Slate recording in the workspace came from, what it may be used for, and which models can use it. |
| `import_voice_candidate` | Adds that recording to a character in Cast as a candidate voice, once the person has seen the preview and confirmed its allowed use. |

Clips are spoken one after another, never over each other. The first clip after the Lab starts also loads the voice's
model, which takes from ten seconds to more than a minute; the call waits rather than failing, and an app that asks for
progress hears where the clip stands.

`import_voice_candidate` is the one tool that adds to your library, and it adds a candidate, which you approve in Cast
as you would one added on the page. None of the tools deletes, restores or installs anything. A clip is spoken without
keeping a take, so an assistant that talks all day does not fill your Cast; it still carries the label that marks it as
AI-generated. Clips go to `out\spoken\` in the Lab's folder, one folder for each time the app ran, and the Lab's cleanup
(**System & backups**, **Clean up old scratch folders and logs**, and each start of the Lab) removes a folder a day
after the app that made it stopped. A clip saved to a file the app named is kept, and `speak` never replaces a file that
is already there.

## When the Lab is closed

The first call that needs the Lab (`list_voices` or `speak`) starts the Lab's service from its folder, with the library the
Lab's window opens, and the service stops when the app does. Opening the Lab's window meanwhile opens it on that service,
and when the service stops, the window closes and says so.

To use only a Lab that is already open, add `--no-start` at the end of the line: then a call to a closed Lab says how to open
it. The server looks for the Lab where the command-line tool does: `--url`, the `VOICE_LAB_URL` environment variable, the
suite's address file, the Lab's address file, then `http://127.0.0.1:8765` ([the basics](API.md#the-basics)). With `--url` it never starts the Lab
itself.

When something goes wrong, the server writes what happened to its standard error, which the app keeps with its own logs,
and a service it started writes `service.log` in the server's folder under `out\spoken\`.

## Apps that use OpenAI's speech API

Many chat and assistant apps can send text to any server that answers like OpenAI's speech API. The Lab answers that way,
on this computer:

1. In the Lab, press **Copy the OpenAI-style address** under **Connect other apps**. It is `http://127.0.0.1:8765/v1`
   unless the Lab is on another port.
2. In the app's speech settings, choose OpenAI (or "OpenAI-compatible"), paste the address as the base address (API base
   URL), and type any key: the Lab does not check it.
3. As the voice, give a voice's name from the Lab, such as `Mira` or `af_heart`. Apps that ask for a list of voices, as
   Open WebUI does, show the Lab's own. As the model, `tts-1` or any other name works: it means the voice's own model.

| What the app sends | What the Lab does with it |
|---|---|
| `input` | Speaks up to 10,000 characters; longer text is made in parts and joined. |
| `response_format` | MP3, the default, and Opus and AAC are made by FFmpeg: press **Install FFmpeg** on **System & backups** first, or ask for `wav`, `flac` or `pcm`. |
| `speed` | Used by a model with a speed control (Kokoro 82M: 0.5 to 2.0). Other voices speak at their own pace and refuse any other speed than 1. |
| `instructions` | An acting direction, for a voice whose model takes one. Other voices speak without it. |

Nothing is kept in your library, and every file with room for it carries the label that marks it as AI-generated. Requests
wait their turn for the model, so an app that sends a reply sentence by sentence hears the sentences in order. OpenAI's own
voices (alloy, nova and the rest) are not in the Lab.

The app must run on this computer, as a program. An app in a Docker container or on another computer cannot reach the Lab,
which answers this computer only; listening on the network needs a key and a design of its own, which the Lab does not
have yet. A web page that calls the address from your browser is refused too, as every other website is.

With the official `openai` package for Python:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8765/v1", api_key="not-needed")
with client.audio.speech.with_streaming_response.create(
    model="tts-1", voice="Mira", input="Hello from the Lab.", response_format="wav"
) as response:
    response.stream_to_file("hello.wav")
```

[The API guide](API.md#openai-style) lists the routes, `GET /v1/audio/voices` and `GET /v1/models` among them.

## The command-line tool

`tools\lab_cli.py` makes one call to the Lab from a command window or a script:

```powershell
.venv\Scripts\python.exe tools\lab_cli.py voices
.venv\Scripts\python.exe tools\lab_cli.py speak --voice Mira --text "Hello from the Lab." --out hello.wav --play
```

[The API guide](API.md#command-line-tool) lists its commands.

## Your own programs

A program on this computer can call the Lab's HTTP API at `http://127.0.0.1:8765` while the Lab is open: `GET /api/voices`
lists the voices, and `POST /api/speak` with `{"text": "...", "voice": "Mira"}` answers with a WAV file.

```powershell
Invoke-WebRequest -Method Post -Uri http://127.0.0.1:8765/api/speak -ContentType application/json -Body '{"text": "Hello.", "voice": "af_heart"}' -OutFile hello.wav
```

[The API guide](API.md) describes every call, its errors and their codes.

## The other apps of Fel's Studio

Fel's Voice Lab is one of four free apps that work together: Fel's Voice Slate records voices, Fel's NPC Forge makes game
characters, and Fel's Altera is a companion to chat with. They find each other through one folder for this Windows user,
the **suite folder**, and never read each other's own folders.

- **Where it is:** `%LOCALAPPDATA%\Fels`. To keep it somewhere else, such as another drive, set the `FELS_SUITE_DIR`
  environment variable to a full path once, then start the apps again:

  ```powershell
  setx FELS_SUITE_DIR D:\Fels
  ```

  If that folder cannot be made, for example because its drive is gone, the apps use the usual folder and the Lab's log
  says so.
- **`apps\voice-lab.json`** names the running Lab: its address (`url`), `version`, `api` number and process (`pid`). The Lab
  writes it when its service starts and removes it when it stops; a file whose process has gone is ignored. The Lab's own
  address file stays too, for tools from 0.4.0.
- **`inbox\voice-lab\`** is where another app leaves a file for the Lab. The Lab makes it when it starts. Voice Slate's
  **Send to Voice Lab** puts a `.voiceslate` clip there, and **Cast library** lists it until you import it or dismiss
  it; both send the file to the Recycle Bin. Nothing is imported without you. A program reads the same list with
  `GET /api/inbox` ([the Voice Slate guide](RECORDER_BRIDGE.md#send-to-voice-lab-the-inbox)).
- **`GET /api/health`** carries a `fels` block, `{"contract": 1, "app": "voice-lab", "version": "...", "api": 1}`, so an app
  can tell the Lab from another app on the same port.

The four apps share these rules in one file, [fels_suite.py](../fels_suite.py), the same in each app, with its test cases in
[tests/suite_vectors.json](../tests/suite_vectors.json).

### Sharing the graphics card

The apps share one **GPU lock** in the suite folder's `gpu\` folder. An app holds it while a model of its own stays on the
graphics card, and lets go when the model is unloaded. Work on the CPU takes nothing.

- **Shared, by default.** The Lab holds it shared while a voice model is loaded on the card, beside Altera's chat model or
  NPC Forge's ComfyUI. A small voice model and a chat model fit on one card side by side, so nobody waits.
- **Alone, only for NPC Forge's One model at a time.** With that switch on, Forge wants the card to itself before it makes
  pictures. It writes `gpu\alone.json`, asks the Lab to unload (`POST /api/unload`, below), and waits until every shared
  holder has let go. From the moment `alone.json` appears until Forge lets go, the Lab loads no new model on the card:
  - A request that would load one is refused at once with `409` and `code` `gpu_busy`, even if it asked to wait in line.
    `error` says who has the card, for example "NPC Forge is using the whole GPU for pictures (One model at a time) since
    10:02. Try again when it is done.", and `holder` names the app (`app`, `pid`, `what` and `since`).
  - A model already loaded keeps working until it is unloaded, so a scene being spoken finishes.
  - A scene batch waits for the card by itself: its lines say "Waiting for the GPU" and go on when Forge is done.
  - Models on the CPU, such as Phonon, keep working.
- **`POST /api/unload`**, sent with no body or `{}`, unloads every model and lets go of the lock:
  - `200` `{"ok": true}` when it is done, or when nothing was loaded.
  - `409` with `code` `busy` while the Lab is in the middle of something. `doing` says what, as the end of the sentence
    "Voice Lab is ...", for the asking app to show: `making a clip with Kokoro 82M`, `installing VoxCPM2`. Ask again a few
    seconds later.
  - A scene batch spoken with a model on the card counts as busy from its first line to its last: `doing` is
    `speaking a scene, 12 of 40 lines`, so pictures never cut a scene in two. That answer comes at once, even with
    `wait_seconds`. With no model on the card, a scene does not stop the unload.
- **`GET /api/gpu`** says who holds the lock now: `holders` (each with `app`, `name`, `what`, `since`, `alone` and
  `this_service`), `alone` (the app that has the card to itself or is clearing it, or null), `held` (whether a Lab model is
  on the card) and `device`. The Lab's page shows the same in its header: "GPU free", "GPU in use: Voice Lab: Kokoro 82M and
  Altera: a chat reply", or "GPU in use: NPC Forge, alone: pictures". Pointing at it says since when.

Two copies of the Lab still keep out of each other's way with the Lab's own model lock, as before. NPC Forge 1.0 waits on
that one, so it keeps working with this Lab.

## Local speech input

Choose **Install speech recognition** in **System & backups**. The OpenAI-style API accepts files at
`/v1/audio/transcriptions` and `/v1/audio/translations` with `model=whisper-1`. It uses Whisper Tiny for
transcription and Whisper Small for speech-to-English translation. Ask for SRT or WebVTT to import captions in Scenes.
Audio is processed on this computer.

For partial and final words, start a session with `POST /api/live` and send ordered mono 16 kHz PCM chunks. The session
supports retries of identical chunks, event replay and cancellation. Use the session's speech route for replies.
See [the service API](API.md) for request fields, limits and response events.

## Give an MCP connection a voice

Use `set_voice` to choose a voice for this connection and `describe_voice` to see what it can do. `speak` can save a
clip as WAV, MP3, FLAC, Opus or AAC; the last four need FFmpeg (**Install FFmpeg** on **System & backups**), a named
voice and `play: false`.

`transcribe` and the two Voice Slate tools read files only from the connection's workspace: add `--workspace` and a
folder to the server's line, and put the files there. Without it they say so and read nothing. With it, a file that
`speak` saves to must be in that folder too. A recording imported from Voice Slate keeps the description of its origin
that was approved in Slate, and arrives in Cast as a candidate.

For a client that uses Streamable HTTP, start Voice Lab, then run this from its folder with your workspace and the
Lab address from **System & backups**.

```powershell
.venv\Scripts\python.exe tools\mcp_server.py --http 8769 --workspace "C:/Audio/AgentWorkspace" --url http://127.0.0.1:8765
```

The adapter creates a private token file and prints its location. Add its contents to the client's `Authorization:
Bearer …` header and use `http://127.0.0.1:8769/mcp`. The adapter listens only on this computer, and each session
has its own voice settings. To use your own private file, pass `--token-file`.

## Use the n8n example on this computer

Import [the example workflow](examples/voice-lab-n8n.json) into a local n8n instance. Set both HTTP Request nodes' base
address from the Lab's **System & backups** page, then supply an audio input and a voice. The example drafts a
transcript and saves generated speech through the OpenAI-style routes. Use **Install speech recognition** first.
