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
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import project_python
import wheel_check


class Abort(Exception):
    pass


# Set by --dry-run. Checked only at the points that change something — run, remove_dir,
# rename_aside, the config write — so the dry run walks the same decisions as a real one.
DRY_RUN = False


def log(msg: str) -> None:
    # flush: phase 1 reads this through a pipe, where stdout is block-buffered and would
    # fall behind the warnings on stderr.
    print(f"[update-python] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[update-python] WARNING: {msg}", file=sys.stderr, flush=True)


def run(cmd: list, *, check=True, cwd=None) -> int:
    label = " ".join(str(c) for c in cmd)
    if DRY_RUN:
        log(f"would run: {label}")
        return 0
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
    except json.JSONDecodeError as e:
        # Rewriting it would throw away every other setting the user keeps there.
        raise Abort(f"{cfg} is not valid JSON ({e}) — fix it, then re-run")
    if os.environ.get("PYTHON_MANAGER_DEFAULT"):
        warn("PYTHON_MANAGER_DEFAULT is set and overrides default_tag — "
             "the new default will not apply until it is removed")
    if data.get("default_tag") == minor:
        log(f"OS default already Python {minor}")
        return
    if DRY_RUN:
        log(f"would set OS default: Python {data.get('default_tag', 'none')} → {minor}")
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
    if uv and poetry:
        warn("project looks like both a uv and a poetry project — cannot tell which to use")
        return None
    if not uv and not poetry:
        return None
    return "uv" if uv else "poetry"


