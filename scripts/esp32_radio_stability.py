#!/usr/bin/env python3
"""Host-side ESP32 Wi-Fi + BLE stability soak test for MicroPython targets.

This script pushes a temporary stress test into an ESP32 over raw REPL,
streams structured JSON events back to the host, and prints a simple
PASS/WARN/FAIL verdict from configurable thresholds.

Notes:
- This expects MicroPython on the ESP32.
- "Bluetooth" here means BLE scan stress, which is what MicroPython exposes
  on most ESP32 builds.
- A pass is evidence, not certification. For serious screening, run this as a
  long soak test with the final firmware, power source, enclosure, and RF
  environment you intend to ship.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import serial
    from serial.tools import list_ports
except ModuleNotFoundError:
    serial = None
    list_ports = None


RAW_REPL_BANNER = b"raw REPL; CTRL-B to exit\r\n"
RAW_REPL_PROMPT = b">"
RAW_TERMINATOR = 0x04
EVENT_PREFIX = "RADIO_TEST "


DEVICE_SCRIPT_TEMPLATE = r"""
import gc
import machine
import network
import socket
import sys
import time

try:
    import json
except ImportError:
    import ujson as json

try:
    import ubinascii
except ImportError:
    ubinascii = None

try:
    import bluetooth
except ImportError:
    bluetooth = None


CONFIG = __CONFIG__
EVENT_PREFIX = "RADIO_TEST "
IRQ_SCAN_RESULT = 5
IRQ_SCAN_DONE = 6


def emit(kind, **fields):
    event = {"kind": kind, "ticks_ms": time.ticks_ms()}
    for key in fields:
        event[key] = fields[key]
    try:
        print(EVENT_PREFIX + json.dumps(event))
    except Exception as exc:
        print(EVENT_PREFIX + '{"kind":"emit_error","detail":"' + repr(exc).replace('"', "'") + '"}')


def unique_id_hex():
    if ubinascii is None:
        return None
    try:
        return ubinascii.hexlify(machine.unique_id()).decode()
    except Exception:
        return None


def reset_cause_name():
    mapping = {}
    for name in (
        "PWRON_RESET",
        "HARD_RESET",
        "WDT_RESET",
        "DEEPSLEEP_RESET",
        "SOFT_RESET",
        "BROWN_OUT_RESET",
    ):
        value = getattr(machine, name, None)
        if value is not None:
            mapping[value] = name
    try:
        cause = machine.reset_cause()
    except Exception:
        return None
    return mapping.get(cause, str(cause))


def get_rssi(wlan):
    try:
        return wlan.status("rssi")
    except Exception:
        return None


def get_temp_raw_f():
    try:
        import esp32
    except ImportError:
        return None
    try:
        return esp32.raw_temperature()
    except Exception:
        return None


def sleep_ms(delay_ms):
    if delay_ms > 0:
        time.sleep_ms(delay_ms)


def connect_wifi(wlan, ssid, password, timeout_ms):
    if wlan.isconnected():
        return True, 0, None
    try:
        wlan.active(True)
    except Exception:
        pass
    try:
        wlan.disconnect()
        sleep_ms(200)
    except Exception:
        pass

    start = time.ticks_ms()
    status = None
    try:
        wlan.connect(ssid, password)
    except Exception as exc:
        return False, 0, repr(exc)

    deadline = time.ticks_add(start, timeout_ms)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if wlan.isconnected():
            return True, time.ticks_diff(time.ticks_ms(), start), None
        try:
            status = wlan.status()
        except Exception:
            status = None
        sleep_ms(250)

    return False, time.ticks_diff(time.ticks_ms(), start), status


