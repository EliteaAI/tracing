"""Pytest configuration - auto-mark tests based on directory."""
import pathlib
import pytest

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def plugin_root() -> pathlib.Path:
    """Absolute path to the tracing plugin root."""
    return PLUGIN_ROOT


@pytest.fixture(scope="session")
def models_path(plugin_root: pathlib.Path) -> pathlib.Path:
    """Path to the models/ directory."""
    return plugin_root / "models"


@pytest.fixture(scope="session")
def utils_path(plugin_root: pathlib.Path) -> pathlib.Path:
    """Path to the utils/ directory."""
    return plugin_root / "utils"


def pytest_collection_modifyitems(items):
    for item in items:
        if '/unit/' in str(item.fspath):
            item.add_marker(pytest.mark.unit)
        elif '/integration/' in str(item.fspath):
            item.add_marker(pytest.mark.integration)