def read_venv_version(venv: Path) -> str | None:
    """The version the venv actually runs, asked of its own python.exe.

    Not pyvenv.cfg: its `version` is written once, when the venv is created, while a patch
    update of the base install (same directory) moves the venv along with it — a venv
    running 3.14.7 still said 3.14.4 there. None for a venv that does not start, which is
    what one whose base install is gone looks like, and that one needs rebuilding anyway.
    """
    exe = venv / "Scripts" / "python.exe"
    if not exe.exists():
        return None
    try:
        r = subprocess.run(
            [str(exe), "-I", "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.strip()
    return out if r.returncode == 0 and re.fullmatch(r"\d+\.\d+\.\d+", out) else None


def read_venv_home(venv: Path) -> str | None:
    cfg = venv / "pyvenv.cfg"
    if not cfg.exists():
        return None
    for line in cfg.read_text(encoding="utf-8").splitlines():
        key, _, val = line.partition("=")
        if key.strip() == "home":
            return val.strip()
    return None


def same_dir(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def remove_dir(path: Path) -> None:
    """Best-effort delete. Every caller re-checks and reports what survived, because
    a directory the IDE still has handles in cannot be removed until it lets go."""
    if DRY_RUN:
        if path.exists():
            log(f"would remove {path}")
        return
    shutil.rmtree(path, ignore_errors=True)


def rename_aside(path: Path) -> Path:
    """Move path → path_old so a tool that refuses to overwrite can build a fresh one.

    This does not defeat a lock: Windows will not rename a directory that still has an
    open file inside it, whatever sharing mode the holder used. Failing here with the
    OS error beats letting the caller retry against a directory that never moved.
    """
    aside = path.parent / (path.name + "_old")
    remove_dir(aside)
    if DRY_RUN:
        if path.exists():
            log(f"would move {path} → {aside.name}")
        return aside
    if path.exists():
        try:
            path.rename(aside)
        except OSError as e:
            raise Abort(f"cannot move {path} aside: {e}")
    return aside


def restore_aside(path: Path, aside: Path) -> None:
    """Undo rename_aside after a failed rebuild, so the project keeps its old, working venv
    instead of being left with nothing but path_old."""
    if not aside.exists():
        return
    remove_dir(path)  # whatever half-built venv the failed attempt left behind
    try:
        if path.exists():
            raise OSError("a partial venv is still in the way")
        aside.rename(path)
        log(f"restored the previous {path.name}")
    except OSError as e:
        warn(f"could not restore {path.name} from {aside.name} ({e}) — rename it back by hand")


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
    # --upgrade re-resolves the lock for the new Python instead of reusing old-Python pins.
    uv_sync = ["uv", "sync", "--upgrade", "--python", exe, "--no-managed-python", "--no-python-downloads"]
    if run(uv_sync, cwd=project, check=False) == 0:
        return
    # uv failed. Usually the existing .venv is in the way, so move it aside and let uv
    # build a fresh one. If something genuinely holds the directory open, the move fails
    # and says so — that is a process to close, not something a retry can fix.
    log("uv sync failed — moving .venv aside and retrying")
    venv = project / ".venv"
    venv_old = rename_aside(venv)
    try:
        run(uv_sync, cwd=project)
    except Abort:
        # The venv was not the problem after all (resolution, network, ...).
        restore_aside(venv, venv_old)
        raise
    remove_dir(venv_old)
    if venv_old.exists() and not DRY_RUN:
        warn(f"{venv_old.name} not deleted — IDE still holds handles; remove it after restarting the IDE")


def poetry_env_name(project: Path) -> str | None:
    """The cache env name Poetry gives this project, minus the "-py<X.Y>" suffix.

    Mirrors EnvManager.generate_env_name exactly. Its input is package.name, which
    poetry-core canonicalizes (PEP 503: runs of "-", "_", "." become "-"), and the hash is
    of the project's own path — so another project that shares the name but not the
    directory never has its env touched.
    """
    data = read_pyproject(project)
    name = (
        data.get("project", {}).get("name")
        or data.get("tool", {}).get("poetry", {}).get("name")
    )
    if not name:
        return None
    name = re.sub(r"[-_.]+", "-", name).lower()
    sanitized = re.sub(r'[ $`!*@"\\\r\n\t]', "_", name)[:42]
    cwd = os.path.normcase(os.path.realpath(project))
    digest = base64.urlsafe_b64encode(hashlib.sha256(cwd.encode()).digest()).decode()[:8]
    return f"{sanitized}-{digest}"


def _remove_stale_poetry_cache_envs(project: Path) -> None:
    env_name = poetry_env_name(project)
    if not env_name:
        return
    pattern = re.compile(rf"^{re.escape(env_name)}-py\d+\.\d+$")
    cache_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "pypoetry" / "Cache" / "virtualenvs"
    if not cache_dir.exists():
        return
    for entry in cache_dir.iterdir():
        if entry.is_dir() and pattern.match(entry.name):
            if not DRY_RUN:
                log(f"removing stale poetry cache env: {entry}")
            remove_dir(entry)


def sync_poetry(project: Path, exe: str) -> None:
    _remove_stale_poetry_cache_envs(project)
    venv = project / ".venv"
    venv_old = rename_aside(venv)

    # The venv belongs in the project, but `poetry config` would make that the rule for
    # every project on the machine (and --local would drop a poetry.toml into the repo).
    # An env var scopes it to the poetry calls below; later runs pick up .venv by itself.
    os.environ["POETRY_VIRTUALENVS_IN_PROJECT"] = "true"
    if run(["poetry", "env", "use", exe], cwd=project, check=False) != 0:
        restore_aside(venv, venv_old)
        raise Abort(f"poetry env use {exe} failed")

    # `poetry update`, not `install`: the lock was resolved for the old Python, and its pins
    # may have no wheels for the new one (pydantic-core on 3.15 tried to build from Rust
    # source). Re-resolving picks versions that ship wheels for the target interpreter.
    if run(["poetry", "update"], cwd=project, check=False) != 0:
        # Keep the old venv until the new one is complete — a half-installed venv is worse
        # than staying on the previous Python.
        restore_aside(venv, venv_old)
        raise Abort("poetry update failed — kept the previous .venv; review output above")

    remove_dir(venv_old)
    if venv_old.exists() and not DRY_RUN:
        warn(f"{venv_old.name} not deleted — IDE still holds handles; remove it after restarting the IDE")


def report_missing_wheels(project: Path, tool: str, target: str) -> None:
    """Warn about compiled packages the target has no wheel for — before the venv is touched,
    and in the dry run too, where it matters most. Never fatal: a machine with a compiler
    builds them, and the rebuild keeps the old venv if one does not build."""
    try:
        gaps = wheel_check.find_gaps(project, tool, target)
    except (OSError, tomllib.TOMLDecodeError, KeyError) as e:
        warn(f"wheel check skipped — cannot read the lock file ({e})")
        return
    cp, plat = wheel_check.target_tags(target)
    tag = f"{cp}-{plat}"
    hard = []
    for g in gaps:
        if g.latest is None:
            warn(f"{g.name} {g.locked}: no {tag} wheel, and PyPI could not be checked for a newer one")
            hard.append(g.name)
        elif g.latest_fits:
            log(f"{g.name} {g.locked}: no {tag} wheel; {g.latest} has one and the re-resolve can pick it")
        else:
            warn(f"{g.name}: no {tag} wheel even in the latest release ({g.latest}) — it would be built from source")
            hard.append(g.name)
    if hard:
        warn(f"building {', '.join(hard)} needs a compiler (MSVC Build Tools; Rust for some) — "
             f"without one the rebuild fails and the previous .venv is kept")


def rebuild_venv(project: Path, target: str, exe: str) -> None:
    tool = detect_venv_tool(project)
    if not tool:
        log("no uv/poetry project detected — skipping venv update")
        return
    log(f"venv tool: {tool}")
    update_tool(tool)

    # Phase 1 already picks a target the project accepts. This catches the case where no
    # release does, and keeps phase 2 honest when it is launched by hand.
    try:
        constraint = project_python.python_constraint(project)
        accepted = constraint is None or project_python.satisfies(target, constraint)
    except project_python.PyprojectError as e:
        raise Abort(f"{e} — fix it, then re-run")
    except project_python.ConstraintError as e:
        warn(f"requires-python not understood ({e}) — not checked")
        accepted = True
    if not accepted:
        warn(f"Python {target} is outside requires-python {constraint!r} — venv left alone")
        return

    venv = project / ".venv"
    current = read_venv_version(venv)
    if current and version_key(current) >= version_key(target):
        log(f"venv already on {current} — skipping")
        return
    home = read_venv_home(venv)
    if current and current.split(".")[:2] == target.split(".")[:2] and home and same_dir(home, Path(exe).parent):
        # The venv runs straight out of the install that a patch update replaces in place,
        # so it is on the new patch as soon as phase 1 is done. Only a dry run gets here: a
        # real one reads the new version off the venv and stops at the check above.
        log(f"venv on {current} runs from {home} — the patch update carries it, no rebuild")
        return

    report_missing_wheels(project, tool, target)
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
    parser.add_argument("--dry-run", action="store_true", help="report what would change, change nothing")
    parser.add_argument("--target", help="with --dry-run: the version phase 1 would install. A dry run "
                                         "installs nothing, so it runs on an older interpreter.")
    parser.add_argument("--os-default", metavar="VERSION",
                        help="the release to make the OS default (default: the venv target). Phase 1 "
                             "passes the latest one when requires-python holds the venv to an older one.")
    args = parser.parse_args(argv)
    if args.target and not args.dry_run:
        parser.error("--target is only for --dry-run; a real run targets the interpreter it runs on")

    global DRY_RUN
    DRY_RUN = args.dry_run
    project = Path(args.project).resolve()
    if args.target:
        target = args.target
        # pymanager keeps one directory per minor and updates a patch in place, so an
        # interpreter of the target's minor already sits where the target will land.
        same_minor = target.split(".")[:2] == [str(sys.version_info[0]), str(sys.version_info[1])]
        exe = sys.executable if same_minor else f"<python {target}>"
    else:
        target, exe = "%d.%d.%d" % sys.version_info[:3], sys.executable

    try:
        if sys.prefix != sys.base_prefix:
            raise Abort(
                f"running from a venv ({sys.prefix}) — rebuilding a venv with its own interpreter "
                f"cannot work. Launch with a base interpreter, or just run update_python.ps1."
            )

        # An activated venv leaks in via VIRTUAL_ENV. Poetry treats it as the project
        # env: once the stale cache env is deleted it reports it "broken" and recreates
        # .venv, and uv warns about the mismatch. Child tools must not see it.
        if os.environ.pop("VIRTUAL_ENV", None):
            log("ignoring inherited VIRTUAL_ENV")

        log(f"target: Python {target} ({exe})")
        set_default_python(args.os_default or target)
        rebuild_venv(project, target, exe)

    except Abort as e:
        warn(str(e))
        return 1

    log("dry run done — nothing was changed" if DRY_RUN else "done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
