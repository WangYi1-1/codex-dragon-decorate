"""Local UI bridge to the installed Codex app-server. No credentials are read here."""
from __future__ import annotations

import argparse
import base64
import collections
import json
import math
import mimetypes
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
SCHEMAS = ROOT / "protocol" / "schema"
# Local client limits. Three 20 MiB images use about 80 MiB after base64;
# leave room for text and the JSON envelope. These are not Codex model limits.
MAX_BODY = 85 * 1024 * 1024
MAX_IMAGE = 20 * 1024 * 1024
MAX_ATTACHMENT = 5 * 1024 * 1024
MAX_ATTACHMENT_STORE = 256 * 1024 * 1024
MAX_STORED_ATTACHMENTS = 512
ATTACHMENT_ID = re.compile(r"^[a-f0-9]{32}$")
SENSITIVE_KEYS = re.compile(r"^(?:accessToken|refreshToken|idToken|apiKey|bearerToken|authorization|clientSecret|password|authJson|encrypted_content)$", re.I)

# These fields deliberately exclude config overrides, sandbox/approval overrides,
# raw filesystem APIs, alternate rollout paths, and dynamic tool definitions.
RPC = {
    "account/read": ("v2/GetAccountParams.json", {"refreshToken"}),
    "account/rateLimits/read": ("v2/NullableGetAccountRateLimitsParams.json", set()),
    "model/list": ("v2/ModelListParams.json", {"cursor", "limit", "includeHidden"}),
    "thread/list": ("v2/ThreadListParams.json", {"cursor", "limit", "sortKey", "sortDirection", "modelProviders", "sourceKinds", "archived", "searchTerm", "cwd", "projectId", "useStateDbOnly"}),
    "thread/read": ("v2/ThreadReadParams.json", {"threadId", "includeTurns"}),
    "thread/turns/list": ("v2/ThreadTurnsListParams.json", {"threadId", "cursor", "limit", "sortDirection", "itemsView"}),
    "thread/items/list": ("v2/ThreadItemsListParams.json", {"threadId", "turnId", "cursor", "limit", "sortDirection"}),
    "thread/start": ("v2/ThreadStartParams.json", {"cwd", "model", "projectId"}),
    "thread/resume": ("v2/ThreadResumeParams.json", {"threadId", "excludeTurns", "model"}),
    "thread/archive": ("v2/ThreadArchiveParams.json", {"threadId"}),
    "turn/start": ("v2/TurnStartParams.json", {"threadId", "input", "model", "effort"}),
    "turn/interrupt": ("v2/TurnInterruptParams.json", {"threadId", "turnId"}),
}
SERVER_RESPONSES = {
    "item/commandExecution/requestApproval": "CommandExecutionRequestApprovalResponse.json",
    "item/fileChange/requestApproval": "FileChangeRequestApprovalResponse.json",
    "item/permissions/requestApproval": "PermissionsRequestApprovalResponse.json",
    "item/tool/requestUserInput": "ToolRequestUserInputResponse.json",
    "mcpServer/elicitation/request": "McpServerElicitationRequestResponse.json",
    "execCommandApproval": "ExecCommandApprovalResponse.json",
    "applyPatchApproval": "ApplyPatchApprovalResponse.json",
}


class BridgeError(Exception):
    def __init__(self, message, code=-32000, kind=None):
        super().__init__(message)
        self.code = code
        self.kind = kind


def attachment_name(value):
    """A browser filename is a label, never a client-provided filesystem path."""
    if not isinstance(value, str) or not value.strip() or len(value) > 255 or "/" in value or "\\" in value:
        raise BridgeError("请提供有效的附件文件名。", -32602)
    name = re.sub(r'[<>:"|?*\x00-\x1f\x7f]', "_", value).strip().rstrip(". ")
    if not name or name in (".", ".."):
        raise BridgeError("请提供有效的附件文件名。", -32602)
    stem = name.split(".", 1)[0].rstrip(". ")
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", stem, re.I):
        name = "_" + name
    # Keep the extension while staying below Windows' legacy path-length limit.
    if len(name) > 120:
        suffix = Path(name).suffix
        suffix = suffix if len(suffix) <= 20 else ""
        name = name[:120 - len(suffix)].rstrip(". ") + suffix
    return name


