#!/usr/bin/env python3
"""CalSci backend service.

Service contract:
- The backend owns the serial port for any open session.
- `session.open` and `session.close` manage a persistent friendly-REPL session.
- `terminal.write` injects user input into that session.
- `hybrid.*` routes helper-path hybrid control and polling through that same session.
- `run-file` and `soft-reset` reuse the same session while serving over stdio.
- Standalone CLI commands keep the v1 one-shot open/run/close behavior.
"""

from __future__ import annotations

import argparse
import codecs
import json
import queue
import re
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import serial
from serial.tools import list_ports

try:
    import fcntl
    import termios
except ImportError:
    fcntl = None
    termios = None

CALSCI_PRODUCT = "CalSci"
DEFAULT_BAUDRATE = 115200
DEFAULT_RUN_TIMEOUT_SEC = 0
RUN_FAILURE_FRIENDLY_REPL_TIMEOUT_SEC = 2.5
RUN_FAILURE_SOFT_RESET_TIMEOUT_SEC = 12.0
RAW_REPL_CHUNK_BYTES = 256
RAW_REPL_CHUNK_DELAY_SEC = 0.01
RAW_REPL_ENTER_TIMEOUT_SEC = 6.0
RAW_REPL_EXIT_TIMEOUT_SEC = 2.0
RAW_REPL_CANCEL_TIMEOUT_SEC = 5.0
RAW_REPL_BANNER = b"raw REPL; CTRL-B to exit\r\n"
RAW_REPL_PROMPT = RAW_REPL_BANNER + b">"
PORT_OPEN_SETTLE_SEC = 0.12
READER_PAUSE_WAIT_SEC = 0.5
HYBRID_HELPER_COMMAND_TIMEOUT_SEC = 1.2
HYBRID_HELPER_ENABLE_TIMEOUT_SEC = 1.5
HYBRID_HELPER_POLL_TIMEOUT_SEC = 0.45
HYBRID_HELPER_POLL_INTERVAL_SEC = 0.025
HYBRID_HELPER_REPL_QUIET_SEC = 2.5
SOFT_RESET_BREAK_DELAY_SEC = 0.05
SOFT_RESET_TIMEOUT_FALLBACK_SEC = 2.5
SOFT_RESET_REBOOT_MARKERS = (
    b"soft reboot",
    b"CalSci - Triple Boot System",
    b"free ram initially=",
)
COMMAND_PRIORITY = {
    "session.open": 5,
    "terminal.write": 6,
    "hybrid.key": 7,
    "hybrid.sync-full": 8,
    "run-file": 10,
    "hybrid.start": 11,
    "hybrid.stop": 12,
    "soft-reset": 20,
    "session.close": 25,
    "session.state": 30,
    "hybrid.snapshot": 31,
    "scan": 90,
    "shutdown": 99,
}
BACKEND_WRITE_RETRIES = 3
EVENT_SESSION = "session"
EVENT_TERMINAL_OUTPUT = "terminal-output"
EVENT_HYBRID = "hybrid"
FRIENDLY_REPL_PROMPTS = (b"CalSci >>>", b">>>")


class ControllerError(RuntimeError):
    pass


class RunCancelledError(ControllerError):
    def __init__(self, output: bytes):
        super().__init__("Run cancelled by user")
        self.output = output


class RawLineSink:
    def __init__(self, emit: Callable[[str], None]):
        self._emit = emit
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buf.extend(chunk)
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                return
            line = bytes(self._buf[:idx]).decode("utf-8", errors="replace").rstrip("\r")
            del self._buf[: idx + 1]
            self._emit(line)

    def flush(self) -> None:
        if not self._buf:
            return
        line = bytes(self._buf).decode("utf-8", errors="replace").rstrip("\r")
        self._buf.clear()
        self._emit(line)


def _has_friendly_prompt(data: bytes) -> bool:
    tail = bytes(data[-128:])
    parts = re.split(br"[\r\n]+", tail)
    last_line = parts[-1] if parts else tail
    stripped = last_line.lstrip()
    return any(stripped.startswith(prompt) for prompt in FRIENDLY_REPL_PROMPTS)


def _join_non_empty_text(parts: list[str]) -> str:
    return "".join(part for part in parts if part)


