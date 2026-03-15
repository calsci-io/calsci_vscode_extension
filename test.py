import time

import lvgl as lv
import st7565 as display

WIDTH = 128
HEIGHT = 64
FB_SIZE = (WIDTH * HEIGHT) // 8
SRC_STRIDE = WIDTH // 8


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


def lvgl_icon_demo():
    ensure_st7565_ready()
    lv.init()

    draw_buf = bytearray(FB_SIZE + 8)
    tx_buf = bytearray(FB_SIZE)

    disp = lv.display_create(WIDTH, HEIGHT)
    disp.set_color_format(lv.COLOR_FORMAT.I1)
    disp.set_buffers(draw_buf, None, len(draw_buf), lv.DISPLAY_RENDER_MODE.FULL)
    # In mpremote "resume" sessions LVGL can already have an older default display.
    # Make this freshly configured display the active target for screen_load().
    disp.set_default()

    def i1_to_st7565(src_bytes, dst_bytes):
        for i in range(FB_SIZE):
            dst_bytes[i] = 0
        for y in range(HEIGHT):
            page = y >> 3
            dst_bit = 1 << (y & 7)
            row = y * SRC_STRIDE
            dst_row = page * WIDTH
            for xb in range(SRC_STRIDE):
                b = src_bytes[row + xb]
                x = xb * 8
                if b & 0x80:
                    dst_bytes[dst_row + x + 0] |= dst_bit
                if b & 0x40:
                    dst_bytes[dst_row + x + 1] |= dst_bit
                if b & 0x20:
                    dst_bytes[dst_row + x + 2] |= dst_bit
                if b & 0x10:
                    dst_bytes[dst_row + x + 3] |= dst_bit
                if b & 0x08:
                    dst_bytes[dst_row + x + 4] |= dst_bit
                if b & 0x04:
                    dst_bytes[dst_row + x + 5] |= dst_bit
                if b & 0x02:
                    dst_bytes[dst_row + x + 6] |= dst_bit
                if b & 0x01:
                    dst_bytes[dst_row + x + 7] |= dst_bit

    def flush_cb(_disp, _area, color_p):
        raw = color_p.__dereference__(len(draw_buf))
        i1_to_st7565(raw[8:8 + FB_SIZE], tx_buf)
        display.graphics(tx_buf)
        disp.flush_ready()

    disp.set_flush_cb(flush_cb)

    scr = lv.obj()
    scr.set_size(WIDTH, HEIGHT)
    scr.set_style_bg_opa(lv.OPA.COVER, 0)
    scr.set_style_bg_color(lv.color_white(), 0)

    title = lv.label(scr)
    title.set_text("ICON DEMO")
    title.set_style_text_color(lv.color_black(), 0)
    title.align(lv.ALIGN.TOP_MID, 0, 2)

    icons = lv.label(scr)
    icons.set_text(lv.SYMBOL.BELL + " " + lv.SYMBOL.OK + " " + lv.SYMBOL.WARNING)
    icons.set_style_text_color(lv.color_black(), 0)
    icons.align(lv.ALIGN.CENTER, 0, -4)

    smile = lv.label(scr)
    smile.set_text(":-)  LVGL")
    smile.set_style_text_color(lv.color_black(), 0)
    smile.align(lv.ALIGN.BOTTOM_MID, 0, -2)

    lv.screen_load(scr)

    for _ in range(600):
        lv.timer_handler()
        time.sleep_ms(20)

    print("ICON_DRAW_OK")


lvgl_icon_demo()
