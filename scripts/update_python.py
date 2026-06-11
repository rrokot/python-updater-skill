#!/usr/bin/env python3
r"""Update Python on Windows via the Python Install Manager (`pymanager`), then
recreate the active project's virtualenv (uv or poetry) on the new interpreter.

The Python Install Manager installs *official* python.org builds, so it tracks
the real latest release (unlike uv's bundled python-build-standalone catalog,
which lags). uv/poetry are used only to (re)build the project venv on top of
the system interpreter.

Flow:
  0. Update the Python Install Manager itself          (winget upgrade)
  1. Find the latest stable official CPython           (pymanager list --online)
  2. Install it if missing                             (pymanager install <X.Y.Z> -y)
  3. Make it the OS default                            (pymanager.json default_tag)
  4. Recreate the project's venv if it is older        (uv / poetry)

Examples (run with `py`, not `python`, on Windows)
---------------------------------------------------
    py update_python.py                 # latest stable; updates OS default + cwd project's venv
    py update_python.py 3.13            # pin a minor series
    py update_python.py -p ../svc --dry-run
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

STABLE_RE = re.compile(r"^\d+\.\d+\.\d+$")          # final release, no a/b/rc/dev suffix
VER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")          # extract X.Y.Z from any string
# Online tags look like "3.14-64", "3.14t-64" (free-threaded), "3.15-dev-32";
# the optional "t" marks a free-threaded build.
ONLINE_TAG_RE = re.compile(r"^\d+\.\d+(t)?(?:-|$)")

MANAGER_WINGET_ID = "Python.PythonInstallManager"
MANAGER_CMD = "pymanager"  # the new manager's unambiguous command (no legacy py.exe clash)


def pym(*args) -> list[str]:
    return [MANAGER_CMD, *args]


class Abort(Exception):
    """A fatal, user-facing error."""


def info(msg: str) -> None:
    print(f"[update-python] {msg}")


def warn(msg: str) -> None:
    print(f"[update-python] WARNING: {msg}", file=sys.stderr)


def run(cmd, *, cwd=None, capture=False, check=True, dry=False):
    """Run a command. Returns (returncode, stdout).

    Mutating commands pass dry=True so they are only printed in --dry-run mode.
    Read-only commands use capture=True and always execute, even in dry-run.
    """
    printable = " ".join(str(c) for c in cmd)
    if dry and not capture:
        info(f"DRY-RUN $ {printable}")
        return 0, ""
    info(f"$ {printable}")
    res = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and res.returncode != 0:
        if capture:
            sys.stderr.write(res.stdout or "")
            sys.stderr.write(res.stderr or "")
        raise Abort(f"command failed ({res.returncode}): {printable}")
    return res.returncode, (res.stdout or "")


def parse_ver(s: str | None) -> tuple[int, int, int] | None:
    m = VER_RE.search(s or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def vstr(v: tuple[int, int, int]) -> str:
    return ".".join(map(str, v))


# --------------------------------------------------------------------------- #
# Python Install Manager (pymanager)
# --------------------------------------------------------------------------- #
def ensure_manager(dry: bool) -> None:
    """Ensure the Python Install Manager is available, installing it via winget if needed."""
    if shutil.which(MANAGER_CMD):
        return
    if shutil.which("winget") is None:
        raise Abort(
            "Python Install Manager not found and winget is unavailable. Install "
            f"winget (App Installer), then re-run; or `winget install --id {MANAGER_WINGET_ID} -e`."
        )
    info("Python Install Manager not found; installing it via winget")
    run(
        ["winget", "install", "--id", MANAGER_WINGET_ID, "-e",
         "--accept-package-agreements", "--accept-source-agreements",
         "--disable-interactivity"],
        check=False, dry=dry,
    )
    if not dry and shutil.which(MANAGER_CMD) is None:
        raise Abort(
            f"installed the Python Install Manager but `{MANAGER_CMD}` is not on PATH "
            "in this session. Open a new terminal and re-run."
        )


def update_manager(dry: bool) -> None:
    """Update the Python Install Manager via winget (no-op if already current)."""
    if shutil.which("winget") is None:
        warn("winget not found; skipping Python Install Manager self-update")
        return
    # winget exits non-zero when there is nothing to upgrade; that is fine, and
    # winget prints its own result, so don't second-guess the exit code here.
    run(
        ["winget", "upgrade", "--id", MANAGER_WINGET_ID, "-e",
         "--accept-package-agreements", "--accept-source-agreements",
         "--disable-interactivity"],
        check=False, dry=dry,
    )


LEGACY_LAUNCHER_ID = "Python.Launcher"


def remove_legacy_launcher(dry: bool) -> None:
    """Uninstall the legacy 'Python Launcher' if present, so plain `py` resolves
    to the new manager (the script itself always uses `pymanager`)."""
    if shutil.which("winget") is None:
        return
    _, out = run(["winget", "list", "--id", LEGACY_LAUNCHER_ID, "-e"], capture=True, check=False)
    if LEGACY_LAUNCHER_ID not in out:
        return
    info("legacy 'Python Launcher' found; removing it")
    run(
        ["winget", "uninstall", "--id", LEGACY_LAUNCHER_ID, "-e", "--disable-interactivity"],
        check=False, dry=dry,
    )


def _online_cpython() -> list[tuple[int, int, int]]:
    """Versions of official, stable, non-free-threaded CPython builds available online."""
    _, out = run(pym("list", "--online", "-f=json"), capture=True)
    result: list[tuple[int, int, int]] = []
    for e in json.loads(out).get("versions", []):
        if e.get("company") != "PythonCore":
            continue
        m = ONLINE_TAG_RE.match(str(e.get("tag", "")))
        if not m or m.group(1) == "t":
            continue  # skip free-threaded builds
        sv = e.get("sort-version", "")
        if not STABLE_RE.match(sv):
            continue  # skip 3.15.0b2, rc, dev, ...
        v = parse_ver(sv)
        if v is not None:
            result.append(v)
    return result


def latest_stable_online() -> tuple[tuple[int, int, int], str]:
    """Newest official CPython overall. Returns (version, minor)."""
    cands = _online_cpython()
    if not cands:
        raise Abort("no matching CPython versions in `pymanager list --online`")
    v = max(cands)
    return v, f"{v[0]}.{v[1]}"


def latest_patch_of(minor: str) -> tuple[int, int, int]:
    """Newest official patch of a requested minor series, e.g. 3.13 -> 3.13.13."""
    want = tuple(int(x) for x in minor.split("."))
    patches = [v for v in _online_cpython() if v[:2] == want]
    if not patches:
        raise Abort(f"Python {minor} not found in `pymanager list --online`")
    return max(patches)


def installed_runtimes() -> list[dict]:
    code, out = run(pym("list", "-f=json"), capture=True, check=False)
    if code != 0 or not out.strip():
        return []
    try:
        return json.loads(out).get("versions", [])
    except json.JSONDecodeError:
        return []


def installed_version_of(minor: str) -> tuple[int, int, int] | None:
    """Newest installed patch of a given minor series, or None if not installed."""
    want = tuple(int(x) for x in minor.split("."))
    vers = [parse_ver(e.get("sort-version", "")) for e in installed_runtimes()]
    vers = [v for v in vers if v and v[:2] == want]
    return max(vers) if vers else None


def find_executable(minor: str) -> str | None:
    """Path to the installed interpreter for `minor` (prefer managed, newest)."""
    cands = [
        e for e in installed_runtimes()
        if str(e.get("tag", "")) == minor or str(e.get("sort-version", "")).startswith(minor + ".")
    ]
    cands.sort(key=lambda e: (0 if e.get("unmanaged") else 1, parse_ver(e.get("sort-version", "")) or (0, 0, 0)))
    return cands[-1].get("executable") if cands else None


def set_os_default(minor: str, dry: bool) -> None:
    """Make `minor` the OS default via the pymanager.json config file."""
    cfg = Path(os.environ["APPDATA"]) / "Python" / "pymanager.json"
    data: dict = {}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            warn(f"{cfg} is not valid JSON; rewriting it")
    if data.get("default_tag") == minor:
        info(f"OS default already Python {minor}")
        return
    data["default_tag"] = minor
    if dry:
        info(f"DRY-RUN would set default_tag={minor} in {cfg}")
        return
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    info(f"set OS default Python -> {minor} ({cfg.name})")


# --------------------------------------------------------------------------- #
# Project venv (uv / poetry)
# --------------------------------------------------------------------------- #
def detect_manager(project: Path) -> str | None:
    has_uv_lock = (project / "uv.lock").exists()
    has_poetry_lock = (project / "poetry.lock").exists()
    pp = project / "pyproject.toml"
    text = pp.read_text(encoding="utf-8") if pp.exists() else ""
    uv = has_uv_lock or "[tool.uv]" in text
    poetry = has_poetry_lock or "[tool.poetry]" in text
    if uv != poetry:
        return "uv" if uv else "poetry"
    # both or neither: fall back to a uniquely-present lock file
    if has_uv_lock and not has_poetry_lock:
        return "uv"
    if has_poetry_lock and not has_uv_lock:
        return "poetry"
    return None


def venv_cfg_version(venv: Path) -> tuple[int, int, int] | None:
    cfg = venv / "pyvenv.cfg"
    if not cfg.exists():
        return None
    for line in cfg.read_text(encoding="utf-8").splitlines():
        if line.split("=")[0].strip() in ("version_info", "version"):
            v = parse_ver(line)
            if v:
                return v
    return None


def current_venv_version(project: Path, manager: str) -> tuple[int, int, int] | None:
    if manager == "uv":
        return venv_cfg_version(project / ".venv")
    # poetry: the env usually lives outside the project tree.
    _, out = run(["poetry", "env", "info", "--path"], cwd=project, capture=True, check=False)
    path = out.strip()
    return venv_cfg_version(Path(path)) if path else None


def _rebuild_poetry_venv(exe: str, dry: bool) -> None:
    """Rebuild Poetry's own venv on the new Python interpreter.

    The official Poetry installer creates a dedicated venv at
    AppData/Roaming/pypoetry/venv.  When Python is updated, that venv must be
    rebuilt so Poetry itself runs on the new interpreter.
    """
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        warn("APPDATA not set; skipping Poetry venv rebuild")
        return
    poetry_venv = Path(appdata) / "pypoetry" / "venv"
    if not poetry_venv.exists():
        return  # Poetry not installed via the official installer
    info(f"rebuilding Poetry's own venv on Python {exe}")
    run([exe, "-m", "venv", "--clear", str(poetry_venv)], dry=dry)
    if not dry:
        pip = poetry_venv / "Scripts" / "pip.exe"
        run([str(pip), "install", "--upgrade", "poetry"], dry=dry)


def _poetry_virtualenvs_dir(project: Path) -> Path | None:
    """Return Poetry's virtualenvs directory, trying config first then the known default."""
    _, out = run(["poetry", "config", "virtualenvs.path"], cwd=project, capture=True, check=False)
    if out.strip():
        p = Path(out.strip())
        if p.exists():
            return p
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        default = Path(local_appdata) / "pypoetry" / "Cache" / "virtualenvs"
        if default.exists():
            return default
    return None


