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
    required_packages = None  # Python packages its environment must hold
    min_free_ram_gb = None  # free RAM, in GB, the Lab wants before it loads the model
    sizes_install = False  # install_size() says what its Install downloads
    uses_torch = True  # its worker runs on PyTorch, so it can be asked for the graphics card
    cpu_only = None  # why the model never uses the graphics card in the Lab, for its card on the Models page

    # Voices, takes and scenes: cast_store.py, serve.py, scene_store.py, performance_settings.py, output_license.py
    direction_refusal = None  # what a line with an acting direction is told, when the model takes none
    preset_voices = False  # a voice of it is one of the model's own fixed voices (identity_options)
    languages = None  # the languages a scene line may ask of it
    performance_defaults = None  # the settings a take uses where the line sets none
    control_ui = None  # each setting's label and bounds, for the Perform and scene forms
    output_license = None  # what its code, weights and clips allow, for each take's licence profile
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

    def mode_for(self, backend_request):
        return self.generation_mode

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