class AttachmentStore:
    """Private bounded uploads; only opaque IDs can be used by the browser."""
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else ROOT / ".runtime" / "attachments"
        self.lock = threading.Lock()

    def _root(self, create=False):
        if self._link(self.root):
            raise OSError("Attachment directory is a link")
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        return self.root.resolve()

    @staticmethod
    def _link(path):
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())

    def upload(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"name", "data"}:
            raise BridgeError("附件需要文件名和 base64 数据。", -32602)
        name = attachment_name(payload["name"])
        encoded = payload["data"]
        if not isinstance(encoded, str) or not encoded or len(encoded) > (MAX_ATTACHMENT + 2) // 3 * 4:
            raise BridgeError("文件不能为空，且不能超过 5 MiB。", -32602)
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeError):
            raise BridgeError("附件数据编码无效。", -32602)
        if not content or len(content) > MAX_ATTACHMENT:
            raise BridgeError("文件不能为空，且不能超过 5 MiB。", -32602)
        directory = None
        try:
            with self.lock:
                root = self._root(create=True)
                entries = [item for item in root.iterdir() if item.is_dir() and not self._link(item)]
                used = sum(file.stat().st_size for item in entries for file in item.iterdir() if file.is_file() and not self._link(file))
                if len(entries) >= MAX_STORED_ATTACHMENTS or used + len(content) + 1024 > MAX_ATTACHMENT_STORE:
                    raise BridgeError("本地附件存储已满，请整理 .runtime/attachments 后再添加文件。", -32000, "attachment_storage_full")
                ident = uuid.uuid4().hex
                candidate = root / ident
                candidate.mkdir()
                directory = candidate
                target = directory / ("file-" + name)
                with target.open("xb") as output:
                    output.write(content)
                metadata = {"version": 1, "id": ident, "name": name, "size": len(content)}
                with (directory / "metadata.json").open("x", encoding="utf-8") as output:
                    json.dump(metadata, output, ensure_ascii=False, allow_nan=False)
                return {key: metadata[key] for key in ("id", "name", "size")}
        except BridgeError:
            raise
        except OSError:
            if directory is not None:
                # Delete only the exact new upload directory and known direct files.
                for child in (directory / ("file-" + name), directory / "metadata.json"):
                    try:
                        child.unlink(missing_ok=True)
                    except OSError:
                        pass
                try:
                    directory.rmdir()
                except OSError:
                    pass
            raise BridgeError("附件无法保存，请检查项目目录的写入权限和剩余空间。", -32000, "attachment_storage_error")

    def native_input(self, item):
        if not isinstance(item, dict) or set(item) != {"type", "id"} or item.get("type") != "attachment" or not isinstance(item.get("id"), str) or not ATTACHMENT_ID.fullmatch(item["id"]):
            raise BridgeError("附件标识无效，请重新添加文件。", -32602)
        try:
            root = self._root()
            directory = root / item["id"]
            metadata_path = directory / "metadata.json"
            if self._link(directory) or directory.resolve() != directory or self._link(metadata_path) or metadata_path.stat().st_size > 2048:
                raise ValueError("Invalid upload directory")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict) or set(metadata) != {"version", "id", "name", "size"} or metadata["version"] != 1 or metadata["id"] != item["id"]:
                raise ValueError("Invalid upload metadata")
            name = attachment_name(metadata["name"])
            size = metadata["size"]
            target = directory / ("file-" + name)
            if name != metadata["name"] or not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_ATTACHMENT or self._link(target) or target.resolve().parent != directory or not target.is_file() or target.stat().st_size != size:
                raise ValueError("Invalid upload file")
            reference = json.dumps({"name": name, "path": target.as_posix()}, ensure_ascii=False)
            # Native 'mention' is not documented as a file attachment. A local
            # file reference lets the agent read this upload through its tools.
            return {"type": "text", "text": f"[用户附件] {reference}\n请根据我的消息读取该本地附件。"}
        except BridgeError:
            raise
        except (OSError, ValueError, UnicodeError, KeyError, TypeError):
            raise BridgeError("附件已不存在或无法读取，请重新添加文件。", -32602, "attachment_missing")


