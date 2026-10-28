# Contributing

Use Python 3.12. Install the core requirements in a new environment; model
dependencies and weights are optional. Node is needed only for frontend tests.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements/core.txt -r requirements/lint.txt
.venv\Scripts\ruff.exe check .
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
node --test tests/ui_workspace.test.cjs tests/drama3_studio.test.cjs tests/theme.test.cjs tests/voice_workflows.test.cjs tests/voice_library.test.cjs tests/compare_cast.test.cjs tests/cast_reference_ui.test.cjs tests/engine_status.test.cjs tests/scene_history.test.cjs tests/take_edit_ui.test.cjs
.venv\Scripts\python.exe tools/check_repo.py
```

The last command checks the repository's files for secrets, personal paths, recordings, models, databases, logs and links to
pages that are not in the repository. Tests use synthetic audio and temporary stores. A guard
(`tests/real_library_guard.py`, loaded first by the full run) fails any test that
writes under the real `data/` folder or opens one of its databases, so give a test
that starts the service its own library: `library_fixture.temp_cast(self)`, passed to
`serve.make_handler`. Checks requiring optional
encoders, ASR models, FFmpeg or Windows are explicitly skipped when absent.
Contract tests do not establish perceptual voice quality. Hardware/model
acceptance should report exact runtimes, checkpoints, words, identity, acting,
resource usage and shutdown behavior separately.

The HTTP API is documented in `docs/API.md` and `openapi.json`, which are written from one table, `api_spec.py`. A route
added to or removed from `serve.py` needs a row there, documented or marked `internal=True` (the Lab's own pages and
helpers); `tests/test_api_docs.py` reads `serve.py` and fails until the table agrees. After editing the table run
`.venv\Scripts\python.exe tools/build_api_docs.py` (`--check` only reports). `/api/speak` stays one blocking call that
answers a whole WAV file, and `tests/test_api_contract.py` holds it to that.

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
