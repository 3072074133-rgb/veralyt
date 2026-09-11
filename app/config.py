from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ANALYSE_AGENT_", env_file=PROJECT_ROOT / ".env", extra="ignore"
    )

    app_name: str = "数据分析智能体"
    api_prefix: str = "/api/v1"
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = PROJECT_ROOT / "data"
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3.5:4b"
    ollama_embedding_model: str = "qwen3-embedding:0.6b"
    knowledge_embedding_dimensions: int = 1024
    knowledge_chunk_size: int = 900
    knowledge_chunk_overlap: int = 150
    knowledge_retrieval_limit: int = 8
    knowledge_retrieval_min_score: float = 0.25
    knowledge_context_max_chars: int = 2800
    max_file_size: int = 100 * 1024 * 1024
    max_files: int = 5
    max_sheets: int = 30
    max_xlsx_uncompressed_size: int = 1024 * 1024 * 1024
    max_xlsx_compression_ratio: int = 100
    max_rows_per_sheet: int = 500_000
    max_columns_per_sheet: int = 2_000
    max_nonempty_cells: int = 2_000_000
    max_csv_field_chars: int = 100_000
    header_search_rows: int = 100
    # A single blank row/column is a common separator in exported workbooks.
    region_blank_row_gap: int = 1
    region_blank_column_gap: int = 1
    max_query_rows: int = 5000
    max_tool_calls: int = 10
    followup_enabled: bool = True
    model_intent_enabled: bool = True
    max_revision_rounds: int = 3
    query_timeout_seconds: int = 30
    model_context_tokens: int = 16384
    model_max_context_tokens: int = 32768
    model_classifier_output_tokens: int = 512
    model_default_output_tokens: int = 1536
    model_draft_output_tokens: int = 3072
    model_summary_output_tokens: int = 1024
    context_output_reserve_tokens: int = 1200
    context_safety_ratio: float = 0.1
    context_base_overhead_tokens: int = 1450
    context_recent_messages: int = 4
    summary_batch_tokens: int = 1800
    dataset_candidate_limit: int = 3
    model_input_safety_tokens: int = 768
    report_max_metrics: int = 8
    report_max_findings: int = 5
    report_max_charts: int = 4
    report_max_suggested_questions: int = 5
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5

    @property
    def metadata_db(self) -> Path:
        return self.data_dir / "app.sqlite"

    @property
    def checkpoint_db(self) -> Path:
        return self.data_dir / "checkpoints.sqlite"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


settings = Settings()
