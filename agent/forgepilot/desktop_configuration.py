"""Shared desktop configuration and one-request GUI helper, independent of HTTP startup."""
from __future__ import annotations

import asyncio
import codecs
from datetime import datetime, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import stat
import sys
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from .errors import ForgePilotError
from .models.gateway import ModelGateway
from .models.profiles import ModelConfiguration, ModelProfile, ModelRoute, SecretResolver
from .security import Redactor
from .settings import checked_model_id, resolve_models, resolve_task_model
from .windows_secrets import protect_launcher_key, unprotect_launcher_key

def application_root() -> Path:
    """Locate the app beside bin/ForgePilotAgent.exe or the source agent package."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent
    return Path(__file__).resolve().parents[2]


def default_config_path(root: Path | None = None) -> Path:
    """Keep each installation's historical launcher directory, including its hash casing."""
    installation = str(application_root() if root is None else root)
    # Match the original PowerShell ToLowerInvariant for non-ASCII installation paths.
    normalized = os.path.normcase(installation) if os.name == "nt" else installation.lower()
    identity = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16].upper()
    return Path(os.environ["LOCALAPPDATA"]) / "ForgePilot" / "launcher" / identity / "config.json"


def read_config(path):
    return validated_launcher_config(json.loads(path.read_bytes()))


def _saved_base_url(data):
    endpoint = data.get("base_url")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ValueError("model_config")
    return endpoint


def validated_launcher_config(data):
    """Validate the shared on-disk launcher shape before using or replacing it."""
    required = {'base_url', 'port', 'server_args'}
    model_fields = {'model', 'chat_model', 'task_model', 'summary_model',
                    'review_model', 'task_model_family', 'review_model_family',
                    'credential_file', 'embedding_credential_file'}
    if not isinstance(data, dict) or not required <= set(data) or not set(data) <= required | model_fields:
        raise ValueError('config_fields')
    _saved_base_url(data)
    try:
        models = resolve_models(model=data.get('model'), chat_model=data.get('chat_model'),
                                task_model=data.get('task_model'), summary_model=data.get('summary_model'))
    except ForgePilotError as exc:
        purposes = [name for name in ('fallback', 'chat', 'task', 'summary') if name in exc.message]
        raise ValueError('model_config_' + '_'.join(purposes or ['unknown'])) from None
    review_model, task_family, review_family = _review_fields(data, models.task)
    if type(data['port']) is not int or not 1 <= data['port'] <= 65535:
        raise ValueError('port_config')
    args = data['server_args']
    if not isinstance(args, list) or not all(isinstance(a, str) and '\x00' not in a for a in args):
        raise ValueError('server_args')
    # Host/port and model identity have one authoritative configuration source.
    reserved = {'--host', '--port', '--base-url', '--model', '--chat-model', '--task-model', '--summary-model',
                '--review-model', '--task-model-family', '--review-model-family'}
    if any(a.split('=')[0] in reserved for a in args):
        raise ValueError('reserved_server_arg')
    return {**data, 'model': data.get('model'), 'chat_model': models.chat,
            'task_model': models.task, 'summary_model': models.summary,
            'review_model': review_model, 'task_model_family': task_family,
            'review_model_family': review_family}


def _review_fields(data, task_model):
    try:
        review = checked_model_id(data.get('review_model'), 'review')
    except ForgePilotError:
        raise ValueError('model_config_review') from None
    task_family = data.get('task_model_family') or None
    review_family = data.get('review_model_family') or None
    extras = (review, task_family, review_family)
    if any(extras) and (not all(extras) or review == task_model
                        or not all(isinstance(value, str) and
                                   re.fullmatch(r'[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*', value)
                                   for value in (task_family, review_family))
                        or task_family == review_family):
        raise ValueError('model_config_review')
    return extras


def _saved_model_config(data):
    return _options(data.get("server_args", [])).get("--model-config", os.environ.get("FORGEPILOT_MODEL_CONFIG"))


def _extra_key_names(configuration):
    return {p.api_key_ref.removeprefix("env:") for p in configuration.profiles
            if p.api_key_ref.startswith("env:") and p.api_key_ref != "env:FORGEPILOT_API_KEY"}


