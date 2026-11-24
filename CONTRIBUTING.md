# Contributing

Use Python 3.12. Install the core requirements in a new environment; model
dependencies and weights are optional. Node is needed only for the frontend tests and the JavaScript
check.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements/core.txt -r requirements/lint.txt
.venv\Scripts\ruff.exe check .
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
node --test "tests/*.test.cjs"
.venv\Scripts\python.exe tools/check_repo.py
npm ci
npm run check
```

The last command checks the repository's files for secrets, personal paths, recordings, models, databases, logs and links to
pages that are not in the repository. Tests use synthetic audio and temporary stores. A guard
(`tests/real_library_guard.py`, loaded first by the full run) fails any test that
writes under the real `data/` folder or opens one of its databases, so give a test
that starts the service its own library: `library_fixture.temp_cast(self)`, passed to
`serve.make_handler`. Checks requiring optional
encoders, ASR models, FFmpeg or Windows are explicitly skipped when absent.
The last two check the page's JavaScript. TypeScript's checker reads the page's scripts as one program, the way the page
loads them, so a function or variable that is misspelt, or used where no script defines it, fails the check, even in a
branch no test reaches. `npm ci` installs the one development tool, TypeScript, at the version `package-lock.json` pins,
into `node_modules`; nothing of it reaches people who use the Lab. The check reads `jsconfig.json`, which lists the scripts
in the order `serve.html` loads them (a new script goes there too), and `types/page.d.ts` declares the few names the
scripts share through `window`. It is not strict: an element from `$` can be any element. When it reports something that
is not a mistake, say so with a JSDoc type where it happens rather than switching the check off. Scripts follow the Python
rule for formatting: reformat one with Prettier (`npx prettier@3.10.1 --write <file>`, which reads `.prettierrc.json`)
only when you rework it.

Contract tests do not establish perceptual voice quality. Hardware/model
acceptance should report exact runtimes, checkpoints, words, identity, acting,
resource usage and shutdown behavior separately.

None of those tests loads a model. `tools/real_model_smoke.py` does. Close the Lab, then run
`.venv\Scripts\python.exe tools/real_model_smoke.py kokoro` (or name other models, or give `--all-installed`). It starts the
service on a free port and, for each model, runs its **Test** from the Models page, has it say one fixed sentence through
`/api/speak` and checks the WAV: the model's sample rate, a length that fits the sentence, audible and not clipped by the
Lab's own waveform checks, and the AI-generated speech label naming that model. It prints a table with each model's load time
and keeps the clips, a `report.json` and the service's log in `out\smoke\`. Listen to the clips: a clip can pass every check
and still not be speech. Its Test results are kept, as the Models page keeps them. [The real-model
workflow](.github/workflows/real-model.yml) does the same each week on an empty Windows runner: the core from `setup.bat` with
the runner's Python hidden, then Kokoro's CPU build with one voice. It downloads from PyPI and Hugging Face, so a release
does not wait for it, and it keeps the clip as an artifact.

The README's pictures come from `tools/readme_shots.py`: the real page and service, with two of Kokoro's preset voices. When
a change alters what one of them shows, close the Lab and run `.venv\Scripts\python.exe tools/readme_shots.py`. It needs
Kokoro, Microsoft Edge or Google Chrome, and FFmpeg, works in a library of its own in a temporary folder, and writes the six
pictures into `docs/images/`. Look at them before you commit them.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is a one-page map of the processes, the environments and which file decides what.

The HTTP API is one table, `api_spec.py`: the service's dispatch is built from it (`routes.py`), and `docs/API.md` and
`openapi.json` are written from it. To add a route, give it a row there, documented or marked `internal=True` (the Lab's own
pages and helpers), and a function in its group's module in `route_groups/`, registered in that module's
`register(router, lab)` with `@router.get(path)` or `@router.post(path)` under the same path. The service refuses to start
with a row that has no function or a function that has no row, and `tests/test_api_docs.py` checks the same. After editing the table run `.venv\Scripts\python.exe tools/build_api_docs.py`
(`--check` only reports). `/api/speak` stays one blocking call that answers a whole WAV file, and
`tests/test_api_contract.py` holds it to that.

The rows marked `suite=True` are the suite API, which the other apps of Fel's Studio rely on
([For Fel's other apps](docs/CONNECT.md#for-fels-other-apps)). `tests/suite_api_<N>.json` keeps their fields, and
`tests/test_suite_api.py` fails when one changes. `build_api_docs.py` adds a new route or field to that file, but refuses a
change that would break a program written for it: a field renamed, removed, retyped or newly required. Such a change needs a
new `SUITE_API` in `app_version.py` and a CHANGELOG line. `tools/suite_api_check.py` runs the four sequences of calls against
a running Lab with real models.

Lint with `ruff check .`. Format the files you add with `ruff format`: `ruff format --check .` runs in CI too, and skips the
older files listed in `ruff.toml` until someone reworks one, reformats it and takes it off the list.

The Cast database is versioned (`CAST_SCHEMA_VERSION` in `cast_store.py`, one `_step` for each version). A step that adds a
column also needs two things beside it. Every test that builds an older database by dropping columns and lowering
`user_version` (search `tests/` for `DROP COLUMN`) must drop the new column, and any index on it, too. And code that reads a
library read-only before the Lab has migrated it, as `tools/scene_export.py` does through `RetirementReader`, must cope with the
older table; `tests/test_take_edit_store.py` shows both for schema 9.

Keep changes scoped. Preserve immutable references, provenance and dry audio.
Do not bypass GPU ownership locks, enable Drama GPU cloning, remove watermarks,
download during ordinary inference, or change pinned model environments as a
side effect. Add meaningful regressions for defects in persistence, security,
worker ownership and export correctness. Never add personal audio as a fixture.

Contributions should be yours to license under the project's MIT terms; preserve
third-party licenses for borrowed code and document its source and modifications.

## Fel's Studio: the shared file and the ports

The Lab is one of the four apps of Fel's Studio. `fels_suite.py` and `tests/suite_vectors.json` are the suite's, the same in
every app's repository: do not change them here. A change goes into the suite's contract first, then into every app's copy
at once. `tests/test_fels_suite.py` runs the shared cases, and the test run gets its own temporary suite folder
(`FELS_SUITE_DIR`, set by `tests/real_library_guard.py`), so it never touches the one the apps on this computer use.

The ports the apps use. An example in the docs that needs some other port uses 8775.

| What | Port |
|---|---|
| Voice Lab | 8765 |
| NPC Forge | 8766 |
| Altera | 8767 (5001 before) |
| A second copy of NPC Forge | 8768 |
| Voice Lab's MCP server over HTTP | 8769 |
| Altera's own voice service | 8770 |
| ComfyUI | 8188 |
| LM Studio | 1234 |
| Ollama | 11434 |

## Translate the Lab

Use `locales/en.json` as the source. Copy it to `locales/<language-tag>.json`
and give the language its name as speakers write it. Keep `"draft": true` until
a speaker has checked the translation in the running Lab. A missing message uses
English. Keep model syntax such as `[laugh]`, `[voice:Mira]` and `<|speaker:0|>`
as written.

In 0.4.0, model card facts, voice descriptions, service messages outside the
common error codes, and diagnostics use English. Native file and audio controls
follow the browser's language.

Keep message keys and named values such as `{count}` and `{title}`. Move the values
where the sentence needs them. For a plural message, supply `other` and any forms
the language needs (`one`, `two`, `few`, `many` or `zero`). Preview changes by choosing
the language beside **Theme** on the main or Compare page. Check the labels,
empty pages, confirmations and messages with a library containing work.

Mark static page words with `{{ui:key}}`. Scripts use `LabText.t(key, values)`
or `LabText.plural(key, count, values)`. Pass each value by name. User text and
proper names are values. Run the Python and Node tests after adding keys or
translations.

## Making a release

Check for updates and `update.bat` compare `VERSION` in `app_version.py` with the newest release on GitHub, so a release is a
version number, a tag and a GitHub release that agree:

1. Set `VERSION` in `app_version.py`, and give the version its heading and date in `CHANGELOG.md` (`## 0.3.0 — 2026-11-20`,
   in place of "Unreleased"), and run `.venv\Scripts\python.exe tools/build_api_docs.py`, as `openapi.json` carries the
   version too. `.venv\Scripts\python.exe tools/release_notes.py` shows the notes the release will get. Keep
   that section short, as it is the release's notes: a line or two for each change under New, Changed, Fixed, After
   updating, and For scripts and contributors. Every change in detail goes in `docs/releases/<version>.md`, which the
   section links to.
