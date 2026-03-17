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
MENU_WIDTH = 56
MENU_HEADER_HEIGHT = 10

MENU_ITEM_HEIGHT = 8
MENU_GAP = 2
PREVIEW_MARGIN = 2
STATUS_BAR_HEIGHT = 18
BACKLIGHT_PIN_29 = 5
BACKLIGHT_PIN_30 = 19
BACKLIGHT_PWM_FREQ = 1000
BACKLIGHT_DIM_PERCENT = 5

_BACKLIGHT_PWM = None

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
STEP_VALUES = (1, 2, 5, 10)
TEXT_ALIGN_VALUES = ("left", "center", "right")
REPEAT_KEYS = {"nav_u", "nav_d", "nav_l", "nav_r", "+", "-"}
DISPLAY_STATE = {}

ELEMENT_CONFIG = {
    "x": PREVIEW_MARGIN,
    "y": PREVIEW_MARGIN,
    "width": WIDTH - (PREVIEW_MARGIN * 2),
    "height": HEIGHT - (PREVIEW_MARGIN * 2),
    "filled": False,
    "border_width": 1,
    "radius": 0,
}

PARAMETERS = (
    {"name": "x", "kind": "int", "min": 0, "max": WIDTH - 1},
    {"name": "y", "kind": "int", "min": 0, "max": HEIGHT - 1},
    {"name": "width", "kind": "int", "min": 1, "max": WIDTH},
    {"name": "height", "kind": "int", "min": 1, "max": HEIGHT},
    {"name": "text", "kind": "text"},
    {"name": "filled", "kind": "bool"},
    {"name": "border_width", "kind": "int", "min": 0, "max": 6},
    {"name": "radius", "kind": "int", "min": 0, "max": 16},
    {"name": "text_align", "kind": "enum", "values": TEXT_ALIGN_VALUES},
    {"name": "text_offset_x", "kind": "int", "min": -30, "max": 30},
    {"name": "text_offset_y", "kind": "int", "min": -20, "max": 20},
    {"name": "padding_left", "kind": "int", "min": 0, "max": 20},
    {"name": "padding_right", "kind": "int", "min": 0, "max": 20},
)

DEFAULT_CONFIG = {}

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

OBJECT_SPECS = (
    {
        "name": "screen",
        "type": "container",
        "params": (),
    },
    {
        "name": "menu",
        "type": "container",
        "params": ("x", "y", "width", "height"),
    },
    {
        "name": "menu_header",
        "type": "label",
        "params": ("x", "y", "text"),
    },
    {
        "name": "menu_row_1",
        "type": "label",
        "params": ("x", "y", "width", "text"),
    },
    {
        "name": "menu_row_2",
        "type": "label",
        "params": ("x", "y", "width", "text"),
    },
    {
        "name": "menu_row_3",
        "type": "label",
        "params": ("x", "y", "width", "text"),
    },
    {
        "name": "preview",
        "type": "container",
        "params": ("x", "y", "width", "height"),
    },
    {
        "name": "element",
        "type": "container",
        "params": ("x", "y", "width", "height", "filled", "border_width", "radius"),
    },
    {
        "name": "element_label",
        "type": "label",
        "params": ("text", "text_align", "text_offset_x", "text_offset_y", "padding_left", "padding_right"),
    },
)

OBJECT_INDEX_BY_NAME = {spec["name"]: index for index, spec in enumerate(OBJECT_SPECS, 1)}
PARAMETER_BY_NAME = {spec["name"]: spec for spec in PARAMETERS}
GEOMETRY_PARAMS = ("x", "y", "width", "height")
STYLE_PARAMS = ("filled", "border_width", "radius")


def compute_layout():
    menu_x = PREVIEW_MARGIN
    menu_y = PREVIEW_MARGIN
    menu_w = WIDTH - (PREVIEW_MARGIN * 2)
    menu_h = HEIGHT - (PREVIEW_MARGIN * 2)
    preview_x = menu_x
    preview_y = menu_y
    preview_w = menu_w
    preview_h = menu_h
    return {
        "menu_x": menu_x,
        "menu_y": menu_y,
        "menu_w": menu_w,
        "menu_h": menu_h,
        "preview_x": preview_x,
        "preview_y": preview_y,
        "preview_w": preview_w,
        "preview_h": preview_h,
    }


