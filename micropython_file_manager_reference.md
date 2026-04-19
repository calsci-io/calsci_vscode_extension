# MicroPython File Manager Function Reference

This guide collects the MicroPython built-in and core-module functions that are most useful for building a complete file manager without relying on anything from `c_modules/`.

It is written against the MicroPython source tree in this workspace, which reports version `1.28.0-preview` in `py/mpconfig.h`.

## Scope

This guide focuses on:

- `open()` and file/stream methods
- `os` filesystem functions
- `vfs` mounting and formatting functions
- support modules that help a file manager work better: `errno`, `time`, `gc`, `json`, `sys`, and `io`

This guide does **not** try to list every module in MicroPython. It lists the ones that are directly useful for browsing, reading, writing, copying, moving, deleting, mounting, formatting, and monitoring storage.

## Quick Map

| File manager task | Main API |
| --- | --- |
| Open a file | `open()` |
| Read and write file contents | `read()`, `write()`, `readline()`, `readinto()` |
| Change folder | `os.chdir()` |
| Get current folder | `os.getcwd()` |
| List names | `os.listdir()` |
| List names with type and size | `os.ilistdir()` |
| Create folder | `os.mkdir()` |
| Delete file | `os.remove()` |
| Delete empty folder | `os.rmdir()` |
| Rename or move | `os.rename()` |
| Inspect file info | `os.stat()` |
| Inspect free space | `os.statvfs()` |
| Force filesystem sync | `os.sync()` |
| Mount and unmount storage | `vfs.mount()`, `vfs.umount()` |
| Format storage | `vfs.VfsFat.mkfs()`, `vfs.VfsLfs1.mkfs()`, `vfs.VfsLfs2.mkfs()` |
| Handle file errors cleanly | `OSError`, `errno` |
| Format timestamps | `time.localtime()` |
| Avoid memory problems during big copy | `gc.collect()`, `readinto()` |
| Save file manager settings | `json.dump()`, `json.load()` |

## 1. `open()` and File Object Methods

MicroPython file objects are stream objects. For a file manager, this is the core API for reading, writing, appending, copying, previewing, and editing files.

### `open(name, mode='r', **kwargs)`

Open a file and return a file object.

Common modes:

- `'r'` read text
- `'w'` write text and truncate file
- `'a'` append text
- `'rb'` read binary
- `'wb'` write binary and truncate
- `'ab'` append binary
- `'r+'` read and write
- `'rb+'` read and write binary

Example:

```python
f = open("/data/notes.txt", "r")
print(f.read())
f.close()
```

Recommended pattern:

```python
with open("/data/notes.txt", "r") as f:
    print(f.read())
```

### `f.read([n])`

Read the whole file, or at most `n` bytes/characters.

```python
with open("/data/log.txt", "r") as f:
    print(f.read(64))   # preview first 64 chars
```

### `f.readline([n])`

Read one line. Useful for file preview, line-by-line viewers, CSV readers, and log viewers.

```python
with open("/data/log.txt", "r") as f:
    print(f.readline())
```

### `f.readlines()`

Read all lines into a list. Handy for small files, but avoid it for large files because it uses more RAM.

```python
with open("/data/list.txt", "r") as f:
    lines = f.readlines()
print(lines)
```

### `f.readinto(buf[, nbytes])`

Read directly into a preallocated buffer. This is one of the best functions for efficient file copying in MicroPython because it avoids creating many temporary objects.

```python
buf = bytearray(128)
with open("/data/blob.bin", "rb") as f:
    n = f.readinto(buf)
print(n, buf[:n])
```

### `f.write(buf)`

Write text or bytes. Returns the number of bytes/characters written.

```python
with open("/data/out.txt", "w") as f:
    count = f.write("hello\n")
print(count)
```

### `f.flush()`

Ask the file object to flush pending data to the filesystem. MicroPython streams are unbuffered at the Python layer, but `flush()` is still a useful explicit sync point.

```python
f = open("/data/live.log", "a")
f.write("started\n")
f.flush()
f.close()
```

### `f.close()`

Close the file. Always close files you are done with.

