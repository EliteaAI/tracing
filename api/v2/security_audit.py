"""Security audit event list (administration mode, #7120, C-1009).

URL: GET /api/v2/tracing/security_audit/administration

Read-only view of the ``security_audit_events`` table for platform operators. The rows are
the schema v1 fields, which are secret-free by construction.
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
        """Platform operators (super admin, admin) list security audit events."""

        @register_openapi(
            name="List Security Audit Events",
            description=(
                "Paginated security audit events (schema v1) with filters by project, agent binding, "
                "credential, user, event type, reason code, would_deny and time range. "
                "would_deny=true selects audit-only runs that enforce would have refused."
            ),
            tags=[OPENAPI_TAG],
            parameters=[
                {"name": "project_id", "in": "query", "required": False, "schema": {"type": "integer"}},
                {"name": "agent_binding_ref", "in": "query", "required": False, "schema": {"type": "string"}},
                {"name": "credential_ref", "in": "query", "required": False, "schema": {"type": "string"}},
                {"name": "user_ref", "in": "query", "required": False, "schema": {"type": "string"}},
                {"name": "event_type", "in": "query", "required": False, "schema": {"type": "string"}},
                {"name": "reason_code", "in": "query", "required": False, "schema": {"type": "string"}},
                {"name": "would_deny", "in": "query", "required": False, "schema": {"type": "boolean"}},
                {"name": "date_from", "in": "query", "required": False,
                 "schema": {"type": "string", "format": "date-time"}},
                {"name": "date_to", "in": "query", "required": False,
                 "schema": {"type": "string", "format": "date-time"}},
                {"name": "limit", "in": "query", "required": False,
                 "schema": {"type": "integer", "default": 20, "maximum": 100}},
                {"name": "offset", "in": "query", "required": False,
                 "schema": {"type": "integer", "default": 0}},
                {"name": "sort_by", "in": "query", "required": False,
                 "schema": {"type": "string", "default": "occurred_at"}},
                {"name": "sort_order", "in": "query", "required": False,
                 "schema": {"type": "string", "enum": ["asc", "desc"], "default": "desc"}},
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
                filters = security_audit.parse_filters(request.args)
                page = security_audit.parse_page(request.args)
            except security_audit.InvalidQuery as exc:
                return {"error": str(exc), "code": "INVALID_REQUEST"}, 400
            try:
                total, rows = security_audit.query_security_events(filters, page)
            except Exception as exc:  # pylint: disable=W0703
                log.error("Security audit query failed: %s", type(exc).__name__)
                return {"error": "Failed to query security audit events", "code": "AUDIT_UNAVAILABLE"}, 503
            return {"total": total, "rows": rows}, 200


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '',
        ])
        mode_handlers = {
            'administration': AdminAPI,
        }
else:
    API = None
