"""
User Context Extraction and Propagation for Tracing

This module provides utilities to extract user identity information
for inclusion in OpenTelemetry spans, enabling per-user observability.

User identity is extracted from (in priority order):
1. OpenTelemetry Baggage (propagated across services)
2. Flask g.auth object (HTTP requests)
3. Request headers (X-Auth-Type, X-Auth-ID, X-Auth-Reference)
4. Function arguments (user_id, project_id kwargs)

Baggage propagation ensures user context flows across all service boundaries
(pylon_main -> pylon_auth, pylon_main -> pylon_indexer, etc.)
"""

from typing import Dict, Any, Optional
from pylon.core.tools import log


# Standard span attribute names for user context
USER_ATTR_PREFIX = "user"
USER_ID_ATTR = f"{USER_ATTR_PREFIX}.id"
USER_TYPE_ATTR = f"{USER_ATTR_PREFIX}.type"
USER_EMAIL_ATTR = f"{USER_ATTR_PREFIX}.email"
USER_NAME_ATTR = f"{USER_ATTR_PREFIX}.name"
USER_REFERENCE_ATTR = f"{USER_ATTR_PREFIX}.reference"
PROJECT_ID_ATTR = "project.id"

# Baggage keys (used for cross-service propagation)
BAGGAGE_USER_ID = "user_id"
BAGGAGE_USER_TYPE = "user_type"
BAGGAGE_USER_EMAIL = "user_email"
BAGGAGE_PROJECT_ID = "project_id"


def set_user_baggage(
    user_id: Any = None,
    user_type: str = None,
    project_id: Any = None,
    user_email: str = None,
):
    """
    Set user context in OpenTelemetry baggage for cross-service propagation.

    This should be called early in request processing (e.g., after auth)
    to ensure user context is propagated to all downstream services.

    Args:
        user_id: User ID to propagate
        user_type: User type (user, token, etc.)
        project_id: Project ID to propagate
        user_email: User email to propagate (avoids RPC in downstream services)
    """
    try:
        from opentelemetry import baggage, context

        ctx = context.get_current()

        if user_id is not None:
            ctx = baggage.set_baggage(BAGGAGE_USER_ID, str(user_id), context=ctx)

        if user_type is not None:
            ctx = baggage.set_baggage(BAGGAGE_USER_TYPE, str(user_type), context=ctx)

        if project_id is not None:
            ctx = baggage.set_baggage(BAGGAGE_PROJECT_ID, str(project_id), context=ctx)

        if user_email is not None:
            ctx = baggage.set_baggage(BAGGAGE_USER_EMAIL, str(user_email), context=ctx)

        # Attach the updated context
        context.attach(ctx)

    except ImportError:
        pass
    except Exception as e:
        log.debug(f"Failed to set user baggage: {e}")


def extract_user_from_baggage() -> Dict[str, Any]:
    """
    Extract user context from OpenTelemetry baggage.

    This retrieves user context that was propagated from upstream services.

    Returns:
        Dictionary of span attributes for user identity.
    """
    attributes = {}

    try:
        from opentelemetry import baggage

        user_id = baggage.get_baggage(BAGGAGE_USER_ID)
        user_type = baggage.get_baggage(BAGGAGE_USER_TYPE)
        project_id = baggage.get_baggage(BAGGAGE_PROJECT_ID)
        user_email = baggage.get_baggage(BAGGAGE_USER_EMAIL)

        if user_id:
            try:
                attributes[USER_ID_ATTR] = int(user_id)
            except (ValueError, TypeError):
                attributes[USER_ID_ATTR] = str(user_id)

        if user_type and user_type != 'public':
            attributes[USER_TYPE_ATTR] = user_type

        if project_id:
            try:
                attributes[PROJECT_ID_ATTR] = int(project_id)
            except (ValueError, TypeError):
                attributes[PROJECT_ID_ATTR] = str(project_id)

        if user_email:
            attributes[USER_EMAIL_ATTR] = str(user_email)

    except ImportError:
        pass
    except Exception as e:
        log.debug(f"Failed to extract user from baggage: {e}")

    return attributes


def extract_user_from_flask() -> Dict[str, Any]:
    """
    Extract user context from Flask's g.auth object.

    Returns:
        Dictionary of span attributes for user identity.
    """
    attributes = {}

    try:
        from flask import g, has_request_context

        if not has_request_context():
            return attributes

        auth = getattr(g, 'auth', None)
        if auth is None:
            return attributes

        # g.auth has: type, id, reference
        auth_type = getattr(auth, 'type', None)
        auth_id = getattr(auth, 'id', None)
        auth_ref = getattr(auth, 'reference', None)

        if auth_type and auth_type != 'public':
            attributes[USER_TYPE_ATTR] = auth_type

        if auth_id and auth_id != '-':
            # Convert to int if possible
            try:
                attributes[USER_ID_ATTR] = int(auth_id)
            except (ValueError, TypeError):
                attributes[USER_ID_ATTR] = str(auth_id)

        if auth_ref and auth_ref != '-':
            # Truncate reference for privacy
            attributes[USER_REFERENCE_ATTR] = str(auth_ref)[:32]

    except ImportError:
        pass
    except Exception as e:
        log.debug(f"Failed to extract user from Flask g.auth: {e}")

    return attributes


