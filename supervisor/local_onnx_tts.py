from __future__ import annotations

"""Lazy, process-wide adapter for the Ryza v2ProPlus ONNX TTS package."""

import asyncio
import gc
import importlib
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PRESETS = {"normal_cn", "normal_ja", "whisper_cn", "whisper_ja"}


@dataclass(frozen=True)
class LocalOnnxTtsConfig:
    package_dir: Path
    provider: str = "cpu"
    validate_hashes: bool = False
    default_preset: str = "normal_cn"
    seed: int = 1234
    # Release the engine after this many seconds without a request. The ONNX
    # sessions hold several GiB of VRAM (hybrid) or RSS (cpu), and TTS demand is
    # bursty, so keeping it resident permanently mostly wastes memory. Set to 0
    # or a negative value to keep the engine alive forever. Reloading costs a
    # few seconds of cold start, which is far shorter than the LLM turn that
    # would precede the next TTS call.
    idle_unload_seconds: float = 30.0


class LocalOnnxTts:
    """Load the ONNX engine lazily and release it again once idle.

    All synthesis requests are serialized. The engine is built on first use and
    dropped after `idle_unload_seconds` without traffic so an idle deployment
    does not hold GPU/RAM indefinitely.
    """

    def __init__(self, config: LocalOnnxTtsConfig, output_dir: Path) -> None:
        self.config = config
        self.output_dir = output_dir
        self._engine: Any | None = None
        self._engine_lock = threading.Lock()
        self._inference_lock = asyncio.Lock()
        self._last_used = 0.0
        self._active_calls = 0
        self._idle_timer: threading.Timer | None = None

    def _schedule_idle_unload_locked(self) -> None:
        """(Re)arm the idle-release timer. Caller must hold `_engine_lock`."""
        seconds = float(self.config.idle_unload_seconds)
        if seconds <= 0:
            return
        if self._idle_timer is not None:
            self._idle_timer.cancel()
        timer = threading.Timer(seconds, self._release_if_idle)
        timer.daemon = True
        self._idle_timer = timer
        timer.start()

    def _release_if_idle(self) -> None:
        released: Any | None = None
        released_g2pw: Any | None = None
        with self._engine_lock:
            self._idle_timer = None
            if self._engine is None:
                return
            if self._active_calls > 0:
                self._schedule_idle_unload_locked()
                return
            if time.monotonic() - self._last_used < float(self.config.idle_unload_seconds):
                self._schedule_idle_unload_locked()
                return
            released = self._engine
            self._engine = None
            for mod_name in ("frontend.chinese2", "hf_space_v2proplus_onnx.frontend.chinese2"):
                ch2 = sys.modules.get(mod_name)
                if ch2 is not None and getattr(ch2, "g2pw", None) is not None:
                    released_g2pw = ch2.g2pw
                    ch2.g2pw = None
        # Drop the last reference outside the lock, then force a collection so
        # the ORT sessions (and their CUDA arenas) are actually reclaimed.
        del released, released_g2pw
        gc.collect()

    def _providers(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        provider = self.config.provider.strip().lower()
        if provider == "cpu":
            cpu = ("CPUExecutionProvider",)
            return cpu, cpu
        if provider == "cuda":
            cuda = ("CUDAExecutionProvider",)
            return cuda, cuda
        if provider == "hybrid":
            return ("CUDAExecutionProvider",), ("CPUExecutionProvider",)
        raise ValueError("local ONNX TTS provider must be cpu, cuda, or hybrid")

    def _preload_cuda_dlls(self) -> None:
        if self.config.provider.strip().lower() not in {"cuda", "hybrid"}:
            return
        # The deployment package documents that torch cu118 must be imported before
        # ONNX Runtime on Windows so the matching CUDA/cuDNN DLLs are selected.
        import torch

        cuda_version = str(torch.version.cuda or "")
        cudnn_version = str(torch.backends.cudnn.version() or "")
        # Accept both the originally documented CUDA 11.x / cuDNN 8 pair and the
        # newer CUDA 12.x / cuDNN 9 pair, matching the onnxruntime-gpu build in use.
        cuda_major = cuda_version.split(".", 1)[0]
        cudnn_major = cudnn_version[:1]
        if cuda_major not in {"11", "12"} or cudnn_major not in {"8", "9"}:
            raise RuntimeError(
                "Ryza hybrid/cuda requires PyTorch CUDA 11.x/cuDNN 8 or CUDA 12.x/cuDNN 9 preload; "
                f"got CUDA={cuda_version!r}, cuDNN={cudnn_version!r}"
            )

    def _get_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        with self._engine_lock:
            if self._engine is not None:
                return self._engine
            package_dir = self.config.package_dir.expanduser().resolve()
            if not (package_dir / "runtime" / "engine.py").is_file():
                raise FileNotFoundError(f"Ryza ONNX package is incomplete or missing: {package_dir}")
            self._preload_cuda_dlls()
            package_text = str(package_dir)
            if package_text not in sys.path:
                sys.path.insert(0, package_text)
            runtime_module = importlib.import_module("runtime")
            for mod_name in ("frontend.chinese2", "hf_space_v2proplus_onnx.frontend.chinese2"):
                ch2 = sys.modules.get(mod_name)
                if ch2 is not None and getattr(ch2, "is_g2pw", False) and getattr(ch2, "g2pw", None) is None:
                    g2pw_cls = ch2.G2PWPinyin
                    ch_dir = os.path.dirname(ch2.__file__)
                    ch2.g2pw = g2pw_cls(
                        model_dir=os.path.join(ch_dir, "G2PWModel"),
                        model_source=os.path.join(ch_dir, "bert_tokenizer"),
                        v_to_u=False,
                        neutral_tone_with_five=True,
                    )
            engine_class = runtime_module.OnnxSynthesisEngine
            model_providers, decoder_providers = self._providers()
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._engine = engine_class(
                package_dir=package_dir,
                providers=model_providers,
                decoder_providers=decoder_providers,
                validate_hashes=self.config.validate_hashes,
                output_dir=self.output_dir,
            )
            return self._engine

    @staticmethod
    def resolve_preset(text: str, requested: str, default: str) -> tuple[str, str]:
        preset = (requested or default or "normal_cn").strip().lower()
        if preset not in PRESETS:
            raise ValueError(f"unsupported Ryza preset: {preset}; available: {', '.join(sorted(PRESETS))}")
        language = "ja" if preset.endswith("_ja") else "zh"
        # With no explicit preset, select Japanese for text containing kana.
        if not requested and any("\u3040" <= char <= "\u30ff" for char in text):
            preset = "whisper_ja" if preset.startswith("whisper_") else "normal_ja"
            language = "ja"
        return preset, language

    async def synthesize(self, text: str, options: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
        requested_preset = str(options.get("preset") or options.get("voice") or "")
        preset, language = self.resolve_preset(text, requested_preset, self.config.default_preset)

        def _run() -> tuple[Path, dict[str, Any]]:
            with self._engine_lock:
                self._active_calls += 1
            try:
                engine = self._get_engine()
                result = engine.synthesize(
                    text=text,
                    language=language,
                    preset=preset,
                    seed=int(options.get("seed", self.config.seed)),
                    top_k=int(options.get("top_k", 15)),
                    top_p=float(options.get("top_p", 1.0)),
                    temperature=float(options.get("temperature", 1.0)),
                    repetition_penalty=float(options.get("repetition_penalty", 1.35)),
                    noise_scale=float(options.get("noise_scale", 0.5)),
                    fragment_strategy=str(options.get("fragment_strategy") or "sentence"),
                    rng_strategy=str(options.get("rng_strategy") or "per_fragment"),
                    whisper_sampling=str(options.get("whisper_sampling") or "default"),
                )
            finally:
                with self._engine_lock:
                    self._active_calls -= 1
                    self._last_used = time.monotonic()
                    if self._engine is not None:
                        self._schedule_idle_unload_locked()
            metadata = dict(result.metadata)
            metadata.update({"backend": "ryza_onnx", "preset": preset, "language": language})
            return Path(result.path), metadata

        async with self._inference_lock:
            return await asyncio.to_thread(_run)


def config_from_mapping(raw: dict[str, Any], workspace_root: Path) -> LocalOnnxTtsConfig:
    package_value = str(raw.get("local_onnx_tts_path") or os.getenv("CLONOTH_LOCAL_ONNX_TTS_PATH", "")).strip()
    package_dir = Path(package_value) if package_value else workspace_root.parent / "BotTTS" / "ryza_v2proplus_onnx_deploy"
    if not package_dir.is_absolute():
        package_dir = workspace_root / package_dir
    provider = str(raw.get("local_onnx_tts_provider") or os.getenv("CLONOTH_LOCAL_ONNX_TTS_PROVIDER", "cpu"))
    validate_raw = raw.get("local_onnx_tts_validate_hashes", False)
    validate_hashes = validate_raw if isinstance(validate_raw, bool) else str(validate_raw).lower() in {"1", "true", "yes", "on"}
    # Note: use an explicit None check so a value of 0 (disable unloading) is
    # preserved instead of being treated as "unset" by a truthiness test.
    idle_raw = raw.get("local_onnx_tts_idle_unload_seconds")
    if idle_raw is None:
        idle_raw = os.getenv("CLONOTH_LOCAL_ONNX_TTS_IDLE_UNLOAD_SECONDS", "30")
    idle_unload_seconds = float(idle_raw)
    return LocalOnnxTtsConfig(
        package_dir=package_dir,
        provider=provider,
        validate_hashes=validate_hashes,
        default_preset=str(raw.get("local_onnx_tts_default_preset") or "normal_cn"),
        seed=int(raw.get("local_onnx_tts_seed", 1234)),
        idle_unload_seconds=idle_unload_seconds,
    )
