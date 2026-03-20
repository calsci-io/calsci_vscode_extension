import time
from machine import Pin
import builtins

import lvgl as lv
import st7565 as display

try:
    import calsci_runtime
except ImportError:
    calsci_runtime = None


WIDTH = 128
HEIGHT = 64
FB_SIZE = (WIDTH * HEIGHT) // 8
SRC_STRIDE = WIDTH // 8
LOOP_DELAY_MS = 20

BACKLIGHT_PIN_29 = 5
BACKLIGHT_PIN_30 = 19
BACKLIGHT_PWM_FREQ = 1000
BACKLIGHT_DIM_PERCENT = 5

MAX_VISIBLE_ROWS = 6
MAX_RADIUS = 16
MAX_BORDER = 6

_BACKLIGHT_PWM = None
DISPLAY_STATE = {}

ROW_PINS = [14, 21, 47, 48, 38, 39, 40, 41, 42, 1]
COL_PINS = [8, 18, 17, 15, 7]

KEYMAP_DEFAULT = [
    ["on", "alpha", "beta", "home", "tab"],
    ["backlight", "back", "toolbox", "diff(", "ln"],
    ["nav_l", "nav_d", "nav_r", "ok", "nav_u"],
    ["module", "bluetooth", "sin(", "cos", "tan"],
    ["ingn(", "pi", "e", "summation", "fraction"],
    ["log", "pow(", "pow( ,0.5)", "pow( ,2)", "S_D"],
    ["7", "8", "9", "nav_b", "AC"],
    ["4", "5", "6", "*", "/"],
    ["1", "2", "3", "+", "-"],
    [".", "0", ",", "ans", "exe"],
]

KEYMAP_ALPHA = [
    ["on", "alpha", "beta", "home", "tab"],
    ["backlight", "back", "caps", "f", "l"],
    ["nav_l", "nav_d", "nav_r", "ok", "nav_u"],
    ["a", "b", "c", "d", "e"],
    ["g", "h", "i", "j", "k"],
    ["m", "n", "o", "p", "q"],
    ["r", "s", "t", "nav_b", "AC"],
    ["u", "v", "w", "*", "/"],
    ["x", "y", "z", "+", "-"],
    [" ", "off", "tab", "ans", "exe"],
]

KEYMAP_BETA = [
    ["on", "alpha", "beta", "home", "tab"],
    ["backlight", "back", "undo", "=", "$"],
    ["nav_l", "nav_d", "nav_r", "ok", "nav_u"],
    ["copy", "paste", "asin(", "acos(", "atan("],
    ["&", "`", '"', "'", "shot"],
    ["^", "~", "!", "<", ">"],
    ["[", "]", "%", "nav_b", "AC"],
    ["{", "}", ":", "*", "/"],
    ["(", ")", ";", "+", "-"],
    ["@", "?", '"', "ans", "exe"],
]

KEYMAP_LAYERS = {"d": KEYMAP_DEFAULT, "a": KEYMAP_ALPHA, "b": KEYMAP_BETA}
LAYER_NAMES = {"d": "DEF", "a": "ALPHA", "b": "BETA"}
TEXT_ALIGN_VALUES = ("left", "center", "right")
REPEAT_KEYS = {"nav_u", "nav_d", "nav_l", "nav_r", "+", "-"}

SAMPLE_ITEMS = (
    "Run Program",
    "Graph Viewer",
    "Equation Solve",
    "Statistics",
    "Matrices",
    "Complex Mode",
    "Tables",
    "Settings",
    "Diagnostics",
    "Backlight",
    "About CalSci",
    "Power Off",
)

CONTROL_KEYS = {
    "on",
    "home",
    "back",
    "AC",
    "alpha",
    "beta",
    "tab",
    "nav_u",
    "nav_d",
    "nav_l",
    "nav_r",
    "nav_b",
    "ok",
    "exe",
    "backlight",
    "wifi",
    "toolbox",
    "module",
    "bluetooth",
    "caps",
    "undo",
    "copy",
    "paste",
    "shot",
}

TOKEN_MAP = {
    "off": " ",
    "tab": "    ",
    "summation": "sum(",
    "fraction": "/",
    "pow( ,0.5)": "sqrt(",
    "pow( ,2)": "^2",
}

PARAMETERS = (
    {"name": "x", "kind": "int", "min": 0, "max": WIDTH - 1},
    {"name": "y", "kind": "int", "min": 0, "max": HEIGHT - 1},
    {"name": "rel_x", "kind": "int", "min": 0, "max": WIDTH - 1},
    {"name": "rel_y", "kind": "int", "min": 0, "max": HEIGHT - 1},
    {"name": "width", "kind": "int", "min": 1, "max": WIDTH},
    {"name": "height", "kind": "int", "min": 1, "max": HEIGHT},
    {"name": "inset_left", "kind": "int", "min": 0, "max": 32},
    {"name": "inset_right", "kind": "int", "min": 0, "max": 32},
    {"name": "inset_top", "kind": "int", "min": 0, "max": 24},
    {"name": "inset_bottom", "kind": "int", "min": 0, "max": 24},
    {"name": "height_adjust", "kind": "int", "min": -8, "max": 8},
    {"name": "min_height", "kind": "int", "min": 1, "max": 32},
    {"name": "text", "kind": "text"},
    {"name": "filled", "kind": "bool"},
    {"name": "border_width", "kind": "int", "min": 0, "max": MAX_BORDER},
    {"name": "radius", "kind": "int", "min": 0, "max": MAX_RADIUS},
    {"name": "text_align", "kind": "enum", "values": TEXT_ALIGN_VALUES},
    {"name": "text_offset_x", "kind": "int", "min": -20, "max": 20},
    {"name": "text_offset_y", "kind": "int", "min": -12, "max": 12},
    {"name": "padding_left", "kind": "int", "min": 0, "max": 20},
    {"name": "padding_right", "kind": "int", "min": 0, "max": 20},
    {"name": "invert", "kind": "bool"},
    {"name": "item_total", "kind": "int", "min": 1, "max": len(SAMPLE_ITEMS)},
    {"name": "visible_rows", "kind": "int", "min": 1, "max": MAX_VISIBLE_ROWS},
    {"name": "selected_index", "kind": "int", "min": 0, "max": len(SAMPLE_ITEMS) - 1},
    {"name": "row_gap", "kind": "int", "min": 0, "max": 4},
    {"name": "row_height", "kind": "int", "min": 5, "max": 14},
    {"name": "show_scrollbar", "kind": "bool"},
    {"name": "show_footer", "kind": "bool"},
)