2. Run every check. On the development PC, also run `.venv\Scripts\python.exe tools/real_model_smoke.py --all-installed`
   and listen to its clips. Commit, then tag that commit `v` and the version (`git tag v0.3.0`) and push the commit and the
   tag.
3. Pushing the tag publishes the release once the checks pass. [The release workflow](.github/workflows/release.yml) runs the
   same checks as every push on the tagged commit. Then it refuses a tag that is not `v` and `VERSION`, writes the notes from
   the changelog's section with `tools/release_notes.py` (which makes the repository's links absolute at the tag), builds
   `FelsVoiceLab-Setup-<version>.exe` from the tag with `tools/build_installer.py` (Inno Setup is on GitHub's Windows
   runner), writes `SHA256SUMS`, records a GitHub attestation of both, and makes the release as a draft with both
   attached. A second job, on a runner that did not build them, downloads the two files from the draft, checks their
   names, hashes and attestations (`tools/check_release_files.py`), and installs that Setup.exe over the release before
   it (`tools/check_setup_update.ps1`). Only then is the draft published, and it is marked latest only when it is the
   newest version. GitHub packs the source ZIP that `update.bat` downloads. Setup.exe carries
   the file lists of every release tag up to its version, so an update can remove what the new version dropped; the build
   needs the tags (`git fetch --tags`), and the workflows check out the whole history for them. Every push also builds
   Setup.exe, installs it silently, starts the Lab and uninstalls it, then installs the release before this version from
   GitHub and runs this Setup.exe over it (the `installer` job of [verify.yml](.github/workflows/verify.yml)). To build it here, install Inno Setup 6.7 or later
   (`winget install JRSoftware.InnoSetup`) and run `.venv\Scripts\python.exe tools/build_installer.py`; it writes to
   `out\installer\`.

When the checks fail on the tag, nothing is published; delete the draft if one was made. Do not move or delete the tag even
then: fix the problem and release the next patch number. When the checks passed and only publishing failed (GitHub was down,
say), run the Release workflow again from the Actions tab with the tag's name.

`update.bat` refuses a release whose `app_version.py` names another version than its tag. It compares the new release with
the previous one's ZIP to tell a file the project removed from one a user changed, so do not move or delete a published tag.
Each release must open the previous one's library: a database change needs its migration step (see the Cast database above).
A release that changes a model's pinned list in `requirements/` or its file list in `catalog/` makes `update.bat` tell people
to reinstall that model; one that changes `requirements/core.txt` or `requirements/desktop.txt` makes it update that
environment itself. `update.py --finish` and the record in `out/update/last.json` are how one version hands over to the next,
so keep both working as they are.
