from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ACWConfig:
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str = "gpt-5"
    llm_model_light: str | None = None
    llm_provider: str = "openai"
    auto_process: bool = False
    max_attempts: int = 3
    needs_review_stale_days: int = 14
    summary_max_words: int = 300
    batch_max_chunks: int = 8


_ENV_TO_FIELD = {
    "ACW_LLM_API_KEY": "llm_api_key",
    "ACW_LLM_BASE_URL": "llm_base_url",
    "ACW_LLM_MODEL": "llm_model",
    "ACW_LLM_MODEL_LIGHT": "llm_model_light",
    "ACW_LLM_PROVIDER": "llm_provider",
    "ACW_AUTO_PROCESS": "auto_process",
    "ACW_MAX_ATTEMPTS": "max_attempts",
    "ACW_NEEDS_REVIEW_STALE_DAYS": "needs_review_stale_days",
    "ACW_SUMMARY_MAX_WORDS": "summary_max_words",
    "ACW_BATCH_MAX_CHUNKS": "batch_max_chunks",
}
_BOOL_FIELDS = {"auto_process"}
_INT_FIELDS = {"max_attempts", "needs_review_stale_days", "summary_max_words", "batch_max_chunks"}


def load_config(workspace: str | Path | None = None, environ: Mapping[str, str] | None = None) -> ACWConfig:
    values: dict[str, Any] = {}
    env = os.environ if environ is None else environ

    for env_name, field_name in _ENV_TO_FIELD.items():
        if env_name in env:
            values[field_name] = _coerce_value(field_name, env[env_name])

    values.update(_load_toml_overrides(workspace))
    return ACWConfig(**values)


def _load_toml_overrides(workspace: str | Path | None) -> dict[str, Any]:
    if workspace is None:
        return {}

    config_path = Path(workspace) / ".llmwiki" / "config.toml"
    if not config_path.exists():
        return {}

    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    acw_section = data.get("acw", {})
    if not isinstance(acw_section, dict):
        return {}

    return {
        field_name: _coerce_value(field_name, value)
        for field_name, value in acw_section.items()
        if field_name in set(_ENV_TO_FIELD.values())
    }


def _coerce_value(field_name: str, value: Any) -> Any:
    if field_name in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    if field_name in _INT_FIELDS:
        return int(value)
    return value
