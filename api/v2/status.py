"""
Tracing status API endpoint.

Note: This module requires api_tools which is only available in pylon_main/pylon_auth.
On pylon_indexer, this module will be skipped.
"""

from pylon.core.tools import log

try:
    from tools import api_tools, auth, config as c
    _API_AVAILABLE = True
except ImportError:
    # api_tools not available (e.g., on pylon_indexer)
    _API_AVAILABLE = False


# Only define API classes if api_tools is available
if _API_AVAILABLE:
    class AdminAPI(api_tools.APIModeHandler):
        """Admin API for tracing status."""

        @auth.decorators.check_api({
            "permissions": ["models.admin.tracing.view"],
            "recommended_roles": {
                c.ADMINISTRATION_MODE: {"admin": True, "editor": True, "viewer": True},
            }
        })
        def get(self, **kwargs):
            """
            Get tracing status and configuration.

            Returns:
                dict: Tracing status including enabled state and configuration
            """
            return {
                "enabled": self.module.enabled,
                "config": self.module.get_config(),
            }, 200


    class PromptLibAPI(api_tools.APIModeHandler):
        """Project-level API for tracing status."""

        @auth.decorators.check_api({
            "permissions": ["models.monitoring.tracing.view"],
            "recommended_roles": {
                c.DEFAULT_MODE: {"admin": True, "editor": True, "viewer": True},
            }
        })
        def get(self, project_id: int, **kwargs):
            """
            Get tracing status for a project.

            Returns:
                dict: Tracing status
            """
            return {
                "enabled": self.module.enabled,
                "project_id": project_id,
            }, 200


    class API(api_tools.APIBase):
        url_params = api_tools.with_modes([
            '',
            '<int:project_id>',
        ])
        mode_handlers = {
            'administration': AdminAPI,
            'prompt_lib': PromptLibAPI,
        }
else:
    # api_tools not available - define API as None so attribute exists
    API = None
