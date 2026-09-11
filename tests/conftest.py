import pytest


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
