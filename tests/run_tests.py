#!/usr/bin/env python3
"""
Test runner for the tracing plugin.
Installs Pylon stubs before running pytest so modules can be imported.

Usage:
    python3 tests/run_tests.py -v              # All tests
    python3 tests/run_tests.py -m unit -v      # Unit tests only
    python3 tests/run_tests.py unit/test_payload_capture.py -v  # Specific file
"""
import os
import sys
import types

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(TESTS_DIR)

os.chdir(TESTS_DIR)


class _Log:
    @staticmethod
    def info(*a, **k): pass
    @staticmethod
    def warning(*a, **k): pass
    @staticmethod
    def warn(*a, **k): pass
    @staticmethod
    def error(*a, **k): pass
    @staticmethod
    def debug(*a, **k): pass
    @staticmethod
    def trace(*a, **k): pass
    @staticmethod
    def exception(*a, **k): pass
    @staticmethod
    def critical(*a, **k): pass


def install_pylon_stubs():
    """Install minimal Pylon stubs so plugin modules can be imported."""
    pylon = types.ModuleType('pylon')
    pylon_core = types.ModuleType('pylon.core')
    pylon_core_tools = types.ModuleType('pylon.core.tools')

    pylon_core_tools.log = _Log()
    pylon_core_tools.web = types.SimpleNamespace(rpc=lambda *a, **k: lambda f: f)
    pylon_core_tools.module = types.SimpleNamespace()

    pylon.core = pylon_core
    pylon_core.tools = pylon_core_tools

    sys.modules.setdefault('pylon', pylon)
    sys.modules.setdefault('pylon.core', pylon_core)
    sys.modules.setdefault('pylon.core.tools', pylon_core_tools)

    # Stub tools module
    tools = types.ModuleType('tools')
    tools.db = types.SimpleNamespace(
        Base=type('Base', (), {}),
        session=types.SimpleNamespace(query=lambda *a: None),
        get_session=lambda pid: types.SimpleNamespace(__enter__=lambda s: s, __exit__=lambda *a: None),
    )
    tools.config = types.SimpleNamespace(POSTGRES_SCHEMA='centry')
    tools.this = types.SimpleNamespace(
        for_module=lambda name: types.SimpleNamespace(
            module=types.SimpleNamespace(enabled=False, get_tracer=lambda: None)
        )
    )
    sys.modules.setdefault('tools', tools)

    # Stub flask
    flask = types.ModuleType('flask')
    flask.request = types.SimpleNamespace(headers={})
    flask.g = types.SimpleNamespace()
    flask.has_request_context = lambda: False
    sys.modules.setdefault('flask', flask)


if __name__ == '__main__':
    install_pylon_stubs()

    import pytest
    sys.exit(pytest.main(['.'] + sys.argv[1:]))
