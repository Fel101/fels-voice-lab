"""What the service knows about the four Qwen3-TTS models outside their worker: the name tools/install_qwen.py --model takes for
each, its launcher, and the files each checkpoint must hold before it can load. The worker itself (qwen_worker.py) keeps the
repositories and revisions it loads."""

# Catalog id -> the model name setup-qwen.bat --model takes.
QWEN_MODELS = {
    "qwen3-tts-0.6b-base": "0.6b-base",
    "qwen3-tts-1.7b-base": "1.7b-base",
    "qwen3-tts-1.7b-voice-design": "1.7b-voice-design",
    "qwen3-tts-1.7b-custom-voice": "1.7b-custom-voice",
}
QWEN_INSTALLER = "tools/install_qwen.py"
QWEN_LAUNCHER = "setup-qwen.bat"
# Each checkpoint holds the model, the speech tokenizer that turns its codes into sound (a folder of its own), and the text
# tokenizer; faster-qwen3-tts cannot load without any of them.
QWEN_FILES = (
    "config.json",
    "model.safetensors",
    "merges.txt",
    "tokenizer_config.json",
    "vocab.json",
    "speech_tokenizer/config.json",
    "speech_tokenizer/model.safetensors",
)
