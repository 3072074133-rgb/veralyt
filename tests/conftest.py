import pytest

from app.config import settings
from app.model_settings import model_settings


@pytest.fixture(autouse=True)
def isolate_saved_model_settings(tmp_path, monkeypatch, request):
    """Keep ordinary unit tests off the developer's live model configuration."""
    if request.node.get_closest_marker("ollama") is not None:
        yield
        return
    monkeypatch.setattr(settings, "data_dir", tmp_path / "model-data")
    model_settings.reset_cache()
    yield
    model_settings.reset_cache()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-ollama",
        action="store_true",
        default=False,
        help="run regression tests against the configured local Ollama model",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-ollama"):
        return
    skip = pytest.mark.skip(reason="use --run-ollama to run local model regressions")
    for item in items:
        if "ollama" in item.keywords:
            item.add_marker(skip)
