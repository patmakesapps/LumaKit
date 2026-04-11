"""Local STT + Edge TTS helpers for Telegram voice features."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import importlib.util
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WHISPER_DIR = REPO_ROOT / ".vendor" / "whisper.cpp"
DEFAULT_WHISPER_BIN = DEFAULT_WHISPER_DIR / "build" / "bin" / "whisper-cli"
DEFAULT_WHISPER_MODEL = DEFAULT_WHISPER_DIR / "models" / "ggml-base.en.bin"
DEFAULT_EDGE_PYTHON = Path(sys.executable)

VOICE_OPTIONS = {
    "ava": "en-US-AvaNeural",
    "jenny": "en-US-JennyNeural",
    "aria": "en-US-AriaNeural",
    "guy": "en-US-GuyNeural",
    "andrew": "en-US-AndrewNeural",
    "brian": "en-US-BrianNeural",
    "emma": "en-US-EmmaNeural",
    "ana": "en-US-AnaNeural",
}


@dataclass
class SpeechConfig:
    whisper_bin: Path
    whisper_model: Path
    edge_python: Path
    default_voice: str
    tts_format: str


def load_speech_config() -> SpeechConfig:
    whisper_dir = Path(os.getenv("LUMIKIT_WHISPER_DIR", str(DEFAULT_WHISPER_DIR))).expanduser()
    whisper_bin = Path(
        os.getenv("LUMIKIT_WHISPER_BIN", str(whisper_dir / "build" / "bin" / "whisper-cli"))
    ).expanduser()
    whisper_model = Path(
        os.getenv("LUMIKIT_WHISPER_MODEL", str(whisper_dir / "models" / "ggml-base.en.bin"))
    ).expanduser()
    edge_python = Path(
        os.getenv("LUMIKIT_EDGE_TTS_PYTHON", str(DEFAULT_EDGE_PYTHON))
    ).expanduser()
    default_voice = os.getenv("LUMIKIT_TTS_VOICE", "en-US-AvaNeural").strip() or "en-US-AvaNeural"
    tts_format = os.getenv("LUMIKIT_TTS_FORMAT", "mp3").strip().lower() or "mp3"
    return SpeechConfig(
        whisper_bin=whisper_bin,
        whisper_model=whisper_model,
        edge_python=edge_python,
        default_voice=default_voice,
        tts_format=tts_format,
    )


class SpeechClient:
    def __init__(self, config: SpeechConfig | None = None):
        self.config = config or load_speech_config()

    @property
    def can_transcribe(self) -> bool:
        return self.config.whisper_bin.exists() and self.config.whisper_model.exists()

    @property
    def can_speak(self) -> bool:
        if not self.config.edge_python.exists():
            return False
        if self.config.edge_python == Path(sys.executable):
            return importlib.util.find_spec("edge_tts") is not None
        return True

    def get_voice_options(self) -> dict[str, str]:
        return VOICE_OPTIONS.copy()

    def resolve_voice(self, value: str | None) -> str:
        raw = (value or "").strip()
        if not raw:
            return self.config.default_voice
        voice = VOICE_OPTIONS.get(raw.lower(), raw)
        if not voice.endswith("Neural"):
            raise ValueError("Unknown voice. Use /voice list to see supported options.")
        return voice

    def transcribe(self, audio_bytes: bytes, filename: str = "telegram-audio.ogg") -> str:
        if not self.can_transcribe:
            raise RuntimeError("whisper.cpp is not ready yet")

        suffix = Path(filename).suffix or ".ogg"
        with tempfile.TemporaryDirectory(prefix="lumakit-stt-") as tmpdir:
            tmpdir_path = Path(tmpdir)
            input_path = tmpdir_path / f"source{suffix}"
            wav_path = tmpdir_path / "converted.wav"
            input_path.write_bytes(audio_bytes)

            ffmpeg_cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(input_path),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                str(wav_path),
            ]
            ffmpeg_result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if ffmpeg_result.returncode != 0:
                raise RuntimeError(ffmpeg_result.stderr.strip() or "ffmpeg conversion failed")

            cmd = [
                str(self.config.whisper_bin),
                "-m",
                str(self.config.whisper_model),
                "-f",
                str(wav_path),
                "-otxt",
                "-nt",
                "-np",
                "-of",
                str(tmpdir_path / "transcript"),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "whisper.cpp failed")

            text_path = tmpdir_path / "transcript.txt"
            if not text_path.exists():
                raise RuntimeError("whisper.cpp did not produce a transcript")
            text = text_path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                raise RuntimeError("Speech transcription returned empty text")
            return text

    def synthesize(self, text: str, voice: str | None = None) -> tuple[bytes, str, str]:
        if not self.can_speak:
            raise RuntimeError("edge-tts is not ready yet")

        voice_name = self.resolve_voice(voice)
        extension = self.config.tts_format
        content_type = "audio/mpeg" if extension == "mp3" else "application/octet-stream"

        with tempfile.TemporaryDirectory(prefix="lumakit-tts-") as tmpdir:
            out_path = Path(tmpdir) / f"reply.{extension}"
            cmd = [
                str(self.config.edge_python),
                "-m",
                "edge_tts",
                "--voice",
                voice_name,
                "--text",
                text,
                "--write-media",
                str(out_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "edge-tts failed")
            return out_path.read_bytes(), content_type, extension
