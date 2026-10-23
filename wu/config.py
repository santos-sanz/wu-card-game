from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
DATA_DIR = ROOT / "data"
RESULTS_DIR = ROOT / "results"

load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


NAN_API_KEY = _env("NAN_API_KEY")
NAN_BASE_URL = _env("NAN_BASE_URL", "https://api.nan.builders/v1").rstrip("/")
OPENROUTER_API_KEY = _env("OPENROUTER_API_KEY")
OPENROUTER_BASE_URL = _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"

DEFAULT_TARGET = 163
DEFAULT_HAND = [2, 8, 9, 3, 12, 12]
DEFAULT_TEXT_MODEL = "deepseek-v4-flash"
DEFAULT_DECISIONS_MODEL = "baseten/mammoth-1:free"
HEURISTIC_MODEL = "harness/heuristic"

# Models on the Nam gateway that are not chat-completion models.
NON_CHAT_MARKERS = (
    "whisper",
    "rerank",
    "embedding",
    "embed",
    "tts",
    "image",
    "flux",
    "kokoro",
)


def mask_key(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:5]}…{value[-4:]}"
