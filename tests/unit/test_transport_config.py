"""Unit tests for HTTP transport configuration (_parse_transport_config)."""

import os
import pytest
from unittest.mock import patch

from garmin_mcp import _parse_transport_config, _VALID_TRANSPORTS

# Every variable the parser reads, cleared so a stray value cannot mask a bug.
_TRANSPORT_ENV = (
    "GARMIN_MCP_TRANSPORT",
    "GARMIN_MCP_HOST",
    "GARMIN_MCP_PORT",
    "GARMIN_MCP_STATELESS",
    "GARMIN_MCP_JSON_RESPONSE",
)


@pytest.fixture
def clean_env():
    """Run with every transport variable unset, restoring them afterwards."""
    with patch.dict(os.environ, {}, clear=False):
        for name in _TRANSPORT_ENV:
            os.environ.pop(name, None)
        yield


class TestParseTransportConfig:
    """Tests for _parse_transport_config."""

    def test_default_is_stdio(self, clean_env):
        transport, settings = _parse_transport_config()
        assert transport == "stdio"
        assert settings["host"] == "127.0.0.1"
        assert settings["port"] == 8000

    @pytest.mark.parametrize("value", list(_VALID_TRANSPORTS))
    def test_valid_transports_are_accepted(self, clean_env, value):
        os.environ["GARMIN_MCP_TRANSPORT"] = value
        transport, _ = _parse_transport_config()
        assert transport == value

    def test_transport_value_is_lowercased(self, clean_env):
        os.environ["GARMIN_MCP_TRANSPORT"] = "STDIO"
        transport, _ = _parse_transport_config()
        assert transport == "stdio"

    def test_transport_value_is_stripped(self, clean_env):
        os.environ["GARMIN_MCP_TRANSPORT"] = "  streamable-http  "
        transport, _ = _parse_transport_config()
        assert transport == "streamable-http"

    def test_invalid_transport_raises_value_error(self, clean_env):
        os.environ["GARMIN_MCP_TRANSPORT"] = "websocket"
        with pytest.raises(ValueError, match="Invalid GARMIN_MCP_TRANSPORT"):
            _parse_transport_config()

    def test_custom_host_is_read(self, clean_env):
        os.environ["GARMIN_MCP_HOST"] = "0.0.0.0"
        _, settings = _parse_transport_config()
        assert settings["host"] == "0.0.0.0"

    def test_custom_port_is_read(self, clean_env):
        os.environ["GARMIN_MCP_PORT"] = "9000"
        _, settings = _parse_transport_config()
        assert settings["port"] == 9000

    def test_invalid_port_raises(self, clean_env):
        os.environ["GARMIN_MCP_PORT"] = "not-a-number"
        with pytest.raises(ValueError):
            _parse_transport_config()


class TestStatelessDefaults:
    """streamable-http must default to stateless.

    A session-based stream stays open while the client sits idle, and a platform
    that bills per request-second charges for all of it. Stateless answers each
    call in its own short request instead, so this default is what keeps a
    hosted deployment cheap.
    """

    def test_streamable_http_is_stateless_by_default(self, clean_env):
        os.environ["GARMIN_MCP_TRANSPORT"] = "streamable-http"
        _, settings = _parse_transport_config()
        assert settings["stateless_http"] is True
        assert settings["json_response"] is True

    def test_stateless_can_be_disabled(self, clean_env):
        os.environ.update(
            {"GARMIN_MCP_TRANSPORT": "streamable-http", "GARMIN_MCP_STATELESS": "false"}
        )
        _, settings = _parse_transport_config()
        assert settings["stateless_http"] is False
        # json_response follows stateless unless it is set on its own.
        assert settings["json_response"] is False

    def test_json_response_can_be_set_independently(self, clean_env):
        os.environ.update(
            {
                "GARMIN_MCP_TRANSPORT": "streamable-http",
                "GARMIN_MCP_STATELESS": "false",
                "GARMIN_MCP_JSON_RESPONSE": "true",
            }
        )
        _, settings = _parse_transport_config()
        assert settings["stateless_http"] is False
        assert settings["json_response"] is True

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_flag_spellings(self, clean_env, value):
        os.environ.update(
            {"GARMIN_MCP_TRANSPORT": "streamable-http", "GARMIN_MCP_STATELESS": value}
        )
        _, settings = _parse_transport_config()
        assert settings["stateless_http"] is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_falsey_flag_spellings(self, clean_env, value):
        os.environ.update(
            {"GARMIN_MCP_TRANSPORT": "streamable-http", "GARMIN_MCP_STATELESS": value}
        )
        _, settings = _parse_transport_config()
        assert settings["stateless_http"] is False

    @pytest.mark.parametrize("transport", ["stdio", "sse"])
    def test_other_transports_carry_no_stateless_settings(self, clean_env, transport):
        """These keys are meaningless outside streamable-http."""
        os.environ["GARMIN_MCP_TRANSPORT"] = transport
        _, settings = _parse_transport_config()
        assert "stateless_http" not in settings
        assert "json_response" not in settings
