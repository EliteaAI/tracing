# Tracing Plugin Tests

Isolated test framework for the tracing plugin without full Pylon runtime dependencies.

## Quick Start

```bash
# From tracing plugin directory
python tests/run_tests.py -v              # All tests
python tests/run_tests.py -m unit -v      # Unit tests only
python tests/run_tests.py -m integration  # Integration tests
```

## Structure

```
tests/
├── run_tests.py       # Entry point (installs stubs before pytest)
├── pytest.ini         # Pytest configuration
├── conftest.py        # Auto-marks tests by directory
├── requirements-dev.txt
├── fixtures/
│   └── helpers.py     # Module loading utilities
└── unit/
    ├── test_payload_capture.py   # PayloadCapture serialization tests
    └── test_trace_context.py     # Trace ID generation and parsing
```

## Test Categories

### Unit Tests (`tests/unit/`)
Pure function tests with no external dependencies:
- `test_payload_capture.py` - Sensitive data masking, serialization, truncation
- `test_trace_context.py` - Trace ID generation, W3C traceparent parsing

### Integration Tests (`tests/integration/`)
Tests requiring module imports with mocked Pylon dependencies.

## Writing Tests

### Unit Tests (Preferred)
Copy pure function logic into test file to avoid import chains:

```python
def my_pure_function(data: str) -> str:
    """Copy of the function for isolated testing."""
    return data.upper()

def test_my_function():
    assert my_pure_function("hello") == "HELLO"
```

### Integration Tests
Use `run_tests.py` stubs for Pylon dependencies:

```python
from fixtures.helpers import load_module_with_stubs

def test_module_function():
    module = load_module_with_stubs(
        Path(__file__).parents[2] / "utils" / "my_module.py",
        "my_module"
    )
    assert module.some_function() == expected
```

## CI Pipeline

GitHub Actions runs unit tests on every push/PR:

```yaml
- run: python tests/run_tests.py -m unit -v
```
