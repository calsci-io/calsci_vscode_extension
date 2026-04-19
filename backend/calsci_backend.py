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
import ast
import codecs
import importlib.util
import json
import os
import posixpath
import queue
import re
import subprocess
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
    from . import sync_core as _sync_core  # type: ignore[import-not-found]
    from . import sync_scripts as _sync_scripts  # type: ignore[import-not-found]
except Exception:
    import sync_core as _sync_core
    import sync_scripts as _sync_scripts

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
FRIENDLY_PASTE_ENTER_TIMEOUT_SEC = 1.5
FRIENDLY_PASTE_RECOVERY_TIMEOUT_SEC = 2.0
FRIENDLY_PASTE_PROMPT = b"=== "
FRIENDLY_PASTE_CHUNK_BYTES = 256
FRIENDLY_PASTE_CHUNK_DELAY_SEC = 0.01
PORT_OPEN_SETTLE_SEC = 0.12
READER_PAUSE_WAIT_SEC = 2.0
HYBRID_HELPER_COMMAND_TIMEOUT_SEC = 1.2
HYBRID_HELPER_ENABLE_TIMEOUT_SEC = 1.5
HYBRID_HELPER_POLL_TIMEOUT_SEC = 0.45
HYBRID_HELPER_POLL_INTERVAL_SEC = 0.025
HYBRID_HELPER_REPL_QUIET_SEC = 2.5
SOFT_RESET_BREAK_DELAY_SEC = 0.05
SOFT_RESET_TIMEOUT_FALLBACK_SEC = 2.5
SYNC_DEVICE_COMMAND_TIMEOUT_SEC = 15.0
SYNC_DIR_COMMAND_TIMEOUT_SEC = 6.0
SYNC_SCAN_COMMAND_TIMEOUT_SEC = 25.0
SYNC_FILE_SCRIPT_CHUNK_BYTES = 512
SYNC_SIGNATURE_SCAN_TIMEOUT_SEC = 60.0
SYNC_FILE_UPLOAD_TIMEOUT_SEC = 5.0
SYNC_REPL_DELAY_SEC = 0.01
SYNC_FILE_RETRY_COUNT = 2
SYNC_FILE_RETRY_DELAY_SEC = 0.2
SYNC_FILE_RETRY_RECONNECT_DELAY_SEC = 3.0
SYNC_FAST_COMPARE_TARGET_SEC = 5.0
SYNC_DYNAMIC_RECENT_WINDOW_SEC = 300.0
SYNC_DYNAMIC_SIGNATURE_MARGIN_SEC = 0.35
SYNC_DYNAMIC_SIGNATURE_BYTES_PER_SEC = 32768.0
SYNC_DYNAMIC_MAX_SIGNATURE_FILES = 64
SYNC_UPLOAD_ONLY_FAST_SCAN_MIN_FILES = 32
SYNC_UPLOAD_ONLY_FAST_SCAN_MAX_FILES = 512
SYNC_TARGETED_SCAN_BATCH_SIZE = 64
SYNC_TARGETED_SCAN_MAX_SCRIPT_CHARS = 2600
SYNC_TARGETED_VERIFY_MAX_FILES = 64
SYNC_TARGETED_VERIFY_TIMEOUT_SEC = 8.0
SYNC_CLEAR_ALL_TIMEOUT_SEC = 60.0
WORKSPACE_IMPORT_FILE_CHUNK_BYTES = 128
WORKSPACE_READ_TEXT_CHUNK_CHARS = 256
WORKSPACE_IMPORT_FILE_TIMEOUT_MAX_SEC = 90.0
WORKSPACE_IMPORT_FILE_THROUGHPUT_BYTES_PER_SEC = 4096.0
FIRMWARE_FLASH_CHIP = "esp32s3"
FIRMWARE_FLASH_BAUDRATE = 115200
FIRMWARE_FLASH_CONNECT_ATTEMPTS = 10
FIRMWARE_FLASH_BEFORE = "usb-reset"
FIRMWARE_FLASH_AFTER = "hard-reset"
FIRMWARE_FLASH_BOOTLOADER_OFFSET = "0x0"
FIRMWARE_FLASH_PARTITION_OFFSET = "0x8000"
FIRMWARE_FLASH_OTA_DATA_OFFSET = "0xe000"
FIRMWARE_FLASH_CALOS_OFFSET = "0x10000"
FIRMWARE_FLASH_PORT_RESCAN_TIMEOUT_SEC = 12.0
FIRMWARE_FLASH_PORT_RESCAN_INTERVAL_SEC = 0.5
FIRMWARE_FLASH_AUTO_BOOT_TIMEOUT_SEC = 15.0
FIRMWARE_FLASH_MANUAL_BOOT_TIMEOUT_SEC = 60.0
ESP32_KEYWORDS = ("Espressif", CALSCI_PRODUCT)
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
    "run-file-interactive": 9,
    "run-file": 10,
    "sync-folder": 11,
    "clear-all-files": 11,
    "workspace.scan-tree": 11,
    "workspace.read-file": 11,
    "workspace.import": 11,
    "hybrid.start": 11,
    "hybrid.stop": 12,
    "chip.erase": 15,
    "firmware.flash": 15,
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
HELPER_FRAME_PREFIX = "{{CALSCI_HYB:"
HELPER_FRAME_SUFFIX = "}}"


class ControllerError(RuntimeError):
    pass


class RunCancelledError(ControllerError):
    def __init__(self, output: bytes):
        super().__init__("Run cancelled by user")
        self.output = output


class SessionAbortedError(ControllerError):
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
    tail = bytes(data[-128:])
    parts = re.split(br"[\r\n]+", tail)
    last_line = parts[-1] if parts else tail
    stripped = last_line.lstrip()
    return any(stripped.startswith(prompt) for prompt in FRIENDLY_REPL_PROMPTS)


def _join_non_empty_text(parts: list[str]) -> str:
    return "".join(part for part in parts if part)


def _normalize_friendly_paste_source(source: str | bytes) -> bytes:
    if isinstance(source, bytes):
        text = source.decode("utf-8")
    else:
        text = source
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _is_disconnect_error_text(error_text: str) -> bool:
    lowered = error_text.lower()
    needles = (
        "input/output error",
        "could not open port",
        "no such file or directory",
        "attempting to use a port that is not open",
        "device reports readiness to read but returned no data",
        "device disconnected",
        "session aborted",
    )
    return any(needle in lowered for needle in needles)


def _should_abort_for_exception(exc: Exception) -> bool:
    return isinstance(exc, SessionAbortedError) or _is_disconnect_error_text(str(exc))


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


def _strip_repl_prompt_prefix(text: str) -> str:
    return re.sub(r"^(?:CalSci >>>|>>>)\s*", "", text)


def _unwrap_helper_frame(text: str) -> str | None:
    if not text.startswith(HELPER_FRAME_PREFIX):
        return None
    if not text.endswith(HELPER_FRAME_SUFFIX):
        return None
    return text[len(HELPER_FRAME_PREFIX) : -len(HELPER_FRAME_SUFFIX)]


def _split_helper_framed_text(text: str) -> tuple[str, list[str], str]:
    visible_parts: list[str] = []
    frames: list[str] = []
    scan = 0

    while True:
        start = text.find(HELPER_FRAME_PREFIX, scan)
        if start < 0:
            remainder = text[scan:]
            overlap = _helper_frame_prefix_overlap(remainder)
            if overlap > 0:
                visible_parts.append(remainder[:-overlap])
                return "".join(visible_parts), frames, remainder[-overlap:]
            visible_parts.append(remainder)
            return "".join(visible_parts), frames, ""

        visible_parts.append(text[scan:start])
        end = _find_helper_frame_suffix(text, start + len(HELPER_FRAME_PREFIX))
        if end < 0:
            return "".join(visible_parts), frames, text[start:]

        frames.append(text[start + len(HELPER_FRAME_PREFIX) : end])
        scan = end + len(HELPER_FRAME_SUFFIX)


def _helper_frame_prefix_overlap(text: str) -> int:
    if not text:
        return 0

    max_overlap = min(len(text), len(HELPER_FRAME_PREFIX) - 1)
    for overlap in range(max_overlap, 0, -1):
        if HELPER_FRAME_PREFIX.startswith(text[-overlap:]):
            return overlap
    return 0


def _find_helper_frame_suffix(text: str, start: int) -> int:
    search = start
    while True:
        end = text.find(HELPER_FRAME_SUFFIX, search)
        if end < 0:
            return -1

        suffix_end = end + len(HELPER_FRAME_SUFFIX)
        if suffix_end >= len(text) or text[suffix_end] == "\n":
            return end

        search = end + 1


def _clean_helper_line(line: str) -> str:
    cleaned = line.replace("\r", "").strip()
    cleaned = _strip_repl_prompt_prefix(cleaned)
    framed = _unwrap_helper_frame(cleaned)
    if framed is not None:
        cleaned = framed
    return cleaned.strip()


def _parse_helper_output(text: str, command: str | None = None) -> dict[str, Any]:
    states: list[dict[str, Any]] = []
    lines: list[str] = []
    command_text = (command or "").strip()
    normalized = text.replace("\r", "\n")
    visible_text, framed_payloads, _ = _split_helper_framed_text(normalized)

    for framed in framed_payloads:
        cleaned = _strip_repl_prompt_prefix(framed.replace("\r", "").strip()).strip()
        if not cleaned:
            continue
        if command_text and cleaned == command_text:
            continue
        lines.append(cleaned)
        states.extend(_extract_state_payloads(cleaned))

    for raw_line in visible_text.split("\n"):
        cleaned = _clean_helper_line(raw_line)
        if not cleaned:
            continue
        if command_text and cleaned == command_text:
            continue
        lines.append(cleaned)
        states.extend(_extract_state_payloads(cleaned))
    return {
        "text": text,
        "lines": lines,
        "states": states,
    }


def _is_prompt_only_fragment(text: str) -> bool:
    fragment = text.replace("\r", "").strip()
    if not fragment:
        return False
    cleaned = _strip_repl_prompt_prefix(fragment)
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

_HELPER_TERMINAL_FRAGMENT_TOKENS = (
    "{{CALSCI_HYB",
    "_hyb_",
    "ECHO:VSCODE_",
    "HYB_",
    "HYBRID_",
    "STATE:",
)

_HELPER_STATE_FRAGMENT_TOKENS = (
    '"frame_id"',
    '"fb"',
    '"fb_full"',
    '"fb_seen"',
    '"lines"',
    '"nav"',
    '"mode"',
    '"capture_enabled"',
    '"start_line"',
    '"all_points_on"',
    '"adc_reverse"',
    '"com_reverse"',
    '"invert"',
)


def _looks_like_helper_terminal_fragment(text: str) -> bool:
    if not text:
        return False
    cleaned = _clean_helper_line(text)
    if cleaned and HELPER_FRAME_PREFIX.startswith(cleaned):
        return True
    if not cleaned:
        return False
    if cleaned.startswith(_HELPER_TERMINAL_PREFIXES):
        return True
    if any(token in cleaned for token in _HELPER_TERMINAL_FRAGMENT_TOKENS):
        return True
    if "{" in cleaned and any(token in cleaned for token in _HELPER_STATE_FRAGMENT_TOKENS):
        return True
    return False


def _looks_like_helper_terminal_line(line: str) -> bool:
    if not line:
        return False
    if _looks_like_helper_terminal_fragment(line):
        return True
    return bool(_extract_state_payloads(line))


