"""
OTLP proxy endpoint for browser-based tracing.

This endpoint proxies OTLP trace data from the browser to Jaeger,
avoiding CORS issues when the browser can't directly reach Jaeger.

Note: This module requires api_tools which is only available in pylon_main/pylon_auth.
On pylon_indexer, this module will be skipped.
"""

import requests
from flask import request, Response
from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c
    _API_AVAILABLE = True
except ImportError:
    _API_AVAILABLE = False


if _API_AVAILABLE:
    class PromptLibAPI(api_tools.APIModeHandler):
        """OTLP proxy for browser traces."""

        @auth.decorators.check_api({
            "permissions": ["models.monitoring.tracing.collect"],
            "recommended_roles": {
                c.DEFAULT_MODE: {"admin": True, "editor": True, "viewer": False},
            }
        })
        def post(self, project_id: int = None, **kwargs):
            """
            Proxy OTLP trace data to Jaeger.

            Accepts trace data from browser OpenTelemetry SDK and forwards
            it to the configured Jaeger/OTLP endpoint.
            """
            if not self.module.enabled:
                return {"error": "Tracing is disabled"}, 503

            otlp_config = self.module.config.get('otlp', {})
            http_endpoint = otlp_config.get('http_endpoint', 'http://jaeger:4318')

            try:
                # Forward the request to Jaeger
                response = requests.post(
                    f"{http_endpoint}/v1/traces",
                    data=request.data,
                    headers={
                        'Content-Type': request.content_type or 'application/json',
                    },
                    timeout=5,
                )

                return Response(
                    response.content,
                    status=response.status_code,
                    content_type=response.headers.get('Content-Type', 'application/json'),
                )

            except requests.exceptions.ConnectionError:
                log.warning("Failed to connect to OTLP endpoint")
                return {"error": "OTLP endpoint unavailable"}, 503
            except requests.exceptions.Timeout:
                log.warning("Timeout connecting to OTLP endpoint")
                return {"error": "OTLP endpoint timeout"}, 504
            except Exception as e:
                log.error(f"Error proxying OTLP request: {e}")
                return {"error": "Internal error"}, 500


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
