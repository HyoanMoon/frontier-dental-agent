"""YAML config loader with pydantic validation.

Reading config goes through ``load_config()`` which is the single source of
truth for the rest of the codebase. Tests can pass an explicit path; the CLI
defaults to ``config/config.yaml``.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class SiteConfig(BaseModel):
    base_url: str
    robots_txt_url: str
    user_agent: str


class CrawlerConfig(BaseModel):
    seeds: list[str]
    hits_per_page: int = 100
    max_pages_per_category: int = 20


class TokenBucketConfig(BaseModel):
    rate_per_sec: float
    burst: int


class RateLimitsConfig(BaseModel):
    algolia: TokenBucketConfig
    safco: TokenBucketConfig


class RetryConfig(BaseModel):
    max_attempts: int = 5
    initial_backoff_sec: float = 1
    max_backoff_sec: float = 30


class LLMConfig(BaseModel):
    enabled: bool = True
    model: str = "claude-haiku-4-5-20251001"
    max_calls_per_run: int = 500
    fallback_only: bool = True
    max_input_chars: int = 8000


class StorageConfig(BaseModel):
    sqlite_path: str
    exports: dict[str, str] = Field(default_factory=dict)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "json"
    file: str | None = None


class Config(BaseModel):
    site: SiteConfig
    crawler: CrawlerConfig
    rate_limits: RateLimitsConfig
    retry: RetryConfig
    llm: LLMConfig
    storage: StorageConfig
    logging: LoggingConfig


def load_config(path: str | Path = "config/config.yaml") -> Config:
    """Load and validate config. Path is relative to CWD by default."""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return Config.model_validate(raw)


def anthropic_api_key() -> str | None:
    """Read from env, returning None when absent so callers can degrade."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key or key.startswith("sk-ant-replace"):
        return None
    return key