def build_default_values():
    values = {}
    for spec in OBJECT_SPECS:
        name = spec["name"]
        if name == "screen":
            values[name] = {}
        elif name == "menu":
            values[name] = {
                "x": PREVIEW_MARGIN,
                "y": PREVIEW_MARGIN,
                "width": WIDTH - (PREVIEW_MARGIN * 2),
                "height": HEIGHT - (PREVIEW_MARGIN * 2),
            }
        elif name == "menu_header":
            values[name] = {"x": 2, "y": 0, "text": "UI TUNER"}
        elif name == "menu_row_1":
            values[name] = {"x": 2, "y": 10, "width": 48, "text": "x"}
        elif name == "menu_row_2":
            values[name] = {"x": 2, "y": 18, "width": 48, "text": "y"}
        elif name == "menu_row_3":
            values[name] = {"x": 2, "y": 26, "width": 48, "text": "width"}
        elif name == "preview":
            values[name] = {
                "x": PREVIEW_MARGIN,
                "y": PREVIEW_MARGIN,
                "width": WIDTH - (PREVIEW_MARGIN * 2),
                "height": HEIGHT - (PREVIEW_MARGIN * 2),
            }
        elif name == "element":
            values[name] = dict(ELEMENT_CONFIG)
        elif name == "element_label":
            values[name] = {
                "text": "FIRST ELEMENT",
                "text_align": "center",
                "text_offset_x": 0,
                "text_offset_y": 0,
                "padding_left": 4,
                "padding_right": 4,
            }
        else:
            values[name] = {}
    return values


def make_object_state():
    return {
        "screen": None,
        "menu": None,
        "menu_header": None,
        "menu_rows": [],
        "preview": None,
        "element": None,
        "element_label": None,
    }


def build_object_registry():
    return [
        {
            "name": "screen",
            "obj": "screen",
            "params": {},
        },
        {
            "name": "menu",
            "obj": "menu",
            "params": {"x": 2, "y": 2, "width": MENU_WIDTH, "height": HEIGHT - 4},
        },
        {
            "name": "menu_header",
            "obj": "menu_header",
            "params": {"x": 2, "y": 0, "text": "PARAMETERS"},
        },
        {
            "name": "menu_row_1",
            "obj": "menu_rows",
            "index": 0,
            "params": {"x": 2, "y": MENU_HEADER_HEIGHT + 0 * MENU_ITEM_HEIGHT, "width": MENU_WIDTH - 8, "text": "x"},
        },
        {
            "name": "menu_row_2",
            "obj": "menu_rows",
            "index": 1,
            "params": {"x": 2, "y": MENU_HEADER_HEIGHT + 1 * MENU_ITEM_HEIGHT, "width": MENU_WIDTH - 8, "text": "y"},
        },
        {
            "name": "menu_row_3",
            "obj": "menu_rows",
            "index": 2,
            "params": {"x": 2, "y": MENU_HEADER_HEIGHT + 2 * MENU_ITEM_HEIGHT, "width": MENU_WIDTH - 8, "text": "width"},
        },
        {
            "name": "preview",
            "obj": "preview",
            "params": {"x": MENU_WIDTH + MENU_GAP + 2, "y": 2, "width": max(1, WIDTH - (MENU_WIDTH + MENU_GAP + 4)), "height": HEIGHT - 4},
        },
        {
            "name": "element",
            "obj": "element",
            "params": {
                "x": ELEMENT_CONFIG["x"],
                "y": ELEMENT_CONFIG["y"],
                "width": ELEMENT_CONFIG["width"],
                "height": ELEMENT_CONFIG["height"],
                "filled": ELEMENT_CONFIG["filled"],
                "border_width": ELEMENT_CONFIG["border_width"],
                "radius": ELEMENT_CONFIG["radius"],
            },
        },
        {
            "name": "element_label",
            "obj": "element_label",
            "params": {"text": ELEMENT_CONFIG["text"]},
        },
    ]


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


def apply_element_style(obj, filled, border_width, radius):
    bg = lv.color_black() if filled else lv.color_white()
    obj.set_style_bg_opa(lv.OPA.COVER, 0)
    obj.set_style_bg_color(bg, 0)
    obj.set_style_border_width(border_width, 0)
    obj.set_style_border_color(lv.color_black(), 0)
    obj.set_style_radius(radius, 0)


def align_label(label, config):
    align_mode = config["text_align"]
    offset_x = config["text_offset_x"]
    offset_y = config["text_offset_y"]

    if align_mode == "left":
        label.align(lv.ALIGN.LEFT_MID, config["padding_left"] + offset_x, offset_y)
    elif align_mode == "right":
        label.align(lv.ALIGN.RIGHT_MID, -config["padding_right"] + offset_x, offset_y)
    else:
        label.align(lv.ALIGN.CENTER, offset_x, offset_y)


def format_value(value):
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    return str(value)


def format_object_summary(values, object_name):
    names = get_object_params(object_name)
    current = get_object_values(values, object_name)
    parts = []
    for name in names[:4]:
        parts.append("{}={}".format(name, format_value(current.get(name))))
    if len(names) > 4:
        parts.append("...")
    summary = "{}: {}".format(object_name, " ".join(parts))
    if len(summary) > 42:
        summary = summary[:39] + "..."
    return summary


