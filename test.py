import gc
import machine
import esp32
import st7565 as display
from sleeping_features import keypad_normal

# ----------------------------
# Hardware bootstrap
# ----------------------------
DISPLAY_PINS = (9, 11, 10, 13, 12)
DEEPSLEEP_KEY_PIN = 14

keypad_normal()
machine.Pin(DEEPSLEEP_KEY_PIN, machine.Pin.OUT, value=1, hold=False)
display.init(*DISPLAY_PINS)

try:
    display.clear_display()
except Exception:
    pass

gc.enable()
print("free ram initially=", gc.mem_free())
print("ram allocated initially=", gc.mem_alloc())

# ----------------------------
# Triple boot helpers
# ----------------------------
print("=================================")
print("  CalSci - Triple Boot System")
print("=================================")
print("  boot.switch_to_cpp()   - Reboot into C++")
print("  boot.switch_to_rust()  - Reboot into Rust")
print("  boot.boot_info()       - Show current partition")
print("=================================")


def boot_info():
    cur = esp32.Partition(esp32.Partition.RUNNING)
    print("Running from:", cur.info())
    print()
    print("All app partitions:")
    for part in esp32.Partition.find(esp32.Partition.TYPE_APP):
        print(" ", part.info())


def switch_to_cpp():
    _switch_to("ota_1", "C++")


def switch_to_rust():
    _switch_to("ota_2", "Rust")


def switch_to_micropython():
    _switch_to("ota_0", "MicroPython")


def _decode_partition_field(value):
    if isinstance(value, bytes):
        try:
            return value.decode()
        except Exception:
            return None
    if isinstance(value, str):
        return value
    return None


def _partition_by_label(label):
    try:
        return esp32.Partition(label)
    except Exception:
        pass

    try:
        parts = esp32.Partition.find(esp32.Partition.TYPE_APP)
    except Exception:
        return None

    for part in parts:
        try:
            info = part.info()
        except Exception:
            continue

        fields = info if isinstance(info, (tuple, list)) else (info,)
        for field in fields:
            if _decode_partition_field(field) == label:
                return part
    return None


def _switch_to(label, name):
    import time as _time

    try:
        part = _partition_by_label(label)
        if part is None:
            print("Error switching to", label, ": partition not found")
            return

        part.set_boot()
        print("Next boot:", name, "(" + label + ")")
        display.clear_display()
        menu.menu_list = ["Switching to:", name, "Rebooting..."]
        menu.update()
        menu_refresh.refresh()
        print("Restarting in 1 second...")
        _time.sleep(1)
        machine.reset()
    except Exception as exc:
        print("Error switching to", label, ":", exc)


# ----------------------------
# Runtime globals
# ----------------------------
from apps.settings.backlight import apply_saved_backlight
import builtins
import calsci_runtime
from data_modules.object_handler import data_bucket, menu, menu_refresh, typer

apply_saved_backlight()
builtins.display = display
builtins.typer = typer
builtins.set_calsci_keypad_blocked = calsci_runtime.set_calsci_keypad_blocked
builtins.block_calsci_keypad = calsci_runtime.block_calsci_keypad
builtins.unblock_calsci_keypad = calsci_runtime.unblock_calsci_keypad
builtins.calsci_keypad_blocked = calsci_runtime.calsci_keypad_blocked

# WiFi startup stays disabled for fast boot.
builtins.sta_if = None
data_bucket["connection_status_g"] = False
data_bucket["ssid_g"] = ""