def _extract_state_payloads(text: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    scan = 0
    expected_keys = {
        "frame_id",
        "fb",
        "fb_full",
        "fb_seen",
        "lines",
        "nav",
        "mode",
        "capture_enabled",
        "start_line",
        "all_points_on",
        "adc_reverse",
        "com_reverse",
        "invert",
    }
    while True:
        marker = text.find("STATE:", scan)
        if marker >= 0:
            left = text.find("{", marker)
        else:
            left = text.find("{", scan)
        if left < 0:
            return payloads
        try:
            payload, offset = decoder.raw_decode(text[left:])
        except Exception:
            payload = None
            offset = 0
        if isinstance(payload, dict) and (marker >= 0 or expected_keys.intersection(payload.keys())):
            payloads.append(payload)
            scan = left + offset
            continue
        scan = left + 1


def _clean_helper_line(line: str) -> str:
    cleaned = line.replace("\r", "").strip()
    cleaned = re.sub(r"^(?:CalSci >>>|>>>)\s*", "", cleaned)
    return cleaned.strip()


def _parse_helper_output(text: str, command: str | None = None) -> dict[str, Any]:
    states = _extract_state_payloads(text)
    lines: list[str] = []
    command_text = (command or "").strip()
    for raw_line in text.replace("\r", "\n").split("\n"):
        cleaned = _clean_helper_line(raw_line)
        if not cleaned:
            continue
        if command_text and cleaned == command_text:
            continue
        lines.append(cleaned)
    return {
        "text": text,
        "lines": lines,
        "states": states,
    }


def _is_prompt_only_fragment(text: str) -> bool:
    fragment = text.replace("\r", "").strip()
    if not fragment:
        return False
    cleaned = re.sub(r"^(?:CalSci >>>|>>>)\s*", "", fragment)
    if cleaned:
        return False
    return fragment.startswith("CalSci >>>") or fragment.startswith(">>>")


_HELPER_TERMINAL_PREFIXES = (
    "HYB_KEY_DEB_MS:",
    "HYB_GRAPH_FAST_MS:",
    "HYBRID_MODE:",
    "HYBRID_BRIDGE_ERR",
    "HYBRID_INIT_ERR",
    "HYBRID_SYNC_ERR",
    "HYBRID_STATUS_ERR",
    "HYBRID_KEY_ERR",
    "HYBRID_KEY_OK:",
    "HYBRID_MODE_ERR",
    "HYBRID_PING_ERR",
    "HYBRID_CONFIG_ERR",
    "HYBRID_PROTO:",
    "HYBRID_READY",
    "HYBRID_BAUD:",
)


def _looks_like_helper_terminal_line(line: str) -> bool:
    if not line:
        return False
    if line.startswith("_hyb_"):
        return True
    if line.startswith("ECHO:VSCODE_"):
        return True
    if line.startswith(_HELPER_TERMINAL_PREFIXES):
        return True
    return bool(_extract_state_payloads(line))


class CalSciController:
    def __init__(self, port: str, baudrate: int = DEFAULT_BAUDRATE, *, exclusive: bool = False):
        self.port = port
        self._conn = serial.Serial()
        self._conn.port = port
        self._conn.baudrate = baudrate
        self._conn.timeout = 0.01
        self._conn.write_timeout = 1.0
        try:
            self._conn.exclusive = exclusive
        except Exception:
            pass
        self._conn.dsrdtr = False
        self._conn.rtscts = False
        try:
            self._conn.dtr = False
            self._conn.rts = False
        except Exception:
            pass
        self._conn.open()
        self._enable_kernel_exclusive_lock(exclusive)
        time.sleep(PORT_OPEN_SETTLE_SEC)
        self._in_raw_repl = False
        self._write_lock = threading.Lock()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _enable_kernel_exclusive_lock(self, exclusive: bool) -> None:
        if not exclusive or fcntl is None or termios is None:
            return

        ioctl_code = getattr(termios, "TIOCEXCL", None)
        if ioctl_code is None:
            return

        try:
            fcntl.ioctl(self._conn.fileno(), ioctl_code)
        except OSError as exc:
            self.close()
            raise ControllerError(f"Could not exclusively lock port {self.port}: {exc}") from exc

    def write_terminal(self, data: bytes) -> None:
        if not data:
            return
        self._write_bytes(data, flush=True)

    def read_terminal_chunk(self) -> bytes:
        waiting = int(getattr(self._conn, "in_waiting", 0) or 0)
        return self._conn.read(waiting if waiting > 0 else 1)

    def drain_terminal_available(self) -> bytes:
        drained = bytearray()
        while True:
            waiting = int(getattr(self._conn, "in_waiting", 0) or 0)
            if waiting <= 0:
                break
            chunk = self._conn.read(waiting)
            if not chunk:
                break
            drained.extend(chunk)
            time.sleep(0.005)
        return bytes(drained)

    def _write_bytes(self, data: bytes, flush: bool = True) -> None:
        last_exc: Exception | None = None
        for attempt in range(BACKEND_WRITE_RETRIES):
            try:
                with self._write_lock:
                    self._conn.write(data)
                    if flush:
                        self._conn.flush()
                return
            except (serial.SerialException, serial.SerialTimeoutException) as exc:
                last_exc = exc
                time.sleep(0.04 * (attempt + 1))
        if last_exc is not None:
            raise last_exc

    def _drain_serial_input(self) -> None:
        try:
            self._conn.reset_input_buffer()
            self._conn.reset_output_buffer()
            return
        except Exception:
            pass

        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            chunk = self.read_terminal_chunk()
            if not chunk:
                break

    def _raw_read_exact(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + max(0.05, timeout)
        out = bytearray()
        while len(out) < size and time.monotonic() < deadline:
            chunk = self._conn.read(size - len(out))
            if chunk:
                out.extend(chunk)
                continue
            time.sleep(0.005)
        return bytes(out)

    def _raw_read_until(
        self,
        ending: bytes,
        timeout: float | None = 1.0,
        timeout_overall: float | None = None,
        data_consumer: Callable[[bytes], None] | None = None,
        cancel_event: threading.Event | None = None,
        cancel_handler: Callable[[], None] | None = None,
    ) -> bytes:
        data = bytearray()
        begin_overall = begin_char = time.monotonic()
        cancel_deadline: float | None = None
        cancel_triggered = False
        while True:
            if data.endswith(ending):
                return bytes(data)

            if cancel_event is not None and cancel_event.is_set() and not cancel_triggered:
                cancel_triggered = True
                cancel_deadline = time.monotonic() + RAW_REPL_CANCEL_TIMEOUT_SEC
                if cancel_handler is not None:
                    cancel_handler()

            chunk = self._conn.read(1)
            if chunk:
                if data_consumer is not None:
                    data_consumer(chunk)
                data.extend(chunk)
                begin_char = time.monotonic()
                continue

            now = time.monotonic()
            if timeout is not None and now >= begin_char + timeout:
                return bytes(data)
            if timeout_overall is not None and now >= begin_overall + timeout_overall:
                return bytes(data)
            if cancel_deadline is not None and now >= cancel_deadline:
                return bytes(data)
            time.sleep(0.005)

    def _enter_raw_repl(self, timeout_overall: float = RAW_REPL_ENTER_TIMEOUT_SEC) -> None:
        self._write_bytes(b"\r\x03", flush=True)
        time.sleep(0.05)
        self._drain_serial_input()

        self._write_bytes(b"\r\x01", flush=True)
        data = self._raw_read_until(RAW_REPL_BANNER, timeout=1.0, timeout_overall=timeout_overall)
        if not data.endswith(RAW_REPL_BANNER):
            raise ControllerError(f"could not enter raw REPL: {data!r}")

        prompt = self._raw_read_until(b">", timeout=0.5, timeout_overall=1.0)
        if not prompt.endswith(b">"):
            raise ControllerError(f"raw prompt missing after banner: {prompt!r}")

        self._in_raw_repl = True

    def _exit_raw_repl(self) -> None:
        self._write_bytes(b"\r\x02", flush=True)
        prompt_seen, _ = self._read_until_friendly_prompt(RAW_REPL_EXIT_TIMEOUT_SEC)
        if not prompt_seen:
            raise ControllerError("friendly REPL prompt missing after leaving raw REPL")
        self._in_raw_repl = False

    def _exec_raw_no_follow(self, source: str | bytes) -> None:
        source_bytes = source if isinstance(source, bytes) else source.encode("utf-8")
        for start in range(0, len(source_bytes), RAW_REPL_CHUNK_BYTES):
            chunk = source_bytes[start : start + RAW_REPL_CHUNK_BYTES]
            self._write_bytes(chunk, flush=False)
            time.sleep(RAW_REPL_CHUNK_DELAY_SEC)

        self._write_bytes(b"\x04", flush=True)
        response = self._raw_read_exact(2, timeout=1.0)
        if response != b"OK":
            raise ControllerError(f"could not exec command (response: {response!r})")

    def _raw_follow(
        self,
        timeout: float | None,
        line_callback: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bytes, bytes, bool]:
        sink = RawLineSink(line_callback) if line_callback is not None else None
        interrupted = False
        read_timeout = timeout if timeout is not None and timeout > 0 else None

        def feed_stdout(chunk: bytes) -> None:
            if sink is None or chunk == b"\x04":
                return
            sink.feed(chunk)

        def interrupt_running_code() -> None:
            nonlocal interrupted
            if interrupted:
                return
            interrupted = True
            self._write_bytes(b"\x03", flush=True)

        normal = self._raw_read_until(
            b"\x04",
            timeout=read_timeout,
            timeout_overall=read_timeout,
            data_consumer=feed_stdout if sink is not None else None,
            cancel_event=cancel_event,
            cancel_handler=interrupt_running_code,
        )
        if not normal.endswith(b"\x04"):
            if interrupted:
                raise ControllerError("Run cancel did not reach raw REPL stdout terminator")
            if read_timeout is None:
                raise ControllerError("raw REPL stdout terminator missing after command")
            raise ControllerError(f"run timed out after {read_timeout:g}s waiting for raw REPL stdout terminator")
        normal = normal[:-1]
        if sink is not None:
            sink.flush()

        post_timeout = RAW_REPL_CANCEL_TIMEOUT_SEC if interrupted else read_timeout
        error = self._raw_read_until(b"\x04", timeout=post_timeout, timeout_overall=post_timeout)
        if not error.endswith(b"\x04"):
            if interrupted:
                raise ControllerError("Run cancel did not reach raw REPL stderr terminator")
            if post_timeout is None:
                raise ControllerError("raw REPL stderr terminator missing after command")
            raise ControllerError(f"run timed out after {post_timeout:g}s waiting for raw REPL stderr terminator")
        error = error[:-1]

        prompt_timeout = RAW_REPL_CANCEL_TIMEOUT_SEC if interrupted else 1.0
        prompt = self._raw_read_until(b">", timeout=prompt_timeout, timeout_overall=prompt_timeout)
        if not prompt.endswith(b">"):
            raise ControllerError("raw REPL prompt missing after command")
        return normal, error, interrupted

    def _read_until_friendly_prompt(self, timeout_seconds: float) -> tuple[bool, bytes]:
        output_chunks: list[bytes] = []
        deadline = time.monotonic() + max(0.2, timeout_seconds)

        while time.monotonic() < deadline:
            chunk = self.read_terminal_chunk()
            if not chunk:
                time.sleep(0.05)
                continue
            output_chunks.append(chunk)
            if _has_friendly_prompt(b"".join(output_chunks[-8:])):
                return True, b"".join(output_chunks)

        return False, b"".join(output_chunks)

    def exec_friendly_helper(self, command: str, timeout_seconds: float) -> dict[str, bytes]:
        pending = self.drain_terminal_available()
        self._write_bytes(command.encode("utf-8") + b"\r", flush=True)
        prompt_seen, output = self._read_until_friendly_prompt(timeout_seconds)
        if not prompt_seen:
            detail = output.decode("utf-8", errors="replace").strip()
            if detail:
                raise ControllerError(f"friendly REPL prompt missing after helper command: {detail}")
            raise ControllerError("friendly REPL prompt missing after helper command")
        return {
            "pending": pending,
            "output": output,
        }

    def exec_source(
        self,
        source: str,
        timeout_seconds: float,
        line_callback: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bytes, bytes]:
        self._enter_raw_repl()
        try:
            self._exec_raw_no_follow(source)
            output, error, interrupted = self._raw_follow(
                timeout_seconds if timeout_seconds > 0 else None,
                line_callback=line_callback,
                cancel_event=cancel_event,
            )
        except Exception:
            # On command failure or timeout the device may still be running user code,
            # so raw->friendly recovery is handled by the outer recovery path.
            self._in_raw_repl = False
            raise

        try:
            self._exit_raw_repl()
        finally:
            self._in_raw_repl = False

        if interrupted:
            raise RunCancelledError(output)

        return output, error

    def recover_friendly_repl(self, timeout_seconds: float) -> dict[str, Any]:
        self._drain_serial_input()
        self._write_bytes(b"\x03\x03", flush=True)
        time.sleep(SOFT_RESET_BREAK_DELAY_SEC)
        self._write_bytes(b"\r\x02\r", flush=True)
        prompt_seen, output_bytes = self._read_until_friendly_prompt(timeout_seconds)
        output = output_bytes.decode("utf-8", errors="replace")
        payload = {
            "ok": prompt_seen,
            "promptSeen": prompt_seen,
            "port": self.port,
            "output": output,
        }
        if not prompt_seen:
            payload["error"] = "Friendly REPL prompt not detected after run recovery."
        return payload

    def soft_reset(self, timeout_seconds: float) -> dict[str, Any]:
        output_chunks: list[bytes] = []
        prompt_seen = False
        reboot_seen = False

        def collect(deadline: float) -> None:
            nonlocal prompt_seen, reboot_seen
            while time.monotonic() < deadline:
                chunk = self.read_terminal_chunk()
                if not chunk:
                    time.sleep(0.05)
                    continue
                output_chunks.append(chunk)
                merged = b"".join(output_chunks[-8:])
                if any(marker in merged for marker in SOFT_RESET_REBOOT_MARKERS):
                    reboot_seen = True
                if _has_friendly_prompt(merged):
                    prompt_seen = True
                    reboot_seen = True
                    return

        self._drain_serial_input()
        self._write_bytes(b"\x03\x03", flush=True)
        time.sleep(SOFT_RESET_BREAK_DELAY_SEC)
        self._drain_serial_input()
        self._write_bytes(b"\x04", flush=True)
        collect(time.monotonic() + max(0.2, timeout_seconds))

        if not reboot_seen and not prompt_seen:
            self._write_bytes(b"\x03\x03", flush=True)
            time.sleep(0.05)
            self._write_bytes(b"\x02", flush=True)
            time.sleep(0.03)
            self._write_bytes(b"\x04", flush=True)
            collect(time.monotonic() + SOFT_RESET_TIMEOUT_FALLBACK_SEC)

        return {
            "ok": bool(prompt_seen or reboot_seen),
            "promptSeen": prompt_seen,
            "rebootSeen": reboot_seen,
            "port": self.port,
            "output": b"".join(output_chunks).decode("utf-8", errors="replace"),
        }


class PersistentSession:
    def __init__(
        self,
        emit_terminal_text: Callable[[str], None],
        emit_session_state: Callable[[dict[str, Any]], None],
        emit_hybrid_event: Callable[[dict[str, Any]], None],
    ):
        self._emit_terminal_text = emit_terminal_text
        self._emit_session_state = emit_session_state
        self._emit_hybrid_event = emit_hybrid_event
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._controller: CalSciController | None = None
        self._port: str | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._reader_pause_requested = threading.Event()
        self._reader_paused = threading.Event()
        self._hybrid_lock = threading.RLock()
        self._hybrid_thread: threading.Thread | None = None
        self._hybrid_stop = threading.Event()
        self._hybrid_force_full = threading.Event()
        self._hybrid_active = False
        self._hybrid_state: dict[str, Any] = {}
        self._hybrid_key_debounce_ms: int | None = None
        self._hybrid_graph_fast_ms: int | None = None
        self._hybrid_last_error: str | None = None
        self._hybrid_repl_quiet_until = 0.0
        self._hybrid_pause_until_prompt = False
        self._hybrid_poll_pending = False
        self._hybrid_poll_sent_at = 0.0
        self._helper_condition = threading.Condition()
        self._helper_line_buffer = ""
        self._helper_lines: deque[tuple[int, str]] = deque(maxlen=256)
        self._helper_line_seq = 0
        self._helper_state_seq = 0
        self._suppress_terminal_helper_output = False
        self._suppress_terminal_helper_output_deadline = 0.0
        self._suppress_terminal_helper_activity_seen = False

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._build_state_locked()

    def open(self, port: str) -> dict[str, Any]:
        if not port:
            return {"ok": False, "connected": False, "port": None, "error": "No port provided."}

        with self._lock:
            if self._controller is not None and self._port == port:
                return {"ok": True, **self._build_state_locked()}

        self.close(emit_event=False, reason="switching")

        try:
            controller = CalSciController(port, exclusive=True)
        except Exception as exc:
            error = str(exc)
            self._emit_session_state_event(error=error, reason="open-failed")
            return {"ok": False, "connected": False, "port": None, "error": error}

        with self._lock:
            self._attach_session_locked(controller)
            payload = {"ok": True, **self._build_state_locked()}

        self._emit_session_state_event(reason="opened")
        self._emit_hybrid_status_event(reason="session-opened")
        return payload

    def close(self, emit_event: bool = True, reason: str = "closed") -> dict[str, Any]:
        self.hybrid_stop(reason=reason, disable_mode=False)
        detached = self._detach_session()
        self._teardown_detached(detached)
        payload = {"ok": True, "connected": False, "port": None}
        if emit_event:
            self._emit_session_state_event(reason=reason)
            self._emit_hybrid_status_event(reason=reason)
        return payload

    def terminal_write(self, data: str) -> dict[str, Any]:
        if not data:
            return {"ok": True}

        with self._lock:
            controller = self._controller
        if controller is None:
            return {"ok": False, "error": "No open CalSci session."}

        try:
            with self._operation_lock:
                if self._hybrid_active:
                    self._hybrid_repl_quiet_until = time.monotonic() + HYBRID_HELPER_REPL_QUIET_SEC
                    self._hybrid_pause_until_prompt = True
                controller.write_terminal(data.encode("utf-8"))
            return {"ok": True}
        except Exception as exc:
            self._handle_reader_failure(controller, str(exc))
            return {"ok": False, "error": str(exc)}

    def soft_reset(self, port: str | None, timeout_seconds: float) -> dict[str, Any]:
        if port:
            opened = self.open(port)
            if not opened.get("ok"):
                return {
                    "ok": False,
                    "promptSeen": False,
                    "rebootSeen": False,
                    "port": port,
                    "output": "",
                    "error": opened.get("error", "Failed to open session."),
                }

        with self._operation_lock:
            try:
                controller, pause_requested = self._begin_exclusive_operation()
            except Exception as exc:
                return {
                    "ok": False,
                    "promptSeen": False,
                    "rebootSeen": False,
                    "port": port or "",
                    "output": "",
                    "error": str(exc),
                }

            try:
                payload = controller.soft_reset(timeout_seconds)
            except Exception as exc:
                payload = {
                    "ok": False,
                    "promptSeen": False,
                    "rebootSeen": False,
                    "port": controller.port,
                    "output": "",
                    "error": str(exc),
                }
            finally:
                self._end_exclusive_operation(pause_requested)

        if payload.get("output"):
            self._emit_terminal_text(str(payload["output"]))
        return payload

    def run_file(
        self,
        port: str | None,
        local_file: str,
        timeout_seconds: float,
        stdout_line_callback: Callable[[str], None] | None = None,
        stderr_line_callback: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        try:
            local_path, source = _load_local_text_file(local_file)
        except Exception as exc:
            return {
                "ok": False,
                "port": port or "",
                "localFile": str(Path(local_file).expanduser().resolve()),
                "output": "",
                "error": str(exc),
            }

        if port:
            opened = self.open(port)
            if not opened.get("ok"):
                return {
                    "ok": False,
                    "port": port,
                    "localFile": str(local_path),
                    "output": "",
                    "error": opened.get("error", "Failed to open session."),
                }

        with self._operation_lock:
            try:
                controller, pause_requested = self._begin_exclusive_operation()
            except Exception as exc:
                return {
                    "ok": False,
                    "port": port or "",
                    "localFile": str(local_path),
                    "output": "",
                    "error": str(exc),
                }

            payload: dict[str, Any]
            recovery_payload: dict[str, Any] | None = None
            try:
                try:
                    stdout_bytes, stderr_bytes = controller.exec_source(
                        source,
                        timeout_seconds,
                        line_callback=stdout_line_callback,
                        cancel_event=cancel_event,
                    )
                    output = stdout_bytes.decode("utf-8", errors="replace")
                    error_text = stderr_bytes.decode("utf-8", errors="replace").strip()

                    if error_text:
                        if stderr_line_callback is not None:
                            for line in error_text.splitlines():
                                stderr_line_callback(line)
                        payload = {
                            "ok": False,
                            "port": controller.port,
                            "localFile": str(local_path),
                            "output": output,
                            "error": error_text,
                        }
                    else:
                        payload = {
                            "ok": True,
                            "port": controller.port,
                            "localFile": str(local_path),
                            "output": output,
                        }
                except RunCancelledError as exc:
                    payload = {
                        "ok": False,
                        "cancelled": True,
                        "port": controller.port,
                        "localFile": str(local_path),
                        "output": exc.output.decode("utf-8", errors="replace"),
                        "error": "Run cancelled by user",
                    }
                except Exception as exc:
                    payload = {
                        "ok": False,
                        "port": controller.port,
                        "localFile": str(local_path),
                        "output": "",
                        "error": str(exc),
                    }
                    recovery_payload = _recover_after_run_failure(controller)
            finally:
                self._end_exclusive_operation(pause_requested)

        if recovery_payload and recovery_payload.get("output"):
            self._emit_terminal_text(str(recovery_payload["output"]))

        if recovery_payload and not recovery_payload.get("ok"):
            payload["ok"] = False
            existing_error = payload.get("error")
            restore_error = recovery_payload.get("error") or "Failed to recover friendly REPL after run"
            if existing_error:
                payload["error"] = f"{existing_error} | restore failed: {restore_error}"
            else:
                payload["error"] = f"restore failed: {restore_error}"

        if recovery_payload is not None:
            payload["restoreDetail"] = {
                "ok": bool(recovery_payload.get("ok")),
                "port": payload.get("port", port or ""),
                "recovery": recovery_payload,
            }
        return payload

    def hybrid_snapshot(self) -> dict[str, Any]:
        with self._hybrid_lock:
            return {
                "ok": True,
                "status": self._build_hybrid_status_locked(),
                "state": dict(self._hybrid_state),
            }

    def hybrid_start(self, port: str | None = None) -> dict[str, Any]:
        if port:
            opened = self.open(port)
            if not opened.get("ok"):
                return {
                    "ok": False,
                    "status": self._build_hybrid_status(),
                    "state": dict(self._hybrid_state),
                    "error": opened.get("error", "Failed to open session."),
                }

        with self._lock:
            if self._controller is None:
                return {
                    "ok": False,
                    "status": self._build_hybrid_status(),
                    "state": dict(self._hybrid_state),
                    "error": "No open CalSci session.",
                }

        token = f"VSCODE_{int(time.time() * 1000)}"
        try:
            with self._operation_lock:
                line_seq, _ = self._send_helper_command_locked(
                    f'_hyb_ping("{token}") if "_hyb_ping" in globals() else print("HYBRID_PING_ERR:HELPER_MISSING")'
                )
                if self._wait_for_helper_marker(f"ECHO:{token}", after_line_seq=line_seq, timeout_seconds=HYBRID_HELPER_COMMAND_TIMEOUT_SEC) is None:
                    raise ControllerError("Hybrid helper ping did not echo back.")

                self._send_helper_command_locked(
                    '_hyb_emit_hybrid_config() if "_hyb_emit_hybrid_config" in globals() else print("HYBRID_CONFIG_ERR:HELPER_MISSING")'
                )
                line_seq, _ = self._send_helper_command_locked(
                    '_hyb_mode(True) if "_hyb_mode" in globals() else print("HYBRID_MODE_ERR:HELPER_MISSING")'
                )
                if self._wait_for_helper_marker("HYBRID_MODE:ON", after_line_seq=line_seq, timeout_seconds=HYBRID_HELPER_ENABLE_TIMEOUT_SEC) is None:
                    raise ControllerError("Hybrid mode did not enable cleanly.")

                with self._hybrid_lock:
                    self._hybrid_active = True
                    self._hybrid_last_error = None
                    self._hybrid_force_full.clear()
                    self._hybrid_stop.clear()

                _, state_seq = self._send_helper_command_locked(
                    '_hyb_sync_full() if "_hyb_sync_full" in globals() else print("HYBRID_SYNC_ERR:HELPER_MISSING")',
                    mark_poll=True,
                )
                if not self._wait_for_state_change(after_state_seq=state_seq, timeout_seconds=HYBRID_HELPER_ENABLE_TIMEOUT_SEC):
                    raise ControllerError("Hybrid sync did not produce a STATE payload.")
        except Exception as exc:
            self._set_hybrid_inactive(error=str(exc), reason="start-failed")
            snapshot = self.hybrid_snapshot()
            snapshot["ok"] = False
            snapshot["error"] = str(exc)
            return snapshot

        with self._hybrid_lock:
            self._ensure_hybrid_thread_locked()

        self._emit_hybrid_status_event(reason="started")
        snapshot = self.hybrid_snapshot()
        snapshot["ok"] = True
        return snapshot

    def hybrid_stop(self, reason: str = "stopped", disable_mode: bool = True) -> dict[str, Any]:
        with self._hybrid_lock:
            was_active = self._hybrid_active
            thread = self._hybrid_thread
            self._hybrid_active = False
            self._hybrid_pause_until_prompt = False
            self._hybrid_stop.set()

        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)

        disable_error: str | None = None
        if disable_mode:
            with self._lock:
                controller = self._controller
            if controller is not None:
                try:
                    with self._operation_lock:
                        self._send_helper_command_locked(
                            '_hyb_mode(False) if "_hyb_mode" in globals() else print("HYBRID_MODE_ERR:HELPER_MISSING")'
                        )
                except Exception as exc:
                    disable_error = str(exc)

        with self._hybrid_lock:
            self._hybrid_thread = None
            self._hybrid_force_full.clear()
            if not was_active and disable_error is None:
                self._hybrid_last_error = None

        self._emit_hybrid_status_event(reason=reason, error=disable_error)
        snapshot = self.hybrid_snapshot()
        snapshot["ok"] = disable_error is None
        if disable_error is not None:
            snapshot["error"] = disable_error
        return snapshot

    def hybrid_sync_full(self) -> dict[str, Any]:
        with self._lock:
            if self._controller is None:
                return {"ok": False, "error": "No open CalSci session."}

        try:
            with self._operation_lock:
                _, state_seq = self._send_helper_command_locked(
                    '_hyb_sync_full() if "_hyb_sync_full" in globals() else print("HYBRID_SYNC_ERR:HELPER_MISSING")',
                    mark_poll=True,
                )
                if not self._wait_for_state_change(after_state_seq=state_seq, timeout_seconds=HYBRID_HELPER_ENABLE_TIMEOUT_SEC):
                    raise ControllerError("Hybrid sync did not produce a STATE payload.")
        except Exception as exc:
            self._emit_hybrid_status_event(reason="sync-failed", error=str(exc))
            return {"ok": False, "error": str(exc)}

        snapshot = self.hybrid_snapshot()
        snapshot["ok"] = True
        return snapshot

    def hybrid_key(self, col: int, row: int) -> dict[str, Any]:
        with self._lock:
            if self._controller is None:
                return {"ok": False, "error": "No open CalSci session."}

        try:
            with self._operation_lock:
                line_seq, _ = self._send_helper_command_locked(
                    f'_hyb_key({int(col)},{int(row)}) if "_hyb_key" in globals() else print("HYBRID_KEY_ERR:HELPER_MISSING")'
                )
        except Exception as exc:
            self._emit_hybrid_status_event(reason="key-failed", error=str(exc))
            return {"ok": False, "error": str(exc)}

        ack = self._wait_for_any_helper_line(
            after_line_seq=line_seq,
            timeout_seconds=HYBRID_HELPER_COMMAND_TIMEOUT_SEC,
            prefixes=("HYBRID_KEY_OK:", "HYBRID_KEY_ERR:"),
        )
        error_line = ack if ack and ack.startswith("HYBRID_KEY_ERR:") else None
        if error_line:
            return {"ok": False, "error": error_line}
        if ack:
            return {"ok": True, "ack": ack}
        return {"ok": True}

    def _ensure_hybrid_thread_locked(self) -> None:
        if self._hybrid_thread is not None and self._hybrid_thread.is_alive():
            return
        self._hybrid_thread = threading.Thread(
            target=self._hybrid_loop,
            daemon=True,
            name=f"CalSciHybridPoll[{self._port or 'unknown'}]",
        )
        self._hybrid_thread.start()

    def _hybrid_loop(self) -> None:
        while not self._hybrid_stop.wait(HYBRID_HELPER_POLL_INTERVAL_SEC):
            with self._hybrid_lock:
                if not self._hybrid_active:
                    return
                if self._hybrid_pause_until_prompt:
                    continue
            if time.monotonic() < self._hybrid_repl_quiet_until:
                continue

            force_full = self._hybrid_force_full.is_set()
            with self._lock:
                controller = self._controller
            if controller is None:
                self._set_hybrid_inactive(error="CalSci session closed.", reason="session-closed")
                return

            command = (
                '_hyb_sync_full() if "_hyb_sync_full" in globals() else print("HYBRID_SYNC_ERR:HELPER_MISSING")'
                if force_full
                else '_hyb_poll_state(%d) if "_hyb_poll_state" in globals() else print("HYBRID_SYNC_ERR:HELPER_MISSING")'
                % int(self._hybrid_state.get("frame_id", -1))
            )

            try:
                with self._operation_lock:
                    _, state_seq = self._send_helper_command_locked(command, mark_poll=True)
                    timeout = HYBRID_HELPER_ENABLE_TIMEOUT_SEC if force_full else HYBRID_HELPER_POLL_TIMEOUT_SEC
                    if not self._wait_for_state_change(after_state_seq=state_seq, timeout_seconds=timeout):
                        with self._hybrid_lock:
                            if self._hybrid_poll_pending and (time.monotonic() - self._hybrid_poll_sent_at) < timeout:
                                continue
                            self._hybrid_poll_pending = False
            except Exception as exc:
                self._set_hybrid_inactive(error=str(exc), reason="poll-failed")
                return

            if force_full:
                self._hybrid_force_full.clear()

    def _send_helper_command_locked(self, command: str, mark_poll: bool = False) -> tuple[int, int]:
        with self._lock:
            controller = self._controller
        if controller is None:
            raise ControllerError("No open CalSci session.")

        with self._helper_condition:
            line_seq = self._helper_line_seq
            state_seq = self._helper_state_seq
            self._suppress_terminal_helper_output = True
            self._suppress_terminal_helper_output_deadline = time.monotonic() + (
                max(
                    HYBRID_HELPER_COMMAND_TIMEOUT_SEC,
                    HYBRID_HELPER_ENABLE_TIMEOUT_SEC,
                    HYBRID_HELPER_POLL_TIMEOUT_SEC,
                )
                + 1.0
            )
            self._suppress_terminal_helper_activity_seen = False

        if mark_poll:
            with self._hybrid_lock:
                self._hybrid_poll_pending = True
                self._hybrid_poll_sent_at = time.monotonic()

        controller.write_terminal((command + "\r").encode("utf-8"))
        return line_seq, state_seq

    def _wait_for_helper_marker(self, marker: str, after_line_seq: int, timeout_seconds: float) -> str | None:
        deadline = time.monotonic() + max(0.05, timeout_seconds)
        while True:
            with self._helper_condition:
                for seq, line in self._helper_lines:
                    if seq <= after_line_seq:
                        continue
                    if marker in line:
                        return line
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._helper_condition.wait(timeout=remaining)

    def _wait_for_any_helper_line(
        self,
        *,
        after_line_seq: int,
        timeout_seconds: float,
        prefixes: tuple[str, ...],
    ) -> str | None:
        deadline = time.monotonic() + max(0.05, timeout_seconds)
        while True:
            with self._helper_condition:
                for seq, line in self._helper_lines:
                    if seq <= after_line_seq:
                        continue
                    if any(line.startswith(prefix) for prefix in prefixes):
                        return line
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._helper_condition.wait(timeout=remaining)

    def _wait_for_state_change(self, *, after_state_seq: int, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + max(0.05, timeout_seconds)
        while True:
            with self._helper_condition:
                if self._helper_state_seq > after_state_seq:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._helper_condition.wait(timeout=remaining)

    def _process_terminal_text(self, text: str) -> None:
        if not text:
            return

        status_events: list[dict[str, Any]] = []
        state_events: list[dict[str, Any]] = []
        visible_chunks: list[str] = []

        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        with self._helper_condition:
            suppress_helper_output = self._suppress_terminal_helper_output
            helper_activity_seen = self._suppress_terminal_helper_activity_seen
            if suppress_helper_output and time.monotonic() >= self._suppress_terminal_helper_output_deadline:
                self._suppress_terminal_helper_output = False
                self._suppress_terminal_helper_output_deadline = 0.0
                self._suppress_terminal_helper_activity_seen = False
                suppress_helper_output = False
            started_suppressed = suppress_helper_output

            self._helper_line_buffer += normalized
            while True:
                newline = self._helper_line_buffer.find("\n")
                if newline < 0:
                    break
                raw_line = self._helper_line_buffer[:newline]
                self._helper_line_buffer = self._helper_line_buffer[newline + 1 :]
                cleaned = _clean_helper_line(raw_line)
                if not cleaned:
                    if _is_prompt_only_fragment(raw_line):
                        with self._hybrid_lock:
                            self._hybrid_pause_until_prompt = False
                    if started_suppressed and _is_prompt_only_fragment(raw_line):
                        if helper_activity_seen:
                            self._suppress_terminal_helper_output = False
                            self._suppress_terminal_helper_output_deadline = 0.0
                            self._suppress_terminal_helper_activity_seen = False
                            suppress_helper_output = False
                    elif started_suppressed and raw_line.strip():
                        visible_chunks.append(raw_line + "\n")
                    continue

                self._helper_line_seq += 1
                self._helper_lines.append((self._helper_line_seq, cleaned))

                status_payloads, state_payloads = self._process_helper_line_locked(cleaned)
                status_events.extend(status_payloads)
                state_events.extend(state_payloads)

                if suppress_helper_output and _looks_like_helper_terminal_line(cleaned):
                    helper_activity_seen = True
                    self._suppress_terminal_helper_activity_seen = True
                    continue
                if started_suppressed and raw_line.strip():
                    visible_chunks.append(raw_line + "\n")

            if self._helper_line_buffer and _is_prompt_only_fragment(self._helper_line_buffer):
                with self._hybrid_lock:
                    self._hybrid_pause_until_prompt = False
                if suppress_helper_output:
                    self._helper_line_buffer = ""
                    if helper_activity_seen:
                        self._suppress_terminal_helper_output = False
                        self._suppress_terminal_helper_output_deadline = 0.0
                        self._suppress_terminal_helper_activity_seen = False

            self._helper_condition.notify_all()

        if started_suppressed:
            if visible_chunks:
                self._emit_terminal_text("".join(visible_chunks))
        else:
            self._emit_terminal_text(text)

        for payload in status_events:
            self._emit_hybrid_event(payload)
        for payload in state_events:
            self._emit_hybrid_event(payload)

    def _process_helper_line_locked(self, line: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        status_events: list[dict[str, Any]] = []
        state_events: list[dict[str, Any]] = []
        status_changed = False
        error_text: str | None = None

        if line.startswith("HYB_KEY_DEB_MS:"):
            try:
                value = int(line.split(":", 1)[1].strip())
            except Exception:
                value = None
            if value and value > 0:
                with self._hybrid_lock:
                    if self._hybrid_key_debounce_ms != value:
                        self._hybrid_key_debounce_ms = value
                        status_changed = True
        elif line.startswith("HYB_GRAPH_FAST_MS:"):
            try:
                value = int(line.split(":", 1)[1].strip())
            except Exception:
                value = None
            if value and value > 0:
                with self._hybrid_lock:
                    if self._hybrid_graph_fast_ms != value:
                        self._hybrid_graph_fast_ms = value
                        status_changed = True
        elif line.startswith("HYBRID_MODE:"):
            mode_on = line.endswith("ON")
            with self._hybrid_lock:
                if self._hybrid_state.get("mode") != mode_on:
                    self._hybrid_state["mode"] = mode_on
                    status_changed = True
        elif line.startswith(
            (
                "HYBRID_BRIDGE_ERR",
                "HYBRID_INIT_ERR",
                "HYBRID_SYNC_ERR",
                "HYBRID_STATUS_ERR",
                "HYBRID_KEY_ERR",
                "HYBRID_MODE_ERR",
                "HYBRID_PING_ERR",
                "HYBRID_CONFIG_ERR",
            )
        ):
            error_text = line

        for state in _extract_state_payloads(line):
            update = self._merge_hybrid_state(state)
            with self._hybrid_lock:
                self._hybrid_poll_pending = False
            self._helper_state_seq += 1
            state_events.append({"type": "state", "state": update})

        if error_text is not None:
            with self._hybrid_lock:
                self._hybrid_last_error = error_text
            status_events.append(self._build_hybrid_status_event_payload(reason="helper-error", error=error_text))
        elif status_changed:
            with self._hybrid_lock:
                self._hybrid_last_error = None
            status_events.append(self._build_hybrid_status_event_payload(reason="updated"))

        return status_events, state_events

    def _run_helper_command_locked(self, command: str, timeout_seconds: float) -> dict[str, Any]:
        controller, pause_requested = self._begin_exclusive_operation()
        try:
            payload = controller.exec_friendly_helper(command, timeout_seconds)
        finally:
            self._end_exclusive_operation(pause_requested)

        pending_bytes = payload.get("pending", b"")
        if pending_bytes:
            self._emit_terminal_text(pending_bytes.decode("utf-8", errors="replace"))
        output_text = payload.get("output", b"").decode("utf-8", errors="replace")
        return _parse_helper_output(output_text, command=command)

    def _apply_hybrid_response(self, response: dict[str, Any]) -> None:
        lines = list(response.get("lines") or [])
        states = list(response.get("states") or [])

        status_changed = False
        error_text: str | None = None
        for line in lines:
            if line.startswith("HYB_KEY_DEB_MS:"):
                try:
                    value = int(line.split(":", 1)[1].strip())
                except Exception:
                    value = None
                if value and value > 0:
                    with self._hybrid_lock:
                        if self._hybrid_key_debounce_ms != value:
                            self._hybrid_key_debounce_ms = value
                            status_changed = True
                continue
            if line.startswith("HYB_GRAPH_FAST_MS:"):
                try:
                    value = int(line.split(":", 1)[1].strip())
                except Exception:
                    value = None
                if value and value > 0:
                    with self._hybrid_lock:
                        if self._hybrid_graph_fast_ms != value:
                            self._hybrid_graph_fast_ms = value
                            status_changed = True
                continue
            if line.startswith("HYBRID_MODE:"):
                mode_on = line.endswith("ON")
                with self._hybrid_lock:
                    if self._hybrid_state.get("mode") != mode_on:
                        self._hybrid_state["mode"] = mode_on
                        status_changed = True
                continue
            if line.startswith(("HYBRID_BRIDGE_ERR", "HYBRID_INIT_ERR", "HYBRID_SYNC_ERR", "HYBRID_STATUS_ERR", "HYBRID_KEY_ERR", "HYBRID_MODE_ERR", "HYBRID_PING_ERR", "HYBRID_CONFIG_ERR")):
                error_text = line

        if error_text is not None:
            with self._hybrid_lock:
                self._hybrid_last_error = error_text
            self._emit_hybrid_status_event(reason="helper-error", error=error_text)

        for state in states:
            update = self._merge_hybrid_state(state)
            self._emit_hybrid_state_event(update)

        if status_changed and error_text is None:
            with self._hybrid_lock:
                self._hybrid_last_error = None
            self._emit_hybrid_status_event(reason="updated")

    def _merge_hybrid_state(self, state: dict[str, Any]) -> dict[str, Any]:
        update = dict(state)
        if "frame_id" in update:
            try:
                update["frame_id"] = int(update["frame_id"])
            except Exception:
                update["frame_id"] = -1
        if "fb_seq" in update:
            try:
                update["fb_seq"] = int(update["fb_seq"])
            except Exception:
                update["fb_seq"] = 0
        if "nav" in update:
            update["nav"] = str(update["nav"])
        if "lines" in update:
            raw_lines = update.get("lines")
            if isinstance(raw_lines, (list, tuple)):
                update["lines"] = [str(item) for item in raw_lines]
            else:
                update["lines"] = []
        for key in ("mode", "capture_enabled", "fb_seen", "fb_full"):
            if key in update:
                update[key] = bool(update[key])
        if "fb" in update and not update["fb"]:
            del update["fb"]

        with self._hybrid_lock:
            for key, value in update.items():
                self._hybrid_state[key] = value
            return dict(update)

    def _build_hybrid_status(self) -> dict[str, Any]:
        with self._hybrid_lock:
            return self._build_hybrid_status_locked()

    def _build_hybrid_status_locked(self) -> dict[str, Any]:
        return {
            "connected": self._controller is not None,
            "active": self._hybrid_active,
            "port": self._port,
            "transport": "helper-poll",
            "mode": bool(self._hybrid_state.get("mode", False)),
            "keyDebounceMs": self._hybrid_key_debounce_ms,
            "graphFastMs": self._hybrid_graph_fast_ms,
            "error": self._hybrid_last_error,
        }

    def _build_hybrid_status_event_payload(
        self,
        *,
        reason: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        with self._hybrid_lock:
            if error is not None:
                self._hybrid_last_error = error
            payload = {
                "type": "status",
                **self._build_hybrid_status_locked(),
            }
        if reason:
            payload["reason"] = reason
        return payload

    def _emit_hybrid_status_event(self, *, reason: str | None = None, error: str | None = None) -> None:
        self._emit_hybrid_event(self._build_hybrid_status_event_payload(reason=reason, error=error))

    def _emit_hybrid_state_event(self, state: dict[str, Any]) -> None:
        self._emit_hybrid_event({"type": "state", "state": state})

    def _set_hybrid_inactive(self, *, error: str | None = None, reason: str = "stopped") -> None:
        with self._hybrid_lock:
            self._hybrid_active = False
            self._hybrid_pause_until_prompt = False
            self._hybrid_stop.set()
            self._hybrid_thread = None
            if error is not None:
                self._hybrid_last_error = error
        self._emit_hybrid_status_event(reason=reason, error=error)

    def _attach_session_locked(self, controller: CalSciController) -> None:
        stop_event = threading.Event()
        pause_requested = threading.Event()
        paused_event = threading.Event()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        thread = threading.Thread(
            target=self._reader_loop,
            args=(controller, stop_event, pause_requested, paused_event, decoder),
            daemon=True,
            name=f"CalSciSessionReader[{controller.port}]",
        )

        self._controller = controller
        self._port = controller.port
        self._reader_stop = stop_event
        self._reader_pause_requested = pause_requested
        self._reader_paused = paused_event
        self._reader_thread = thread
        with self._helper_condition:
            self._helper_line_buffer = ""
            self._helper_lines.clear()
            self._helper_line_seq = 0
            self._helper_state_seq = 0
            self._suppress_terminal_helper_output = False
            self._suppress_terminal_helper_output_deadline = 0.0
            self._suppress_terminal_helper_activity_seen = False
            self._hybrid_pause_until_prompt = False
        thread.start()

    def _detach_session(self) -> tuple[CalSciController, threading.Thread | None, threading.Event, threading.Event] | None:
        with self._lock:
            return self._detach_session_locked()

    def _detach_session_locked(
        self,
    ) -> tuple[CalSciController, threading.Thread | None, threading.Event, threading.Event] | None:
        if self._controller is None:
            return None

        detached = (
            self._controller,
            self._reader_thread,
            self._reader_stop,
            self._reader_pause_requested,
        )
        self._controller = None
        self._port = None
        self._reader_thread = None
        self._reader_stop = threading.Event()
        self._reader_pause_requested = threading.Event()
        self._reader_paused = threading.Event()
        return detached

    def _teardown_detached(
        self,
        detached: tuple[CalSciController, threading.Thread | None, threading.Event, threading.Event] | None,
    ) -> None:
        if detached is None:
            return

        controller, thread, stop_event, pause_requested = detached
        stop_event.set()
        pause_requested.clear()
        controller.close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    def _reader_loop(
        self,
        controller: CalSciController,
        stop_event: threading.Event,
        pause_requested: threading.Event,
        paused_event: threading.Event,
        decoder: Any,
    ) -> None:
        try:
            while not stop_event.is_set():
                if pause_requested.is_set():
                    paused_event.set()
                    while pause_requested.is_set() and not stop_event.is_set():
                        time.sleep(0.01)
                    paused_event.clear()
                    continue

                try:
                    chunk = controller.read_terminal_chunk()
                except (serial.SerialException, serial.SerialTimeoutException, OSError, TypeError) as exc:
                    if stop_event.is_set():
                        return
                    self._handle_reader_failure(controller, str(exc))
                    return

                if not chunk:
                    continue

                text = decoder.decode(chunk, final=False)
                if text:
                    self._process_terminal_text(text)
        finally:
            try:
                text = decoder.decode(b"", final=True)
            except Exception:
                text = ""
            if text:
                self._process_terminal_text(text)
            paused_event.set()

    def _handle_reader_failure(self, controller: CalSciController, error: str) -> None:
        detached = None
        with self._lock:
            if self._controller is not controller:
                return
            detached = self._detach_session_locked()
        self._set_hybrid_inactive(error=error, reason="reader-failed")
        self._teardown_detached(detached)
        self._emit_session_state_event(error=error, reason="reader-failed")

    def _begin_exclusive_operation(self) -> tuple[CalSciController, threading.Event]:
        with self._lock:
            controller = self._controller
            pause_requested = self._reader_pause_requested
            paused_event = self._reader_paused

        if controller is None:
            raise ControllerError("No open CalSci session.")

        pause_requested.set()
        paused_event.wait(timeout=READER_PAUSE_WAIT_SEC)
        return controller, pause_requested

    def _end_exclusive_operation(self, pause_requested: threading.Event) -> None:
        pause_requested.clear()

    def _build_state_locked(self) -> dict[str, Any]:
        return {
            "connected": self._controller is not None,
            "port": self._port,
        }

    def _emit_session_state_event(self, *, error: str | None = None, reason: str | None = None) -> None:
        payload = self.state()
        if error:
            payload["error"] = error
        if reason:
            payload["reason"] = reason
        self._emit_session_state(payload)


_service_write_lock = threading.Lock()


def _service_emit(message: dict[str, Any]) -> None:
    wire = json.dumps(message, ensure_ascii=False)
    with _service_write_lock:
        print(wire, flush=True)


def _service_emit_terminal_output(data: str) -> None:
    if not data:
        return
    _service_emit({"type": "event", "event": EVENT_TERMINAL_OUTPUT, "data": data})


def _service_emit_session_state(payload: dict[str, Any]) -> None:
    _service_emit({"type": "event", "event": EVENT_SESSION, "payload": payload})


def _service_emit_hybrid_event(payload: dict[str, Any]) -> None:
    _service_emit({"type": "event", "event": EVENT_HYBRID, "payload": payload})


@dataclass(order=True)
class ServiceJob:
    priority: int
    seq: int
    request_id: str
    command: str
    args: dict[str, Any]
    stream: bool


class JobDispatcher:
    def __init__(self):
        self._queue: "queue.PriorityQueue[ServiceJob]" = queue.PriorityQueue()
        self._stop = threading.Event()
        self._seq = 0
        self._lock = threading.Lock()
        self._active_run_lock = threading.Lock()
        self._active_run_request_id: str | None = None
        self._active_run_cancel: threading.Event | None = None
        self._session = PersistentSession(_service_emit_terminal_output, _service_emit_session_state, _service_emit_hybrid_event)
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="CalSciBackendDispatcher")
        self._worker.start()

    def submit(self, request_id: str, command: str, args: dict[str, Any], stream: bool) -> None:
        with self._lock:
            self._seq += 1
            seq = self._seq
        priority = COMMAND_PRIORITY.get(command, 80)
        self._queue.put(ServiceJob(priority, seq, request_id, command, args, stream))

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            self._seq += 1
            seq = self._seq
        self._queue.put(ServiceJob(999, seq, "", "shutdown", {}, False))
        self._worker.join(timeout=2.0)
        self._session.close(emit_event=False, reason="shutdown")

    def cancel_active_run(self) -> dict[str, Any]:
        with self._active_run_lock:
            if self._active_run_cancel is None or self._active_run_request_id is None:
                return {"ok": True, "active": False, "cancelled": False}
            self._active_run_cancel.set()
            return {
                "ok": True,
                "active": True,
                "cancelled": True,
                "requestId": self._active_run_request_id,
            }

    def _register_active_run(self, request_id: str, cancel_event: threading.Event) -> None:
        with self._active_run_lock:
            self._active_run_request_id = request_id
            self._active_run_cancel = cancel_event

    def _clear_active_run(self, request_id: str) -> None:
        with self._active_run_lock:
            if self._active_run_request_id != request_id:
                return
            self._active_run_request_id = None
            self._active_run_cancel = None

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue

            if job.command == "shutdown":
                self._queue.task_done()
                continue

            payload = self._execute_job(job)
            _service_emit({"id": job.request_id, "type": "result", "payload": payload})
            self._queue.task_done()

    def _execute_job(self, job: ServiceJob) -> dict[str, Any]:
        args = job.args or {}
        if args.get("enabled") is False:
            return {"ok": True, "skipped": True, "reason": "disabled", "command": job.command}

        try:
            if job.command == "scan":
                return {"ok": True, "devices": list_calsci_ports()}
            if job.command == "session.open":
                return self._session.open(port=str(args["port"]))
            if job.command == "session.close":
                return self._session.close(reason="closed-by-command")
            if job.command == "session.state":
                return {"ok": True, **self._session.state()}
            if job.command == "terminal.write":
                return self._session.terminal_write(data=str(args.get("data", "")))
            if job.command == "hybrid.start":
                return self._session.hybrid_start(port=_optional_arg_string(args, "port"))
            if job.command == "hybrid.stop":
                return self._session.hybrid_stop(reason="stopped-by-command")
            if job.command == "hybrid.sync-full":
                return self._session.hybrid_sync_full()
            if job.command == "hybrid.key":
                return self._session.hybrid_key(
                    col=int(args.get("col", -1)),
                    row=int(args.get("row", -1)),
                )
            if job.command == "hybrid.snapshot":
                return self._session.hybrid_snapshot()
            if job.command == "soft-reset":
                return self._session.soft_reset(
                    port=_optional_arg_string(args, "port"),
                    timeout_seconds=float(args.get("timeout", 5.0)),
                )
            if job.command == "run-file":
                cancel_event = threading.Event()
                self._register_active_run(job.request_id, cancel_event)
                try:
                    if job.stream:
                        return self._session.run_file(
                            port=_optional_arg_string(args, "port"),
                            local_file=str(args["localFile"]),
                            timeout_seconds=float(args.get("timeout", DEFAULT_RUN_TIMEOUT_SEC)),
                            stdout_line_callback=lambda line, req_id=job.request_id: _service_emit(
                                {"id": req_id, "type": "stream", "stream": "stdout", "line": line}
                            ),
                            stderr_line_callback=lambda line, req_id=job.request_id: _service_emit(
                                {"id": req_id, "type": "stream", "stream": "stderr", "line": line}
                            ),
                            cancel_event=cancel_event,
                        )
                    return self._session.run_file(
                        port=_optional_arg_string(args, "port"),
                        local_file=str(args["localFile"]),
                        timeout_seconds=float(args.get("timeout", DEFAULT_RUN_TIMEOUT_SEC)),
                        cancel_event=cancel_event,
                    )
                finally:
                    self._clear_active_run(job.request_id)
            return {"ok": False, "error": f"Unsupported command: {job.command}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def _optional_arg_string(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _recover_after_run_failure(controller: CalSciController) -> dict[str, Any]:
    try:
        friendly_repl = controller.recover_friendly_repl(RUN_FAILURE_FRIENDLY_REPL_TIMEOUT_SEC)
    except Exception as exc:
        friendly_repl = {
            "ok": False,
            "promptSeen": False,
            "port": controller.port,
            "output": "",
            "error": str(exc),
        }

    if friendly_repl.get("ok"):
        return {
            "ok": True,
            "mode": "friendly-repl",
            "port": controller.port,
            "output": friendly_repl.get("output", ""),
            "friendlyRepl": friendly_repl,
        }

    try:
        soft_reset = controller.soft_reset(RUN_FAILURE_SOFT_RESET_TIMEOUT_SEC)
    except Exception as exc:
        soft_reset = {
            "ok": False,
            "promptSeen": False,
            "rebootSeen": False,
            "port": controller.port,
            "output": "",
            "error": str(exc),
        }

    if soft_reset.get("ok"):
        return {
            "ok": True,
            "mode": "soft-reset",
            "port": controller.port,
            "output": _join_non_empty_text([
                str(friendly_repl.get("output", "")),
                str(soft_reset.get("output", "")),
            ]),
            "friendlyRepl": friendly_repl,
            "softReset": soft_reset,
        }

    return {
        "ok": False,
        "mode": "failed",
        "port": controller.port,
        "output": _join_non_empty_text([
            str(friendly_repl.get("output", "")),
            str(soft_reset.get("output", "")),
        ]),
        "friendlyRepl": friendly_repl,
        "softReset": soft_reset,
        "error": soft_reset.get("error")
        or friendly_repl.get("error")
        or "Failed to recover friendly REPL after run.",
    }


def list_calsci_ports() -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    for port in list_ports.comports():
        product = (port.product or "").strip()
        if product != CALSCI_PRODUCT:
            continue
        devices.append(
            {
                "port": port.device,
                "product": product,
                "description": (port.description or "").strip(),
            }
        )
    devices.sort(key=lambda item: item["port"])
    return devices


def _load_local_text_file(local_file: str) -> tuple[Path, str]:
    local_path = Path(local_file).expanduser().resolve()
    if not local_path.exists():
        raise FileNotFoundError(f"Local file not found: {local_path}")
    if not local_path.is_file():
        raise IsADirectoryError(f"Path is not a file: {local_path}")
    try:
        return local_path, local_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"File must be UTF-8 text: {exc}") from exc


def run_soft_reset(port: str, timeout_seconds: float) -> dict[str, Any]:
    controller = CalSciController(port, exclusive=False)
    try:
        return controller.soft_reset(timeout_seconds)
    except Exception as exc:
        return {
            "ok": False,
            "promptSeen": False,
            "rebootSeen": False,
            "port": port,
            "output": "",
            "error": str(exc),
        }
    finally:
        controller.close()


def run_file(
    port: str,
    local_file: str,
    timeout_seconds: float,
    stdout_line_callback: Callable[[str], None] | None = None,
    stderr_line_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    try:
        local_path, source = _load_local_text_file(local_file)
    except Exception as exc:
        return {
            "ok": False,
            "port": port,
            "localFile": str(Path(local_file).expanduser().resolve()),
            "output": "",
            "error": str(exc),
        }

    controller = CalSciController(port, exclusive=False)
    payload: dict[str, Any]
    recovery_payload: dict[str, Any] | None = None
    try:
        stdout_bytes, stderr_bytes = controller.exec_source(
            source,
            timeout_seconds,
            line_callback=stdout_line_callback,
        )
        output = stdout_bytes.decode("utf-8", errors="replace")
        error_text = stderr_bytes.decode("utf-8", errors="replace").strip()

        if error_text:
            if stderr_line_callback is not None:
                for line in error_text.splitlines():
                    stderr_line_callback(line)
            payload = {
                "ok": False,
                "port": port,
                "localFile": str(local_path),
                "output": output,
                "error": error_text,
            }
        else:
            payload = {
                "ok": True,
                "port": port,
                "localFile": str(local_path),
                "output": output,
            }
    except Exception as exc:
        payload = {
            "ok": False,
            "port": port,
            "localFile": str(local_path),
            "output": "",
            "error": str(exc),
        }
        recovery_payload = _recover_after_run_failure(controller)
    finally:
        controller.close()

    if recovery_payload and not recovery_payload.get("ok"):
        payload["ok"] = False
        existing_error = payload.get("error")
        restore_error = recovery_payload.get("error") or "Failed to recover friendly REPL after run"
        if existing_error:
            payload["error"] = f"{existing_error} | restore failed: {restore_error}"
        else:
            payload["error"] = f"restore failed: {restore_error}"
    if recovery_payload is not None:
        payload["restoreDetail"] = {
            "ok": bool(recovery_payload.get("ok")),
            "port": port,
            "recovery": recovery_payload,
        }
    return payload


def serve_loop() -> int:
    dispatcher = JobDispatcher()
    _service_emit({"type": "ready"})
    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue

            request_id: str | None = None
            try:
                request = json.loads(line)
                request_id = str(request.get("id")) if request.get("id") is not None else ""
                command = str(request.get("command", ""))
                args = dict(request.get("args") or {})
                stream = bool(request.get("stream"))
            except Exception as exc:
                _service_emit({
                    "id": request_id,
                    "type": "result",
                    "payload": {"ok": False, "error": f"Invalid request: {exc}"},
                })
                continue

            if command == "shutdown":
                _service_emit({"id": request_id, "type": "result", "payload": {"ok": True}})
                break
            if command == "run.cancel":
                _service_emit({"id": request_id, "type": "result", "payload": dispatcher.cancel_active_run()})
                continue

            dispatcher.submit(request_id, command, args, stream)
    finally:
        dispatcher.shutdown()
    return 0


def emit(payload: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(payload))
    else:
        print(payload)
    return 0 if payload.get("ok") else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="CalSci serial controller backend")
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument("--stream", action="store_true", help="Stream output in CLI mode")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("scan", help="List CalSci serial ports")

    soft_reset = subparsers.add_parser("soft-reset", help="Soft reset device")
    soft_reset.add_argument("--port", required=True, help="Serial port path")
    soft_reset.add_argument("--timeout", type=float, default=5.0, help="Timeout seconds")

    run_file_parser = subparsers.add_parser("run-file", help="Run Python file on device")
    run_file_parser.add_argument("--port", required=True, help="Serial port path")
    run_file_parser.add_argument("--local-file", required=True, help="Local file path")
    run_file_parser.add_argument("--timeout", type=float, default=DEFAULT_RUN_TIMEOUT_SEC, help="Timeout seconds (0 disables timeout)")

    subparsers.add_parser("serve", help="Run persistent backend service over stdio")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command == "serve":
        return serve_loop()
    if args.command == "scan":
        return emit({"ok": True, "devices": list_calsci_ports()}, getattr(args, "json", False))
    if args.command == "soft-reset":
        return emit(run_soft_reset(port=args.port, timeout_seconds=args.timeout), getattr(args, "json", False))
    if args.command == "run-file":
        stream = bool(getattr(args, "stream", False))
        if stream:
            payload = run_file(
                port=args.port,
                local_file=args.local_file,
                timeout_seconds=args.timeout,
                stdout_line_callback=lambda line: print(f"CALSCI_OUT:{line}", flush=True),
                stderr_line_callback=lambda line: print(f"CALSCI_ERR:{line}", flush=True),
            )
            print(json.dumps(payload), flush=True)
            return 0 if payload.get("ok") else 1
        return emit(
            run_file(port=args.port, local_file=args.local_file, timeout_seconds=args.timeout),
            getattr(args, "json", False),
        )
    return emit({"ok": False, "error": f"Unsupported command: {args.command}"}, getattr(args, "json", False))


if __name__ == "__main__":
    raise SystemExit(main())
