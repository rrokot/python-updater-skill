#!/usr/bin/env python3
"""Phase 2 of the python-updater skill: make the running interpreter the OS default
and rebuild the project's virtualenv on it.

Not an entry point. update_python.ps1 installs the interpreter — from PowerShell,
because pymanager replaces an install by deleting its directory and Windows refuses
while its files are open — and then launches this script with the interpreter it just
installed. Nothing here installs anything, so nothing here requires a Python to be closed.

The target version and executable come from the interpreter running this script rather
than from flags or from pymanager: launched with the right python.exe, phase 2 cannot
disagree with phase 1.

Usage:
    powershell -File update_python.ps1
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


class Abort(Exception):
    pass


def log(msg: str) -> None:
    print(f"[update-python] {msg}")


def warn(msg: str) -> None:
    print(f"[update-python] WARNING: {msg}", file=sys.stderr)


def run(cmd: list, *, check=True, cwd=None) -> int:
    label = " ".join(str(c) for c in cmd)
    log(f"$ {label}")
    r = subprocess.run(cmd, cwd=cwd)
    if check and r.returncode != 0:
        raise Abort(f"command failed ({r.returncode}): {label}")
    return r.returncode


def version_key(ver: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", ver)[:3])


# ── OS default ───────────────────────────────────────────────────────────────

def set_default_python(version: str) -> None:
    minor = ".".join(version.split(".")[:2])
    cfg = Path(os.environ["APPDATA"]) / "Python" / "pymanager.json"
    try:
        data = json.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    except json.JSONDecodeError:
        data = {}
    if data.get("default_tag") == minor:
        log(f"OS default already Python {minor}")
        return
    data["default_tag"] = minor
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    log(f"OS default → Python {minor}")


# ── venv ─────────────────────────────────────────────────────────────────────

def read_pyproject(project: Path) -> dict:
    try:
        with open(project / "pyproject.toml", "rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return {}


def detect_venv_tool(project: Path) -> str | None:
    tool = read_pyproject(project).get("tool", {})
    uv = (project / "uv.lock").exists() or "uv" in tool
    poetry = (project / "poetry.lock").exists() or "poetry" in tool
    if uv == poetry:
        return None
    return "uv" if uv else "poetry"


def read_venv_version(venv: Path) -> str | None:
    cfg = venv / "pyvenv.cfg"
    if not cfg.exists():
        return None
    for line in cfg.read_text(encoding="utf-8").splitlines():
        key, _, val = line.partition("=")
        if key.strip() in ("version", "version_info"):
            m = re.search(r"\d+\.\d+\.\d+", val)
            return m.group() if m else None
    return None


def remove_dir(path: Path) -> None:
    """Best-effort delete. Every caller re-checks and reports what survived, because
    a directory the IDE still has handles in cannot be removed until it lets go."""
    shutil.rmtree(path, ignore_errors=True)


def rename_aside(path: Path) -> Path:
    """Move path → path_old so a tool that refuses to overwrite can build a fresh one.

    This does not defeat a lock: Windows will not rename a directory that still has an
    open file inside it, whatever sharing mode the holder used. Failing here with the
    OS error beats letting the caller retry against a directory that never moved.
    """
    aside = path.parent / (path.name + "_old")
    remove_dir(aside)
    if path.exists():
        try:
            path.rename(aside)
        except OSError as e:
            raise Abort(f"cannot move {path} aside: {e}")
    return aside


def update_tool(tool: str) -> None:
    """Freshen the venv manager itself. Runs whether or not the venv needs rebuilding,
    and is never fatal — a stale uv or poetry is no reason to abandon a Python update."""
    if tool == "uv":
        # A standalone-installed uv updates itself; one installed through pip or winget
        # refuses, and that refusal is not an error worth stopping for.
        if run(["uv", "self", "update"], check=False) != 0:
            warn("uv self update failed — continuing with the installed uv")
        return

    # Poetry is upgraded in place. Tearing its venv down and rebuilding it on the new
    # interpreter was tried and dropped: the venv does not break in the first place — it
    # names its install by path, a patch bump replaces that directory with an ABI-compatible
    # one, and a minor bump leaves it alone — while the teardown left the machine with no
    # poetry at all whenever the reinstall could not reach PyPI.
    poetry_pip = Path(os.environ["APPDATA"]) / "pypoetry" / "venv" / "Scripts" / "pip.exe"
    if poetry_pip.exists():
        log("upgrading Poetry")
        if run([str(poetry_pip), "install", "--upgrade", "poetry"], check=False) != 0:
            warn("Poetry upgrade failed — continuing with the installed version")


def sync_uv(project: Path, exe: str) -> None:
    uv_sync = ["uv", "sync", "--python", exe, "--no-managed-python", "--no-python-downloads"]
    if run(uv_sync, cwd=project, check=False) == 0:
        return
    # uv failed. Usually the existing .venv is in the way, so move it aside and let uv
    # build a fresh one. If something genuinely holds the directory open, the move fails
    # and says so — that is a process to close, not something a retry can fix.
    log("uv sync failed — moving .venv aside and retrying")
    venv_old = rename_aside(project / ".venv")
    run(uv_sync, cwd=project)
    remove_dir(venv_old)
    if venv_old.exists():
        warn(f"{venv_old.name} not deleted — IDE still holds handles; remove it after restarting the IDE")


def _remove_stale_poetry_cache_envs(project: Path) -> None:
    data = read_pyproject(project)
    project_name = (
        data.get("tool", {}).get("poetry", {}).get("name")
        or data.get("project", {}).get("name")
        or ""
    )
    if not project_name:
        return
    normalised = re.sub(r"[^a-z0-9]+", "-", project_name.lower()).strip("-")
    cache_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "pypoetry" / "Cache" / "virtualenvs"
    if not cache_dir.exists():
        return
    for entry in cache_dir.iterdir():
        if entry.is_dir() and entry.name.startswith(normalised + "-"):
            log(f"removing stale poetry cache env: {entry}")
            remove_dir(entry)


def sync_poetry(project: Path, exe: str) -> None:
    _remove_stale_poetry_cache_envs(project)
    venv_old = rename_aside(project / ".venv")

    run(["poetry", "config", "virtualenvs.in-project", "true"], cwd=project)
    if run(["poetry", "env", "use", exe], cwd=project, check=False) != 0:
        raise Abort(f"poetry env use {exe} failed")

    remove_dir(venv_old)
    if venv_old.exists():
        warn(f"{venv_old.name} not deleted — IDE still holds handles; remove it after restarting the IDE")

    if run(["poetry", "install"], cwd=project, check=False) != 0:
        warn("poetry install reported errors — review output above")


def rebuild_venv(project: Path, target: str, exe: str) -> None:
    tool = detect_venv_tool(project)
    if not tool:
        log("no uv/poetry project detected — skipping venv update")
        return
    log(f"venv tool: {tool}")
    update_tool(tool)

    current = read_venv_version(project / ".venv")
    if current and version_key(current) >= version_key(target):
        log(f"venv already on {current} — skipping")
        return
    log(f"updating venv: {current or 'none'} → {target}")

    if tool == "uv":
        sync_uv(project, exe)
    else:
        sync_poetry(project, exe)


# ── entry point ──────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Make the running interpreter the OS default and rebuild the project venv "
                    "(phase 2 of update_python.ps1).",
    )
    parser.add_argument("-p", "--project", default=".", help="project directory (default: cwd)")
    args = parser.parse_args(argv)

    project = Path(args.project).resolve()
    target = "%d.%d.%d" % sys.version_info[:3]
    exe = sys.executable

    try:
        if sys.prefix != sys.base_prefix:
            raise Abort(
                f"running from a venv ({sys.prefix}) — rebuilding a venv with its own interpreter "
                f"cannot work. Launch with a base interpreter, or just run update_python.ps1."
            )

        log(f"target: Python {target} ({exe})")
        set_default_python(target)
        rebuild_venv(project, target, exe)

    except Abort as e:
        warn(str(e))
        return 1

    log("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
