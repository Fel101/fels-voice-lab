# Connect other apps

Fel's Voice Lab can be the voice of other programs on this computer: an assistant such as Claude Code, any other app that
uses MCP, the command-line tool, and your own scripts. Everything stays on this computer. Programs on other computers cannot
reach the Lab, and a web page from another site cannot use it.

In the Lab, **System & backups** has **Connect other apps**. It shows the lines on this page with this computer's real paths,
each with a Copy button, so nothing needs typing.

[Claude Code](#claude-code) · [Other MCP apps](#other-mcp-apps) · [What the tools do](#what-the-tools-do) ·
[When the Lab is closed](#when-the-lab-is-closed) · [The command-line tool](#the-command-line-tool) ·
[Your own programs](#your-own-programs)

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

Clips are spoken one after another, never over each other. The first clip after the Lab starts also loads the voice's
model, which takes from ten seconds to more than a minute; the call waits rather than failing, and an app that asks for
progress hears where the clip stands.

None of the tools deletes, restores, installs or changes anything in your library. A clip is spoken without keeping a take,
so an assistant that talks all day does not fill your Cast; it still carries the label that marks it as AI-generated. Clips
go to `out\spoken\` in the Lab's folder, one folder for each time the app ran, and the Lab's cleanup (**System & backups**,
**Clean up old scratch folders and logs**, and each start of the Lab) removes a folder a day after the app that made it
stopped. A clip saved to a file the app named is kept, and `speak` never replaces a file that is already there.

## When the Lab is closed

The first call that needs the Lab (`list_voices` or `speak`) starts the Lab's service from its folder, with the library the
Lab's window opens, and the service stops when the app does. Opening the Lab's window meanwhile opens it on that service,
and when the service stops, the window closes and says so.

To use only a Lab that is already open, add `--no-start` at the end of the line: then a call to a closed Lab says how to open
it. The server looks for the Lab where the command-line tool does: `--url`, the `VOICE_LAB_URL` environment variable, the
Lab's address file, then `http://127.0.0.1:8765` ([the basics](API.md#the-basics)). With `--url` it never starts the Lab
itself.

When something goes wrong, the server writes what happened to its standard error, which the app keeps with its own logs,
and a service it started writes `service.log` in the server's folder under `out\spoken\`.

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