```python
f = open("/data/temp.txt", "w")
f.write("done")
f.close()
```

### `f.seek(offset[, whence])`

Move the file cursor. `whence` is:

- `0` from start (`SEEK_SET`)
- `1` from current position (`SEEK_CUR`)
- `2` from end (`SEEK_END`)

```python
with open("/data/book.txt", "r") as f:
    f.seek(10)
    print(f.read(20))
```

Read last 32 bytes of a binary file:

```python
with open("/data/blob.bin", "rb") as f:
    f.seek(-32, 2)
    print(f.read())
```

### `f.tell()`

Return current file cursor position.

```python
with open("/data/book.txt", "r") as f:
    f.read(10)
    print(f.tell())
```

## 2. `os` Module Functions for Filesystem Work

These are the main functions your file manager will call all the time.

### `os.chdir(path)`

Change current directory.

```python
import os
os.chdir("/data")
```

### `os.getcwd()`

Return current working directory.

```python
import os
print(os.getcwd())
```

### `os.listdir([dir])`

Return a list of names in a directory.

```python
import os
print(os.listdir("/"))
```

### `os.ilistdir([dir])`

Return an iterator of tuples:

`(name, type, inode[, size])`

Important values:

- `type == 0x4000` means directory
- `type == 0x8000` means regular file
- `size` may be missing on some filesystems
- file size may be `-1` if unknown

This is usually better than `os.listdir()` when building a file manager because you also get type information and sometimes size, without extra `stat()` calls.

```python
import os

for entry in os.ilistdir("/"):
    print(entry)
```

Example with labels:

```python
import os

for name, kind, inode, *rest in os.ilistdir("/"):
    size = rest[0] if rest else None
    label = "DIR" if kind == 0x4000 else "FILE"
    print(label, name, size)
```

### `os.mkdir(path)`

Create one directory.

```python
import os
os.mkdir("/data/projects")
```

Note: MicroPython does not guarantee `os.makedirs()`, so nested paths are usually created one level at a time.

### `os.remove(path)`

Delete a file.

```python
import os
os.remove("/data/old.txt")
```

### `os.rmdir(path)`

Delete an empty directory.

```python
import os
os.rmdir("/data/empty_folder")
```

### `os.rename(old_path, new_path)`

Rename a file or move it to another path on the same mounted filesystem.

```python
import os
os.rename("/data/a.txt", "/data/archive/a.txt")
```

### `os.stat(path)`

Return a 10-item tuple:

`(st_mode, st_ino, st_dev, st_nlink, st_uid, st_gid, st_size, st_atime, st_mtime, st_ctime)`

Most useful indexes for a file manager:

- `stat[0]` type/mode
- `stat[6]` size
- `stat[8]` modification time

Directory test:

```python
import os

st = os.stat("/data")
is_dir = bool(st[0] & 0x4000)
print(is_dir)
```

File size and mtime:

```python
import os

st = os.stat("/data/report.txt")
print("size:", st[6])
print("mtime:", st[8])
```

### `os.statvfs(path)`

Return filesystem information:

`(f_bsize, f_frsize, f_blocks, f_bfree, f_bavail, f_files, f_ffree, f_favail, f_flag, f_namemax)`

Most useful for free-space display:

```python
import os

st = os.statvfs("/")
block_size = st[0]
total = st[2] * block_size
free = st[3] * block_size
used = total - free
print("total:", total, "free:", free, "used:", used)
```

### `os.sync()`

Sync all mounted filesystems.

Use this after important writes, before reset, or before physically removing a storage device.

```python
import os

with open("/data/config.txt", "w") as f:
    f.write("mode=safe\n")

os.sync()
```

### `os.uname()`

Not a file operation, but useful for showing board and firmware info inside a file manager "About" page.

```python
import os
print(os.uname())
```

### Backward-compatible mount helpers in `os`

MicroPython keeps these in `os` for backward compatibility, but the preferred API is the `vfs` module:

- `os.mount(...)`
- `os.umount(...)`
- `os.VfsFat(...)`
- `os.VfsLfs1(...)`
- `os.VfsLfs2(...)`
- `os.VfsPosix(...)`

