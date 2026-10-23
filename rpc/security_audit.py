"""RPC exposing the security audit backpressure signal (#6945, C-1009)."""

from pylon.core.tools import web


class RPC:
    @web.rpc('security_audit_admission_allowed')
    def security_audit_admission_allowed(self) -> bool:
        """False only when failure_policy is fail_closed and the outbox backlog is over its limit."""
        from ..utils import security_audit

        return security_audit.security_audit_admission_allowed()
