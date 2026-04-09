"""
Payload Capture Utility for Tracing

Provides safe serialization of request payloads for span attributes with:
- Size limits (truncation)
- Sensitive data masking (passwords, tokens, API keys)
- Type handling (non-serializable objects)
- Configurable options

Usage:
    from .payload_capture import PayloadCapture

    capture = PayloadCapture(max_size=4096, mask_keys=['password', 'token'])
    safe_payload = capture.serialize(data)
"""

import json
import re
from typing import Any, Dict, List, Optional, Set, Union


# Default sensitive keys to mask (case-insensitive matching)
DEFAULT_SENSITIVE_KEYS = {
    'password', 'passwd', 'pwd', 'secret', 'token', 'api_key', 'apikey',
    'api-key', 'auth', 'authorization', 'bearer', 'credential', 'credentials',
    'private_key', 'privatekey', 'access_token', 'refresh_token', 'session',
    'cookie', 'x-api-key', 'x-auth-token', 'jwt', 'key', 'cert', 'certificate',
}

# Default max payload size in characters
DEFAULT_MAX_SIZE = 4096

# Mask replacement
MASK_VALUE = "***MASKED***"
TRUNCATED_SUFFIX = "...[TRUNCATED]"


class PayloadCapture:
    """
    Safe payload serializer for tracing span attributes.

    Features:
    - Masks sensitive keys (passwords, tokens, etc.)
    - Truncates large payloads
    - Handles non-serializable types
    - Configurable depth limit for nested structures
    """

    def __init__(
        self,
        max_size: int = DEFAULT_MAX_SIZE,
        max_depth: int = 5,
        mask_keys: Optional[Set[str]] = None,
        additional_mask_keys: Optional[Set[str]] = None,
        mask_patterns: Optional[List[str]] = None,
        include_types: bool = True,
    ):
        """
        Initialize the payload capture utility.

        Args:
            max_size: Maximum size of serialized output in characters
            max_depth: Maximum depth for nested structures
            mask_keys: Set of keys to mask (replaces default if provided)
            additional_mask_keys: Additional keys to mask (added to defaults)
            mask_patterns: Regex patterns for values to mask (e.g., API key patterns)
            include_types: Include type info for non-serializable objects
        """
        self.max_size = max_size
        self.max_depth = max_depth
        self.include_types = include_types

        # Build mask keys set (case-insensitive)
        if mask_keys is not None:
            self.mask_keys = {k.lower() for k in mask_keys}
        else:
            self.mask_keys = DEFAULT_SENSITIVE_KEYS.copy()

        if additional_mask_keys:
            self.mask_keys.update(k.lower() for k in additional_mask_keys)

        # Compile mask patterns
        self.mask_patterns = []
        if mask_patterns:
            for pattern in mask_patterns:
                try:
                    self.mask_patterns.append(re.compile(pattern, re.IGNORECASE))
                except re.error:
                    pass

        # Add default patterns for common secrets
        self._add_default_patterns()

    def _add_default_patterns(self):
        """Add default regex patterns for common secret formats."""
        default_patterns = [
            r'sk-[a-zA-Z0-9]{20,}',      # OpenAI API keys
            r'Bearer\s+[a-zA-Z0-9._-]+',  # Bearer tokens
            r'Basic\s+[a-zA-Z0-9+/=]+',   # Basic auth
            r'ghp_[a-zA-Z0-9]{36}',        # GitHub PAT
            r'gho_[a-zA-Z0-9]{36}',        # GitHub OAuth
            r'xox[baprs]-[a-zA-Z0-9-]+',   # Slack tokens
        ]
        for pattern in default_patterns:
            try:
                self.mask_patterns.append(re.compile(pattern))
            except re.error:
                pass

    def _should_mask_key(self, key: str) -> bool:
        """Check if a key should be masked."""
        if not isinstance(key, str):
            return False
        key_lower = key.lower()
        # Check exact match or partial match
        for mask_key in self.mask_keys:
            if mask_key in key_lower:
                return True
        return False

    def _mask_value(self, value: Any) -> Any:
        """Mask a value if it matches sensitive patterns."""
        if not isinstance(value, str):
            return value

        # Check against patterns
        for pattern in self.mask_patterns:
            if pattern.search(value):
                return MASK_VALUE

        return value

    def _serialize_value(self, value: Any, depth: int = 0) -> Any:
        """
        Recursively serialize a value with masking and depth limits.

        Args:
            value: Value to serialize
            depth: Current recursion depth

        Returns:
            Serialized value safe for JSON
        """
        # Check depth limit
        if depth > self.max_depth:
            return f"[MAX_DEPTH_EXCEEDED:{type(value).__name__}]"

        # Handle None
        if value is None:
            return None

        # Handle basic types
        if isinstance(value, (bool, int, float)):
            return value

        if isinstance(value, str):
            # Mask if matches patterns
            masked = self._mask_value(value)
            if masked != value:
                return masked
            # Truncate long strings
            if len(value) > 500:
                return value[:500] + "...[truncated]"
            return value

        # Handle bytes
        if isinstance(value, bytes):
            try:
                decoded = value.decode('utf-8', errors='replace')
                if len(decoded) > 200:
                    return f"[bytes:{len(value)}]"
                return decoded
            except Exception:
                return f"[bytes:{len(value)}]"

        # Handle dict
        if isinstance(value, dict):
            result = {}
            for k, v in value.items():
                str_key = str(k) if not isinstance(k, str) else k
                if self._should_mask_key(str_key):
                    result[str_key] = MASK_VALUE
                else:
                    result[str_key] = self._serialize_value(v, depth + 1)
            return result

        # Handle list/tuple
        if isinstance(value, (list, tuple)):
            if len(value) > 50:
                # Truncate long lists
                return [self._serialize_value(v, depth + 1) for v in value[:50]] + [f"...[{len(value)-50} more items]"]
            return [self._serialize_value(v, depth + 1) for v in value]

        # Handle set
        if isinstance(value, set):
            return self._serialize_value(list(value), depth)

        # Handle other objects
        if self.include_types:
            # Try to get useful info from the object
            obj_type = type(value).__name__

            # Try __dict__ for object attributes
            if hasattr(value, '__dict__'):
                try:
                    obj_dict = self._serialize_value(value.__dict__, depth + 1)
                    return {"_type": obj_type, "_attrs": obj_dict}
                except Exception:
                    pass

            # Try str representation
            try:
                str_repr = str(value)
                if len(str_repr) > 200:
                    str_repr = str_repr[:200] + "..."
                return f"[{obj_type}:{str_repr}]"
            except Exception:
                return f"[{obj_type}]"

        return f"[{type(value).__name__}]"

    def serialize(self, data: Any) -> str:
        """
        Serialize data to a JSON string safe for span attributes.

        Args:
            data: Data to serialize (dict, list, or any value)

        Returns:
            JSON string representation with sensitive data masked
        """
        try:
            serialized = self._serialize_value(data, depth=0)
            json_str = json.dumps(serialized, default=str, ensure_ascii=False)

            # Truncate if too long
            if len(json_str) > self.max_size:
                json_str = json_str[:self.max_size - len(TRUNCATED_SUFFIX)] + TRUNCATED_SUFFIX

            return json_str

        except Exception as e:
            return f'{{"_error": "serialization_failed", "_type": "{type(data).__name__}"}}'

    def serialize_args(self, args: tuple, kwargs: dict) -> Dict[str, str]:
        """
        Serialize function arguments for span attributes.

        Args:
            args: Positional arguments tuple
            kwargs: Keyword arguments dict

        Returns:
            Dict with 'args' and 'kwargs' keys containing serialized values
        """
        result = {}

        if args:
            result['rpc.request.args'] = self.serialize(list(args))

        if kwargs:
            result['rpc.request.kwargs'] = self.serialize(kwargs)

        return result

    def serialize_http_request(
        self,
        query_params: Optional[Dict] = None,
        body: Optional[Any] = None,
        headers: Optional[Dict] = None,
        selected_headers: Optional[List[str]] = None,
    ) -> Dict[str, str]:
        """
        Serialize HTTP request data for span attributes.

        Args:
            query_params: URL query parameters
            body: Request body (dict, str, or bytes)
            headers: Request headers
            selected_headers: List of header names to include (default: content-type, accept)

        Returns:
            Dict with serialized request data
        """
        result = {}

        if query_params:
            result['http.request.query'] = self.serialize(query_params)

        if body is not None:
            # Try to parse JSON body
            if isinstance(body, (bytes, str)):
                try:
                    if isinstance(body, bytes):
                        body = body.decode('utf-8')
                    parsed = json.loads(body)
                    result['http.request.body'] = self.serialize(parsed)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Not JSON, store as string (truncated)
                    if len(body) > 1000:
                        body = body[:1000] + "...[truncated]"
                    result['http.request.body'] = body
            else:
                result['http.request.body'] = self.serialize(body)

        if headers:
            selected = selected_headers or ['content-type', 'accept', 'x-request-id', 'x-correlation-id']
            filtered = {}
            for key in selected:
                # Case-insensitive header lookup
                for h_key, h_value in headers.items():
                    if h_key.lower() == key.lower():
                        if not self._should_mask_key(h_key):
                            filtered[h_key] = h_value
                        break
            if filtered:
                result['http.request.headers'] = self.serialize(filtered)

        return result

    def serialize_socketio_event(self, event_data: Any) -> Dict[str, str]:
        """
        Serialize Socket.IO event data for span attributes.

        Args:
            event_data: Event payload

        Returns:
            Dict with serialized event data
        """
        result = {}

        if event_data is not None:
            result['messaging.payload'] = self.serialize(event_data)

        return result


# Global instance with default settings
_default_capture: Optional[PayloadCapture] = None


def get_payload_capture(config: Optional[dict] = None) -> PayloadCapture:
    """
    Get or create a PayloadCapture instance.

    Args:
        config: Optional configuration dict with keys:
            - max_size: int
            - max_depth: int
            - additional_mask_keys: list

    Returns:
        PayloadCapture instance
    """
    global _default_capture

    if config:
        return PayloadCapture(
            max_size=config.get('max_size', DEFAULT_MAX_SIZE),
            max_depth=config.get('max_depth', 5),
            additional_mask_keys=set(config.get('additional_mask_keys', [])),
        )

    if _default_capture is None:
        _default_capture = PayloadCapture()

    return _default_capture