Prefer `import vfs` and use `vfs.mount(...)`.

## 3. `vfs` Module for Mounting and Formatting

Use `vfs` when your file manager needs to show mounted devices, mount an SD card, or format a block device.

### `vfs.mount(fsobj, mount_point, *, readonly=False)`

Mount a filesystem object or block device at a mount point.

```python
import vfs

# Example only: actual block device depends on port
vfs.mount(bdev, "/sd")
```

Read-only mount:

```python
import vfs
vfs.mount(bdev, "/sd", readonly=True)
```

### `vfs.mount()`

With no arguments, return all active mounts as:

`[(fsobj, mount_point), ...]`

```python
import vfs
print(vfs.mount())
```

### `vfs.umount(mount_point)`

Unmount a filesystem.

```python
import vfs
vfs.umount("/sd")
```

### `vfs.VfsFat(block_dev)`

Create a FAT filesystem object from a block device.

```python
import vfs
fat = vfs.VfsFat(bdev)
vfs.mount(fat, "/flash2")
```

### `vfs.VfsFat.mkfs(block_dev)`

Format a block device as FAT.

```python
import vfs
vfs.VfsFat.mkfs(bdev)
```

### `vfs.VfsLfs1(block_dev, readsize=32, progsize=32, lookahead=32)`

Create a littlefs v1 filesystem object.

```python
import vfs
lfs1 = vfs.VfsLfs1(bdev)
vfs.mount(lfs1, "/data")
```

### `vfs.VfsLfs1.mkfs(block_dev, readsize=32, progsize=32, lookahead=32)`

Format block device as littlefs v1.

```python
import vfs
vfs.VfsLfs1.mkfs(bdev)
```

### `vfs.VfsLfs2(block_dev, readsize=32, progsize=32, lookahead=32, mtime=True)`

Create a littlefs v2 filesystem object.

`mtime=True` lets littlefs store file modification timestamps.

```python
import vfs
lfs2 = vfs.VfsLfs2(bdev, mtime=True)
vfs.mount(lfs2, "/data")
```

### `vfs.VfsLfs2.mkfs(block_dev, readsize=32, progsize=32, lookahead=32)`

Format block device as littlefs v2.

```python
import vfs
vfs.VfsLfs2.mkfs(bdev)
```

### `vfs.VfsPosix(root=None)`

Mostly useful on the Unix port for testing file manager logic on a desktop host.

```python
import vfs
hostfs = vfs.VfsPosix("/")
print(hostfs)
```

## 4. `errno` for Safe Error Handling

MicroPython raises `OSError` for many filesystem problems. Your file manager should inspect `exc.errno`.

### Common pattern

```python
import errno
import os

try:
    os.mkdir("/data")
except OSError as exc:
    if exc.errno == errno.EEXIST:
        print("already exists")
    else:
        raise
```

Common error codes you will likely use:

- `errno.ENOENT` path not found
- `errno.EEXIST` file or folder already exists
- `errno.ENOTDIR` expected directory but got file
- `errno.EISDIR` expected file but got directory
- `errno.ENOSPC` no space left
- `errno.EPERM` operation not permitted
- `errno.EINVAL` invalid argument

Use `errno.errorcode` to map numbers back to names:

```python
import errno
print(errno.errorcode[errno.EEXIST])
```

## 5. `time` for Timestamps and Progress

### `time.localtime([secs])`

Convert a filesystem timestamp into a readable tuple.

```python
import os
import time

mtime = os.stat("/data/report.txt")[8]
print(time.localtime(mtime))
```

### `time.gmtime([secs])`

UTC version of `localtime()`.

```python
import time
print(time.gmtime())
```

### `time.mktime(tuple8)`

Convert a date tuple back into seconds since the port epoch.

```python
import time

t = (2026, 4, 19, 12, 0, 0, 6, 109)
print(time.mktime(t))
```

### `time.time()`

Get current timestamp. Useful for benchmarking copy operations or writing logs.

```python
import time

start = time.time()
# do work
print("elapsed:", time.time() - start)
```

