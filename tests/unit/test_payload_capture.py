"""Unit tests for utils/payload_capture.py - PayloadCapture class."""
import pytest
import json
import re
from typing import Any, Dict, List, Optional, Set


DEFAULT_SENSITIVE_KEYS = {
    'password', 'passwd', 'pwd', 'secret', 'token', 'api_key', 'apikey',
    'api-key', 'auth', 'authorization', 'bearer', 'credential', 'credentials',
    'private_key', 'privatekey', 'access_token', 'refresh_token', 'session',
    'cookie', 'x-api-key', 'x-auth-token', 'jwt', 'key', 'cert', 'certificate',
}

DEFAULT_MAX_SIZE = 4096
MASK_VALUE = "***MASKED***"
TRUNCATED_SUFFIX = "...[TRUNCATED]"


class PayloadCapture:
    """Copy of PayloadCapture for isolated testing."""

    def __init__(
        self,
        max_size: int = DEFAULT_MAX_SIZE,
        max_depth: int = 5,
        mask_keys: Optional[Set[str]] = None,
        additional_mask_keys: Optional[Set[str]] = None,
        mask_patterns: Optional[List[str]] = None,
        include_types: bool = True,
    ):
        self.max_size = max_size
        self.max_depth = max_depth
        self.include_types = include_types

        if mask_keys is not None:
            self.mask_keys = {k.lower() for k in mask_keys}
        else:
            self.mask_keys = DEFAULT_SENSITIVE_KEYS.copy()

        if additional_mask_keys:
            self.mask_keys.update(k.lower() for k in additional_mask_keys)

        self.mask_patterns = []
        if mask_patterns:
            for pattern in mask_patterns:
                try:
                    self.mask_patterns.append(re.compile(pattern, re.IGNORECASE))
                except re.error:
                    pass

        self._add_default_patterns()

    def _add_default_patterns(self):
        default_patterns = [
            r'sk-[a-zA-Z0-9]{20,}',
            r'Bearer\s+[a-zA-Z0-9._-]+',
            r'Basic\s+[a-zA-Z0-9+/=]+',
            r'ghp_[a-zA-Z0-9]{36}',
            r'gho_[a-zA-Z0-9]{36}',
            r'xox[baprs]-[a-zA-Z0-9-]+',
        ]
        for pattern in default_patterns:
            try:
                self.mask_patterns.append(re.compile(pattern))
            except re.error:
                pass

    def _should_mask_key(self, key: str) -> bool:
        if not isinstance(key, str):
            return False
        key_lower = key.lower()
        for mask_key in self.mask_keys:
            if mask_key in key_lower:
                return True
        return False

    def _mask_value(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        for pattern in self.mask_patterns:
            if pattern.search(value):
                return MASK_VALUE
        return value

    def _serialize_value(self, value: Any, depth: int = 0) -> Any:
        if depth > self.max_depth:
            return f"[MAX_DEPTH_EXCEEDED:{type(value).__name__}]"

        if value is None:
            return None

        if isinstance(value, (bool, int, float)):
            return value

        if isinstance(value, str):
            masked = self._mask_value(value)
            if masked != value:
                return masked
            if len(value) > 500:
                return value[:500] + "...[truncated]"
            return value

        if isinstance(value, bytes):
            try:
                decoded = value.decode('utf-8', errors='replace')
                if len(decoded) > 200:
                    return f"[bytes:{len(value)}]"
                return decoded
            except Exception:
                return f"[bytes:{len(value)}]"

        if isinstance(value, dict):
            result = {}
            for k, v in value.items():
                str_key = str(k) if not isinstance(k, str) else k
                if self._should_mask_key(str_key):
                    result[str_key] = MASK_VALUE
                else:
                    result[str_key] = self._serialize_value(v, depth + 1)
            return result

        if isinstance(value, (list, tuple)):
            if len(value) > 50:
                return [self._serialize_value(v, depth + 1) for v in value[:50]] + [f"...[{len(value)-50} more items]"]
            return [self._serialize_value(v, depth + 1) for v in value]

        if isinstance(value, set):
            return self._serialize_value(list(value), depth)

        if self.include_types:
            obj_type = type(value).__name__
            if hasattr(value, '__dict__'):
                try:
                    obj_dict = self._serialize_value(value.__dict__, depth + 1)
                    return {"_type": obj_type, "_attrs": obj_dict}
                except Exception:
                    pass
            try:
                str_repr = str(value)
                if len(str_repr) > 200:
                    str_repr = str_repr[:200] + "..."
                return f"[{obj_type}:{str_repr}]"
            except Exception:
                return f"[{obj_type}]"

        return f"[{type(value).__name__}]"

    def serialize(self, data: Any) -> str:
        try:
            serialized = self._serialize_value(data, depth=0)
            json_str = json.dumps(serialized, default=str, ensure_ascii=False)
            if len(json_str) > self.max_size:
                json_str = json_str[:self.max_size - len(TRUNCATED_SUFFIX)] + TRUNCATED_SUFFIX
            return json_str
        except Exception as e:
            return f'{{"_error": "serialization_failed", "_type": "{type(data).__name__}"}}'


class TestPayloadCaptureBasics:
    """Tests for basic PayloadCapture functionality."""

    def test_serialize_none(self):
        capture = PayloadCapture()
        result = capture.serialize(None)
        assert result == "null"

    def test_serialize_string(self):
        capture = PayloadCapture()
        result = capture.serialize("hello")
        assert result == '"hello"'

    def test_serialize_int(self):
        capture = PayloadCapture()
        result = capture.serialize(42)
        assert result == "42"

    def test_serialize_float(self):
        capture = PayloadCapture()
        result = capture.serialize(3.14)
        assert result == "3.14"

    def test_serialize_bool(self):
        capture = PayloadCapture()
        assert capture.serialize(True) == "true"
        assert capture.serialize(False) == "false"

    def test_serialize_dict(self):
        capture = PayloadCapture()
        data = {"name": "test", "value": 123}
        result = json.loads(capture.serialize(data))
        assert result == {"name": "test", "value": 123}

    def test_serialize_list(self):
        capture = PayloadCapture()
        data = [1, 2, 3]
        result = json.loads(capture.serialize(data))
        assert result == [1, 2, 3]


class TestPayloadCaptureMasking:
    """Tests for sensitive data masking."""

    def test_masks_password_key(self):
        capture = PayloadCapture()
        data = {"username": "admin", "password": "secret123"}
        result = json.loads(capture.serialize(data))
        assert result["username"] == "admin"
        assert result["password"] == MASK_VALUE

    def test_masks_api_key(self):
        capture = PayloadCapture()
        data = {"api_key": "sk-abc123", "data": "value"}
        result = json.loads(capture.serialize(data))
        assert result["api_key"] == MASK_VALUE
        assert result["data"] == "value"

    def test_masks_token_key(self):
        capture = PayloadCapture()
        data = {"access_token": "eyJ...", "refresh_token": "xyz"}
        result = json.loads(capture.serialize(data))
        assert result["access_token"] == MASK_VALUE
        assert result["refresh_token"] == MASK_VALUE

    def test_masks_authorization_header(self):
        capture = PayloadCapture()
        data = {"Authorization": "Bearer token123", "Content-Type": "application/json"}
        result = json.loads(capture.serialize(data))
        assert result["Authorization"] == MASK_VALUE
        assert result["Content-Type"] == "application/json"

    def test_masks_partial_key_match(self):
        capture = PayloadCapture()
        data = {"user_password_hash": "abc123", "db_password": "secret"}
        result = json.loads(capture.serialize(data))
        assert result["user_password_hash"] == MASK_VALUE
        assert result["db_password"] == MASK_VALUE

    def test_masks_openai_api_key_pattern(self):
        capture = PayloadCapture()
        data = {"key": "sk-abcdefghijklmnopqrstuvwxyz123456"}
        result = json.loads(capture.serialize(data))
        assert result["key"] == MASK_VALUE

    def test_masks_github_pat_pattern(self):
        capture = PayloadCapture()
        token = "ghp_" + "a" * 36
        data = {"token": token}
        result = json.loads(capture.serialize(data))
        assert result["token"] == MASK_VALUE

    def test_masks_bearer_token_in_value(self):
        capture = PayloadCapture()
        data = {"header": "Bearer eyJhbGciOiJIUzI1NiJ9.test"}
        result = json.loads(capture.serialize(data))
        assert result["header"] == MASK_VALUE

    def test_custom_mask_keys(self):
        capture = PayloadCapture(mask_keys={"custom_secret"})
        data = {"custom_secret": "value", "password": "visible"}
        result = json.loads(capture.serialize(data))
        assert result["custom_secret"] == MASK_VALUE
        assert result["password"] == "visible"

    def test_additional_mask_keys(self):
        capture = PayloadCapture(additional_mask_keys={"my_secret"})
        data = {"my_secret": "hidden", "password": "also_hidden", "name": "visible"}
        result = json.loads(capture.serialize(data))
        assert result["my_secret"] == MASK_VALUE
        assert result["password"] == MASK_VALUE
        assert result["name"] == "visible"


class TestPayloadCaptureTruncation:
    """Tests for size limits and truncation."""

    def test_truncates_long_string(self):
        capture = PayloadCapture()
        long_string = "x" * 600
        result = capture.serialize(long_string)
        parsed = json.loads(result)
        assert len(parsed) == 500 + len("...[truncated]")
        assert parsed.endswith("...[truncated]")

    def test_truncates_long_list(self):
        capture = PayloadCapture()
        long_list = list(range(100))
        result = json.loads(capture.serialize(long_list))
        assert len(result) == 51  # 50 items + truncation message
        assert "more items" in result[-1]

    def test_truncates_total_output(self):
        capture = PayloadCapture(max_size=100)
        data = {"name": "x" * 200}
        result = capture.serialize(data)
        assert len(result) <= 100
        assert result.endswith(TRUNCATED_SUFFIX)

    def test_max_depth_exceeded(self):
        capture = PayloadCapture(max_depth=2)
        data = {"a": {"b": {"c": {"d": "deep"}}}}
        result = json.loads(capture.serialize(data))
        # At depth 3, it should hit MAX_DEPTH_EXCEEDED
        assert "MAX_DEPTH_EXCEEDED" in str(result)


class TestPayloadCaptureSpecialTypes:
    """Tests for handling special types."""

    def test_handles_bytes(self):
        capture = PayloadCapture()
        data = b"hello bytes"
        result = capture.serialize(data)
        assert "hello bytes" in result

    def test_handles_long_bytes(self):
        capture = PayloadCapture()
        data = b"x" * 300
        result = capture.serialize(data)
        assert "[bytes:300]" in result

    def test_handles_set(self):
        capture = PayloadCapture()
        data = {1, 2, 3}
        result = json.loads(capture.serialize(data))
        assert sorted(result) == [1, 2, 3]

    def test_handles_nested_dict(self):
        capture = PayloadCapture()
        data = {"outer": {"inner": {"value": 42}}}
        result = json.loads(capture.serialize(data))
        assert result["outer"]["inner"]["value"] == 42

    def test_handles_mixed_types(self):
        capture = PayloadCapture()
        data = {
            "string": "hello",
            "int": 42,
            "float": 3.14,
            "bool": True,
            "none": None,
            "list": [1, 2, 3],
            "nested": {"key": "value"}
        }
        result = json.loads(capture.serialize(data))
        assert result["string"] == "hello"
        assert result["int"] == 42
        assert result["bool"] is True
        assert result["none"] is None


class TestPayloadCaptureEdgeCases:
    """Tests for edge cases."""

    def test_empty_dict(self):
        capture = PayloadCapture()
        result = capture.serialize({})
        assert result == "{}"

    def test_empty_list(self):
        capture = PayloadCapture()
        result = capture.serialize([])
        assert result == "[]"

    def test_non_string_dict_keys(self):
        capture = PayloadCapture()
        data = {1: "one", 2: "two"}
        result = json.loads(capture.serialize(data))
        assert result["1"] == "one"
        assert result["2"] == "two"

    def test_case_insensitive_key_masking(self):
        capture = PayloadCapture()
        data = {"PASSWORD": "secret", "Password": "secret2", "password": "secret3"}
        result = json.loads(capture.serialize(data))
        for key in result:
            assert result[key] == MASK_VALUE

    def test_should_mask_key_non_string(self):
        capture = PayloadCapture()
        assert capture._should_mask_key(123) is False
        assert capture._should_mask_key(None) is False