_DESKTOP_KEY_PREFIX = "FORGEPILOT_DESKTOP_DPAPI_"


def model_check_environment(config):
    """Pass only the configured model references through the desktop client's restricted environment."""
    environment = {}
    if model_file := os.environ.get("FORGEPILOT_MODEL_CONFIG"):
        environment["FORGEPILOT_MODEL_CONFIG"] = model_file
    if config:
        try:
            model_file = _saved_model_config(read_config(Path(config)))
            if model_file:
                configuration = ModelConfiguration.load(application_root() / model_file)
                for name in _extra_key_names(configuration):
                    if value := os.environ.get(name):
                        environment[_DESKTOP_KEY_PREFIX + name] = protect_launcher_key(value)
        except (OSError, ValueError, ForgePilotError):
            # The settings window also opens to repair an invalid configuration or credential.
            pass
    return environment


def _check_credentials(configuration, key):
    environment = {**os.environ, "FORGEPILOT_API_KEY": key}
    for name in _extra_key_names(configuration):
        if encrypted := os.environ.get(_DESKTOP_KEY_PREFIX + name):
            environment[name] = unprotect_launcher_key(encrypted)
    return SecretResolver(environment=environment)


async def _check_selected_models(data, key, *, model_file=None, transport=None, gateway_factory=ModelGateway):
    """Check each desktop purpose with the same primary and fallback as runtime."""
    routes = [("chat", None), ("task", None), ("summary", None)]
    if model_file or data.get("review_model"):
        configuration = (ModelConfiguration.load(application_root() / model_file) if model_file
                         else model_configuration(data, data.get("server_args", [])))
        gateway = gateway_factory(configuration,
            secret_resolver=_check_credentials(configuration, key),
            transport_factory=lambda _: transport)
        from .agents.registry import BUILTIN_AGENT_DEFINITIONS
        configured = {(r.route_id, r.role) for r in configuration.routes}
        routes.extend((agent.model_route, agent.role) for agent in BUILTIN_AGENT_DEFINITIONS
                      if (agent.model_route, agent.role) in configured or (agent.model_route, None) in configured)
        if ("task", "generalist") in configured:
            routes.append(("task", "generalist"))
    else:
        gateway = gateway_factory.from_legacy(data["base_url"], key,
            {purpose: data[purpose + "_model"] for purpose, _ in routes}, timeout=30, transport=transport)
    async with gateway:
        groups = {}
        for purpose, role in routes:
            client = gateway.for_route(purpose, role=role)
            target = tuple((profile.base_url, profile.model, profile.api_key_ref)
                           for profile in (client.route.primary, client.route.fallback) if profile)
            if target not in groups:
                groups[target] = (client, [])
            labels = groups[target][1]
            # Mission roles using the task connection are already covered by its check.
            if purpose not in labels and (role in (None, "reviewer") or "task" not in labels):
                labels.append(purpose)
        results = []
        for client, grouped_purposes in groups.values():
            result = {"purposes": grouped_purposes, "ok": True}
            try:
                await client.chat([{"role": "user", "content": "Reply with only OK."}])
            except ForgePilotError as exc:
                result.update(ok=False, code=exc.code, message=gateway.redactor.text(str(exc)))
            results.append(result)
        return results


