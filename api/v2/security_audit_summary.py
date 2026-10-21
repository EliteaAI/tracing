"""Security audit event counts (administration mode, #7120, C-1009).

URL: GET /api/v2/tracing/security_audit_summary/administration

One GROUP BY over the last ``since_hours`` hours. The admin UI shows it before a governance
mode switch ("audit-only recorded N runs that enforce would have refused").
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c, register_openapi
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    from flask import request

    OPENAPI_TAG = "tracing/security_audit"

    class AdminAPI(api_tools.APIModeHandler):
        """Platform operators (super admin, admin) read the grouped counts."""

        @register_openapi(
            name="Summarize Security Audit Events",
            description=(
                "Event counts grouped by decision, would_deny and reason_code over the last "
                "since_hours hours (1..168, default 24), most frequent first."
            ),
            tags=[OPENAPI_TAG],
            parameters=[
                {"name": "since_hours", "in": "query", "required": False,
                 "schema": {"type": "integer", "default": 24, "minimum": 1, "maximum": 168}},
                {"name": "would_deny", "in": "query", "required": False, "schema": {"type": "boolean"}},
            ],
            mcp_tool=False,
            available_to_users=False,
        )
        @auth.decorators.check_api({
            "permissions": ["models.admin.audit_trail.view"],
            "recommended_roles": {
                c.ADMINISTRATION_MODE: {"admin": True, "editor": False, "viewer": False},
            }
        })
        def get(self, **kwargs):
            from ...utils import security_audit  # pylint: disable=C0415

            try:
                since_hours = security_audit.parse_since_hours(request.args)
                would_deny = security_audit.parse_would_deny(request.args)
            except security_audit.InvalidQuery as exc:
                return {"error": str(exc), "code": "INVALID_REQUEST"}, 400
            try:
                return security_audit.summarize_security_events(since_hours, would_deny), 200
            except Exception as exc:  # pylint: disable=W0703
                log.error("Security audit summary failed: %s", type(exc).__name__)
                return {"error": "Failed to summarize security audit events", "code": "AUDIT_UNAVAILABLE"}, 503


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '',
        ])
        mode_handlers = {
            'administration': AdminAPI,
        }
else:
    API = None