def build_demo_screen(values):
    scr = lv.obj()
    scr.set_size(WIDTH, HEIGHT)
    clear_obj_flag(scr, lv.obj.FLAG.SCROLLABLE)
    scr.set_style_bg_opa(lv.OPA.COVER, 0)
    scr.set_style_bg_color(lv.color_white(), 0)

    status = lv.obj(scr)
    status.set_pos(0, 0)
    status.set_size(WIDTH, STATUS_BAR_HEIGHT)
    status.set_style_bg_opa(lv.OPA.COVER, 0)
    status.set_style_bg_color(lv.color_black(), 0)
    status.set_style_border_width(0, 0)

    field_label = lv.label(status)
    field_label.set_pos(2, 1)
    field_label.set_style_text_color(lv.color_white(), 0)

    value_label = lv.label(status)
    value_label.set_pos(2, 9)
    value_label.set_style_text_color(lv.color_white(), 0)

    detail_label = lv.label(status)
    detail_label.set_pos(2, 17)
    detail_label.set_style_text_color(lv.color_white(), 0)

    element_values = get_object_values(values, "element")
    element_label_values = get_object_values(values, "element_label")

    element = lv.obj(scr)
    clear_obj_flag(element, lv.obj.FLAG.SCROLLABLE)

    label = lv.label(element)

    ui = {
        "screen": scr,
        "status": status,
        "field_label": field_label,
        "value_label": value_label,
        "detail_label": detail_label,
        "element": element,
        "label": label,
    }
    selected_object_index, selected_param_index = get_default_selection()
    refresh_demo(ui, values, selected_object_index, selected_param_index)
    return ui


def refresh_demo(ui, values, selected_object_index, selected_param_index):
    object_spec = get_object_spec(selected_object_index)
    object_name = object_spec["name"]
    param_names = object_spec["params"]
    selected_values = get_object_values(values, object_name)

    if param_names:
        selected_param_index = clamp(selected_param_index, 0, len(param_names) - 1)
        param_name = param_names[selected_param_index]
        selected_value = selected_values.get(param_name)
        value_text = "{}: {}".format(param_name, format_value(selected_value))
    else:
        value_text = "no parameters"

    ui["field_label"].set_text(
        "{}/{}  {}".format(selected_object_index + 1, len(OBJECT_SPECS), object_name)
    )
    ui["value_label"].set_text(value_text)
    ui["detail_label"].set_text(format_object_summary(values, object_name))

    element_values = get_object_values(values, "element")
    ui["element"].set_size(element_values["width"], element_values["height"])
    ui["element"].align(lv.ALIGN.TOP_LEFT, element_values["x"], element_values["y"])
    apply_element_style(
        ui["element"],
        element_values["filled"],
        element_values["border_width"],
        element_values["radius"],
    )

    element_label_values = get_object_values(values, "element_label")
    ui["label"].set_text(element_label_values["text"] if element_label_values["text"] else " ")
    ui["label"].set_style_text_color(
        lv.color_white() if element_values["filled"] else lv.color_black(),
        0,
    )
    align_label(ui["label"], element_label_values)


def print_help():
    print("EDITOR_KEYS")
    print("1..4        : select object")
    print("tab         : next parameter")
    print("nav_u/nav_d : object up/down")
    print("nav_l/nav_r : value change")
    print("+/-         : value change")
    print("ok          : step size")
    print("exe         : print current object")
    print("back        : delete text or reset field")
    print("AC          : reset all")
    print("alpha/beta  : toggle text layer")
    print("on          : exit")


def get_object_params(object_name):
    for spec in OBJECT_SPECS:
        if spec["name"] == object_name:
            return spec["params"]
    return ()


def get_object_spec(selected_object_index):
    return OBJECT_SPECS[selected_object_index]


def get_default_selection():
    object_index = OBJECT_INDEX_BY_NAME["element"] - 1
    param_names = get_object_params("element")
    try:
        param_index = param_names.index("width")
    except ValueError:
        param_index = 0
    return object_index, param_index


def get_object_values(values, object_name):
    obj = values.get(object_name)
    if obj is None:
        obj = {}
        values[object_name] = obj
    return obj


def format_object_params(values, object_name):
    param_names = get_object_params(object_name)
    if not param_names:
        return "-"
    current_values = get_object_values(values, object_name)
    parts = []
    for name in param_names:
        parts.append("{}={}".format(name, format_value(current_values.get(name))))
    return ",".join(parts)


def print_object_registry(values, selected_object_index):
    print("OBJECT_MAP")
    for index, spec in enumerate(OBJECT_SPECS, 1):
        selected = "*" if (index - 1) == selected_object_index else " "
        params = format_object_params(values, spec["name"])
        print("{} {}. {} params={}".format(selected, index, spec["name"], params))


