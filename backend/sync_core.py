from __future__ import annotations

import os
import posixpath
from pathlib import Path
from typing import Any


def normalize_remote_folder(remote_folder: str) -> str:
    text = str(remote_folder).strip().replace("\\", "/")
    if not text:
        raise ValueError("Remote folder is required.")
    if not text.startswith("/"):
        text = "/" + text
    normalized = posixpath.normpath(text)
    if normalized in ("", "."):
        raise ValueError("Remote folder is invalid.")
    return normalized


def sync_device_relative_path(remote_path: str) -> str:
    normalized = posixpath.normpath(str(remote_path).strip().replace("\\", "/"))
    if normalized in ("", ".", "/"):
        return ""
    return normalized.lstrip("/")


def sync_device_absolute_path(remote_path: str) -> str:
    normalized = posixpath.normpath(str(remote_path).strip().replace("\\", "/"))
    if normalized in ("", "."):
        return "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def fnv1a32_bytes(data: bytes) -> str:
    value = 2166136261
    for byte in data:
        value ^= byte
        value = (value * 16777619) & 0xFFFFFFFF
    return f"{value:08x}"


def compute_local_file_signature(local_path: Path, chunk_size: int = 4096) -> str:
    value = 2166136261
    with local_path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            for byte in chunk:
                value ^= byte
                value = (value * 16777619) & 0xFFFFFFFF
    return f"{value:08x}"


def should_skip_sync_dir(name: str) -> bool:
    return name.startswith(".") or name == "__pycache__"


def should_skip_sync_file(relative_path: Path) -> bool:
    if any(part.startswith(".") for part in relative_path.parts):
        return True
    name = relative_path.name
    return name in {".gitignore", ".gitattributes", ".DS_Store", "Thumbs.db"} or name.endswith((".pyc", ".pyo"))


def scan_local_folder(local_folder: str, remote_folder: str) -> tuple[Path, list[str], list[dict[str, Any]]]:
    local_root = Path(local_folder).expanduser().resolve()
    if not local_root.exists():
        raise FileNotFoundError(f"Local folder not found: {local_root}")
    if not local_root.is_dir():
        raise NotADirectoryError(f"Path is not a folder: {local_root}")

    normalized_remote = normalize_remote_folder(remote_folder)
    directories: list[str] = [normalized_remote]
    files: list[dict[str, Any]] = []

    for current_root, dir_names, file_names in os.walk(local_root, topdown=True):
        dir_names[:] = [name for name in sorted(dir_names) if not should_skip_sync_dir(name)]
        current_path = Path(current_root).resolve()
        relative_root = current_path.relative_to(local_root)
        remote_root = normalized_remote if relative_root == Path(".") else posixpath.join(normalized_remote, *relative_root.parts)
        if remote_root != normalized_remote:
            directories.append(remote_root)

        for file_name in sorted(file_names):
            local_path = current_path / file_name
            if local_path.is_symlink():
                continue
            relative_path_obj = local_path.relative_to(local_root)
            if should_skip_sync_file(relative_path_obj):
                continue
            relative_path = relative_path_obj.as_posix()
            stat_result = local_path.stat()
            files.append(
                {
                    "local_path": local_path,
                    "relative_path": relative_path,
                    "remote_path": posixpath.join(remote_root, file_name),
                    "size_bytes": int(stat_result.st_size),
                    "modified_time": float(stat_result.st_mtime),
                }
            )

    return local_root, directories, files


def build_sync_directory_plan(remote_root: str, files: list[dict[str, Any]]) -> list[str]:
    normalized_root = normalize_remote_folder(remote_root)
    required = {normalized_root}
    for file_info in files:
        remote_path = str(file_info["remote_path"])
        parent = posixpath.dirname(remote_path)
        while parent and parent != "/":
            required.add(parent)
            if parent == normalized_root:
                break
            next_parent = posixpath.dirname(parent)
            if next_parent == parent:
                break
            parent = next_parent
    return sorted(required, key=lambda remote_dir: (remote_dir.count("/"), remote_dir))


def build_sync_plan(
    files: list[dict[str, Any]],
    remote_sizes: dict[str, int],
    delete_extraneous: bool,
    signature_matches: set[str] | None = None,
    size_fallback_paths: set[str] | None = None,
) -> tuple[list[str], list[dict[str, Any]], list[str], list[str]]:
    local_map = {str(file_info["remote_path"]): file_info for file_info in files}
    unchanged: list[str] = []
    to_upload: list[dict[str, Any]] = []

    for remote_path, file_info in sorted(local_map.items()):
        local_size = int(file_info["size_bytes"])
        remote_size = remote_sizes.get(remote_path)
        if remote_size == local_size:
            if signature_matches is None:
                unchanged.append(remote_path)
                continue
            if remote_path in signature_matches:
                unchanged.append(remote_path)
                continue
            if size_fallback_paths is not None and remote_path in size_fallback_paths:
                unchanged.append(remote_path)
                continue
        to_upload.append(file_info)

    extra_remote = sorted(remote_path for remote_path in remote_sizes if remote_path not in local_map)
    to_delete = extra_remote if delete_extraneous else []
    return unchanged, to_upload, to_delete, extra_remote
