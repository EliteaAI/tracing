"""#6945: a pylon without the `db` tool (pylon_auth, pylon_indexer) must still init tracing."""
import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_security_audit_init_checks_the_db_tool_before_importing_the_outbox():
    source = (ROOT / 'module.py').read_text()
    body = source.split('def _init_security_audit(self):', 1)[1].split('\n    def ', 1)[0]
    assert body.index('from tools import db') < body.index('from .utils import security_audit')
    assert 'except ImportError' in body and 'return' in body.split('except ImportError', 1)[1].split('from .utils', 1)[0]
    ast.parse(source)  # still valid python
