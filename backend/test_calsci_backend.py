import ast
import pathlib
import sys
import tempfile
import threading
import time
import unittest


BACKEND_DIR = pathlib.Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import calsci_backend as backend


def _frame(line: str) -> str:
    return f"{backend.HELPER_FRAME_PREFIX}{line}{backend.HELPER_FRAME_SUFFIX}"


class HelperSuppressionTests(unittest.TestCase):
    def test_normalize_friendly_paste_source_normalizes_newlines(self) -> None:
        self.assertEqual(
            backend._normalize_friendly_paste_source("print(1)\r\nprint(2)\rprint(3)\n"),
            b"print(1)\nprint(2)\nprint(3)\n",
        )

    def test_normalize_friendly_paste_source_accepts_bytes(self) -> None:
        self.assertEqual(
            backend._normalize_friendly_paste_source(b"print(1)\r\n"),
            b"print(1)\n",
        )

    def test_detects_truncated_state_fragment(self) -> None:
        self.assertTrue(backend._looks_like_helper_terminal_fragment('STATE:{"frame_id":7,"fb":"AAAA"'))

    def test_detects_truncated_helper_echo_fragment(self) -> None:
        fragment = '"_hyb_poll_state" in globals() else print("HYBRID_SYNC_ERR:HELPER_MISSING")'
        self.assertTrue(backend._looks_like_helper_terminal_fragment(fragment))

    def test_unwraps_framed_helper_line(self) -> None:
        self.assertEqual(backend._clean_helper_line(_frame("HYBRID_READY")), "HYBRID_READY")

    def test_parse_helper_output_reads_framed_state(self) -> None:
        parsed = backend._parse_helper_output(_frame('STATE:{"frame_id":7,"changed":false}') + "\n")
        self.assertEqual(parsed["lines"], ['STATE:{"frame_id":7,"changed":false}'])
        self.assertEqual(parsed["states"], [{"frame_id": 7, "changed": False}])

    def test_suppressed_helper_fragment_does_not_pollute_terminal(self) -> None:
        emitted: list[str] = []
        session = backend.PersistentSession(emitted.append, lambda _payload: None, lambda _payload: None)

        with session._helper_condition:
            session._suppress_terminal_helper_output = True
            session._suppress_terminal_helper_depth = 1
            session._suppress_terminal_helper_output_deadline = time.monotonic() + 1.0
            session._suppress_terminal_helper_activity_seen = False

        session._process_terminal_text(_frame('STATE:{"frame_id":1'))
        session._process_terminal_text('\nCalSci >>> ')

        self.assertEqual(emitted, [])

    def test_overlapping_helper_prompts_and_next_command_stay_suppressed(self) -> None:
        emitted: list[str] = []
        session = backend.PersistentSession(emitted.append, lambda _payload: None, lambda _payload: None)

        with session._helper_condition:
            session._suppress_terminal_helper_output = True
            session._suppress_terminal_helper_depth = 2
            session._suppress_terminal_helper_output_deadline = time.monotonic() + 1.0
            session._suppress_terminal_helper_activity_seen = True

        session._process_terminal_text(
            'CalSci >>> \n'
            '_hyb_poll_state(11427) if "_hyb_poll_state" in globals() else print("HYBRID_SYNC_ERR:HELPER_MISSING")\n'
            + _frame('STATE:{"frame_id":11427,"changed":false}')
            + '\n'
            'CalSci >>> '
        )

        self.assertEqual(emitted, [])
        self.assertFalse(session._suppress_terminal_helper_output)
        self.assertEqual(session._suppress_terminal_helper_depth, 0)

    def test_enter_raw_repl_accepts_banner_and_prompt_in_same_read(self) -> None:
        class Dummy:
            def __init__(self) -> None:
                self._in_raw_repl = False
                self.read_calls = 0

            def _write_bytes(self, _data: bytes, flush: bool = True) -> None:
                return None

            def _drain_serial_input(self) -> None:
                return None

            def _raw_read_until(
                self,
                ending: bytes,
                timeout: float | None = 1.0,
                timeout_overall: float | None = None,
                data_consumer=None,
                cancel_event=None,
                cancel_handler=None,
            ) -> bytes:
                self.read_calls += 1
                if ending == backend.RAW_REPL_BANNER:
                    return b"\r\nCalSci >>> \r\nraw REPL; CTRL-B to exit\r\n>"
                return b">"

        dummy = Dummy()
        backend.CalSciController._enter_raw_repl(dummy)

        self.assertTrue(dummy._in_raw_repl)
        self.assertEqual(dummy.read_calls, 1)

    def test_sync_exec_raw_and_read_reuses_existing_raw_repl(self) -> None:
        class Dummy:
            def __init__(self) -> None:
                self._in_raw_repl = True
                self.enter_calls = 0
                self.exit_calls = 0
                self.exec_calls = 0

            def _enter_raw_repl(self, timeout_overall: float = 0.0) -> None:
                self.enter_calls += 1

            def _exit_raw_repl(self) -> None:
                self.exit_calls += 1

            def _exec_raw_no_follow(self, _code: str) -> None:
                self.exec_calls += 1

            def _raw_follow(
                self,
                timeout: float | None,
                line_callback=None,
                cancel_event=None,
            ) -> tuple[bytes, bytes, bool]:
                return (b"OK", b"", False)

        dummy = Dummy()
        output = backend.CalSciController.sync_exec_raw_and_read(dummy, "print('x')", timeout=2.0)

        self.assertEqual(output, "OK")
        self.assertEqual(dummy.exec_calls, 1)
        self.assertEqual(dummy.enter_calls, 0)
        self.assertEqual(dummy.exit_calls, 0)


