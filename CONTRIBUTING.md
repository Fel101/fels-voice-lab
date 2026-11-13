# Contributing

Use Python 3.12. Install the core requirements in a new environment; model
dependencies and weights are optional. Node is needed only for the frontend tests and the JavaScript
check.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements/core.txt -r requirements/lint.txt
.venv\Scripts\ruff.exe check .
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
node --test tests/ui_workspace.test.cjs tests/drama3_studio.test.cjs tests/theme.test.cjs tests/voice_workflows.test.cjs tests/voice_library.test.cjs tests/compare_cast.test.cjs tests/cast_reference_ui.test.cjs tests/engine_status.test.cjs tests/scene_history.test.cjs tests/take_edit_ui.test.cjs tests/updates_ui.test.cjs tests/queue_ui.test.cjs
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

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is a one-page map of the processes, the environments and which file decides what.

The HTTP API is one table, `api_spec.py`: the service's dispatch is built from it (`routes.py`), and `docs/API.md` and
`openapi.json` are written from it. To add a route, give it a row there, documented or marked `internal=True` (the Lab's own
pages and helpers), and a function in its group's module in `route_groups/`, registered in that module's
`register(router, lab)` with `@router.get(path)` or `@router.post(path)` under the same path. The service refuses to start with a row that has no function or a function that has no row, and
`tests/test_api_docs.py` checks the same. After editing the table run `.venv\Scripts\python.exe tools/build_api_docs.py`
(`--check` only reports). `/api/speak` stays one blocking call that answers a whole WAV file, and
`tests/test_api_contract.py` holds it to that.

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

## Making a release

Check for updates and `update.bat` compare `VERSION` in `app_version.py` with the newest release on GitHub, so a release is a
version number, a tag and a GitHub release that agree:

1. Set `VERSION` in `app_version.py`, and give the version its heading and date in `CHANGELOG.md` (`## 0.3.0 — 2026-11-20`,
   in place of "Unreleased"). `.venv\Scripts\python.exe tools/release_notes.py` shows the notes the release will get.
2. Run every check. On the development PC, also run `.venv\Scripts\python.exe tools/real_model_smoke.py --all-installed`
   and listen to its clips. Commit, then tag that commit `v` and the version (`git tag v0.3.0`) and push the commit and the
   tag.
3. Pushing the tag publishes the release once the checks pass. [The release workflow](.github/workflows/release.yml) runs the
   same checks as every push on the tagged commit. Then it refuses a tag that is not `v` and `VERSION`, writes the notes from
   the changelog's section with `tools/release_notes.py` (which makes the repository's links absolute at the tag), and
   publishes the release. GitHub packs the source ZIP that `update.bat` downloads. Before publishing, it builds
   `FelsVoiceLab-Setup-<version>.exe` from the tag with `tools/build_installer.py` (Inno Setup is on GitHub's Windows
   runner), writes `SHA256SUMS`, records a GitHub attestation of both, and attaches both to the release. Every push also
   builds Setup.exe, installs it silently, starts the Lab and uninstalls it (the `installer` job of
   [verify.yml](.github/workflows/verify.yml)). To build it here, install Inno Setup 6.7 or later
   (`winget install JRSoftware.InnoSetup`) and run `.venv\Scripts\python.exe tools/build_installer.py`; it writes to
   `out\installer\`.

When the checks fail on the tag, nothing is published. Do not move or delete the tag even then: fix the problem and release the
next patch number. When the checks passed and only publishing failed (GitHub was down, say), run the Release workflow again
from the Actions tab with the tag's name.

`update.bat` refuses a release whose `app_version.py` names another version than its tag. It compares the new release with
the previous one's ZIP to tell a file the project removed from one a user changed, so do not move or delete a published tag.
Each release must open the previous one's library: a database change needs its migration step (see the Cast database above).
A release that changes a model's pinned list in `requirements/` or its file list in `catalog/` makes `update.bat` tell people
to reinstall that model; one that changes `requirements/core.txt` or `requirements/desktop.txt` makes it update that
environment itself. `update.py --finish` and the record in `out/update/last.json` are how one version hands over to the next,
so keep both working as they are.
