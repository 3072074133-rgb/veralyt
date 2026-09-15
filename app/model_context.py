"""Cloud-model context capacities used for proactive conversation compaction."""
from __future__ import annotations

from dataclasses import dataclass


DEFAULT_CLOUD_CONTEXT_TOKENS = 200_000
CLOUD_COMPACTION_RATIO = 0.8

# Keep this table limited to current model families verified in provider docs.
# More-specific patterns must appear before broader family patterns.
MODEL_CONTEXT_TABLE: tuple[tuple[str, int], ...] = (
    ("gpt-5.6", 1_050_000),
    ("deepseek-flash", 1_000_000),
    # DeepSeek 暂时仍接受旧 V4 模型名并路由到 V4.1 Flash。
    ("deepseek-v4", 1_000_000),
    ("qwen3.8", 1_000_000),
    ("qwen3.7", 1_000_000),
    ("kimi-k3", 1_000_000),
    ("kimi-k2.7-code", 262_144),
    ("kimi-k2.6", 262_144),
)


@dataclass(frozen=True)
class CloudContextPolicy:
    model: str
    max_context_tokens: int
    compression_trigger_tokens: int
    matched_pattern: str | None

    @property
    def source(self) -> str:
        return "model_table" if self.matched_pattern else "default_200k"


def cloud_context_policy(model_name: str) -> CloudContextPolicy:
    normalized = model_name.strip().lower()
    matched_pattern: str | None = None
    limit = DEFAULT_CLOUD_CONTEXT_TOKENS
    for pattern, tokens in MODEL_CONTEXT_TABLE:
        if pattern in normalized:
            matched_pattern = pattern
            limit = tokens
            break
    return CloudContextPolicy(
        model=model_name,
        max_context_tokens=limit,
        compression_trigger_tokens=int(limit * CLOUD_COMPACTION_RATIO),
        matched_pattern=matched_pattern,
    )
