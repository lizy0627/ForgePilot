"""Safe, stable diagnostics. Never include raw transport/process exceptions."""


class ForgePilotError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class MCPTransportError(ForgePilotError):
    """Safe HTTP recovery metadata; never retain headers, bodies or session IDs."""
    def __init__(self, code, message, *, status_code=None, retry_after_seconds=None):
        super().__init__(code, message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class MCPRequestError(ForgePilotError):
    """A validated JSON-RPC error ends one request, not its healthy session."""
    def __init__(self, message="MCP server rejected the request"):
        super().__init__("mcp_request", message)