async def check_model(config, *, transport=None, gateway_factory=ModelGateway):
    """Legacy CLI text presentation over the same checks used by the GUI helper."""
    key = os.environ.get("FORGEPILOT_API_KEY", "")
    redactor = Redactor((key,))
    try:
        data = read_config(config)
        if not key:
            raise ForgePilotError("llm_config", "未配置 API Key，请运行 Configure-ForgePilot.cmd。")
        results = await _check_selected_models(data, key, model_file=_saved_model_config(data),
                                              transport=transport, gateway_factory=gateway_factory)
        failed = [result for result in results if not result["ok"]]
        labels = {"chat": "聊天", "task": "任务", "summary": "摘要", "review": "审查",
                  "orchestration": "编排", "analysis": "分析", "implementation": "实现", "repair": "修复"}
        for result in failed:
            label = "/".join(labels[purpose] for purpose in result["purposes"])
            print(f"{label}模型检查失败：" + result["message"], file=sys.stderr)
        if failed:
            return 1
    except ForgePilotError as exc:
        print(redactor.text(str(exc)), file=sys.stderr)
        return 1
    except ValueError as exc:
        code = str(exc)
        if code.startswith("model_config_"):
            labels = [("chat", "聊天"), ("task", "任务"), ("summary", "摘要"),
                      ("review", "审查"), ("fallback", "缺省")]
            affected = "/".join(label for name, label in labels if name in code) or "用途"
            print(f"{affected}模型配置无效，请检查本机 config.json。", file=sys.stderr)
        else:
            print("模型连接检查未完成，请检查本机 config.json 和运行环境。", file=sys.stderr)
        return 1
    except Exception:
        print("模型连接检查未完成，请检查本机 config.json 和运行环境。", file=sys.stderr)
        return 1
    purposes = "、".join(dict.fromkeys(labels[purpose] for result in results for purpose in result["purposes"]))
    print(f"模型连接检查通过：{purposes}用途的所有不同模型均返回了文本回答。")
    return 0


# The GUI edits these values; other trusted launcher arguments remain intact.
OPTION_FLAGS = {"cmake": "--cmake", "ctest": "--ctest", "git": "--git", "rg": "--rg",
                "clangd": "--clangd", "oracle_path": "--mission-oracle",
                "embedding_base_url": "--embedding-base-url", "embedding_model": "--embedding-model"}
BOOLEAN_FLAGS = {"embedding_enabled": "--enable-rag", "cloud_model_allowed": "--mission-allow-cloud-model"}
MODEL_FIELDS = ("chat_model", "task_model", "review_model", "summary_model",
                "task_model_family", "review_model_family")
MAX_CONFIG_BYTES = 1_048_576


class RuntimeFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    base_url: str = "https://api.openai.com/v1"
    port: int = 8765
    chat_model: str = ""
    task_model: str = ""
    review_model: str = ""
    summary_model: str = ""
    task_model_family: str = ""
    review_model_family: str = ""
    external_model_config: str = ""
    embedding_enabled: bool = False
    embedding_base_url: str = ""
    embedding_model: str = ""
    cmake: str = ""
    ctest: str = ""
    git: str = ""
    rg: str = ""
    clangd: str = ""
    oracle_path: str = ""
    cloud_model_allowed: bool = False

    @field_validator("*")
    @classmethod
    def bounded_text(cls, value):
        if isinstance(value, str):
            value = value.strip()
            if len(value) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError("Invalid bounded configuration text")
            value.encode("utf-8")
        return value


class ConfigurationError(ValueError):
    def __init__(self, code, message, purpose="configuration"):
        self.code, self.message, self.purpose = code, message, purpose
        super().__init__(code)


class InvalidCredentialReference(ConfigurationError):
    """The saved reference itself is malformed, before opening the credential."""


def _raw_config(config):
    if not config.exists():
        return {}
    raw = config.read_bytes()
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigurationError("config_invalid", "配置文件过大，请重新填写。")
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError):
        raise ConfigurationError("config_invalid", "配置文件无效，请重新填写。") from None


def _credential_path(config: Path, data: dict, *, embedding=False):
    field = "embedding_credential_file" if embedding else "credential_file"
    if field not in data:
        return config.parent / ("embedding-key.dpapi" if embedding else "api-key.dpapi")
    name = data[field]
    if (not isinstance(name, str) or not re.fullmatch(r"(?:api|embedding)-key-[a-f0-9]{32}\.dpapi", name)):
        raise InvalidCredentialReference("credential_invalid", "已保存的凭据引用无效，请重新填写密钥。")
    return config.parent / name