def extract_user_from_headers() -> Dict[str, Any]:
    """
    Extract user context from request headers (X-Auth-* headers).

    This is useful when auth context is passed via Traefik or internal requests.

    Returns:
        Dictionary of span attributes for user identity.
    """
    attributes = {}

    try:
        from flask import request, has_request_context

        if not has_request_context():
            return attributes

        # Check X-Auth-* headers (set by Traefik or internal services)
        auth_type = request.headers.get('X-Auth-Type')
        auth_id = request.headers.get('X-Auth-ID')
        auth_ref = request.headers.get('X-Auth-Reference')

        if auth_type and auth_type != 'public':
            attributes[USER_TYPE_ATTR] = auth_type

        if auth_id and auth_id != '-':
            try:
                attributes[USER_ID_ATTR] = int(auth_id)
            except (ValueError, TypeError):
                attributes[USER_ID_ATTR] = str(auth_id)

        if auth_ref and auth_ref != '-':
            attributes[USER_REFERENCE_ATTR] = str(auth_ref)[:32]

    except ImportError:
        pass
    except Exception as e:
        log.debug(f"Failed to extract user from headers: {e}")

    return attributes


def extract_user_from_kwargs(kwargs: dict) -> Dict[str, Any]:
    """
    Extract user context from function arguments.

    Many RPC calls pass user_id and project_id as keyword arguments.

    Args:
        kwargs: Keyword arguments from the function call

    Returns:
        Dictionary of span attributes for user identity.
    """
    attributes = {}

    if not kwargs:
        return attributes

    # Look for user_id in various forms
    user_id = kwargs.get('user_id') or kwargs.get('author_id') or kwargs.get('created_by')
    if user_id:
        try:
            attributes[USER_ID_ATTR] = int(user_id)
        except (ValueError, TypeError):
            attributes[USER_ID_ATTR] = str(user_id)

    # Look for project_id
    project_id = kwargs.get('project_id') or kwargs.get('proj_id')
    if project_id:
        try:
            attributes[PROJECT_ID_ATTR] = int(project_id)
        except (ValueError, TypeError):
            attributes[PROJECT_ID_ATTR] = str(project_id)

    return attributes


def extract_user_context(
    from_flask: bool = True,
    from_headers: bool = True,
    from_baggage: bool = True,
    kwargs: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    Extract user context from all available sources.

    Priority order (later sources override earlier):
    1. Function kwargs (lowest priority)
    2. OpenTelemetry baggage (propagated from upstream)
    3. Request headers (X-Auth-*)
    4. Flask g.auth (highest priority)

    Args:
        from_flask: Whether to check Flask g.auth
        from_headers: Whether to check request headers
        from_baggage: Whether to check OpenTelemetry baggage
        kwargs: Function kwargs to check for user_id/project_id

    Returns:
        Dictionary of span attributes for user identity.
    """
    attributes = {}

    # Start with kwargs (lowest priority)
    if kwargs:
        attributes.update(extract_user_from_kwargs(kwargs))

    # Then baggage (propagated from upstream services)
    if from_baggage:
        attributes.update(extract_user_from_baggage())

    # Then headers
    if from_headers:
        attributes.update(extract_user_from_headers())

    # Then g.auth (highest priority)
    if from_flask:
        attributes.update(extract_user_from_flask())

    return attributes


def get_current_user_info() -> Optional[Dict[str, Any]]:
    """
    Get detailed user info from auth.current_user() RPC, with caching.

    Uses a service-level in-memory cache keyed by user_id to avoid
    repeated RPC calls. The cache lives for the service lifetime.

    Returns:
        User info dict with id, email, name, or None if not available.
    """
    try:
        from tools import auth

        user = auth.current_user()
        if user:
            user_id = user.get('id')
            email = user.get('email')
            name = user.get('name')
            # Populate the shared email cache while we have the data
            if user_id is not None and email:
                _user_email_cache[int(user_id)] = email
            return {
                USER_ID_ATTR: user_id,
                USER_EMAIL_ATTR: email,
                USER_NAME_ATTR: name,
            }
    except Exception as e:
        log.debug(f"Failed to get current user info: {e}")

    return None


# ---------------------------------------------------------------------------
# Service-level user email cache
# ---------------------------------------------------------------------------
# Keyed by int(user_id) → str(email) | None.
# ~20 K entries ≈ 2 MB — negligible.  Lives for the service lifetime.
_user_email_cache: Dict[int, Optional[str]] = {}


def resolve_user_email(user_id) -> Optional[str]:
    """Resolve user email from user_id using a service-level cache.

    Intended to be called ONLY from background threads (audit processor
    worker, audit writer).  Never call this on request hot paths.

    On cache miss the RPC is attempted once per user_id; the result
    (including None on failure) is cached permanently.
    """
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None

    if uid in _user_email_cache:
        return _user_email_cache[uid]

    # First miss — do the RPC (OK to block here, we're on a background thread)
    try:
        from tools import auth
        user = auth.get_user(user_id=uid)
        email = user.get("email") if user else None
    except Exception:
        email = None

    _user_email_cache[uid] = email
    return email


def enrich_span_with_user(span, kwargs: Optional[dict] = None, detailed: bool = False):
    """
    Add user context attributes to an existing span.

    Args:
        span: OpenTelemetry span to enrich
        kwargs: Optional kwargs to extract user_id/project_id from
        detailed: If True, makes RPC call for full user details (slower)
    """
    if span is None:
        return

    try:
        # Get basic user context
        attributes = extract_user_context(kwargs=kwargs)

        # Optionally get detailed info (includes email, name)
        if detailed and USER_ID_ATTR in attributes:
            user_info = get_current_user_info()
            if user_info:
                attributes.update(user_info)

        # Set attributes on span
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)

    except Exception as e:
        log.debug(f"Failed to enrich span with user context: {e}")