# ----------------------------
# Hybrid REPL helpers
# ----------------------------
try:
    import sys
    import time as _pytime

    try:
        import ujson as _json
    except Exception:
        import json as _json

    try:
        import ubinascii as _binascii
    except Exception:
        import binascii as _binascii

    try:
        import utime as _utime
    except Exception:
        _utime = None

    try:
        import hybrid_sim as _hyb_mod
    except Exception:
        _hyb_mod = None

    HYBRID_BAUDRATE = 115200
    _HYB_GLOBAL_DEBOUNCE_SEC = 0.150
    _HYB_GRAPH_DEBOUNCE_SEC = 0.001

    def _hyb_norm_delay(value, fallback):
        try:
            parsed = float(value)
            if parsed > 0:
                return parsed
        except Exception:
            pass
        return float(fallback)

    if not isinstance(data_bucket.get("hyb_delay_local_map"), dict):
        data_bucket["hyb_delay_local_map"] = {}

    data_bucket["hyb_delay_global_sec"] = _hyb_norm_delay(
        data_bucket.get("hyb_delay_global_sec", _HYB_GLOBAL_DEBOUNCE_SEC),
        _HYB_GLOBAL_DEBOUNCE_SEC,
    )
    data_bucket["hyb_delay_local_map"]["graph"] = _hyb_norm_delay(
        data_bucket["hyb_delay_local_map"].get("graph", _HYB_GRAPH_DEBOUNCE_SEC),
        _HYB_GRAPH_DEBOUNCE_SEC,
    )
    data_bucket["hyb_global_debounce_sec"] = data_bucket["hyb_delay_global_sec"]
    data_bucket["hyb_graph_fast_debounce_sec"] = data_bucket["hyb_delay_local_map"]["graph"]

    def hyb_delay_set_global(sec):
        sec = _hyb_norm_delay(sec, data_bucket.get("hyb_delay_global_sec", _HYB_GLOBAL_DEBOUNCE_SEC))
        data_bucket["hyb_delay_global_sec"] = sec
        data_bucket["hyb_global_debounce_sec"] = sec
        return sec

    def hyb_delay_set_local(name, sec):
        key = str(name).strip().lower()
        if not key:
            return None
        if not isinstance(data_bucket.get("hyb_delay_local_map"), dict):
            data_bucket["hyb_delay_local_map"] = {}
        sec = _hyb_norm_delay(sec, data_bucket.get("hyb_delay_global_sec", _HYB_GLOBAL_DEBOUNCE_SEC))
        data_bucket["hyb_delay_local_map"][key] = sec
        if key == "graph":
            data_bucket["hyb_graph_fast_debounce_sec"] = sec
        return sec

    def hyb_delay_use_global():
        sec = _hyb_norm_delay(
            data_bucket.get("hyb_delay_global_sec", _HYB_GLOBAL_DEBOUNCE_SEC),
            _HYB_GLOBAL_DEBOUNCE_SEC,
        )
        typer.debounce_delay_time = sec
        data_bucket["hyb_delay_active"] = "global"
        data_bucket["hyb_delay_active_sec"] = sec
        return sec

    def hyb_delay_use_local(name):
        key = str(name).strip().lower()
        if not key:
            return hyb_delay_use_global()
        local_map = data_bucket.get("hyb_delay_local_map")
        if not isinstance(local_map, dict):
            return hyb_delay_use_global()
        sec = _hyb_norm_delay(
            local_map.get(key, data_bucket.get("hyb_delay_global_sec", _HYB_GLOBAL_DEBOUNCE_SEC)),
            data_bucket.get("hyb_delay_global_sec", _HYB_GLOBAL_DEBOUNCE_SEC),
        )
        typer.debounce_delay_time = sec
        data_bucket["hyb_delay_active"] = key
        data_bucket["hyb_delay_active_sec"] = sec
        return sec

    builtins.hyb_delay_set_global = hyb_delay_set_global
    builtins.hyb_delay_set_local = hyb_delay_set_local
    builtins.hyb_delay_use_global = hyb_delay_use_global
    builtins.hyb_delay_use_local = hyb_delay_use_local
    hyb_delay_use_global()

    _hyb_compat_state = {
        "mode": "local",
        "hybrid_requested": False,
        "stream_enabled": False,
        "protocol_enabled": False,
        "accept_protocol_stdin": False,
        "keypad_mode": "local",
    }
    data_bucket["hyb_stream_enabled"] = False
    data_bucket["hyb_protocol_enabled"] = False
    data_bucket["hyb_accept_protocol_stdin"] = False
    data_bucket["hyb_mode"] = "local"
    data_bucket["hyb_requested"] = False
    data_bucket["hyb_keypad_mode"] = "local"

    def _hyb_apply_compat_state(
        mode=None,
        hybrid_requested=None,
        stream_enabled=None,
        protocol_enabled=None,
        accept_protocol_stdin=None,
        keypad_mode=None,
    ):
        if mode is not None:
            _hyb_compat_state["mode"] = str(mode)
        if hybrid_requested is not None:
            _hyb_compat_state["hybrid_requested"] = bool(hybrid_requested)
        if stream_enabled is not None:
            _hyb_compat_state["stream_enabled"] = bool(stream_enabled)
        if protocol_enabled is not None:
            _hyb_compat_state["protocol_enabled"] = bool(protocol_enabled)
        if accept_protocol_stdin is not None:
            _hyb_compat_state["accept_protocol_stdin"] = bool(accept_protocol_stdin)
        if keypad_mode is not None:
            _hyb_compat_state["keypad_mode"] = str(keypad_mode)

        data_bucket["hyb_mode"] = _hyb_compat_state["mode"]
        data_bucket["hyb_requested"] = _hyb_compat_state["hybrid_requested"]
        data_bucket["hyb_stream_enabled"] = _hyb_compat_state["stream_enabled"]
        data_bucket["hyb_protocol_enabled"] = _hyb_compat_state["protocol_enabled"]
        data_bucket["hyb_accept_protocol_stdin"] = _hyb_compat_state["accept_protocol_stdin"]
        data_bucket["hyb_keypad_mode"] = _hyb_compat_state["keypad_mode"]
        return dict(_hyb_compat_state)

    _hyb_key_queue = []

    def _hyb_sleep_ms(ms):
        if _utime is not None and hasattr(_utime, "sleep_ms"):
            _utime.sleep_ms(ms)
        else:
            _pytime.sleep(ms / 1000.0)

    def _hyb_write_line(text):
        text = str(text)
        try:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
        except Exception:
            try:
                print(text)
            except Exception:
                pass

    def _hyb_global(name):
        try:
            value = globals().get(name, None)
        except Exception:
            value = None
        if value is not None:
            return value
        try:
            return getattr(builtins, name, None)
        except Exception:
            return None

    def _hyb_clean_line(text):
        try:
            return str(text).replace("𖤓", "_")
        except Exception:
            return ""

    def _hyb_nav_state():
        try:
            nav_obj = _hyb_global("nav")
            if nav_obj is not None and hasattr(nav_obj, "current_state"):
                return str(nav_obj.current_state())
        except Exception:
            pass
        return ""

    def _hyb_menu_lines():
        try:
            menu_obj = _hyb_global("menu")
            if menu_obj is None or not hasattr(menu_obj, "buffer"):
                return []
            buf = menu_obj.buffer()
            if not isinstance(buf, (list, tuple)) or not buf:
                return []
            if all(_hyb_clean_line(item).startswith("label_") for item in buf):
                return []
            cur = -1
            if hasattr(menu_obj, "cursor"):
                try:
                    cur = int(menu_obj.cursor())
                except Exception:
                    cur = -1
            lines = []
            for index, row in enumerate(buf):
                prefix = ">" if index == cur else " "
                lines.append(prefix + _hyb_clean_line(row))
            return lines[:7]
        except Exception:
            return []

    def _hyb_form_lines():
        try:
            form_obj = _hyb_global("form")
            if form_obj is None or not hasattr(form_obj, "buffer"):
                return []
            buf = form_obj.buffer()
            if not isinstance(buf, (list, tuple)) or not buf:
                return []
            if all(_hyb_clean_line(item).startswith("label_") for item in buf):
                return []
            cur = -1
            if hasattr(form_obj, "cursor"):
                try:
                    cur = int(form_obj.cursor())
                except Exception:
                    cur = -1
            inp_list = {}
            if hasattr(form_obj, "inp_list"):
                try:
                    inp_list = form_obj.inp_list() or {}
                except Exception:
                    inp_list = {}
            inp_start = 0
            if hasattr(form_obj, "inp_display_position"):
                try:
                    inp_start = int(form_obj.inp_display_position())
                except Exception:
                    inp_start = 0
            inp_cols = 19
            if hasattr(form_obj, "inp_cols"):
                try:
                    inp_cols = int(form_obj.inp_cols())
                except Exception:
                    inp_cols = 19
            lines = []
            for index, row in enumerate(buf):
                name = _hyb_clean_line(row)
                if name.startswith("inp_"):
                    value = _hyb_clean_line(inp_list.get(name, ""))
                    line = "=>" + value[inp_start : inp_start + inp_cols]
                else:
                    line = name
                prefix = ">" if index == cur and not name.startswith("inp_") else " "
                lines.append(prefix + line)
            return lines[:7]
        except Exception:
            return []

    def _hyb_text_lines():
        try:
            text_obj = _hyb_global("text")
            if text_obj is None or not hasattr(text_obj, "buffer"):
                return []
            buf = text_obj.buffer()
            if not isinstance(buf, (list, tuple)) or not buf:
                return []
            return [_hyb_clean_line(row) for row in buf[:7]]
        except Exception:
            return []

    def _hyb_lines_snapshot():
        for producer in (_hyb_text_lines, _hyb_form_lines, _hyb_menu_lines):
            lines = producer()
            if lines:
                return lines
        return []

    def _hyb_fb_to_b64(raw_fb):
        if raw_fb is None:
            return ""
        try:
            if isinstance(raw_fb, memoryview):
                raw_fb = raw_fb.tobytes()
            elif not isinstance(raw_fb, (bytes, bytearray)):
                raw_fb = bytes(raw_fb)
            encoded = _binascii.b2a_base64(raw_fb)
            if isinstance(encoded, bytes):
                return encoded.decode().strip()
            return str(encoded).strip()
        except Exception:
            return ""

    def _hyb_capture_mode():
        if _hyb_mod is None or not hasattr(_hyb_mod, "mode"):
            return False
        try:
            return bool(_hyb_mod.mode())
        except Exception:
            return False

    def _hyb_capture_enabled():
        if _hyb_mod is None or not hasattr(_hyb_mod, "enabled"):
            return False
        try:
            return bool(_hyb_mod.enabled())
        except Exception:
            return False

    def _hyb_state_payload(state, include_fb=False):
        payload = {}
        if isinstance(state, dict):
            try:
                payload.update(state)
            except Exception:
                payload = {}
        try:
            payload["frame_id"] = int(payload.get("frame_id", -1))
        except Exception:
            payload["frame_id"] = -1
        payload["fb_seq"] = payload["frame_id"] & 0x7F if payload["frame_id"] >= 0 else 0
        payload["mode"] = bool(payload.get("mode", _hyb_capture_mode()))
        payload["capture_enabled"] = bool(payload.get("capture_enabled", _hyb_capture_enabled()))
        payload["fb_seen"] = bool(payload.get("fb_seen", payload["capture_enabled"]))
        payload["nav"] = _hyb_nav_state()
        payload["lines"] = _hyb_lines_snapshot()
        raw_fb = payload.pop("fb", None)
        if include_fb or raw_fb is not None:
            if raw_fb is None and _hyb_mod is not None and hasattr(_hyb_mod, "read_fb"):
                try:
                    raw_fb = _hyb_mod.read_fb()
                except Exception:
                    raw_fb = None
            fb_b64 = _hyb_fb_to_b64(raw_fb)
            if fb_b64:
                payload["fb"] = fb_b64
                payload["fb_full"] = True
        return payload

    def _hyb_emit_state(last_frame=-1, force_full=False):
        if _hyb_mod is None or not hasattr(_hyb_mod, "status") or not hasattr(_hyb_mod, "poll_state"):
            _hyb_write_line("HYBRID_SYNC_ERR:MODULE_MISSING")
            return False
        try:
            last_frame = int(last_frame)
        except Exception:
            last_frame = -1
        try:
            if force_full:
                state = _hyb_mod.status()
            else:
                state = _hyb_mod.poll_state(last_frame)
            payload = _hyb_state_payload(state, include_fb=force_full)
            _hyb_write_line("STATE:" + _json.dumps(payload))
            return True
        except Exception as exc:
            _hyb_write_line("HYBRID_SYNC_ERR:%s" % exc)
            return False

    def _hyb_ping(token=""):
        _hyb_write_line("ECHO:%s" % str(token).strip())

    def _hyb_mode(enabled=None):
        if _hyb_mod is None or not hasattr(_hyb_mod, "mode"):
            if enabled is not None:
                _hyb_write_line("HYBRID_MODE_ERR:MODULE_MISSING")
            return False
        if enabled is None:
            try:
                return bool(_hyb_mod.mode())
            except Exception:
                return False
        try:
            _hyb_mod.mode(bool(enabled))
            return True
        except Exception as exc:
            _hyb_write_line("HYBRID_MODE_ERR:%s" % exc)
            return False

    def _hyb_status():
        if _hyb_mod is None or not hasattr(_hyb_mod, "status"):
            _hyb_write_line("HYBRID_STATUS_ERR:MODULE_MISSING")
            return
        try:
            payload = _hyb_state_payload(_hyb_mod.status(), include_fb=False)
            _hyb_write_line("STATE:" + _json.dumps(payload))
        except Exception as exc:
            _hyb_write_line("HYBRID_STATUS_ERR:%s" % exc)

    def _hyb_queue_key(col, row):
        try:
            col = int(col)
            row = int(row)
            if not (0 <= col <= 4 and 0 <= row <= 9):
                return False
            _hyb_key_queue.append((col, row))
            if len(_hyb_key_queue) > 1:
                del _hyb_key_queue[:-1]
            return True
        except Exception:
            return False

    def _hyb_key(col, row):
        if _hyb_queue_key(col, row):
            _hyb_write_line("HYBRID_KEY_OK:%d,%d" % (int(col), int(row)))
            return True
        _hyb_write_line("HYBRID_KEY_ERR:RANGE")
        return False

    def _hyb_key_enqueue(col, row):
        return _hyb_queue_key(col, row)

    def _hyb_poll_state(last_frame=-1):
        return _hyb_emit_state(last_frame, False)

    def _hyb_sync_full():
        return _hyb_emit_state(-1, True)

    def _hyb_emit_hybrid_config():
        try:
            debounce_ms = int(float(getattr(typer, "debounce_delay_time", 0.100)) * 1000)
            if debounce_ms > 0:
                _hyb_write_line("HYB_KEY_DEB_MS:%d" % debounce_ms)
        except Exception:
            pass
        try:
            graph_sec = data_bucket.get("hyb_graph_fast_debounce_sec", None)
            if graph_sec is None:
                graph_sec = data_bucket.get("hyb_delay_local_map", {}).get("graph", _HYB_GRAPH_DEBOUNCE_SEC)
            graph_ms = int(float(graph_sec) * 1000)
            if graph_ms > 0:
                _hyb_write_line("HYB_GRAPH_FAST_MS:%d" % graph_ms)
        except Exception:
            pass

    def hyb_stream_set_enabled(enabled):
        enabled = bool(enabled)
        _hyb_apply_compat_state(stream_enabled=enabled)
        return enabled

    def hyb_stream_is_enabled():
        return bool(_hyb_compat_state.get("stream_enabled"))

    def hyb_bridge_status():
        return {
            "mode": _hyb_compat_state.get("mode", "local"),
            "hybrid_requested": bool(_hyb_compat_state.get("hybrid_requested", False)),
            "stream_enabled": bool(_hyb_compat_state.get("stream_enabled", False)),
            "protocol_enabled": bool(_hyb_compat_state.get("protocol_enabled", False)),
            "accept_protocol_stdin": bool(_hyb_compat_state.get("accept_protocol_stdin", False)),
            "delay_active": data_bucket.get("hyb_delay_active", "global"),
            "delay_active_sec": data_bucket.get("hyb_delay_active_sec", data_bucket.get("hyb_delay_global_sec")),
        }

    def hyb_enter_local_mode():
        _hyb_apply_compat_state(
            mode="local",
            hybrid_requested=False,
            stream_enabled=False,
            protocol_enabled=False,
            accept_protocol_stdin=False,
            keypad_mode="local",
        )
        _hyb_mode(False)
        _hyb_write_line("CTRL:HYBRID_DISABLED:OK")
        return True

    def hyb_enter_command_mode():
        _hyb_apply_compat_state(
            mode="command",
            hybrid_requested=_hyb_compat_state.get("hybrid_requested", False),
            stream_enabled=False,
            protocol_enabled=False,
            accept_protocol_stdin=False,
            keypad_mode="local",
        )
        _hyb_mode(False)
        _hyb_write_line("CTRL:COMMAND:OK")
        return True

    def hyb_enter_exec_mode():
        _hyb_apply_compat_state(
            mode="exec",
            hybrid_requested=_hyb_compat_state.get("hybrid_requested", False),
            stream_enabled=False,
            protocol_enabled=False,
            accept_protocol_stdin=False,
            keypad_mode="local",
        )
        _hyb_mode(False)
        _hyb_write_line("CTRL:HYBRID_OFF:OK")
        return True

    def hyb_enter_hybrid_mode(stream_enabled=False):
        _hyb_apply_compat_state(
            mode="hybrid",
            hybrid_requested=True,
            stream_enabled=bool(stream_enabled),
            protocol_enabled=False,
            accept_protocol_stdin=False,
            keypad_mode="hybrid",
        )
        hyb_delay_use_global()
        _hyb_mode(True)
        _hyb_write_line("CTRL:HYBRID_ON:OK")
        if stream_enabled:
            _hyb_sync_full()
        return True

    def hyb_stream_updated_buffer():
        while True:
            if hyb_stream_is_enabled():
                _hyb_sleep_ms(100)
            else:
                _hyb_sleep_ms(250)

    def _hyb_release_rows(rows):
        for row_pin in rows:
            try:
                machine.Pin(row_pin, machine.Pin.OUT).value(1)
            except Exception:
                pass

    def _hyb_wait_while_keypad_blocked(rows):
        if not calsci_runtime.calsci_keypad_blocked():
            return False
        if _hyb_key_queue:
            del _hyb_key_queue[:]
        calsci_runtime.wait_if_repl_busy(lambda: _hyb_release_rows(rows))
        return True

    def _hyb_keypad_loop():
        rows = getattr(typer.keypad, "rows", [])
        cols = getattr(typer.keypad, "cols", [])
        while True:
            if _hyb_wait_while_keypad_blocked(rows):
                continue
            if _hyb_key_queue:
                return _hyb_key_queue.pop(0)
            for row in range(len(rows)):
                if _hyb_wait_while_keypad_blocked(rows):
                    break
                machine.Pin(rows[row], machine.Pin.OUT).value(0)
                hit = None
                for col in range(len(cols)):
                    if _hyb_wait_while_keypad_blocked(rows):
                        hit = None
                        break
                    if machine.Pin(cols[col], machine.Pin.IN, machine.Pin.PULL_UP).value() == 0:
                        hit = (col, row)
                        break
                machine.Pin(rows[row], machine.Pin.OUT).value(1)
                if calsci_runtime.calsci_keypad_blocked():
                    break
                if hit is not None:
                    return hit
            _hyb_sleep_ms(5)

    builtins.hyb_stream_set_enabled = hyb_stream_set_enabled
    builtins.hyb_stream_is_enabled = hyb_stream_is_enabled
    builtins.hyb_bridge_status = hyb_bridge_status
    builtins.hyb_enter_local_mode = hyb_enter_local_mode
    builtins.hyb_enter_command_mode = hyb_enter_command_mode
    builtins.hyb_enter_exec_mode = hyb_enter_exec_mode
    builtins.hyb_enter_hybrid_mode = hyb_enter_hybrid_mode
    builtins.hyb_keypad_input = _hyb_keypad_loop
    builtins.hyb_stream_updated_buffer = hyb_stream_updated_buffer
    typer.keypad.keypad_loop = _hyb_keypad_loop

    if _hyb_mod is not None:
        try:
            if hasattr(_hyb_mod, "enable"):
                _hyb_mod.enable(True)
        except Exception:
            pass
        try:
            if hasattr(_hyb_mod, "mode") and bool(_hyb_mod.mode()):
                _hyb_mod.mode(False)
        except Exception:
            pass

    _hyb_write_line("HYBRID_PROTO:POLL_V1")
    _hyb_emit_hybrid_config()
    _hyb_write_line("HYBRID_READY")
    _hyb_write_line("HYBRID_BAUD:%d" % HYBRID_BAUDRATE)

except Exception as _hyb_exc:
    print("HYBRID_BRIDGE_ERR:", _hyb_exc)
    try:
        import sys as _sys
        _sys.print_exception(_hyb_exc)
    except Exception:
        pass