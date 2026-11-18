"""Runtime connection values and fixed, immutable configuration-service models.

MCPConfig keeps the legacy CLI file shape. Neither model arguments nor server
instructions are ever merged into these connection settings.
Persisted server settings exclude credential values; secret_updates is a separate
service argument. Runtime settings are built only after merging/decrypting keys.
"""
import ipaddress
from pathlib import Path
import re
from typing import Annotated, Literal
from uuid import UUID
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator, model_validator

from .errors import ForgePilotError

SERVER_NAME = "project_notes"
REMOTE_TOOL = "project_info"
TOOL_NAME = f"mcp_{SERVER_NAME}__{REMOTE_TOOL}"


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    command: str
    args: tuple[str, ...] = ()
    cwd: str
    request_timeout: float = Field(default=8.0, gt=0, le=30, allow_inf_nan=False)
    env: tuple[tuple[str, str], ...] = Field(default=(), repr=False)
    public_env_names: tuple[str, ...] = Field(default=(), max_length=16)

    def checked(self):
        command, cwd = Path(self.command), Path(self.cwd)
        if (not command.is_absolute() or not command.is_file() or command.suffix.lower() != ".exe"
                or not cwd.is_absolute() or not cwd.is_dir() or len(self.args) > 32
                or any("\0" in arg or len(arg) > 4096 for arg in self.args)):
            raise ForgePilotError("mcp_config", "MCP requires a trusted absolute executable, cwd and bounded argv")
        names = [name.upper() for name, _ in self.env]
        if (len(names) > 16 or len(set(names)) != len(names)
                or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name)
                       or "\0" in value or len(value) > 4096 for name, value in self.env)):
            raise ForgePilotError("mcp_config", "Invalid explicit MCP environment")
        _check_public_env(self.public_env_names, names)
        return self


class MCPHttpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    transport: Literal["streamable_http"] = "streamable_http"
    endpoint: str = Field(min_length=1, max_length=2048)
    auth_type: Literal["none", "bearer", "api_key"] = "none"
    header_name: str | None = None
    # Runtime credential; never serialize this object as a persisted configuration.
    secret: str | None = Field(default=None, repr=False, exclude=True)
    request_timeout: float = Field(default=8.0, gt=0, le=30, allow_inf_nan=False)

    def checked(self):
        try:
            url = urlsplit(self.endpoint)
            loopback = url.hostname == "localhost"
            if url.hostname:
                try:
                    loopback |= ipaddress.ip_address(url.hostname).is_loopback
                except ValueError:
                    pass
            if (not url.hostname or url.username is not None or url.password is not None
                    or "?" in self.endpoint or "#" in self.endpoint or "\\" in self.endpoint
                    or any(ord(c) <= 32 or ord(c) == 127 for c in self.endpoint)
                    or url.scheme not in {"https", "http"} or (url.scheme == "http" and not loopback)
                    or url.port == 0):
                raise ValueError()
            if self.auth_type == "none":
                if self.secret is not None or self.header_name is not None:
                    raise ValueError()
            else:
                if (not self.secret or len(self.secret) > 4096
                        or any(ord(c) < 32 or ord(c) > 126 for c in self.secret)):
                    raise ValueError()
                if self.auth_type == "bearer" and self.header_name is not None:
                    raise ValueError()
                if self.auth_type == "api_key":
                    name = self.header_name or ""
                    if (not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", name)
                            or name.lower().startswith(("mcp-", "proxy-", "sec-"))
                            or name.lower() in {"host", "content-type", "content-length", "accept", "accept-encoding",
                                "connection", "transfer-encoding", "authorization", "cookie", "origin", "last-event-id",
                                "te", "trailer", "upgrade"}):
                        raise ValueError()
        except ValueError:
            raise ForgePilotError("mcp_config", "Invalid MCP endpoint or authentication settings") from None
        return self

    def headers(self):
        self.checked()
        if self.auth_type == "bearer":
            return {"Authorization": "Bearer " + self.secret}
        if self.auth_type == "api_key":
            return {self.header_name: self.secret}
        return {}


def create_mcp_connection(config):
    """Construct one owned connection; callers share it across selected tools."""
    if isinstance(config, MCPHttpConfig):
        from .mcp_http import MCPHttpClient
        return MCPHttpClient(config)
    if isinstance(config, MCPConfig):
        from .mcp import MCPClient
        return MCPClient(config)
    raise ForgePilotError("mcp_config", "Unsupported MCP connection configuration")


