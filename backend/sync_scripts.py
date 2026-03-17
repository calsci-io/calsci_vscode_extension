from __future__ import annotations

import json


def device_mkdir_script(remote_dir: str) -> str:
    target = json.dumps(remote_dir)
    return (
        "import os\n"
        "def _mk(_p):\n"
        "    _cur = ''\n"
        "    for _part in _p.split('/'):\n"
        "        if not _part:\n"
        "            continue\n"
        "        _cur += '/' + _part\n"
        "        try:\n"
        "            os.mkdir(_cur)\n"
        "        except OSError:\n"
        "            pass\n"
        f"_mk({target})\n"
    )


def device_delete_file_script(remote_file: str) -> str:
    target = json.dumps(remote_file)
    return "import os\n" f"os.remove({target})\n"


def device_list_file_sizes_script(remote_root: str) -> str:
    remote_root_json = json.dumps(remote_root)
    return (
        "import os\n"
        f"_root = {remote_root_json}\n"
        "_result = {}\n"
        "def _is_dir(_entry, _full, _stat):\n"
        "    try:\n"
        "        if len(_entry) > 1 and isinstance(_entry[1], int) and (_entry[1] & 0x4000):\n"
        "            return True\n"
        "    except:\n"
        "        pass\n"
        "    try:\n"
        "        _mode = _stat[0] if _stat and len(_stat) > 0 else 0\n"
        "        if _mode & 0x4000:\n"
        "            return True\n"
        "    except:\n"
        "        pass\n"
        "    try:\n"
        "        os.ilistdir(_full)\n"
        "        return True\n"
        "    except:\n"
        "        return False\n"
        "def _scan(_path):\n"
        "    try:\n"
        "        for _entry in os.ilistdir(_path):\n"
        "            _name = _entry[0]\n"
        "            _full = _path + '/' + _name if _path != '/' else '/' + _name\n"
        "            try:\n"
        "                _stat = None\n"
        "                try:\n"
        "                    _stat = os.stat(_full)\n"
        "                except:\n"
        "                    _stat = None\n"
        "                if _is_dir(_entry, _full, _stat):\n"
        "                    _scan(_full)\n"
        "                else:\n"
        "                    if _stat is None:\n"
        "                        try:\n"
        "                            _stat = os.stat(_full)\n"
        "                        except:\n"
        "                            _stat = None\n"
        "                    if _stat is not None and len(_stat) > 6:\n"
        "                        _result[_full] = _stat[6]\n"
        "                    elif len(_entry) > 3 and isinstance(_entry[3], int):\n"
        "                        _result[_full] = _entry[3]\n"
        "                    else:\n"
        "                        _result[_full] = 0\n"
        "            except:\n"
        "                pass\n"
        "    except:\n"
        "        pass\n"
        "_scan(_root)\n"
        "print('SIZES:' + repr(_result))\n"
    )


def device_list_file_sizes_stream_script(remote_root: str) -> str:
    remote_root_json = json.dumps(remote_root)
    return (
        "import os, sys\n"
        f"_root = {remote_root_json}\n"
        "def _is_dir(_entry, _full, _stat):\n"
        "    try:\n"
        "        if len(_entry) > 1 and isinstance(_entry[1], int) and (_entry[1] & 0x4000):\n"
        "            return True\n"
        "    except:\n"
        "        pass\n"
        "    try:\n"
        "        _mode = _stat[0] if _stat and len(_stat) > 0 else 0\n"
        "        if _mode & 0x4000:\n"
        "            return True\n"
        "    except:\n"
        "        pass\n"
        "    try:\n"
        "        os.ilistdir(_full)\n"
        "        return True\n"
        "    except:\n"
        "        return False\n"
        "def _scan(_path):\n"
        "    try:\n"
        "        for _entry in os.ilistdir(_path):\n"
        "            _name = _entry[0]\n"
        "            _full = _path + '/' + _name if _path != '/' else '/' + _name\n"
        "            try:\n"
        "                _stat = None\n"
        "                try:\n"
        "                    _stat = os.stat(_full)\n"
        "                except:\n"
        "                    _stat = None\n"
        "                if _is_dir(_entry, _full, _stat):\n"
        "                    _scan(_full)\n"
        "                else:\n"
        "                    if _stat is None:\n"
        "                        try:\n"
        "                            _stat = os.stat(_full)\n"
        "                        except:\n"
        "                            _stat = None\n"
        "                    if _stat is not None and len(_stat) > 6:\n"
        "                        _size = _stat[6]\n"
        "                    elif len(_entry) > 3 and isinstance(_entry[3], int):\n"
        "                        _size = _entry[3]\n"
        "                    else:\n"
        "                        _size = 0\n"
        "                    sys.stdout.write('SIZE:' + _full + ':' + str(_size) + '\\n')\n"
        "            except:\n"
        "                pass\n"
        "    except:\n"
        "        pass\n"
        "_scan(_root)\n"
        "print('SIZE_SCAN_DONE')\n"
    )