def read_configured_key(config, data, *, embedding=False):
    """Read the credential referenced by this configuration snapshot."""
    path = _credential_path(config, data, embedding=embedding)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return ""
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_size > MAX_CONFIG_BYTES
            or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ConfigurationError("credential_invalid", "已保存的凭据文件不可用，请重新填写密钥。")
    try:
        ciphertext = path.read_bytes()
        # Windows PowerShell Out-File wraps the same hexadecimal DPAPI blob in UTF-16.
        encoding = "utf-16" if ciphertext.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
        return unprotect_launcher_key(ciphertext.decode(encoding))
    except (ForgePilotError, ValueError, UnicodeError):
        raise ConfigurationError("credential_unavailable", "已保存的密钥无法解密，请重新填写。",
                                 "embedding" if embedding else "configuration") from None


def load_saved_key(config: Path) -> str:
    return read_configured_key(Path(config), _raw_config(Path(config)))


def load_embedding_key(config: Path) -> str:
    return read_configured_key(Path(config), _raw_config(Path(config)), embedding=True)


def load_benchmark_settings(config: Path, *, review_model=None, task_model_family=None, review_model_family=None):
    """Resolve the model-only launcher format and credentials for the benchmark child environment."""
    if not config.is_file():
        raise ConfigurationError("config_missing", "缺少已保存的模型配置。")
    data = _raw_config(config)
    endpoint = _saved_base_url(data)
    try:
        task = resolve_task_model(data.get("task_model"), data.get("model"))
    except ForgePilotError:
        raise ValueError("model_config_task") from None
    if not task:
        raise ValueError("model_config_task")
    key = read_configured_key(config, data)
    if not key:
        raise ConfigurationError("credential_required", "已保存的密钥不存在，请重新配置。")

    overrides = {"review_model": review_model, "task_model_family": task_model_family,
                 "review_model_family": review_model_family}
    routes = {name: value or os.environ.get("FORGEPILOT_" + name.upper()) or data.get(name)
              for name, value in overrides.items()}
    try:
        review, task_family, review_family = _review_fields(routes, task)
        # Formal benchmarks also reject two spellings of the same model ID.
        valid = bool(review and review.casefold() != task.casefold())
    except ValueError:
        # CheckOnly reports presence separately from route validity.
        review = routes["review_model"]
        task_family = review_family = None
        valid = False
    return {"FORGEPILOT_BASE_URL": endpoint, "FORGEPILOT_TASK_MODEL": task,
            "FORGEPILOT_API_KEY": key, "FORGEPILOT_REVIEW_MODEL": review or "",
            "FORGEPILOT_TASK_MODEL_FAMILY": task_family or "",
            "FORGEPILOT_REVIEW_MODEL_FAMILY": review_family or ""}, valid


def _revision(config, data):
    digest = hashlib.sha256(config.read_bytes() if config.exists() else b"missing")
    for embedding in (False, True):
        try:
            path = _credential_path(config, data, embedding=embedding)
        except ConfigurationError:
            digest.update(b"invalid-credential-reference")
            continue
        digest.update(path.name.encode())
        if path.is_file():
            if path.stat().st_size <= 1_048_576 and not path.is_symlink():
                digest.update(path.read_bytes())
            else:
                digest.update(b"unavailable-credential-file")
    return digest.hexdigest()


def configuration_revision(config: Path) -> str:
    """Identity of saved configuration and referenced ciphertext, without decryption."""
    config = Path(config)
    try:
        data = _raw_config(config)
    except ConfigurationError:
        data = {}
    return _revision(config, data)


def _options(args):
    result = {}
    values, booleans = {*OPTION_FLAGS.values(), "--model-config"}, set(BOOLEAN_FLAGS.values())
    index = 0
    while index < len(args):
        argument = args[index]
        option, sep, value = argument.partition("=")
        if option in values:
            if not sep and index + 1 < len(args):
                index += 1
                value = args[index]
            result[option] = value
        elif option in booleans:
            result[option] = True
        index += 1
    return result