class CalSciController:
    def __init__(self, port: str, baudrate: int = DEFAULT_BAUDRATE, *, exclusive: bool = False):
        self.port = port
        self._baudrate = baudrate
        self._exclusive = exclusive
        self._in_raw_repl = False
        self._aborted = False
        self._write_lock = threading.Lock()
        self._conn = self._open_connection()

    def abort(self) -> None:
        self._aborted = True
        self.close()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _ensure_active(self) -> None:
        if self._aborted:
            raise SessionAbortedError("CalSci device disconnected.")

    def _open_connection(self) -> serial.Serial:
        self._ensure_active()
        conn = serial.Serial()
        conn.port = self.port
        conn.baudrate = self._baudrate
        conn.timeout = 0.01
        conn.write_timeout = 1.0
        try:
            conn.exclusive = self._exclusive
        except Exception:
            pass
        conn.dsrdtr = False
        conn.rtscts = False
        try:
            conn.dtr = False
            conn.rts = False
        except Exception:
            pass
        conn.open()
        self._enable_kernel_exclusive_lock(conn, self._exclusive)
        time.sleep(PORT_OPEN_SETTLE_SEC)
        return conn

    def _enable_kernel_exclusive_lock(self, conn: serial.Serial, exclusive: bool) -> None:
        if not exclusive or fcntl is None or termios is None:
            return

        ioctl_code = getattr(termios, "TIOCEXCL", None)
        if ioctl_code is None:
            return

        try:
            fcntl.ioctl(conn.fileno(), ioctl_code)
        except OSError as exc:
            try:
                conn.close()
            except Exception:
                pass
            raise ControllerError(f"Could not exclusively lock port {self.port}: {exc}") from exc

    def sync_reconnect(self, delay_seconds: float = SYNC_FILE_RETRY_RECONNECT_DELAY_SEC) -> None:
        self._ensure_active()
        with self._write_lock:
            try:
                self._conn.dtr = False
                self._conn.rts = True
                self._conn.dtr = True
                self._conn.rts = False
            except Exception:
                pass

            try:
                self._conn.close()
            except Exception:
                pass

            self._in_raw_repl = False
            if delay_seconds > 0:
                time.sleep(delay_seconds)
            self._ensure_active()
            self._conn = self._open_connection()

    def write_terminal(self, data: bytes) -> None:
        if not data:
            return
        self._write_bytes(data, flush=True)

    def read_terminal_chunk(self) -> bytes:
        self._ensure_active()
        waiting = int(getattr(self._conn, "in_waiting", 0) or 0)
        return self._conn.read(waiting if waiting > 0 else 1)

    def drain_terminal_available(self) -> bytes:
        self._ensure_active()
        drained = bytearray()
        while True:
            self._ensure_active()
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
        if not data:
            return
        self._ensure_active()
        last_exc: Exception | None = None
        for attempt in range(BACKEND_WRITE_RETRIES):
            try:
                self._ensure_active()
                with self._write_lock:
                    sent = 0
                    view = memoryview(data)
                    while sent < len(view):
                        self._ensure_active()
                        wrote = self._conn.write(view[sent:])
                        if wrote is None:
                            wrote = 0
                        if wrote <= 0:
                            raise serial.SerialTimeoutException("Serial write stalled")
                        sent += int(wrote)
                    if flush:
                        self._conn.flush()
                return
            except (serial.SerialException, serial.SerialTimeoutException, OSError, ValueError) as exc:
                last_exc = exc
                time.sleep(0.04 * (attempt + 1))
        if last_exc is not None:
            raise last_exc

    def _drain_serial_input(self) -> None:
        self._ensure_active()
        try:
            self._conn.reset_input_buffer()
            self._conn.reset_output_buffer()
            return
        except Exception:
            pass

        deadline = time.monotonic() + 0.2
        while time.monotonic() < deadline:
            self._ensure_active()
            chunk = self.read_terminal_chunk()
            if not chunk:
                break

    def _raw_read_exact(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + max(0.05, timeout)
        out = bytearray()
        while len(out) < size and time.monotonic() < deadline:
            self._ensure_active()
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
            self._ensure_active()
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
        if RAW_REPL_BANNER not in data:
            raise ControllerError(f"could not enter raw REPL: {data!r}")

        after_banner = data.split(RAW_REPL_BANNER, 1)[1]
        if b">" not in after_banner:
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

    def exec_friendly_source_start(self, source: str | bytes) -> dict[str, bytes]:
        pending = self.drain_terminal_available()
        self._write_bytes(b"\r\x03", flush=True)
        time.sleep(SOFT_RESET_BREAK_DELAY_SEC)
        interrupt_output = self.drain_terminal_available()
        if interrupt_output:
            pending += interrupt_output

        self._write_bytes(b"\r\x05", flush=True)
        banner = self._raw_read_until(
            FRIENDLY_PASTE_PROMPT,
            timeout=0.5,
            timeout_overall=FRIENDLY_PASTE_ENTER_TIMEOUT_SEC,
        )
        if not banner.endswith(FRIENDLY_PASTE_PROMPT):
            raise ControllerError(f"friendly paste prompt missing after enter: {banner!r}")

        source_bytes = _normalize_friendly_paste_source(source)
        for start in range(0, len(source_bytes), FRIENDLY_PASTE_CHUNK_BYTES):
            chunk = source_bytes[start : start + FRIENDLY_PASTE_CHUNK_BYTES]
            self._write_bytes(chunk, flush=False)
            time.sleep(FRIENDLY_PASTE_CHUNK_DELAY_SEC)
        self._write_bytes(b"\x04", flush=True)

        return {
            "pending": pending,
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

    def exec_source_in_raw_repl(
        self,
        source: str,
        timeout_seconds: float,
        line_callback: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bytes, bytes]:
        if not self._in_raw_repl:
            raise ControllerError("raw REPL is not active")
        try:
            self._exec_raw_no_follow(source)
            output, error, interrupted = self._raw_follow(
                timeout_seconds if timeout_seconds > 0 else None,
                line_callback=line_callback,
                cancel_event=cancel_event,
            )
        except Exception:
            self._in_raw_repl = False
            raise

        if interrupted:
            raise RunCancelledError(output)

        return output, error

    def sync_enter_friendly_repl(self) -> None:
        self._write_bytes(b"\x03\x03", flush=True)
        time.sleep(SYNC_REPL_DELAY_SEC)
        self._sync_reset_input_buffer()
        self._write_bytes(b"\x01", flush=True)
        time.sleep(SYNC_REPL_DELAY_SEC)
        self._sync_reset_input_buffer()
        self._write_bytes(b"\x02", flush=True)
        time.sleep(SYNC_REPL_DELAY_SEC)
        self._sync_reset_input_buffer()
        self._in_raw_repl = False

    def sync_enter_raw_repl(self) -> None:
        self._write_bytes(b"\x03\x03", flush=True)
        time.sleep(SYNC_REPL_DELAY_SEC)
        self._sync_reset_input_buffer()
        self._write_bytes(b"\x01", flush=True)
        time.sleep(SYNC_REPL_DELAY_SEC)
        self._sync_reset_input_buffer()
        self._in_raw_repl = True

    def sync_exit_raw_repl(self) -> None:
        self._write_bytes(b"\x03\x03", flush=True)
        time.sleep(SYNC_REPL_DELAY_SEC)
        self._write_bytes(b"\x02", flush=True)
        time.sleep(SYNC_REPL_DELAY_SEC)
        self._sync_reset_input_buffer()
        self._in_raw_repl = False

    def sync_exec_raw_and_read(self, code: str, timeout: float = 5.0) -> str:
        opened_here = False
        if not self._in_raw_repl:
            self._enter_raw_repl(timeout_overall=max(RAW_REPL_ENTER_TIMEOUT_SEC, timeout))
            opened_here = True
        try:
            self._exec_raw_no_follow(code)
            output, error, interrupted = self._raw_follow(
                timeout if timeout > 0 else None,
                line_callback=None,
                cancel_event=None,
            )
        except Exception:
            # Existing raw session might be out of sync after a command failure.
            if not opened_here:
                self._in_raw_repl = False
            raise
        finally:
            if opened_here:
                try:
                    self._exit_raw_repl()
                except Exception:
                    self._in_raw_repl = False
                    raise

        if interrupted:
            raise ControllerError("Sync command interrupted")

        stderr_text = error.decode("utf-8", errors="replace").strip()
        if stderr_text:
            raise ControllerError(stderr_text)

        result = output.decode(errors="ignore")
        if "Traceback" in result:
            raise ControllerError(result)
        return result

    def sync_exec_friendly_and_read(self, code: str, timeout: float = 5.0) -> str:
        payload = self.exec_friendly_source_start(code)
        pending = payload.get("pending", b"")
        prompt_seen, output = self._read_until_friendly_prompt(timeout)
        text = pending.decode("utf-8", errors="replace") + output.decode("utf-8", errors="replace")
        if not prompt_seen:
            snippet = text.strip()
            if snippet:
                raise ControllerError(f"friendly REPL prompt missing after sync command: {snippet[:200]}")
            raise ControllerError("friendly REPL prompt missing after sync command")
        if "Traceback" in text:
            raise ControllerError(text)
        return text

    def sync_get_file_sizes(self, remote_root: str, timeout: float = SYNC_SCAN_COMMAND_TIMEOUT_SEC) -> dict[str, int]:
        code = _device_list_file_sizes_script(remote_root)
        raw = ""
        last_error: Exception | None = None
        stream_code = _device_list_file_sizes_stream_script(remote_root)
        for attempt in range(2):
            try:
                raw = self.sync_exec_raw_and_read(code, timeout=timeout)
                last_error = None
                break
            except ControllerError as exc:
                last_error = exc
                if attempt == 0:
                    self.sync_enter_friendly_repl()
                    continue
                break
        if last_error is not None:
            try:
                stream_raw = self.sync_exec_friendly_and_read(stream_code, timeout=timeout)
                return _parse_device_sizes_stream_output(stream_raw)
            except Exception as friendly_exc:
                raise ControllerError(f"{last_error} | friendly scan failed: {friendly_exc}") from friendly_exc

        try:
            return _parse_device_sizes_output(raw)
        except ControllerError as exc:
            # Fallback to line-stream parser when large dict repr output is truncated.
            error_text = str(exc)
            if "Device size scan marker missing" not in error_text and "Device size scan returned no output" not in error_text:
                raise

            try:
                stream_raw = self.sync_exec_raw_and_read(stream_code, timeout=timeout)
            except Exception:
                stream_raw = self.sync_exec_friendly_and_read(stream_code, timeout=timeout)
            return _parse_device_sizes_stream_output(stream_raw)

    def sync_get_file_signatures(
        self,
        remote_paths: list[str],
        timeout: float = SYNC_SIGNATURE_SCAN_TIMEOUT_SEC,
    ) -> dict[str, str | None]:
        code = _device_list_file_signatures_script(remote_paths)
        stream_code = _device_list_file_signatures_stream_script(remote_paths)
        raw = ""
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                raw = self.sync_exec_raw_and_read(code, timeout=timeout)
                last_error = None
                break
            except ControllerError as exc:
                last_error = exc
                if attempt == 0:
                    self.sync_enter_friendly_repl()
                    continue
                break
        if last_error is not None:
            try:
                stream_raw = self.sync_exec_friendly_and_read(stream_code, timeout=timeout)
                return _parse_device_signatures_stream_output(stream_raw)
            except Exception as friendly_exc:
                raise ControllerError(
                    f"{last_error} | friendly signature scan failed: {friendly_exc}"
                ) from friendly_exc

        try:
            return _parse_device_signatures_output(raw)
        except ControllerError as exc:
            # Fallback to line-stream parser when large dict repr output is truncated.
            error_text = str(exc)
            if (
                "Device signature scan marker missing" not in error_text
                and "Device signature scan returned no output" not in error_text
            ):
                raise

            try:
                stream_raw = self.sync_exec_raw_and_read(stream_code, timeout=timeout)
            except Exception:
                stream_raw = self.sync_exec_friendly_and_read(stream_code, timeout=timeout)
            return _parse_device_signatures_stream_output(stream_raw)

    def sync_get_selected_file_sizes(
        self,
        remote_paths: list[str],
        timeout: float = SYNC_TARGETED_VERIFY_TIMEOUT_SEC,
    ) -> dict[str, int | None]:
        if not remote_paths:
            return {}

        result: dict[str, int | None] = {}
        path_batches = _chunk_remote_paths_for_targeted_scan(remote_paths)
        for batch_paths in path_batches:
            batch_timeout = max(2.0, min(timeout, 1.2 + (len(batch_paths) * 0.05)))
            result.update(CalSciController._sync_get_selected_file_sizes_batch(self, batch_paths, timeout=batch_timeout))
        return result

    def _sync_get_selected_file_sizes_batch(
        self,
        remote_paths: list[str],
        timeout: float,
    ) -> dict[str, int | None]:
        code = _device_selected_file_sizes_script(remote_paths)
        stream_code = _device_selected_file_sizes_stream_script(remote_paths)

        raw = ""
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                raw = self.sync_exec_raw_and_read(code, timeout=timeout)
                last_error = None
                break
            except ControllerError as exc:
                last_error = exc
                if attempt == 0:
                    self.sync_enter_friendly_repl()
                    continue
                break

        if last_error is not None:
            try:
                friendly_raw = self.sync_exec_friendly_and_read(code, timeout=timeout)
                try:
                    return _parse_device_selected_sizes_output(friendly_raw)
                except ControllerError:
                    friendly_stream_raw = self.sync_exec_friendly_and_read(stream_code, timeout=timeout)
                    return _parse_device_selected_sizes_stream_output(friendly_stream_raw)
            except Exception as friendly_exc:
                raise ControllerError(
                    f"{last_error} | friendly targeted size scan failed: {friendly_exc}"
                ) from friendly_exc

        try:
            return _parse_device_selected_sizes_output(raw)
        except ControllerError as exc:
            error_text = str(exc)
            if (
                "Device targeted size scan marker missing" not in error_text
                and "Device targeted size scan returned no output" not in error_text
            ):
                raise

            try:
                stream_raw = self.sync_exec_raw_and_read(stream_code, timeout=timeout)
            except Exception:
                stream_raw = self.sync_exec_friendly_and_read(stream_code, timeout=timeout)
            return _parse_device_selected_sizes_stream_output(stream_raw)

    def sync_scan_tree(
        self,
        remote_root: str,
        timeout: float = SYNC_SCAN_COMMAND_TIMEOUT_SEC,
    ) -> tuple[list[str], dict[str, int]]:
        stream_code = _device_scan_tree_stream_script(remote_root)
        raw = ""
        last_error: Exception | None = None

        for attempt in range(2):
            try:
                raw = self.sync_exec_raw_and_read(stream_code, timeout=timeout)
                last_error = None
                break
            except ControllerError as exc:
                last_error = exc
                if attempt == 0:
                    self.sync_enter_friendly_repl()
                    continue
                break

        if last_error is not None:
            try:
                friendly_raw = self.sync_exec_friendly_and_read(stream_code, timeout=timeout)
                return _parse_device_tree_stream_output(friendly_raw)
            except Exception as friendly_exc:
                raise ControllerError(
                    f"{last_error} | friendly tree scan failed: {friendly_exc}"
                ) from friendly_exc

        return _parse_device_tree_stream_output(raw)

    def sync_read_file_bytes(
        self,
        remote_path: str,
        timeout: float,
    ) -> bytes:
        stream_code = _device_read_file_hex_stream_script(
            remote_path,
            chunk_bytes=WORKSPACE_IMPORT_FILE_CHUNK_BYTES,
        )
        raw = ""
        last_error: Exception | None = None

        for attempt in range(2):
            try:
                raw = self.sync_exec_raw_and_read(stream_code, timeout=timeout)
                last_error = None
                break
            except ControllerError as exc:
                last_error = exc
                if attempt == 0:
                    self.sync_enter_friendly_repl()
                    continue
                break

        if last_error is not None:
            try:
                friendly_raw = self.sync_exec_friendly_and_read(stream_code, timeout=timeout)
                return _parse_device_file_hex_output(friendly_raw, remote_path=remote_path)
            except Exception as friendly_exc:
                raise ControllerError(
                    f"{last_error} | friendly file read failed for {remote_path}: {friendly_exc}"
                ) from friendly_exc

        return _parse_device_file_hex_output(raw, remote_path=remote_path)

    def sync_read_file_text(
        self,
        remote_path: str,
        timeout: float,
    ) -> str:
        stream_code = _device_read_text_file_stream_script(
            remote_path,
            chunk_chars=WORKSPACE_READ_TEXT_CHUNK_CHARS,
        )
        raw = ""
        last_error: Exception | None = None

        for attempt in range(2):
            try:
                raw = self.sync_exec_raw_and_read(stream_code, timeout=timeout)
                last_error = None
                break
            except ControllerError as exc:
                last_error = exc
                if attempt == 0:
                    self.sync_enter_friendly_repl()
                    continue
                break

        if last_error is not None:
            try:
                friendly_raw = self.sync_exec_friendly_and_read(stream_code, timeout=timeout)
                return _parse_device_text_file_output(friendly_raw, remote_path=remote_path)
            except Exception as friendly_exc:
                raise ControllerError(
                    f"{last_error} | friendly file read failed for {remote_path}: {friendly_exc}"
                ) from friendly_exc

        return _parse_device_text_file_output(raw, remote_path=remote_path)

    def sync_mkdir(self, path: str) -> bool:
        target = json.dumps(path)
        code = (
            "import os\r\n"
            "try:\r\n"
            f"    os.mkdir({target})\r\n"
            "except:\r\n"
            "    pass\r\n"
            "try:\r\n"
            f"    os.stat({target})\r\n"
            "    print('EXISTS')\r\n"
            "except:\r\n"
            "    print('MISSING')\r\n"
        )
        result = self.sync_exec_raw_and_read(code, timeout=1.0)
        return "EXISTS" in result

    def sync_delete_file(self, path: str) -> bool:
        target = json.dumps(path)
        code = (
            "import os\r\n"
            "try:\r\n"
            f"    os.remove({target})\r\n"
            "    print('DELETED')\r\n"
            "except Exception as e:\r\n"
            "    print('ERROR:' + str(e))\r\n"
        )
        result = self.sync_exec_raw_and_read(code, timeout=3.0)
        return "DELETED" in result

    def sync_clear_all(self, timeout: float = SYNC_CLEAR_ALL_TIMEOUT_SEC) -> str:
        return self.sync_exec_raw_and_read(_device_clear_all_script(), timeout=timeout)

    def sync_put_content(self, remote_path: str, data: bytes, timeout: float | None = None) -> None:
        code = _device_put_file_script(remote_path, data)
        result = self.sync_exec_raw_and_read(
            code,
            timeout=timeout if timeout is not None else _estimate_sync_source_timeout(
                code,
                minimum_seconds=SYNC_FILE_UPLOAD_TIMEOUT_SEC,
            ),
        )
        if "Traceback" in result:
            raise ControllerError(result.strip())
        if "OK" not in result:
            preview = result.strip()
            raise ControllerError(f"No OK confirmation: {preview[:200]}")

    def sync_put_raw(self, local_path: Path, remote_path: str) -> None:
        if not self._in_raw_repl:
            raise ControllerError("raw REPL is not active")

        data = local_path.read_bytes()
        total_len = len(data)
        num_chunks = (total_len + SYNC_FILE_SCRIPT_CHUNK_BYTES - 1) // SYNC_FILE_SCRIPT_CHUNK_BYTES

        self._sync_reset_input_buffer()

        lines = [
            "import os",
            "try:",
            f"    os.remove({json.dumps(remote_path)})",
            "except OSError:",
            "    pass",
            f"f = open({json.dumps(remote_path)}, \"wb\")",
        ]
        for index in range(num_chunks):
            chunk = data[index * SYNC_FILE_SCRIPT_CHUNK_BYTES : (index + 1) * SYNC_FILE_SCRIPT_CHUNK_BYTES]
            lines.append(f"f.write({repr(chunk)})")
        lines.extend([
            "f.close()",
            'print("OK")',
        ])

        code = "\r\n".join(lines) + "\r\n"
        self._exec_raw_no_follow(code)
        output, error, interrupted = self._raw_follow(
            SYNC_FILE_UPLOAD_TIMEOUT_SEC,
            line_callback=None,
            cancel_event=None,
        )
        if interrupted:
            raise ControllerError("Sync upload interrupted")

        stderr_text = error.decode("utf-8", errors="replace").strip()
        if stderr_text:
            raise ControllerError(stderr_text)

        stdout_text = output.decode("utf-8", errors="replace")
        if "Traceback" in stdout_text:
            raise ControllerError(stdout_text.strip())
        if "OK" not in stdout_text:
            preview = stdout_text.strip()
            raise ControllerError(f"No OK confirmation: {preview[:200]}")

    def _sync_reset_input_buffer(self) -> None:
        try:
            self._conn.reset_input_buffer()
        except Exception:
            pass

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

    def recover_friendly_prompt(self, timeout_seconds: float) -> dict[str, Any]:
        self._write_bytes(b"\x03\r", flush=True)
        prompt_seen, output_bytes = self._read_until_friendly_prompt(timeout_seconds)
        output = output_bytes.decode("utf-8", errors="replace")
        payload = {
            "ok": prompt_seen,
            "promptSeen": prompt_seen,
            "port": self.port,
            "output": output,
        }
        if not prompt_seen:
            payload["error"] = "Friendly REPL prompt not detected after interactive run recovery."
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

    def request_bootloader(self) -> None:
        self._drain_serial_input()
        try:
            self._enter_raw_repl(timeout_overall=RAW_REPL_ENTER_TIMEOUT_SEC)
            self._exec_raw_no_follow("import machine\r\nmachine.bootloader()\r\n")
            time.sleep(0.05)
        except Exception:
            self._in_raw_repl = False
            raise
        self._in_raw_repl = False


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
        self._abort_requested = threading.Event()
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
        self._helper_frame_remainder = ""
        self._helper_lines: deque[tuple[int, str]] = deque(maxlen=256)
        self._helper_line_seq = 0
        self._helper_state_seq = 0
        self._suppress_terminal_helper_output = False
        self._suppress_terminal_helper_depth = 0
        self._suppress_terminal_helper_output_deadline = 0.0
        self._suppress_terminal_helper_activity_seen = False

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._build_state_locked()

    def _raise_if_abort_requested(self) -> None:
        if self._abort_requested.is_set():
            raise SessionAbortedError("CalSci device disconnected.")

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

    def abort(self, reason: str = "aborted") -> dict[str, Any]:
        self._abort_requested.set()
        with self._lock:
            controller = self._controller
        if controller is not None:
            controller.abort()
        return self.close(reason=reason)

    def terminal_write(self, data: str) -> dict[str, Any]:
        if not data:
            return {"ok": True}

        self._raise_if_abort_requested()
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

    def request_bootloader(
        self,
        port: str | None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        requested_port = str(port or "").strip()
        with self._lock:
            session_port = self._port
            controller = self._controller

        if controller is None or not session_port:
            return {
                "ok": False,
                "prepared": False,
                "skipped": True,
                "port": requested_port,
            }

        if requested_port and session_port != requested_port:
            return {
                "ok": False,
                "prepared": False,
                "skipped": True,
                "port": requested_port,
                "sessionPort": session_port,
            }

        with self._operation_lock:
            try:
                controller, pause_requested = self._begin_exclusive_operation()
            except Exception as exc:
                return {
                    "ok": False,
                    "prepared": False,
                    "port": requested_port or session_port,
                    "error": str(exc),
                }

            try:
                if progress_callback is not None:
                    progress_callback(f"Requesting bootloader mode via active CalSci session on {controller.port}...")
                controller.request_bootloader()
                return {
                    "ok": True,
                    "prepared": True,
                    "port": controller.port,
                }
            except Exception as exc:
                return {
                    "ok": False,
                    "prepared": False,
                    "port": controller.port,
                    "error": str(exc),
                }
            finally:
                self._end_exclusive_operation(pause_requested)

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
                    if not _should_abort_for_exception(exc):
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

    def run_file_interactive(
        self,
        port: str | None,
        local_file: str,
    ) -> dict[str, Any]:
        try:
            local_path, source = _load_local_text_file(local_file)
        except Exception as exc:
            return {
                "ok": False,
                "port": port or "",
                "localFile": str(Path(local_file).expanduser().resolve()),
                "error": str(exc),
            }

        if port:
            opened = self.open(port)
            if not opened.get("ok"):
                return {
                    "ok": False,
                    "port": port,
                    "localFile": str(local_path),
                    "error": opened.get("error", "Failed to open session."),
                }

        pending_bytes = b""
        recovery_payload: dict[str, Any] | None = None

        with self._operation_lock:
            try:
                controller, pause_requested = self._begin_exclusive_operation()
            except Exception as exc:
                return {
                    "ok": False,
                    "port": port or "",
                    "localFile": str(local_path),
                    "error": str(exc),
                }

            try:
                with self._hybrid_lock:
                    if self._hybrid_active:
                        self._hybrid_repl_quiet_until = time.monotonic() + HYBRID_HELPER_REPL_QUIET_SEC
                        self._hybrid_pause_until_prompt = True

                start_payload = controller.exec_friendly_source_start(source)
                pending_bytes = start_payload.get("pending", b"")
                payload = {
                    "ok": True,
                    "port": controller.port,
                    "localFile": str(local_path),
                }
            except Exception as exc:
                payload = {
                    "ok": False,
                    "port": controller.port,
                    "localFile": str(local_path),
                    "error": str(exc),
                }
                if not _should_abort_for_exception(exc):
                    recovery_payload = controller.recover_friendly_prompt(FRIENDLY_PASTE_RECOVERY_TIMEOUT_SEC)
            finally:
                self._end_exclusive_operation(pause_requested)

        if pending_bytes:
            self._emit_terminal_text(pending_bytes.decode("utf-8", errors="replace"))

        if recovery_payload and recovery_payload.get("output"):
            self._emit_terminal_text(str(recovery_payload["output"]))

        if recovery_payload and not recovery_payload.get("ok"):
            payload["ok"] = False
            existing_error = payload.get("error")
            restore_error = recovery_payload.get("error") or "Failed to recover friendly REPL after interactive run"
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

    def sync_folder(
        self,
        port: str | None,
        local_folder: str,
        remote_folder: str,
        delete_extraneous: bool = False,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        try:
            local_root, directories, files = _scan_local_folder(local_folder, remote_folder)
        except Exception as exc:
            return {
                "ok": False,
                "port": port or "",
                "localFolder": str(Path(local_folder).expanduser().resolve()),
                "remoteFolder": str(remote_folder),
                "error": str(exc),
            }

        if port:
            opened = self.open(port)
            if not opened.get("ok"):
                return {
                    "ok": False,
                    "port": port,
                    "localFolder": str(local_root),
                    "remoteFolder": directories[0],
                    "error": opened.get("error", "Failed to open session."),
                }

        def report(line: str) -> None:
            if progress_callback is not None:
                progress_callback(line)

        with self._operation_lock:
            try:
                controller, pause_requested = self._begin_exclusive_operation()
            except Exception as exc:
                return {
                    "ok": False,
                    "port": port or "",
                    "localFolder": str(local_root),
                    "remoteFolder": directories[0],
                    "error": str(exc),
                }

            payload: dict[str, Any]
            recovery_payload: dict[str, Any] | None = None
            try:
                # Fast path: keep one raw REPL session open across sync operations
                # instead of enter/exit for every scan/dir/delete command.
                try:
                    enter_raw = getattr(controller, "_enter_raw_repl", None)
                    if callable(enter_raw):
                        enter_raw(timeout_overall=RAW_REPL_ENTER_TIMEOUT_SEC)
                except Exception:
                    pass

                remote_root = directories[0]
                report(f"Scanning {remote_root} on device…")
                remote_sizes: dict[str, int] = {}
                remote_scan_error: Exception | None = None

                for attempt in range(2):
                    self._raise_if_abort_requested()
                    try:
                        remote_sizes = _read_remote_file_sizes(controller, remote_root)
                        report(f"CalSci has {len(remote_sizes)} file(s)")
                        remote_scan_error = None
                        break
                    except ControllerError as exc:
                        remote_scan_error = exc
                        if _should_abort_for_exception(exc):
                            raise
                        if attempt == 0:
                            scan_recovery = _recover_after_run_failure(controller)
                            if scan_recovery.get("output"):
                                self._emit_terminal_text(str(scan_recovery["output"]))
                            if not scan_recovery.get("ok"):
                                restore_error = scan_recovery.get("error") or "Unknown recovery failure"
                                raise ControllerError(f"{exc} | scan recovery failed: {restore_error}") from exc
                            report(f"Remote scan failed ({exc}). Retrying after REPL recovery…")
                            continue
                        break
                    except Exception as exc:
                        remote_scan_error = exc
                        if attempt == 0:
                            report(f"Remote scan unavailable ({exc}). Retrying once…")
                            continue
                        break

                if remote_scan_error is not None:
                    raise remote_scan_error

                unchanged, to_upload, to_delete, extra_remote = _build_sync_plan(
                    files,
                    remote_sizes,
                    delete_extraneous=delete_extraneous,
                )
                unchanged_count = len(unchanged)

                report("─── Sync comparison ───")
                report(f"  Unchanged : {unchanged_count} file(s)")
                report(f"  To upload : {len(to_upload)} file(s)")
                report(f"  To delete : {len(to_delete)} file(s)")
                if not delete_extraneous and extra_remote:
                    report(f"  Extra remote kept : {len(extra_remote)} file(s)")
                report("───────────────────────")

                deleted_count = 0
                delete_failures: list[str] = []
                if to_delete:
                    report(f"Deleting {len(to_delete)} stale file(s)…")
                    for index, remote_path in enumerate(to_delete, start=1):
                        self._raise_if_abort_requested()
                        try:
                            if controller.sync_delete_file(remote_path):
                                deleted_count += 1
                                report(f"[{index}/{len(to_delete)}] Deleted: {remote_path}")
                            else:
                                delete_failures.append(remote_path)
                                report(f"[{index}/{len(to_delete)}] Failed: {remote_path}")
                        except Exception as exc:
                            if _should_abort_for_exception(exc):
                                raise
                            delete_failures.append(remote_path)
                            report(f"[{index}/{len(to_delete)}] Failed: {remote_path} ({exc})")

                directory_failures: list[str] = []
                if to_upload:
                    required_dirs = _build_sync_directory_plan(remote_root, to_upload)
                    device_dirs = [
                        device_dir
                        for device_dir in (_sync_device_relative_path(remote_dir) for remote_dir in required_dirs)
                        if device_dir
                    ]
                    report("Creating folder structure…")
                    for device_dir in device_dirs:
                        self._raise_if_abort_requested()
                        try:
                            if controller.sync_mkdir(device_dir):
                                report(f"  + {device_dir}")
                            else:
                                directory_failures.append(device_dir)
                                report(f"  ! {device_dir} (failed)")
                        except Exception as exc:
                            if _should_abort_for_exception(exc):
                                raise
                            directory_failures.append(device_dir)
                            report(f"  ! {device_dir} ({exc})")
                    report("Folder structure synced ✓")

                    synced_bytes = 0
                    uploaded_count = 0
                    upload_failures: list[str] = []
                    raw_upload_open = bool(getattr(controller, "_in_raw_repl", False))
                    report(f"Uploading {len(to_upload)} file(s)…")

                    try:
                        for index, file_info in enumerate(sorted(to_upload, key=lambda item: str(item["remote_path"])), start=1):
                            self._raise_if_abort_requested()
                            local_path = Path(file_info["local_path"])
                            relative_path = str(file_info["relative_path"])
                            remote_path = str(file_info["remote_path"])
                            remote_write_path = _sync_device_relative_path(remote_path)
                            file_size = int(file_info["size_bytes"])
                            uploaded = False

                            for attempt in range(SYNC_FILE_RETRY_COUNT):
                                self._raise_if_abort_requested()
                                try:
                                    if not raw_upload_open:
                                        controller.sync_enter_raw_repl()
                                        raw_upload_open = True
                                    controller.sync_put_raw(local_path, remote_write_path)
                                    synced_bytes += file_size
                                    uploaded_count += 1
                                    report(f"[{index}/{len(to_upload)}] Uploaded: {remote_path} ({file_size} bytes)")
                                    uploaded = True
                                    break
                                except Exception as exc:
                                    if _should_abort_for_exception(exc):
                                        raise SessionAbortedError(str(exc)) from exc
                                    if raw_upload_open:
                                        try:
                                            controller.sync_exit_raw_repl()
                                        except Exception:
                                            pass
                                        raw_upload_open = False
                                    if attempt + 1 < SYNC_FILE_RETRY_COUNT:
                                        retry_detail = ""
                                        reconnect = getattr(controller, "sync_reconnect", None)
                                        if callable(reconnect):
                                            try:
                                                reconnect()
                                                retry_detail = " after connection reset"
                                            except Exception as reconnect_exc:
                                                if _should_abort_for_exception(reconnect_exc):
                                                    raise SessionAbortedError(str(reconnect_exc)) from reconnect_exc
                                                retry_detail = f" after failed connection reset ({reconnect_exc})"
                                        report(f"[{index}/{len(to_upload)}] Retry: {relative_path} ({exc}){retry_detail}")
                                        time.sleep(SYNC_FILE_RETRY_DELAY_SEC)
                                        continue
                                    upload_failures.append(remote_path)
                                    report(f"[{index}/{len(to_upload)}] Failed: {remote_path} ({exc})")

                            if not uploaded:
                                continue
                    finally:
                        if raw_upload_open:
                            controller.sync_exit_raw_repl()
                else:
                    required_dirs = []
                    synced_bytes = 0
                    uploaded_count = 0
                    upload_failures = []
                    if not to_delete:
                        report("Everything is already in sync")

                directory_warning_count = 0
                if directory_failures and not upload_failures:
                    directory_warning_count = len(directory_failures)
                    report(
                        f"Directory creation reported {directory_warning_count} issue(s), "
                        "but uploads succeeded. Treating as warning."
                    )
                    directory_failures = []

                ok = not upload_failures and not delete_failures and not directory_failures
                error_summary = ""
                if ok:
                    report(
                        f"Sync complete: {uploaded_count} uploaded, {deleted_count} deleted, "
                        f"{unchanged_count} skipped, {synced_bytes} bytes -> {remote_root}"
                    )
                else:
                    error_summary = (
                        f"Sync finished with {len(upload_failures)} upload failure(s), "
                        f"{len(delete_failures)} delete failure(s), {len(directory_failures)} directory failure(s)."
                    )
                    report(error_summary)

                payload = {
                    "ok": ok,
                    "port": controller.port,
                    "localFolder": str(local_root),
                    "remoteFolder": remote_root,
                    "filesSynced": uploaded_count,
                    "filesDeleted": deleted_count,
                    "filesFailed": len(upload_failures),
                    "filesSkipped": unchanged_count,
                    "filesTotal": len(files),
                    "directoriesEnsured": len(required_dirs),
                    "directoriesFailed": len(directory_failures),
                    "directoriesWarnings": directory_warning_count,
                    "bytesSynced": synced_bytes,
                    "error": error_summary or None,
                }
            except Exception as exc:
                payload = {
                    "ok": False,
                    "port": controller.port,
                    "localFolder": str(local_root),
                    "remoteFolder": directories[0],
                    "error": str(exc),
                }
                if not _should_abort_for_exception(exc):
                    recovery_payload = _recover_after_run_failure(controller)
            finally:
                try:
                    if bool(getattr(controller, "_in_raw_repl", False)):
                        exit_raw = getattr(controller, "sync_exit_raw_repl", None)
                        if callable(exit_raw):
                            exit_raw()
                except Exception:
                    pass
                self._end_exclusive_operation(pause_requested)

        if recovery_payload and recovery_payload.get("output"):
            self._emit_terminal_text(str(recovery_payload["output"]))

        if recovery_payload and not recovery_payload.get("ok"):
            payload["ok"] = False
            existing_error = payload.get("error")
            restore_error = recovery_payload.get("error") or "Failed to recover friendly REPL after folder sync"
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

    def clear_all_files(
        self,
        port: str | None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        if port:
            opened = self.open(port)
            if not opened.get("ok"):
                return {
                    "ok": False,
                    "port": port,
                    "filesDeleted": 0,
                    "directoriesDeleted": 0,
                    "warningsReported": 0,
                    "bootCreated": False,
                    "error": opened.get("error", "Failed to open session."),
                }

        def report(line: str) -> None:
            if progress_callback is not None:
                progress_callback(line)

        with self._operation_lock:
            try:
                controller, pause_requested = self._begin_exclusive_operation()
            except Exception as exc:
                return {
                    "ok": False,
                    "port": port or "",
                    "filesDeleted": 0,
                    "directoriesDeleted": 0,
                    "warningsReported": 0,
                    "bootCreated": False,
                    "error": str(exc),
                }

            payload: dict[str, Any]
            recovery_payload: dict[str, Any] | None = None
            try:
                try:
                    enter_raw = getattr(controller, "_enter_raw_repl", None)
                    if callable(enter_raw):
                        enter_raw(timeout_overall=RAW_REPL_ENTER_TIMEOUT_SEC)
                except Exception:
                    pass

                report("Starting desktop-style CalSci cleanup…")
                cleanup_output = controller.sync_clear_all(timeout=SYNC_CLEAR_ALL_TIMEOUT_SEC)
                cleanup_summary = _parse_clear_all_output(cleanup_output)

                if not cleanup_summary["doneSeen"]:
                    raise ControllerError("Cleanup timeout - operation may be incomplete")

                for file_path in cleanup_summary["filesDeleted"]:
                    report(f"Deleted file: {file_path}")
                for dir_path in cleanup_summary["directoriesDeleted"]:
                    report(f"Deleted folder: {dir_path}")
                for warning_line in cleanup_summary["warningLines"]:
                    report(f"Warning: {warning_line}")
                for extra_line in cleanup_summary["otherLines"]:
                    report(extra_line)

                report("Creating empty boot.py…")
                controller.sync_put_content("boot.py", b"")
                report("Empty boot.py created ✓")

                warning_count = len(cleanup_summary["warningLines"])
                report(
                    "Clear complete: "
                    f"{len(cleanup_summary['filesDeleted'])} files deleted, "
                    f"{len(cleanup_summary['directoriesDeleted'])} folders deleted, "
                    f"{warning_count} warning(s)"
                )
                payload = {
                    "ok": True,
                    "port": controller.port,
                    "filesDeleted": len(cleanup_summary["filesDeleted"]),
                    "directoriesDeleted": len(cleanup_summary["directoriesDeleted"]),
                    "warningsReported": warning_count,
                    "bootCreated": True,
                }
            except Exception as exc:
                payload = {
                    "ok": False,
                    "port": controller.port,
                    "filesDeleted": 0,
                    "directoriesDeleted": 0,
                    "warningsReported": 0,
                    "bootCreated": False,
                    "error": str(exc),
                }
                if not _should_abort_for_exception(exc):
                    recovery_payload = _recover_after_run_failure(controller)
            finally:
                try:
                    if bool(getattr(controller, "_in_raw_repl", False)):
                        exit_raw = getattr(controller, "sync_exit_raw_repl", None)
                        if callable(exit_raw):
                            exit_raw()
                except Exception:
                    pass
                self._end_exclusive_operation(pause_requested)

        if recovery_payload and recovery_payload.get("output"):
            self._emit_terminal_text(str(recovery_payload["output"]))

        if recovery_payload and not recovery_payload.get("ok"):
            payload["ok"] = False
            existing_error = payload.get("error")
            restore_error = recovery_payload.get("error") or "Failed to recover friendly REPL after clear-all"
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

    def workspace_scan_tree(self, port: str | None) -> dict[str, Any]:
        if port:
            opened = self.open(port)
            if not opened.get("ok"):
                return {
                    "ok": False,
                    "port": port,
                    "entries": [],
                    "error": opened.get("error", "Failed to open session."),
                }

        with self._operation_lock:
            try:
                controller, pause_requested = self._begin_exclusive_operation()
            except Exception as exc:
                return {
                    "ok": False,
                    "port": port or "",
                    "entries": [],
                    "error": str(exc),
                }

            payload: dict[str, Any]
            recovery_payload: dict[str, Any] | None = None
            try:
                remote_dirs, remote_files = controller.sync_scan_tree("/", timeout=SYNC_SCAN_COMMAND_TIMEOUT_SEC)
                entries = [
                    {"path": remote_dir, "kind": "directory"}
                    for remote_dir in remote_dirs
                ]
                entries.extend(
                    {
                        "path": remote_path,
                        "kind": "file",
                        "size": int(file_size),
                    }
                    for remote_path, file_size in sorted(remote_files.items())
                )
                payload = {
                    "ok": True,
                    "port": controller.port,
                    "entries": entries,
                }
            except Exception as exc:
                payload = {
                    "ok": False,
                    "port": controller.port,
                    "entries": [],
                    "error": str(exc),
                }
                if not _should_abort_for_exception(exc):
                    recovery_payload = _recover_after_run_failure(controller)
            finally:
                self._end_exclusive_operation(pause_requested)

        if recovery_payload and recovery_payload.get("output"):
            self._emit_terminal_text(str(recovery_payload["output"]))

        if recovery_payload and not recovery_payload.get("ok"):
            payload["ok"] = False
            existing_error = payload.get("error")
            restore_error = recovery_payload.get("error") or "Failed to recover friendly REPL after workspace scan"
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

    def workspace_read_file(self, port: str | None, remote_path: str) -> dict[str, Any]:
        normalized_remote_path = _sync_device_absolute_path(remote_path)
        if port:
            opened = self.open(port)
            if not opened.get("ok"):
                return {
                    "ok": False,
                    "port": port,
                    "remotePath": normalized_remote_path,
                    "error": opened.get("error", "Failed to open session."),
                }

        with self._operation_lock:
            try:
                controller, pause_requested = self._begin_exclusive_operation()
            except Exception as exc:
                return {
                    "ok": False,
                    "port": port or "",
                    "remotePath": normalized_remote_path,
                    "error": str(exc),
                }

            payload: dict[str, Any]
            recovery_payload: dict[str, Any] | None = None
            try:
                content_text = controller.sync_read_file_text(
                    normalized_remote_path,
                    timeout=SYNC_SCAN_COMMAND_TIMEOUT_SEC,
                )
                payload = {
                    "ok": True,
                    "port": controller.port,
                    "remotePath": normalized_remote_path,
                    "content": content_text,
                }
            except Exception as exc:
                payload = {
                    "ok": False,
                    "port": controller.port,
                    "remotePath": normalized_remote_path,
                    "error": str(exc),
                }
                if not _should_abort_for_exception(exc):
                    recovery_payload = _recover_after_run_failure(controller)
            finally:
                self._end_exclusive_operation(pause_requested)

        if recovery_payload and recovery_payload.get("output"):
            self._emit_terminal_text(str(recovery_payload["output"]))

        if recovery_payload and not recovery_payload.get("ok"):
            payload["ok"] = False
            existing_error = payload.get("error")
            restore_error = recovery_payload.get("error") or "Failed to recover friendly REPL after file read"
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

    def import_workspace(
        self,
        port: str | None,
        local_folder: str,
        progress_callback: Callable[[str], None] | None = None,
        remote_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        local_root = Path(local_folder).expanduser().resolve()
        if local_root.exists() and not local_root.is_dir():
            return {
                "ok": False,
                "port": port or "",
                "localFolder": str(local_root),
                "error": f"Workspace import target is not a directory: {local_root}",
            }

        if port:
            opened = self.open(port)
            if not opened.get("ok"):
                return {
                    "ok": False,
                    "port": port,
                    "localFolder": str(local_root),
                    "error": opened.get("error", "Failed to open session."),
                }

        def report(line: str) -> None:
            if progress_callback is not None:
                progress_callback(line)

        with self._operation_lock:
            try:
                controller, pause_requested = self._begin_exclusive_operation()
            except Exception as exc:
                return {
                    "ok": False,
                    "port": port or "",
                    "localFolder": str(local_root),
                    "error": str(exc),
                }

            payload: dict[str, Any]
            recovery_payload: dict[str, Any] | None = None
            try:
                local_root.mkdir(parents=True, exist_ok=True)

                try:
                    enter_raw = getattr(controller, "_enter_raw_repl", None)
                    if callable(enter_raw):
                        enter_raw(timeout_overall=RAW_REPL_ENTER_TIMEOUT_SEC)
                except Exception:
                    pass

                report("Scanning CalSci workspace…")
                remote_dirs, remote_files = controller.sync_scan_tree("/", timeout=SYNC_SCAN_COMMAND_TIMEOUT_SEC)
                selected_dirs, selected_files = _select_workspace_entries(remote_dirs, remote_files, remote_paths)
                report(f"Found {len(selected_files)} file(s) and {len(selected_dirs)} folder(s)")

                ensured_dirs = 0
                for remote_dir in selected_dirs:
                    self._raise_if_abort_requested()
                    relative_dir = _sync_device_relative_path(remote_dir)
                    if not relative_dir:
                        continue
                    (local_root / Path(relative_dir)).mkdir(parents=True, exist_ok=True)
                    ensured_dirs += 1

                bytes_imported = 0
                imported_files = 0
                sorted_files = sorted(selected_files.items())
                for index, (remote_path, file_size) in enumerate(sorted_files, start=1):
                    self._raise_if_abort_requested()
                    relative_file = _sync_device_relative_path(remote_path)
                    local_path = local_root / Path(relative_file)
                    local_path.parent.mkdir(parents=True, exist_ok=True)

                    timeout = min(
                        WORKSPACE_IMPORT_FILE_TIMEOUT_MAX_SEC,
                        max(5.0, 2.0 + (max(0, int(file_size)) / WORKSPACE_IMPORT_FILE_THROUGHPUT_BYTES_PER_SEC)),
                    )
                    content = controller.sync_read_file_bytes(remote_path, timeout=timeout)
                    local_path.write_bytes(content)
                    imported_files += 1
                    bytes_imported += len(content)
                    report(f"[{index}/{len(sorted_files)}] Imported: {remote_path}")

                report(
                    f"Workspace import complete: {imported_files} file(s), "
                    f"{ensured_dirs} folder(s), {bytes_imported} bytes"
                )
                payload = {
                    "ok": True,
                    "port": controller.port,
                    "localFolder": str(local_root),
                    "filesImported": imported_files,
                    "directoriesImported": ensured_dirs,
                    "bytesImported": bytes_imported,
                }
            except Exception as exc:
                payload = {
                    "ok": False,
                    "port": controller.port,
                    "localFolder": str(local_root),
                    "error": str(exc),
                }
                if not _should_abort_for_exception(exc):
                    recovery_payload = _recover_after_run_failure(controller)
            finally:
                try:
                    if bool(getattr(controller, "_in_raw_repl", False)):
                        exit_raw = getattr(controller, "sync_exit_raw_repl", None)
                        if callable(exit_raw):
                            exit_raw()
                except Exception:
                    pass
                self._end_exclusive_operation(pause_requested)

        if recovery_payload and recovery_payload.get("output"):
            self._emit_terminal_text(str(recovery_payload["output"]))

        if recovery_payload and not recovery_payload.get("ok"):
            payload["ok"] = False
            existing_error = payload.get("error")
            restore_error = recovery_payload.get("error") or "Failed to recover friendly REPL after workspace import"
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
            was_suppressed = self._suppress_terminal_helper_output
            self._suppress_terminal_helper_output = True
            self._suppress_terminal_helper_depth += 1
            self._suppress_terminal_helper_output_deadline = time.monotonic() + (
                max(
                    HYBRID_HELPER_COMMAND_TIMEOUT_SEC,
                    HYBRID_HELPER_ENABLE_TIMEOUT_SEC,
                    HYBRID_HELPER_POLL_TIMEOUT_SEC,
                )
                + 1.0
            )
            if not was_suppressed:
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
                if _looks_like_helper_terminal_fragment(self._helper_line_buffer):
                    self._helper_line_buffer = ""
                if _looks_like_helper_terminal_fragment(self._helper_frame_remainder):
                    self._helper_frame_remainder = ""
                self._suppress_terminal_helper_output = False
                self._suppress_terminal_helper_depth = 0
                self._suppress_terminal_helper_output_deadline = 0.0
                self._suppress_terminal_helper_activity_seen = False
                suppress_helper_output = False
            started_suppressed = suppress_helper_output

            stream_text = self._helper_frame_remainder + normalized
            visible_text, framed_payloads, frame_remainder = _split_helper_framed_text(stream_text)
            self._helper_frame_remainder = frame_remainder
            helper_content_seen = bool(framed_payloads or frame_remainder)

            for framed in framed_payloads:
                cleaned = _strip_repl_prompt_prefix(framed.replace("\r", "").strip()).strip()
                if not cleaned:
                    continue
                self._helper_line_seq += 1
                self._helper_lines.append((self._helper_line_seq, cleaned))
                status_payloads, state_payloads = self._process_helper_line_locked(cleaned)
                status_events.extend(status_payloads)
                state_events.extend(state_payloads)
                helper_content_seen = True
                if suppress_helper_output or started_suppressed:
                    helper_activity_seen = True
                    self._suppress_terminal_helper_activity_seen = True

            self._helper_line_buffer += visible_text
            while True:
                newline = self._helper_line_buffer.find("\n")
                if newline < 0:
                    break
                raw_line = self._helper_line_buffer[:newline]
                self._helper_line_buffer = self._helper_line_buffer[newline + 1 :]
                raw_line_is_helper_fragment = _looks_like_helper_terminal_fragment(raw_line)
                prompt_only_fragment = _is_prompt_only_fragment(raw_line)
                cleaned = _clean_helper_line(raw_line)
                if not cleaned:
                    if prompt_only_fragment:
                        with self._hybrid_lock:
                            self._hybrid_pause_until_prompt = False
                    if started_suppressed and prompt_only_fragment:
                        if helper_activity_seen:
                            suppress_helper_output = self._consume_helper_prompt_locked()
                            helper_activity_seen = self._suppress_terminal_helper_activity_seen
                    elif started_suppressed and raw_line_is_helper_fragment:
                        helper_activity_seen = True
                        self._suppress_terminal_helper_activity_seen = True
                        helper_content_seen = True
                    elif raw_line.strip() and not prompt_only_fragment and not raw_line_is_helper_fragment:
                        visible_chunks.append(raw_line + "\n")
                    continue

                self._helper_line_seq += 1
                self._helper_lines.append((self._helper_line_seq, cleaned))

                status_payloads, state_payloads = self._process_helper_line_locked(cleaned)
                status_events.extend(status_payloads)
                state_events.extend(state_payloads)

                helper_line = _looks_like_helper_terminal_line(cleaned) or raw_line_is_helper_fragment
                if helper_line:
                    helper_content_seen = True
                if suppress_helper_output and helper_line:
                    helper_activity_seen = True
                    self._suppress_terminal_helper_activity_seen = True
                    continue
                if raw_line.strip() and not helper_line:
                    visible_chunks.append(raw_line + "\n")

            if self._helper_line_buffer and _is_prompt_only_fragment(self._helper_line_buffer):
                with self._hybrid_lock:
                    self._hybrid_pause_until_prompt = False
                if suppress_helper_output:
                    self._helper_line_buffer = ""
                    if helper_activity_seen:
                        suppress_helper_output = self._consume_helper_prompt_locked()

            self._helper_condition.notify_all()

        if started_suppressed or helper_content_seen:
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

    def _consume_helper_prompt_locked(self) -> bool:
        if self._suppress_terminal_helper_depth > 0:
            self._suppress_terminal_helper_depth -= 1
        if self._suppress_terminal_helper_depth > 0:
            return True
        self._suppress_terminal_helper_output = False
        self._suppress_terminal_helper_output_deadline = 0.0
        self._suppress_terminal_helper_activity_seen = False
        self._suppress_terminal_helper_depth = 0
        return False

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
        self._abort_requested.clear()
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
            self._helper_frame_remainder = ""
            self._helper_lines.clear()
            self._helper_line_seq = 0
            self._helper_state_seq = 0
            self._suppress_terminal_helper_output = False
            self._suppress_terminal_helper_depth = 0
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
        self._abort_requested.set()
        with self._lock:
            if self._controller is not controller:
                return
            detached = self._detach_session_locked()
        self._set_hybrid_inactive(error=error, reason="reader-failed")
        self._teardown_detached(detached)
        self._emit_session_state_event(error=error, reason="reader-failed")

    def _begin_exclusive_operation(self) -> tuple[CalSciController, threading.Event]:
        self._raise_if_abort_requested()
        with self._lock:
            controller = self._controller
            pause_requested = self._reader_pause_requested
            paused_event = self._reader_paused

        if controller is None:
            raise ControllerError("No open CalSci session.")

        pause_requested.set()
        if not paused_event.wait(timeout=READER_PAUSE_WAIT_SEC):
            pause_requested.clear()
            raise ControllerError("Session reader did not pause in time.")

        self._raise_if_abort_requested()

        # Drop any buffered async output before running an exclusive command.
        # This avoids mixing stale terminal traffic into raw REPL command output.
        controller._drain_serial_input()
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

    def abort_session_activity(self, reason: str = "aborted") -> dict[str, Any]:
        cancel_result = self.cancel_active_run()
        self._session.abort(reason=reason)
        return {
            "ok": True,
            "connected": False,
            "port": None,
            "reason": reason,
            "activeRunCancelled": bool(cancel_result.get("cancelled")),
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

    def _prepare_bootloader_for_flash_operation(
        self,
        requested_port: str,
        *,
        manual_bootloader: bool,
        close_reason: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> tuple[str, bool]:
        target_port = str(requested_port or "").strip()
        bootloader_ready = False
        prepare_result: dict[str, Any] | None = None

        if not manual_bootloader:
            prepare_result = self._session.request_bootloader(
                port=target_port or None,
                progress_callback=progress_callback,
            )

        self._session.close(reason=close_reason)

        if manual_bootloader:
            return target_port, False

        if prepare_result and prepare_result.get("ok") and prepare_result.get("prepared"):
            target_port = str(prepare_result.get("port") or target_port)
            try:
                target_port = _wait_for_bootloader_ready(
                    target_port,
                    progress_callback=progress_callback,
                    timeout_seconds=FIRMWARE_FLASH_AUTO_BOOT_TIMEOUT_SEC,
                    wait_message="Waiting for automatic bootloader confirmation...",
                )
                bootloader_ready = True
            except Exception as exc:
                if progress_callback is not None:
                    progress_callback(f"Automatic bootloader request did not confirm: {exc}. Falling back to host reset.")
        elif prepare_result and prepare_result.get("error") and progress_callback is not None:
            progress_callback(f"Automatic bootloader request failed: {prepare_result['error']}. Falling back to host reset.")

        return target_port, bootloader_ready

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
                return {"ok": True, "devices": list_detected_esp_ports()}
            if job.command == "session.open":
                return self._session.open(port=str(args["port"]))
            if job.command == "session.close":
                reason = str(args.get("reason") or "closed-by-command")
                return self._session.close(reason=reason)
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
            if job.command == "run-file-interactive":
                return self._session.run_file_interactive(
                    port=_optional_arg_string(args, "port"),
                    local_file=str(args["localFile"]),
                )
            if job.command == "sync-folder":
                return self._session.sync_folder(
                    port=_optional_arg_string(args, "port"),
                    local_folder=str(args["localFolder"]),
                    remote_folder=str(args["remoteFolder"]),
                    delete_extraneous=bool(args.get("deleteExtraneous", False)),
                    progress_callback=(
                        lambda line, req_id=job.request_id: _service_emit(
                            {"id": req_id, "type": "stream", "stream": "stdout", "line": line}
                        )
                    ) if job.stream else None,
                )
            if job.command == "clear-all-files":
                return self._session.clear_all_files(
                    port=_optional_arg_string(args, "port"),
                    progress_callback=(
                        lambda line, req_id=job.request_id: _service_emit(
                            {"id": req_id, "type": "stream", "stream": "stdout", "line": line}
                        )
                    ) if job.stream else None,
                )
            if job.command == "workspace.scan-tree":
                return self._session.workspace_scan_tree(
                    port=_optional_arg_string(args, "port"),
                )
            if job.command == "workspace.read-file":
                return self._session.workspace_read_file(
                    port=_optional_arg_string(args, "port"),
                    remote_path=str(args["remotePath"]),
                )
            if job.command == "workspace.import":
                return self._session.import_workspace(
                    port=_optional_arg_string(args, "port"),
                    local_folder=str(args["localFolder"]),
                    remote_paths=_optional_arg_string_list(args, "remotePaths"),
                    progress_callback=(
                        lambda line, req_id=job.request_id: _service_emit(
                            {"id": req_id, "type": "stream", "stream": "stdout", "line": line}
                        )
                    ) if job.stream else None,
                )
            if job.command == "chip.erase":
                progress_callback = (
                    lambda line, req_id=job.request_id: _service_emit(
                        {"id": req_id, "type": "stream", "stream": "stdout", "line": line}
                    )
                ) if job.stream else None
                erase_port, bootloader_ready = self._prepare_bootloader_for_flash_operation(
                    _optional_arg_string(args, "port") or "",
                    manual_bootloader=bool(args.get("manualBootloader", False)),
                    close_reason="chip-erase",
                    progress_callback=progress_callback,
                )
                return erase_chip(
                    port=erase_port,
                    manual_bootloader=bool(args.get("manualBootloader", False)),
                    bootloader_ready=bootloader_ready,
                    progress_callback=progress_callback,
                )
            if job.command == "firmware.flash":
                progress_callback = (
                    lambda line, req_id=job.request_id: _service_emit(
                        {"id": req_id, "type": "stream", "stream": "stdout", "line": line}
                    )
                ) if job.stream else None
                flash_port, bootloader_ready = self._prepare_bootloader_for_flash_operation(
                    _optional_arg_string(args, "port") or "",
                    manual_bootloader=bool(args.get("manualBootloader", False)),
                    close_reason="firmware-flash",
                    progress_callback=progress_callback,
                )
                return flash_firmware_bundle(
                    port=flash_port,
                    bootloader_path=str(args["bootloaderPath"]),
                    calos_path=str(args["calOsPath"]),
                    partition_table_path=str(args["partitionTablePath"]),
                    ota_data_path=str(args["otaDataPath"]),
                    manual_bootloader=bool(args.get("manualBootloader", False)),
                    bootloader_ready=bootloader_ready,
                    progress_callback=progress_callback,
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


def _optional_arg_string_list(args: dict[str, Any], key: str) -> list[str] | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = value
    elif isinstance(value, set):
        items = sorted(value, key=str)
    else:
        items = [value]
    normalized = [str(item).strip() for item in items if str(item).strip()]
    return normalized or None


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


def _build_esptool_multi_write_cmd(
    port: str,
    image_pairs: list[tuple[str, Path]],
    baudrate: int,
    before: str,
    after: str,
    chip: str = FIRMWARE_FLASH_CHIP,
    connect_attempts: int = FIRMWARE_FLASH_CONNECT_ATTEMPTS,
) -> list[str]:
    if importlib.util.find_spec("esptool") is not None:
        cmd = [
            sys.executable,
            "-m",
            "esptool",
            "--chip",
            chip,
            "--port",
            port,
            "--baud",
            str(baudrate),
            "--connect-attempts",
            str(connect_attempts),
            "--before",
            before,
            "--after",
            after,
            "write-flash",
        ]
    else:
        cmd = [
            "esptool",
            "--chip",
            chip,
            "--port",
            port,
            "--baud",
            str(baudrate),
            "--connect-attempts",
            str(connect_attempts),
            "--before",
            before,
            "--after",
            after,
            "write-flash",
        ]
    for offset, image_path in image_pairs:
        cmd.extend([offset, str(image_path)])
    return cmd


def _build_esptool_boot_cmd(
    port: str,
    baudrate: int = FIRMWARE_FLASH_BAUDRATE,
    before: str = "no-reset",
    after: str = "no-reset",
    chip: str = FIRMWARE_FLASH_CHIP,
    connect_attempts: int = FIRMWARE_FLASH_CONNECT_ATTEMPTS,
) -> list[str]:
    if importlib.util.find_spec("esptool") is not None:
        return [
            sys.executable,
            "-m",
            "esptool",
            "--chip",
            chip,
            "--port",
            port,
            "--baud",
            str(baudrate),
            "--connect-attempts",
            str(connect_attempts),
            "--before",
            before,
            "--after",
            after,
            "chip-id",
        ]
    return [
        "esptool",
        "--chip",
        chip,
        "--port",
        port,
        "--baud",
        str(baudrate),
        "--connect-attempts",
        str(connect_attempts),
        "--before",
        before,
        "--after",
        after,
        "chip-id",
    ]


def _build_esptool_erase_cmd(
    port: str,
    baudrate: int,
    before: str,
    after: str,
    chip: str = FIRMWARE_FLASH_CHIP,
    connect_attempts: int = FIRMWARE_FLASH_CONNECT_ATTEMPTS,
) -> list[str]:
    if importlib.util.find_spec("esptool") is not None:
        return [
            sys.executable,
            "-m",
            "esptool",
            "--chip",
            chip,
            "--port",
            port,
            "--baud",
            str(baudrate),
            "--connect-attempts",
            str(connect_attempts),
            "--before",
            before,
            "--after",
            after,
            "erase-flash",
        ]
    return [
        "esptool",
        "--chip",
        chip,
        "--port",
        port,
        "--baud",
        str(baudrate),
        "--connect-attempts",
        str(connect_attempts),
        "--before",
        before,
        "--after",
        after,
        "erase-flash",
    ]


def _run_esptool(cmd: list[str], progress_callback: Callable[[str], None] | None = None) -> None:
    if progress_callback is not None:
        progress_callback(f"Running: {' '.join(cmd)}")
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError as exc:
        raise ControllerError(f"esptool not found: {exc}") from exc

    output_lines: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        cleaned = line.rstrip()
        output_lines.append(cleaned)
        if progress_callback is not None and cleaned:
            progress_callback(cleaned)

    return_code = process.wait()
    if return_code != 0:
        tail = "\n".join(output_lines[-10:]) if output_lines else "No output"
        raise ControllerError(f"esptool failed (exit {return_code}).\n{tail}")


def _is_esptool_connect_error(error_text: str) -> bool:
    lowered = error_text.lower()
    needles = (
        "write timeout",
        "failed to connect",
        "timed out waiting for packet header",
        "serial exception",
        "could not open port",
        "device not found",
        "no serial data received",
    )
    return any(needle in lowered for needle in needles)


def _retry_baud_candidates(primary_baud: int) -> list[int]:
    bauds: list[int] = []
    for baud in (460800, primary_baud, 230400, 115200):
        try:
            value = int(baud)
        except Exception:
            continue
        if value > 0 and value not in bauds:
            bauds.append(value)
    return bauds


def _scan_esp_ports() -> list[str]:
    strict_ports: list[str] = []
    fallback_ports: list[str] = []
    for port in list_ports.comports():
        device = str(port.device or "")
        text = f"{port.manufacturer or ''} {port.product or ''} {port.description or ''}".lower()
        vid = getattr(port, "vid", None)
        if any(keyword.lower() in text for keyword in ESP32_KEYWORDS) or vid == 0x303A:
            if device:
                strict_ports.append(device)
            continue
        if device.startswith("/dev/ttyACM") or device.startswith("/dev/ttyUSB") or device.upper().startswith("COM"):
            fallback_ports.append(device)

    ordered: list[str] = []
    seen: set[str] = set()
    for device in strict_ports + fallback_ports:
        if device and device not in seen:
            seen.add(device)
            ordered.append(device)
    return ordered


def _wait_for_esp_port(preferred: str, progress_callback: Callable[[str], None] | None = None) -> str:
    deadline = time.time() + FIRMWARE_FLASH_PORT_RESCAN_TIMEOUT_SEC
    while time.time() < deadline:
        ports = _scan_esp_ports()
        if preferred in ports:
            return preferred
        if ports:
            if progress_callback is not None and ports[0] != preferred:
                progress_callback(f"Port changed: {preferred} -> {ports[0]}")
            return ports[0]
        time.sleep(FIRMWARE_FLASH_PORT_RESCAN_INTERVAL_SEC)
    return preferred


def _detect_initial_flash_port(
    preferred: str | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> str:
    preferred_port = str(preferred or "").strip()
    ports = _scan_esp_ports()

    if preferred_port:
        if preferred_port in ports:
            if progress_callback is not None:
                progress_callback(f"Using preferred port {preferred_port}")
            return preferred_port
        if ports:
            if progress_callback is not None:
                progress_callback(f"Preferred port {preferred_port} not detected; using {ports[0]}")
            return ports[0]
        if progress_callback is not None:
            progress_callback(f"No ESP port detected; trying preferred port {preferred_port}")
        return preferred_port

    if ports:
        if progress_callback is not None:
            progress_callback(f"Using detected ESP port {ports[0]}")
        return ports[0]

    raise ControllerError("No ESP device detected for erase or flash operations.")


def _run_esptool_with_connect_retries(
    image_pairs: list[tuple[str, Path]],
    port: str,
    progress_callback: Callable[[str], None] | None = None,
    before_modes: tuple[str, ...] | None = None,
    after_mode: str = FIRMWARE_FLASH_AFTER,
) -> str:
    if before_modes is None:
        ordered_modes: list[str] = []
        for mode in ("default-reset", FIRMWARE_FLASH_BEFORE, "usb-reset"):
            if mode and mode not in ordered_modes:
                ordered_modes.append(mode)
        before_modes = tuple(ordered_modes)
    last_error: Exception | None = None
    attempt = 0

    for before_mode in before_modes:
        for baud in _retry_baud_candidates(FIRMWARE_FLASH_BAUDRATE):
            attempt += 1
            if attempt > 1 and progress_callback is not None:
                progress_callback(f"Retry with --before {before_mode}, --baud {baud}")
            try:
                cmd = _build_esptool_multi_write_cmd(
                    port=port,
                    image_pairs=image_pairs,
                    baudrate=baud,
                    before=before_mode,
                    after=after_mode,
                )
                _run_esptool(cmd, progress_callback=progress_callback)
                return _wait_for_esp_port(port, progress_callback=progress_callback)
            except Exception as exc:
                last_error = exc
                if not _is_esptool_connect_error(str(exc)):
                    raise
                time.sleep(0.5)
                port = _wait_for_esp_port(port, progress_callback=progress_callback)

    if last_error is None:
        raise ControllerError("firmware-upload: unknown esptool failure")
    raise last_error


def _run_esptool_erase_with_connect_retries(
    port: str,
    progress_callback: Callable[[str], None] | None = None,
    before_modes: tuple[str, ...] | None = None,
    after_mode: str = FIRMWARE_FLASH_AFTER,
) -> str:
    if before_modes is None:
        ordered_modes: list[str] = []
        for mode in ("default-reset", FIRMWARE_FLASH_BEFORE, "usb-reset"):
            if mode and mode not in ordered_modes:
                ordered_modes.append(mode)
        before_modes = tuple(ordered_modes)
    last_error: Exception | None = None
    attempt = 0

    for before_mode in before_modes:
        for baud in _retry_baud_candidates(FIRMWARE_FLASH_BAUDRATE):
            attempt += 1
            if attempt > 1 and progress_callback is not None:
                progress_callback(f"Retry with --before {before_mode}, --baud {baud}")
            try:
                cmd = _build_esptool_erase_cmd(
                    port=port,
                    baudrate=baud,
                    before=before_mode,
                    after=after_mode,
                )
                _run_esptool(cmd, progress_callback=progress_callback)
                return _wait_for_esp_port(port, progress_callback=progress_callback)
            except Exception as exc:
                last_error = exc
                if not _is_esptool_connect_error(str(exc)):
                    raise
                time.sleep(0.5)
                port = _wait_for_esp_port(port, progress_callback=progress_callback)

    if last_error is None:
        raise ControllerError("chip-erase: unknown esptool failure")
    raise last_error


def _wait_for_bootloader_ready(
    port: str,
    progress_callback: Callable[[str], None] | None = None,
    timeout_seconds: float = FIRMWARE_FLASH_AUTO_BOOT_TIMEOUT_SEC,
    require_port_reset: bool = False,
    wait_message: str | None = None,
) -> str:
    deadline = time.time() + max(1.0, timeout_seconds)
    last_error = ""
    current_port = port
    missing_seen = False

    if progress_callback is not None and wait_message:
        progress_callback(wait_message)

    while time.time() < deadline:
        ports = _scan_esp_ports()
        if current_port not in ports:
            missing_seen = True
        if current_port not in ports and ports:
            if progress_callback is not None and ports[0] != current_port:
                progress_callback(f"Port changed: {current_port} -> {ports[0]}")
            current_port = ports[0]

        if require_port_reset and not missing_seen:
            time.sleep(FIRMWARE_FLASH_PORT_RESCAN_INTERVAL_SEC)
            continue

        try:
            boot_cmd = _build_esptool_boot_cmd(port=current_port, before="no-reset", after="no-reset")
            _run_esptool(boot_cmd, progress_callback=None)
            if progress_callback is not None:
                progress_callback("Bootloader confirmed.")
            return _wait_for_esp_port(current_port, progress_callback=progress_callback)
        except Exception as exc:
            last_error = str(exc)

        time.sleep(FIRMWARE_FLASH_PORT_RESCAN_INTERVAL_SEC)

    if require_port_reset and not missing_seen:
        raise ControllerError("Bootloader signal not detected (port did not reset)")
    if last_error:
        raise ControllerError(f"Bootloader confirmation timeout. {last_error[:160]}")
    raise ControllerError("Bootloader confirmation timeout.")


def _confirm_manual_bootloader(
    port: str,
    progress_callback: Callable[[str], None] | None = None,
    timeout_seconds: float = FIRMWARE_FLASH_MANUAL_BOOT_TIMEOUT_SEC,
) -> str:
    return _wait_for_bootloader_ready(
        port,
        progress_callback=progress_callback,
        timeout_seconds=timeout_seconds,
        require_port_reset=True,
        wait_message="Waiting for manual bootloader confirmation (hold BOOT, tap RESET, release BOOT)...",
    )


def flash_firmware_bundle(
    port: str,
    bootloader_path: str,
    calos_path: str,
    partition_table_path: str,
    ota_data_path: str,
    manual_bootloader: bool = False,
    bootloader_ready: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    requested_port = str(port or "").strip()

    images = {
        "bootloader": Path(bootloader_path).expanduser().resolve(),
        "calos": Path(calos_path).expanduser().resolve(),
        "partition table": Path(partition_table_path).expanduser().resolve(),
        "ota data": Path(ota_data_path).expanduser().resolve(),
    }
    for label, image_path in images.items():
        if not image_path.exists():
            return {
                "ok": False,
                "port": requested_port,
                "bootloaderPath": str(images["bootloader"]),
                "calOsPath": str(images["calos"]),
                "partitionTablePath": str(images["partition table"]),
                "otaDataPath": str(images["ota data"]),
                "error": f"{label.capitalize()} image not found: {image_path}",
            }
        if not image_path.is_file():
            return {
                "ok": False,
                "port": requested_port,
                "bootloaderPath": str(images["bootloader"]),
                "calOsPath": str(images["calos"]),
                "partitionTablePath": str(images["partition table"]),
                "otaDataPath": str(images["ota data"]),
                "error": f"{label.capitalize()} path is not a file: {image_path}",
            }

    image_pairs = [
        (FIRMWARE_FLASH_BOOTLOADER_OFFSET, images["bootloader"]),
        (FIRMWARE_FLASH_PARTITION_OFFSET, images["partition table"]),
        (FIRMWARE_FLASH_OTA_DATA_OFFSET, images["ota data"]),
        (FIRMWARE_FLASH_CALOS_OFFSET, images["calos"]),
    ]

    try:
        flash_port = _detect_initial_flash_port(requested_port, progress_callback=progress_callback)
        use_no_reset = False
        if manual_bootloader:
            if progress_callback is not None:
                progress_callback("Manual bootloader mode requested.")
            flash_port = _confirm_manual_bootloader(flash_port, progress_callback=progress_callback)
            use_no_reset = True
        elif bootloader_ready:
            use_no_reset = True
            if progress_callback is not None:
                progress_callback("Automatic bootloader entry confirmed.")
        elif progress_callback is not None:
            progress_callback("Using automatic bootloader entry mode.")

        if progress_callback is not None:
            progress_callback("Flashing bootloader + partition table + OTA data + CalOS...")
        flashed_port = _run_esptool_with_connect_retries(
            image_pairs,
            flash_port,
            progress_callback=progress_callback,
            before_modes=("no-reset",) if use_no_reset else None,
        )
        if progress_callback is not None:
            progress_callback(f"Firmware flash complete on {flashed_port}")
        return {
            "ok": True,
            "port": flashed_port,
            "bootloaderPath": str(images["bootloader"]),
            "calOsPath": str(images["calos"]),
            "partitionTablePath": str(images["partition table"]),
            "otaDataPath": str(images["ota data"]),
        }
    except Exception as exc:
        return {
            "ok": False,
            "port": requested_port,
            "bootloaderPath": str(images["bootloader"]),
            "calOsPath": str(images["calos"]),
            "partitionTablePath": str(images["partition table"]),
            "otaDataPath": str(images["ota data"]),
            "error": str(exc),
        }


def erase_chip(
    port: str,
    manual_bootloader: bool = False,
    bootloader_ready: bool = False,
    progress_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    requested_port = str(port or "").strip()
    erase_port = requested_port

    try:
        erase_port = _detect_initial_flash_port(requested_port, progress_callback=progress_callback)
        use_no_reset = False
        if manual_bootloader:
            if progress_callback is not None:
                progress_callback("Manual bootloader mode requested.")
            erase_port = _confirm_manual_bootloader(erase_port, progress_callback=progress_callback)
            use_no_reset = True
        elif bootloader_ready:
            use_no_reset = True
            if progress_callback is not None:
                progress_callback("Automatic bootloader entry confirmed.")
        elif progress_callback is not None:
            progress_callback("Using automatic bootloader entry mode.")

        if progress_callback is not None:
            progress_callback("Erasing chip flash...")
        erased_port = _run_esptool_erase_with_connect_retries(
            erase_port,
            progress_callback=progress_callback,
            before_modes=("no-reset",) if use_no_reset else None,
        )
        if progress_callback is not None:
            progress_callback(f"Chip erase complete on {erased_port}")
        return {
            "ok": True,
            "port": erased_port,
        }
    except Exception as exc:
        return {
            "ok": False,
            "port": erase_port or requested_port,
            "error": str(exc),
        }


def list_detected_esp_ports() -> list[dict[str, str]]:
    current_ports = {str(port.device or ""): port for port in list_ports.comports()}
    devices: list[dict[str, str]] = []

    for device in _scan_esp_ports():
        port = current_ports.get(device)
        product = ""
        description = ""
        if port is not None:
            product = (port.product or port.manufacturer or "").strip()
            description = (port.description or "").strip()
        devices.append(
            {
                "port": device,
                "product": product,
                "description": description,
            }
        )

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


def _normalize_remote_folder(remote_folder: str) -> str:
    return _sync_core.normalize_remote_folder(remote_folder)


def _sync_device_relative_path(remote_path: str) -> str:
    return _sync_core.sync_device_relative_path(remote_path)


def _sync_device_absolute_path(remote_path: str) -> str:
    return _sync_core.sync_device_absolute_path(remote_path)


def _select_workspace_entries(
    remote_dirs: list[str],
    remote_files: dict[str, int],
    selected_paths: list[str] | None,
) -> tuple[list[str], dict[str, int]]:
    normalized_dirs = {
        _sync_device_absolute_path(remote_dir)
        for remote_dir in remote_dirs
        if _sync_device_absolute_path(remote_dir) != "/"
    }
    normalized_files = {
        _sync_device_absolute_path(remote_path): int(file_size)
        for remote_path, file_size in remote_files.items()
    }

    if not selected_paths:
        return sorted(normalized_dirs), dict(sorted(normalized_files.items()))

    selected_dirs: set[str] = set()
    selected_files: dict[str, int] = {}
    normalized_selected: list[str] = []
    missing_paths: list[str] = []
    for remote_path in selected_paths:
        normalized_path = _sync_device_absolute_path(remote_path)
        if normalized_path not in normalized_selected:
            normalized_selected.append(normalized_path)

    for normalized_path in normalized_selected:
        if normalized_path == "/":
            return sorted(normalized_dirs), dict(sorted(normalized_files.items()))

        if normalized_path in normalized_files:
            selected_files[normalized_path] = normalized_files[normalized_path]
            parent = posixpath.dirname(normalized_path)
            while parent and parent != "/":
                if parent in normalized_dirs:
                    selected_dirs.add(parent)
                parent = posixpath.dirname(parent)
            continue

        if normalized_path in normalized_dirs:
            prefix = f"{normalized_path.rstrip('/')}/"
            selected_dirs.add(normalized_path)
            parent = posixpath.dirname(normalized_path)
            while parent and parent != "/":
                if parent in normalized_dirs:
                    selected_dirs.add(parent)
                parent = posixpath.dirname(parent)
            for remote_dir in normalized_dirs:
                if remote_dir.startswith(prefix):
                    selected_dirs.add(remote_dir)
            for remote_file, file_size in normalized_files.items():
                if remote_file.startswith(prefix):
                    selected_files[remote_file] = file_size
            continue

        missing_paths.append(normalized_path)

    if not selected_dirs and not selected_files:
        raise ValueError(
            "Selected CalSci files or folders were not found on the device: "
            + ", ".join(missing_paths or normalized_selected)
        )

    return sorted(selected_dirs), dict(sorted(selected_files.items()))


def _fnv1a32_bytes(data: bytes) -> str:
    return _sync_core.fnv1a32_bytes(data)


def _compute_local_file_signature(local_path: Path, chunk_size: int = 4096) -> str:
    return _sync_core.compute_local_file_signature(local_path, chunk_size=chunk_size)


def _should_skip_sync_dir(name: str) -> bool:
    return _sync_core.should_skip_sync_dir(name)


def _should_skip_sync_file(relative_path: Path) -> bool:
    return _sync_core.should_skip_sync_file(relative_path)


def _scan_local_folder(local_folder: str, remote_folder: str) -> tuple[Path, list[str], list[dict[str, Any]]]:
    return _sync_core.scan_local_folder(local_folder, remote_folder)


def _build_sync_directory_plan(remote_root: str, files: list[dict[str, Any]]) -> list[str]:
    return _sync_core.build_sync_directory_plan(remote_root, files)


def _build_sync_plan(
    files: list[dict[str, Any]],
    remote_sizes: dict[str, int],
    delete_extraneous: bool,
    signature_matches: set[str] | None = None,
    size_fallback_paths: set[str] | None = None,
) -> tuple[list[str], list[dict[str, Any]], list[str], list[str]]:
    return _sync_core.build_sync_plan(
        files,
        remote_sizes,
        delete_extraneous,
        signature_matches=signature_matches,
        size_fallback_paths=size_fallback_paths,
    )


def _device_mkdir_script(remote_dir: str) -> str:
    return _sync_scripts.device_mkdir_script(remote_dir)


def _device_delete_file_script(remote_file: str) -> str:
    return _sync_scripts.device_delete_file_script(remote_file)


def _device_clear_all_script() -> str:
    return _sync_scripts.device_clear_all_script()


def _device_list_file_sizes_script(remote_root: str) -> str:
    return _sync_scripts.device_list_file_sizes_script(remote_root)


def _device_list_file_sizes_stream_script(remote_root: str) -> str:
    return _sync_scripts.device_list_file_sizes_stream_script(remote_root)


def _device_list_file_signatures_script(remote_paths: list[str]) -> str:
    return _sync_scripts.device_list_file_signatures_script(remote_paths)


def _device_list_file_signatures_stream_script(remote_paths: list[str]) -> str:
    return _sync_scripts.device_list_file_signatures_stream_script(remote_paths)


def _device_selected_file_sizes_script(remote_paths: list[str]) -> str:
    return _sync_scripts.device_selected_file_sizes_script(remote_paths)


def _device_selected_file_sizes_stream_script(remote_paths: list[str]) -> str:
    return _sync_scripts.device_selected_file_sizes_stream_script(remote_paths)


def _device_scan_tree_stream_script(remote_root: str) -> str:
    return _sync_scripts.device_scan_tree_stream_script(remote_root)


def _device_read_file_hex_stream_script(remote_file: str, chunk_bytes: int) -> str:
    return _sync_scripts.device_read_file_hex_stream_script(remote_file, chunk_bytes=chunk_bytes)


def _device_read_text_file_stream_script(remote_file: str, chunk_chars: int) -> str:
    return _sync_scripts.device_read_text_file_stream_script(remote_file, chunk_chars=chunk_chars)


def _device_put_file_script(remote_file: str, data: bytes) -> str:
    return _sync_scripts.device_put_file_script(remote_file, data, chunk_bytes=SYNC_FILE_SCRIPT_CHUNK_BYTES)


def _estimate_sync_source_timeout(source: str, minimum_seconds: float = SYNC_DEVICE_COMMAND_TIMEOUT_SEC) -> float:
    return _sync_scripts.estimate_sync_source_timeout(source, minimum_seconds=minimum_seconds)


def _exec_sync_script(
    controller: CalSciController,
    source: str,
    timeout_seconds: float = SYNC_DEVICE_COMMAND_TIMEOUT_SEC,
    keep_raw_repl: bool = False,
) -> str:
    if keep_raw_repl:
        stdout_bytes, stderr_bytes = controller.exec_source_in_raw_repl(source, timeout_seconds)
    else:
        stdout_bytes, stderr_bytes = controller.exec_source(source, timeout_seconds)
    stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
    if stderr_text:
        raise ControllerError(stderr_text)
    return stdout_bytes.decode("utf-8", errors="replace")


def _parse_device_sizes_output(output: str) -> dict[str, int]:
    marker = "SIZES:"
    start = output.find(marker)
    if start < 0:
        snippet = output.strip()
        if not snippet:
            raise ControllerError("Device size scan returned no output.")
        raise ControllerError(f"Device size scan marker missing in output: {snippet[:200]}")

    start += len(marker)
    depth = 0
    end = start
    for index in range(start, len(output)):
        char = output[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break

    payload = output[start:end].strip()
    if not payload:
        return {}

    parsed = ast.literal_eval(payload)
    if not isinstance(parsed, dict):
        raise ControllerError("Device size scan returned an invalid payload.")

    result: dict[str, int] = {}
    for remote_path, file_size in parsed.items():
        result[str(remote_path)] = int(file_size)
    return result


def _parse_device_sizes_stream_output(output: str) -> dict[str, int]:
    result: dict[str, int] = {}
    done_seen = False

    for raw_line in output.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line == "SIZE_SCAN_DONE":
            done_seen = True
            continue
        if not line.startswith("SIZE:"):
            continue

        payload = line[len("SIZE:") :]
        split_index = payload.rfind(":")
        if split_index <= 0:
            continue
        remote_path = payload[:split_index]
        size_text = payload[split_index + 1 :]
        try:
            result[remote_path] = int(size_text)
        except Exception:
            continue

    if result or done_seen:
        return result

    snippet = output.strip()
    if not snippet:
        raise ControllerError("Device size scan stream returned no output.")
    raise ControllerError(f"Device size scan stream produced no SIZE rows: {snippet[:200]}")


def _parse_device_signatures_output(output: str) -> dict[str, str | None]:
    marker = "SIGS:"
    start = output.find(marker)
    if start < 0:
        snippet = output.strip()
        if not snippet:
            raise ControllerError("Device signature scan returned no output.")
        raise ControllerError(f"Device signature scan marker missing in output: {snippet[:200]}")

    start += len(marker)
    depth = 0
    end = start
    for index in range(start, len(output)):
        char = output[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break

    payload = output[start:end].strip()
    if not payload:
        return {}

    parsed = ast.literal_eval(payload)
    if not isinstance(parsed, dict):
        raise ControllerError("Device signature scan returned an invalid payload.")

    result: dict[str, str | None] = {}
    for remote_path, signature in parsed.items():
        result[str(remote_path)] = None if signature is None else str(signature)
    return result


def _parse_device_signatures_stream_output(output: str) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    done_seen = False

    for raw_line in output.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line == "SIG_SCAN_DONE":
            done_seen = True
            continue
        if not line.startswith("SIG:"):
            continue

        payload = line[len("SIG:") :]
        length_sep = payload.find(":")
        if length_sep <= 0:
            continue
        try:
            path_length = int(payload[:length_sep])
        except Exception:
            continue
        if path_length < 0:
            continue

        remainder = payload[length_sep + 1 :]
        minimum_remainder_length = path_length + 3
        if len(remainder) < minimum_remainder_length:
            continue

        remote_path = remainder[:path_length]
        if len(remainder) <= path_length or remainder[path_length] != ":":
            continue

        flag_and_value = remainder[path_length + 1 :]
        flag_sep = flag_and_value.find(":")
        if flag_sep < 0:
            continue
        flag = flag_and_value[:flag_sep]
        signature = flag_and_value[flag_sep + 1 :]

        if flag == "0":
            result[remote_path] = None
        elif flag == "1":
            result[remote_path] = signature

    if result or done_seen:
        return result

    snippet = output.strip()
    if not snippet:
        raise ControllerError("Device signature scan stream returned no output.")
    raise ControllerError(f"Device signature scan stream produced no SIG rows: {snippet[:200]}")


def _parse_device_selected_sizes_output(output: str) -> dict[str, int | None]:
    marker = "PATH_SIZES:"
    start = output.find(marker)
    if start < 0:
        snippet = output.strip()
        if not snippet:
            raise ControllerError("Device targeted size scan returned no output.")
        raise ControllerError(f"Device targeted size scan marker missing in output: {snippet[:200]}")

    start += len(marker)
    depth = 0
    end = start
    for index in range(start, len(output)):
        char = output[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break

    payload = output[start:end].strip()
    if not payload:
        return {}

    parsed = ast.literal_eval(payload)
    if not isinstance(parsed, dict):
        raise ControllerError("Device targeted size scan returned an invalid payload.")

    result: dict[str, int | None] = {}
    for remote_path, file_size in parsed.items():
        if file_size is None:
            result[str(remote_path)] = None
        else:
            result[str(remote_path)] = int(file_size)
    return result


def _parse_device_selected_sizes_stream_output(output: str) -> dict[str, int | None]:
    result: dict[str, int | None] = {}
    done_seen = False

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "PATH_SIZE_SCAN_DONE":
            done_seen = True
            continue
        if not line.startswith("PATHSIZE:"):
            continue

        payload = line[len("PATHSIZE:") :]
        length_sep = payload.find(":")
        if length_sep <= 0:
            continue
        try:
            path_length = int(payload[:length_sep])
        except Exception:
            continue
        if path_length < 0:
            continue

        remainder = payload[length_sep + 1 :]
        minimum_remainder_length = path_length + 3
        if len(remainder) < minimum_remainder_length:
            continue

        remote_path = remainder[:path_length]
        if len(remainder) <= path_length or remainder[path_length] != ":":
            continue

        flag_and_size = remainder[path_length + 1 :]
        flag_sep = flag_and_size.find(":")
        if flag_sep < 0:
            continue
        flag = flag_and_size[:flag_sep]
        size_text = flag_and_size[flag_sep + 1 :]

        if flag == "0":
            result[remote_path] = None
            continue
        if flag == "1":
            try:
                result[remote_path] = int(size_text)
            except Exception:
                continue

    if result or done_seen:
        return result

    snippet = output.strip()
    if not snippet:
        raise ControllerError("Device targeted size scan stream returned no output.")
    raise ControllerError(f"Device targeted size scan stream produced no PATHSIZE rows: {snippet[:200]}")


def _parse_device_tree_stream_output(output: str) -> tuple[list[str], dict[str, int]]:
    dirs: set[str] = set()
    files: dict[str, int] = {}
    errors: list[str] = []
    done_seen = False

    for raw_line in output.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line == "TREE_SCAN_DONE":
            done_seen = True
            continue
        if line.startswith("SCANERR:"):
            errors.append(line[len("SCANERR:") :])
            continue
        if line.startswith("DIR:"):
            payload = line[len("DIR:") :]
            length_sep = payload.find(":")
            if length_sep <= 0:
                continue
            try:
                path_length = int(payload[:length_sep])
            except Exception:
                continue
            remainder = payload[length_sep + 1 :]
            if path_length < 0 or len(remainder) < path_length:
                continue
            dirs.add(remainder[:path_length])
            continue
        if line.startswith("FILE:"):
            payload = line[len("FILE:") :]
            length_sep = payload.find(":")
            if length_sep <= 0:
                continue
            try:
                path_length = int(payload[:length_sep])
            except Exception:
                continue
            remainder = payload[length_sep + 1 :]
            if path_length < 0 or len(remainder) < path_length + 2:
                continue
            remote_path = remainder[:path_length]
            if len(remainder) <= path_length or remainder[path_length] != ":":
                continue
            size_text = remainder[path_length + 1 :]
            try:
                files[remote_path] = int(size_text)
            except Exception:
                files[remote_path] = 0

    if files or dirs or done_seen:
        return sorted(dirs), {remote_path: files[remote_path] for remote_path in sorted(files)}

    if errors:
        raise ControllerError(errors[0])

    snippet = output.strip()
    if not snippet:
        raise ControllerError("Device tree scan returned no output.")
    raise ControllerError(f"Device tree scan produced no DIR/FILE rows: {snippet[:200]}")


def _parse_device_file_hex_output(output: str, *, remote_path: str) -> bytes:
    chunks: list[bytes] = []
    done_seen = False
    error_text: str | None = None

    for raw_line in output.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line == "FILE_READ_DONE":
            done_seen = True
            continue
        if line.startswith("FILE_READ_ERR:"):
            error_text = line[len("FILE_READ_ERR:") :].strip() or f"Failed to read {remote_path}"
            continue
        if not line.startswith("HEX:"):
            continue
        payload = line[len("HEX:") :]
        try:
            chunks.append(bytes.fromhex(payload))
        except Exception:
            continue

    if error_text is not None:
        raise ControllerError(error_text)
    if chunks or done_seen:
        return b"".join(chunks)

    snippet = output.strip()
    if not snippet:
        raise ControllerError(f"Device file read returned no output for {remote_path}.")
    raise ControllerError(f"Device file read produced no HEX rows for {remote_path}: {snippet[:200]}")


def _parse_device_text_file_output(output: str, *, remote_path: str) -> str:
    start_marker = "[[CALSCI_FILE_CONTENT_START]]"
    end_marker = "[[CALSCI_FILE_CONTENT_END]]"
    error_text: str | None = None

    for raw_line in output.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if line.startswith("FILE_READ_ERR:"):
            error_text = line[len("FILE_READ_ERR:") :].strip() or f"Failed to read {remote_path}"
            break

    if error_text is not None:
        raise ControllerError(error_text)

    start_index = output.find(start_marker)
    end_index = output.find(end_marker)
    if start_index < 0 or end_index < 0 or end_index < start_index:
        snippet = output.strip()
        if not snippet:
            raise ControllerError(f"Device text file read returned no output for {remote_path}.")
        raise ControllerError(f"Device text file read markers missing for {remote_path}: {snippet[:200]}")

    content_start = output.find("\n", start_index)
    if content_start < 0:
        return ""
    content = output[content_start + 1 : end_index]
    return content.rstrip("\r\n")


def _parse_clear_all_output(output: str) -> dict[str, Any]:
    files_deleted: list[str] = []
    directories_deleted: list[str] = []
    warning_lines: list[str] = []
    other_lines: list[str] = []
    start_seen = False
    done_seen = False

    for raw_line in output.replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line == "CLEANUP_START":
            start_seen = True
            continue
        if line == "CLEANUP_DONE":
            done_seen = True
            continue
        if line.startswith("FILE_DEL:"):
            files_deleted.append(line[len("FILE_DEL:") :].strip())
            continue
        if line.startswith("DIR_DEL:"):
            directories_deleted.append(line[len("DIR_DEL:") :].strip())
            continue
        if line.startswith("FILE_ERR:") or line.startswith("DIR_ERR:") or line.startswith("ERR:"):
            warning_lines.append(line)
            continue
        other_lines.append(line)

    return {
        "startSeen": start_seen,
        "doneSeen": done_seen,
        "filesDeleted": files_deleted,
        "directoriesDeleted": directories_deleted,
        "warningLines": warning_lines,
        "otherLines": other_lines,
    }


def _chunk_remote_paths_for_targeted_scan(
    remote_paths: list[str],
    max_batch_size: int = SYNC_TARGETED_SCAN_BATCH_SIZE,
    max_script_chars: int = SYNC_TARGETED_SCAN_MAX_SCRIPT_CHARS,
) -> list[list[str]]:
    if not remote_paths:
        return []

    batches: list[list[str]] = []
    current_batch: list[str] = []
    for remote_path in remote_paths:
        candidate = current_batch + [remote_path]
        estimated_script_chars = len(_device_selected_file_sizes_script(candidate))
        if current_batch and (
            len(candidate) > max_batch_size or estimated_script_chars > max_script_chars
        ):
            batches.append(current_batch)
            current_batch = [remote_path]
            continue
        current_batch = candidate

    if current_batch:
        batches.append(current_batch)
    return batches


def _is_raw_stdout_timeout(exc: Exception) -> bool:
    return "waiting for raw REPL stdout terminator" in str(exc)


def _read_remote_file_sizes(controller: CalSciController, remote_root: str) -> dict[str, int]:
    return controller.sync_get_file_sizes(remote_root, timeout=SYNC_SCAN_COMMAND_TIMEOUT_SEC)


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
            if command == "scan":
                _service_emit({"id": request_id, "type": "result", "payload": {"ok": True, "devices": list_detected_esp_ports()}})
                continue
            if command == "run.cancel":
                _service_emit({"id": request_id, "type": "result", "payload": dispatcher.cancel_active_run()})
                continue
            if command == "session.abort":
                _service_emit({
                    "id": request_id,
                    "type": "result",
                    "payload": dispatcher.abort_session_activity(reason=str(args.get("reason") or "aborted")),
                })
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
        return emit({"ok": True, "devices": list_detected_esp_ports()}, getattr(args, "json", False))
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
