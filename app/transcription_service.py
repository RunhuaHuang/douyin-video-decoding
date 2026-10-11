import gc
import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Dict, List, Tuple

from .config import DEFAULT_MODEL, MLX_SUPPORTED


@dataclass(frozen=True)
class ModelSpec:
    backend: str
    repo_id: str


class TranscriptionService:
    def __init__(self, default_model: str = DEFAULT_MODEL, default_language: str = "zh") -> None:
        self.default_model = default_model
        self.default_language = default_language
        self._sensevoice_cache: Dict[Tuple[str, str], object] = {}
        self._sensevoice_mps_disabled = False
        self._sensevoice_cuda_disabled = False

    def runtime_status(self) -> Dict[str, str]:
        return {
            "mlx": self._module_status("mlx_whisper") if MLX_SUPPORTED else "此平台不支持，请使用 SenseVoice",
            "sensevoice": self._sensevoice_status(),
        }

    @staticmethod
    def _module_status(module_name: str) -> str:
        try:
            importlib.import_module(module_name)
            return "已检测"
        except Exception as exc:
            return f"不可用，请执行 uv sync --extra asr: {exc}"

    def _sensevoice_status(self) -> str:
        try:
            importlib.import_module("funasr")
            importlib.import_module("torchaudio")
        except Exception as exc:
            return f"不可用，请执行 uv sync --extra asr: {exc}"

        device = self._preferred_sensevoice_device()
        return f"已检测（{device.upper()}）" if device != "cpu" else "已检测（CPU）"

    def normalize_model_names(self, model_names_text: str) -> List[str]:
        normalized_text = model_names_text.replace("，", ",")
        candidates = []
        for line in normalized_text.splitlines():
            candidates.extend(part.strip() for part in line.split(","))

        models = []
        seen = set()
        for candidate in candidates:
            if candidate and candidate not in seen:
                models.append(candidate)
                seen.add(candidate)

        return models or [self.default_model]

    @classmethod
    def parse_model_name(cls, model_name: str) -> ModelSpec:
        raw_value = model_name.strip() or DEFAULT_MODEL
        lowered = raw_value.lower()

        for prefix, backend in (
            ("mlx:", "mlx_whisper"),
            ("mlx_whisper:", "mlx_whisper"),
            ("sensevoice:", "sensevoice"),
        ):
            if lowered.startswith(prefix):
                return ModelSpec(backend=backend, repo_id=raw_value[len(prefix):].strip())

        backend = "sensevoice" if "sensevoice" in lowered else "mlx_whisper"
        return ModelSpec(backend=backend, repo_id=raw_value)

    @classmethod
    def model_alias(cls, model_name: str) -> str:
        spec = cls.parse_model_name(model_name)
        tail = spec.repo_id.rsplit("/", 1)[-1] if "/" in spec.repo_id else spec.repo_id
        tail = tail.replace("SenseVoice", "sensevoice")
        tail = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", tail)
        alias = re.sub(r"[^0-9A-Za-z\u4e00-\u9fa5]+", "_", tail).strip("_").lower()
        return alias or "model"

    def transcribe(self, audio_path: Path, model_name: str = "", language: str = "") -> str:
        spec = self.parse_model_name(model_name or self.default_model)
        language = language or self.default_language

        if spec.backend == "sensevoice":
            return self._transcribe_sensevoice(audio_path, spec.repo_id, language)
        return self._transcribe_mlx(audio_path, spec.repo_id, language)

    def _transcribe_mlx(self, audio_path: Path, model_name: str, language: str) -> str:
        if not MLX_SUPPORTED:
            raise RuntimeError("MLX Whisper 仅支持 Apple Silicon Mac；请改用 FunAudioLLM/SenseVoiceSmall")
        try:
            mlx_whisper = importlib.import_module("mlx_whisper")
        except Exception as exc:
            raise RuntimeError(
                "无法导入 mlx_whisper，请先执行 `uv sync --extra asr`"
            ) from exc

        transcribe = getattr(mlx_whisper, "transcribe", None)
        if transcribe is None:
            raise RuntimeError("mlx_whisper 包中未找到 transcribe 方法")

        signature = inspect.signature(transcribe)
        kwargs: Dict[str, object] = {}

        if "path_or_hf_repo" in signature.parameters:
            kwargs["path_or_hf_repo"] = model_name
        elif "model" in signature.parameters:
            kwargs["model"] = model_name
        elif "repo_id" in signature.parameters:
            kwargs["repo_id"] = model_name

        if "language" in signature.parameters:
            kwargs["language"] = language
        if "task" in signature.parameters:
            kwargs["task"] = "transcribe"
        if "verbose" in signature.parameters:
            kwargs["verbose"] = False

        result = transcribe(str(audio_path), **kwargs)

        if isinstance(result, dict):
            text = result.get("text", "")
        else:
            text = getattr(result, "text", "")

        return str(text).strip()

    def _transcribe_sensevoice(self, audio_path: Path, model_name: str, language: str) -> str:
        model, device = self._get_sensevoice_model(model_name)
        try:
            return self._run_sensevoice_generate(model, audio_path, language, device)
        except Exception as exc:
            if device in {"mps", "cuda"}:
                self._disable_sensevoice_device(device)
                self._sensevoice_cache.pop((model_name, device), None)
                try:
                    cpu_model, cpu_device = self._get_sensevoice_model(model_name)
                    return self._run_sensevoice_generate(cpu_model, audio_path, language, cpu_device)
                except Exception as retry_exc:
                    raise RuntimeError(f"SenseVoice 转写失败，{device.upper()} 和 CPU 回退都不可用: {retry_exc}") from retry_exc
            raise RuntimeError(f"SenseVoice 转写失败: {exc}") from exc

    def _run_sensevoice_generate(self, model: object, audio_path: Path, language: str, device: str) -> str:
        try:
            return self._generate_sensevoice_text(model, audio_path, language)
        finally:
            self._cleanup_sensevoice_runtime(device)

    def _get_sensevoice_model(self, model_name: str) -> Tuple[object, str]:
        last_error = None
        for device in self._sensevoice_device_candidates():
            cache_key = (model_name, device)
            if cache_key in self._sensevoice_cache:
                return self._sensevoice_cache[cache_key], device

            try:
                funasr = importlib.import_module("funasr")
                AutoModel = getattr(funasr, "AutoModel")
                model = AutoModel(
                    model=model_name,
                    vad_model="fsmn-vad",
                    vad_kwargs={"max_single_segment_time": 30000},
                    device=device,
                    hub=self._sensevoice_hub(model_name),
                    disable_update=True,
                )
                self._sensevoice_cache[cache_key] = model
                return model, device
            except Exception as exc:
                last_error = exc
                if device in {"mps", "cuda"}:
                    self._disable_sensevoice_device(device)

        raise RuntimeError(f"无法加载 SenseVoice 模型 {model_name}（请确认已执行 uv sync --extra asr）: {last_error}") from last_error

    @staticmethod
    def _sensevoice_hub(model_name: str) -> str:
        lowered = model_name.lower()
        if lowered.startswith("iic/") or lowered.startswith("damo/"):
            return "ms"
        return "hf"

    def _generate_sensevoice_text(self, model: object, audio_path: Path, language: str) -> str:
        postprocess_utils = importlib.import_module("funasr.utils.postprocess_utils")
        postprocess = getattr(postprocess_utils, "rich_transcription_postprocess")
        generate = getattr(model, "generate", None)
        if generate is None:
            raise RuntimeError("SenseVoice 模型实例中未找到 generate 方法")

        result = generate(
            input=str(audio_path),
            cache={},
            language=self._sensevoice_language(language),
            use_itn=True,
            batch_size_s=60,
            merge_vad=True,
            merge_length_s=15,
        )
        raw_text = self._extract_text(result)
        text = postprocess(raw_text) if raw_text else ""
        return str(text).strip()

    @staticmethod
    def _cleanup_sensevoice_runtime(device: str) -> None:
        gc.collect()
        if device not in {"mps", "cuda"}:
            return

        try:
            torch = importlib.import_module("torch")
        except Exception:
            return

        try:
            device_module = getattr(torch, device, None)
            empty_cache = getattr(device_module, "empty_cache", None)
            if callable(empty_cache):
                empty_cache()
        except Exception:
            return

    @staticmethod
    def _extract_text(result: object) -> str:
        if isinstance(result, dict):
            return str(result.get("text", "")).strip()
        if isinstance(result, list):
            parts = []
            for item in result:
                if isinstance(item, dict):
                    value = item.get("text", "")
                else:
                    value = getattr(item, "text", "")
                if value:
                    parts.append(str(value))
            return "".join(parts).strip()
        return str(getattr(result, "text", "")).strip()

    @staticmethod
    def _sensevoice_language(language: str) -> str:
        normalized = language.strip().lower()
        if normalized in {"auto", "zh", "en", "yue", "ja", "ko", "nospeech"}:
            return normalized
        return "auto"

    def _sensevoice_device_candidates(self) -> List[str]:
        preferred = self._preferred_sensevoice_device()
        if preferred == "mps":
            return ["cpu"] if self._sensevoice_mps_disabled else ["mps", "cpu"]
        if preferred == "cuda":
            return ["cpu"] if self._sensevoice_cuda_disabled else ["cuda", "cpu"]
        return [preferred]

    def _disable_sensevoice_device(self, device: str) -> None:
        if device == "mps":
            self._sensevoice_mps_disabled = True
        elif device == "cuda":
            self._sensevoice_cuda_disabled = True

    @staticmethod
    def _preferred_sensevoice_device() -> str:
        try:
            torch = importlib.import_module("torch")
            cuda = getattr(torch, "cuda", None)
            if cuda and cuda.is_available():
                return "cuda"
            backends = getattr(torch, "backends", None)
            mps_backend = getattr(backends, "mps", None)
            if mps_backend and mps_backend.is_available():
                return "mps"
        except Exception:
            return "cpu"
        return "cpu"