def print_selected_object_preview(values, selected_object_index):
    spec = get_object_spec(selected_object_index)
    params = format_object_params(values, spec["name"])
    print("{} {}. {} params={}".format("*", selected_object_index + 1, spec["name"], params))


def print_editor_state(values, selected_object_index, selected_param_index, step, layer):
    object_spec = get_object_spec(selected_object_index)
    object_name = object_spec["name"]
    param_names = object_spec["params"]
    if param_names:
        selected_param_index = clamp(selected_param_index, 0, len(param_names) - 1)
        param_name = param_names[selected_param_index]
    else:
        param_name = "-"
    value = get_object_values(values, object_name).get(param_name, None) if param_names else None
    print(
        "EDITOR {}. {} param={} step={} layer={} value={}".format(
            selected_object_index + 1,
            object_name,
            param_name,
            step,
            LAYER_NAMES[layer],
            format_value(value),
        )
    )


def print_config(values, selected_object_index, selected_param_index, step, layer):
    object_spec = get_object_spec(selected_object_index)
    object_name = object_spec["name"]
    param_names = object_spec["params"]
    if param_names:
        selected_param_index = clamp(selected_param_index, 0, len(param_names) - 1)
        param_name = param_names[selected_param_index]
    else:
        param_name = "-"
    layout = compute_layout()
    current_values = get_object_values(values, object_name)
    print("EDITOR_STATE")
    print("selected =", object_name)
    print("selected_param =", param_name)
    print("step =", step)
    print("layer =", LAYER_NAMES[layer])
    print("menu_w =", layout["menu_w"])
    print("menu_h =", layout["menu_h"])
    print("preview_w =", layout["preview_w"])
    print("preview_h =", layout["preview_h"])
    for name in get_object_params(object_name):
        print("{}.{} = {}".format(object_name, name, current_values.get(name)))


def token_from_key(key):
    if key in CONTROL_KEYS:
        return None
    return TOKEN_MAP.get(key, key)


def clamp(value, minimum, maximum):
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def cycle_enum(current, values, direction):
    index = values.index(current)
    index = (index + direction) % len(values)
    return values[index]


def adjust_parameter(values, object_name, param_spec, direction, step):
    name = param_spec["name"]
    kind = param_spec["kind"]
    current = get_object_values(values, object_name)

    if kind == "int":
        current[name] = clamp(current[name] + direction * step, param_spec["min"], param_spec["max"])
        return True

    if kind == "bool":
        current[name] = not current[name]
        return True

    if kind == "enum":
        current[name] = cycle_enum(current[name], param_spec["values"], direction)
        return True

    return False


def reset_selected_parameter(values, object_name, param_name):
    defaults = build_default_values()
    get_object_values(values, object_name)[param_name] = defaults.get(object_name, {}).get(param_name)


def reset_all_parameters(values):
    defaults = build_default_values()
    values.clear()
    for name, value in defaults.items():
        values[name] = dict(value)


def handle_text_key(values, object_name, key):
    token = token_from_key(key)
    if token is None:
        return False
    current = get_object_values(values, object_name)
    current["text"] = str(current.get("text", "")) + token
    return True


def handle_key_event(values, keypad, selected_object_index, selected_param_index, step_index, key):
    object_spec = get_object_spec(selected_object_index)
    object_name = object_spec["name"]
    param_names = object_spec["params"]
    param_spec = None
    param_name = "-"
    if param_names:
        selected_param_index = clamp(selected_param_index, 0, len(param_names) - 1)
        param_name = param_names[selected_param_index]
        param_spec = PARAMETER_BY_NAME[param_name]
    changed = False
    should_print = False
    should_exit = False

    if key == "on":
        should_exit = True
        should_print = True
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if key in ("1", "2", "3", "4"):
        target = int(key)
        if target <= len(OBJECT_SPECS):
            selected_object_index = target - 1
            selected_param_index = 0
            should_print = True
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if key == "nav_u":
        selected_object_index = (selected_object_index - 1) % len(OBJECT_SPECS)
        selected_param_index = 0
        should_print = True
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if key == "nav_d":
        selected_object_index = (selected_object_index + 1) % len(OBJECT_SPECS)
        selected_param_index = 0
        should_print = True
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if key == "tab":
        if param_names:
            selected_param_index = (selected_param_index + 1) % len(param_names)
            should_print = True
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if key == "ok":
        step_index = (step_index + 1) % len(STEP_VALUES)
        should_print = True
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if key == "exe":
        should_print = True
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if key == "alpha":
        keypad.toggle_layer("a")
        should_print = True
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if key == "beta":
        keypad.toggle_layer("b")
        should_print = True
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if key == "home":
        keypad.layer = "d"
        selected_object_index, selected_param_index = get_default_selection()
        step_index = 1
        should_print = True
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if key == "AC":
        reset_all_parameters(values)
        changed = True
        should_print = True
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if key == "back":
        if param_spec is None:
            return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit
        if param_spec is not None and param_spec["kind"] == "text":
            current = get_object_values(values, object_name)
            current["text"] = str(current.get("text", ""))[:-1]
        changed = True
        should_print = True
        if param_spec is not None and param_spec["kind"] != "text":
            reset_selected_parameter(values, object_name, param_name)
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if param_spec is not None and param_spec["kind"] == "text":
        if handle_text_key(values, object_name, key):
            changed = True
            should_print = True
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    step = STEP_VALUES[step_index]
    if key in ("nav_l", "-"):
        if param_spec is not None:
            changed = adjust_parameter(values, object_name, param_spec, -1, step)
        should_print = changed
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    if key in ("nav_r", "+", "nav_b"):
        if param_spec is not None:
            changed = adjust_parameter(values, object_name, param_spec, 1, step)
        should_print = changed
        return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit

    return selected_object_index, selected_param_index, step_index, changed, should_print, should_exit


