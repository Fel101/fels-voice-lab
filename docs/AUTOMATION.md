# Automation in Voice Lab

Open **Automation** to save a recipe. Pick the input type, give the recipe a name, and add steps such as speaking text,
transcribing audio, mastering a WAV, converting a voice or queuing a dub. Each step can use the original input or the
result of an earlier step. Use **Preview operations** to check the inputs and destinations. **Run saved recipe** starts
a run and lists progress and outputs. A restart preserves the completed steps of each input.

Transcription uses **Install speech recognition** in **System & backups**. Voice conversion uses an approved Cast
recording and the installed conversion model. Dubbing uses a saved timed scene and FFmpeg, available from the same
System page.

## Optional provider profiles

Open **Provider profiles** in **Automation**. Enter its name, its OpenAI-compatible base address ending in `/v1`, the
model and the operations to use. For an address outside this computer, enter an HTTPS address and a key. The Lab saves
the profile on this computer and keeps the key in memory until it closes. Re-enter the key when starting another session.
Saving the profile makes no requests to the provider. Profiles live beside the program in `out/provider-profiles.json`.
They stay there when you choose a different library with `--state-root`, and library backups leave them out.

Pick a provider in a recipe step when you want it to translate text or speak. The confirmation lists each destination
and the fields to be sent. For scripts, use the recipe preview's provider revisions when approving a run. The Lab checks
the revision again before requests, including when queued work resumes after restart.

In **Dub media**, type your translation directly or open **Ask a provider for a draft**. Choose a saved line, enter both
language codes and review the destination and fields. A draft fills the target box only after you choose to use it. Save
the line and render it once you have checked the words.

Inspired by Palash Debnath (`debpalash`) and the VoiceStudio contributors.
[VoiceStudio](https://github.com/debpalash/voicestudio)
