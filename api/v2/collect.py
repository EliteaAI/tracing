"""
UI trace collection endpoint.

Receives trace data from the browser and forwards to OpenTelemetry.
This allows the UI to report its own spans (button clicks, render times, etc.)

Note: This module requires api_tools which is only available in pylon_main/pylon_auth.
On pylon_indexer, this module will be skipped.
"""

from flask import request
from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    class PromptLibAPI(api_tools.APIModeHandler):
        """Collect traces from UI."""

        @auth.decorators.check_api({
            "permissions": ["models.monitoring.tracing.collect"],
            "recommended_roles": {
                c.DEFAULT_MODE: {"admin": True, "editor": True, "viewer": True},
            }
        })
        def post(self, project_id: int = None, **kwargs):
            """
            Collect UI trace spans.

            Accepts trace spans from the browser and creates corresponding
            OpenTelemetry spans that will be exported to Jaeger.
            """
            if not self.module.enabled:
                return {"error": "Tracing is disabled", "received": 0}, 503

            tracer = self.module.get_tracer()
            if not tracer:
                return {"error": "Tracer not available", "received": 0}, 503

            data = request.json
            if not data:
                return {"error": "No data provided", "received": 0}, 400

            traces = data.get('traces', [])
            spans_created = 0

            try:
                from opentelemetry.trace import Status, StatusCode
                from datetime import datetime

                from ..utils.user_context import extract_user_context

                for trace_data in traces:
                    trace_id = trace_data.get('trace_id')
                    trace_name = trace_data.get('name', 'ui_trace')
                    metadata = trace_data.get('metadata', {})

                    # Get user's actual project context, don't use URL project_id directly
                    user_context = extract_user_context(from_flask=True, from_baggage=True, from_headers=True)
                    user_project_id = user_context.get('project.id')
                    
                    # Only fall back to URL project_id if no user project context found
                    effective_project_id = user_project_id or project_id or metadata.get('project_id')

                    # Create parent span for the UI trace
                    with tracer.start_as_current_span(
                        f"ui:{trace_name}",
                        attributes={
                            'trace.source': 'ui',
                            'trace.id': trace_id,
                            'project.id': effective_project_id,
                            'user.id': auth.current_user().get('id'),
                            **{f'ui.{k}': str(v) for k, v in metadata.items() if v is not None}
                        }
                    ) as parent_span:
                        spans_created += 1

                        # Create child spans for each recorded span
                        for span_data in trace_data.get('spans', []):
                            span_name = span_data.get('name', 'unknown')
                            span_metadata = span_data.get('metadata', {})
                            duration_ms = span_data.get('duration_ms')

                            with tracer.start_as_current_span(
                                span_name,
                                attributes={
                                    'span.source': 'ui',
                                    'duration_ms': duration_ms,
                                    **{f'ui.{k}': str(v) for k, v in span_metadata.items() if v is not None}
                                }
                            ) as child_span:
                                spans_created += 1

                                # Set status based on metadata
                                if span_metadata.get('error'):
                                    child_span.set_status(Status(StatusCode.ERROR, span_metadata.get('error')))
                                else:
                                    child_span.set_status(Status(StatusCode.OK))

                log.debug(f"Collected {spans_created} UI spans from {len(traces)} traces")
                return {"received": len(traces), "spans_created": spans_created}, 200

            except Exception as e:
                log.error(f"Error collecting UI traces: {e}")
                return {"error": str(e), "received": 0}, 500


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '',
            '<int:project_id>',
        ])
        mode_handlers = {
            'prompt_lib': PromptLibAPI,
        }
else:
    # api_tools not available - define API as None so attribute exists
    API = None