SIMPLE_ELEMENT_PARAM_SPECS = (
    {"name": "x", "kind": "int", "min": 0, "max": WIDTH - 1, "step": 1},
    {"name": "y", "kind": "int", "min": 0, "max": HEIGHT - 1, "step": 1},
    {"name": "width", "kind": "int", "min": 1, "max": WIDTH, "step": 1},
    {"name": "height", "kind": "int", "min": 1, "max": HEIGHT, "step": 1},
    {"name": "filled", "kind": "bool"},
    {"name": "border_width", "kind": "int", "min": 0, "max": 6, "step": 1},
    {"name": "radius", "kind": "int", "min": 0, "max": 16, "step": 1},
)

SIMPLE_ELEMENT_PARAM_COUNT = len(SIMPLE_ELEMENT_PARAM_SPECS)
SIMPLE_ELEMENT_PARAM_INDEX_BY_NAME = {
    spec["name"]: index for index, spec in enumerate(SIMPLE_ELEMENT_PARAM_SPECS)
}
SIMPLE_ELEMENT_DEFAULTS = {
    "x": PREVIEW_MARGIN,
    "y": PREVIEW_MARGIN,
    "width": WIDTH - (PREVIEW_MARGIN * 2),
    "height": HEIGHT - (PREVIEW_MARGIN * 2),
    "filled": False,
    "border_width": 1,
    "radius": 0,
}


def build_default_values():
    return dict(SIMPLE_ELEMENT_DEFAULTS)


def normalize_element(values):
    values["x"] = clamp(int(values.get("x", SIMPLE_ELEMENT_DEFAULTS["x"])), 0, WIDTH - 1)
    values["y"] = clamp(int(values.get("y", SIMPLE_ELEMENT_DEFAULTS["y"])), 0, HEIGHT - 1)

    max_width = max(1, WIDTH - values["x"])
    max_height = max(1, HEIGHT - values["y"])
    values["width"] = clamp(int(values.get("width", SIMPLE_ELEMENT_DEFAULTS["width"])), 1, max_width)
    values["height"] = clamp(int(values.get("height", SIMPLE_ELEMENT_DEFAULTS["height"])), 1, max_height)
    values["filled"] = bool(values.get("filled", False))
    values["border_width"] = clamp(int(values.get("border_width", 1)), 0, 6)
    values["radius"] = clamp(int(values.get("radius", 0)), 0, 16)
    return values


def apply_single_element_style(obj, values):
    apply_element_style(
        obj,
        values["filled"],
        values["border_width"],
        values["radius"],
    )


def build_demo_screen(values):
    scr = lv.obj()
    scr.set_size(WIDTH, HEIGHT)
    clear_obj_flag(scr, lv.obj.FLAG.SCROLLABLE)
    scr.set_style_bg_opa(lv.OPA.COVER, 0)
    scr.set_style_bg_color(lv.color_white(), 0)

    element = lv.obj(scr)
    clear_obj_flag(element, lv.obj.FLAG.SCROLLABLE)

    ui = {
        "screen": scr,
        "element": element,
    }
    refresh_demo(ui, values, 0)
    return ui


def refresh_demo(ui, values, selected_param_index):
    normalize_element(values)
    selected_param_index = clamp(selected_param_index, 0, SIMPLE_ELEMENT_PARAM_COUNT - 1)

    element = ui["element"]
    element.align(lv.ALIGN.TOP_LEFT, values["x"], values["y"])
    element.set_size(values["width"], values["height"])
    apply_single_element_style(element, values)