class SyncFolderTests(unittest.TestCase):
    def test_normalize_remote_folder_allows_device_root(self) -> None:
        self.assertEqual(backend._normalize_remote_folder("/"), "/")
        self.assertEqual(backend._normalize_remote_folder("apps/demo"), "/apps/demo")

    def test_scan_local_folder_skips_hidden_entries_like_desktop_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "visible").mkdir()
            (root / ".hidden_dir").mkdir()
            (root / "__pycache__").mkdir()
            (root / "main.py").write_text("print('ok')\n", encoding="utf-8")
            (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
            (root / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
            (root / "visible" / "module.py").write_text("print('visible')\n", encoding="utf-8")
            (root / ".hidden_dir" / "secret.py").write_text("print('no')\n", encoding="utf-8")
            (root / "__pycache__" / "module.cpython-312.pyc").write_bytes(b"skip")

            local_root, directories, files = backend._scan_local_folder(str(root), "/")

        self.assertEqual(local_root, root.resolve())
        self.assertEqual(directories, ["/", "/visible"])
        self.assertEqual(
            [file_info["relative_path"] for file_info in files],
            ["main.py", "visible/module.py"],
        )
        self.assertEqual(
            [file_info["remote_path"] for file_info in files],
            ["/main.py", "/visible/module.py"],
        )

    def test_build_sync_plan_respects_delete_mode(self) -> None:
        files = [
            {"remote_path": "/main.py", "size_bytes": 10},
            {"remote_path": "/lib/util.py", "size_bytes": 20},
        ]
        remote_sizes = {
            "/main.py": 10,
            "/lib/util.py": 25,
            "/stale.py": 5,
        }

        unchanged, to_upload, to_delete, extra_remote = backend._build_sync_plan(
            files,
            remote_sizes,
            delete_extraneous=False,
        )
        self.assertEqual(unchanged, ["/main.py"])
        self.assertEqual([file_info["remote_path"] for file_info in to_upload], ["/lib/util.py"])
        self.assertEqual(to_delete, [])
        self.assertEqual(extra_remote, ["/stale.py"])

        _, _, to_delete_mirror, _ = backend._build_sync_plan(
            files,
            remote_sizes,
            delete_extraneous=True,
        )
        self.assertEqual(to_delete_mirror, ["/stale.py"])

    def test_build_sync_plan_uses_signatures_for_same_size_files(self) -> None:
        files = [
            {"remote_path": "/same.py", "size_bytes": 10},
            {"remote_path": "/changed.py", "size_bytes": 10},
        ]
        remote_sizes = {
            "/same.py": 10,
            "/changed.py": 10,
        }

        unchanged, to_upload, _, _ = backend._build_sync_plan(
            files,
            remote_sizes,
            delete_extraneous=False,
            signature_matches={"/same.py"},
        )

        self.assertEqual(unchanged, ["/same.py"])
        self.assertEqual([file_info["remote_path"] for file_info in to_upload], ["/changed.py"])

    def test_build_sync_directory_plan_includes_intermediate_folders(self) -> None:
        files = [{"remote_path": "/lib/usb/device/core.mpy"}]
        directories = backend._build_sync_directory_plan("/", files)
        self.assertEqual(directories, ["/", "/lib", "/lib/usb", "/lib/usb/device"])

    def test_build_sync_plan_supports_signature_and_size_fallback_mix(self) -> None:
        files = [
            {"remote_path": "/sig-match.py", "size_bytes": 10},
            {"remote_path": "/size-fallback.py", "size_bytes": 20},
            {"remote_path": "/recent-change.py", "size_bytes": 30},
        ]
        remote_sizes = {
            "/sig-match.py": 10,
            "/size-fallback.py": 20,
            "/recent-change.py": 30,
        }

        unchanged, to_upload, _, _ = backend._build_sync_plan(
            files,
            remote_sizes,
            delete_extraneous=False,
            signature_matches={"/sig-match.py"},
            size_fallback_paths={"/size-fallback.py"},
        )

        self.assertEqual(unchanged, ["/sig-match.py", "/size-fallback.py"])
        self.assertEqual([file_info["remote_path"] for file_info in to_upload], ["/recent-change.py"])

    def test_device_size_scan_script_uses_desktop_style_ilistdir(self) -> None:
        script = backend._device_list_file_sizes_script("/apps")
        self.assertIn("os.ilistdir", script)
        self.assertIn("def _is_dir", script)
        self.assertIn("_entry[1] & 0x4000", script)
        self.assertIn("_stat = os.stat(_full)", script)
        self.assertIn("_mode & 0x4000", script)
        self.assertIn('_scan(_root)', script)

    def test_device_size_stream_scan_script_emits_rows(self) -> None:
        script = backend._device_list_file_sizes_stream_script("/apps")
        self.assertIn("SIZE:", script)
        self.assertIn("SIZE_SCAN_DONE", script)
        self.assertIn("os.ilistdir", script)
        self.assertIn("def _is_dir", script)

    def test_device_signature_scan_script_uses_expected_marker(self) -> None:
        script = backend._device_list_file_signatures_script(["/apps/main.py"])
        self.assertIn("open(_path, 'rb')", script)
        self.assertIn("SIGS:", script)

    def test_device_signature_stream_scan_script_emits_rows(self) -> None:
        script = backend._device_list_file_signatures_stream_script(["/apps/main.py"])
        self.assertIn("SIG:", script)
        self.assertIn("SIG_SCAN_DONE", script)
        self.assertIn("open(_path, 'rb')", script)

    def test_device_selected_size_scan_script_uses_expected_marker(self) -> None:
        script = backend._device_selected_file_sizes_script(["/apps/main.py"])
        self.assertIn("_stat = os.stat(_path)", script)
        self.assertIn("open(_path, 'rb')", script)
        self.assertIn("PATH_SIZES:", script)

    def test_device_selected_size_stream_scan_script_uses_expected_marker(self) -> None:
        script = backend._device_selected_file_sizes_stream_script(["/apps/main.py"])
        self.assertIn("PATHSIZE:", script)
        self.assertIn("PATH_SIZE_SCAN_DONE", script)
        self.assertIn("open(_path, 'rb')", script)

    def test_parse_device_signatures_output(self) -> None:
        parsed = backend._parse_device_signatures_output("SIGS:{'/a.py': 'deadbeef', '/b.py': None}")
        self.assertEqual(parsed, {"/a.py": "deadbeef", "/b.py": None})

    def test_parse_device_signatures_stream_output(self) -> None:
        path_a = "/apps/main.py"
        path_b = "/apps/missing.py"
        parsed = backend._parse_device_signatures_stream_output(
            f"SIG:{len(path_a)}:{path_a}:1:deadbeef\n"
            f"SIG:{len(path_b)}:{path_b}:0:\n"
            "SIG_SCAN_DONE\n"
        )
        self.assertEqual(parsed, {path_a: "deadbeef", path_b: None})

    def test_parse_device_sizes_stream_output(self) -> None:
        parsed = backend._parse_device_sizes_stream_output(
            "SIZE:/apps/main.py:42\nSIZE:/apps/lib/util.py:7\nSIZE_SCAN_DONE\n"
        )
        self.assertEqual(parsed, {"/apps/main.py": 42, "/apps/lib/util.py": 7})

    def test_parse_device_selected_sizes_output(self) -> None:
        parsed = backend._parse_device_selected_sizes_output("PATH_SIZES:{'/a.py': 42, '/b.py': None}")
        self.assertEqual(parsed, {"/a.py": 42, "/b.py": None})

    def test_parse_device_selected_sizes_stream_output(self) -> None:
        path_a = "/apps/main.py"
        path_b = "/apps/missing.py"
        parsed = backend._parse_device_selected_sizes_stream_output(
            f"PATHSIZE:{len(path_a)}:{path_a}:1:42\n"
            f"PATHSIZE:{len(path_b)}:{path_b}:0:\n"
            "PATH_SIZE_SCAN_DONE\n"
        )
        self.assertEqual(parsed, {path_a: 42, path_b: None})

    def test_device_put_file_script_matches_desktop_write_shape(self) -> None:
        script = backend._device_put_file_script("/db/test.json", b'{"ok": true}\r\n')
        self.assertIn('_f = open("/db/test.json", \'wb\')', script)
        self.assertIn('_f.write(b\'{"ok": true}\\r\\n\')', script)
        self.assertIn('_f.close()', script)
        self.assertIn('print("OK")', script)
        self.assertNotIn("with open(", script)

    def test_sync_device_relative_path_matches_desktop_upload_paths(self) -> None:
        self.assertEqual(backend._sync_device_relative_path("/db/test.json"), "db/test.json")
        self.assertEqual(backend._sync_device_relative_path("db/test.json"), "db/test.json")
        self.assertEqual(backend._sync_device_relative_path("/"), "")

    def test_sync_device_absolute_path_keeps_writes_rooted(self) -> None:
        self.assertEqual(backend._sync_device_absolute_path("/db/test.json"), "/db/test.json")
        self.assertEqual(backend._sync_device_absolute_path("db/test.json"), "/db/test.json")
        self.assertEqual(backend._sync_device_absolute_path("/"), "/")

    def test_local_file_signature_matches_fnv_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "x.txt"
            payload = b"abc123\n"
            path.write_bytes(payload)
            self.assertEqual(backend._compute_local_file_signature(path), backend._fnv1a32_bytes(payload))

    def test_parse_device_sizes_output_raises_on_missing_marker(self) -> None:
        with self.assertRaises(backend.ControllerError):
            backend._parse_device_sizes_output("Traceback (most recent call last): boom")

    def test_parse_device_signatures_output_raises_on_missing_marker(self) -> None:
        with self.assertRaises(backend.ControllerError):
            backend._parse_device_signatures_output("Traceback (most recent call last): boom")

    def test_exec_sync_script_can_reuse_open_raw_repl(self) -> None:
        class FakeController:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def exec_source(self, source: str, timeout_seconds: float) -> tuple[bytes, bytes]:
                self.calls.append(f"fresh:{timeout_seconds}:{source}")
                return b"fresh", b""

            def exec_source_in_raw_repl(self, source: str, timeout_seconds: float) -> tuple[bytes, bytes]:
                self.calls.append(f"raw:{timeout_seconds}:{source}")
                return b"raw", b""

        controller = FakeController()
        result = backend._exec_sync_script(controller, "print('x')", timeout_seconds=3.0, keep_raw_repl=True)

        self.assertEqual(result, "raw")
        self.assertEqual(controller.calls, ["raw:3.0:print('x')"])

    def test_sync_get_file_sizes_retries_once_after_timeout(self) -> None:
        class FakeController:
            def __init__(self) -> None:
                self.exec_calls = 0
                self.recover_calls = 0

            def sync_exec_raw_and_read(self, code: str, timeout: float = 5.0) -> str:
                self.exec_calls += 1
                self.code = code
                self.timeout = timeout
                if self.exec_calls == 1:
                    raise backend.ControllerError("Timeout waiting for raw REPL output")
                return "SIZES:{'/apps/main.py': 42}"

            def sync_enter_friendly_repl(self) -> None:
                self.recover_calls += 1

        controller = FakeController()
        sizes = backend.CalSciController.sync_get_file_sizes(controller, "/apps", timeout=25.0)

        self.assertEqual(sizes, {"/apps/main.py": 42})
        self.assertEqual(controller.exec_calls, 2)
        self.assertEqual(controller.recover_calls, 1)
        self.assertEqual(controller.timeout, 25.0)
        self.assertIn("os.ilistdir", controller.code)

    def test_sync_get_file_sizes_uses_stream_fallback_when_marker_missing(self) -> None:
        class FakeController:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def sync_exec_raw_and_read(self, code: str, timeout: float = 5.0) -> str:
                if "SIZE_SCAN_DONE" in code:
                    self.calls.append("stream")
                    return "SIZE:/apps/main.py:42\nSIZE_SCAN_DONE\n"
                self.calls.append("dict")
                return "{'/apps/main.py': 42}"

            def sync_enter_friendly_repl(self) -> None:
                pass

        controller = FakeController()
        sizes = backend.CalSciController.sync_get_file_sizes(controller, "/apps", timeout=25.0)

        self.assertEqual(sizes, {"/apps/main.py": 42})
        self.assertEqual(controller.calls, ["dict", "stream"])

    def test_sync_get_file_sizes_uses_friendly_scan_when_raw_entry_fails(self) -> None:
        class FakeController:
            def __init__(self) -> None:
                self.raw_calls = 0
                self.friendly_calls = 0
                self.recover_calls = 0

            def sync_exec_raw_and_read(self, code: str, timeout: float = 5.0) -> str:
                self.raw_calls += 1
                raise backend.ControllerError("could not enter raw REPL: banner")

            def sync_exec_friendly_and_read(self, code: str, timeout: float = 5.0) -> str:
                self.friendly_calls += 1
                return "SIZE:/apps/main.py:42\nSIZE_SCAN_DONE\n"

            def sync_enter_friendly_repl(self) -> None:
                self.recover_calls += 1

        controller = FakeController()
        sizes = backend.CalSciController.sync_get_file_sizes(controller, "/apps", timeout=25.0)

        self.assertEqual(sizes, {"/apps/main.py": 42})
        self.assertEqual(controller.raw_calls, 2)
        self.assertEqual(controller.recover_calls, 1)
        self.assertEqual(controller.friendly_calls, 1)

    def test_sync_get_file_signatures_retries_once_after_timeout(self) -> None:
        class FakeController:
            def __init__(self) -> None:
                self.exec_calls = 0
                self.recover_calls = 0

            def sync_exec_raw_and_read(self, code: str, timeout: float = 5.0) -> str:
                self.exec_calls += 1
                self.code = code
                self.timeout = timeout
                if self.exec_calls == 1:
                    raise backend.ControllerError("Timeout waiting for raw REPL output")
                return "SIGS:{'/apps/main.py': 'deadbeef'}"

            def sync_enter_friendly_repl(self) -> None:
                self.recover_calls += 1

        controller = FakeController()
        signatures = backend.CalSciController.sync_get_file_signatures(
            controller,
            ["/apps/main.py"],
            timeout=33.0,
        )

        self.assertEqual(signatures, {"/apps/main.py": "deadbeef"})
        self.assertEqual(controller.exec_calls, 2)
        self.assertEqual(controller.recover_calls, 1)
        self.assertEqual(controller.timeout, 33.0)
        self.assertIn("SIGS:", controller.code)

    def test_sync_get_file_signatures_uses_stream_fallback_when_marker_missing(self) -> None:
        class FakeController:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def sync_exec_raw_and_read(self, code: str, timeout: float = 5.0) -> str:
                if "SIG_SCAN_DONE" in code:
                    self.calls.append("stream")
                    path = "/apps/main.py"
                    return f"SIG:{len(path)}:{path}:1:deadbeef\nSIG_SCAN_DONE\n"
                self.calls.append("dict")
                return "{'/apps/main.py': 'deadbeef'}"

            def sync_enter_friendly_repl(self) -> None:
                pass

            def sync_exec_friendly_and_read(self, code: str, timeout: float = 5.0) -> str:
                raise AssertionError("friendly fallback should not be used in this scenario")

        controller = FakeController()
        signatures = backend.CalSciController.sync_get_file_signatures(
            controller,
            ["/apps/main.py"],
            timeout=33.0,
        )

        self.assertEqual(signatures, {"/apps/main.py": "deadbeef"})
        self.assertEqual(controller.calls, ["dict", "stream"])

    def test_sync_get_file_signatures_uses_friendly_scan_when_raw_entry_fails(self) -> None:
        class FakeController:
            def __init__(self) -> None:
                self.raw_calls = 0
                self.friendly_calls = 0
                self.recover_calls = 0

            def sync_exec_raw_and_read(self, code: str, timeout: float = 5.0) -> str:
                self.raw_calls += 1
                raise backend.ControllerError("could not enter raw REPL: banner")

            def sync_exec_friendly_and_read(self, code: str, timeout: float = 5.0) -> str:
                self.friendly_calls += 1
                path = "/apps/main.py"
                return f"SIG:{len(path)}:{path}:1:deadbeef\nSIG_SCAN_DONE\n"

            def sync_enter_friendly_repl(self) -> None:
                self.recover_calls += 1

        controller = FakeController()
        signatures = backend.CalSciController.sync_get_file_signatures(
            controller,
            ["/apps/main.py"],
            timeout=33.0,
        )

        self.assertEqual(signatures, {"/apps/main.py": "deadbeef"})
        self.assertEqual(controller.raw_calls, 2)
        self.assertEqual(controller.recover_calls, 1)
        self.assertEqual(controller.friendly_calls, 1)

    def test_sync_get_selected_file_sizes_uses_friendly_scan_when_raw_entry_fails(self) -> None:
        class FakeController:
            def __init__(self) -> None:
                self.raw_calls = 0
                self.friendly_calls = 0
                self.recover_calls = 0

            def sync_exec_raw_and_read(self, code: str, timeout: float = 5.0) -> str:
                self.raw_calls += 1
                raise backend.ControllerError("could not enter raw REPL: banner")

            def sync_exec_friendly_and_read(self, code: str, timeout: float = 5.0) -> str:
                self.friendly_calls += 1
                return "PATH_SIZES:{'/apps/main.py': 42, '/apps/missing.py': None}"

            def sync_enter_friendly_repl(self) -> None:
                self.recover_calls += 1

        controller = FakeController()
        sizes = backend.CalSciController.sync_get_selected_file_sizes(
            controller,
            ["/apps/main.py", "/apps/missing.py"],
            timeout=9.0,
        )

        self.assertEqual(sizes, {"/apps/main.py": 42, "/apps/missing.py": None})
        self.assertEqual(controller.raw_calls, 2)
        self.assertEqual(controller.recover_calls, 1)
        self.assertEqual(controller.friendly_calls, 1)

    def test_sync_get_selected_file_sizes_uses_stream_fallback_when_marker_missing(self) -> None:
        class FakeController:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def sync_exec_raw_and_read(self, code: str, timeout: float = 5.0) -> str:
                if "PATH_SIZE_SCAN_DONE" in code:
                    self.calls.append("stream")
                    path = "/apps/main.py"
                    return f"PATHSIZE:{len(path)}:{path}:1:42\nPATH_SIZE_SCAN_DONE\n"
                self.calls.append("dict")
                return "{'/apps/main.py': 42}"

            def sync_enter_friendly_repl(self) -> None:
                pass

            def sync_exec_friendly_and_read(self, code: str, timeout: float = 5.0) -> str:
                raise AssertionError("friendly fallback should not be used in this scenario")

        controller = FakeController()
        sizes = backend.CalSciController.sync_get_selected_file_sizes(
            controller,
            ["/apps/main.py"],
            timeout=9.0,
        )

        self.assertEqual(sizes, {"/apps/main.py": 42})
        self.assertEqual(controller.calls, ["dict", "stream"])

    def test_sync_get_selected_file_sizes_chunks_large_requests(self) -> None:
        class FakeController:
            def __init__(self) -> None:
                self.batch_sizes: list[int] = []

            def sync_exec_raw_and_read(self, code: str, timeout: float = 5.0) -> str:
                marker = "_paths = "
                marker_start = code.find(marker)
                if marker_start < 0:
                    raise AssertionError("missing _paths marker in targeted scan code")
                line_end = code.find("\n", marker_start)
                paths_literal = code[marker_start + len(marker) : line_end]
                batch_paths = ast.literal_eval(paths_literal)
                self.batch_sizes.append(len(batch_paths))
                return "PATH_SIZES:" + repr({path: 1 for path in batch_paths})

            def sync_enter_friendly_repl(self) -> None:
                pass

            def sync_exec_friendly_and_read(self, code: str, timeout: float = 5.0) -> str:
                raise AssertionError("friendly fallback should not be used in this scenario")

        controller = FakeController()
        remote_paths = [f"/apps/p{i}.py" for i in range(130)]
        sizes = backend.CalSciController.sync_get_selected_file_sizes(
            controller,
            remote_paths,
            timeout=9.0,
        )

        self.assertEqual(len(sizes), len(remote_paths))
        self.assertGreaterEqual(len(controller.batch_sizes), 3)
        self.assertTrue(all(size <= backend.SYNC_TARGETED_SCAN_BATCH_SIZE for size in controller.batch_sizes))

    def test_read_remote_file_sizes_delegates_to_sync_get_file_sizes(self) -> None:
        class FakeController:
            def __init__(self) -> None:
                self.calls: list[tuple[str, float]] = []

            def sync_get_file_sizes(self, remote_root: str, timeout: float = 0.0) -> dict[str, int]:
                self.calls.append((remote_root, timeout))
                return {"/main.py": 12}

        controller = FakeController()
        sizes = backend._read_remote_file_sizes(controller, "/")

        self.assertEqual(sizes, {"/main.py": 12})
        self.assertEqual(controller.calls, [("/", backend.SYNC_SCAN_COMMAND_TIMEOUT_SEC)])

    def test_sync_folder_signature_failure_falls_back_to_size_compare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            file_path = root / "main.py"
            file_path.write_text("print('ok')\n", encoding="utf-8")
            size_bytes = file_path.stat().st_size

            class FakeController:
                def __init__(self) -> None:
                    self.port = "COM_TEST"

                def sync_get_file_sizes(self, remote_root: str, timeout: float = 0.0) -> dict[str, int]:
                    return {"/main.py": size_bytes}

                def sync_get_file_signatures(self, remote_paths: list[str], timeout: float = 0.0) -> dict[str, str | None]:
                    raise backend.ControllerError("signature unavailable")

                def sync_delete_file(self, path: str) -> bool:
                    raise AssertionError("delete should not run when nothing changed")

                def sync_mkdir(self, path: str) -> bool:
                    raise AssertionError("mkdir should not run when nothing changed")

                def sync_enter_raw_repl(self) -> None:
                    raise AssertionError("upload should not start when nothing changed")

                def sync_put_raw(self, local_path: pathlib.Path, remote_path: str) -> None:
                    raise AssertionError("upload should not run when nothing changed")

                def sync_exit_raw_repl(self) -> None:
                    pass

            progress_lines: list[str] = []
            session = backend.PersistentSession(lambda _text: None, lambda _state: None, lambda _event: None)
            controller = FakeController()
            session._begin_exclusive_operation = lambda: (controller, False)  # type: ignore[method-assign]
            session._end_exclusive_operation = lambda _paused: None  # type: ignore[method-assign]

            result = session.sync_folder(
                port=None,
                local_folder=str(root),
                remote_folder="/",
                delete_extraneous=False,
                progress_callback=progress_lines.append,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["filesSynced"], 0)
            self.assertEqual(result["filesSkipped"], 1)
            self.assertIn("Falling back to size comparison.", "\n".join(progress_lines))

    def test_sync_folder_direct_verify_skips_false_positive_reupload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            file_path = root / "main.py"
            file_path.write_text("print('ok')\n", encoding="utf-8")
            size_bytes = file_path.stat().st_size

            class FakeController:
                def __init__(self) -> None:
                    self.port = "COM_TEST"
                    self.targeted_calls: list[list[str]] = []
                    self.raw_enter_calls = 0

                def sync_get_file_sizes(self, remote_root: str, timeout: float = 0.0) -> dict[str, int]:
                    return {}

                def sync_get_selected_file_sizes(
                    self,
                    remote_paths: list[str],
                    timeout: float = 0.0,
                ) -> dict[str, int | None]:
                    self.targeted_calls.append(list(remote_paths))
                    return {"/main.py": size_bytes}

                def sync_delete_file(self, path: str) -> bool:
                    raise AssertionError("delete should not run for upload-only sync")

                def sync_mkdir(self, path: str) -> bool:
                    raise AssertionError("mkdir should not run when upload is skipped")

                def sync_enter_raw_repl(self) -> None:
                    self.raw_enter_calls += 1

                def sync_put_raw(self, local_path: pathlib.Path, remote_path: str) -> None:
                    raise AssertionError("upload should be skipped after direct verify")

                def sync_exit_raw_repl(self) -> None:
                    pass

            progress_lines: list[str] = []
            session = backend.PersistentSession(lambda _text: None, lambda _state: None, lambda _event: None)
            controller = FakeController()
            session._begin_exclusive_operation = lambda: (controller, False)  # type: ignore[method-assign]
            session._end_exclusive_operation = lambda _paused: None  # type: ignore[method-assign]

            result = session.sync_folder(
                port=None,
                local_folder=str(root),
                remote_folder="/",
                delete_extraneous=False,
                progress_callback=progress_lines.append,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["filesSynced"], 0)
            self.assertEqual(result["filesSkipped"], 1)
            self.assertEqual(controller.targeted_calls, [["/main.py"]])
            self.assertEqual(controller.raw_enter_calls, 0)
            self.assertIn("Direct verify: checked 1 file(s), confirmed 1 present.", "\n".join(progress_lines))
            self.assertIn("Direct verify: 1 file(s) already present on device; skipping re-upload.", "\n".join(progress_lines))

    def test_sync_folder_upload_only_uses_fast_targeted_scan_for_large_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            local_sizes: dict[str, int] = {}
            missing_path = "/f39.py"
            for index in range(40):
                file_path = root / f"f{index}.py"
                file_path.write_text(f"print({index})\n", encoding="utf-8")
                local_sizes[f"/f{index}.py"] = file_path.stat().st_size

            class FakeController:
                def __init__(self) -> None:
                    self.port = "COM_TEST"
                    self.targeted_calls = 0
                    self.full_scan_calls = 0
                    self.upload_calls: list[str] = []

                def sync_get_selected_file_sizes(
                    self,
                    remote_paths: list[str],
                    timeout: float = 0.0,
                ) -> dict[str, int | None]:
                    self.targeted_calls += 1
                    return {
                        remote_path: (None if remote_path == missing_path else local_sizes[remote_path])
                        for remote_path in remote_paths
                    }

                def sync_get_file_sizes(self, remote_root: str, timeout: float = 0.0) -> dict[str, int]:
                    self.full_scan_calls += 1
                    raise AssertionError("full subtree scan should not run in upload-only fast scan mode")

                def sync_get_file_signatures(self, remote_paths: list[str], timeout: float = 0.0) -> dict[str, str | None]:
                    return {}

                def sync_delete_file(self, path: str) -> bool:
                    raise AssertionError("delete should not run for upload-only sync")

                def sync_mkdir(self, path: str) -> bool:
                    return True

                def sync_enter_raw_repl(self) -> None:
                    pass

                def sync_put_raw(self, local_path: pathlib.Path, remote_path: str) -> None:
                    self.upload_calls.append(remote_path)

                def sync_exit_raw_repl(self) -> None:
                    pass

            progress_lines: list[str] = []
            session = backend.PersistentSession(lambda _text: None, lambda _state: None, lambda _event: None)
            controller = FakeController()
            session._begin_exclusive_operation = lambda: (controller, False)  # type: ignore[method-assign]
            session._end_exclusive_operation = lambda _paused: None  # type: ignore[method-assign]

            result = session.sync_folder(
                port=None,
                local_folder=str(root),
                remote_folder="/",
                delete_extraneous=False,
                progress_callback=progress_lines.append,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["filesSynced"], 1)
            self.assertEqual(result["filesSkipped"], 39)
            self.assertEqual(controller.targeted_calls, 1)
            self.assertEqual(controller.full_scan_calls, 0)
            self.assertEqual(controller.upload_calls, ["f39.py"])
            self.assertIn("Upload-only fast scan: found 39 existing file(s), 1 missing.", "\n".join(progress_lines))
            self.assertNotIn("Direct verify:", "\n".join(progress_lines))

    def test_sync_folder_uploads_with_relative_device_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            file_path = root / "main.py"
            file_path.write_text("print('ok')\n", encoding="utf-8")

            class FakeController:
                def __init__(self) -> None:
                    self.port = "COM_TEST"
                    self.mkdir_calls: list[str] = []
                    self.upload_calls: list[str] = []

                def sync_get_file_sizes(self, remote_root: str, timeout: float = 0.0) -> dict[str, int]:
                    return {}

                def sync_get_file_signatures(self, remote_paths: list[str], timeout: float = 0.0) -> dict[str, str | None]:
                    return {}

                def sync_delete_file(self, path: str) -> bool:
                    raise AssertionError("delete should not run for upload-only sync")

                def sync_mkdir(self, path: str) -> bool:
                    self.mkdir_calls.append(path)
                    return True

                def sync_enter_raw_repl(self) -> None:
                    pass

                def sync_put_raw(self, local_path: pathlib.Path, remote_path: str) -> None:
                    self.upload_calls.append(remote_path)

                def sync_exit_raw_repl(self) -> None:
                    pass

            session = backend.PersistentSession(lambda _text: None, lambda _state: None, lambda _event: None)
            controller = FakeController()
            session._begin_exclusive_operation = lambda: (controller, False)  # type: ignore[method-assign]
            session._end_exclusive_operation = lambda _paused: None  # type: ignore[method-assign]

            result = session.sync_folder(
                port=None,
                local_folder=str(root),
                remote_folder="/apps",
                delete_extraneous=False,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(controller.mkdir_calls, ["apps"])
            self.assertEqual(controller.upload_calls, ["apps/main.py"])

    def test_sync_folder_directory_failure_becomes_warning_when_upload_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            file_path = root / "main.py"
            file_path.write_text("print('ok')\n", encoding="utf-8")

            class FakeController:
                def __init__(self) -> None:
                    self.port = "COM_TEST"
                    self.upload_calls: list[str] = []

                def sync_get_file_sizes(self, remote_root: str, timeout: float = 0.0) -> dict[str, int]:
                    return {}

                def sync_get_file_signatures(self, remote_paths: list[str], timeout: float = 0.0) -> dict[str, str | None]:
                    return {}

                def sync_delete_file(self, path: str) -> bool:
                    raise AssertionError("delete should not run for upload-only sync")

                def sync_mkdir(self, path: str) -> bool:
                    return False

                def sync_enter_raw_repl(self) -> None:
                    pass

                def sync_put_raw(self, local_path: pathlib.Path, remote_path: str) -> None:
                    self.upload_calls.append(remote_path)

                def sync_exit_raw_repl(self) -> None:
                    pass

            progress_lines: list[str] = []
            session = backend.PersistentSession(lambda _text: None, lambda _state: None, lambda _event: None)
            controller = FakeController()
            session._begin_exclusive_operation = lambda: (controller, False)  # type: ignore[method-assign]
            session._end_exclusive_operation = lambda _paused: None  # type: ignore[method-assign]

            result = session.sync_folder(
                port=None,
                local_folder=str(root),
                remote_folder="/apps",
                delete_extraneous=False,
                progress_callback=progress_lines.append,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["directoriesFailed"], 0)
            self.assertEqual(result["directoriesWarnings"], 1)
            self.assertEqual(controller.upload_calls, ["apps/main.py"])
            self.assertIn("Treating as warning", "\n".join(progress_lines))

    def test_sync_folder_reconnects_before_retrying_failed_upload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            file_path = root / "main.py"
            file_path.write_text("print('ok')\n", encoding="utf-8")

            class FakeController:
                def __init__(self) -> None:
                    self.port = "COM_TEST"
                    self.raw_enter_calls = 0
                    self.exit_calls = 0
                    self.reconnect_calls = 0
                    self.upload_calls: list[str] = []

                def sync_get_file_sizes(self, remote_root: str, timeout: float = 0.0) -> dict[str, int]:
                    return {}

                def sync_get_file_signatures(self, remote_paths: list[str], timeout: float = 0.0) -> dict[str, str | None]:
                    return {}

                def sync_delete_file(self, path: str) -> bool:
                    raise AssertionError("delete should not run for upload-only sync")

                def sync_mkdir(self, path: str) -> bool:
                    return True

                def sync_enter_raw_repl(self) -> None:
                    self.raw_enter_calls += 1

                def sync_exit_raw_repl(self) -> None:
                    self.exit_calls += 1

                def sync_reconnect(self, delay_seconds: float = 0.0) -> None:
                    self.reconnect_calls += 1

                def sync_put_raw(self, local_path: pathlib.Path, remote_path: str) -> None:
                    self.upload_calls.append(remote_path)
                    if len(self.upload_calls) == 1:
                        raise backend.ControllerError("serial write stalled")

            progress_lines: list[str] = []
            session = backend.PersistentSession(lambda _text: None, lambda _state: None, lambda _event: None)
            controller = FakeController()
            session._begin_exclusive_operation = lambda: (controller, False)  # type: ignore[method-assign]
            session._end_exclusive_operation = lambda _paused: None  # type: ignore[method-assign]

            result = session.sync_folder(
                port=None,
                local_folder=str(root),
                remote_folder="/",
                delete_extraneous=False,
                progress_callback=progress_lines.append,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["filesSynced"], 1)
            self.assertEqual(controller.reconnect_calls, 1)
            self.assertEqual(controller.raw_enter_calls, 2)
            self.assertEqual(controller.upload_calls, ["main.py", "main.py"])
            self.assertIn("after connection reset", "\n".join(progress_lines))

    def test_write_bytes_sends_full_payload_when_serial_write_is_partial(self) -> None:
        class FakeConn:
            def __init__(self) -> None:
                self.accepted = bytearray()
                self.flush_calls = 0

            def write(self, data: bytes) -> int:
                chunk = bytes(data)
                if not chunk:
                    return 0
                count = min(3, len(chunk))
                self.accepted.extend(chunk[:count])
                return count

            def flush(self) -> None:
                self.flush_calls += 1

        dummy = type("DummyController", (), {})()
        dummy._conn = FakeConn()
        dummy._write_lock = threading.Lock()

        backend.CalSciController._write_bytes(dummy, b"abcdef", flush=True)

        self.assertEqual(bytes(dummy._conn.accepted), b"abcdef")
        self.assertEqual(dummy._conn.flush_calls, 1)

    def test_sync_put_raw_raises_when_device_reports_traceback_in_stderr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_path = pathlib.Path(tmp) / "main.py"
            local_path.write_text("print('ok')\n", encoding="utf-8")

            class Dummy:
                def __init__(self) -> None:
                    self._in_raw_repl = True
                    self.exec_calls: list[str] = []

                def _sync_reset_input_buffer(self) -> None:
                    pass

                def _exec_raw_no_follow(self, source: str) -> None:
                    self.exec_calls.append(source)

                def _raw_follow(
                    self,
                    timeout: float | None,
                    line_callback=None,
                    cancel_event=None,
                ) -> tuple[bytes, bytes, bool]:
                    return (b"", b"Traceback (most recent call last):\nOSError: [Errno 2]\n", False)

            dummy = Dummy()
            with self.assertRaises(backend.ControllerError):
                backend.CalSciController.sync_put_raw(dummy, local_path, "apps/main.py")

            self.assertEqual(len(dummy.exec_calls), 1)
            self.assertIn('f = open("apps/main.py", "wb")', dummy.exec_calls[0])


if __name__ == "__main__":
    unittest.main()
