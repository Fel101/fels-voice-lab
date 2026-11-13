"""What every engine adapter answers, and the answer most engines give.

A family module subclasses Adapter for each of its engines and sets what is its own. None means the engine has none (no RAM
rule, no installer, no settings), and the shared tables leave it out. The methods named worker_* run inside the model's worker
process (worker_runtime.EngineWorkerRuntime calls them with itself), so their heavy imports go inside them; everything else
runs in the service, which never imports a model."""


class Adapter:
    engine_id = None

    # Installing and checking: engine_checks.py, engine_install.py, install_sizes.py, ram_needs.py, engine_devices.py
    guided_installer = None  # the launcher in the lab folder that puts its files back, with its arguments
    installer = None  # the script, or the script and its arguments, the Models page's Install runs
    gpu_build = None  # the installer module whose --gpu puts in the GPU build of PyTorch for it
    required_packages = None  # Python packages its environment must hold
    required_files = None  # files its snapshot must hold besides a weight file
    min_free_ram_gb = None  # free RAM, in GB, the Lab wants before it loads the model
    sizes_install = False  # install_size() says what its Install downloads
    uses_torch = True  # its worker runs on PyTorch, so it can be asked for the graphics card
    cpu_only = None  # why the model never uses the graphics card in the Lab, for its card on the Models page
    gpu_only = None  # why the model never runs on the CPU in the Lab, for its card on the Models page

    # Voices, takes and scenes: cast_store.py, serve.py, scene_store.py, performance_settings.py, output_license.py
    direction_refusal = None  # what a line with an acting direction is told, when the model takes none
    preset_voices = False  # a voice of it is one of the model's preset voices (identity_options)
    reference_voices = False  # a voice of it is a voice recording it clones, with its policy (reference_policy)
    reference_policy = None  # the longest voice recording it takes, why, and the sample rate it wants
    takes_transcript = False  # a voice of it keeps its recording's transcript, which each take sends
    takes_direction = False  # a take of it may carry an acting direction
    clone_check = False  # its Test clones the bundled clip, so the evaluation compares the voices
    saves_designs = False  # a take that designs a voice may be kept as a voice recording (save_voice_as)
    streams = False  # it streams its audio as it speaks (POST /api/stream)
    fixed_voices = False  # a voice of it is one or more of the model's fixed voice packs (identity_options)
    takes_voice_recordings = True  # it reads voice recordings from the library, not only the bundled one
    phonemizes = False  # its worker shows the phonemes it would speak (worker_phonemize)
    voice_per_language = False  # each of its voices speaks one language (the language matrix picks one per column)
    languages = None  # the languages a scene line may ask of it
    performance_defaults = None  # the settings a take uses where the line sets none
    control_ui = None  # each setting's label and bounds, for the Perform and scene forms
    output_license = None  # what its code, weights and clips allow, for each take's licence profile
    acted_markup = None  # the tags it acts instead of saying: "square" ([laugh]) or "round" ((laugh)), for word timing
    converts = False  # it converts a recording into another voice (POST /api/convert) and never speaks text
    default_text = None  # the text its worker is given when a request has none
    generation_mode = "clone"  # what a take records as its mode; mode_for() may say otherwise

    def check_speak(self, req, pool):
        """Refuse a speak request the engine cannot take, before the worker is taken. What it answers goes to speak_params."""
        return None

    def speak_params(self, req, pool, worker, checked):
        """What the worker's generate gets besides the text."""
        return {}

    def performance_request(self, identity, settings, applied, direction, language):
        """What a take of this voice asks of speak(), besides the engine, the text and its voice recording."""
        return {}

    def settle_settings(self, settings):
        """A take's settings, with the defaults filled in, as the model takes them."""
        return settings

    def reference_problem(self, seconds):
        """Why a voice recording this long will not do, or None."""
        return None

    def check_reference(self, path):
        """Refuse a voice recording the model cannot clone (resolve_reference_path)."""

    def reference_call(self, reference, reference_text, xvec_only, exaggeration):
        """What the worker's prepare_reference gets when the pool prepares a voice recording ahead of a take."""
        return {"path": str(reference), "exaggeration": exaggeration}

    def mode_for(self, backend_request):
        return self.generation_mode

    def take_record(self, voice_options):
        """What a take of this voice records about its voice, besides the voice itself."""
        return {}

    def identity_options(self, capabilities, req, controls, pool):
        """The options a voice of it keeps for good, from the request that creates it."""
        raise ValueError("This model has no voices of its own to choose from.")

    def smoke_request(self, base, loaded=None, root=None):
        """The shortest request that uses the engine's normal path, for its Test."""
        return base

    def matrix_request(self, request, clip, loaded=None, root=None):
        """The request the language matrix sends for one of its clips (language_matrix.request_for)."""
        return request

    def info(self):
        """Fields it adds to GET /api/info."""
        return {}

    def install_size(self, root, nvidia, card, files_bytes):
        """(download bytes, disk bytes) of a first install, when sizes_install."""
        raise NotImplementedError

    # The worker's half

    def worker_load(self, runtime, params):
        raise NotImplementedError

    def worker_prepare_reference(self, runtime, params):
        raise ValueError(f"{self.engine_id} does not take a voice recording.")

    def worker_generate(self, runtime, text, params):
        raise NotImplementedError

    def worker_unload(self, runtime):
        """What its own unload reports, besides the memory the worker freed."""
        return {}
