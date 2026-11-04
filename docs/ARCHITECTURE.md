# How the Lab fits together

A page for anyone about to change the code: what runs where, which file decides what, and where to add a route, an engine or
a setting. [Setup](SETUP.md) says how to install it and the [API reference](API.md) what the service answers.

## Three kinds of process

```text
Fel's Voice Lab window   desktop.py in .venv-desktop (pywebview, the WebView2 runtime)
  starts and owns
the service              serve.py in .venv: the pages, the HTTP API on 127.0.0.1, the Cast, Scenes and Comparisons stores
  starts one at a time
a model worker           engine_worker.py in that model's own environment: .venv-kokoro, .venv-phonon, .venv-chatterbox,
                         .venv-breeze, .venv-fqwen, ...
```

- **The service never imports a model.** Its `.venv` holds only `requirements/core.txt` (NumPy, SciPy, SoundFile,
  pyloudnorm), so nothing a model needs can break it, and no installer ever changes it.
- **Each model runs in its own environment**, named by `runtime.python` in [catalog/engines.json](../catalog/engines.json).
  Its pinned list (for example [requirements/kokoro-cpu.txt](../requirements/kokoro-cpu.txt)) repeats the core packages at the
  same versions, because the worker imports the Lab's own modules too. Two models never share a PyTorch.
- **`worker_pool.EnginePool`** keeps at most one model loaded, checks free RAM first ([ram_needs.py](../ram_needs.py)), and
  talks to the worker in JSON lines over stdin and stdout ([worker_protocol.py](../worker_protocol.py)). Audio comes back as a
  temporary `.npy` file under `out/workers/<engine>-<pid>`. `worker_runtime.py` is the worker's side: load, generate, unload.
- **The device.** The service asks for `cpu` or `cuda` (`common.pick_device`: `runtime.local.json`, then an NVIDIA driver,
  then the CPU). A worker whose own PyTorch cannot use the card runs on the CPU and says so (`worker_runtime.usable_device`).

## A request

1. `serve.py` refuses anything that is not local (`_local_only`: the Host, Origin and Sec-Fetch-Site headers).
2. [routes.py](../routes.py) matches the path against the rows of [api_spec.py](../api_spec.py), the one table of every route,
   and finds the function `serve.make_handler` registered for that row with `@router.get(...)` or `@router.post(...)`.
3. The function works with the stores built in `make_handler`, which it closes over: `cast` (characters, voices, references
   and takes; [cast_store.py](../cast_store.py)), `scenes` ([scene_store.py](../scene_store.py)), `evaluations`, `engines` (the
   pool), `installer` (the Models page's Install button) and the rest.
4. What a handler raises and does not answer itself, `_dispatch` answers the same way for every route: 413 for a body too
   big, 404 for something that does not exist, 400 for a request the Lab cannot take, a worker's own status for its errors.

## Where things live

| Folder | What is there |
|---|---|
| `data/` (the library) | Cast, Scenes and Comparisons: SQLite databases and the media they point at. A different library is a `--state-root`. |
| `out/` | What a run makes and can lose: service and worker logs, worker scratch folders, export packages, conversions, installer receipts. |
| `hf-cache/` | Model checkpoints at pinned revisions. |
| `catalog/` | The engine catalog and the pinned file lists the installers check every download against. |
| `tools/` | The installers, setup, the exporter, backups and other command-line tools. |
| `tests/` | `test_*.py` (unittest) and `*.test.cjs` (Node), all on synthetic audio. |

## The page

`serve.html` loads ten classic scripts in order (`voice_workflows.js` first, `voice_lab_app.js` last) that share one global
scope, plus the Drama studio as a module. The first line of each script says what it owns. The Node tests run a script in a
`vm` context with a stand-in page, and `tests/page_source.cjs` gives them the whole page as one text. `theme.js` sets the
appearance before the page paints.

## Deciding things

| Question | Answered in |
|---|---|
| Is a model installed, usable, out of date? | [engine_checks.py](../engine_checks.py) |
| How much free RAM does it need? | [ram_needs.py](../ram_needs.py) (and `min_free_ram_gb` in `runtime.local.json`) |
| What may its output be used for? | [output_license.py](../output_license.py) |
| What does a performance's setting accept? | [performance_settings.py](../performance_settings.py) |
| What label does a clip carry inside its file? | [synthetic_label.py](../synthetic_label.py) |
| What does the copied report say? | [system_report.py](../system_report.py) and [hardware.py](../hardware.py) |
| Which NVIDIA build runs on this card? | `card_problems` in each installer, from [hardware.py](../hardware.py)'s `nvidia_cards`; `worker_runtime.usable_device` at run time |
| Is there a newer release, and how does a folder get it? | [updates.py](../updates.py) (the check) and [tools/update.py](../tools/update.py) (`update.bat`) |

## Adding

- **A route:** a row in `api_spec.ROUTES` (documented, or `internal=True` for the Lab's own pages), a function in
  `serve.make_handler` with `@router.get(path)` or `@router.post(path)` and the same path, then
  `.venv\Scripts\python.exe tools\build_api_docs.py` to write `docs/API.md` and `openapi.json`. The service will not start
  with a row and no function, or a function and no row.
- **An engine:** an entry in `catalog/engines.json` with its own environment, its case in `worker_runtime.py`, a RAM need in
  `ram_needs.py`, and a speech check in `engine_checks.smoke_request`. A guided installer follows
  [tools/install_phonon.py](../tools/install_phonon.py), the smallest one; the shared pieces are in `tools/install_kokoro.py`.
- **A setting for one computer:** a key in `runtime.local.json`, read with `common.runtime_setting`, and a line in
  [Setup](SETUP.md#hardware-and-state-settings).

## Style

`ruff check .` runs in CI. Files written from now on are formatted with `ruff format`; [ruff.toml](../ruff.toml) lists the older
files it leaves alone for now, so reformat one when you rework it and take it off that list. Comments say why, not what.