def _force_clear_poetry_envs(project: Path, dry: bool) -> None:
    """Directly delete Poetry virtualenv dirs for this project, bypassing Poetry commands.

    Poetry validates that the old Python executable still exists before it will
    remove or replace an env.  When that interpreter is gone (e.g. Python 3.13
    was uninstalled), every `poetry env …` call fails.  We break the cycle by
    deleting the directories ourselves.
    """
    venvs_dir = _poetry_virtualenvs_dir(project)
    if venvs_dir is None:
        warn("could not locate Poetry's virtualenvs directory; skipping force-clear")
        return
    project_slug = re.sub(r"[^a-zA-Z0-9]", "-", project.name.lower())
    found = False
    for d in venvs_dir.iterdir():
        if not d.is_dir():
            continue
        if d.name.lower().startswith(project_slug + "-"):
            found = True
            if dry:
                info(f"DRY-RUN would remove stale venv: {d}")
            else:
                shutil.rmtree(d, ignore_errors=True)
                info(f"removed stale venv: {d}")
    if not found:
        info("no stale Poetry venvs found to remove")


def recreate_venv(project: Path, manager: str, exe: str, dry: bool) -> None:
    if manager == "uv":
        code, _ = run(
            ["uv", "sync", "--python", exe, "--no-managed-python", "--no-python-downloads"],
            cwd=project, dry=dry, check=False,
        )
        if code != 0:
            warn("`uv sync` failed; creating the venv directly")
            run(
                ["uv", "venv", "--python", exe, "--no-managed-python", "--no-python-downloads", "--clear"],
                cwd=project, dry=dry,
            )
    else:
        # Rebuild Poetry's own venv on the new Python before doing anything else.
        _rebuild_poetry_venv(exe, dry)
        # Force-delete stale project venv dirs: Poetry refuses to remove/replace
        # an env whose Python interpreter no longer exists at the registered path.
        _force_clear_poetry_envs(project, dry)
        code, _ = run(["poetry", "env", "use", exe], cwd=project, dry=dry, check=False)
        if code != 0:
            raise Abort(f"`poetry env use {exe}` failed (exit {code})")
        code, _ = run(["poetry", "install"], cwd=project, dry=dry, check=False)
        if code != 0:
            warn("`poetry install` reported errors; review the output above")


