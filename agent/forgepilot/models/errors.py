"""Safe provider failure metadata; never store response bodies or transport errors."""
from ..errors import ForgePilotError


class ModelError(ForgePilotError):
    def __init__(self, code: str, message: str, *, category: str,
                 status_code: int | None = None):
        super().__init__(code, message)
        self.category = category
        self.status_code = status_code


def http_error(status: int) -> ModelError:
    category = ("authentication" if status == 401 else "permission" if status == 403
                else "policy" if status == 451 else "network" if status == 408
                else "rate_limit" if status == 429 else "server" if status >= 500
                else "request")
    reason = ("模型鉴权失败，请核对 API Key 与 Base URL 所属服务；运行 Configure-ForgePilot.cmd 更新密钥后，依次运行 Stop-ForgePilot.cmd 和 Start-ForgePilot.cmd。" if status == 401 else
              "模型访问被拒绝，请检查模型访问权限。" if status == 403 else
              "Rate limited" if status == 429 else
              "Redirects are disabled" if 300 <= status < 400 else
              "Model service rejected the request")
    return ModelError("llm_http", f"HTTP {status}. {reason}", category=category,
                      status_code=status)


def category_of(error: ForgePilotError) -> str:
    if isinstance(error, ModelError):
        return error.category if error.category in {
            "authentication", "permission", "policy", "network", "rate_limit", "server",
            "request", "schema", "budget", "incomplete",
        } else "request"
    return {"llm_timeout": "network", "llm_network": "network",
            "llm_protocol": "schema", "llm_schema": "schema",
            "llm_tool_schema": "schema", "llm_tool_unknown": "policy",
            "llm_budget": "budget", "policy_denied": "policy",
            "unknown_tool": "policy", "invalid_arguments": "schema"}.get(error.code, "request")
