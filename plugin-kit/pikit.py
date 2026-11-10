#!/usr/bin/env python3
"""pikit — python-pi 插件开发工具（MVP）。

四个命令覆盖「写 → 验 → 跑 → 发」：

    pikit new  <id> [--template basic|http] [--dir .]
    pikit check <插件目录> [--json]
    pikit dev  <插件目录> [--tool NAME] [--args JSON] [--config JSON]
    pikit pack <插件目录> [--out FILE] [--force]

设计原则：
- 只用标准库，插件作者不需要装 python-pi 也能先跑通；
- 校验规则尽量与宿主加载器一致（manifest 正则、factory 别名、工具 schema）；
- check 会真的 import 模块并执行 setup()，不是静态猜测；
- 不执行任何网络请求，除非作者自己的工具在 dev 里被显式调用。

退出码：0 通过（允许警告），1 校验失败，2 用法错误。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import sys
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# --------------------------------------------------------------------------------------
# 常量：与宿主保持一致的部分
# --------------------------------------------------------------------------------------

DEFAULT_SDK_VERSION = "0.1.0"

#: 宿主会发出的事件（docs/EXTENSIONS.md / extensions/types.py）。
EVENTS = frozenset(
    {
        "session_start",
        "session_shutdown",
        "session_info_changed",
        "session_before_switch",
        "session_before_fork",
        "input",
        "before_agent_start",
        "agent_start",
        "agent_end",
        "turn_start",
        "turn_end",
        "message_start",
        "message_update",
        "message_end",
        "tool_execution_start",
        "tool_execution_update",
        "tool_execution_end",
        "tool_call",
        "tool_result",
        "model_select",
        "thinking_level_select",
    }
)

CAPABILITIES = ("tools", "commands", "events", "providers", "ui")
FACTORY_NAMES = ("setup", "extension", "register")
API_VERSIONS = ("1", "2")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
COMMAND_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,63}$")
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
PARAM_TYPES = {"string", "number", "integer", "boolean", "array", "object"}

NETWORK_MODULES = {
    "aiohttp",
    "ftplib",
    "http",
    "httpx",
    "requests",
    "smtplib",
    "socket",
    "ssl",
    "telnetlib",
    "urllib",
    "websocket",
    "websockets",
}
PROCESS_MODULES = {"ctypes", "multiprocessing", "pty", "subprocess"}
#: 明确会写盘的模块函数（按点分名匹配，避免把 str.replace 之类误判）。
WRITE_CALLS = {
    "os.chmod",
    "os.chown",
    "os.makedirs",
    "os.mkdir",
    "os.remove",
    "os.rename",
    "os.replace",
    "os.rmdir",
    "os.unlink",
    "shutil.copy",
    "shutil.copy2",
    "shutil.copyfile",
    "shutil.move",
    "shutil.rmtree",
}
JUNK_DIRS = {"__pycache__", ".git", ".venv", "node_modules", ".pytest_cache"}
JUNK_FILES = {".DS_Store"}
MAX_ZIP_BYTES = 20 * 1024 * 1024  # 宿主安装接口的上限


# --------------------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------------------

@dataclass
class Finding:
    level: str  # error | warning | note
    message: str
    where: str = ""

    def render(self) -> str:
        prefix = {"error": "✗", "warning": "!", "note": "·"}.get(self.level, "·")
        location = f"  [{self.where}]" if self.where else ""
        return f"  {prefix} {self.message}{location}"


@dataclass
class Report:
    path: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    api: Any = None
    findings: list[Finding] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[Finding]:
        return [item for item in self.findings if item.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [item for item in self.findings if item.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors


def _error(report: Report, message: str, where: str = "") -> None:
    report.findings.append(Finding("error", message, where))


def _warn(report: Report, message: str, where: str = "") -> None:
    report.findings.append(Finding("warning", message, where))


def _note(report: Report, message: str, where: str = "") -> None:
    report.findings.append(Finding("note", message, where))


# --------------------------------------------------------------------------------------
# manifest
# --------------------------------------------------------------------------------------

def read_manifest(path: Path, report: Report) -> dict[str, Any]:
    manifest_path = path / "plugin.json"
    if not manifest_path.is_file():
        _error(report, "缺少 plugin.json", str(manifest_path))
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _error(report, f"plugin.json 不是合法 JSON：{error}", str(manifest_path))
        return {}
    if not isinstance(data, dict):
        _error(report, "plugin.json 顶层必须是对象", str(manifest_path))
        return {}
    return data


def entry_path(path: Path, manifest: dict[str, Any]) -> Path | None:
    entry = str(manifest.get("entry") or "main.py").strip()
    if not entry or entry.startswith(("/", "..")) or ".." in entry.split("/"):
        return None
    candidate = (path / entry).resolve()
    try:
        candidate.relative_to(path.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def validate_manifest(path: Path, manifest: dict[str, Any], sdk_version: str, report: Report) -> None:
    plugin_id = str(manifest.get("id") or "").strip()
    if not ID_RE.match(plugin_id):
        _error(report, "id 必须是小写字母/数字/连字符，且 ≤41 字符", "id")

    version = str(manifest.get("version") or "").strip()
    if not version:
        _warn(report, "缺少 version，宿主会按 0.0.0 处理", "version")
    elif not VERSION_RE.match(version):
        _warn(report, f"version {version!r} 不是语义化版本（建议 x.y.z）", "version")

    api_version = str(manifest.get("apiVersion") or "1")
    if api_version not in API_VERSIONS:
        _warn(report, f"apiVersion {api_version!r} 未识别（支持 {'/'.join(API_VERSIONS)}）", "apiVersion")

    entry = entry_path(path, manifest)
    if entry is None:
        _error(report, "entry 必须是插件目录内的 .py 文件，且实际存在", "entry")

    capabilities = manifest.get("capabilities")
    if capabilities is not None:
        if not isinstance(capabilities, list):
            _error(report, "capabilities 必须是数组", "capabilities")
        else:
            for item in capabilities:
                if item not in CAPABILITIES:
                    _warn(report, f"未知 capability {item!r}（可用：{', '.join(CAPABILITIES)}）", "capabilities")

    permissions = manifest.get("permissions")
    if permissions is not None:
        if not isinstance(permissions, dict):
            _error(report, "permissions 必须是对象", "permissions")
        else:
            filesystem = permissions.get("filesystem", "none")
            if filesystem not in ("none", "read", "write"):
                _error(report, "permissions.filesystem 只能是 none/read/write", "permissions")
            network = permissions.get("network", False)
            if not isinstance(network, (bool, list)):
                _error(report, "permissions.network 必须是布尔或域名数组", "permissions")
            if "process" in permissions and not isinstance(permissions["process"], bool):
                _error(report, "permissions.process 必须是布尔", "permissions")
            secrets = permissions.get("secrets")
            if secrets is not None:
                if not isinstance(secrets, list) or not all(isinstance(x, str) for x in secrets):
                    _error(report, "permissions.secrets 必须是字符串数组", "permissions")

    validate_config(manifest, report)
    validate_requires(manifest, sdk_version, report)
    validate_limits(manifest, report)

    if manifest and not manifest.get("name"):
        _note(report, "没有 name，宿主会回退到 id", "name")


def validate_config(manifest: dict[str, Any], report: Report) -> None:
    config = manifest.get("config")
    if config is None:
        return
    if not isinstance(config, dict):
        _error(report, "config 必须是 JSON Schema 对象", "config")
        return
    if config.get("type") not in (None, "object"):
        _error(report, "config.type 必须是 object", "config")
    properties = config.get("properties", {})
    if not isinstance(properties, dict):
        _error(report, "config.properties 必须是对象", "config")
        return
    for name, spec in properties.items():
        where = f"config.properties.{name}"
        if not isinstance(spec, dict):
            _error(report, "配置项必须是对象（type/default/description）", where)
            continue
        kind = spec.get("type")
        if kind is not None and kind not in PARAM_TYPES:
            _error(report, f"不支持的配置类型 {kind!r}", where)
        if "default" in spec and kind in PARAM_TYPES and not _matches_type(spec["default"], kind):
            _error(report, f"默认值 {spec['default']!r} 与类型 {kind} 不匹配", where)
        env = spec.get("env")
        if env is not None and (not isinstance(env, str) or not ENV_RE.match(env)):
            _error(report, "env 必须是全大写环境变量名", where)
        if spec.get("secret"):
            if kind not in (None, "string"):
                _error(report, "secret 配置项必须是字符串", where)
            if "default" in spec:
                _warn(report, "secret 项不应写 default（不要提交密钥）", where)
    required = config.get("required", [])
    if required and not isinstance(required, list):
        _error(report, "config.required 必须是数组", "config")
    elif isinstance(required, list):
        for name in required:
            if name not in properties:
                _error(report, f"required 里的 {name!r} 没有在 properties 中定义", "config")


def validate_requires(manifest: dict[str, Any], sdk_version: str, report: Report) -> None:
    requires = manifest.get("requires")
    if requires is None:
        return
    if not isinstance(requires, dict):
        _error(report, "requires 必须是对象", "requires")
        return
    spec = requires.get("pi")
    if spec:
        if not isinstance(spec, str):
            _error(report, "requires.pi 必须是版本范围字符串", "requires")
        elif not satisfies(sdk_version, spec):
            _error(report, f"要求 python-pi {spec}，当前 SDK {sdk_version}", "requires")
    python_spec = requires.get("python")
    if python_spec:
        current = ".".join(str(part) for part in sys.version_info[:3])
        if isinstance(python_spec, str) and not satisfies(current, python_spec):
            _warn(report, f"要求 Python {python_spec}，当前 {current}", "requires")
    packages = requires.get("packages")
    if packages is not None:
        if not isinstance(packages, list) or not all(isinstance(x, str) for x in packages):
            _error(report, "requires.packages 必须是字符串数组", "requires")
        else:
            for name in packages:
                module = name.split("[")[0].replace("-", "_")
                if importlib.util.find_spec(module) is None and importlib.util.find_spec(name) is None:
                    _warn(report, f"依赖包 {name!r} 未安装，运行时可能 ImportError", "requires")


def validate_limits(manifest: dict[str, Any], report: Report) -> None:
    limits = manifest.get("limits")
    if limits is None:
        return
    if not isinstance(limits, dict):
        _error(report, "limits 必须是对象", "limits")
        return
    timeout = limits.get("timeoutSeconds")
    if timeout is not None and (not isinstance(timeout, int) or not 1 <= timeout <= 600):
        _error(report, "limits.timeoutSeconds 必须是 1–600 的整数", "limits")
    max_chars = limits.get("maxResultChars")
    if max_chars is not None and (not isinstance(max_chars, int) or not 1 <= max_chars <= 1_000_000):
        _error(report, "limits.maxResultChars 必须是 1–1000000 的整数", "limits")


def _matches_type(value: Any, kind: str) -> bool:
    if kind == "string":
        return isinstance(value, str)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "array":
        return isinstance(value, list)
    if kind == "object":
        return isinstance(value, dict)
    return True


# --------------------------------------------------------------------------------------
# 版本范围
# --------------------------------------------------------------------------------------

def _version_key(value: str) -> tuple[int, int, int]:
    numbers = [int(part) for part in re.findall(r"\d+", value)[:3]]
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)  # type: ignore[return-value]


def satisfies(version: str, spec: str) -> bool:
    """支持 `*`、`>=x`、`>x`、`<=x`、`<x`、`=x`/`==x`、`^x`、`~x`、空格求交、`||` 求并。"""
    if not spec or not spec.strip() or spec.strip() == "*":
        return True
    for alternative in spec.split("||"):
        if _satisfies_all(version, alternative.strip()):
            return True
    return False


def _satisfies_all(version: str, spec: str) -> bool:
    for token in spec.replace(",", " ").split():
        if not _satisfies_one(version, token):
            return False
    return True


def _satisfies_one(version: str, token: str) -> bool:
    match = re.match(r"^(>=|<=|==|=|>|<|\^|~)?\s*v?(\d[\w.+-]*)$", token)
    if not match:
        return True  # 不认识的写法不阻塞，由 check 的其它规则兜底
    operator, raw = match.group(1) or "=", match.group(2)
    have, want = _version_key(version), _version_key(raw)
    if operator in ("=", "=="):
        return have == want
    if operator == ">=":
        return have >= want
    if operator == ">":
        return have > want
    if operator == "<=":
        return have <= want
    if operator == "<":
        return have < want
    if operator == "^":
        if want[0] > 0:
            return have >= want and have[0] == want[0]
        return have >= want and have[:2] == want[:2]
    if operator == "~":
        return have >= want and have[:2] == want[:2]
    return True


# --------------------------------------------------------------------------------------
# 运行时对象：记录 setup() 的注册动作
# --------------------------------------------------------------------------------------

@dataclass
class ToolInfo:
    name: str
    description: str
    parameters: dict[str, Any]
    execute: Any = None
    meta: Any = None


@dataclass
class CommandInfo:
    name: str
    description: str
    handler: Any = None


class AbortSignalStub:
    """足够让插件里的 `signal.aborted` / `on_abort()` 不炸。"""

    aborted = False

    def on_abort(self, callback: Any) -> Any:
        del callback
        return lambda: None


class RecordingAPI:
    """与 ``ExtensionAPI`` 同形的注册面，用于在宿主之外跑 setup()。"""

    def __init__(
        self,
        path: str = "<inline>",
        config: dict[str, Any] | None = None,
        cwd: str | None = None,
    ) -> None:
        self.path = path
        self.config = dict(config or {})
        # 与宿主一致：插件必须用 api.cwd 定位工作区，不能用 os.getcwd()。
        self.cwd = cwd or os.getcwd()
        self.handlers: dict[str, list[Any]] = {}
        self.tools: dict[str, ToolInfo] = {}
        self.commands: dict[str, CommandInfo] = {}
        self.flags: dict[str, Any] = {}
        self.providers: list[Any] = []
        self._host = None

    # -- events ------------------------------------------------------------------------

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    # -- tools -------------------------------------------------------------------------

    def register_tool(
        self,
        tool: Any = None,
        *,
        name: str | None = None,
        description: str = "",
        parameters: dict[str, Any] | None = None,
        label: str | None = None,
        prompt_snippet: str | None = None,
        prompt_guidelines: list[str] | None = None,
        execution_mode: str = "parallel",
        prepare_arguments: Any = None,
    ) -> Any:
        del label, prompt_snippet, prompt_guidelines, execution_mode, prepare_arguments

        def build(function: Any) -> ToolInfo:
            return ToolInfo(
                name=name or getattr(function, "__name__", "unnamed_tool"),
                description=description or (inspect.getdoc(function) or "").strip().split("\n")[0],
                parameters=parameters or {"type": "object", "properties": {}},
                execute=function,
            )

        if tool is not None:
            info = ToolInfo(
                name=str(getattr(tool, "name", "")),
                description=str(getattr(tool, "description", "")),
                parameters=getattr(tool, "parameters", {}) or {},
                execute=getattr(tool, "execute", None),
                meta=tool,
            )
            self._add_tool(info)
            return tool
        if name is None and parameters is None:
            def bare(function: Any) -> ToolInfo:
                info = build(function)
                self._add_tool(info)
                return info

            return bare

        def decorator(function: Any) -> Any:
            info = build(function)
            self._add_tool(info)
            return function

        return decorator

    def _add_tool(self, tool: ToolInfo) -> None:
        if not tool.name:
            raise ValueError("工具必须有 name")
        self.tools[tool.name] = tool

    # -- commands / flags / providers ---------------------------------------------------

    def register_command(self, name: str, description: str = "", handler: Any = None) -> Any:
        if handler is not None:
            self.commands[name] = CommandInfo(name, description, handler)
            return handler

        def decorator(function: Any) -> Any:
            self.commands[name] = CommandInfo(name, description or (inspect.getdoc(function) or "").strip().split("\n")[0], function)
            return function

        return decorator

    def register_flag(self, name: str, description: str = "", default: Any = None) -> None:
        self.flags[name] = {"description": description, "default": default}

    def register_provider(self, name: str, config: dict[str, Any] | None = None) -> None:
        self.providers.append({"name": name, "config": dict(config or {})})


# --------------------------------------------------------------------------------------
# 加载
# --------------------------------------------------------------------------------------

async def load(
    path: Path,
    report: Report,
    overrides: dict[str, Any] | None = None,
    workspace: Path | None = None,
) -> None:
    manifest = report.manifest
    target = entry_path(path, manifest)
    if target is None:
        return
    config = resolve_config(manifest, overrides)
    report.config = config
    try:
        module = _import_module(target)
    except Exception as error:  # noqa: BLE001 - 作者的模块可能抛任何东西
        _error(report, f"导入失败：{type(error).__name__}: {error}", str(target))
        return

    factory = next((getattr(module, key) for key in FACTORY_NAMES if callable(getattr(module, key, None))), None)
    if factory is None:
        _error(report, "模块里没有可调用的 setup / extension / register", str(target))
        return

    api = RecordingAPI(path=str(target), config=config, cwd=str(workspace or Path.cwd()))
    try:
        returned = factory(api)
        if inspect.isawaitable(returned):
            await asyncio.wait_for(returned, timeout=30)
    except Exception as error:  # noqa: BLE001
        _error(report, f"setup() 失败：{type(error).__name__}: {error}", str(target))
        return
    report.api = api


def _import_module(target: Path) -> Any:
    name = "pikit_plugin_" + hashlib.sha256(str(target).encode()).hexdigest()[:16]
    if target.name == "__init__.py":
        spec = importlib.util.spec_from_file_location(
            name, str(target), submodule_search_locations=[str(target.parent)]
        )
    else:
        spec = importlib.util.spec_from_file_location(name, str(target))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {target} 构造 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def resolve_config(manifest: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {}
    properties = (manifest.get("config") or {}).get("properties") or {}
    if isinstance(properties, dict):
        for key, spec in properties.items():
            if not isinstance(spec, dict):
                continue
            if "default" in spec:
                config[key] = spec["default"]
            env = spec.get("env")
            if isinstance(env, str) and os.environ.get(env):
                config[key] = os.environ[env]
    config.update(overrides or {})
    return config


# --------------------------------------------------------------------------------------
# 校验：注册物
# --------------------------------------------------------------------------------------

def validate_registrations(manifest: dict[str, Any], report: Report) -> None:
    api: RecordingAPI | None = report.api
    if api is None:
        return

    for name, tool in api.tools.items():
        where = f"tool:{name}"
        if not TOOL_NAME_RE.match(name):
            _error(report, f"工具名 {name!r} 不合法（字母/数字/下划线/连字符，≤64）", where)
        if not tool.description:
            _warn(report, "没有 description，模型不知道何时调用", where)
        elif len(tool.description) > 1024:
            _warn(report, "description 超过 1024 字符，建议精简", where)
        schema = tool.parameters
        if not isinstance(schema, dict):
            _error(report, "parameters 必须是 JSON Schema 对象", where)
            continue
        if schema.get("type") != "object":
            _error(report, 'parameters.type 必须是 "object"', where)
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            _error(report, "parameters.properties 必须是对象", where)
            properties = {}
        if not properties:
            _note(report, "工具没有参数", where)
        for required in schema.get("required") or []:
            if required not in properties:
                _error(report, f"required 里的 {required!r} 没有在 properties 中定义", where)
        for prop, spec in properties.items():
            if isinstance(spec, dict) and spec.get("type") not in (None, *PARAM_TYPES):
                _error(report, f"参数 {prop} 的类型 {spec.get('type')!r} 不支持", where)
        if tool.execute is None:
            _error(report, "工具没有可执行的函数", where)

    for name, command in api.commands.items():
        if not COMMAND_NAME_RE.match(name):
            _warn(report, f"命令名 {name!r} 建议用小写字母/数字/冒号/连字符", f"command:{name}")

    for event, handlers in api.handlers.items():
        if event not in EVENTS:
            _warn(report, f"事件 {event!r} 本版本不会发出，处理器永远不会被调用", f"event:{event}")
        if not handlers:
            _warn(report, "事件没有任何处理器", f"event:{event}")

    if api.providers:
        _note(report, "register_provider 目前没有消费方（宿主未实现 provider 注册表）", "provider")

    declared = set(manifest.get("capabilities") or [])
    if declared:
        if api.tools and "tools" not in declared:
            _warn(report, '注册了工具但 capabilities 没有 "tools"', "capabilities")
        if api.commands and "commands" not in declared:
            _warn(report, '注册了命令但 capabilities 没有 "commands"', "capabilities")
        if api.handlers and "events" not in declared:
            _warn(report, '注册了事件处理器但 capabilities 没有 "events"', "capabilities")
        if "ui" in declared:
            _note(report, "宿主暂不支持插件贡献界面（dsh.client 的对应物不存在）", "capabilities")


# --------------------------------------------------------------------------------------
# 校验：静态扫描（声称的权限 vs 代码实际做的事）
# --------------------------------------------------------------------------------------

def static_scan(path: Path, manifest: dict[str, Any], report: Report) -> None:
    permissions = manifest.get("permissions") or {}
    filesystem = permissions.get("filesystem", "none")
    network = permissions.get("network", False)
    process = permissions.get("process", False)
    secrets = permissions.get("secrets") or []

    network_hits: list[str] = []
    process_hits: list[str] = []
    write_hits: list[str] = []
    env_hits: list[str] = []

    for source in _python_files(path):
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError):
            continue
        relative = source.relative_to(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    _classify(root, f"{relative}:{node.lineno}", network_hits, process_hits)
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                _classify(root, f"{relative}:{node.lineno}", network_hits, process_hits)
            elif isinstance(node, ast.Call):
                name = _call_name(node.func)
                if _is_write_call(name, node):
                    write_hits.append(f"{relative}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == "environ":
                env_hits.append(f"{relative}:{node.lineno}")

    if network is False and network_hits:
        _warn(report, f"permissions.network=false，但代码引用了网络模块（{', '.join(network_hits[:3])}）", "permissions")
    if filesystem == "none" and write_hits:
        _warn(report, f"permissions.filesystem=none，但代码会写文件（{', '.join(write_hits[:3])}）", "permissions")
    if filesystem == "read" and write_hits:
        _warn(report, f"permissions.filesystem=read，但代码会写文件（{', '.join(write_hits[:3])}）", "permissions")
    if not process and process_hits:
        _warn(report, f"permissions.process=false，但代码会起子进程（{', '.join(process_hits[:3])}）", "permissions")
    if env_hits and not secrets:
        _note(report, "读取了环境变量，建议在 permissions.secrets 里列出变量名", "permissions")


def _classify(root: str, where: str, network_hits: list[str], process_hits: list[str]) -> None:
    if root in NETWORK_MODULES:
        network_hits.append(where)
    elif root in PROCESS_MODULES:
        process_hits.append(where)


def _call_name(node: ast.AST) -> str:
    """Dotted callee name: ``open``、``os.remove``、``path.write_text``。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _is_write_call(name: str, node: ast.Call) -> bool:
    if name in ("open", "builtins.open", "io.open", "os.open"):
        return _writes(node)
    if name in WRITE_CALLS:
        return True
    return name.endswith(".write_text") or name.endswith(".write_bytes")