def load_mcp_config(path):
    if path is None:
        return None
    try:
        source = Path(path)
        if not source.is_absolute() or source.stat().st_size > 16_384:
            raise ValueError()
        # JSON validation permits JSON arrays for the immutable argv tuple.
        return MCPConfig.model_validate_json(source.read_text(encoding="utf-8")).checked()
    except (OSError, UnicodeError, ValueError, ValidationError):
        raise ForgePilotError("mcp_config", "Cannot read a valid explicit MCP configuration file") from None


# Configuration-service contracts; credentials are supplied separately at runtime.
MAX_SERVERS = 8
MAX_ENABLED_SERVERS = 4
MAX_ENABLED_TOOLS = 20
MAX_DEFINITION_CHARS = 24_576
MAX_CATALOG_BYTES = 262_144
MAX_RESPONSE_BYTES = 524_288
PROBE_TIMEOUT = 20.0
TAVILY_ENDPOINT = "https://mcp.tavily.com/mcp/"
TAVILY_TOOLS = ("tavily-search", "tavily-extract")
# Exact names observed on Tavily's remote 4.0.4 server. Keep the documented
# legacy pair for existing configurations; never normalize arbitrary names.
TAVILY_TOOL_SETS = (TAVILY_TOOLS, ("tavily_search", "tavily_extract"))
TAVILY_PRESET_ID = "tavily"
Revision = Annotated[int, Field(ge=0, le=2**63 - 2, strict=True)]
ToolName = Annotated[str, Field(min_length=1, max_length=128)]


class _ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    @field_validator("modes", "selected_tools", "args", "env_names", "public_env_names", mode="before", check_fields=False)
    @classmethod
    def json_arrays(cls, value):
        # FastAPI validates decoded JSON, while CLI/service validate JSON text.
        # Accept its arrays in either entrypoint, retaining immutable tuples.
        return tuple(value) if isinstance(value, list) else value


class MCPHTTPSettings(_ConfigModel):
    endpoint: str = Field(min_length=1, max_length=2048)
    auth_type: Literal["none", "bearer", "api_key"] = "none"
    header_name: str | None = None
    preset_id: Literal["tavily"] | None = None

    @model_validator(mode="after")
    def connection_fields(self):
        MCPHttpConfig(endpoint=self.endpoint, auth_type=self.auth_type, header_name=self.header_name,
                      secret=None if self.auth_type == "none" else "validation-placeholder").checked()
        if self.preset_id is not None and (self.endpoint != TAVILY_ENDPOINT or self.auth_type != "bearer"
                                           or self.header_name is not None):
            raise ForgePilotError("mcp_config", "Tavily uses its fixed endpoint and Bearer authentication")
        return self


class MCPStdioSettings(_ConfigModel):
    command: str = Field(min_length=1, max_length=4096)
    args: tuple[Annotated[str, Field(max_length=4096)], ...] = Field(default=(), max_length=32)
    cwd: str | None = Field(default=None, max_length=4096)
    env_names: tuple[str, ...] = Field(default=(), max_length=16)
    public_env_names: tuple[str, ...] = Field(default=(), max_length=16)

    @model_validator(mode="after")
    def connection_fields(self):
        command = Path(self.command)
        # Existence is checked immediately before probing, so an unavailable exe
        # cannot prevent viewing, disabling or deleting a stored configuration.
        if not command.is_absolute() or command.suffix.lower() != ".exe":
            raise ForgePilotError("mcp_config", "Local MCP command must be an absolute exe path")
        cwd = self.cwd or str(command.parent)
        if not Path(cwd).is_absolute() or any("\0" in value for value in (self.command, cwd, *self.args)):
            raise ForgePilotError("mcp_config", "Invalid MCP working directory or argv")
        names = [name.upper() for name in self.env_names]
        if (len(names) != len(set(names)) or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name)
                                               for name in self.env_names)):
            raise ForgePilotError("mcp_config", "Environment names must be valid and distinct")
        _check_public_env(self.public_env_names, names)
        object.__setattr__(self, "cwd", cwd)
        return self