def tcp_probe(host, port, timeout_ms, send_http_head):
    if not host:
        return None, None, None

    start = time.ticks_ms()
    sock = None
    try:
        addr = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0][-1]
        sock = socket.socket()
        try:
            sock.settimeout(timeout_ms / 1000)
        except Exception:
            pass
        sock.connect(addr)
        if send_http_head:
            request = "HEAD / HTTP/1.0\r\nHost: %s\r\n\r\n" % host
            try:
                sock.send(request.encode())
            except Exception:
                pass
            try:
                sock.recv(1)
            except Exception:
                pass
        return True, time.ticks_diff(time.ticks_ms(), start), None
    except Exception as exc:
        return False, time.ticks_diff(time.ticks_ms(), start), repr(exc)
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class BleScanner:
    def __init__(self):
        self.ble = None
        self.done = False
        self.results = 0
        self.last_error = None
        if bluetooth is None:
            return
        try:
            self.ble = bluetooth.BLE()
            self.ble.active(True)
            self.ble.irq(self._irq)
        except Exception as exc:
            self.last_error = repr(exc)
            self.ble = None

    def _irq(self, event, data):
        if event == IRQ_SCAN_RESULT:
            self.results += 1
        elif event == IRQ_SCAN_DONE:
            self.done = True

    def scan_once(self, duration_ms, interval_us, window_us, active_scan):
        if self.ble is None:
            return False, None, 0, self.last_error or "bluetooth unavailable"

        self.done = False
        self.results = 0
        start = time.ticks_ms()
        try:
            self.ble.gap_scan(duration_ms, interval_us, window_us, active_scan)
        except Exception as exc:
            return False, 0, 0, repr(exc)

        deadline = time.ticks_add(start, duration_ms + 3000)
        while not self.done and time.ticks_diff(deadline, time.ticks_ms()) > 0:
            sleep_ms(50)

        elapsed = time.ticks_diff(time.ticks_ms(), start)
        if not self.done:
            try:
                self.ble.gap_scan(None)
            except Exception:
                pass
            return False, elapsed, self.results, "ble scan timeout"

        return True, elapsed, self.results, None


def main():
    wlan = network.WLAN(network.STA_IF)
    try:
        wlan.active(True)
    except Exception:
        pass

    ble_scanner = BleScanner() if CONFIG.get("enable_ble") else None
    state = {
        "cycles": 0,
        "wifi_reconnects": 0,
        "wifi_connect_failures": 0,
        "probe_ok": 0,
        "probe_fail": 0,
        "ble_ok": 0,
        "ble_fail": 0,
        "device_errors": 0,
        "min_heap": None,
    }

    emit(
        "start",
        board=unique_id_hex(),
        reset_cause=reset_cause_name(),
        ssid=CONFIG["ssid"],
        duration_ms=CONFIG["duration_ms"],
        cycle_delay_ms=CONFIG["cycle_delay_ms"],
        probe_host=CONFIG.get("probe_host"),
        probe_port=CONFIG.get("probe_port"),
        ble_enabled=bool(ble_scanner is not None and ble_scanner.ble is not None),
    )

    deadline = time.ticks_add(time.ticks_ms(), CONFIG["duration_ms"])
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        gc.collect()
        state["cycles"] += 1
        errors = []
        reconnect_attempted = False
        wifi_connect_ms = 0
        wifi_status = None

        wifi_ok = wlan.isconnected()
        if not wifi_ok:
            reconnect_attempted = True
            wifi_ok, wifi_connect_ms, wifi_status = connect_wifi(
                wlan,
                CONFIG["ssid"],
                CONFIG["password"],
                CONFIG["wifi_connect_timeout_ms"],
            )
            if wifi_ok:
                state["wifi_reconnects"] += 1
            else:
                state["wifi_connect_failures"] += 1
                errors.append("wifi_connect_failed:%s" % wifi_status)

        probe_ok = None
        probe_ms = None
        probe_error = None
        if wifi_ok and CONFIG.get("probe_host"):
            probe_ok, probe_ms, probe_error = tcp_probe(
                CONFIG["probe_host"],
                CONFIG["probe_port"],
                CONFIG["probe_timeout_ms"],
                CONFIG["probe_send_http_head"],
            )
            if probe_ok:
                state["probe_ok"] += 1
            else:
                state["probe_fail"] += 1
                errors.append("probe:%s" % probe_error)

        ble_ok = None
        ble_ms = None
        ble_results = None
        ble_error = None
        if ble_scanner is not None:
            ble_ok, ble_ms, ble_results, ble_error = ble_scanner.scan_once(
                CONFIG["ble_scan_ms"],
                CONFIG["ble_interval_us"],
                CONFIG["ble_window_us"],
                CONFIG["ble_active_scan"],
            )
            if ble_ok:
                state["ble_ok"] += 1
            else:
                state["ble_fail"] += 1
                errors.append("ble:%s" % ble_error)

        free_heap = gc.mem_free()
        if state["min_heap"] is None or free_heap < state["min_heap"]:
            state["min_heap"] = free_heap

        if errors:
            state["device_errors"] += len(errors)

        ip = None
        gateway = None
        if wifi_ok:
            try:
                ip, _, gateway, _ = wlan.ifconfig()
            except Exception:
                pass

        emit(
            "cycle",
            cycle=state["cycles"],
            wifi_ok=wifi_ok,
            reconnect_attempted=reconnect_attempted,
            wifi_connect_ms=wifi_connect_ms,
            wifi_status=wifi_status,
            ip=ip,
            gateway=gateway,
            rssi=get_rssi(wlan) if wifi_ok else None,
            probe_ok=probe_ok,
            probe_ms=probe_ms,
            probe_error=probe_error,
            ble_ok=ble_ok,
            ble_ms=ble_ms,
            ble_results=ble_results,
            ble_error=ble_error,
            free_heap=free_heap,
            min_heap=state["min_heap"],
            raw_temp_f=get_temp_raw_f(),
            errors=errors,
        )

        sleep_ms(CONFIG["cycle_delay_ms"])

    emit("summary", final_wifi_ok=wlan.isconnected(), **state)