def _fields_from_config(data, config):
    fields = RuntimeFields().model_dump()
    fields.update({name: data[name] for name in ("base_url", "port", *MODEL_FIELDS)
                   if name in data and type(data[name]) is type(fields[name])})
    try:
        selected = resolve_models(model=data.get("model"), chat_model=data.get("chat_model"),
                                  task_model=data.get("task_model"), summary_model=data.get("summary_model"))
        fields.update(chat_model=selected.chat, task_model=selected.task, summary_model=selected.summary)
    except ForgePilotError:
        pass  # An incomplete local form remains editable; configured stays false.
    args = data.get("server_args", [])
    if isinstance(args, list) and all(isinstance(arg, str) for arg in args):
        options = _options(args)
        fields.update({key: options.get(flag, "") for key, flag in OPTION_FLAGS.items()})
        fields.update({key: bool(options.get(flag, False)) for key, flag in BOOLEAN_FLAGS.items()})
        reference = options.get("--model-config")
        if reference:
            path = application_root() / reference
            generated = False
            if path.parent == config.parent and re.fullmatch(r"models-[a-f0-9]{32}\.json", path.name):
                try:
                    generated = (ModelConfiguration.load(path, preserve_output_limits=True)
                                 == model_configuration(fields, args))
                except (ForgePilotError, ConfigurationError):
                    pass  # An unreadable or hand-edited file remains under its owner's control.
            if not generated:
                fields["external_model_config"] = reference
    return fields


def _checked_fields(raw, *, check_optional=True):
    try:
        fields = RuntimeFields.model_validate(raw).model_dump()
    except ValidationError as exc:
        name = next(iter(exc.errors(include_input=False)), {}).get("loc", ("configuration",))[0]
        purpose = str(name).removesuffix("_model") if name in MODEL_FIELDS else "configuration"
        raise ConfigurationError("config_fields", "配置字段无效，请检查对应项目。", purpose) from None
    try:
        fields["base_url"] = ModelProfile.valid_endpoint(fields["base_url"])
    except ValueError:
        raise ConfigurationError("model_endpoint", "服务地址须为不含凭据或查询参数的 HTTP(S) API 根地址。") from None
    if not 1 <= fields["port"] <= 65535:
        raise ConfigurationError("port_config", "本机端口须为 1 到 65535。")
    try:
        selected = resolve_models(chat_model=fields["chat_model"], task_model=fields["task_model"],
                                  summary_model=fields["summary_model"])
    except ForgePilotError as exc:
        purpose = next(name for name in ("chat", "task", "summary") if name in exc.message)
        raise ConfigurationError("model_required", "请填写有效的模型标识。", purpose) from None
    fields.update(chat_model=selected.chat, task_model=selected.task, summary_model=selected.summary)
    try:
        _review_fields(fields, selected.task)
    except ValueError:
        raise ConfigurationError("model_config_review", "独立审查须使用不同模型与模型家族；请填写两种家族标识。", "review") from None
    if check_optional:
        issues = _optional_issues(fields)
        if issues:
            raise issues[0]
    return fields


def _optional_issues(fields):
    from .process import executable
    issues = []
    for name in ("cmake", "ctest", "git", "rg", "clangd"):
        if fields[name]:
            try:
                fields[name] = executable(fields[name], name)
            except ForgePilotError:
                impact = ("语义检索退回文本。" if name == "clangd" else
                          "构建或测试不可用，普通聊天仍可用。" if name in {"cmake", "ctest"} else
                          "代码任务可能不可用，普通聊天仍可用。")
                issues.append(ConfigurationError("tool_missing", "工具路径不可用；请选择存在的绝对路径。" + impact, name))
    oracle = fields["oracle_path"]
    if oracle and (not Path(oracle).is_absolute() or not Path(oracle).is_dir()):
        issues.append(ConfigurationError("oracle_config", "Oracle 目录不可用；仅要求受保护测试的任务受影响。", "oracle"))
    if fields["embedding_enabled"]:
        from .rag.config import RagConfig
        try:
            config = RagConfig(enabled=True, embedding_model=fields["embedding_model"],
                               embedding_base_url=fields["embedding_base_url"])
            if not config.embedding_model or not config.embedding_base_url:
                raise ValueError()
        except ValueError:
            issues.append(ConfigurationError("embedding_config", "Embedding 配置不完整或无效；代码索引退回文件与文本检索。", "embedding"))
    return issues