def _writes(node: ast.Call) -> bool:
    mode: ast.expr | None = node.args[1] if len(node.args) >= 2 else None
    if mode is None:
        for keyword in node.keywords:
            if keyword.arg == "mode":
                mode = keyword.value
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and any(ch in mode.value for ch in "wax+")


def _python_files(path: Path) -> Iterable[Path]:
    for current, dirs, files in os.walk(path):
        dirs[:] = [item for item in dirs if item not in JUNK_DIRS and item not in ("tests", "test")]
        for name in files:
            if name.endswith(".py"):
                yield Path(current) / name


# --------------------------------------------------------------------------------------
# 命令：check
# --------------------------------------------------------------------------------------

async def run_check(
    path: Path,
    sdk_version: str = DEFAULT_SDK_VERSION,
    overrides: dict[str, Any] | None = None,
    workspace: Path | None = None,
) -> Report:
    report = Report(path=path)
    if not path.is_dir():
        _error(report, f"目录不存在：{path}")
        return report
    report.manifest = read_manifest(path, report)
    validate_manifest(path, report.manifest, sdk_version, report)
    await load(path, report, overrides, workspace)
    validate_registrations(report.manifest, report)
    static_scan(path, report.manifest, report)
    return report


def render_report(report: Report) -> str:
    manifest = report.manifest or {}
    lines: list[str] = []
    title = f"{manifest.get('id', '?')}  {manifest.get('version', '?')}"
    lines.append(f"{title}   apiVersion {manifest.get('apiVersion', '1')}")
    lines.append(f"  目录 {report.path}")
    lines.append(f"  入口 {manifest.get('entry', 'main.py')}")

    api = report.api
    if api is not None:
        lines.append(f"  工具 {len(api.tools)} · 命令 {len(api.commands)} · 事件 {len(api.handlers)} · flag {len(api.flags)}")
        for name, tool in api.tools.items():
            params = sorted(((tool.parameters or {}).get("properties") or {}).keys())
            detail = f"({', '.join(params)})" if params else "()"
            lines.append(f"    - {name}{detail}")
        for name in api.commands:
            lines.append(f"    / {name}")
    else:
        lines.append("  未能加载：没有注册物")

    if report.config:
        masked = {
            key: ("***" if _is_secret(report.manifest, key) else value)
            for key, value in report.config.items()
        }
        lines.append(f"  配置 {json.dumps(masked, ensure_ascii=False)}")

    permissions = manifest.get("permissions") or {}
    if permissions:
        lines.append(
            "  权限 "
            f"network={permissions.get('network', False)} "
            f"filesystem={permissions.get('filesystem', 'none')} "
            f"process={permissions.get('process', False)}"
        )

    errors = report.errors
    warnings = report.warnings
    notes = [item for item in report.findings if item.level == "note"]
    if errors:
        lines.append("  错误")
        lines.extend(item.render() for item in errors)
    if warnings:
        lines.append("  警告")
        lines.extend(item.render() for item in warnings)
    if notes:
        lines.append("  提示")
        lines.extend(item.render() for item in notes)
    if not errors:
        lines.append(f"  ✓ 通过：{len(warnings)} 个警告，{len(notes)} 个提示")
    else:
        lines.append(f"  ✗ 失败：{len(errors)} 个错误")
    return "\n".join(lines)