class _ServerSettings(_ConfigModel):
    name: str = Field(min_length=1, max_length=40)
    modes: tuple[Literal["chat", "task"], ...] = ("chat", "task")
    selected_tools: tuple[ToolName, ...] = Field(default=(), max_length=MAX_ENABLED_TOOLS)
    effect: Literal["read"] = "read"
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def display_name(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Name must contain printable text")
        return value

    @model_validator(mode="after")
    def selections(self):
        if not self.modes or len(set(self.modes)) != len(self.modes) or len(set(self.selected_tools)) != len(self.selected_tools):
            raise ForgePilotError("mcp_config", "Choose distinct modes and tool names")
        return self


class MCPHTTPServer(_ServerSettings):
    transport: Literal["streamable_http"]
    connection: MCPHTTPSettings


class MCPStdioServer(_ServerSettings):
    transport: Literal["stdio"]
    connection: MCPStdioSettings


MCPServerSettings = Annotated[MCPHTTPServer | MCPStdioServer, Field(discriminator="transport")]
SERVER_SETTINGS = TypeAdapter(MCPServerSettings)
CredentialUpdates = Annotated[dict[Annotated[str, Field(min_length=1, max_length=132)],
                                   Annotated[str, Field(max_length=4096)] | None], Field(max_length=32)]
SECRET_UPDATES = TypeAdapter(CredentialUpdates, config=ConfigDict(strict=True, hide_input_in_errors=True))


def checked_server_settings(value):
    """Parse the fixed JSON-facing shape, accepting arrays without coercing scalars."""
    import json
    try:
        if isinstance(value, (MCPHTTPServer, MCPStdioServer)):
            value = value.model_dump(mode="json")
        return SERVER_SETTINGS.validate_json(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError, RecursionError):
        raise ForgePilotError("mcp_config", "Invalid MCP configuration fields or limits") from None


def checked_revision(value, *, existing=False):
    try:
        result = TypeAdapter(Revision).validate_python(value)
        if existing and result == 0:
            raise ValueError()
        return result
    except ValueError:
        raise ForgePilotError("mcp_config", "A valid configuration revision is required") from None


def checked_server_id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ForgePilotError("mcp_config", "A valid MCP server UUID is required") from None


def required_secrets(config):
    """Credential patch keys: api_key for HTTP; env:NAME for explicit stdio env."""
    if isinstance(config, MCPHTTPServer):
        return () if config.connection.auth_type == "none" else ("api_key",)
    return tuple("env:" + name for name in config.connection.env_names)


def _check_public_env(public_names, env_names):
    names = [name.upper() for name in public_names]
    if len(names) > 16 or len(names) != len(set(names)) or not set(names) <= set(env_names):
        raise ForgePilotError("mcp_config", "Public environment names must be distinct configured names")


def credential_scope(config):
    """Only these HTTP fields authorize reuse of a stored credential."""
    if not isinstance(config, MCPHTTPServer):
        return ("stdio",)
    c = config.connection
    url = urlsplit(c.endpoint)
    return (config.transport, url.scheme.lower(), url.hostname.lower(),
            url.port or (443 if url.scheme.lower() == "https" else 80),
            url.path or "/", c.auth_type, (c.header_name or "").lower())


def sensitive_values(config, values):
    """DPAPI still stores every value; classification only controls redaction."""
    connection = config.connection if hasattr(config, "connection") else config
    public = {name.upper() for name in getattr(connection, "public_env_names", ())}
    return tuple(value for key, value in values.items() if value is not None
                 and (key == "api_key" or key.startswith("env:") and key[4:].upper() not in public))


def runtime_config(config, secrets, *, request_timeout=30.0):
    connection = config.connection
    if any(name not in secrets for name in required_secrets(config)):
        raise ForgePilotError("mcp_secret_required", "Required MCP credentials are missing")
    if isinstance(config, MCPHTTPServer):
        return MCPHttpConfig(endpoint=connection.endpoint, auth_type=connection.auth_type,
            header_name=connection.header_name, secret=secrets.get("api_key"), request_timeout=request_timeout).checked()
    if not Path(connection.command).is_file():
        raise ForgePilotError("mcp_executable_missing", "Configured MCP executable does not exist")
    return MCPConfig(command=connection.command, args=connection.args, cwd=connection.cwd,
        env=tuple((name, secrets["env:" + name]) for name in connection.env_names),
        public_env_names=connection.public_env_names,
        request_timeout=request_timeout).checked()


def mcp_presets():
    # Names/endpoint are from the approved plan, NOT a claim of live verification.
    return [{"preset_id": TAVILY_PRESET_ID, "name": "联网搜索", "transport": "streamable_http",
        "connection": {"endpoint": TAVILY_ENDPOINT, "auth_type": "bearer", "header_name": None,
                       "preset_id": TAVILY_PRESET_ID},
        "default_tools": list(TAVILY_TOOLS), "required_secrets": ["api_key"],
        "real_service_verified": False}]