def _server_arguments(old, fields, keep_model_config):
    args = old.get("server_args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) and "\0" not in arg for arg in args):
        args = []
    result = []
    owned_values = {*OPTION_FLAGS.values(), "--host", "--port", "--base-url", "--model",
                    "--chat-model", "--task-model", "--summary-model", "--review-model",
                    "--task-model-family", "--review-model-family"}
    if not keep_model_config:
        owned_values.add("--model-config")
    owned_bools = set(BOOLEAN_FLAGS.values())
    index = 0
    while index < len(args):
        option, sep, _ = args[index].partition("=")
        if option in owned_values:
            index += 1 if sep else 2
            continue
        if option not in owned_bools:
            result.append(args[index])
        index += 1
    for field, flag in OPTION_FLAGS.items():
        if fields[field]:
            result.extend((flag, fields[field]))
    result.extend(flag for field, flag in BOOLEAN_FLAGS.items() if fields[field])
    return result


def _uses_saved_models(fields, previous):
    return bool(previous["external_model_config"]) or all(
        fields[name] == previous[name] for name in ("base_url", *MODEL_FIELDS))


def _candidate(old, fields, previous, config):
    # Only the established non-secret launcher shape is persisted.
    model_fields = ("base_url", *MODEL_FIELDS)
    if previous["external_model_config"]:
        data = {key: old[key] for key in ("model", *model_fields) if key in old}
    else:
        data = {key: fields[key] for key in model_fields}
    data["port"] = fields["port"]
    data["server_args"] = _server_arguments(old, fields, _uses_saved_models(fields, previous))
    if not old:
        data["server_args"].extend(("--database", str(config.parent / "history.sqlite3"), "--timeout", "120"))
    for key in ("credential_file", "embedding_credential_file"):
        if key in old:
            try:
                _credential_path(config, old, embedding=key.startswith("embedding"))
                data[key] = old[key]
            except ConfigurationError:
                pass  # An invalid reference is not carried into a new valid configuration.
    return data


def _error(error):
    return {"purpose": error.purpose, "code": error.code, "message": error.message}


async def _runtime_status(status):
    if status is None:
        return {"managed": False, "active_count": 0, "restarting": False, "restart_allowed": True}
    result = status()
    return await result if inspect.isawaitable(result) else result


async def load_configuration(config: Path, *, status=None):
    config = Path(config)
    errors = []
    try:
        data = _raw_config(config)
    except ConfigurationError as exc:
        data = {}
        errors.append(_error(exc))
    fields = _fields_from_config(data, config)
    try:
        validated_launcher_config(data)
        _checked_fields(fields, check_optional=False)
        valid = bool(data)
    except (ConfigurationError, ValueError):
        valid = False
    flags = {}
    for embedding, name in ((False, "has_api_key"), (True, "has_embedding_key")):
        try:
            flags[name] = bool(read_configured_key(config, data, embedding=embedding))
        except ConfigurationError as exc:
            flags[name] = False
            errors.append(_error(exc))
    warnings = [issue.purpose + "：" + issue.message for issue in _optional_issues(dict(fields))]
    if not fields["oracle_path"]:
        warnings.append("未配置 Oracle：普通任务可运行；明确要求受保护测试的任务将暂停。")
    if not fields["embedding_enabled"]:
        warnings.append("未启用 Embedding：代码索引不可用，任务保留文件与文本检索。")
    if not fields["cmake"]:
        warnings.append("未指定 CMake：构建时使用本机 PATH；缺失时构建任务不可用，普通聊天可用。")
    if not fields["cloud_model_allowed"]:
        warnings.append("任务未授权向远端模型发送仓库内容，本地模型任务仍可用。")
    revision = _revision(config, data)
    runtime = await _runtime_status(status)
    configured = valid and flags["has_api_key"]
    pending = configured and (not runtime.get("managed") or runtime.get("running_revision") != revision)
    return {"ok": True, "fields": fields, "configured": configured, **flags,
            "config_revision": revision, "restart_required": pending, "errors": errors, "warnings": warnings,
            "notice": "已有配置；保存仅在受控重启后生效。" if configured else "请完成模型和密钥配置。",
            **runtime}


def model_configuration(fields: dict, server_args: list[str]) -> ModelConfiguration:
    """Generate the established profiles/routes format, without consulting legacy flags."""
    timeout = 120.0
    for index, argument in enumerate(server_args):
        option, sep, value = argument.partition("=")
        if option == "--timeout":
            try:
                timeout = float(value if sep else server_args[index + 1])
            except (ValueError, IndexError):
                raise ConfigurationError("model_timeout", "模型超时配置无效，请检查本机启动参数。") from None
    try:
        profiles = tuple(ModelProfile(profile_id="desktop-" + purpose, base_url=fields["base_url"],
            api_key_ref="env:FORGEPILOT_API_KEY", model=fields[purpose + "_model"],
            family=fields["review_model_family"] if purpose == "review" else fields["task_model_family"],
            timeout_seconds=timeout) for purpose in ("chat", "task", "review", "summary"))
        routes = tuple(ModelRoute(route_id=route, primary="desktop-task")
            for route in ("task", "orchestration", "analysis", "implementation", "repair"))
        routes += tuple(ModelRoute(route_id=purpose, primary="desktop-" + purpose)
                        for purpose in ("chat", "review", "summary"))
        return ModelConfiguration(profiles=profiles, routes=routes, enforce_review_family_separation=True)
    except ValueError:
        raise ConfigurationError("model_configuration", "模型路由配置无效，请检查模型、家族或超时。") from None


def _write_new_file(path: Path, payload: bytes) -> None:
    stream = path.open("xb")
    try:
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _new_models(config, fields, server_args):
    models = model_configuration(fields, server_args)
    path = config.parent / ("models-" + uuid4().hex + ".json")
    _write_new_file(path, (models.model_dump_json(indent=2) + "\n").encode("utf-8"))
    return path


def _new_credential(config, value, *, embedding=False):
    filename = ("embedding-key-" if embedding else "api-key-") + uuid4().hex + ".dpapi"
    path = config.parent / filename
    # Encrypt before creating a file; plaintext never enters temporary storage.
    _write_new_file(path, protect_launcher_key(value).encode("ascii"))
    return filename


def _replace_config(config, data):
    temporary = config.with_name(config.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(config)
    finally:
        temporary.unlink(missing_ok=True)


class ConfigurationLock:
    """Nonblocking per-config write serialization, released by OS on process exit."""
    def __init__(self, config):
        self.path = config.parent / "configuration.lock"
        self.stream = None

    def __enter__(self):
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise ConfigurationError("config_busy", "另一窗口正在保存配置，请读取最新配置后重试。") from None
        return self

    def __exit__(self, *_):
        self.stream.close()


def save_configuration(config, request):
    fields = _checked_fields(request.get("fields"))
    config.parent.mkdir(parents=True, exist_ok=True)
    with ConfigurationLock(config):
        try:
            old = _raw_config(config)
        except ConfigurationError:
            old = {}  # Replacing malformed configuration requires the loaded byte revision.
        if request.get("config_revision") != _revision(config, old):
            raise ConfigurationError("config_conflict", "配置已被其他窗口修改，请重新读取后保存。")
        previous = _fields_from_config(old, config)
        data = _candidate(old, fields, previous, config)
        key = request.get("api_key", "")
        embedding_key = request.get("embedding_api_key", "")
        if not isinstance(key, str) or not isinstance(embedding_key, str):
            raise ConfigurationError("credential_invalid", "请输入有效密钥。")
        if not key and not read_configured_key(config, old):
            raise ConfigurationError("credential_required", "首次配置须填写 API Key。")
        created = []
        try:
            for value, name, embedding in ((key, "credential_file", False),
                                            (embedding_key, "embedding_credential_file", True)):
                if value:
                    filename = _new_credential(config, value, embedding=embedding)
                    created.append(config.parent / filename)
                    data[name] = filename
            if fields["review_model"] and not _options(data["server_args"]).get("--model-config"):
                models_path = _new_models(config, fields, data["server_args"])
                created.append(models_path)
                data["server_args"].extend(("--model-config", str(models_path)))
            validated_launcher_config(data)
            _replace_config(config, data)
        except BaseException:
            for path in created:
                path.unlink(missing_ok=True)
            raise
    return fields


async def check_configuration(config, request, *, transport=None):
    if any(not isinstance(request.get(name, ""), str) for name in ("api_key", "embedding_api_key")):
        raise ConfigurationError("credential_invalid", "请输入有效密钥。")
    fields = _checked_fields(request.get("fields"))
    data = _raw_config(config)
    if request.get("config_revision") != _revision(config, data):
        raise ConfigurationError("config_conflict", "配置已改变，请重新读取后检测。")
    key = request.get("api_key") or read_configured_key(config, data)
    if not isinstance(key, str) or not key:
        raise ConfigurationError("credential_required", "请填写或保存 API Key。")
    errors, checks = [], []
    saved = _fields_from_config(data, config)
    use_saved = data and _uses_saved_models(fields, saved)
    results = await _check_selected_models({**fields, "server_args": data.get("server_args", [])}, key,
        model_file=_saved_model_config(data) if use_saved else None, transport=transport)
    for result in results:
        if result["ok"]:
            checks.extend({"purpose": purpose, "ok": True} for purpose in result["purposes"])
        else:
            errors.extend({"purpose": purpose, "code": result["code"], "message": result["message"]}
                          for purpose in result["purposes"])
    if fields["embedding_enabled"]:
        from .rag.embedding import OpenAICompatibleEmbeddingProvider
        embedding_key = request.get("embedding_api_key") or read_configured_key(config, data, embedding=True) or key
        try:
            async with OpenAICompatibleEmbeddingProvider(fields["embedding_base_url"], embedding_key,
                    fields["embedding_model"], timeout=30, transport=transport) as provider:
                await provider.embed_query("Connection check")
            checks.append({"purpose": "embedding", "ok": True})
        except ForgePilotError as exc:
            errors.append({"purpose": "embedding", "code": exc.code, "message": Redactor((embedding_key,)).text(str(exc))})
        except Exception:
            errors.append({"purpose": "embedding", "code": "connection_failed", "message": "Embedding 连接或响应无效。"})
    return {"ok": not errors, "checks": checks, "errors": errors,
            "notice": "所有已选用途连接检测通过。" if not errors else "连接检测未通过，请查看对应用途。"}


async def handle_request(config, request, *, restart=None, status=None, transport=None):
    """Exactly one request per helper; input secrets are never included in responses."""
    config = Path(config)
    try:
        if (not isinstance(request, dict) or not set(request) <= {
                "action", "fields", "api_key", "embedding_api_key", "config_revision"}):
            raise ConfigurationError("invalid_request", "配置请求格式无效。")
        action = request.get("action")
        if action == "load":
            return await load_configuration(config, status=status)
        if action == "save":
            save_configuration(config, request)
            result = await load_configuration(config, status=status)
            result.update(saved_at=datetime.now(timezone.utc).isoformat(), restart_required=True,
                          notice="配置已保存；当前运行继续使用原配置，受控重启后新配置生效。")
            return result
        if action == "check":
            return await check_configuration(config, request, transport=transport)
        if action == "restart":
            if not (await load_configuration(config))["configured"]:
                raise ConfigurationError("config_required", "请先保存完整有效配置，再重启。")
            if request.get("config_revision") != _revision(config, _raw_config(config)):
                raise ConfigurationError("config_conflict", "配置已改变，请重新读取后重启。")
            if restart is None:
                raise ConfigurationError("restart_unavailable", "请通过本机启动器重新启动。")
            result = restart()
            return await result if inspect.isawaitable(result) else result
        raise ConfigurationError("invalid_request", "未知配置操作。")
    except ConfigurationError as exc:
        return {"ok": False, "errors": [_error(exc)], "notice": exc.message}
    except (OSError, ForgePilotError, ValueError):
        return {"ok": False, "errors": [{"purpose": "configuration", "code": "configuration_failed",
                "message": "配置操作未完成，请重新读取确认状态，并检查文件权限或密钥。"}],
                "notice": "配置操作未完成，请检查本机运行环境。"}