OBJECT_SPECS = (
    {
        "name": "menu_panel",
        "type": "container",
        "params": ("x", "y", "width", "height", "filled", "border_width", "radius"),
    },
    {
        "name": "header_box",
        "type": "container",
        "params": ("rel_x", "rel_y", "width", "height", "filled", "border_width", "radius"),
    },
    {
        "name": "header_label",
        "type": "label",
        "params": ("text", "text_align", "text_offset_x", "text_offset_y", "padding_left", "padding_right", "invert"),
    },
    {
        "name": "list_box",
        "type": "container",
        "params": ("rel_x", "rel_y", "width", "height", "filled", "border_width", "radius"),
    },
    {
        "name": "menu_state",
        "type": "state",
        "params": ("item_total", "visible_rows", "selected_index", "row_gap", "row_height", "show_scrollbar", "show_footer"),
    },
    {
        "name": "row_box",
        "type": "row_style",
        "params": ("inset_left", "inset_right", "inset_top", "height_adjust", "filled", "border_width", "radius"),
    },
    {
        "name": "row_text",
        "type": "label_style",
        "params": ("text_align", "text_offset_x", "text_offset_y", "padding_left", "padding_right", "invert"),
    },
    {
        "name": "selected_row_box",
        "type": "row_style",
        "params": ("inset_left", "inset_right", "inset_top", "height_adjust", "filled", "border_width", "radius"),
    },
    {
        "name": "selected_row_text",
        "type": "label_style",
        "params": ("text_align", "text_offset_x", "text_offset_y", "padding_left", "padding_right", "invert"),
    },
    {
        "name": "scrollbar_track",
        "type": "container",
        "params": ("inset_right", "inset_top", "inset_bottom", "width", "filled", "border_width", "radius"),
    },
    {
        "name": "scrollbar_thumb",
        "type": "container",
        "params": ("width", "min_height", "filled", "border_width", "radius"),
    },
    {
        "name": "footer_box",
        "type": "container",
        "params": ("rel_x", "rel_y", "width", "height", "filled", "border_width", "radius"),
    },
    {
        "name": "footer_label",
        "type": "label",
        "params": ("text", "text_align", "text_offset_x", "text_offset_y", "padding_left", "padding_right", "invert"),
    },
)

OBJECT_DEFAULTS = {
    "menu_panel": {
        "x": 2,
        "y": 2,
        "width": WIDTH - 4,
        "height": HEIGHT - 4,
        "filled": False,
        "border_width": 1,
        "radius": 0,
    },
    "header_box": {
        "rel_x": 3,
        "rel_y": 3,
        "width": 118,
        "height": 11,
        "filled": True,
        "border_width": 1,
        "radius": 0,
    },
    "header_label": {
        "text": "SCROLL MENU",
        "text_align": "center",
        "text_offset_x": 0,
        "text_offset_y": -1,
        "padding_left": 4,
        "padding_right": 4,
        "invert": True,
    },
    "list_box": {
        "rel_x": 3,
        "rel_y": 15,
        "width": 118,
        "height": 36,
        "filled": False,
        "border_width": 1,
        "radius": 0,
    },
    "menu_state": {
        "item_total": 10,
        "visible_rows": 4,
        "selected_index": 3,
        "row_gap": 1,
        "row_height": 8,
        "show_scrollbar": True,
        "show_footer": True,
    },
    "row_box": {
        "inset_left": 2,
        "inset_right": 9,
        "inset_top": 1,
        "height_adjust": 0,
        "filled": False,
        "border_width": 0,
        "radius": 0,
    },
    "row_text": {
        "text_align": "left",
        "text_offset_x": 0,
        "text_offset_y": 0,
        "padding_left": 2,
        "padding_right": 2,
        "invert": False,
    },
    "selected_row_box": {
        "inset_left": 1,
        "inset_right": 9,
        "inset_top": 0,
        "height_adjust": 0,
        "filled": True,
        "border_width": 1,
        "radius": 0,
    },
    "selected_row_text": {
        "text_align": "left",
        "text_offset_x": 0,
        "text_offset_y": 0,
        "padding_left": 2,
        "padding_right": 2,
        "invert": True,
    },
    "scrollbar_track": {
        "inset_right": 2,
        "inset_top": 1,
        "inset_bottom": 1,
        "width": 4,
        "filled": False,
        "border_width": 1,
        "radius": 1,
    },
    "scrollbar_thumb": {
        "width": 2,
        "min_height": 6,
        "filled": True,
        "border_width": 0,
        "radius": 1,
    },
    "footer_box": {
        "rel_x": 3,
        "rel_y": 53,
        "width": 118,
        "height": 7,
        "filled": True,
        "border_width": 0,
        "radius": 0,
    },
    "footer_label": {
        "text": "OK=EDIT HOME=LIST",
        "text_align": "center",
        "text_offset_x": 0,
        "text_offset_y": -1,
        "padding_left": 2,
        "padding_right": 2,
        "invert": True,
    },
}

OBJECT_INDEX_BY_NAME = {spec["name"]: index for index, spec in enumerate(OBJECT_SPECS, 1)}
PARAMETER_BY_NAME = {spec["name"]: spec for spec in PARAMETERS}


def clamp(value, minimum, maximum):
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def cycle_enum(current, values, direction):
    if current not in values:
        current = values[0]
    index = values.index(current)
    index = (index + direction) % len(values)
    return values[index]


def format_value(value):
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    return str(value)


def token_from_key(key):
    if key in CONTROL_KEYS:
        return None
    return TOKEN_MAP.get(key, key)


def build_object_defaults():
    values = {}
    for name, current in OBJECT_DEFAULTS.items():
        values[name] = dict(current)
    return values


def get_object_spec(selected_object_index):
    return OBJECT_SPECS[selected_object_index]


def get_object_params(object_name):
    for spec in OBJECT_SPECS:
        if spec["name"] == object_name:
            return spec["params"]
    return ()


def get_object_values(values, object_name):
    current = values.get(object_name)
    if current is None:
        current = dict(OBJECT_DEFAULTS.get(object_name, {}))
        values[object_name] = current
    return current


def get_default_selection():
    object_index = OBJECT_INDEX_BY_NAME["menu_state"] - 1
    param_names = get_object_params("menu_state")
    try:
        param_index = param_names.index("selected_index")
    except ValueError:
        param_index = 0
    return object_index, param_index