### `time.ticks_ms()`, `time.ticks_diff()`

Better than `time.time()` for short elapsed-time measurement.

```python
import time

start = time.ticks_ms()
# do work
elapsed = time.ticks_diff(time.ticks_ms(), start)
print("ms:", elapsed)
```

## 6. `gc` for Large Files and Better Reliability

MicroPython runs on small RAM, so a file manager should avoid unnecessary allocations.

### `gc.collect()`

Run garbage collection manually.

```python
import gc
gc.collect()
```

Use it before large copy/move operations:

```python
import gc
gc.collect()
```

### `gc.mem_free()`

Check free heap RAM.

```python
import gc
print(gc.mem_free())
```

### `gc.mem_alloc()`

Check allocated heap RAM.

```python
import gc
print(gc.mem_alloc())
```

### `gc.threshold([amount])`

Tune collection frequency if your app does repeated allocations.

```python
import gc

print(gc.threshold())
gc.threshold(4096)
```

## 7. `json` for File Manager Settings and Metadata

Useful for saving favorites, recent files, bookmarks, UI settings, and cached directory info.

### `json.dump(obj, stream, separators=None)`

Write JSON directly to a file.

```python
import json

config = {"theme": "light", "home": "/data", "show_hidden": False}
with open("/data/fm_config.json", "w") as f:
    json.dump(config, f)
```

Compact JSON:

```python
import json

with open("/data/fm_config.json", "w") as f:
    json.dump({"a": 1, "b": 2}, f, separators=(",", ":"))
```

### `json.load(stream)`

Read JSON from a file.

```python
import json

with open("/data/fm_config.json", "r") as f:
    config = json.load(f)
print(config)
```

### `json.dumps(obj)` and `json.loads(str)`

Convert between Python objects and JSON strings.

```python
import json

s = json.dumps({"name": "demo"})
print(s)
print(json.loads(s))
```

## 8. `sys` Helpers That Are Useful in a File Manager

### `sys.implementation`

Check the MicroPython version at runtime.

```python
import sys
print(sys.implementation)
```

### `sys.path`

Useful if your file manager wants to show import search paths or install scripts into importable folders.

```python
import sys
print(sys.path)
```

### `sys.print_exception(exc, file=sys.stdout)`

Very useful for logging file errors to screen or to a log file.

```python
import sys

try:
    open("/missing.txt")
except Exception as exc:
    sys.print_exception(exc)
```

## 9. `io` Helpers

These are not required for normal file browsing, but they are useful for testing or buffering data in RAM.

### `io.open(...)`

Alias of built-in `open()`.

```python
import io

with io.open("/data/demo.txt", "w") as f:
    f.write("demo")
```

### `io.StringIO([string])`

In-memory text stream.

```python
import io

s = io.StringIO("hello")
print(s.read())
```

### `io.BytesIO([bytes])`

In-memory binary stream.

```python
import io

b = io.BytesIO(b"abc")
print(b.read())
```

## 10. Ready-to-Use File Manager Recipes

These recipes combine the functions above into practical operations.

### Show current directory

```python
import os
print(os.getcwd())
```

### Change directory

```python
import os
os.chdir("/data")
```

### Simple `ls`

```python
import os

for name in os.listdir("."):
    print(name)
```

### Detailed `ls -l` style listing

```python
import os
import time

for name, kind, inode, *rest in os.ilistdir("."):
    st = os.stat(name)
    mtime = time.localtime(st[8])
    label = "DIR " if kind == 0x4000 else "FILE"
    print(label, name, "size=", st[6], "mtime=", mtime)
```

### Check whether a path exists

```python
import os

def exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False

print(exists("/data/test.txt"))
```

### Check whether a path is a directory

```python
import os

def is_dir(path):
    try:
        return bool(os.stat(path)[0] & 0x4000)
    except OSError:
        return False

print(is_dir("/data"))
```

### Create directory if missing

```python
import errno
import os

def mkdir_if_needed(path):
    try:
        os.mkdir(path)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
```

### Read text file

```python
with open("/data/readme.txt", "r") as f:
    print(f.read())
```

