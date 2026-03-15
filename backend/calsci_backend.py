#!/usr/bin/env python3
"""CalSci backend service.

Service contract:
- The backend owns the serial port for any open session.
- `session.open` and `session.close` manage a persistent friendly-REPL session.
- `terminal.write` injects user input into that session.
- `run-file` and `soft-reset` reuse the same session while serving over stdio.
- Standalone CLI commands keep the v1 one-shot open/run/close behavior.
"""

from __future__ import annotations

import argparse
import codecs
import json
import queue
import sys
import threading
import time
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
DEFAULT_RUN_TIMEOUT_SEC = 30.0
RUN_FAILURE_FRIENDLY_REPL_TIMEOUT_SEC = 2.5
RUN_FAILURE_SOFT_RESET_TIMEOUT_SEC = 12.0
RAW_REPL_CHUNK_BYTES = 256
RAW_REPL_CHUNK_DELAY_SEC = 0.01
RAW_REPL_ENTER_TIMEOUT_SEC = 6.0
RAW_REPL_BANNER = b"raw REPL; CTRL-B to exit\r\n"
RAW_REPL_PROMPT = RAW_REPL_BANNER + b">"
PORT_OPEN_SETTLE_SEC = 0.12
LINE_FLUSH_DELAY_SEC = 0.03
READER_PAUSE_WAIT_SEC = 0.5
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
    "run-file": 10,
    "soft-reset": 20,
    "session.close": 25,
    "session.state": 30,
    "scan": 90,
    "shutdown": 99,
}
BACKEND_WRITE_RETRIES = 3
EVENT_SESSION = "session"
EVENT_TERMINAL_OUTPUT = "terminal-output"
FRIENDLY_REPL_PROMPTS = (b"CalSci >>>", b">>>")


class ControllerError(RuntimeError):
    pass


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
    return any(prompt in data for prompt in FRIENDLY_REPL_PROMPTS)


def _join_non_empty_text(parts: list[str]) -> str:
    return "".join(part for part in parts if part)


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
        timeout: float = 1.0,
        timeout_overall: float | None = None,
        data_consumer: Callable[[bytes], None] | None = None,
    ) -> bytes:
        data = bytearray()
        begin_overall = begin_char = time.monotonic()
        while True:
            if data.endswith(ending):
                return bytes(data)

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
        time.sleep(LINE_FLUSH_DELAY_SEC)
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

    def _raw_follow(self, timeout: float, line_callback: Callable[[str], None] | None = None) -> tuple[bytes, bytes]:
        sink = RawLineSink(line_callback) if line_callback is not None else None

        def feed_stdout(chunk: bytes) -> None:
            if sink is None or chunk == b"\x04":
                return
            sink.feed(chunk)

        normal = self._raw_read_until(
            b"\x04",
            timeout=timeout,
            timeout_overall=timeout,
            data_consumer=feed_stdout if sink is not None else None,
        )
        if not normal.endswith(b"\x04"):
            raise ControllerError("timeout waiting for raw REPL stdout terminator")
        normal = normal[:-1]
        if sink is not None:
            sink.flush()

        error = self._raw_read_until(b"\x04", timeout=timeout, timeout_overall=timeout)
        if not error.endswith(b"\x04"):
            raise ControllerError("timeout waiting for raw REPL stderr terminator")
        error = error[:-1]
        return normal, error

    def exec_source(
        self,
        source: str,
        timeout_seconds: float,
        line_callback: Callable[[str], None] | None = None,
    ) -> tuple[bytes, bytes]:
        try:
            self._enter_raw_repl()
            self._exec_raw_no_follow(source)
            return self._raw_follow(timeout_seconds, line_callback=line_callback)
        finally:
            if self._in_raw_repl:
                try:
                    self._exit_raw_repl()
                finally:
                    self._in_raw_repl = False

    def recover_friendly_repl(self, timeout_seconds: float) -> dict[str, Any]:
        output_chunks: list[bytes] = []
        prompt_seen = False

        self._drain_serial_input()
        self._write_bytes(b"\x03\x03", flush=True)
        time.sleep(SOFT_RESET_BREAK_DELAY_SEC)
        self._write_bytes(b"\r\x02\r", flush=True)

        deadline = time.monotonic() + max(0.2, timeout_seconds)
        while time.monotonic() < deadline:
            chunk = self.read_terminal_chunk()
            if not chunk:
                time.sleep(0.05)
                continue
            output_chunks.append(chunk)
            if _has_friendly_prompt(b"".join(output_chunks[-8:])):
                prompt_seen = True
                break

        output = b"".join(output_chunks).decode("utf-8", errors="replace")
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
    ):
        self._emit_terminal_text = emit_terminal_text
        self._emit_session_state = emit_session_state
        self._lock = threading.RLock()
        self._controller: CalSciController | None = None
        self._port: str | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._reader_pause_requested = threading.Event()
        self._reader_paused = threading.Event()

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
        return payload

    def close(self, emit_event: bool = True, reason: str = "closed") -> dict[str, Any]:
        detached = self._detach_session()
        self._teardown_detached(detached)
        payload = {"ok": True, "connected": False, "port": None}
        if emit_event:
            self._emit_session_state_event(reason=reason)
        return payload

    def terminal_write(self, data: str) -> dict[str, Any]:
        if not data:
            return {"ok": True}

        with self._lock:
            controller = self._controller
        if controller is None:
            return {"ok": False, "error": "No open CalSci session."}

        try:
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
                    self._emit_terminal_text(text)
        finally:
            try:
                text = decoder.decode(b"", final=True)
            except Exception:
                text = ""
            if text:
                self._emit_terminal_text(text)
            paused_event.set()

    def _handle_reader_failure(self, controller: CalSciController, error: str) -> None:
        detached = None
        with self._lock:
            if self._controller is not controller:
                return
            detached = self._detach_session_locked()
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
        self._session = PersistentSession(_service_emit_terminal_output, _service_emit_session_state)
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
            if job.command == "soft-reset":
                return self._session.soft_reset(
                    port=_optional_arg_string(args, "port"),
                    timeout_seconds=float(args.get("timeout", 5.0)),
                )
            if job.command == "run-file":
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
                    )
                return self._session.run_file(
                    port=_optional_arg_string(args, "port"),
                    local_file=str(args["localFile"]),
                    timeout_seconds=float(args.get("timeout", DEFAULT_RUN_TIMEOUT_SEC)),
                )
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
    run_file_parser.add_argument("--timeout", type=float, default=DEFAULT_RUN_TIMEOUT_SEC, help="Timeout seconds")

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