try:
    main()
except Exception as exc:
    try:
        emit("fatal", error=repr(exc))
    except Exception:
        pass
    sys.print_exception(exc)
    raise
"""


@dataclass
class HostObservations:
    start_event: dict[str, Any] | None = None
    summary_event: dict[str, Any] | None = None
    raw_lines: int = 0
    event_count: int = 0
    reboot_signatures: int = 0
    parse_errors: int = 0
    notes: list[str] = field(default_factory=list)


class JsonlWriter:
    def __init__(self, path: Path | None) -> None:
        self._handle = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a", encoding="utf-8")

    def write(self, record: dict[str, Any]) -> None:
        if self._handle is None:
            return
        self._handle.write(json.dumps(record, sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()


class RawReplSession:
    def __init__(self, port: str, baud: int, chunk_bytes: int) -> None:
        if serial is None:
            raise RuntimeError("pyserial is required on the host. Install it with: pip install pyserial")
        self._chunk_bytes = max(32, chunk_bytes)
        self._serial = serial.Serial(
            port=port,
            baudrate=baud,
            timeout=0.25,
            write_timeout=2.0,
        )

    def close(self) -> None:
        self._serial.close()

    def _write(self, data: bytes, *, flush: bool = True) -> None:
        self._serial.write(data)
        if flush:
            self._serial.flush()

    def _drain_input(self) -> bytes:
        chunks: list[bytes] = []
        time.sleep(0.05)
        while True:
            waiting = self._serial.in_waiting
            if waiting <= 0:
                break
            chunks.append(self._serial.read(waiting))
            time.sleep(0.02)
        return b"".join(chunks)

    def _read_until(self, marker: bytes, timeout_sec: float) -> bytes:
        deadline = time.monotonic() + timeout_sec
        buffer = bytearray()
        while time.monotonic() < deadline:
            chunk = self._serial.read(max(1, self._serial.in_waiting or 1))
            if chunk:
                buffer.extend(chunk)
                if marker in buffer:
                    return bytes(buffer)
                continue
            time.sleep(0.02)
        return bytes(buffer)

    def _read_exact(self, size: int, timeout_sec: float) -> bytes:
        deadline = time.monotonic() + timeout_sec
        buffer = bytearray()
        while len(buffer) < size and time.monotonic() < deadline:
            chunk = self._serial.read(size - len(buffer))
            if chunk:
                buffer.extend(chunk)
                continue
            time.sleep(0.02)
        return bytes(buffer)

    def enter_raw_repl(self) -> None:
        self._write(b"\r\x03\x03", flush=True)
        time.sleep(0.1)
        self._drain_input()

        self._write(b"\r\x01", flush=True)
        banner = self._read_until(RAW_REPL_BANNER, 6.0)
        if RAW_REPL_BANNER not in banner:
            raise RuntimeError(f"could not enter raw REPL: {banner!r}")

        if RAW_REPL_PROMPT not in banner.split(RAW_REPL_BANNER, 1)[1]:
            prompt = self._read_until(RAW_REPL_PROMPT, 1.0)
            if not prompt.endswith(RAW_REPL_PROMPT):
                raise RuntimeError(f"raw REPL prompt missing after banner: {prompt!r}")

    def exit_raw_repl(self) -> None:
        self._write(b"\r\x02", flush=True)
        time.sleep(0.05)
        self._drain_input()

    def exec_raw_start(self, source: str) -> None:
        source_bytes = source.encode("utf-8")
        for offset in range(0, len(source_bytes), self._chunk_bytes):
            self._write(source_bytes[offset : offset + self._chunk_bytes], flush=False)
            time.sleep(0.01)

        self._write(b"\x04", flush=True)
        response = self._read_exact(2, 1.0)
        if response != b"OK":
            raise RuntimeError(f"could not exec raw REPL source (response={response!r})")

    def follow_output(
        self,
        stale_timeout_sec: float,
        line_callback,
    ) -> tuple[bytes, bytes]:
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        line_buffer = bytearray()
        phase = "stdout"
        last_activity = time.monotonic()

        def flush_line() -> None:
            if not line_buffer:
                return
            text = line_buffer.decode("utf-8", errors="replace").strip()
            line_buffer.clear()
            if text:
                line_callback(text)

        while True:
            chunk = self._serial.read(max(1, self._serial.in_waiting or 1))
            if not chunk:
                if time.monotonic() - last_activity > stale_timeout_sec:
                    raise TimeoutError(
                        f"device produced no serial output for {stale_timeout_sec:.1f}s; "
                        "possible hang, reboot, or Wi-Fi connect stall"
                    )
                time.sleep(0.02)
                continue

            last_activity = time.monotonic()
            for byte in chunk:
                if phase == "stdout":
                    if byte == RAW_TERMINATOR:
                        flush_line()
                        phase = "stderr"
                        continue
                    stdout_buffer.append(byte)
                    if byte in (10, 13):
                        flush_line()
                    else:
                        line_buffer.append(byte)
                    continue

                if phase == "stderr":
                    if byte == RAW_TERMINATOR:
                        phase = "prompt"
                        continue
                    stderr_buffer.append(byte)
                    continue

                if phase == "prompt" and byte == ord(">"):
                    return bytes(stdout_buffer), bytes(stderr_buffer)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Soak-test ESP32 Wi-Fi and BLE coexistence over MicroPython raw REPL.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", help="serial device path, for example /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200, help="serial baud rate")
    parser.add_argument("--ssid", required=True, help="Wi-Fi SSID to join")
    parser.add_argument("--password", default="", help="Wi-Fi password")
    parser.add_argument("--duration-min", type=float, default=60.0, help="total test duration in minutes")
    parser.add_argument("--cycle-sec", type=float, default=5.0, help="delay between test cycles")
    parser.add_argument("--wifi-connect-timeout-sec", type=float, default=15.0, help="timeout for each Wi-Fi reconnect attempt")
    parser.add_argument("--probe-host", help="optional TCP host to verify application-level Wi-Fi stability")
    parser.add_argument("--probe-port", type=int, default=80, help="TCP port for the optional Wi-Fi probe")
    parser.add_argument("--probe-timeout-sec", type=float, default=3.0, help="timeout for each TCP probe")
    parser.add_argument("--probe-http-head", action="store_true", help="send a simple HTTP HEAD after connecting to the probe host")
    parser.add_argument("--disable-ble", action="store_true", help="skip BLE scan stress if you only want Wi-Fi testing")
    parser.add_argument("--ble-scan-ms", type=int, default=2500, help="duration of each BLE scan window in milliseconds")
    parser.add_argument("--ble-interval-us", type=int, default=30000, help="BLE scan interval in microseconds")
    parser.add_argument("--ble-window-us", type=int, default=30000, help="BLE scan window in microseconds")
    parser.add_argument("--ble-active-scan", action="store_true", help="use active BLE scans instead of passive scans")
    parser.add_argument("--stale-timeout-sec", type=float, default=45.0, help="host-side timeout for missing serial activity")
    parser.add_argument("--chunk-bytes", type=int, default=256, help="raw REPL upload chunk size")
    parser.add_argument("--log-jsonl", type=Path, help="optional JSONL file for all parsed device events")
    parser.add_argument("--min-probe-success-rate", type=float, default=0.995, help="minimum acceptable TCP probe success rate")
    parser.add_argument("--min-ble-success-rate", type=float, default=0.98, help="minimum acceptable BLE scan success rate")
    parser.add_argument("--min-free-heap", type=int, default=20000, help="minimum acceptable free heap observed on the device")
    parser.add_argument("--max-wifi-reconnects-per-hour", type=float, default=2.0, help="maximum acceptable reconnect rate")
    parser.add_argument("--max-device-errors", type=int, default=0, help="maximum acceptable device-side error count")
    parser.add_argument("--max-host-reboots", type=int, default=0, help="maximum reboot/banner signatures tolerated on the host serial stream")
    return parser.parse_args(argv)


def resolve_serial_port(explicit_port: str | None) -> str:
    if serial is None or list_ports is None:
        if explicit_port:
            return explicit_port
        raise SystemExit("pyserial is required on the host. Install it with: pip install pyserial")

    if explicit_port:
        return explicit_port

    ports = list(list_ports.comports())
    if len(ports) == 1:
        return ports[0].device

    details = []
    for port in ports:
        label = port.description or port.manufacturer or "unknown"
        details.append(f"{port.device} ({label})")

    if not details:
        raise SystemExit("No serial ports found. Connect the ESP32 or pass --port explicitly.")

    joined = ", ".join(details)
    raise SystemExit(f"Multiple serial ports found. Pass --port explicitly. Available: {joined}")


def build_device_script(args: argparse.Namespace) -> str:
    config = {
        "ssid": args.ssid,
        "password": args.password,
        "duration_ms": int(args.duration_min * 60_000),
        "cycle_delay_ms": int(args.cycle_sec * 1000),
        "wifi_connect_timeout_ms": int(args.wifi_connect_timeout_sec * 1000),
        "probe_host": args.probe_host,
        "probe_port": args.probe_port,
        "probe_timeout_ms": int(args.probe_timeout_sec * 1000),
        "probe_send_http_head": bool(args.probe_http_head),
        "enable_ble": not args.disable_ble,
        "ble_scan_ms": args.ble_scan_ms,
        "ble_interval_us": args.ble_interval_us,
        "ble_window_us": args.ble_window_us,
        "ble_active_scan": bool(args.ble_active_scan),
    }
    return DEVICE_SCRIPT_TEMPLATE.replace("__CONFIG__", json.dumps(config, separators=(",", ":")))


def parse_event_line(line: str) -> dict[str, Any] | None:
    if not line.startswith(EVENT_PREFIX):
        return None
    payload = line[len(EVENT_PREFIX) :].strip()
    return json.loads(payload)


def looks_like_reboot(line: str) -> bool:
    lower = line.lower()
    return any(
        token in lower
        for token in (
            "rst:",
            "ets ",
            "esp-rom",
            "brownout detector",
            "guru meditation",
            "panic'ed",
            "rebooting...",
        )
    )


def format_runtime(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def make_line_handler(observations: HostObservations, writer: JsonlWriter, start_time: float):
    def handle(line: str) -> None:
        observations.raw_lines += 1
        elapsed = format_runtime(time.monotonic() - start_time)

        try:
            event = parse_event_line(line)
        except json.JSONDecodeError as exc:
            observations.parse_errors += 1
            print(f"[{elapsed}] parse-error {exc}: {line}", flush=True)
            writer.write({"kind": "host_parse_error", "line": line, "ts": time.time()})
            return

        if event is None:
            if looks_like_reboot(line):
                observations.reboot_signatures += 1
            print(f"[{elapsed}] device {line}", flush=True)
            writer.write({"kind": "raw_line", "line": line, "ts": time.time()})
            return

        observations.event_count += 1
        writer.write({"kind": "device_event", "event": event, "ts": time.time()})
        kind = event.get("kind")
        if kind == "start":
            observations.start_event = event
            print(
                f"[{elapsed}] start board={event.get('board')} reset={event.get('reset_cause')} "
                f"probe={event.get('probe_host')} ble={event.get('ble_enabled')}",
                flush=True,
            )
            return

        if kind == "cycle":
            print(
                f"[{elapsed}] cycle={event.get('cycle')} "
                f"wifi={'ok' if event.get('wifi_ok') else 'down'} "
                f"rssi={event.get('rssi')} "
                f"probe={event.get('probe_ok')}/{event.get('probe_ms')}ms "
                f"ble={event.get('ble_ok')}/{event.get('ble_ms')}ms "
                f"adv={event.get('ble_results')} "
                f"heap={event.get('free_heap')} "
                f"errors={len(event.get('errors') or [])}",
                flush=True,
            )
            return

        if kind == "summary":
            observations.summary_event = event
            print(
                f"[{elapsed}] summary cycles={event.get('cycles')} "
                f"wifi_reconnects={event.get('wifi_reconnects')} "
                f"wifi_failures={event.get('wifi_connect_failures')} "
                f"probe_ok={event.get('probe_ok')} probe_fail={event.get('probe_fail')} "
                f"ble_ok={event.get('ble_ok')} ble_fail={event.get('ble_fail')} "
                f"min_heap={event.get('min_heap')}",
                flush=True,
            )
            return

        if kind == "fatal":
            print(f"[{elapsed}] fatal {event.get('error')}", flush=True)
            return

        print(f"[{elapsed}] event {event}", flush=True)

    return handle


def evaluate_verdict(args: argparse.Namespace, observations: HostObservations, stderr_text: str) -> tuple[str, list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    summary = observations.summary_event

    if stderr_text.strip():
        failures.append(f"device stderr was not empty: {stderr_text.strip().splitlines()[-1]}")

    if observations.parse_errors:
        failures.append(f"host failed to parse {observations.parse_errors} structured event line(s)")

    if observations.reboot_signatures > args.max_host_reboots:
        failures.append(
            f"serial stream showed {observations.reboot_signatures} reboot/banner signature(s), "
            f"limit is {args.max_host_reboots}"
        )

    if summary is None:
        failures.append("device never emitted a final summary")
        return "FAIL", failures, warnings

    cycles = int(summary.get("cycles") or 0)
    if cycles <= 0:
        failures.append("device completed zero cycles")

    wifi_connect_failures = int(summary.get("wifi_connect_failures") or 0)
    if wifi_connect_failures > 0:
        failures.append(f"{wifi_connect_failures} Wi-Fi reconnect attempt(s) failed")

    device_errors = int(summary.get("device_errors") or 0)
    if device_errors > args.max_device_errors:
        failures.append(f"device reported {device_errors} error(s), limit is {args.max_device_errors}")

    min_heap = summary.get("min_heap")
    if isinstance(min_heap, int) and min_heap < args.min_free_heap:
        failures.append(f"minimum free heap {min_heap} is below threshold {args.min_free_heap}")

    duration_hours = max(args.duration_min / 60.0, 1e-6)
    reconnects = int(summary.get("wifi_reconnects") or 0)
    reconnect_rate = reconnects / duration_hours
    if reconnect_rate > args.max_wifi_reconnects_per_hour:
        failures.append(
            f"Wi-Fi reconnect rate {reconnect_rate:.2f}/hour exceeds limit "
            f"{args.max_wifi_reconnects_per_hour:.2f}/hour"
        )

    if not bool(summary.get("final_wifi_ok")):
        failures.append("device finished with Wi-Fi disconnected")

    probe_ok = int(summary.get("probe_ok") or 0)
    probe_fail = int(summary.get("probe_fail") or 0)
    probe_total = probe_ok + probe_fail
    if probe_total > 0:
        probe_rate = probe_ok / probe_total
        if probe_rate < args.min_probe_success_rate:
            failures.append(
                f"TCP probe success rate {probe_rate:.4f} is below threshold {args.min_probe_success_rate:.4f}"
            )
    else:
        warnings.append("no TCP probe was configured, so Wi-Fi traffic stability was not checked above association level")

    if not args.disable_ble:
        if observations.start_event and not bool(observations.start_event.get("ble_enabled")):
            failures.append("BLE was requested but the device firmware did not provide a working BLE stack")
        ble_ok = int(summary.get("ble_ok") or 0)
        ble_fail = int(summary.get("ble_fail") or 0)
        ble_total = ble_ok + ble_fail
        if ble_total <= 0:
            failures.append("BLE testing was enabled but no BLE scan cycles completed")
        else:
            ble_rate = ble_ok / ble_total
            if ble_rate < args.min_ble_success_rate:
                failures.append(
                    f"BLE scan success rate {ble_rate:.4f} is below threshold {args.min_ble_success_rate:.4f}"
                )

    if args.duration_min < 120:
        warnings.append("for production screening, run this for at least 2 hours and preferably 8-24 hours")

    if failures:
        return "FAIL", failures, warnings
    if warnings:
        return "WARN", failures, warnings
    return "PASS", failures, warnings


def print_verdict(level: str, failures: list[str], warnings: list[str], args: argparse.Namespace) -> None:
    print("", flush=True)
    print(f"Verdict: {level}", flush=True)
    if failures:
        for item in failures:
            print(f"- {item}", flush=True)
    if warnings:
        for item in warnings:
            print(f"- {item}", flush=True)
    print(
        f"- Thresholds used: probe>={args.min_probe_success_rate:.4f}, "
        f"ble>={args.min_ble_success_rate:.4f}, min_heap>={args.min_free_heap}, "
        f"reconnects/hour<={args.max_wifi_reconnects_per_hour:.2f}",
        flush=True,
    )


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    port = resolve_serial_port(args.port)
    writer = JsonlWriter(args.log_jsonl)
    observations = HostObservations()
    session = None
    stderr_text = ""
    start_time = time.monotonic()

    print(f"Using serial port: {port}", flush=True)
    print(
        f"Duration={args.duration_min:g} min cycle={args.cycle_sec:g}s "
        f"probe={args.probe_host or 'disabled'} ble={'off' if args.disable_ble else 'on'}",
        flush=True,
    )

    try:
        session = RawReplSession(port=port, baud=args.baud, chunk_bytes=args.chunk_bytes)
        session.enter_raw_repl()
        session.exec_raw_start(build_device_script(args))
        line_handler = make_line_handler(observations, writer, start_time)
        _, stderr_bytes = session.follow_output(args.stale_timeout_sec, line_handler)
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
    except ((serial.SerialException if serial is not None else Exception), TimeoutError, RuntimeError) as exc:
        print(f"Host error: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        if session is not None:
            try:
                session.exit_raw_repl()
            except Exception:
                pass
            session.close()
        writer.close()

    verdict, failures, warnings = evaluate_verdict(args, observations, stderr_text)
    print_verdict(verdict, failures, warnings, args)
    return {"PASS": 0, "WARN": 1, "FAIL": 2}[verdict]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