### Write text file

```python
with open("/data/readme.txt", "w") as f:
    f.write("hello\n")
```

### Append to file

```python
with open("/data/log.txt", "a") as f:
    f.write("next line\n")
```

### Copy a small file

```python
with open("/data/src.txt", "rb") as src:
    data = src.read()

with open("/data/dst.txt", "wb") as dst:
    dst.write(data)
```

### Copy a large file efficiently

```python
import gc

buf = bytearray(512)
view = memoryview(buf)
gc.collect()

with open("/data/big.bin", "rb") as src:
    with open("/backup/big.bin", "wb") as dst:
        while True:
            n = src.readinto(buf)
            if not n:
                break
            dst.write(view[:n])
        dst.flush()
```

### Move a file

```python
import os
os.rename("/data/report.txt", "/archive/report.txt")
```

### Delete a file

```python
import os
os.remove("/data/trash.txt")
```

### Delete an empty folder

```python
import os
os.rmdir("/data/empty")
```

### Show disk usage

```python
import os

st = os.statvfs("/")
block = st[0]
total = st[2] * block
free = st[3] * block
used = total - free

print("total bytes:", total)
print("used bytes :", used)
print("free bytes :", free)
```

### Save file manager settings

```python
import json

settings = {
    "cwd": "/data",
    "sort": "name",
    "show_hidden": False,
}

with open("/data/fm_settings.json", "w") as f:
    json.dump(settings, f, separators=(",", ":"))
```

### Load file manager settings

```python
import json

with open("/data/fm_settings.json", "r") as f:
    settings = json.load(f)

print(settings)
```

### Show mounted filesystems

```python
import vfs
print(vfs.mount())
```

## 11. Important Notes for Real Devices

### 1. Paths vary by port

Examples:

- ESP32 and ESP8266 often use `/`
- Pyboard often uses `/flash`
- SD cards are often mounted at `/sd`

So your file manager should not hard-code only one root path.

### 2. `os.ilistdir()` is often better than `os.listdir()`

Use `ilistdir()` for directory browsers because it provides type information and may include size.

### 3. Use chunked copy for large files

Avoid:

```python
data = open("/big.bin", "rb").read()
```

Prefer:

```python
buf = bytearray(512)
```

with looped `readinto()` and `write()`.

### 4. Use `os.sync()` before reset or unsafe power events

This is especially important on embedded boards.

### 5. `littlefs` is usually safer than FAT for flash devices

FAT is convenient, but littlefs is generally more resilient against power loss on flash storage.

### 6. Mounting depends on a block device object

The `vfs` module is core, but the block device is often port-specific, for example:

- `bdev` on ESP8266 and ESP32
- `machine.SDCard()`
- `pyb.Flash(start=0)`
- `esp32.Partition(...)`

Your file manager can stay generic if you keep mount logic separate from file browsing logic.

## 12. Minimal Function Checklist for Your Own File Manager

If you implement only the functions below first, you already have a strong base:

- `open()`
- `read()`
- `write()`
- `readinto()`
- `seek()`
- `close()`
- `os.getcwd()`
- `os.chdir()`
- `os.ilistdir()`
- `os.mkdir()`
- `os.remove()`
- `os.rmdir()`
- `os.rename()`
- `os.stat()`
- `os.statvfs()`
- `os.sync()`
- `vfs.mount()`
- `vfs.umount()`
- `errno`
- `time.localtime()`
- `gc.collect()`
- `json.dump()` / `json.load()`

## Source Notes

This guide was derived from the MicroPython docs and source files already present in this workspace, especially:

- `docs/library/builtins.rst`
- `docs/library/io.rst`
- `docs/library/os.rst`
- `docs/library/vfs.rst`
- `docs/library/errno.rst`
- `docs/library/gc.rst`
- `docs/library/json.rst`
- `docs/library/sys.rst`
- `docs/library/time.rst`
- `docs/reference/filesystem.rst`
- `extmod/vfs.c`
- `extmod/vfs_fat_file.c`
- `extmod/vfs_lfsx_file.c`
- `extmod/vfs_posix_file.c`
