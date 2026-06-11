#!/usr/bin/env python3
"""Update the system Python to the latest stable release via the Python Install Manager,
then recreate the project's virtualenv on the new interpreter.

Usage:
    py update_python.py
    py update_python.py -p ../other-project
    py update_python.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PYMANAGER = "pymanager"
PYMANAGER_WINGET_ID = "Python.PythonInstallManager"
LEGACY_LAUNCHER_WINGET_ID = "Python.Launcher"

WINGET_FLAGS = ["--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]


def winget(*args) -> list[str]:
    return ["winget", *args, *WINGET_FLAGS]


class Abort(Exception):
    pass


def log(msg: str) -> None:
    print(f"[update-python] {msg}")


def warn(msg: str) -> None:
    print(f"[update-python] WARNING: {msg}", file=sys.stderr)


def run(cmd: list, *, capture=False, check=True, cwd=None, dry=False) -> tuple[int, str]:
    label = " ".join(str(c) for c in cmd)
    if dry and not capture:
        log(f"DRY-RUN $ {label}")
        return 0, ""
    log(f"$ {label}")
    r = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and r.returncode != 0:
        if capture:
            sys.stderr.write(r.stdout or "")
            sys.stderr.write(r.stderr or "")
        raise Abort(f"command failed ({r.returncode}): {label}")
    return r.returncode, r.stdout or ""


def version_key(ver: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", ver)[:3])


# ── pymanager ────────────────────────────────────────────────────────────────

def setup_pymanager(dry: bool) -> None:
    """Install pymanager if missing, update it, and remove the legacy Python Launcher."""
    if not shutil.which(PYMANAGER):
        log("pymanager not found — installing via winget")
        run(winget("install", "--id", PYMANAGER_WINGET_ID, "-e"), check=False, dry=dry)
        if not dry and not shutil.which(PYMANAGER):
            raise Abort("pymanager installed but not on PATH — open a new terminal and re-run")

    run(winget("upgrade", "--id", PYMANAGER_WINGET_ID, "-e"), check=False, dry=dry)
    _, out = run(["winget", "list", "--id", LEGACY_LAUNCHER_WINGET_ID, "-e"], capture=True, check=False)
    if LEGACY_LAUNCHER_WINGET_ID in out:
        log("removing legacy Python Launcher")
        run(winget("uninstall", "--id", LEGACY_LAUNCHER_WINGET_ID, "-e"), check=False, dry=dry)


def latest_stable_version() -> str:
    """Return the newest stable CPython version string, e.g. '3.14.6'."""
    _, out = run([PYMANAGER, "list", "--online", "-f=json"], capture=True)
    versions = [
        e["sort-version"]
        for e in json.loads(out).get("versions", [])
        if e.get("company") == "PythonCore"
        and not re.match(r"^\d+\.\d+t", str(e.get("tag", "")))
        and re.match(r"^\d+\.\d+\.\d+$", e.get("sort-version", ""))
    ]
    if not versions:
        raise Abort("no stable CPython found in pymanager online catalog")
    return max(versions, key=version_key)


def find_installed_exe(version: str) -> str | None:
    """Return the executable path for a pymanager-installed `version`, or None."""
    _, out = run([PYMANAGER, "list", "-f=json"], capture=True, check=False)
    try:
        entries = json.loads(out).get("versions", [])
    except json.JSONDecodeError:
        return None
    for e in entries:
        if (e.get("company") == "PythonCore"
                and not e.get("unmanaged")
                and e.get("sort-version") == version):
            return e.get("executable")
    return None


def set_default_python(version: str, dry: bool) -> None:
    minor = ".".join(version.split(".")[:2])
    cfg = Path(os.environ["APPDATA"]) / "Python" / "pymanager.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    except json.JSONDecodeError:
        data = {}
    if data.get("default_tag") == minor:
        log(f"OS default already Python {minor}")
        return
    if dry:
        log(f"DRY-RUN would set OS default to Python {minor}")
        return
    data["default_tag"] = minor
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log(f"OS default → Python {minor}")


# ── venv ─────────────────────────────────────────────────────────────────────

def detect_venv_tool(project: Path) -> str | None:
    text = (project / "pyproject.toml").read_text(encoding="utf-8") if (project / "pyproject.toml").exists() else ""
    uv = (project / "uv.lock").exists() or "[tool.uv]" in text
    poetry = (project / "poetry.lock").exists() or "[tool.poetry]" in text
    if uv == poetry:
        return None
    return "uv" if uv else "poetry"


def read_venv_version(venv: Path) -> str | None:
    """Read Python version from pyvenv.cfg, return as '3.14.6' or None."""
    cfg = venv / "pyvenv.cfg"
    if not cfg.exists():
        return None
    for line in cfg.read_text(encoding="utf-8").splitlines():
        key, _, val = line.partition("=")
        if key.strip() in ("version", "version_info"):
            m = re.search(r"\d+\.\d+\.\d+", val)
            return m.group() if m else None
    return None


def remove_dir(path: Path, dry: bool) -> bool:
    """Remove a directory via PowerShell to handle IDE file locks."""
    if not path.exists():
        return True
    if dry:
        log(f"DRY-RUN would remove {path}")
        return True
    for attempt in range(2):
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Remove-Item -Recurse -Force '{path}'"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if not path.exists():
            return True
        if attempt == 0:
            import time
            time.sleep(1)
    warn(f"could not remove {path} — close the IDE and delete manually")
    return False


def sync_uv(project: Path, exe: str, dry: bool) -> None:
    uv_sync = ["uv", "sync", "--python", exe, "--no-managed-python", "--no-python-downloads"]
    code, _ = run(uv_sync, cwd=project, check=False, dry=dry)
    if code != 0:
        warn("uv sync failed — removing .venv and retrying")
        if not remove_dir(project / ".venv", dry):
            raise Abort(".venv is locked — close the IDE and retry")
        run(uv_sync, cwd=project, dry=dry)


def _remove_stale_poetry_cache_envs(project: Path, dry: bool) -> None:
    """Directly delete poetry cache virtualenvs for this project by name prefix."""
    import tomllib  # Python 3.11+
    pyproject = project / "pyproject.toml"
    if not pyproject.exists():
        return
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)
    project_name = (
        data.get("tool", {}).get("poetry", {}).get("name")
        or data.get("project", {}).get("name")
        or ""
    )
    if not project_name:
        return
    # Poetry normalises the project name: lowercase, replace non-alphanumeric with -
    normalised = re.sub(r"[^a-z0-9]+", "-", project_name.lower()).strip("-")
    cache_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "pypoetry" / "Cache" / "virtualenvs"
    if not cache_dir.exists():
        return
    for entry in cache_dir.iterdir():
        if entry.is_dir() and entry.name.startswith(normalised + "-"):
            log(f"removing stale poetry cache env: {entry}")
            remove_dir(entry, dry)


def sync_poetry(project: Path, exe: str, dry: bool) -> None:
    # Poetry itself runs inside its own venv — rebuild it on the new interpreter first
    poetry_home = Path(os.environ["APPDATA"]) / "pypoetry" / "venv"
    if poetry_home.exists():
        log("rebuilding Poetry's own venv")
        remove_dir(poetry_home, dry)
        run([exe, "-m", "venv", str(poetry_home)], dry=dry)
        if not dry:
            run([str(poetry_home / "Scripts" / "pip.exe"), "install", "--upgrade", "poetry"], dry=dry)

    # Remove all poetry-managed envs for this project.
    # poetry env remove --all fails when the env's Python is missing, so we also
    # directly purge matching entries from the poetry virtualenvs cache.
    run(["poetry", "env", "remove", "--all"], cwd=project, check=False, dry=dry)
    _remove_stale_poetry_cache_envs(project, dry)
    # Also remove in-project .venv directly in case it's broken/unregistered
    inproject_venv = project / ".venv"
    if inproject_venv.exists():
        remove_dir(inproject_venv, dry)
    run(["poetry", "config", "virtualenvs.in-project", "true"], cwd=project, dry=dry)
    code, _ = run(["poetry", "env", "use", exe], cwd=project, check=False, dry=dry)
    if code != 0:
        raise Abort(f"poetry env use {exe} failed")
    code, _ = run(["poetry", "install"], cwd=project, check=False, dry=dry)
    if code != 0:
        warn("poetry install reported errors — review output above")


def rebuild_venv(project: Path, target: str, exe: str, dry: bool) -> None:
    tool = detect_venv_tool(project)
    if not tool:
        log("no uv/poetry project detected — skipping venv update")
        return
    log(f"venv tool: {tool}")

    current = read_venv_version(project / ".venv")
    if current and version_key(current) >= version_key(target):
        log(f"venv already on {current} — skipping")
        return
    log(f"updating venv: {current or 'none'} → {target}")

    if tool == "uv":
        sync_uv(project, exe, dry)
    else:
        sync_poetry(project, exe, dry)


# ── entry point ──────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Update Python to the latest stable release and rebuild the project venv."
    )
    parser.add_argument("-p", "--project", default=".", help="project directory (default: cwd)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    project = Path(args.project).resolve()
    dry = args.dry_run

    try:
        setup_pymanager(dry)

        target = latest_stable_version()
        log(f"latest stable: Python {target}")

        exe = find_installed_exe(target)
        if exe:
            log(f"already installed: {exe}")
        else:
            log(f"installing Python {target}")
            run([PYMANAGER, "install", target, "-y"], dry=dry)
            exe = find_installed_exe(target)

        set_default_python(target, dry)
        rebuild_venv(project, target, exe or f"python{target}", dry)

    except Abort as e:
        warn(str(e))
        return 1

    log("done" + (" (dry-run)" if dry else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