def device_list_file_signatures_script(remote_paths: list[str]) -> str:
    remote_paths_json = json.dumps(remote_paths)
    return (
        "import os\n"
        f"_paths = {remote_paths_json}\n"
        "_result = {}\n"
        "def _sig(_path):\n"
        "    _hash = 2166136261\n"
        "    try:\n"
        "        _file = open(_path, 'rb')\n"
        "    except:\n"
        "        return None\n"
        "    try:\n"
        "        while True:\n"
        "            _chunk = _file.read(512)\n"
        "            if not _chunk:\n"
        "                break\n"
        "            for _byte in _chunk:\n"
        "                if not isinstance(_byte, int):\n"
        "                    _byte = ord(_byte)\n"
        "                _hash ^= _byte\n"
        "                _hash = (_hash * 16777619) & 0xffffffff\n"
        "    finally:\n"
        "        try:\n"
        "            _file.close()\n"
        "        except:\n"
        "            pass\n"
        "    return '%08x' % _hash\n"
        "for _path in _paths:\n"
        "    try:\n"
        "        _result[_path] = _sig(_path)\n"
        "    except:\n"
        "        _result[_path] = None\n"
        "print('SIGS:' + repr(_result))\n"
    )


def device_list_file_signatures_stream_script(remote_paths: list[str]) -> str:
    remote_paths_json = json.dumps(remote_paths)
    return (
        "import sys\n"
        f"_paths = {remote_paths_json}\n"
        "def _sig(_path):\n"
        "    _hash = 2166136261\n"
        "    try:\n"
        "        _file = open(_path, 'rb')\n"
        "    except:\n"
        "        return None\n"
        "    try:\n"
        "        while True:\n"
        "            _chunk = _file.read(512)\n"
        "            if not _chunk:\n"
        "                break\n"
        "            for _byte in _chunk:\n"
        "                if not isinstance(_byte, int):\n"
        "                    _byte = ord(_byte)\n"
        "                _hash ^= _byte\n"
        "                _hash = (_hash * 16777619) & 0xffffffff\n"
        "    finally:\n"
        "        try:\n"
        "            _file.close()\n"
        "        except:\n"
        "            pass\n"
        "    return '%08x' % _hash\n"
        "for _path in _paths:\n"
        "    try:\n"
        "        _value = _sig(_path)\n"
        "    except:\n"
        "        _value = None\n"
        "    if _value is None:\n"
        "        sys.stdout.write('SIG:' + str(len(_path)) + ':' + _path + ':0:\\n')\n"
        "    else:\n"
        "        sys.stdout.write('SIG:' + str(len(_path)) + ':' + _path + ':1:' + _value + '\\n')\n"
        "print('SIG_SCAN_DONE')\n"
    )


def device_selected_file_sizes_script(remote_paths: list[str]) -> str:
    remote_paths_json = json.dumps(remote_paths)
    return (
        "import os\n"
        f"_paths = {remote_paths_json}\n"
        "_result = {}\n"
        "for _path in _paths:\n"
        "    try:\n"
        "        _stat = os.stat(_path)\n"
        "        _result[_path] = _stat[6] if len(_stat) > 6 else None\n"
        "    except:\n"
        "        try:\n"
        "            _f = open(_path, 'rb')\n"
        "            _size = 0\n"
        "            while True:\n"
        "                _chunk = _f.read(512)\n"
        "                if not _chunk:\n"
        "                    break\n"
        "                _size += len(_chunk)\n"
        "            _f.close()\n"
        "            _result[_path] = _size\n"
        "        except:\n"
        "            _result[_path] = None\n"
        "print('PATH_SIZES:' + repr(_result))\n"
    )


def device_selected_file_sizes_stream_script(remote_paths: list[str]) -> str:
    remote_paths_json = json.dumps(remote_paths)
    return (
        "import os, sys\n"
        f"_paths = {remote_paths_json}\n"
        "for _path in _paths:\n"
        "    _size = None\n"
        "    try:\n"
        "        _stat = os.stat(_path)\n"
        "        _size = _stat[6] if len(_stat) > 6 else None\n"
        "    except:\n"
        "        try:\n"
        "            _f = open(_path, 'rb')\n"
        "            _size = 0\n"
        "            while True:\n"
        "                _chunk = _f.read(512)\n"
        "                if not _chunk:\n"
        "                    break\n"
        "                _size += len(_chunk)\n"
        "            _f.close()\n"
        "        except:\n"
        "            _size = None\n"
        "    if _size is None:\n"
        "        sys.stdout.write('PATHSIZE:' + str(len(_path)) + ':' + _path + ':0:\\n')\n"
        "    else:\n"
        "        sys.stdout.write('PATHSIZE:' + str(len(_path)) + ':' + _path + ':1:' + str(_size) + '\\n')\n"
        "print('PATH_SIZE_SCAN_DONE')\n"
    )