def print_help():
    print("EDITOR_KEYS")
    print("home       : object list")
    print("tab/nav_d  : next field")
    print("nav_u      : previous field")
    print("nav_l/-    : decrease value")
    print("nav_r/+    : increase value")
    print("ok         : import / manual edit / save")
    print("exe        : next object in editor")
    print("back       : backspace in edit mode / reset field")
    print("AC         : reset all fields")
    print("on         : exit")


def print_param_map(values, selected_param_index, manual_mode=False, manual_buffer=""):
    selected_param_index = clamp(selected_param_index, 0, SIMPLE_ELEMENT_PARAM_COUNT - 1)
    print("PARAM_MAP")
    for index, param_spec in enumerate(SIMPLE_ELEMENT_PARAM_SPECS, 1):
        marker = "*" if (index - 1) == selected_param_index else " "
        name = param_spec["name"]
        print("{} {}. {} = {}".format(marker, index, name, format_value(values[name])))
    if manual_mode:
        param_spec = SIMPLE_ELEMENT_PARAM_SPECS[selected_param_index]
        current_value = format_value(values[param_spec["name"]])
        print("EDIT {} buffer={} current={}".format(param_spec["name"], manual_buffer, current_value))


def print_config(values):
    print("EDITOR_STATE")
    for param_spec in SIMPLE_ELEMENT_PARAM_SPECS:
        name = param_spec["name"]
        print("{} = {}".format(name, format_value(values[name])))


def reset_selected_parameter(values, param_spec):
    defaults = build_default_values()
    values[param_spec["name"]] = defaults[param_spec["name"]]
    normalize_element(values)


def reset_all_parameters(values):
    defaults = build_default_values()
    values.clear()
    values.update(defaults)
    normalize_element(values)


def commit_manual_value(values, param_spec, manual_buffer):
    raw = manual_buffer.strip()
    if raw == "":
        return False

    name = param_spec["name"]
    kind = param_spec["kind"]

    if kind == "int":
        values[name] = clamp(int(raw), param_spec["min"], param_spec["max"])
        normalize_element(values)
        return True

    if kind == "bool":
        try:
            values[name] = bool(int(raw))
        except ValueError:
            return False
        normalize_element(values)
        return True

    return False


def adjust_parameter(values, param_spec, direction):
    name = param_spec["name"]
    kind = param_spec["kind"]

    if kind == "bool":
        values[name] = not bool(values.get(name, False))
        normalize_element(values)
        return True

    if kind == "int":
        step = param_spec.get("step", 1)
        values[name] = clamp(
            int(values.get(name, 0)) + (direction * step),
            param_spec["min"],
            param_spec["max"],
        )
        normalize_element(values)
        return True

    return False


def handle_key_event(values, selected_param_index, manual_mode, manual_buffer, key):
    changed = False
    should_print = False
    should_exit = False

    if manual_mode:
        if key == "ok":
            param_spec = SIMPLE_ELEMENT_PARAM_SPECS[selected_param_index]
            changed = commit_manual_value(values, param_spec, manual_buffer)
            manual_mode = False
            manual_buffer = ""
            should_print = True
            return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

        if key == "back":
            if manual_buffer:
                manual_buffer = manual_buffer[:-1]
                should_print = True
            else:
                manual_mode = False
                should_print = True
            return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

        if key == "AC":
            reset_all_parameters(values)
            manual_mode = False
            manual_buffer = ""
            changed = True
            should_print = True
            return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

        if key == "exe":
            should_print = True
            return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

        if key in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
            manual_buffer += key
            should_print = True
            return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

        return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

    if key == "on":
        should_exit = True
        return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

    if key == "ok":
        manual_mode = True
        manual_buffer = ""
        should_print = True
        return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

    if key in ("tab", "nav_d"):
        selected_param_index = (selected_param_index + 1) % SIMPLE_ELEMENT_PARAM_COUNT
        should_print = True
        return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

    if key == "nav_u":
        selected_param_index = (selected_param_index - 1) % SIMPLE_ELEMENT_PARAM_COUNT
        should_print = True
        return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

    param_spec = SIMPLE_ELEMENT_PARAM_SPECS[selected_param_index]

    if key in ("nav_l", "-"):
        changed = adjust_parameter(values, param_spec, -1)
        should_print = changed
        return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

    if key in ("nav_r", "+"):
        changed = adjust_parameter(values, param_spec, 1)
        should_print = changed
        return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

    if key == "back":
        reset_selected_parameter(values, param_spec)
        changed = True
        should_print = True
        return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

    if key == "AC":
        reset_all_parameters(values)
        changed = True
        should_print = True
        return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

    if key == "exe":
        should_print = True
        return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit

    return selected_param_index, manual_mode, manual_buffer, changed, should_print, should_exit