def native_bridge_error(error):
    message = str(error.get("message", "Codex request failed"))
    code = error.get("code", -32000)
    if re.search(r"\balready has an active writer\b", message, re.I):
        return BridgeError(
            "这段会话正在另一个 Codex 窗口中使用。请在原窗口继续，或点击「另开新会话」后再发送。",
            code, "thread_in_use",
        )
    return BridgeError(message, code)


def redact(value):
    if isinstance(value, dict):
        return {key: ("[redacted]" if SENSITIVE_KEYS.match(key) else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def account_summary(value):
    account = value.get("account")
    return {
        "account": ({key: account[key] for key in ("type", "planType") if key in account} if isinstance(account, dict) else None),
        "requiresOpenaiAuth": bool(value.get("requiresOpenaiAuth", True)),
    }


def validate(value, schema, document=None, path="params"):
    """Validate the JSON Schema vocabulary used by our generated protocol subset."""
    if schema is True:
        return
    if schema is False:
        raise BridgeError(f"{path}: value is not allowed", -32602)
    document = document or schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise BridgeError("Unsupported protocol reference", -32602)
        target = document
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        validate(value, target, document, path)
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            matches = 0
            for option in schema[keyword]:
                try:
                    validate(value, option, document, path)
                    matches += 1
                except BridgeError:
                    pass
            if matches == 0 or (keyword == "oneOf" and matches != 1):
                raise BridgeError(f"{path}: invalid value", -32602)
    for option in schema.get("allOf", []):
        validate(value, option, document, path)
    kind = schema.get("type")
    if kind:
        kinds = [kind] if isinstance(kind, str) else kind
        tests = {
            "null": value is None,
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        }
        if not any(tests.get(item, False) for item in kinds):
            raise BridgeError(f"{path}: incorrect type", -32602)
    if "enum" in schema and value not in schema["enum"]:
        raise BridgeError(f"{path}: unsupported choice", -32602)
    if "const" in schema and value != schema["const"]:
        raise BridgeError(f"{path}: unsupported value", -32602)
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise BridgeError(f"{path}.{key}: required", -32602)
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                validate(item, properties[key], document, f"{path}.{key}")
            elif extra is False:
                raise BridgeError(f"{path}.{key}: not allowed", -32602)
            elif isinstance(extra, dict):
                validate(item, extra, document, f"{path}.{key}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise BridgeError(f"{path}: too few entries", -32602)
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise BridgeError(f"{path}: too many entries", -32602)
        for index, item in enumerate(value):
            validate(item, schema.get("items", True), document, f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", MAX_BODY):
            raise BridgeError(f"{path}: invalid length", -32602)
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise BridgeError(f"{path}: invalid format", -32602)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise BridgeError(f"{path}: non-finite numbers are not allowed", -32602)
        if ("minimum" in schema and value < schema["minimum"]) or ("maximum" in schema and value > schema["maximum"]):
            raise BridgeError(f"{path}: out of range", -32602)


def load_schema(relative):
    return json.loads((SCHEMAS / relative).read_text(encoding="utf-8"))


def discover_codex():
    command = "$p = Get-AppxPackage -Name OpenAI.Codex | Select-Object -First 1; if ($p) { [Console]::Write((Join-Path $p.InstallLocation 'app\\resources\\codex.exe')) }"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=20, creationflags=flags)
    target = Path(result.stdout.strip())
    if result.returncode or not target.is_file():
        raise BridgeError("找不到已安装的 Codex 程序，请重新启动官方应用后再打开这个界面。")
    return target


def request_key(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise BridgeError("Invalid server request id", -32602)
    return (type(value).__name__, value)


def requested_grant(granted, requested):
    """The client can approve a request or a subset, never invent permissions."""
    if granted is None or granted == {} or granted == []:
        return True
    if isinstance(granted, dict):
        return isinstance(requested, dict) and all(
            key in requested and requested_grant(value, requested[key])
            for key, value in granted.items()
        )
    if isinstance(granted, list):
        return isinstance(requested, list) and all(item in requested for item in granted)
    if granted is False and isinstance(requested, bool):
        return True
    return type(granted) is type(requested) and granted == requested


class CodexBridge:
    def __init__(self, executable, cwd):
        self.executable = executable
        self.instance_id = uuid.uuid4().hex
        self.cwd = cwd
        self.process = None
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.starting = False
        self.pending = {}
        self.server_requests = {}
        self.responding = set()
        self.owned_threads = set()
        self.clients = set()
        self.history = collections.deque(maxlen=1024)
        self.sequence = 0
        self.next_request = 1
        self.ready = False
        self.closed = False
        self.error = None
        self.account = {"account": None, "requiresOpenaiAuth": True}
        self.protocol = {method: load_schema(info[0]) for method, info in RPC.items()}
        self.response_schemas = {method: load_schema(path) for method, path in SERVER_RESPONSES.items()}
        self.attachments = AttachmentStore()

    def start(self):
        # Initialization is serialized separately from the protocol reader. A
        # reconnect must never replace a still-running task or pending approval.
        with self.start_lock:
            with self.lock:
                if self.closed or (self.process and self.process.poll() is None):
                    return self.status()
                self.starting = True
                self.ready = False
                self.error = None
                previous = self.process
                with self.write_lock:
                    self.process = None
                waiters = list(self.pending.values())
                self.pending.clear()
                self.server_requests.clear()
                self.responding.clear()
                self.owned_threads.clear()
            for waiter in waiters:
                try:
                    waiter.put_nowait({"error": {"code": -32002, "message": "Codex 连接已断开，请重试连接。"}})
                except queue.Full:
                    pass
            if previous and previous.stdin:
                try:
                    previous.stdin.close()
                except OSError:
                    pass
            try:
                self._start_process()
            finally:
                with self.lock:
                    self.starting = False
            return self.status()

    def reconnect(self):
        with self.lock:
            if self.closed:
                raise BridgeError("本地服务已关闭，请重新打开龙娘界面。", -32002)
            if self.starting or (self.process and self.process.poll() is None):
                return self.status()
            self.starting = True
            self.ready = False
            self.error = None
            try:
                threading.Thread(target=self.start, name="codex-reconnect", daemon=True).start()
            except Exception:
                self.starting = False
                self.error = "Codex 连接启动失败，请重新打开龙娘界面。"
                raise BridgeError(self.error, -32002)
            return self.status()

    def _start_process(self):
        process = None
        established = False
        try:
            with self.lock:
                if self.closed:
                    return
                with self.write_lock:
                    process = subprocess.Popen([str(self.executable), "app-server", "--listen", "stdio://"], cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    self.process = process
            threading.Thread(target=self._read_stdout, args=(process,), name="codex-protocol", daemon=True).start()
            threading.Thread(target=self._drain_stderr, args=(process,), name="codex-stderr", daemon=True).start()
            self._request("initialize", {"clientInfo": {"name": "codex_dragon_skin", "title": "Codex 龙娘界面", "version": "1.0.0"}, "capabilities": {"experimentalApi": True}}, 30, process=process)
            self._write({"method": "initialized", "params": {}}, process=process)
            with self.lock:
                if self.closed or self.process is not process or process.poll() is not None:
                    raise BridgeError("Codex 连接已断开，请重试连接。", -32002)
                self.ready = True
                established = True
            self.publish({"type": "connection", **self.status()})
            response = self._request("account/read", {"refreshToken": False}, 30, process=process)
            with self.lock:
                if self.closed or self.process is not process or process.poll() is not None:
                    raise BridgeError("Codex 连接已断开，请重试连接。", -32002)
                self.account = account_summary(response)
            self.publish({"type": "connection", **self.status()})
        except Exception as exc:
            failed_start = None
            with self.lock:
                if self.process is process and not self.closed:
                    if established and self.ready and process.poll() is None and not isinstance(exc, OSError):
                        # Account metadata is optional once the stdio handshake
                        # succeeded. Preserve any task/approval on this transport.
                        self.account = {"account": None, "requiresOpenaiAuth": True}
                        self.error = None
                    else:
                        self.ready = False
                        self.error = str(exc) if isinstance(exc, BridgeError) else "Codex 连接启动失败，请重新打开龙娘界面。"
                        if not established and process and not self.owned_threads and not self.server_requests and not self.responding:
                            # Only retire our own failed initialization. Detach it
                            # before cleanup so a late reader cannot clear state.
                            with self.write_lock:
                                self.process = None
                            failed_start = process
            if failed_start:
                self._stop_process(failed_start)
            self.publish({"type": "connection", **self.status()})

    def status(self):
        with self.lock:
            return {"appId": "codex-dragon-client", "version": 1, "instanceId": self.instance_id, "connection": "ready" if self.ready else ("error" if self.error else "connecting"), "connected": self.ready, "defaultCwd": self.cwd, "account": self.account, "error": self.error, "pendingRequests": list(self.server_requests.values()), "ownedThreadIds": sorted(self.owned_threads)}

    def publish(self, message):
        message = redact(message)
        with self.lock:
            self.sequence += 1
            event = (self.sequence, message)
            self.history.append(event)
            for subscriber in tuple(self.clients):
                try:
                    subscriber.put_nowait(event)
                except queue.Full:
                    self.clients.discard(subscriber)
                    while not subscriber.empty():
                        try:
                            subscriber.get_nowait()
                        except queue.Empty:
                            break
                    subscriber.put_nowait(None)

    def subscribe(self, after):
        subscriber = queue.Queue(maxsize=2048)
        with self.lock:
            replay = [event for event in self.history if after is not None and event[0] > after]
            self.clients.add(subscriber)
            snapshot = {"type": "connection", **self.status()}
            return subscriber, replay, self.sequence, snapshot

    def unsubscribe(self, subscriber):
        with self.lock:
            self.clients.discard(subscriber)

    def _write(self, message, process=None):
        with self.write_lock:
            target = process or self.process
            if self.closed or not target or target is not self.process or target.poll() is not None:
                raise BridgeError("Codex 连接已断开，请重试连接。", -32002)
            target.stdin.write(json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n")
            target.stdin.flush()

    def _request(self, method, params, timeout=60, process=None):
        waiter = queue.Queue(maxsize=1)
        with self.lock:
            target = process or self.process
            ident = self.next_request
            self.next_request += 1
            self.pending[ident] = waiter
        try:
            self._write({"id": ident, "method": method, "params": params}, process=target)
            try:
                reply = waiter.get(timeout=timeout)
            except queue.Empty:
                raise BridgeError("Codex 响应超时，请稍后重试。", -32001)
            if "error" in reply:
                raise native_bridge_error(reply["error"])
            return reply.get("result")
        finally:
            with self.lock:
                self.pending.pop(ident, None)

    def rpc(self, method, params):
        if method not in RPC:
            raise BridgeError("此界面没有开放这个操作。", -32601)
        if not isinstance(params, dict):
            raise BridgeError("params must be an object", -32602)
        unknown = set(params) - RPC[method][1]
        if unknown:
            raise BridgeError("Unsupported parameters: " + ", ".join(sorted(unknown)), -32602)
        params = dict(params)
        if method == "turn/start":
            if not isinstance(params.get("input"), list) or not params["input"] or len(params["input"]) > 32:
                raise BridgeError("请输入消息。", -32602)
            image_count = 0
            attachment_count = 0
            native_input = []
            for item in params["input"]:
                if not isinstance(item, dict):
                    raise BridgeError("消息输入无效。", -32602)
                if item.get("type") == "text":
                    if set(item) - {"type", "text", "text_elements"}:
                        raise BridgeError("Unsupported text fields", -32602)
                elif item.get("type") == "image":
                    image_count += 1
                    attachment_count += 1
                    if image_count > 3 or set(item) - {"type", "url"}:
                        raise BridgeError("最多添加三张图片。", -32602)
                    url = item.get("url", "")
                    match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", url) if isinstance(url, str) else None
                    if not match or len(match[2]) > (MAX_IMAGE + 2) // 3 * 4:
                        raise BridgeError("请添加不超过 20 MiB 的 PNG、JPEG 或 WebP 图片。", -32602)
                    try:
                        image = base64.b64decode(match[2], validate=True)
                    except ValueError:
                        raise BridgeError("Invalid image encoding", -32602)
                    if not image or len(image) > MAX_IMAGE:
                        raise BridgeError("图片不能为空，且不能超过 20 MiB。", -32602)
                elif item.get("type") == "attachment":
                    attachment_count += 1
                    item = self.attachments.native_input(item)
                else:
                    raise BridgeError("只开放文字、图片和本界面上传的附件。", -32602)
                if attachment_count > 10:
                    raise BridgeError("每次最多添加十个附件，其中最多三张图片。", -32602)
                native_input.append(item)
            params["input"] = native_input
        validate(params, self.protocol[method])
        if "limit" in params and params["limit"] is not None and not 1 <= params["limit"] <= 100:
            raise BridgeError("limit must be between 1 and 100", -32602)
        if method == "account/read":
            params["refreshToken"] = False
        with self.lock:
            if not self.ready:
                raise BridgeError(self.error or "Codex 正在连接，请稍候。", -32002)
            process = self.process
        result = self._request(method, params, process=process)
        with self.lock:
            if self.process is not process or not self.ready:
                raise BridgeError("Codex 连接已变化，请重试连接。", -32002)
            if method in ("thread/start", "thread/resume"):
                thread = result.get("thread", {}) if isinstance(result, dict) else {}
                thread_id = thread.get("id") or (params.get("threadId") if method == "thread/resume" else None)
                if isinstance(thread_id, str):
                    self.owned_threads.add(thread_id)
            elif method == "thread/archive":
                self.owned_threads.discard(params["threadId"])
            if method == "account/read":
                result = account_summary(result)
                self.account = result
        return redact(result)

    def respond(self, ident, result):
        key = request_key(ident)
        with self.lock:
            request = self.server_requests.get(key)
            process = self.process
        if not request:
            raise BridgeError("这个确认已经结束。", -32602)
        method = request["method"]
        schema = self.response_schemas[method]
        validate(result, schema, path="result")
        if not isinstance(result, dict) or set(result) - set(schema.get("properties", {})):
            raise BridgeError("Unsupported approval response fields", -32602)
        params = request["params"]
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            # Persistent execution/network policy amendments are not exposed by
            # this lightweight client. The official client can manage those rules.
            if result.get("decision") not in ("accept", "acceptForSession", "decline", "cancel"):
                raise BridgeError("Unsupported approval decision", -32602)
            choices = params.get("availableDecisions")
            if choices and result["decision"] not in choices:
                raise BridgeError("This decision was not offered by Codex", -32602)
        if method in ("execCommandApproval", "applyPatchApproval"):
            decision = result.get("decision")
            if isinstance(decision, dict):
                if set(decision) != {"denied"} or set(decision["denied"]) != {"rejection"}:
                    raise BridgeError("Persistent policy amendments are not available", -32602)
            elif decision not in ("approved", "approved_for_session", "abort"):
                raise BridgeError("Unsupported approval decision", -32602)
        if method == "item/tool/requestUserInput":
            requested = {item["id"] for item in params.get("questions", [])}
            if set(result["answers"]) - requested:
                raise BridgeError("Unknown question id", -32602)
            if any(set(answer) != {"answers"} for answer in result["answers"].values()):
                raise BridgeError("Unsupported answer fields", -32602)
        if method == "item/permissions/requestApproval":
            # Native Codex additionally intersects any grant with the request.
            # Never infer a grant or persist policy changes without this UI reply.
            if result.get("scope", "turn") not in ("turn", "session"):
                raise BridgeError("Unsupported permission scope", -32602)
            if set(result) - {"permissions", "scope"}:
                raise BridgeError("Permission policy overrides are not available", -32602)
            if not requested_grant(result["permissions"], params.get("permissions", {})):
                raise BridgeError("Only requested permissions may be granted", -32602)
        if method == "mcpServer/elicitation/request" and result["action"] == "accept":
            if params.get("mode") in ("form", "openai/form") and isinstance(params.get("requestedSchema"), dict):
                validate(result.get("content"), params["requestedSchema"], path="content")
        with self.lock:
            if self.process is not process or self.server_requests.get(key) is not request or key in self.responding:
                raise BridgeError("这个确认已经结束或正在提交。", -32602)
            self.responding.add(key)
        try:
            self._write({"id": ident, "result": result}, process=process)
            with self.lock:
                if self.process is process and self.server_requests.get(key) is request:
                    self.server_requests.pop(key, None)
        finally:
            with self.lock:
                if self.process is process:
                    self.responding.discard(key)
        self.publish({"type": "approval_submitted", "id": ident})
        return {"accepted": True}

    def _read_stdout(self, process):
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(message, dict):
                    continue
                with self.lock:
                    if self.process is not process:
                        return
                    if "method" not in message and "id" in message:
                        waiter = self.pending.get(message["id"])
                        if waiter:
                            try:
                                waiter.put_nowait(message)
                            except queue.Full:
                                pass
                    elif "method" in message and "id" in message:
                        self._server_request(message, process=process)
                    elif "method" in message:
                        if message["method"] in ("thread/closed", "thread/archived"):
                            self.owned_threads.discard(message.get("params", {}).get("threadId"))
                        if message["method"] == "serverRequest/resolved":
                            ident = message.get("params", {}).get("requestId")
                            if ident is not None:
                                self.server_requests.pop(request_key(ident), None)
                        self.publish({"type": "notification", **message})
        except Exception:
            pass
        finally:
            with self.lock:
                if self.process is not process:
                    return
                self.ready = False
                self.owned_threads.clear()
                self.server_requests.clear()
                self.responding.clear()
                if not self.closed:
                    self.error = "Codex 连接已断开，请重试连接。"
                waiters = list(self.pending.values())
                self.pending.clear()
            for waiter in waiters:
                try:
                    waiter.put_nowait({"error": {"code": -32000, "message": self.error or "Connection closed"}})
                except queue.Full:
                    pass
            self.publish({"type": "connection", **self.status()})

    def _server_request(self, message, process=None):
        method = message["method"]
        if method == "currentTime/read":
            self._write({"id": message["id"], "result": {"currentTimeAt": int(time.time())}}, process=process)
            return
        if method not in SERVER_RESPONSES:
            self._write({"id": message["id"], "error": {"code": -32601, "message": "This client does not support this server request"}}, process=process)
            self.publish({"type": "unsupported_request", "method": method})
            return
        request = redact({"type": "server_request", "id": message["id"], "method": method, "params": message.get("params", {})})
        with self.lock:
            self.server_requests[request_key(message["id"])] = request
        self.publish(request)

    def _drain_stderr(self, process):
        # App-server diagnostics may contain private values; do not log or expose.
        while process.stderr.read(8192):
            pass

    @staticmethod
    def _stop_process(process):
        """Stop only this captured child; never discover or terminate by name."""
        if process and process.poll() is None:
            try:
                process.stdin.close()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

    def close(self):
        with self.lock:
            self.closed = True
            self.ready = False
            self.owned_threads.clear()
            process = self.process
        self._stop_process(process)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def _local(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in hosts:
            self._json(HTTPStatus.FORBIDDEN, {"error": {"message": "Local host required"}})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in hosts}:
            self._json(HTTPStatus.FORBIDDEN, {"error": {"message": "Origin not allowed"}})
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._json(HTTPStatus.FORBIDDEN, {"error": {"message": "Cross-site requests are not allowed"}})
            return False
        return True

    def _headers(self, status, content_type, length=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _json(self, status, value):
        data = json.dumps(redact(value), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(data))
        self.wfile.write(data)

    def do_GET(self):
        if not self._local():
            return
        path = urlsplit(self.path).path
        if path == "/api/status":
            self._json(HTTPStatus.OK, {**self.server.bridge.status(), "port": self.server.server_port})
        elif path == "/api/events":
            self._events()
        elif path.startswith("/api/"):
            self._json(HTTPStatus.NOT_FOUND, {"error": {"message": "Unknown endpoint"}})
        else:
            self._static(path)

    def do_POST(self):
        if not self._local():
            self.close_connection = True
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": {"message": "JSON body required"}})
            self.close_connection = True
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > MAX_BODY:
                self.close_connection = True
                raise BridgeError("发送内容超过 85 MiB，请减少附件总大小或分次发送。", -32602)
            if length <= 0 or self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                raise BridgeError("Invalid request body", -32602)
            body = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
            if not isinstance(body, dict):
                raise BridgeError("JSON object required", -32602)
            path = urlsplit(self.path).path
            if path == "/api/rpc":
                if set(body) - {"method", "params"} or not isinstance(body.get("method"), str):
                    raise BridgeError("Invalid RPC envelope", -32602)
                result = self.server.bridge.rpc(body["method"], body.get("params", {}))
            elif path == "/api/approval":
                if set(body) != {"id", "result"}:
                    raise BridgeError("Approval id and result required", -32602)
                result = self.server.bridge.respond(body["id"], body["result"])
            elif path == "/api/attachments":
                result = self.server.bridge.attachments.upload(body)
            elif path == "/api/reconnect":
                if body:
                    raise BridgeError("Reconnect expects an empty object", -32602)
                result = self.server.bridge.reconnect()
            elif path == "/api/shutdown":
                if body:
                    raise BridgeError("Shutdown expects an empty object", -32602)
                self._json(HTTPStatus.OK, {"ok": True})
                threading.Thread(target=self.server.shutdown, name="local-shutdown", daemon=True).start()
                return
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": {"message": "Unknown endpoint"}})
                return
            self._json(HTTPStatus.OK, {"result": result})
        except BridgeError as exc:
            error = {"code": exc.code, "message": str(exc)}
            if exc.kind:
                error["kind"] = exc.kind
            status = HTTPStatus.CONFLICT if exc.kind == "thread_in_use" else HTTPStatus.BAD_REQUEST
            self._json(status, {"error": error})
        except (ValueError, UnicodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": {"code": -32700, "message": "Invalid JSON"}})
            self.close_connection = True
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": {"message": "本地服务遇到错误，请重新打开界面。"}})

    def _static(self, path):
        try:
            decoded = unquote(path)
            if "\\" in decoded or "\x00" in decoded:
                raise ValueError()
            relative = decoded.lstrip("/") or "index.html"
            target = (PUBLIC / relative).resolve()
            target.relative_to(PUBLIC.resolve())
            if not target.is_file():
                self._json(HTTPStatus.NOT_FOUND, {"error": {"message": "Page not found"}})
                return
            data = target.read_bytes()
            content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
                content_type += "; charset=utf-8"
            self._headers(HTTPStatus.OK, content_type, len(data))
            self.wfile.write(data)
        except (ValueError, OSError):
            self._json(HTTPStatus.NOT_FOUND, {"error": {"message": "Page not found"}})

    def _events(self):
        try:
            marker = self.headers.get("Last-Event-ID")
            after = int(marker) if marker is not None else None
        except ValueError:
            after = None
        subscriber, replay, sequence, snapshot = self.server.bridge.subscribe(after)
        self._headers(HTTPStatus.OK, "text/event-stream; charset=utf-8")
        self.close_connection = True
        try:
            self.wfile.write(b"retry: 1500\n\n")
            for ident, message in replay:
                self._event(ident, message)
            self._event(sequence, snapshot)
            while True:
                try:
                    event = subscriber.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                if event is None:
                    break
                self._event(*event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.bridge.unsubscribe(subscriber)

    def _event(self, ident, message):
        payload = json.dumps(message, ensure_ascii=False, allow_nan=False)
        self.wfile.write(f"id: {ident}\nevent: message\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


def main():
    parser = argparse.ArgumentParser(description="Local Codex dragon skin client")
    parser.add_argument("--port", type=int, default=8741)
    parser.add_argument("--codex", type=Path)
    parser.add_argument("--cwd", type=Path, default=ROOT.parent)
    args = parser.parse_args()
    if not args.cwd.is_dir() or not args.cwd.is_absolute():
        parser.error("--cwd must be an existing absolute directory")
    executable = args.codex or discover_codex()
    if not executable.is_file():
        parser.error("Codex executable not found")
    bridge = CodexBridge(executable, str(args.cwd))
    server = LocalServer(("127.0.0.1", args.port), Handler)
    server.bridge = bridge
    threading.Thread(target=bridge.start, name="codex-initialize", daemon=True).start()
    print(f"Codex 龙娘界面：http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        bridge.close()


if __name__ == "__main__":
    main()