def acquire_calsci_keypad():
    state = {
        "runtime_owned": False,
        "keypad": None,
        "original_keypad_loop": None,
        "loop_globals": None,
        "original_machine": None,
        "original_queue": None,
        "keypad_module": None,
        "original_keypad_runtime": None,
    }

    if calsci_runtime is not None and not calsci_runtime.calsci_keypad_blocked():
        state["runtime_owned"] = bool(calsci_runtime.block_calsci_keypad())

    typer = getattr(builtins, "typer", None)
    keypad = getattr(typer, "keypad", None)
    if keypad is None:
        return state

    state["keypad"] = keypad
    state["original_keypad_loop"] = getattr(keypad, "keypad_loop", None)

    rows = tuple(getattr(keypad, "rows", ()) or ())
    cols = tuple(getattr(keypad, "cols", ()) or ())

    original_loop = state["original_keypad_loop"]
    loop_globals = getattr(original_loop, "__globals__", None) if callable(original_loop) else None
    if isinstance(loop_globals, dict):
        state["loop_globals"] = loop_globals
        original_machine = loop_globals.get("machine")
        if original_machine is not None:
            state["original_machine"] = original_machine
            loop_globals["machine"] = CalSciBlockedMachine(original_machine, rows, cols)
        if "_hyb_key_queue" in loop_globals:
            state["original_queue"] = loop_globals["_hyb_key_queue"]
            loop_globals["_hyb_key_queue"] = CalSciDropQueue()

    try:
        import input_modules.keypad as keypad_module
    except ImportError:
        keypad_module = None

    if keypad_module is not None:
        state["keypad_module"] = keypad_module
        state["original_keypad_runtime"] = getattr(keypad_module, "calsci_runtime", None)
        if calsci_runtime is not None:
            keypad_module.calsci_runtime = calsci_runtime

    def blocked_keypad_loop():
        while True:
            release_keypad_rows(rows)
            time.sleep_ms(5)

    keypad.keypad_loop = blocked_keypad_loop
    release_keypad_rows(rows)
    return state


def release_calsci_keypad(state):
    if not isinstance(state, dict):
        return False

    keypad = state.get("keypad")
    original_keypad_loop = state.get("original_keypad_loop")
    if keypad is not None and original_keypad_loop is not None:
        keypad.keypad_loop = original_keypad_loop

    loop_globals = state.get("loop_globals")
    if isinstance(loop_globals, dict):
        if state.get("original_machine") is not None:
            loop_globals["machine"] = state["original_machine"]
        if state.get("original_queue") is not None:
            loop_globals["_hyb_key_queue"] = state["original_queue"]

    keypad_module = state.get("keypad_module")
    if keypad_module is not None:
        keypad_module.calsci_runtime = state.get("original_keypad_runtime")

    if state.get("runtime_owned") and calsci_runtime is not None:
        return bool(calsci_runtime.unblock_calsci_keypad())
    return False


def release_keypad_rows(rows):
    for row_pin in rows:
        try:
            Pin(row_pin, Pin.OUT).value(1)
        except Exception:
            pass


class CalSciDropQueue:
    def append(self, _item):
        return None

    def pop(self, _index=0):
        raise IndexError

    def __len__(self):
        return 0

    def __bool__(self):
        return False

    def __delitem__(self, _index):
        return None


class CalSciBlockedPin:
    def __init__(self, real_pin, pin_id, rows, cols):
        self._real_pin = real_pin
        self._pin_id = pin_id
        self._rows = rows
        self._cols = cols

    def value(self, new_value=None):
        if new_value is None:
            if self._pin_id in self._cols:
                return 1
            return self._real_pin.value()

        if self._pin_id in self._rows:
            self._real_pin.value(1)
            return 1

        return self._real_pin.value(new_value)

    def __getattr__(self, name):
        return getattr(self._real_pin, name)


class CalSciBlockedPinFactory:
    def __init__(self, real_pin_type, rows, cols):
        self._real_pin_type = real_pin_type
        self._rows = rows
        self._cols = cols
        self.IN = getattr(real_pin_type, "IN", None)
        self.OUT = getattr(real_pin_type, "OUT", None)
        self.PULL_UP = getattr(real_pin_type, "PULL_UP", None)

    def __call__(self, pin_id, *args, **kwargs):
        real_pin = self._real_pin_type(pin_id, *args, **kwargs)
        return CalSciBlockedPin(real_pin, pin_id, self._rows, self._cols)


class CalSciBlockedMachine:
    def __init__(self, real_machine, rows, cols):
        self._real_machine = real_machine
        self.Pin = CalSciBlockedPinFactory(real_machine.Pin, rows, cols)

    def __getattr__(self, name):
        return getattr(self._real_machine, name)


class MatrixKeypad:
    def __init__(self, rows, cols, layers, layer="d", debounce_ms=70, repeat_ms=170):
        self.row_pins = [Pin(pin, Pin.OUT) for pin in rows]
        self.col_pins = [Pin(pin, Pin.IN, Pin.PULL_UP) for pin in cols]
        for pin in self.row_pins:
            pin.value(1)

        self.layers = layers
        self.layer = layer
        self.debounce_ms = debounce_ms
        self.repeat_ms = repeat_ms

        now = time.ticks_ms()
        self._sample_raw = None
        self._sample_since = now
        self._stable_key = None
        self._next_repeat = now
        self._events = []

    def toggle_layer(self, target):
        if self.layer == target:
            self.layer = "d"
        else:
            self.layer = target

    def _scan_once(self):
        keymap = self.layers[self.layer]
        for row_index, row_pin in enumerate(self.row_pins):
            row_pin.value(0)
            for col_index, col_pin in enumerate(self.col_pins):
                if col_pin.value() == 0:
                    row_pin.value(1)
                    return keymap[row_index][col_index]
            row_pin.value(1)
        return None

    def tick(self):
        now = time.ticks_ms()
        raw = self._scan_once()

        if raw != self._sample_raw:
            self._sample_raw = raw
            self._sample_since = now
            return

        if time.ticks_diff(now, self._sample_since) < self.debounce_ms:
            return

        if raw != self._stable_key:
            self._stable_key = raw
            if raw is not None:
                self._events.append(raw)
                self._next_repeat = time.ticks_add(now, self.repeat_ms)
            return

        if raw in REPEAT_KEYS and time.ticks_diff(now, self._next_repeat) >= 0:
            self._events.append(raw)
            self._next_repeat = time.ticks_add(now, self.repeat_ms)

    def pop_event(self):
        if not self._events:
            return None
        return self._events.pop(0)


def ensure_st7565_ready():
    probe = bytearray(FB_SIZE)
    try:
        display.graphics(probe)
    except Exception:
        display.init(9, 11, 10, 13, 12)

    display.on()
    display.invert(False)
    display.all_points_on(False)
    display.graphics(probe)


