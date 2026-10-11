"""Focused bridge boundary tests; they never contact a model or read user data."""
import http.client
import base64
import concurrent.futures
import io
import json
import queue
import subprocess
import sys
import threading
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

import server


class StubBridge(server.CodexBridge):
    def __init__(self):
        super().__init__(Path("unused.exe"), str(server.ROOT))
        self.ready = True
        self.sent = []

    def _request(self, method, params, timeout=60, process=None):
        self.sent.append((method, params))
        return {"ok": True}

    def _write(self, message, process=None):
        self.sent.append(message)


class FakeProcess:
    """A process-shaped value for deterministic lifecycle race checks."""
    def __init__(self, returncode=None, stdout=""):
        self.returncode = returncode
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class RecoveryTests(unittest.TestCase):
    # This fixture speaks only enough stdio JSON-RPC to initialize the bridge.
    # It has no model, authentication, filesystem, or network behavior.
    FIXTURE = """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    result = {"account": None, "requiresOpenaiAuth": True} if method == "account/read" else {}
    if method == "thread/resume":
        result = {"thread": {"id": message["params"]["threadId"]}}
    print(json.dumps({"id": message["id"], "result": result}), flush=True)
"""

    def setUp(self):
        self.bridge = server.CodexBridge(Path("unused.exe"), str(server.ROOT))

    def tearDown(self):
        self.bridge.close()

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("Local fixture did not reach the expected state")
            time.sleep(0.01)

    def test_dead_child_can_reconnect_without_an_official_app(self):
        children = []
        real_popen = subprocess.Popen

        def launch_fixture(*args, **kwargs):
            process = real_popen(
                [sys.executable, "-u", "-c", self.FIXTURE],
                cwd=str(server.ROOT), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            children.append(process)
            return process

        try:
            with patch.object(server.subprocess, "Popen", side_effect=launch_fixture):
                self.bridge.start()
                self.assertTrue(self.bridge.status()["connected"])
                first = self.bridge.process
                instance_id = self.bridge.instance_id
                self.bridge.rpc("thread/resume", {"threadId": "fixture-history"})
                self.bridge._server_request({"id": 5, "method": "item/fileChange/requestApproval", "params": {}})
                first.stdin.close()
                first.wait(timeout=5)
                self.wait_for(lambda: not self.bridge.ready)
                self.assertEqual(self.bridge.status()["ownedThreadIds"], [])
                self.assertEqual(self.bridge.status()["pendingRequests"], [])
                reconnecting = self.bridge.reconnect()
                self.assertEqual(reconnecting["instanceId"], instance_id)
                self.wait_for(lambda: self.bridge.ready and not self.bridge.starting)
                self.assertIsNot(self.bridge.process, first)
                self.assertEqual(len(children), 2)
                self.bridge.rpc("thread/resume", {"threadId": "fixture-history"})
                self.assertEqual(self.bridge.status()["ownedThreadIds"], ["fixture-history"])
                self.assertEqual(self.bridge.pending, {})
                self.assertIsNone(self.bridge.error)
        finally:
            self.bridge.close()
            for process in children:
                if process.poll() is None:
                    process.stdin.close()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def test_failed_handshake_retires_only_failed_child_then_reconnects(self):
        failed = FakeProcess()
        recovered = FakeProcess()

        def reply(method, params, timeout=60, process=None):
            if process is failed:
                raise server.BridgeError("Fixture initialization failed", -32001)
            self.assertIs(process, recovered)
            return {"account": None, "requiresOpenaiAuth": True} if method == "account/read" else {}

        with patch.object(server.subprocess, "Popen", side_effect=[failed, recovered]) as launch, patch.object(self.bridge, "_read_stdout"), patch.object(self.bridge, "_drain_stderr"), patch.object(self.bridge, "_request", side_effect=reply), patch.object(self.bridge, "_stop_process", wraps=self.bridge._stop_process) as stop:
            self.bridge.start()
            self.assertFalse(self.bridge.ready)
            self.assertIsNone(self.bridge.process)
            self.assertTrue(failed.stdin.closed)
            self.assertIsNotNone(failed.poll())
            stop.assert_called_once_with(failed)
            self.bridge.reconnect()
            self.wait_for(lambda: self.bridge.ready and not self.bridge.starting)
            self.assertIs(self.bridge.process, recovered)
            self.assertEqual(launch.call_count, 2)
            self.assertFalse(recovered.stdin.closed)
            self.assertIsNone(self.bridge.error)
            stop.assert_called_once_with(failed)

    def test_failed_initialized_notification_retires_unestablished_child(self):
        process = FakeProcess()
        with patch.object(server.subprocess, "Popen", return_value=process), patch.object(self.bridge, "_read_stdout"), patch.object(self.bridge, "_drain_stderr"), patch.object(self.bridge, "_request", return_value={}), patch.object(self.bridge, "_write", side_effect=server.BridgeError("Fixture initialized notification failed")):
            self.bridge.start()
        self.assertFalse(self.bridge.ready)
        self.assertIsNone(self.bridge.process)
        self.assertTrue(process.stdin.closed)

    def test_failed_handshake_does_not_stop_child_with_owned_work_or_approval(self):
        for protected in ("task", "approval"):
            with self.subTest(protected=protected):
                bridge = server.CodexBridge(Path("unused.exe"), str(server.ROOT))
                process = FakeProcess()

                def fail_initialization(*args, **kwargs):
                    if protected == "task":
                        bridge.owned_threads.add("working")
                    else:
                        bridge.server_requests[("int", 7)] = {"id": 7, "method": "item/fileChange/requestApproval", "params": {}}
                    raise server.BridgeError("Fixture initialization failed")

                try:
                    with patch.object(server.subprocess, "Popen", return_value=process), patch.object(bridge, "_read_stdout"), patch.object(bridge, "_drain_stderr"), patch.object(bridge, "_request", side_effect=fail_initialization), patch.object(bridge, "_stop_process", wraps=bridge._stop_process) as stop:
                        bridge.start()
                        stop.assert_not_called()
                        self.assertIs(bridge.process, process)
                        self.assertFalse(process.stdin.closed)
                        self.assertIsNone(process.poll())
                        self.assertTrue(bridge.owned_threads or bridge.server_requests)
                finally:
                    bridge.close()

    def test_optional_account_metadata_failure_preserves_live_transport_and_work(self):
        process = FakeProcess()

        def reply(method, params, timeout=60, process=None):
            if method == "account/read":
                self.assertTrue(self.bridge.ready)
                self.bridge.owned_threads.add("working")
                self.bridge.server_requests[("int", 8)] = {"id": 8, "method": "item/fileChange/requestApproval", "params": {}}
                raise server.BridgeError("Fixture account metadata timed out", -32001)
            return {}

        with patch.object(server.subprocess, "Popen", return_value=process), patch.object(self.bridge, "_read_stdout"), patch.object(self.bridge, "_drain_stderr"), patch.object(self.bridge, "_request", side_effect=reply), patch.object(self.bridge, "_stop_process", wraps=self.bridge._stop_process) as stop:
            self.bridge.start()
            stop.assert_not_called()
        self.assertTrue(self.bridge.ready)
        self.assertIsNone(self.bridge.error)
        self.assertIs(self.bridge.process, process)
        self.assertFalse(process.stdin.closed)
        self.assertIsNone(process.poll())
        self.assertEqual(self.bridge.account, {"account": None, "requiresOpenaiAuth": True})
        self.assertEqual(self.bridge.owned_threads, {"working"})
        self.assertEqual(len(self.bridge.server_requests), 1)

    def test_transport_death_during_metadata_read_remains_disconnected(self):
        process = FakeProcess()

        def reply(method, params, timeout=60, process=None):
            if method == "account/read":
                process.returncode = 1
                raise server.BridgeError("Fixture transport exited", -32002)
            return {}

        with patch.object(server.subprocess, "Popen", return_value=process), patch.object(self.bridge, "_read_stdout"), patch.object(self.bridge, "_drain_stderr"), patch.object(self.bridge, "_request", side_effect=reply), patch.object(self.bridge, "_stop_process", wraps=self.bridge._stop_process) as stop:
            self.bridge.start()
            stop.assert_not_called()
        self.assertFalse(self.bridge.ready)
        self.assertEqual(self.bridge.error, "Fixture transport exited")
        self.assertIs(self.bridge.process, process)

    def test_live_child_is_not_replaced_and_keeps_pending_approval(self):
        process = FakeProcess()
        self.bridge.process = process
        self.bridge.ready = True
        self.bridge.owned_threads.add("working")
        pending = {"id": 8, "method": "item/fileChange/requestApproval", "params": {}}
        self.bridge.server_requests[("int", 8)] = pending
        with patch.object(self.bridge, "_start_process") as start:
            for _ in range(3):
                status = self.bridge.reconnect()
                self.assertTrue(status["connected"])
            self.bridge.start()
        start.assert_not_called()
        self.assertIs(self.bridge.process, process)
        self.assertEqual(self.bridge.owned_threads, {"working"})
        self.assertEqual(list(self.bridge.server_requests.values()), [pending])

    def test_alive_initializing_child_is_not_replaced(self):
        self.bridge.process = FakeProcess()
        self.bridge.ready = False
        with patch.object(self.bridge, "_start_process") as start:
            self.assertFalse(self.bridge.reconnect()["connected"])
            self.bridge.start()
        start.assert_not_called()

    def test_concurrent_reconnects_start_only_one_child(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        process = FakeProcess()

        def initialize():
            entered.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("Fixture initialization was not released")
                with self.bridge.lock:
                    self.bridge.process = process
                    self.bridge.ready = True
            finally:
                finished.set()

        with patch.object(self.bridge, "_start_process", side_effect=initialize) as start:
            try:
                self.bridge.reconnect()
                self.assertTrue(entered.wait(2))
                workers = [threading.Thread(target=self.bridge.reconnect) for _ in range(8)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())
                start.assert_called_once()
            finally:
                release.set()
                self.assertTrue(finished.wait(2))
                self.wait_for(lambda: not self.bridge.starting)
        self.assertTrue(self.bridge.ready)

    def test_old_reader_cannot_clear_recovered_state_or_deliver_old_reply(self):
        old = FakeProcess(returncode=0, stdout=json.dumps({"id": 17, "result": {"stale": True}}) + "\n")
        current = FakeProcess()
        waiter = queue.Queue(maxsize=1)
        pending = {"id": 9, "method": "item/fileChange/requestApproval", "params": {}}
        self.bridge.process = current
        self.bridge.ready = True
        self.bridge.owned_threads.add("recovered")
        self.bridge.pending[17] = waiter
        self.bridge.server_requests[("int", 9)] = pending
        self.bridge.responding.add(("int", 9))
        self.bridge._read_stdout(old)
        self.assertTrue(self.bridge.ready)
        self.assertIsNone(self.bridge.error)
        self.assertEqual(self.bridge.owned_threads, {"recovered"})
        self.assertEqual(self.bridge.server_requests[("int", 9)], pending)
        self.assertEqual(self.bridge.responding, {("int", 9)})
        self.assertTrue(waiter.empty())
        self.assertEqual(self.bridge.sequence, 0)

    def test_late_rpc_cannot_reacquire_ownership_after_reconnect(self):
        for method, params in (("thread/start", {"cwd": str(server.ROOT)}), ("thread/resume", {"threadId": "old-thread"})):
            with self.subTest(method=method):
                old = FakeProcess()
                current = FakeProcess()
                self.bridge.process = old
                self.bridge.ready = True
                entered = threading.Event()
                release = threading.Event()
                errors = []

                def delayed_request(*args, **kwargs):
                    self.assertIs(kwargs["process"], old)
                    entered.set()
                    if not release.wait(5):
                        raise RuntimeError("Fixture RPC was not released")
                    return {"thread": {"id": "old-thread"}}

                def invoke():
                    try:
                        self.bridge.rpc(method, params)
                    except Exception as exc:
                        errors.append(exc)

                with patch.object(self.bridge, "_request", side_effect=delayed_request):
                    worker = threading.Thread(target=invoke)
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        with self.bridge.lock:
                            self.bridge.process = current
                            self.bridge.owned_threads = {"new-thread"}
                    finally:
                        release.set()
                        worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], server.BridgeError)
                self.assertEqual(errors[0].code, -32002)
                self.assertEqual(self.bridge.owned_threads, {"new-thread"})

    def test_stale_write_never_reaches_new_child(self):
        old = FakeProcess(returncode=0)
        current = FakeProcess()
        self.bridge.process = current
        with self.assertRaises(server.BridgeError):
            self.bridge._write({"method": "turn/start"}, process=old)
        self.assertEqual(current.stdin.getvalue(), "")

    def test_closed_bridge_cannot_reconnect(self):
        self.bridge.close()
        with patch.object(self.bridge, "_start_process") as start:
            with self.assertRaises(server.BridgeError) as error:
                self.bridge.reconnect()
        self.assertEqual(error.exception.code, -32002)
        start.assert_not_called()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = StubBridge()

    def test_no_raw_or_configuration_apis(self):
        for method in ("config/read", "config/batchWrite", "command/exec", "fs/readFile", "thread/shellCommand", "account/login/start"):
            with self.subTest(method=method), self.assertRaises(server.BridgeError):
                self.bridge.rpc(method, {})
        self.assertEqual(self.bridge.sent, [])

    def test_permissions_and_alternate_rollouts_not_rpc_parameters(self):
        for method, params in (
            ("thread/start", {"sandbox": "danger-full-access"}),
            ("thread/start", {"approvalPolicy": "never"}),
            ("thread/start", {"config": {"model": "other"}}),
            ("thread/resume", {"threadId": "test", "path": "other.jsonl"}),
            ("turn/start", {"threadId": "test", "input": [], "dynamicTools": []}),
        ):
            with self.subTest(method=method, params=params), self.assertRaises(server.BridgeError):
                self.bridge.rpc(method, params)
        self.assertEqual(self.bridge.sent, [])

    def test_models_limits_and_types(self):
        self.bridge.rpc("model/list", {"limit": 100})
        for limit in (0, 101, True, "20", float("inf")):
            with self.subTest(limit=limit), self.assertRaises(server.BridgeError):
                self.bridge.rpc("model/list", {"limit": limit})

    def test_default_model_is_omitted(self):
        self.bridge.rpc("thread/start", {"cwd": str(server.ROOT)})
        self.assertNotIn("model", self.bridge.sent[-1][1])

    def test_history_index_flag_is_read_only_and_preserves_explicit_repair(self):
        for use_index in (True, False):
            self.bridge.rpc("thread/list", {"limit": 30, "useStateDbOnly": use_index})
            self.assertIs(self.bridge.sent[-1][1]["useStateDbOnly"], use_index)
        self.assertEqual(self.bridge.status()["ownedThreadIds"], [])

    def test_history_index_flag_rejects_non_booleans_before_transport(self):
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(server.BridgeError):
                self.bridge.rpc("thread/list", {"useStateDbOnly": value})
        self.assertEqual(self.bridge.sent, [])

    def test_history_pages_preserve_cursors_content_and_read_only_ownership(self):
        pages = [
            {"data": [{"id": "turn-2", "items": [{"id": "assistant", "type": "agentMessage", "text": "saved reply"}]}], "nextCursor": "older-turns", "backwardsCursor": "newer-turns"},
            {"data": [{"id": "turn-1", "items": [{"id": "user", "type": "userMessage", "content": [{"type": "text", "text": "saved question"}]}]}], "nextCursor": None},
            {"data": [{"turnId": "turn-1", "item": {"id": "tool", "type": "commandExecution", "aggregatedOutput": "saved output", "encrypted_content": "private"}}], "nextCursor": "more-items"},
        ]
        with patch.object(self.bridge, "_request", side_effect=pages) as request:
            first = self.bridge.rpc("thread/turns/list", {"threadId": "history", "itemsView": "full", "sortDirection": "desc", "limit": 30})
            second = self.bridge.rpc("thread/turns/list", {"threadId": "history", "itemsView": "full", "sortDirection": "desc", "cursor": first["nextCursor"], "limit": 30})
            items = self.bridge.rpc("thread/items/list", {"threadId": "history", "turnId": "turn-1", "cursor": {"type": "item", "itemId": "user"}, "limit": 100, "sortDirection": "asc"})
        self.assertEqual(first["nextCursor"], "older-turns")
        self.assertEqual(first["backwardsCursor"], "newer-turns")
        self.assertEqual(first["data"][0]["items"][0]["text"], "saved reply")
        self.assertIsNone(second["nextCursor"])
        self.assertEqual(request.call_args_list[1].args[1]["cursor"], "older-turns")
        self.assertEqual(items["data"][0]["turnId"], "turn-1")
        self.assertEqual(items["data"][0]["item"]["aggregatedOutput"], "saved output")
        self.assertEqual(items["data"][0]["item"]["encrypted_content"], "[redacted]")
        self.assertEqual(self.bridge.status()["ownedThreadIds"], [])

    def test_history_pages_reject_invalid_inputs_before_transport(self):
        for method in ("thread/turns/list", "thread/items/list"):
            for params in ({}, {"threadId": "history", "limit": 0}, {"threadId": "history", "limit": 101}, {"threadId": "history", "sortDirection": "sideways"}, {"threadId": "history", "path": "other.jsonl"}):
                with self.subTest(method=method, params=params), self.assertRaises(server.BridgeError):
                    self.bridge.rpc(method, params)
        for method, params in (
            ("thread/turns/list", {"threadId": "history", "itemsView": "secret"}),
            ("thread/items/list", {"threadId": "history", "cursor": {"type": "item"}}),
            ("thread/items/list", {"threadId": "history", "turnId": 3}),
        ):
            with self.subTest(method=method, params=params), self.assertRaises(server.BridgeError):
                self.bridge.rpc(method, params)
        self.assertEqual(self.bridge.sent, [])

    def test_instance_id_is_stable_and_unique(self):
        first = self.bridge.status()["instanceId"]
        self.assertEqual(len(first), 32)
        self.assertEqual(first, self.bridge.status()["instanceId"])
        self.assertNotEqual(first, StubBridge().status()["instanceId"])

    def test_default_workspace_follows_the_launched_clone_and_does_not_include_credentials(self):
        self.bridge.cwd = r"E:\Projects\codex-dragon-decorate"
        snapshot = self.bridge.status()
        self.assertEqual(snapshot["defaultCwd"], self.bridge.cwd)
        self.assertEqual(set(snapshot["account"]), {"account", "requiresOpenaiAuth"})
        for private_key in ("authJson", "accessToken", "refreshToken", "apiKey"):
            self.assertNotIn(private_key, snapshot)
        self.assertNotIn("lenovo", json.dumps(snapshot))

    def test_native_writer_conflict_is_structured_and_hides_thread_id(self):
        error = server.native_bridge_error({"code": -32600, "message": "thread private-thread-id already has an active writer"})
        self.assertEqual(error.code, -32600)
        self.assertEqual(error.kind, "thread_in_use")
        self.assertIn("另一个 Codex 窗口", str(error))
        self.assertNotIn("private-thread-id", str(error))
        self.assertNotIn("active writer", str(error))
        other = server.native_bridge_error({"code": -32602, "message": "Unknown thread"})
        self.assertIsNone(other.kind)
        self.assertEqual(str(other), "Unknown thread")

    def test_thread_ownership_requires_successful_start_or_resume(self):
        self.bridge.rpc("thread/read", {"threadId": "view-only", "includeTurns": True})
        self.assertEqual(self.bridge.status()["ownedThreadIds"], [])
        with patch.object(self.bridge, "_request", return_value={"thread": {"id": "created"}}):
            self.bridge.rpc("thread/start", {"cwd": str(server.ROOT)})
        with patch.object(self.bridge, "_request", return_value={"thread": {"id": "resumed"}}):
            self.bridge.rpc("thread/resume", {"threadId": "resumed"})
        with patch.object(self.bridge, "_request", side_effect=server.native_bridge_error({"message": "thread elsewhere already has an active writer"})) as request:
            with self.assertRaises(server.BridgeError):
                self.bridge.rpc("thread/resume", {"threadId": "elsewhere"})
            request.assert_called_once()
        self.assertEqual(self.bridge.status()["ownedThreadIds"], ["created", "resumed"])
        self.bridge.rpc("thread/archive", {"threadId": "resumed"})
        self.assertEqual(self.bridge.status()["ownedThreadIds"], ["created"])
        self.bridge.close()
        self.assertEqual(self.bridge.status()["ownedThreadIds"], [])

    def test_text_and_inline_images_only(self):
        self.bridge.rpc("turn/start", {"threadId": "test", "input": [{"type": "text", "text": "hi"}]})
        for item in (
            {"type": "image", "url": "https://example.com/picture.png"},
            {"type": "image", "url": "file:///C:/secret.png"},
            {"type": "localImage", "path": "C:/secret.png"},
            {"type": "skill", "name": "anything", "path": "C:/somewhere"},
            {"type": "image", "url": "data:image/png;base64,AAAA", "detail": "high"},
        ):
            with self.subTest(item=item), self.assertRaises(server.BridgeError):
                self.bridge.rpc("turn/start", {"threadId": "test", "input": [item]})
        with self.assertRaises(server.BridgeError):
            self.bridge.rpc("turn/start", {"threadId": "test", "input": [{"type": "image", "url": "data:image/png;base64,AAAA"}] * 4})

    def test_images_above_old_limit_and_at_twenty_mib_are_forwarded_without_changes(self):
        for size in (2 * 1024 * 1024 + 1, 20 * 1024 * 1024):
            with self.subTest(size=size):
                url = "data:image/png;base64," + base64.b64encode(b"x" * size).decode("ascii")
                self.bridge.rpc("turn/start", {"threadId": "test", "input": [{"type": "image", "url": url}]})
                self.assertIs(self.bridge.sent[-1][1]["input"][0]["url"], url)
                self.bridge.sent.clear()

    def test_image_above_twenty_mib_is_rejected_before_native_request(self):
        url = "data:image/png;base64," + base64.b64encode(b"x" * (20 * 1024 * 1024 + 1)).decode("ascii")
        with self.assertRaises(server.BridgeError) as error:
            self.bridge.rpc("turn/start", {"threadId": "test", "input": [{"type": "image", "url": url}]})
        self.assertIn("20 MiB", str(error.exception))
        self.assertEqual(self.bridge.sent, [])

    def test_three_maximum_images_fit_the_bounded_json_envelope(self):
        url = "data:image/webp;base64," + base64.b64encode(b"x" * (20 * 1024 * 1024)).decode("ascii")
        params = {"threadId": "test", "input": [{"type": "text", "text": "三个附件"}] + [{"type": "image", "url": url}] * 3}
        encoded_body = json.dumps({"method": "turn/start", "params": params}, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(encoded_body), 80 * 1024 * 1024)
        self.assertLess(len(encoded_body), server.MAX_BODY)
        del encoded_body
        self.bridge.rpc("turn/start", params)
        self.assertEqual(len(self.bridge.sent[-1][1]["input"]), 4)

    def pending(self, method, params=None, ident=7):
        self.bridge._server_request({"id": ident, "method": method, "params": params or {}})
        self.assertEqual(self.bridge.sent, [])

    def test_approval_not_automatic_and_enum_checked(self):
        self.pending("item/commandExecution/requestApproval", {"availableDecisions": ["accept", "cancel"]})
        for result in ({"decision": "approved"}, {"decision": "decline"}, {"decision": "accept", "extra": True}, {"decision": {"acceptWithExecpolicyAmendment": {"execpolicy_amendment": ["anything"]}}}):
            with self.subTest(result=result), self.assertRaises(server.BridgeError):
                self.bridge.respond(7, result)
        self.assertEqual(self.bridge.sent, [])
        self.bridge.respond(7, {"decision": "accept"})
        self.assertEqual(self.bridge.sent[-1], {"id": 7, "result": {"decision": "accept"}})
        with self.assertRaises(server.BridgeError):
            self.bridge.respond(7, {"decision": "accept"})

    def test_approval_id_type_is_preserved(self):
        self.pending("item/fileChange/requestApproval")
        with self.assertRaises(server.BridgeError):
            self.bridge.respond("7", {"decision": "accept"})
        with self.assertRaises(server.BridgeError):
            self.bridge.respond(True, {"decision": "accept"})

    def test_permissions_cannot_exceed_request(self):
        requested = {"network": {"enabled": True}, "fileSystem": {"read": ["D:/work"]}}
        self.pending("item/permissions/requestApproval", {"permissions": requested})
        for result in (
            {"permissions": {"fileSystem": {"write": ["C:/"]}}},
            {"permissions": {"fileSystem": {"read": ["D:/work", "C:/"]}}},
            {"permissions": requested, "strictAutoReview": False},
            {"permissions": requested, "scope": "forever"},
        ):
            with self.subTest(result=result), self.assertRaises(server.BridgeError):
                self.bridge.respond(7, result)
        self.assertEqual(self.bridge.sent, [])
        self.bridge.respond(7, {"permissions": {}, "scope": "turn"})

    def test_requested_subset_allowed(self):
        self.assertTrue(server.requested_grant({"network": {"enabled": False}}, {"network": {"enabled": True}}))
        self.assertTrue(server.requested_grant({"fileSystem": {"read": ["D:/a"]}}, {"fileSystem": {"read": ["D:/a", "D:/b"]}}))
        self.assertFalse(server.requested_grant({"network": {"enabled": True}}, {"network": {"enabled": False}}))

    def test_legacy_approval_cannot_persist_policy(self):
        self.pending("execCommandApproval")
        for decision in ("accept", "approved_mcp_policy_amendment", "timed_out", {"approved_execpolicy_amendment": {"proposed_execpolicy_amendment": ["anything"]}}):
            with self.subTest(decision=decision), self.assertRaises(server.BridgeError):
                self.bridge.respond(7, {"decision": decision})
        self.bridge.respond(7, {"decision": {"denied": {"rejection": "User declined"}}})

    def test_unknown_server_requests_error(self):
        self.bridge._server_request({"id": 9, "method": "notSupported", "params": {}})
        self.assertEqual(self.bridge.sent[-1]["error"]["code"], -32601)
        self.assertEqual(self.bridge.server_requests, {})

    def test_redaction_and_account_summary(self):
        self.assertEqual(server.redact({"list": [{"accessToken": "secret"}]}), {"list": [{"accessToken": "[redacted]"}]})
        summary = server.account_summary({"account": {"type": "chatgpt", "planType": "plus", "email": "private", "accessToken": "secret"}, "requiresOpenaiAuth": True})
        self.assertEqual(set(summary["account"]), {"type", "planType"})


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "attachments"
        self.bridge = StubBridge()
        self.bridge.attachments = server.AttachmentStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def upload(self, name="资料.txt", content=b"example attachment"):
        return self.bridge.attachments.upload({"name": name, "data": base64.b64encode(content).decode("ascii")})

    def reference(self, ident):
        self.bridge.rpc("turn/start", {"threadId": "fixture", "input": [{"type": "attachment", "id": ident}]})
        return self.bridge.sent[-1][1]["input"][0]

    def test_upload_returns_only_id_name_size_and_native_reference_is_owned(self):
        result = self.upload()
        self.assertEqual(set(result), {"id", "name", "size"})
        self.assertRegex(result["id"], r"^[a-f0-9]{32}$")
        native = self.reference(result["id"])
        self.assertEqual(native["type"], "text")
        marker, instruction = native["text"].split("\n", 1)
        reference = json.loads(marker.removeprefix("[用户附件] "))
        target = Path(reference["path"])
        self.assertEqual(reference["name"], result["name"])
        self.assertEqual(target.parent, self.root.resolve() / result["id"])
        self.assertEqual(target.read_bytes(), b"example attachment")
        self.assertEqual(instruction, "请根据我的消息读取该本地附件。")

    def test_saved_id_survives_bridge_restart_and_duplicate_filename_is_isolated(self):
        first = self.upload("metadata.json", b"first")
        second = self.upload("metadata.json", b"second")
        self.assertNotEqual(first["id"], second["id"])
        self.bridge = StubBridge()
        self.bridge.attachments = server.AttachmentStore(self.root)
        for uploaded, expected in ((first, b"first"), (second, b"second")):
            native = self.reference(uploaded["id"])
            reference = json.loads(native["text"].split("\n", 1)[0].removeprefix("[用户附件] "))
            self.assertEqual(Path(reference["path"]).read_bytes(), expected)

    def test_filename_is_windows_safe_preserves_extension_and_cannot_escape(self):
        for name in ("../outside.txt", "..\\outside.txt", "D:\\private.txt", "", "..", ".", "a" * 256):
            with self.subTest(name=name), self.assertRaises(server.BridgeError):
                self.upload(name)
        for source, expected in (("CON.txt", "_CON.txt"), ('report:part?.pdf ', "report_part_.pdf"), ("a\ntext.txt", "a_text.txt"), ("Lpt1.xlsx", "_Lpt1.xlsx")):
            with self.subTest(source=source):
                result = self.upload(source)
                self.assertEqual(result["name"], expected)
                self.reference(result["id"])
        result = self.upload("中" * 200 + ".docx")
        self.assertLessEqual(len(result["name"]), 120)
        self.assertTrue(result["name"].endswith(".docx"))

    def test_invalid_upload_encoding_empty_oversize_and_extra_fields_are_rejected(self):
        valid = {"name": "x.txt", "data": "eA=="}
        for payload in ({}, [], {**valid, "path": "C:/private"}, {**valid, "name": 1}, {**valid, "data": None}, {**valid, "data": ""}, {**valid, "data": "!!!!"}, {**valid, "data": "你好"}, {**valid, "data": "eA==\n"}, {**valid, "data": base64.b64encode(b"x" * (server.MAX_ATTACHMENT + 1)).decode("ascii")}):
            with self.subTest(payload_type=type(payload).__name__), self.assertRaises(server.BridgeError):
                self.bridge.attachments.upload(payload)
        self.assertFalse(self.root.exists())
        result = self.upload(content=b"x" * server.MAX_ATTACHMENT)
        self.assertEqual(result["size"], server.MAX_ATTACHMENT)

    def test_spoofed_ids_paths_fields_and_missing_file_never_reach_native_engine(self):
        uploaded = self.upload()
        for item in ({"type": "attachment", "id": "../../private"}, {"type": "attachment", "id": "a" * 32}, {"type": "attachment", "id": uploaded["id"], "path": "C:/private"}, {"type": "attachment", "id": None}, {"type": "localImage", "path": str(self.root)}, {"type": "mention", "name": "file", "path": str(self.root)}, {"type": "skill", "name": "file", "path": str(self.root)}, 1):
            with self.subTest(item=item), self.assertRaises(server.BridgeError):
                self.bridge.rpc("turn/start", {"threadId": "fixture", "input": [item]})
        self.assertEqual(self.bridge.sent, [])
        (self.root / uploaded["id"] / ("file-" + uploaded["name"])).unlink()
        with self.assertRaises(server.BridgeError) as missing:
            self.reference(uploaded["id"])
        self.assertEqual(missing.exception.kind, "attachment_missing")

    def test_modified_metadata_cannot_reference_outside_storage_or_wrong_size(self):
        uploaded = self.upload()
        path = self.root / uploaded["id"] / "metadata.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        for changes in ({"name": "../outside.txt"}, {"size": metadata["size"] + 1}, {"size": True}, {"id": "b" * 32}, {"path": "C:/private"}):
            with self.subTest(changes=changes):
                path.write_text(json.dumps({**metadata, **changes}), encoding="utf-8")
                with self.assertRaises(server.BridgeError):
                    self.reference(uploaded["id"])
        self.assertEqual(self.bridge.sent, [])

    def test_mixed_attachment_limit_and_three_image_limit_are_retained(self):
        uploaded = self.upload()
        attachment = {"type": "attachment", "id": uploaded["id"]}
        image = {"type": "image", "url": "data:image/png;base64,eA=="}
        self.bridge.rpc("turn/start", {"threadId": "fixture", "input": [{"type": "text", "text": "read"}] + [attachment] * 7 + [image] * 3})
        self.assertEqual(len(self.bridge.sent[-1][1]["input"]), 11)
        for entries in ([attachment] * 11, [attachment] * 8 + [image] * 3, [image] * 4):
            with self.subTest(count=len(entries)), self.assertRaises(server.BridgeError):
                self.bridge.rpc("turn/start", {"threadId": "fixture", "input": entries})

    def test_concurrent_uploads_same_filename_do_not_overwrite_or_share_ids(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            uploaded = list(pool.map(lambda index: self.upload("same.txt", str(index).encode()), range(12)))
        self.assertEqual(len({item["id"] for item in uploaded}), 12)
        for index, item in enumerate(uploaded):
            self.assertEqual((self.root / item["id"] / "file-same.txt").read_bytes(), str(index).encode())

    def test_storage_quota_and_storage_failure_have_structured_errors(self):
        with patch.object(server, "MAX_STORED_ATTACHMENTS", 1):
            self.upload()
            with self.assertRaises(server.BridgeError) as full:
                self.upload()
            self.assertEqual(full.exception.kind, "attachment_storage_full")
        with patch.object(server, "MAX_ATTACHMENT_STORE", 10):
            with self.assertRaises(server.BridgeError) as full:
                self.upload()
            self.assertEqual(full.exception.kind, "attachment_storage_full")
        with patch.object(server.json, "dump", side_effect=OSError("fixture disk full")):
            with self.assertRaises(server.BridgeError) as failed:
                self.upload()
            self.assertEqual(failed.exception.kind, "attachment_storage_error")
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_uuid_collision_does_not_delete_or_overwrite_an_existing_attachment(self):
        existing = self.upload(content=b"keep this")
        with patch.object(server.uuid, "uuid4") as uuid:
            uuid.return_value.hex = existing["id"]
            with self.assertRaises(server.BridgeError) as error:
                self.upload(content=b"replace this")
        self.assertEqual(error.exception.kind, "attachment_storage_error")
        self.assertEqual((self.root / existing["id"] / ("file-" + existing["name"])).read_bytes(), b"keep this")
        self.reference(existing["id"])

    def test_windows_junction_storage_roots_and_upload_directories_are_rejected(self):
        if not hasattr(Path, "is_junction"):
            self.skipTest("Path.is_junction requires Python 3.12")
        with patch.object(Path, "is_junction", return_value=True):
            with self.assertRaises(server.BridgeError) as root_error:
                self.upload()
            self.assertEqual(root_error.exception.kind, "attachment_storage_error")
        self.assertFalse(self.root.exists())
        uploaded = self.upload()
        directory = self.root / uploaded["id"]
        with patch.object(Path, "is_junction", lambda path: path == directory):
            with self.assertRaises(server.BridgeError) as directory_error:
                self.reference(uploaded["id"])
            self.assertEqual(directory_error.exception.kind, "attachment_missing")
        self.assertEqual(self.bridge.sent, [])


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = server.LocalServer(("127.0.0.1", 0), server.Handler)
        cls.server.bridge = StubBridge()
        cls.attachments_temp = tempfile.TemporaryDirectory()
        cls.server.bridge.attachments = server.AttachmentStore(Path(cls.attachments_temp.name) / "attachments")
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=2)
        cls.attachments_temp.cleanup()

    def request(self, method="GET", path="/api/status", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_host_and_origin_boundaries(self):
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], 403)
        status, headers, _ = self.request()
        self.assertEqual(status, 200)
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_only_json_posts(self):
        self.assertEqual(self.request("POST", "/api/rpc", '{}', {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/rpc", '{}', {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request("POST", "/api/rpc", '{"method":"model/list","params":{"limit":NaN}}', {"Content-Type": "application/json"})[0], 400)

    def test_body_size_bound(self):
        status, _, body = self.request("POST", "/api/rpc", '', {"Content-Type": "application/json", "Content-Length": str(server.MAX_BODY + 1)})
        self.assertEqual(status, 400)
        self.assertIn("分次发送", json.loads(body)["error"]["message"])

    def test_image_body_above_old_ten_mib_limit_reaches_stub_native_rpc(self):
        url = "data:image/jpeg;base64," + base64.b64encode(b"x" * (9 * 1024 * 1024)).decode("ascii")
        body = json.dumps({"method": "turn/start", "params": {"threadId": "fixture", "input": [{"type": "image", "url": url}]}})
        self.assertGreater(len(body), 10 * 1024 * 1024)
        with patch.object(self.server.bridge, "_request", return_value={"ok": True}) as request:
            status, _, response = self.request("POST", "/api/rpc", body, {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(response), {"result": {"ok": True}})
        request.assert_called_once()
        self.assertEqual(len(request.call_args.args[1]["input"][0]["url"]), len(url))

    def test_attachment_upload_requires_local_json_and_returns_opaque_id(self):
        body = json.dumps({"name": "fixture.txt", "data": "Zml4dHVyZQ=="})
        for headers, expected in (({"Content-Type": "text/plain"}, 415), ({"Content-Type": "application/json", "Origin": "https://evil.example"}, 403), ({"Content-Type": "application/json", "Host": "evil.example"}, 403), ({"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, 403)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/attachments", body, headers)[0], expected)
        status, _, response = self.request("POST", "/api/attachments", body, {"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        result = json.loads(response)["result"]
        self.assertEqual(set(result), {"id", "name", "size"})
        self.assertEqual(result["size"], 7)
        self.assertEqual(self.request(path="/api/attachments/" + result["id"])[0], 404)
        self.assertEqual(self.request("POST", "/api/attachments", json.dumps({"name": "x", "data": "!!!!"}), {"Content-Type": "application/json"})[0], 400)

    def test_no_static_directory_escape(self):
        for path in ("/%2e%2e/server.py", "/%2e%2e/%2e%2e/config.toml", "/..%5cserver.py", "/%00"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path=path)[0], 404)

    def test_shutdown_requires_same_origin_json(self):
        self.assertEqual(self.request("POST", "/api/shutdown", '{}', {"Content-Type": "application/json", "Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/api/shutdown", '{}', {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.request("POST", "/api/shutdown", '{}', {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/shutdown", '{"extra":true}', {"Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.request()[0], 200)

    def test_reconnect_requires_local_origin_and_empty_json_object(self):
        with patch.object(self.server.bridge, "reconnect") as reconnect:
            for body, headers, expected in (
                ('{}', {"Content-Type": "application/json", "Origin": "https://evil.example"}, 403),
                ('{}', {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"}, 403),
                ('{}', {"Content-Type": "text/plain"}, 415),
                ('{"extra":true}', {"Content-Type": "application/json"}, 400),
                ('[]', {"Content-Type": "application/json"}, 400),
                ('', {"Content-Type": "application/json"}, 400),
            ):
                with self.subTest(body=body, headers=headers):
                    self.assertEqual(self.request("POST", "/api/reconnect", body, headers)[0], expected)
            reconnect.assert_not_called()

    def test_reconnect_returns_connection_status_without_rpc_or_send(self):
        status = self.server.bridge.status()
        headers = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.server.server_port}"}
        with patch.object(self.server.bridge, "reconnect", return_value=status) as reconnect, patch.object(self.server.bridge, "rpc") as rpc:
            result_status, _, body = self.request("POST", "/api/reconnect", '{}', headers)
        self.assertEqual(result_status, 200)
        self.assertEqual(json.loads(body), {"result": status})
        reconnect.assert_called_once_with()
        rpc.assert_not_called()

    def test_closed_service_reconnect_is_structured_error(self):
        closed = server.CodexBridge(Path("unused.exe"), str(server.ROOT))
        closed.close()
        with patch.object(self.server, "bridge", closed):
            status, _, body = self.request("POST", "/api/reconnect", '{}', {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32002)

    def test_status_identity(self):
        data = json.loads(self.request()[2])
        self.assertEqual(data["appId"], "codex-dragon-client")
        self.assertEqual(data["version"], 1)

    def test_writer_conflict_returns_409_without_retrying_or_sending(self):
        error = server.native_bridge_error({"code": -32600, "message": "thread private-thread-id already has an active writer"})
        with patch.object(self.server.bridge, "_request", side_effect=error) as request:
            status, _, body = self.request("POST", "/api/rpc", json.dumps({"method": "thread/resume", "params": {"threadId": "private-thread-id"}}), {"Content-Type": "application/json"})
        self.assertEqual(status, 409)
        payload = json.loads(body)["error"]
        self.assertEqual(payload["kind"], "thread_in_use")
        self.assertEqual(payload["code"], -32600)
        self.assertNotIn("private-thread-id", payload["message"])
        request.assert_called_once_with("thread/resume", {"threadId": "private-thread-id"}, process=None)

    def test_successful_shutdown_stops_only_local_server(self):
        local = server.LocalServer(("127.0.0.1", 0), server.Handler)
        local.bridge = StubBridge()
        worker = threading.Thread(target=local.serve_forever, daemon=True)
        worker.start()
        connection = http.client.HTTPConnection("127.0.0.1", local.server_port, timeout=5)
        try:
            connection.request("POST", "/api/shutdown", '{}', {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{local.server_port}"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"ok": True})
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
        finally:
            connection.close()
            local.shutdown()
            local.server_close()


if __name__ == "__main__":
    unittest.main()