def build_object_defaults():
    values = {}
    for spec in OBJECT_SPECS:
        name = spec["name"]
        if name == "screen":
            values[name] = {}
        elif name == "menu":
            values[name] = {
                "x": PREVIEW_MARGIN,
                "y": PREVIEW_MARGIN,
                "width": WIDTH - (PREVIEW_MARGIN * 2),
                "height": HEIGHT - (PREVIEW_MARGIN * 2),
            }
        elif name == "menu_header":
            values[name] = {"x": 2, "y": 0, "text": "UI TUNER"}
        elif name == "menu_row_1":
            values[name] = {"x": 2, "y": 10, "width": 48, "text": "x"}
        elif name == "menu_row_2":
            values[name] = {"x": 2, "y": 18, "width": 48, "text": "y"}
        elif name == "menu_row_3":
            values[name] = {"x": 2, "y": 26, "width": 48, "text": "width"}
        elif name == "preview":
            values[name] = {
                "x": PREVIEW_MARGIN,
                "y": PREVIEW_MARGIN,
                "width": WIDTH - (PREVIEW_MARGIN * 2),
                "height": HEIGHT - (PREVIEW_MARGIN * 2),
            }
        elif name == "element":
            values[name] = dict(SIMPLE_ELEMENT_DEFAULTS)
        elif name == "element_label":
            values[name] = {
                "text": "FIRST ELEMENT",
                "text_align": "center",
                "text_offset_x": 0,
                "text_offset_y": 0,
                "padding_left": 4,
                "padding_right": 4,
            }
        else:
            values[name] = {}
    return values


def normalize_object_values(values, object_name):
    current = get_object_values(values, object_name)
    for param_name in get_object_params(object_name):
        param_spec = PARAMETER_BY_NAME.get(param_name)
        if param_spec is None:
            continue
        kind = param_spec["kind"]
        if kind == "int":
            current[param_name] = clamp(
                int(current.get(param_name, param_spec["min"])),
                param_spec["min"],
                param_spec["max"],
            )
        elif kind == "bool":
            current[param_name] = bool(current.get(param_name, False))
        elif kind == "enum":
            enum_values = param_spec["values"]
            current[param_name] = current.get(param_name, enum_values[0])
            if current[param_name] not in enum_values:
                current[param_name] = enum_values[0]
        elif kind == "text":
            current[param_name] = str(current.get(param_name, ""))
    if object_name == "element":
        normalize_element(current)
    return current


