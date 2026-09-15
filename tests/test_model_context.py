from app.model_context import cloud_context_policy


def test_current_cloud_model_families_use_verified_context_capacities() -> None:
    assert cloud_context_policy("gpt-5.6-terra").max_context_tokens == 1_050_000
    assert cloud_context_policy("deepseek-flash").max_context_tokens == 1_000_000
    assert cloud_context_policy("DeepSeek-V4-Pro-0813").max_context_tokens == 1_000_000
    assert cloud_context_policy("qwen3.8-max-2026-08-01").max_context_tokens == 1_000_000
    assert cloud_context_policy("kimi-k3").max_context_tokens == 1_000_000
    assert cloud_context_policy("kimi-k2.7-code-highspeed").max_context_tokens == 262_144
    assert cloud_context_policy("kimi-k2.6").max_context_tokens == 262_144


def test_cloud_model_match_is_case_insensitive_and_compacts_at_eighty_percent() -> None:
    policy = cloud_context_policy("Vendor/GPT-5.6-SOL")
    assert policy.matched_pattern == "gpt-5.6"
    assert policy.compression_trigger_tokens == 840_000
    assert policy.source == "model_table"


def test_unknown_cloud_model_defaults_to_200k() -> None:
    policy = cloud_context_policy("private-finance-model-v9")
    assert policy.max_context_tokens == 200_000
    assert policy.compression_trigger_tokens == 160_000
    assert policy.matched_pattern is None
    assert policy.source == "default_200k"
