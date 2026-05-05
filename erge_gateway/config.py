from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path("/Users/shawnwang/Documents/Playground")
RUNTIME_ROOT = PROJECT_ROOT / "runtime" / "erge-cache"
CACHE_ROOT = RUNTIME_ROOT / "cache"
RUNS_ROOT = RUNTIME_ROOT / "runs"
TMP_ROOT = RUNTIME_ROOT / "tmp"


@dataclass(slots=True)
class Settings:
    host: str = os.getenv("ERGE_HOST", "127.0.0.1")
    port: int = int(os.getenv("ERGE_PORT", "4010"))

    logic_primary_base_url: str = os.getenv("ERGE_LOGIC_BASE_URL", "http://192.168.10.2:11434")
    logic_primary_model: str = os.getenv("ERGE_LOGIC_MODEL", "qwen3.5:9b-q8_0")

    logic_local_base_url: str = os.getenv("ERGE_LOCAL_LOGIC_BASE_URL", "http://127.0.0.1:11434")
    logic_local_model: str = os.getenv("ERGE_LOCAL_LOGIC_MODEL", "qwen3.5:9b")

    health_pc_base_url: str = os.getenv("ERGE_HEALTH_PC_BASE_URL", "http://192.168.10.2:11434")
    health_pc_model: str = os.getenv("ERGE_HEALTH_PC_MODEL", "qwen3.5:9b-q8_0")
    game_mode_url: str = os.getenv("ERGE_GAME_MODE_URL", "http://192.168.10.2:4011/game-mode")

    vision_base_url: str = os.getenv("ERGE_VISION_BASE_URL", "http://127.0.0.1:11434")
    vision_model: str = os.getenv("ERGE_VISION_MODEL", "qwen3-vl:4b")
    ocr_helper_path: str = os.getenv("ERGE_OCR_HELPER_PATH", str(PROJECT_ROOT / "tools" / "wechat_ocr.swift"))

    connect_timeout_seconds: float = float(os.getenv("ERGE_CONNECT_TIMEOUT_SECONDS", "2"))
    game_mode_timeout_seconds: float = float(os.getenv("ERGE_GAME_MODE_TIMEOUT_SECONDS", "1"))
    probe_timeout_seconds: float = float(os.getenv("ERGE_PROBE_TIMEOUT_SECONDS", "8"))
    busy_threshold_seconds: float = float(os.getenv("ERGE_BUSY_THRESHOLD_SECONDS", "5"))
    request_timeout_seconds: float = float(os.getenv("ERGE_REQUEST_TIMEOUT_SECONDS", "120"))

    pdf_text_threshold_chars: int = int(os.getenv("ERGE_PDF_TEXT_THRESHOLD_CHARS", "120"))
    pdf_avg_chars_per_page_threshold: int = int(os.getenv("ERGE_PDF_AVG_CHARS_PER_PAGE_THRESHOLD", "60"))

    @property
    def cache_root(self) -> Path:
        return CACHE_ROOT

    @property
    def runs_root(self) -> Path:
        return RUNS_ROOT

    @property
    def tmp_root(self) -> Path:
        return TMP_ROOT

    def ensure_dirs(self) -> None:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.tmp_root.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