def update_project_venv(project: Path, target: tuple[int, int, int], minor: str, dry: bool) -> None:
    manager = detect_manager(project)
    if manager is None:
        info("no uv/poetry project detected here; skipping venv update")
        return
    info(f"project venv manager: {manager}")
    current = current_venv_version(project, manager)
    if current is not None and current >= target:
        info(f"venv already on {vstr(current)} (>= target); skipping")
        return
    if current:
        info(f"venv is {vstr(current)}; updating to {vstr(target)}")
    else:
        info("no existing venv found; creating it")
    exe = find_executable(minor)
    if not exe:
        if dry:
            info(f"DRY-RUN would recreate venv on Python {minor} (not installed yet in dry-run)")
            return
        raise Abort(f"could not locate an installed Python {minor} interpreter")
    info(f"using interpreter: {exe}")
    recreate_venv(project, manager, exe, dry)


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Update Python via the Python Install Manager and recreate the project venv."
    )
    parser.add_argument("version", nargs="?", help="target minor, e.g. 3.13 (default: latest stable)")
    parser.add_argument("-p", "--project", default=".", help="project directory (default: cwd)")
    parser.add_argument("--dry-run", action="store_true", help="print actions without changing anything")
    args = parser.parse_args(argv)

    if os.name != "nt":
        warn("this script targets Windows (Python Install Manager); behavior elsewhere is undefined")

    project = Path(args.project).resolve()

    try:
        # 0. Ensure the manager exists (install if missing), update it, and
        #    clear out the legacy launcher so `py` is the new manager.
        ensure_manager(args.dry_run)
        update_manager(args.dry_run)
        remove_legacy_launcher(args.dry_run)

        # 1. Resolve the target version.
        if args.version:
            minor = args.version
            target = latest_patch_of(minor)
        else:
            target, minor = latest_stable_online()
        info(f"target Python: {vstr(target)} (series {minor})")

        # 2. Install it if the target series is missing or behind.
        have = installed_version_of(minor)
        if have and have >= target:
            info(f"Python {minor} already at {vstr(have)} (>= target); skipping install")
        else:
            if have:
                info(f"Python {minor} is {vstr(have)}; installing {vstr(target)}")
            run(pym("install", vstr(target), "-y"), dry=args.dry_run)

        # 3. Make it the OS default.
        set_os_default(minor, args.dry_run)

        # 4. Recreate the project venv if outdated.
        update_project_venv(project, target, minor, args.dry_run)
    except Abort as e:
        warn(str(e))
        return 1

    info("done" + (" (dry-run, nothing changed)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
