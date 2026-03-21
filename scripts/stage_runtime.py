#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path


EXTENSION_ID = "calsci-io.calsci-vscode-extension"
RUNTIME_ROOT_NAME = "runtime"


class RuntimeBuildError(RuntimeError):
    pass


def platform_key() -> str:
    if sys.platform != "linux":
        raise RuntimeBuildError("Bundled CalSci runtime packaging currently supports Linux only.")

    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "linux-x64"
    if machine in {"aarch64", "arm64"}:
        return "linux-arm64"
    raise RuntimeBuildError(f"Unsupported Linux architecture for bundled runtime: {machine}")


def python_version_tag() -> str:
    return f"python{sys.version_info.major}.{sys.version_info.minor}"


def resolve_source_site_packages() -> Path:
    version_tag = python_version_tag()
    explicit_site_packages = os.environ.get("CALSCI_SOURCE_SITE_PACKAGES")
    explicit_pyenv = os.environ.get("CALSCI_SOURCE_PYENV")

    candidates: list[Path] = []
    if explicit_site_packages:
        candidates.append(Path(explicit_site_packages).expanduser())
    if explicit_pyenv:
        candidates.append(Path(explicit_pyenv).expanduser() / "lib" / version_tag / "site-packages")

    home = Path.home()
    for code_dir in ("Code", "Code - Insiders"):
        candidates.append(home / ".config" / code_dir / "User" / "globalStorage" / EXTENSION_ID / "pyenv" / "lib" / version_tag / "site-packages")

    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    raise RuntimeBuildError(
        "No CalSci source site-packages found. Set CALSCI_SOURCE_SITE_PACKAGES or CALSCI_SOURCE_PYENV, "
        "or activate the extension once so it bootstraps its builder venv."
    )


def ensure_required_packages(site_packages: Path) -> None:
    required = ("serial", "esptool")
    missing = [name for name in required if not (site_packages / name).exists()]
    if missing:
        raise RuntimeBuildError(f"Missing required packages in {site_packages}: {', '.join(missing)}")


def ignore_stdlib(_: str, names: list[str]) -> set[str]:
    ignored = {"__pycache__", "site-packages", "dist-packages"}
    return {name for name in names if name in ignored or name.endswith((".pyc", ".pyo"))}


def ignore_site_packages(_: str, names: list[str]) -> set[str]:
    ignored_prefixes = ("pip", "setuptools", "wheel")
    ignored = {"__pycache__"}
    for name in names:
        if name in ignored or name.endswith((".pyc", ".pyo")):
            ignored.add(name)
            continue
        if name.startswith(ignored_prefixes):
            ignored.add(name)
    return ignored


def copy_entry(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, target, symlinks=False, ignore=ignore_site_packages)
        return
    shutil.copy2(source, target, follow_symlinks=True)


def stage_runtime(repo_root: Path) -> Path:
    key = platform_key()
    runtime_root = repo_root / RUNTIME_ROOT_NAME
    destination = runtime_root / key
    runtime_root.mkdir(parents=True, exist_ok=True)

    stdlib_source = Path(sysconfig.get_path("stdlib"))
    if not stdlib_source.is_dir():
        raise RuntimeBuildError(f"Python stdlib path does not exist: {stdlib_source}")

    libpython_name = sysconfig.get_config_var("LDLIBRARY")
    lib_dir = sysconfig.get_config_var("LIBDIR")
    if not libpython_name or not lib_dir:
        raise RuntimeBuildError("Could not resolve libpython shared library for bundled runtime packaging.")
    libpython_source = (Path(lib_dir) / libpython_name).resolve()
    if not libpython_source.is_file():
        raise RuntimeBuildError(f"libpython shared library not found: {libpython_source}")

    interpreter_source = Path(sys.executable).resolve()
    if not interpreter_source.is_file():
        raise RuntimeBuildError(f"Python executable not found: {interpreter_source}")

    source_site_packages = resolve_source_site_packages()
    ensure_required_packages(source_site_packages)

    temp_parent = runtime_root / ".tmp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=f"{key}-", dir=temp_parent))
    try:
        bin_dir = temp_dir / "bin"
        lib_dir_path = temp_dir / "lib"
        python_lib_dir = lib_dir_path / python_version_tag()
        site_packages_dest = python_lib_dir / "site-packages"

        bin_dir.mkdir(parents=True, exist_ok=True)
        site_packages_dest.mkdir(parents=True, exist_ok=True)

        shutil.copy2(interpreter_source, bin_dir / "python3")
        os.chmod(bin_dir / "python3", 0o755)
        python_link = bin_dir / "python"
        if python_link.exists() or python_link.is_symlink():
            python_link.unlink()
        python_link.symlink_to("python3")

        shutil.copytree(stdlib_source, python_lib_dir, symlinks=False, dirs_exist_ok=True, ignore=ignore_stdlib)
        shutil.copy2(libpython_source, lib_dir_path / libpython_source.name)
        soname_link = lib_dir_path / libpython_name
        if soname_link.name != libpython_source.name:
            if soname_link.exists() or soname_link.is_symlink():
                soname_link.unlink()
            soname_link.symlink_to(libpython_source.name)

        for entry in source_site_packages.iterdir():
            if entry.name.startswith(("pip", "setuptools", "wheel")):
                continue
            if entry.name == "__pycache__":
                continue
            copy_entry(entry, site_packages_dest / entry.name)

        manifest = {
            "platformKey": key,
            "pythonVersion": sys.version.split()[0],
            "pythonExecutable": "bin/python3",
            "sitePackages": f"lib/{python_version_tag()}/site-packages",
            "libraryPath": "lib",
            "sourceSitePackages": str(source_site_packages),
        }
        (temp_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        validate_runtime(temp_dir)

        if destination.exists():
            shutil.rmtree(destination)
        temp_dir.rename(destination)
        return destination
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def validate_runtime(runtime_dir: Path) -> None:
    manifest = json.loads((runtime_dir / "manifest.json").read_text(encoding="utf-8"))
    python_path = runtime_dir / manifest["pythonExecutable"]
    env = os.environ.copy()
    env["PYTHONHOME"] = str(runtime_dir)
    env["PYTHONPATH"] = str(runtime_dir / manifest["sitePackages"])
    env["PYTHONNOUSERSITE"] = "1"
    env["LD_LIBRARY_PATH"] = str(runtime_dir / manifest["libraryPath"])

    result = subprocess.run(
        [
            str(python_path),
            "-c",
            "import sys, serial, esptool; print(sys.executable); print(sys.prefix); print(esptool.__version__)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        details = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        raise RuntimeBuildError(f"Bundled runtime validation failed.\n{details}")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    try:
        destination = stage_runtime(repo_root)
    except RuntimeBuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    size = subprocess.run(
        ["du", "-sh", str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    if size.returncode == 0 and size.stdout.strip():
        print(size.stdout.strip())
    print(f"Bundled runtime staged at {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