def _is_secret(manifest: dict[str, Any], key: str) -> bool:
    spec = ((manifest.get("config") or {}).get("properties") or {}).get(key)
    return bool(isinstance(spec, dict) and spec.get("secret"))


def report_json(report: Report) -> dict[str, Any]:
    return {
        "path": str(report.path),
        "manifest": report.manifest,
        "config": {key: ("***" if _is_secret(report.manifest, key) else value) for key, value in report.config.items()},
        "tools": [
            {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
            for tool in (report.api.tools.values() if report.api else [])
        ],
        "commands": list(report.api.commands) if report.api else [],
        "events": list(report.api.handlers) if report.api else [],
        "findings": [{"level": item.level, "message": item.message, "where": item.where} for item in report.findings],
        "ok": report.ok,
    }


# --------------------------------------------------------------------------------------
# 命令：dev
# --------------------------------------------------------------------------------------

async def run_dev(
    path: Path,
    tool_name: str | None,
    args: dict[str, Any],
    overrides: dict[str, Any] | None,
    sdk_version: str,
    force: bool,
    workspace: Path | None = None,
) -> int:
    report = await run_check(path, sdk_version, overrides, workspace)
    print(render_report(report))
    if not report.ok and not force:
        print("\n校验未通过，先修错误（或加 --force 强跑）。")
        return 1
    if report.api is None:
        return 1
    tools = report.api.tools
    if not tools:
        print("\n这个插件没有注册任何工具。")
        return 1
    if tool_name is None:
        if len(tools) != 1:
            print(f"\n有 {len(tools)} 个工具，用 --tool 指定：{', '.join(tools)}")
            return 2
        tool_name = next(iter(tools))
    if tool_name not in tools:
        print(f"\n没有名为 {tool_name!r} 的工具，可用：{', '.join(tools)}")
        return 2

    tool = tools[tool_name]
    limits = report.manifest.get("limits") or {}
    timeout = float(limits.get("timeoutSeconds") or 30)
    max_chars = int(limits.get("maxResultChars") or 20000)

    print(f"\n运行 {tool_name}  args={json.dumps(args, ensure_ascii=False)}")
    started = time.monotonic()
    try:
        result = await asyncio.wait_for(_invoke(tool.execute, args), timeout=timeout)
    except asyncio.TimeoutError:
        print(f"✗ 超时（{timeout:g}s）。给工具加超时，或调大 limits.timeoutSeconds。")
        return 1
    except Exception as error:  # noqa: BLE001
        print(f"✗ 抛出异常：{type(error).__name__}: {error}")
        return 1
    elapsed = (time.monotonic() - started) * 1000
    text = result_text(result)
    print(f"✓ 返回（{elapsed:.0f} ms，{len(text)} 字符）")
    print("-" * 60)
    print(text[:max_chars] + ("\n…（已截断）" if len(text) > max_chars else ""))
    print("-" * 60)
    return 0


async def _invoke(function: Any, args: dict[str, Any]) -> Any:
    arity = _arity(function)
    if arity >= 4:
        result = function("dev-call", args, AbortSignalStub(), lambda _update: None)
    elif arity == 2:
        result = function(args, AbortSignalStub())
    elif arity == 1:
        result = function(args)
    else:
        result = function()
    if inspect.isawaitable(result):
        result = await result
    return result


def _arity(function: Any) -> int:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return 1
    count = 0
    for parameter in signature.parameters.values():
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            count += 1
        elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return 4
    return count


def result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text", "")))
            else:
                parts.append(str(getattr(block, "text", "") or ""))
        text = "\n".join(part for part in parts if part)
        if text:
            return text
    details = getattr(result, "details", None)
    if details is not None:
        try:
            return json.dumps(details, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(details)
    return str(result)


# --------------------------------------------------------------------------------------
# 命令：new
# --------------------------------------------------------------------------------------

def run_new(
    plugin_id: str,
    name: str | None,
    description: str,
    template: str,
    directory: Path,
    force: bool,
) -> int:
    if not ID_RE.match(plugin_id):
        print(f"✗ id {plugin_id!r} 不合法：小写字母/数字/连字符，≤41 字符")
        return 2
    templates = Path(__file__).resolve().parent / "templates"
    source = templates / template
    if not source.is_dir():
        print(f"✗ 没有模板 {template!r}，可用：{', '.join(sorted(p.name for p in templates.iterdir() if p.is_dir()))}")
        return 2
    target = (directory / plugin_id).resolve()
    if target.exists() and any(target.iterdir()):
        if not force:
            print(f"✗ {target} 已存在且非空（加 --force 覆盖）")
            return 2
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)

    display = name or plugin_id.replace("-", " ").title()
    replacements = {
        "__PLUGIN_ID__": plugin_id,
        "__PLUGIN_NAME__": display,
        "__PLUGIN_DESC__": description,
        "__PLUGIN_ENV__": plugin_id.upper().replace("-", "_") + "_API_KEY",
    }
    for path in target.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".md", ".json", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for token, value in replacements.items():
            text = text.replace(token, value)
        path.write_text(text, encoding="utf-8")

    manifest_path = target / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["id"] = plugin_id
    manifest["name"] = display
    manifest["description"] = description
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    kit = _display_path(Path(__file__).resolve())
    print(f"✓ 已创建 {target}")
    print("\n下一步：")
    print(f"  python3 {kit} check {_display_path(target)}")
    print(f"  python3 {kit} dev {_display_path(target)} --args '{{}}'")
    print(f"  python3 {kit} pack {_display_path(target)}")
    return 0


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------------------
# 命令：pack
# --------------------------------------------------------------------------------------

async def run_pack(path: Path, output: Path | None, force: bool, sdk_version: str) -> int:
    report = await run_check(path, sdk_version)
    print(render_report(report))
    if not report.ok and not force:
        print("\n校验未通过，不打包（或加 --force）。")
        return 1
    plugin_id = str(report.manifest.get("id") or path.name)
    destination = output or (Path.cwd() / f"{plugin_id}.zip")

    files: list[Path] = []
    total = 0
    for current, dirs, names in os.walk(path):
        dirs[:] = sorted(item for item in dirs if item not in JUNK_DIRS)
        for name in sorted(names):
            if name in JUNK_FILES or name.endswith((".pyc", ".pyo", ".zip")):
                continue
            file = Path(current) / name
            if file.is_symlink():
                print(f"✗ 拒绝打包符号链接：{file}")
                return 1
            files.append(file)
            total += file.stat().st_size
    if not files:
        print("✗ 目录里没有可打包的文件")
        return 1
    if total > MAX_ZIP_BYTES:
        print(f"✗ 解压后 {total} 字节，超过宿主 20MB 上限")
        return 1

    if destination.exists() and not force:
        print(f"✗ {destination} 已存在（加 --force 覆盖）")
        return 2
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        # 宿主要求 zip 内恰好有一个目录，所以固定包一层 <id>/
        for file in files:
            arcname = f"{plugin_id}/{file.relative_to(path).as_posix()}"
            archive.write(file, arcname)

    print(f"\n✓ 已打包 {destination}（{destination.stat().st_size} 字节，{len(files)} 个文件）")
    print("  在「插件 → 安装插件」上传这个 zip；结构：")
    for file in files[:6]:
        print(f"    {plugin_id}/{file.relative_to(path).as_posix()}")
    if len(files) > 6:
        print(f"    … 共 {len(files)} 个")
    return 0


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pikit", description="python-pi 插件开发工具")
    parser.add_argument("--sdk-version", default=DEFAULT_SDK_VERSION, help="用于校验 requires.pi 的 SDK 版本")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="从模板创建一个插件")
    new.add_argument("plugin_id")
    new.add_argument("--dir", default=".", help="父目录（默认当前目录）")
    new.add_argument("--name", default=None, help="显示名（默认由 id 推导）")
    new.add_argument("--desc", default="", help="一句话描述")
    new.add_argument("--template", default="basic", choices=["basic", "http"], help="模板")
    new.add_argument("--force", action="store_true", help="覆盖已存在的目录")

    check = sub.add_parser("check", help="校验插件并在本地执行 setup()")
    check.add_argument("path", nargs="?", default=".")
    check.add_argument("--json", action="store_true", help="输出机器可读结果")
    check.add_argument("--workspace", default=None, help="插件看到的工作区（默认当前目录）")

    dev = sub.add_parser("dev", help="试跑一个工具")
    dev.add_argument("path", nargs="?", default=".")
    dev.add_argument("--tool", default=None)
    dev.add_argument("--args", default="{}", help="JSON 参数")
    dev.add_argument("--config", default=None, help="覆盖配置的 JSON")
    dev.add_argument("--workspace", default=None, help="插件看到的工作区（默认当前目录）")
    dev.add_argument("--force", action="store_true", help="校验失败也继续")

    pack = sub.add_parser("pack", help="校验并打包成可安装的 zip")
    pack.add_argument("path", nargs="?", default=".")
    pack.add_argument("--out", default=None)
    pack.add_argument("--force", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "new":
            return run_new(
                args.plugin_id,
                args.name,
                args.desc,
                args.template,
                Path(args.dir),
                args.force,
            )
        if args.command == "check":
            workspace = Path(args.workspace).resolve() if args.workspace else None
            report = asyncio.run(run_check(Path(args.path).resolve(), args.sdk_version, workspace=workspace))
            if args.json:
                print(json.dumps(report_json(report), ensure_ascii=False, indent=2))
            else:
                print(render_report(report))
            return 0 if report.ok else 1
        if args.command == "dev":
            payload = json.loads(args.args)
            if not isinstance(payload, dict):
                print("✗ --args 必须是 JSON 对象")
                return 2
            overrides = json.loads(args.config) if args.config else None
            return asyncio.run(
                run_dev(
                    Path(args.path).resolve(),
                    args.tool,
                    payload,
                    overrides,
                    args.sdk_version,
                    args.force,
                    Path(args.workspace).resolve() if args.workspace else None,
                )
            )
        if args.command == "pack":
            return asyncio.run(
                run_pack(
                    Path(args.path).resolve(),
                    Path(args.out).resolve() if args.out else None,
                    args.force,
                    args.sdk_version,
                )
            )
    except json.JSONDecodeError as error:
        print(f"✗ JSON 解析失败：{error}")
        return 2
    except KeyboardInterrupt:
        return 130
    return 2


if __name__ == "__main__":
    sys.exit(main())
