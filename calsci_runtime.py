import time

try:
    import utime as _utime
except ImportError:
    _utime = time

_calsci_keypad_blocked = False


def _sleep_ms(ms):
    sleeper = getattr(_utime, "sleep_ms", None)
    if sleeper is not None:
        sleeper(ms)
        return
    time.sleep(ms / 1000.0)


def calsci_keypad_blocked():
    return _calsci_keypad_blocked


def set_calsci_keypad_blocked(blocked):
    global _calsci_keypad_blocked
    _calsci_keypad_blocked = bool(blocked)
    return _calsci_keypad_blocked


def block_calsci_keypad():
    return set_calsci_keypad_blocked(True)


def unblock_calsci_keypad():
    return set_calsci_keypad_blocked(False)


def wait_if_repl_busy():
    while _calsci_keypad_blocked:
        _sleep_ms(5)