def device_scan_tree_stream_script(remote_root: str) -> str:
    remote_root_json = json.dumps(remote_root)
    return (
        "import os, sys\n"
        f"_root = {remote_root_json}\n"
        "def _is_dir(_entry, _full, _stat):\n"
        "    try:\n"
        "        if len(_entry) > 1 and isinstance(_entry[1], int) and (_entry[1] & 0x4000):\n"
        "            return True\n"
        "    except:\n"
        "        pass\n"
        "    try:\n"
        "        _mode = _stat[0] if _stat and len(_stat) > 0 else 0\n"
        "        if _mode & 0x4000:\n"
        "            return True\n"
        "    except:\n"
        "        pass\n"
        "    try:\n"
        "        os.ilistdir(_full)\n"
        "        return True\n"
        "    except:\n"
        "        return False\n"
        "def _emit_dir(_path):\n"
        "    sys.stdout.write('DIR:' + str(len(_path)) + ':' + _path + '\\n')\n"
        "def _emit_file(_path, _size):\n"
        "    sys.stdout.write('FILE:' + str(len(_path)) + ':' + _path + ':' + str(_size) + '\\n')\n"
        "def _scan(_path):\n"
        "    try:\n"
        "        for _entry in os.ilistdir(_path):\n"
        "            _name = _entry[0]\n"
        "            _full = _path + '/' + _name if _path != '/' else '/' + _name\n"
        "            _stat = None\n"
        "            try:\n"
        "                _stat = os.stat(_full)\n"
        "            except:\n"
        "                _stat = None\n"
        "            try:\n"
        "                if _is_dir(_entry, _full, _stat):\n"
        "                    _emit_dir(_full)\n"
        "                    _scan(_full)\n"
        "                else:\n"
        "                    if _stat is not None and len(_stat) > 6:\n"
        "                        _size = _stat[6]\n"
        "                    elif len(_entry) > 3 and isinstance(_entry[3], int):\n"
        "                        _size = _entry[3]\n"
        "                    else:\n"
        "                        _size = 0\n"
        "                    _emit_file(_full, _size)\n"
        "            except Exception as _inner_exc:\n"
        "                sys.stdout.write('SCANERR:' + _full + ':' + str(_inner_exc) + '\\n')\n"
        "    except Exception as _exc:\n"
        "        sys.stdout.write('SCANERR:' + _path + ':' + str(_exc) + '\\n')\n"
        "_scan(_root)\n"
        "print('TREE_SCAN_DONE')\n"
    )


def device_read_file_hex_stream_script(remote_file: str, chunk_bytes: int) -> str:
    remote_file_json = json.dumps(remote_file)
    return (
        "import sys\n"
        "try:\n"
        "    import ubinascii as _binascii\n"
        "except ImportError:\n"
        "    import binascii as _binascii\n"
        f"_path = {remote_file_json}\n"
        "try:\n"
        "    _f = open(_path, 'rb')\n"
        "    try:\n"
        f"        while True:\n"
        f"            _chunk = _f.read({int(chunk_bytes)})\n"
        "            if not _chunk:\n"
        "                break\n"
        "            sys.stdout.write('HEX:' + _binascii.hexlify(_chunk).decode() + '\\n')\n"
        "    finally:\n"
        "        _f.close()\n"
        "    print('FILE_READ_DONE')\n"
        "except Exception as _exc:\n"
        "    print('FILE_READ_ERR:' + str(_exc))\n"
    )


def device_read_text_file_stream_script(remote_file: str, chunk_chars: int) -> str:
    remote_file_json = json.dumps(remote_file)
    return (
        "import sys\n"
        f"_path = {remote_file_json}\n"
        "_start = '[[CALSCI_FILE_CONTENT_START]]'\n"
        "_end = '[[CALSCI_FILE_CONTENT_END]]'\n"
        "try:\n"
        "    _f = open(_path, 'r')\n"
        "    try:\n"
        "        print(_start)\n"
        f"        while True:\n"
        f"            _chunk = _f.read({int(chunk_chars)})\n"
        "            if not _chunk:\n"
        "                break\n"
        "            sys.stdout.write(_chunk)\n"
        "        print(_end)\n"
        "    finally:\n"
        "        _f.close()\n"
        "except Exception as _exc:\n"
        "    print('FILE_READ_ERR:' + str(_exc))\n"
    )


def device_put_file_script(remote_file: str, data: bytes, chunk_bytes: int) -> str:
    remote_file_json = json.dumps(remote_file)
    lines = [
        "import os",
        "try:",
        f"    os.remove({remote_file_json})",
        "except OSError:",
        "    pass",
        f"_f = open({remote_file_json}, 'wb')",
    ]
    for start in range(0, len(data), chunk_bytes):
        chunk = data[start : start + chunk_bytes]
        lines.append(f"_f.write({repr(chunk)})")
    lines.extend([
        "_f.close()",
        'print("OK")',
    ])
    return "\\r\\n".join(lines) + "\\r\\n"


def estimate_sync_source_timeout(source: str, minimum_seconds: float, bytes_per_second: float = 8192.0) -> float:
    wire_size = len(source.encode("utf-8"))
    return max(minimum_seconds, 5.0 + (wire_size / bytes_per_second))