def _set_backlight_pwm(pin_obj, brightness_percent):
    global _BACKLIGHT_PWM

    try:
        from machine import PWM
    except Exception:
        return False

    try:
        pwm = PWM(pin_obj)
    except Exception:
        return False

    try:
        pwm.freq(BACKLIGHT_PWM_FREQ)
    except Exception:
        pass

    brightness_percent = clamp(int(brightness_percent), 0, 100)
    duty_u16 = 65535 - int((brightness_percent * 65535) // 100)

    try:
        pwm.duty_u16(duty_u16)
    except Exception:
        try:
            pwm.duty(int((duty_u16 * 1023) // 65535))
        except Exception:
            return False

    _BACKLIGHT_PWM = pwm
    return True


def ensure_backlight_on():
    try:
        from apps.settings.backlight import backlight_pin
    except Exception:
        backlight_pin = None

    if backlight_pin is not None:
        if _set_backlight_pwm(backlight_pin, BACKLIGHT_DIM_PERCENT):
            return True
        try:
            backlight_pin.off()
            return True
        except Exception:
            try:
                backlight_pin.value(0)
                return True
            except Exception:
                pass

    for backlight_pin_id in (BACKLIGHT_PIN_29, BACKLIGHT_PIN_30):
        try:
            pin = Pin(backlight_pin_id, Pin.OUT)
            if _set_backlight_pwm(pin, BACKLIGHT_DIM_PERCENT):
                return True
            try:
                pin.off()
            except Exception:
                pin.value(0)
            return True
        except Exception:
            pass

    return False


def build_lvgl_display():
    if not lv.is_initialized():
        lv.init()
    else:
        old = lv.display_get_default()
        if old is not None:
            try:
                old.delete()
            except Exception:
                pass

    draw_buf = bytearray(FB_SIZE + 8)
    tx_buf = bytearray(FB_SIZE)

    disp = lv.display_create(WIDTH, HEIGHT)
    disp.set_color_format(lv.COLOR_FORMAT.I1)
    disp.set_buffers(draw_buf, None, len(draw_buf), lv.DISPLAY_RENDER_MODE.FULL)
    disp.set_default()

    def i1_to_st7565(src_bytes, dst_bytes):
        for index in range(FB_SIZE):
            dst_bytes[index] = 0

        for y in range(HEIGHT):
            page = y >> 3
            bit = 1 << (y & 7)
            src_row = y * SRC_STRIDE
            dst_row = page * WIDTH

            for xb in range(SRC_STRIDE):
                value = src_bytes[src_row + xb]
                x = xb * 8

                if value & 0x80:
                    dst_bytes[dst_row + x + 0] |= bit
                if value & 0x40:
                    dst_bytes[dst_row + x + 1] |= bit
                if value & 0x20:
                    dst_bytes[dst_row + x + 2] |= bit
                if value & 0x10:
                    dst_bytes[dst_row + x + 3] |= bit
                if value & 0x08:
                    dst_bytes[dst_row + x + 4] |= bit
                if value & 0x04:
                    dst_bytes[dst_row + x + 5] |= bit
                if value & 0x02:
                    dst_bytes[dst_row + x + 6] |= bit
                if value & 0x01:
                    dst_bytes[dst_row + x + 7] |= bit

    def flush_cb(_disp, _area, color_p):
        raw = color_p.__dereference__(len(draw_buf))
        i1_to_st7565(raw[8 : 8 + FB_SIZE], tx_buf)
        display.graphics(tx_buf)
        disp.flush_ready()

    disp.set_flush_cb(flush_cb)

    DISPLAY_STATE["draw_buf"] = draw_buf
    DISPLAY_STATE["tx_buf"] = tx_buf
    DISPLAY_STATE["disp"] = disp
    DISPLAY_STATE["flush_cb"] = flush_cb


def clear_obj_flag(obj, flag):
    method = getattr(obj, "clear_flag", None)
    if method is not None:
        method(flag)
        return

    method = getattr(obj, "remove_flag", None)
    if method is not None:
        method(flag)


def set_obj_hidden(obj, hidden):
    if hidden:
        method = getattr(obj, "add_flag", None)
        if method is not None:
            method(lv.obj.FLAG.HIDDEN)
    else:
        clear_obj_flag(obj, lv.obj.FLAG.HIDDEN)


def prepare_box(obj):
    clear_obj_flag(obj, lv.obj.FLAG.SCROLLABLE)


def prepare_label(label):
    clear_obj_flag(label, lv.obj.FLAG.SCROLLABLE)
    try:
        label.set_long_mode(lv.label.LONG.CLIP)
    except Exception:
        try:
            label.set_long_mode(lv.label.LONG_MODE.CLIP)
        except Exception:
            pass


def apply_box_style(obj, config):
    bg = lv.color_black() if config["filled"] else lv.color_white()
    obj.set_style_bg_opa(lv.OPA.COVER, 0)
    obj.set_style_bg_color(bg, 0)
    obj.set_style_border_width(config["border_width"], 0)
    obj.set_style_border_color(lv.color_black(), 0)
    obj.set_style_radius(config["radius"], 0)


def apply_label_style(label, config, available_width):
    text_width = max(1, available_width - config["padding_left"] - config["padding_right"])
    try:
        label.set_width(text_width)
    except Exception:
        pass

    label.set_style_text_color(lv.color_white() if config["invert"] else lv.color_black(), 0)

    offset_x = config["text_offset_x"]
    offset_y = config["text_offset_y"]
    align_mode = config["text_align"]
    if align_mode == "left":
        label.align(lv.ALIGN.LEFT_MID, config["padding_left"] + offset_x, offset_y)
    elif align_mode == "right":
        label.align(lv.ALIGN.RIGHT_MID, -config["padding_right"] + offset_x, offset_y)
    else:
        label.align(lv.ALIGN.CENTER, offset_x, offset_y)


def normalize_geometry(current, defaults, x_name, y_name):
    current[x_name] = clamp(int(current.get(x_name, defaults[x_name])), 0, WIDTH - 1)
    current[y_name] = clamp(int(current.get(y_name, defaults[y_name])), 0, HEIGHT - 1)
    max_width = max(1, WIDTH - current[x_name])
    max_height = max(1, HEIGHT - current[y_name])
    current["width"] = clamp(int(current.get("width", defaults["width"])), 1, max_width)
    current["height"] = clamp(int(current.get("height", defaults["height"])), 1, max_height)


def normalize_label_values(current, defaults):
    current["text"] = str(current.get("text", defaults.get("text", "")))
    current["text_align"] = current.get("text_align", defaults.get("text_align", "left"))
    if current["text_align"] not in TEXT_ALIGN_VALUES:
        current["text_align"] = defaults.get("text_align", "left")
    current["text_offset_x"] = clamp(int(current.get("text_offset_x", defaults.get("text_offset_x", 0))), -20, 20)
    current["text_offset_y"] = clamp(int(current.get("text_offset_y", defaults.get("text_offset_y", 0))), -12, 12)
    current["padding_left"] = clamp(int(current.get("padding_left", defaults.get("padding_left", 0))), 0, 20)
    current["padding_right"] = clamp(int(current.get("padding_right", defaults.get("padding_right", 0))), 0, 20)
    current["invert"] = bool(current.get("invert", defaults.get("invert", False)))


def normalize_row_style(current, defaults):
    current["inset_left"] = clamp(int(current.get("inset_left", defaults["inset_left"])), 0, 32)
    current["inset_right"] = clamp(int(current.get("inset_right", defaults["inset_right"])), 0, 32)
    current["inset_top"] = clamp(int(current.get("inset_top", defaults["inset_top"])), 0, 24)
    current["height_adjust"] = clamp(int(current.get("height_adjust", defaults["height_adjust"])), -8, 8)
    current["filled"] = bool(current.get("filled", defaults["filled"]))
    current["border_width"] = clamp(int(current.get("border_width", defaults["border_width"])), 0, MAX_BORDER)
    current["radius"] = clamp(int(current.get("radius", defaults["radius"])), 0, MAX_RADIUS)


def normalize_scrollbar_track(current, defaults):
    current["inset_right"] = clamp(int(current.get("inset_right", defaults["inset_right"])), 0, 24)
    current["inset_top"] = clamp(int(current.get("inset_top", defaults["inset_top"])), 0, 24)
    current["inset_bottom"] = clamp(int(current.get("inset_bottom", defaults["inset_bottom"])), 0, 24)
    current["width"] = clamp(int(current.get("width", defaults["width"])), 1, 16)
    current["filled"] = bool(current.get("filled", defaults["filled"]))
    current["border_width"] = clamp(int(current.get("border_width", defaults["border_width"])), 0, MAX_BORDER)
    current["radius"] = clamp(int(current.get("radius", defaults["radius"])), 0, MAX_RADIUS)


def normalize_scrollbar_thumb(current, defaults):
    current["width"] = clamp(int(current.get("width", defaults["width"])), 1, 16)
    current["min_height"] = clamp(int(current.get("min_height", defaults["min_height"])), 1, 32)
    current["filled"] = bool(current.get("filled", defaults["filled"]))
    current["border_width"] = clamp(int(current.get("border_width", defaults["border_width"])), 0, MAX_BORDER)
    current["radius"] = clamp(int(current.get("radius", defaults["radius"])), 0, MAX_RADIUS)


def normalize_menu_state(current, defaults):
    current["item_total"] = clamp(int(current.get("item_total", defaults["item_total"])), 1, len(SAMPLE_ITEMS))
    current["visible_rows"] = clamp(int(current.get("visible_rows", defaults["visible_rows"])), 1, MAX_VISIBLE_ROWS)
    current["selected_index"] = clamp(int(current.get("selected_index", defaults["selected_index"])), 0, current["item_total"] - 1)
    current["row_gap"] = clamp(int(current.get("row_gap", defaults["row_gap"])), 0, 4)
    current["row_height"] = clamp(int(current.get("row_height", defaults["row_height"])), 5, 14)
    current["show_scrollbar"] = bool(current.get("show_scrollbar", defaults["show_scrollbar"]))
    current["show_footer"] = bool(current.get("show_footer", defaults["show_footer"]))


def normalize_object_values(values, object_name):
    defaults = OBJECT_DEFAULTS[object_name]
    current = get_object_values(values, object_name)

    if object_name == "menu_panel":
        normalize_geometry(current, defaults, "x", "y")
        current["filled"] = bool(current.get("filled", defaults["filled"]))
        current["border_width"] = clamp(int(current.get("border_width", defaults["border_width"])), 0, MAX_BORDER)
        current["radius"] = clamp(int(current.get("radius", defaults["radius"])), 0, MAX_RADIUS)
    elif object_name in ("header_box", "list_box", "footer_box"):
        normalize_geometry(current, defaults, "rel_x", "rel_y")
        current["filled"] = bool(current.get("filled", defaults["filled"]))
        current["border_width"] = clamp(int(current.get("border_width", defaults["border_width"])), 0, MAX_BORDER)
        current["radius"] = clamp(int(current.get("radius", defaults["radius"])), 0, MAX_RADIUS)
    elif object_name in ("header_label", "footer_label", "row_text", "selected_row_text"):
        normalize_label_values(current, defaults)
    elif object_name in ("row_box", "selected_row_box"):
        normalize_row_style(current, defaults)
    elif object_name == "scrollbar_track":
        normalize_scrollbar_track(current, defaults)
    elif object_name == "scrollbar_thumb":
        normalize_scrollbar_thumb(current, defaults)
    elif object_name == "menu_state":
        normalize_menu_state(current, defaults)

    return current


def normalize_all_object_values(values):
    for spec in OBJECT_SPECS:
        normalize_object_values(values, spec["name"])


def compute_runtime_state(values):
    normalize_all_object_values(values)
    menu_state = get_object_values(values, "menu_state")
    list_box = get_object_values(values, "list_box")

    total = menu_state["item_total"]
    requested_rows = menu_state["visible_rows"]
    row_gap = menu_state["row_gap"]
    row_height = menu_state["row_height"]

    usable_height = max(1, list_box["height"] - 2)
    slot_height = row_height + row_gap
    if slot_height <= 0:
        fit_rows = 1
    else:
        fit_rows = max(1, (usable_height + row_gap) // slot_height)

    visible_rows = min(total, requested_rows, MAX_VISIBLE_ROWS, fit_rows)
    selected_index = clamp(menu_state["selected_index"], 0, total - 1)
    max_top = max(0, total - visible_rows)
    top_index = selected_index - visible_rows + 1
    if top_index < 0:
        top_index = 0
    if top_index > max_top:
        top_index = max_top

    return {
        "total": total,
        "visible_rows": visible_rows,
        "selected_index": selected_index,
        "top_index": top_index,
        "row_gap": row_gap,
        "row_height": row_height,
    }


def compute_row_frame(list_box_values, row_style, runtime_state, slot_index):
    y = row_style["inset_top"] + slot_index * (runtime_state["row_height"] + runtime_state["row_gap"])
    height = max(2, runtime_state["row_height"] + row_style["height_adjust"])
    width = max(1, list_box_values["width"] - row_style["inset_left"] - row_style["inset_right"])
    if y >= list_box_values["height"]:
        return None
    if y + height > list_box_values["height"]:
        height = max(1, list_box_values["height"] - y)
    return {
        "x": row_style["inset_left"],
        "y": y,
        "width": width,
        "height": height,
    }


def build_demo_screen(values):
    scr = lv.obj()
    scr.set_size(WIDTH, HEIGHT)
    prepare_box(scr)
    scr.set_style_bg_opa(lv.OPA.COVER, 0)
    scr.set_style_bg_color(lv.color_white(), 0)
    scr.set_style_border_width(0, 0)

    menu_panel = lv.obj(scr)
    prepare_box(menu_panel)

    header_box = lv.obj(menu_panel)
    prepare_box(header_box)
    header_label = lv.label(header_box)
    prepare_label(header_label)

    list_box = lv.obj(menu_panel)
    prepare_box(list_box)

    row_boxes = []
    row_labels = []
    for _index in range(MAX_VISIBLE_ROWS):
        row = lv.obj(list_box)
        prepare_box(row)
        label = lv.label(row)
        prepare_label(label)
        row_boxes.append(row)
        row_labels.append(label)

    scrollbar_track = lv.obj(list_box)
    prepare_box(scrollbar_track)
    scrollbar_thumb = lv.obj(scrollbar_track)
    prepare_box(scrollbar_thumb)

    footer_box = lv.obj(menu_panel)
    prepare_box(footer_box)
    footer_label = lv.label(footer_box)
    prepare_label(footer_label)

    ui = {
        "screen": scr,
        "menu_panel": menu_panel,
        "header_box": header_box,
        "header_label": header_label,
        "list_box": list_box,
        "row_boxes": row_boxes,
        "row_labels": row_labels,
        "scrollbar_track": scrollbar_track,
        "scrollbar_thumb": scrollbar_thumb,
        "footer_box": footer_box,
        "footer_label": footer_label,
    }
    refresh_demo(ui, values)
    return ui


def refresh_demo(ui, values):
    normalize_all_object_values(values)

    menu_panel_cfg = get_object_values(values, "menu_panel")
    header_box_cfg = get_object_values(values, "header_box")
    header_label_cfg = get_object_values(values, "header_label")
    list_box_cfg = get_object_values(values, "list_box")
    row_box_cfg = get_object_values(values, "row_box")
    row_text_cfg = get_object_values(values, "row_text")
    selected_row_box_cfg = get_object_values(values, "selected_row_box")
    selected_row_text_cfg = get_object_values(values, "selected_row_text")
    track_cfg = get_object_values(values, "scrollbar_track")
    thumb_cfg = get_object_values(values, "scrollbar_thumb")
    footer_box_cfg = get_object_values(values, "footer_box")
    footer_label_cfg = get_object_values(values, "footer_label")
    menu_state_cfg = get_object_values(values, "menu_state")
    runtime = compute_runtime_state(values)

    ui["menu_panel"].set_pos(menu_panel_cfg["x"], menu_panel_cfg["y"])
    ui["menu_panel"].set_size(menu_panel_cfg["width"], menu_panel_cfg["height"])
    apply_box_style(ui["menu_panel"], menu_panel_cfg)

    ui["header_box"].set_pos(header_box_cfg["rel_x"], header_box_cfg["rel_y"])
    ui["header_box"].set_size(header_box_cfg["width"], header_box_cfg["height"])
    apply_box_style(ui["header_box"], header_box_cfg)
    ui["header_label"].set_text(header_label_cfg["text"] if header_label_cfg["text"] else " ")
    apply_label_style(ui["header_label"], header_label_cfg, header_box_cfg["width"])

    ui["list_box"].set_pos(list_box_cfg["rel_x"], list_box_cfg["rel_y"])
    ui["list_box"].set_size(list_box_cfg["width"], list_box_cfg["height"])
    apply_box_style(ui["list_box"], list_box_cfg)

    for slot_index in range(MAX_VISIBLE_ROWS):
        row_obj = ui["row_boxes"][slot_index]
        label_obj = ui["row_labels"][slot_index]
        item_index = runtime["top_index"] + slot_index

        if slot_index >= runtime["visible_rows"] or item_index >= runtime["total"]:
            set_obj_hidden(row_obj, True)
            continue

        set_obj_hidden(row_obj, False)
        selected = item_index == runtime["selected_index"]
        active_box_cfg = selected_row_box_cfg if selected else row_box_cfg
        active_text_cfg = selected_row_text_cfg if selected else row_text_cfg
        frame = compute_row_frame(list_box_cfg, active_box_cfg, runtime, slot_index)
        if frame is None:
            set_obj_hidden(row_obj, True)
            continue

        row_obj.set_pos(frame["x"], frame["y"])
        row_obj.set_size(frame["width"], frame["height"])
        apply_box_style(row_obj, active_box_cfg)

        label_obj.set_text(SAMPLE_ITEMS[item_index])
        apply_label_style(label_obj, active_text_cfg, frame["width"])

    show_scrollbar = menu_state_cfg["show_scrollbar"] and runtime["total"] > runtime["visible_rows"]
    set_obj_hidden(ui["scrollbar_track"], not show_scrollbar)
    set_obj_hidden(ui["scrollbar_thumb"], not show_scrollbar)
    if show_scrollbar:
        track_width = min(track_cfg["width"], list_box_cfg["width"])
        track_height = max(1, list_box_cfg["height"] - track_cfg["inset_top"] - track_cfg["inset_bottom"])
        track_x = max(0, list_box_cfg["width"] - track_cfg["inset_right"] - track_width)
        track_y = track_cfg["inset_top"]

        ui["scrollbar_track"].set_pos(track_x, track_y)
        ui["scrollbar_track"].set_size(track_width, track_height)
        apply_box_style(ui["scrollbar_track"], track_cfg)

        thumb_width = min(thumb_cfg["width"], track_width)
        thumb_height = max(thumb_cfg["min_height"], (track_height * runtime["visible_rows"]) // runtime["total"])
        thumb_height = min(track_height, thumb_height)
        thumb_x = max(0, (track_width - thumb_width) // 2)
        max_thumb_y = track_height - thumb_height
        max_top = runtime["total"] - runtime["visible_rows"]
        thumb_y = 0
        if max_top > 0 and max_thumb_y > 0:
            thumb_y = (max_thumb_y * runtime["top_index"]) // max_top

        ui["scrollbar_thumb"].set_pos(thumb_x, thumb_y)
        ui["scrollbar_thumb"].set_size(thumb_width, thumb_height)
        apply_box_style(ui["scrollbar_thumb"], thumb_cfg)

    show_footer = menu_state_cfg["show_footer"]
    set_obj_hidden(ui["footer_box"], not show_footer)
    if show_footer:
        ui["footer_box"].set_pos(footer_box_cfg["rel_x"], footer_box_cfg["rel_y"])
        ui["footer_box"].set_size(footer_box_cfg["width"], footer_box_cfg["height"])
        apply_box_style(ui["footer_box"], footer_box_cfg)
        ui["footer_label"].set_text(footer_label_cfg["text"] if footer_label_cfg["text"] else " ")
        apply_label_style(ui["footer_label"], footer_label_cfg, footer_box_cfg["width"])


def format_object_summary(values, object_name):
    param_names = get_object_params(object_name)
    current = get_object_values(values, object_name)
    parts = []
    for name in param_names[:4]:
        parts.append("{}={}".format(name, format_value(current.get(name))))
    if len(param_names) > 4:
        parts.append("...")
    summary = "{}: {}".format(object_name, " ".join(parts))
    if len(summary) > 52:
        summary = summary[:49] + "..."
    return summary


def format_object_params(values, object_name):
    current = get_object_values(values, object_name)
    parts = []
    for name in get_object_params(object_name):
        parts.append("{}={}".format(name, format_value(current.get(name))))
    if not parts:
        return "-"
    return ",".join(parts)


def print_help():
    print("SCROLL_MENU_KEYS")
    print("home       : object list")
    print("tab/nav_d  : next field")
    print("nav_u      : previous field")
    print("nav_l/-    : decrease value")
    print("nav_r/+    : increase value")
    print("ok         : manual edit / save")
    print("exe        : next object")
    print("back       : reset field / backspace")
    print("AC         : reset current object")
    print("alpha/beta : text entry layer")
    print("module     : print current object")
    print("toolbox    : print full config")
    print("on         : exit")


def print_object_registry(values, selected_object_index):
    print("OBJECT_MAP")
    for index, spec in enumerate(OBJECT_SPECS, 1):
        selected = "*" if (index - 1) == selected_object_index else " "
        print("{} {}. {}".format(selected, index, format_object_summary(values, spec["name"])))


def print_selected_object_preview(values, selected_object_index):
    object_name = get_object_spec(selected_object_index)["name"]
    print("* {}. {} params={}".format(selected_object_index + 1, object_name, format_object_params(values, object_name)))


def print_object_param_map(values, object_name, selected_param_index, manual_mode=False, manual_buffer=""):
    param_names = get_object_params(object_name)
    if not param_names:
        print("OBJECT_PARAMS")
        print("selected_object =", object_name)
        print("no parameters")
        return

    selected_param_index = clamp(selected_param_index, 0, len(param_names) - 1)
    current = get_object_values(values, object_name)
    print("OBJECT_PARAMS")
    print("selected_object =", object_name)
    for index, param_name in enumerate(param_names, 1):
        marker = "*" if (index - 1) == selected_param_index else " "
        print("{} {}. {} = {}".format(marker, index, param_name, format_value(current.get(param_name))))
    if manual_mode:
        current_param = param_names[selected_param_index]
        print("EDIT {} buffer={} current={}".format(current_param, manual_buffer, format_value(current.get(current_param))))


def format_object_export(values, object_name):
    current = get_object_values(values, object_name)
    parts = []
    for param_name in get_object_params(object_name):
        parts.append("{!r}: {!r}".format(param_name, current.get(param_name)))
    return "{" + ", ".join(parts) + "}"


def print_current_object(values, object_name):
    print("OBJECT_CONFIG")
    print("selected_object =", object_name)
    for param_name in get_object_params(object_name):
        print("{}.{} = {!r}".format(object_name, param_name, get_object_values(values, object_name).get(param_name)))


def print_runtime_state(values):
    runtime = compute_runtime_state(values)
    print("RUNTIME_STATE")
    print("total_items =", runtime["total"])
    print("visible_rows =", runtime["visible_rows"])
    print("top_index =", runtime["top_index"])
    print("selected_index =", runtime["selected_index"])


def print_full_config(values):
    print("SCROLL_MENU_CONFIG")
    runtime = compute_runtime_state(values)
    print("runtime_top_index =", runtime["top_index"])
    print("runtime_visible_rows =", runtime["visible_rows"])
    print("SCROLL_MENU_DEFAULTS = {")
    for spec in OBJECT_SPECS:
        print("    {!r}: {},".format(spec["name"], format_object_export(values, spec["name"])))
    print("}")


def handle_text_key(values, object_name, key):
    token = token_from_key(key)
    if token is None:
        return False
    current = get_object_values(values, object_name)
    current["text"] = str(current.get("text", "")) + token
    return True


def adjust_object_parameter(values, object_name, param_spec, direction):
    current = get_object_values(values, object_name)
    name = param_spec["name"]
    kind = param_spec["kind"]

    if kind == "bool":
        current[name] = not bool(current.get(name, False))
        normalize_object_values(values, object_name)
        return True

    if kind == "int":
        current[name] = clamp(
            int(current.get(name, 0)) + direction,
            param_spec["min"],
            param_spec["max"],
        )
        normalize_object_values(values, object_name)
        return True

    if kind == "enum":
        current[name] = cycle_enum(current.get(name, param_spec["values"][0]), param_spec["values"], direction)
        normalize_object_values(values, object_name)
        return True

    return False


def commit_object_manual_value(values, object_name, param_spec, manual_buffer):
    raw = manual_buffer.strip()
    if raw in ("", "-", "+"):
        return False

    current = get_object_values(values, object_name)
    name = param_spec["name"]
    kind = param_spec["kind"]

    if kind == "int":
        current[name] = clamp(int(raw), param_spec["min"], param_spec["max"])
        normalize_object_values(values, object_name)
        return True

    if kind == "bool":
        try:
            current[name] = bool(int(raw))
        except ValueError:
            return False
        normalize_object_values(values, object_name)
        return True

    return False


def reset_object_parameter(values, object_name, param_name):
    defaults = build_object_defaults()
    get_object_values(values, object_name)[param_name] = defaults[object_name][param_name]
    normalize_object_values(values, object_name)


def reset_object(values, object_name):
    defaults = build_object_defaults()
    values[object_name] = dict(defaults[object_name])
    normalize_object_values(values, object_name)


def append_manual_char(param_spec, manual_buffer, key):
    if key in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
        return manual_buffer + key
    if param_spec["kind"] == "int" and key == "-" and manual_buffer == "":
        return "-"
    if param_spec["kind"] == "int" and key == "+" and manual_buffer == "":
        return ""
    return manual_buffer


def handle_object_browser_key_event(selected_object_index, key):
    should_print = False
    should_exit = False
    editor_requested = False

    if key == "on":
        should_exit = True
        return selected_object_index, editor_requested, should_print, should_exit

    if key == "nav_u":
        selected_object_index = (selected_object_index - 1) % len(OBJECT_SPECS)
        should_print = True
        return selected_object_index, editor_requested, should_print, should_exit

    if key == "nav_d":
        selected_object_index = (selected_object_index + 1) % len(OBJECT_SPECS)
        should_print = True
        return selected_object_index, editor_requested, should_print, should_exit

    if key == "ok":
        editor_requested = True
        should_print = True
        return selected_object_index, editor_requested, should_print, should_exit

    if key == "exe":
        should_print = True
        return selected_object_index, editor_requested, should_print, should_exit

    return selected_object_index, editor_requested, should_print, should_exit


def handle_object_editor_key_event(values, selected_object_index, selected_param_index, manual_mode, manual_buffer, keypad, key):
    changed = False
    should_print = False
    should_exit = False
    browser_requested = False

    object_name = get_object_spec(selected_object_index)["name"]
    param_names = get_object_params(object_name)
    param_spec = None
    if param_names:
        selected_param_index = clamp(selected_param_index, 0, len(param_names) - 1)
        param_spec = PARAMETER_BY_NAME[param_names[selected_param_index]]

    if key == "on":
        should_exit = True
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if key == "home":
        browser_requested = True
        should_print = True
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if key == "exe":
        selected_object_index = (selected_object_index + 1) % len(OBJECT_SPECS)
        selected_param_index = 0
        manual_mode = False
        manual_buffer = ""
        should_print = True
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if key == "alpha":
        keypad.toggle_layer("a")
        should_print = True
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if key == "beta":
        keypad.toggle_layer("b")
        should_print = True
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if manual_mode:
        if key == "ok":
            if param_spec is not None:
                changed = commit_object_manual_value(values, object_name, param_spec, manual_buffer)
            manual_mode = False
            manual_buffer = ""
            should_print = True
            return (
                selected_object_index,
                selected_param_index,
                manual_mode,
                manual_buffer,
                changed,
                should_print,
                should_exit,
                browser_requested,
            )

        if key == "back":
            if manual_buffer:
                manual_buffer = manual_buffer[:-1]
            else:
                manual_mode = False
            should_print = True
            return (
                selected_object_index,
                selected_param_index,
                manual_mode,
                manual_buffer,
                changed,
                should_print,
                should_exit,
                browser_requested,
            )

        if key == "AC":
            reset_object(values, object_name)
            manual_mode = False
            manual_buffer = ""
            changed = True
            should_print = True
            return (
                selected_object_index,
                selected_param_index,
                manual_mode,
                manual_buffer,
                changed,
                should_print,
                should_exit,
                browser_requested,
            )

        if param_spec is not None:
            updated = append_manual_char(param_spec, manual_buffer, key)
            if updated != manual_buffer:
                manual_buffer = updated
                should_print = True

        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if key == "ok":
        if param_spec is not None and param_spec["kind"] in ("int", "bool"):
            manual_mode = True
            manual_buffer = ""
            should_print = True
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if param_spec is not None and param_spec["kind"] == "text":
        if key == "back":
            current = get_object_values(values, object_name)
            current["text"] = str(current.get("text", ""))[:-1]
            normalize_object_values(values, object_name)
            changed = True
            should_print = True
        elif handle_text_key(values, object_name, key):
            normalize_object_values(values, object_name)
            changed = True
            should_print = True
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if key in ("tab", "nav_d"):
        if param_names:
            selected_param_index = (selected_param_index + 1) % len(param_names)
            should_print = True
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if key == "nav_u":
        if param_names:
            selected_param_index = (selected_param_index - 1) % len(param_names)
            should_print = True
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if key in ("nav_l", "-"):
        if param_spec is not None:
            changed = adjust_object_parameter(values, object_name, param_spec, -1)
        should_print = changed
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if key in ("nav_r", "+", "nav_b"):
        if param_spec is not None:
            changed = adjust_object_parameter(values, object_name, param_spec, 1)
        should_print = changed
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if key == "back":
        if param_spec is not None:
            reset_object_parameter(values, object_name, param_spec["name"])
            changed = True
            should_print = True
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    if key == "AC":
        reset_object(values, object_name)
        changed = True
        should_print = True
        return (
            selected_object_index,
            selected_param_index,
            manual_mode,
            manual_buffer,
            changed,
            should_print,
            should_exit,
            browser_requested,
        )

    return (
        selected_object_index,
        selected_param_index,
        manual_mode,
        manual_buffer,
        changed,
        should_print,
        should_exit,
        browser_requested,
    )


def main():
    keypad_blocked_here = None
    try:
        ensure_backlight_on()
        keypad_blocked_here = acquire_calsci_keypad()
        ensure_st7565_ready()
        build_lvgl_display()

        values = build_object_defaults()
        normalize_all_object_values(values)
        keypad = MatrixKeypad(ROW_PINS, COL_PINS, KEYMAP_LAYERS)
        ui = build_demo_screen(values)
        lv.screen_load(ui["screen"])

        selected_object_index, selected_param_index = get_default_selection()
        manual_mode = False
        manual_buffer = ""
        mode = "editor"

        print_help()
        print_object_param_map(values, get_object_spec(selected_object_index)["name"], selected_param_index, manual_mode, manual_buffer)
        print_runtime_state(values)

        exit_requested = False

        while not exit_requested:
            keypad.tick()

            key = keypad.pop_event()
            while key is not None:
                if key == "toolbox":
                    print_full_config(values)
                    key = keypad.pop_event()
                    continue

                if key == "module":
                    print_current_object(values, get_object_spec(selected_object_index)["name"])
                    print_runtime_state(values)
                    key = keypad.pop_event()
                    continue

                if mode == "browser":
                    (
                        selected_object_index,
                        editor_requested,
                        should_print,
                        exit_requested,
                    ) = handle_object_browser_key_event(selected_object_index, key)

                    if editor_requested:
                        mode = "editor"
                        selected_param_index = 0
                        manual_mode = False
                        manual_buffer = ""
                        print_object_param_map(
                            values,
                            get_object_spec(selected_object_index)["name"],
                            selected_param_index,
                            manual_mode,
                            manual_buffer,
                        )
                    elif should_print:
                        print_object_registry(values, selected_object_index)
                        print_selected_object_preview(values, selected_object_index)
                else:
                    (
                        selected_object_index,
                        selected_param_index,
                        manual_mode,
                        manual_buffer,
                        changed,
                        should_print,
                        exit_requested,
                        browser_requested,
                    ) = handle_object_editor_key_event(
                        values,
                        selected_object_index,
                        selected_param_index,
                        manual_mode,
                        manual_buffer,
                        keypad,
                        key,
                    )

                    if changed:
                        refresh_demo(ui, values)

                    if browser_requested:
                        mode = "browser"
                        print_object_registry(values, selected_object_index)
                        print_selected_object_preview(values, selected_object_index)
                    elif should_print:
                        print_object_param_map(
                            values,
                            get_object_spec(selected_object_index)["name"],
                            selected_param_index,
                            manual_mode,
                            manual_buffer,
                        )

                key = keypad.pop_event()

            lv.tick_inc(LOOP_DELAY_MS)
            lv.timer_handler()
            time.sleep_ms(LOOP_DELAY_MS)

        print("SCROLL_MENU_EDITOR_EXIT")
    finally:
        release_calsci_keypad(keypad_blocked_here)


main()