def adjust_object_parameter(values, object_name, param_spec, direction):
    current = get_object_values(values, object_name)
    name = param_spec["name"]
    kind = param_spec["kind"]

    if kind == "bool":
        current[name] = not bool(current.get(name, False))
        normalize_object_values(values, object_name)
        return True

    if kind == "int":
        step = param_spec.get("step", 1)
        current[name] = clamp(
            int(current.get(name, 0)) + (direction * step),
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
    if raw == "":
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
    get_object_values(values, object_name)[param_name] = defaults.get(object_name, {}).get(param_name)
    normalize_object_values(values, object_name)


def reset_object(values, object_name):
    defaults = build_object_defaults()
    values[object_name] = dict(defaults.get(object_name, {}))
    normalize_object_values(values, object_name)


def print_object_param_map(values, object_name, selected_param_index, manual_mode=False, manual_buffer=""):
    param_names = get_object_params(object_name)
    selected_param_index = clamp(selected_param_index, 0, len(param_names) - 1) if param_names else 0
    print("OBJECT_PARAMS")
    print("selected_object =", object_name)
    if not param_names:
        print("no parameters")
        return
    current_values = get_object_values(values, object_name)
    for index, param_name in enumerate(param_names, 1):
        marker = "*" if (index - 1) == selected_param_index else " "
        print("{} {}. {} = {}".format(marker, index, param_name, format_value(current_values.get(param_name))))
    if manual_mode:
        current_param = param_names[selected_param_index]
        print(
            "EDIT {} buffer={} current={}".format(
                current_param,
                manual_buffer,
                format_value(current_values.get(current_param)),
            )
        )


def handle_object_browser_key_event(selected_object_index, key):
    should_print = False
    should_exit = False
    import_requested = False

    if key == "on":
        should_exit = True
        return selected_object_index, import_requested, should_print, should_exit

    if key == "nav_u":
        selected_object_index = (selected_object_index - 1) % len(OBJECT_SPECS)
        should_print = True
        return selected_object_index, import_requested, should_print, should_exit

    if key == "nav_d":
        selected_object_index = (selected_object_index + 1) % len(OBJECT_SPECS)
        should_print = True
        return selected_object_index, import_requested, should_print, should_exit

    if key == "ok":
        import_requested = True
        should_print = True
        return selected_object_index, import_requested, should_print, should_exit

    if key == "exe":
        should_print = True
        return selected_object_index, import_requested, should_print, should_exit

    return selected_object_index, import_requested, should_print, should_exit


def handle_object_editor_key_event(
    object_values,
    selected_object_index,
    selected_param_index,
    manual_mode,
    manual_buffer,
    keypad,
    key,
):
    changed = False
    should_print = False
    should_exit = False
    browser_requested = False

    object_spec = get_object_spec(selected_object_index)
    object_name = object_spec["name"]
    param_names = object_spec["params"]
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
                changed = commit_object_manual_value(object_values, object_name, param_spec, manual_buffer)
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
            reset_object(object_values, object_name)
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

        if key == "exe":
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

        if key in ("0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
            manual_buffer += key
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
            current = get_object_values(object_values, object_name)
            current["text"] = str(current.get("text", ""))[:-1]
            normalize_object_values(object_values, object_name)
            changed = True
            should_print = True
        elif handle_text_key(object_values, object_name, key):
            normalize_object_values(object_values, object_name)
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
            changed = adjust_object_parameter(object_values, object_name, param_spec, -1)
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

    if key in ("nav_r", "+"):
        if param_spec is not None:
            changed = adjust_object_parameter(object_values, object_name, param_spec, 1)
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
        if param_spec is None:
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
        if param_spec["kind"] == "text":
            current = get_object_values(object_values, object_name)
            current["text"] = str(current.get("text", ""))[:-1]
            normalize_object_values(object_values, object_name)
        else:
            reset_object_parameter(object_values, object_name, param_spec["name"])
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
        reset_object(object_values, object_name)
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

        values = build_default_values()
        object_values = build_object_defaults()
        keypad = MatrixKeypad(ROW_PINS, COL_PINS, KEYMAP_LAYERS)
        ui = build_demo_screen(values)
        lv.screen_load(ui["screen"])

        selected_param_index = 0
        manual_mode = False
        manual_buffer = ""
        selected_object_index = OBJECT_INDEX_BY_NAME["element"] - 1
        mode = "simple"

        print_help()
        print_param_map(values, selected_param_index, manual_mode, manual_buffer)

        exit_requested = False

        while not exit_requested:
            keypad.tick()

            key = keypad.pop_event()
            while key is not None:
                if mode == "simple":
                    if key == "home":
                        mode = "browser"
                        selected_object_index = OBJECT_INDEX_BY_NAME["element"] - 1
                        print_object_registry(object_values, selected_object_index)
                    else:
                        (
                            selected_param_index,
                            manual_mode,
                            manual_buffer,
                            changed,
                            should_print,
                            exit_requested,
                        ) = handle_key_event(
                            values,
                            selected_param_index,
                            manual_mode,
                            manual_buffer,
                            key,
                        )

                        if changed:
                            refresh_demo(ui, values, selected_param_index)
                            object_values["element"] = dict(values)

                        if should_print:
                            print_param_map(values, selected_param_index, manual_mode, manual_buffer)
                elif mode == "browser":
                    (
                        selected_object_index,
                        import_requested,
                        should_print,
                        exit_requested,
                    ) = handle_object_browser_key_event(selected_object_index, key)

                    if import_requested:
                        mode = "editor"
                        selected_param_index = 0
                        manual_mode = False
                        manual_buffer = ""
                        object_name = get_object_spec(selected_object_index)["name"]
                        if object_name == "element":
                            object_values["element"] = dict(values)
                            values.update(object_values["element"])
                            refresh_demo(ui, values, selected_param_index)
                        normalize_object_values(object_values, object_name)
                        print_object_param_map(object_values, object_name, selected_param_index, manual_mode, manual_buffer)
                    elif should_print:
                        print_object_registry(object_values, selected_object_index)
                        print_selected_object_preview(object_values, selected_object_index)
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
                        object_values,
                        selected_object_index,
                        selected_param_index,
                        manual_mode,
                        manual_buffer,
                        keypad,
                        key,
                    )

                    object_name = get_object_spec(selected_object_index)["name"]
                    if changed and object_name == "element":
                        values.update(object_values["element"])
                        refresh_demo(ui, values, selected_param_index)
                    elif object_name == "element" and should_print:
                        values.update(object_values["element"])
                        refresh_demo(ui, values, selected_param_index)

                    if browser_requested:
                        mode = "browser"
                        print_object_registry(object_values, selected_object_index)
                        print_selected_object_preview(object_values, selected_object_index)
                    elif should_print:
                        print_object_param_map(
                            object_values,
                            object_name,
                            selected_param_index,
                            manual_mode,
                            manual_buffer,
                        )

                key = keypad.pop_event()

            lv.tick_inc(LOOP_DELAY_MS)
            lv.timer_handler()
            time.sleep_ms(LOOP_DELAY_MS)

        print("EDITOR_EXIT")
    finally:
        release_calsci_keypad(keypad_blocked_here)


main()
