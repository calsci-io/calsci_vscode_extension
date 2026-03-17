import time
from machine import Pin

import lvgl as lv
import st7565 as display

WIDTH = 128
HEIGHT = 64
FB_SIZE = (WIDTH * HEIGHT) // 8
SRC_STRIDE = WIDTH // 8
LOOP_DELAY_MS = 20

ROW_PINS = [14, 21, 47, 48, 38, 39, 40, 41, 42, 1]
COL_PINS = [8, 18, 17, 15, 7]

KEYMAP_DEFAULT = [
    ["on", "alpha", "beta", "home", "wifi"],
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
    ["on", "alpha", "beta", "home", "wifi"],
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
    ["on", "alpha", "beta", "home", "wifi"],
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
    "x": 10,
    "y": 22,
    "width": 108,
    "height": 18,
    "text": "FIRST ELEMENT",
    "filled": False,
    "border_width": 1,
    "radius": 0,
    "text_align": "center",
    "text_offset_x": 0,
    "text_offset_y": 0,
    "padding_left": 4,
    "padding_right": 4,
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


def build_demo_screen(config):
    scr = lv.obj()
    scr.set_size(WIDTH, HEIGHT)
    clear_obj_flag(scr, lv.obj.FLAG.SCROLLABLE)
    scr.set_style_bg_opa(lv.OPA.COVER, 0)
    scr.set_style_bg_color(lv.color_white(), 0)

    element = lv.obj(scr)
    clear_obj_flag(element, lv.obj.FLAG.SCROLLABLE)

    label = lv.label(element)

    ui = {
        "screen": scr,
        "element": element,
        "label": label,
    }
    refresh_demo(ui, config)
    return ui


def refresh_demo(ui, config):
    element = ui["element"]
    label = ui["label"]

    element.set_size(config["width"], config["height"])
    element.align(lv.ALIGN.TOP_LEFT, config["x"], config["y"])
    apply_element_style(
        element,
        config["filled"],
        config["border_width"],
        config["radius"],
    )

    text_color = lv.color_white() if config["filled"] else lv.color_black()
    label.set_text(config["text"] if config["text"] else " ")
    label.set_style_text_color(text_color, 0)
    align_label(label, config)


def print_help():
    print("EDITOR_KEYS")
    print("nav_u/nav_d : select parameter")
    print("nav_l/nav_r : change value")
    print("+/-         : change value")
    print("ok          : next step")
    print("exe         : print config")
    print("back        : delete text or reset selected parameter")
    print("AC          : reset all parameters")
    print("alpha/beta  : toggle text layer")
    print("on          : exit")


def print_config(config, selected_index, step, layer):
    selected_name = PARAMETERS[selected_index]["name"]
    print("EDITOR_STATE")
    print("selected =", selected_name)
    print("step =", step)
    print("layer =", LAYER_NAMES[layer])
    print("x =", config["x"])
    print("y =", config["y"])
    print("width =", config["width"])
    print("height =", config["height"])
    print("text =", repr(config["text"]))
    print("filled =", config["filled"])
    print("border_width =", config["border_width"])
    print("radius =", config["radius"])
    print("text_align =", config["text_align"])
    print("text_offset_x =", config["text_offset_x"])
    print("text_offset_y =", config["text_offset_y"])
    print("padding_left =", config["padding_left"])
    print("padding_right =", config["padding_right"])


def get_selected_param(selected_index):
    return PARAMETERS[selected_index]


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


def adjust_parameter(config, param, direction, step):
    name = param["name"]
    kind = param["kind"]

    if kind == "int":
        config[name] = clamp(config[name] + direction * step, param["min"], param["max"])
        return True

    if kind == "bool":
        config[name] = not config[name]
        return True

    if kind == "enum":
        config[name] = cycle_enum(config[name], param["values"], direction)
        return True

    return False


def reset_selected_parameter(config, selected_index):
    name = PARAMETERS[selected_index]["name"]
    config[name] = DEFAULT_CONFIG[name]


def reset_all_parameters(config):
    for name, value in DEFAULT_CONFIG.items():
        config[name] = value


def handle_text_key(config, key):
    token = token_from_key(key)
    if token is None:
        return False
    config["text"] += token
    return True


def handle_key_event(config, keypad, selected_index, step_index, key):
    selected_param = get_selected_param(selected_index)
    changed = False
    should_print = False
    should_exit = False

    if key == "on":
        should_exit = True
        should_print = True
        return selected_index, step_index, changed, should_print, should_exit

    if key == "nav_u":
        selected_index = (selected_index - 1) % len(PARAMETERS)
        should_print = True
        return selected_index, step_index, changed, should_print, should_exit

    if key == "nav_d":
        selected_index = (selected_index + 1) % len(PARAMETERS)
        should_print = True
        return selected_index, step_index, changed, should_print, should_exit

    if key == "ok":
        step_index = (step_index + 1) % len(STEP_VALUES)
        should_print = True
        return selected_index, step_index, changed, should_print, should_exit

    if key == "exe":
        should_print = True
        return selected_index, step_index, changed, should_print, should_exit

    if key == "alpha":
        keypad.toggle_layer("a")
        should_print = True
        return selected_index, step_index, changed, should_print, should_exit

    if key == "beta":
        keypad.toggle_layer("b")
        should_print = True
        return selected_index, step_index, changed, should_print, should_exit

    if key == "home":
        keypad.layer = "d"
        selected_index = 0
        step_index = 0
        should_print = True
        return selected_index, step_index, changed, should_print, should_exit

    if key == "AC":
        reset_all_parameters(config)
        changed = True
        should_print = True
        return selected_index, step_index, changed, should_print, should_exit

    if key == "back":
        if selected_param["kind"] == "text":
            config["text"] = config["text"][:-1]
        else:
            reset_selected_parameter(config, selected_index)
        changed = True
        should_print = True
        return selected_index, step_index, changed, should_print, should_exit

    if selected_param["kind"] == "text":
        if handle_text_key(config, key):
            changed = True
            should_print = True
        return selected_index, step_index, changed, should_print, should_exit

    step = STEP_VALUES[step_index]
    if key in ("nav_l", "-"):
        changed = adjust_parameter(config, selected_param, -1, step)
        should_print = changed
        return selected_index, step_index, changed, should_print, should_exit

    if key in ("nav_r", "+", "nav_b"):
        changed = adjust_parameter(config, selected_param, 1, step)
        should_print = changed
        return selected_index, step_index, changed, should_print, should_exit

    return selected_index, step_index, changed, should_print, should_exit


def main():
    for name, value in ELEMENT_CONFIG.items():
        DEFAULT_CONFIG[name] = value

    ensure_st7565_ready()
    build_lvgl_display()

    config = {}
    for name, value in ELEMENT_CONFIG.items():
        config[name] = value

    keypad = MatrixKeypad(ROW_PINS, COL_PINS, KEYMAP_LAYERS)
    ui = build_demo_screen(config)
    lv.screen_load(ui["screen"])

    selected_index = 0
    step_index = 0

    print_help()
    print_config(config, selected_index, STEP_VALUES[step_index], keypad.layer)

    exit_requested = False

    while not exit_requested:
        keypad.tick()

        key = keypad.pop_event()
        while key is not None:
            print("key =", key)
            selected_index, step_index, changed, should_print, exit_requested = handle_key_event(
                config,
                keypad,
                selected_index,
                step_index,
                key,
            )
            if changed:
                refresh_demo(ui, config)
            if should_print:
                print_config(config, selected_index, STEP_VALUES[step_index], keypad.layer)
            key = keypad.pop_event()

        lv.tick_inc(LOOP_DELAY_MS)
        lv.timer_handler()
        time.sleep_ms(LOOP_DELAY_MS)

    print("EDITOR_EXIT")


main()
