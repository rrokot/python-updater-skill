#!/usr/bin/env python3
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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import project_python
import wheel_check


class Abort(Exception):
    pass


def log(message: str) -> None:
    print(f"[update-python] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[update-python] WARNING: {message}", file=sys.stderr, flush=True)


def run_command(
    command: list[str],
    *,
    dry_run: bool,
    check: bool = True,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> int:
    command_line = " ".join(command)
    if dry_run:
        log(f"would run: {command_line}")
        return 0
    log(f"$ {command_line}")
    result = subprocess.run(command, cwd=cwd, env=env)
    if check and result.returncode != 0:
        raise Abort(f"command failed ({result.returncode}): {command_line}")
    return result.returncode


def release_version_parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def minor_version_tag(version: str) -> str:
    return ".".join(version.split(".")[:2])


def set_os_default_python(version: str, *, dry_run: bool) -> None:
    minor_tag = minor_version_tag(version)
    config_path = Path(os.environ["APPDATA"]) / "Python" / "pymanager.json"
    try:
        settings = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    except json.JSONDecodeError as error:
        raise Abort(f"{config_path} is not valid JSON ({error}) — fix it, then re-run")
    if os.environ.get("PYTHON_MANAGER_DEFAULT"):
        warn("PYTHON_MANAGER_DEFAULT is set and overrides default_tag — "
             "the new default will not apply until it is removed")
    if settings.get("default_tag") == minor_tag:
        log(f"OS default already Python {minor_tag}")
        return
    if dry_run:
        log(f"would set OS default: Python {settings.get('default_tag', 'none')} → {minor_tag}")
        return
    settings["default_tag"] = minor_tag
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    log(f"OS default → Python {minor_tag}")


def read_pyproject(project: Path) -> dict:
    try:
        with (project / "pyproject.toml").open("rb") as pyproject:
            return tomllib.load(pyproject)
    except (FileNotFoundError, tomllib.TOMLDecodeError):
        return {}


def detect_venv_tool(project: Path) -> str | None:
    tool_settings = read_pyproject(project).get("tool", {})
    uses_uv = (project / "uv.lock").exists() or "uv" in tool_settings
    uses_poetry = (project / "poetry.lock").exists() or "poetry" in tool_settings
    if uses_uv and uses_poetry:
        warn("project looks like both a uv and a poetry project — cannot tell which to use")
        return None
    if not uses_uv and not uses_poetry:
        return None
    return "uv" if uses_uv else "poetry"


def read_running_venv_version(venv: Path) -> str | None:
    venv_python = venv / "Scripts" / "python.exe"
    if not venv_python.exists():
        return None
    print_version = "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    try:
        result = subprocess.run(
            [str(venv_python), "-I", "-c", print_version],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    version = result.stdout.strip()
    return version if result.returncode == 0 and re.fullmatch(r"\d+\.\d+\.\d+", version) else None


def read_venv_base_install(venv: Path) -> str | None:
    config_path = venv / "pyvenv.cfg"
    if not config_path.exists():
        return None
    for line in config_path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == "home":
            return value.strip()
    return None


def same_directory(first: str | Path, second: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def try_remove_directory(path: Path, *, dry_run: bool) -> None:
    if dry_run:
        if path.exists():
            log(f"would remove {path}")
        return
    shutil.rmtree(path, ignore_errors=True)


def backup_venv(venv: Path, *, dry_run: bool) -> Path:
    backup = venv.with_name(f"{venv.name}_old")
    try_remove_directory(backup, dry_run=dry_run)
    if not venv.exists():
        return backup
    if dry_run:
        log(f"would move {venv} → {backup.name}")
        return backup
    try:
        venv.rename(backup)
    except OSError as error:
        raise Abort(f"cannot move {venv} aside: {error}")
    return backup


def restore_venv_backup(venv: Path, backup: Path) -> None:
    if not backup.exists():
        return
    try_remove_directory(venv, dry_run=False)
    try:
        if venv.exists():
            raise OSError("a partial venv is still in the way")
        backup.rename(venv)
        log(f"restored the previous {venv.name}")
    except OSError as error:
        warn(f"could not restore {venv.name} from {backup.name} ({error}) — rename it back by hand")


@contextmanager
def rebuild_with_venv_backup(venv: Path, *, dry_run: bool) -> Iterator[None]:
    backup = backup_venv(venv, dry_run=dry_run)
    try:
        yield
    except Abort:
        if not dry_run:
            restore_venv_backup(venv, backup)
        raise
    try_remove_directory(backup, dry_run=dry_run)
    if backup.exists() and not dry_run:
        warn(f"{backup.name} not deleted — IDE still holds handles; remove it after restarting the IDE")


def try_update_venv_tool(tool: str, *, dry_run: bool) -> None:
    if tool == "uv":
        if run_command(["uv", "self", "update"], dry_run=dry_run, check=False) != 0:
            warn("uv self update failed — continuing with the installed uv")
        return

    poetry_pip = Path(os.environ["APPDATA"]) / "pypoetry" / "venv" / "Scripts" / "pip.exe"
    if poetry_pip.exists():
        log("upgrading Poetry")
        upgrade_poetry = [str(poetry_pip), "install", "--upgrade", "poetry"]
        if run_command(upgrade_poetry, dry_run=dry_run, check=False) != 0:
            warn("Poetry upgrade failed — continuing with the installed version")


def sync_uv_venv(project: Path, python_executable: str, *, dry_run: bool) -> None:
    sync_command = [
        "uv", "sync", "--upgrade", "--python", python_executable,
        "--no-managed-python", "--no-python-downloads",
    ]
    if run_command(sync_command, cwd=project, dry_run=dry_run, check=False) == 0:
        return
    log("uv sync failed — moving .venv aside and retrying")
    with rebuild_with_venv_backup(project / ".venv", dry_run=dry_run):
        run_command(sync_command, cwd=project, dry_run=dry_run)


def poetry_cache_env_base_name(project: Path) -> str | None:
    pyproject = read_pyproject(project)
    package_name = (
        pyproject.get("project", {}).get("name")
        or pyproject.get("tool", {}).get("poetry", {}).get("name")
    )
    if not package_name:
        return None
    canonical_name = re.sub(r"[-_.]+", "-", package_name).lower()
    sanitized_name = re.sub(r'[ $`!*@"\\\r\n\t]', "_", canonical_name)[:42]
    normalized_project_path = os.path.normcase(os.path.realpath(project))
    project_digest = hashlib.sha256(normalized_project_path.encode()).digest()
    project_hash = base64.urlsafe_b64encode(project_digest).decode()[:8]
    return f"{sanitized_name}-{project_hash}"


def remove_stale_poetry_cache_envs(project: Path, *, dry_run: bool) -> None:
    base_name = poetry_cache_env_base_name(project)
    if not base_name:
        return
    project_env_name = re.compile(rf"^{re.escape(base_name)}-py\d+\.\d+$")
    cache_dir = Path(os.environ.get("LOCALAPPDATA", "")) / "pypoetry" / "Cache" / "virtualenvs"
    if not cache_dir.exists():
        return
    for entry in cache_dir.iterdir():
        if entry.is_dir() and project_env_name.match(entry.name):
            if not dry_run:
                log(f"removing stale poetry cache env: {entry}")
            try_remove_directory(entry, dry_run=dry_run)


def sync_poetry_venv(project: Path, python_executable: str, *, dry_run: bool) -> None:
    remove_stale_poetry_cache_envs(project, dry_run=dry_run)
    poetry_env = {**os.environ, "POETRY_VIRTUALENVS_IN_PROJECT": "true"}
    use_python = ["poetry", "env", "use", python_executable]
    with rebuild_with_venv_backup(project / ".venv", dry_run=dry_run):
        use_python_status = run_command(use_python, cwd=project, env=poetry_env, dry_run=dry_run, check=False)
        if use_python_status != 0:
            raise Abort(f"poetry env use {python_executable} failed")
        update_status = run_command(["poetry", "update"], cwd=project, env=poetry_env, dry_run=dry_run, check=False)
        if update_status != 0:
            raise Abort("poetry update failed — kept the previous .venv; review output above")


def report_missing_wheels(project: Path, tool: str, target_version: str) -> None:
    try:
        gaps = wheel_check.find_gaps(project, tool, target_version)
    except (OSError, tomllib.TOMLDecodeError, KeyError) as error:
        warn(f"wheel check skipped — cannot read the lock file ({error})")
        return
    python_tag, platform_tag = wheel_check.target_tags(target_version)
    wheel_tag = f"{python_tag}-{platform_tag}"
    packages_requiring_build = []
    for gap in gaps:
        if gap.latest is None:
            warn(f"{gap.name} {gap.locked}: no {wheel_tag} wheel, and PyPI could not be checked for a newer one")
            packages_requiring_build.append(gap.name)
        elif gap.latest_fits:
            log(f"{gap.name} {gap.locked}: no {wheel_tag} wheel; {gap.latest} has one and the re-resolve can pick it")
        else:
            warn(f"{gap.name}: no {wheel_tag} wheel even in the latest release ({gap.latest}) — it would be built from source")
            packages_requiring_build.append(gap.name)
    if packages_requiring_build:
        warn(f"building {', '.join(packages_requiring_build)} needs a compiler (MSVC Build Tools; Rust for some) — "
             f"without one the rebuild fails and the previous .venv is kept")


def rebuild_project_venv(project: Path, target_version: str, python_executable: str, *, dry_run: bool) -> None:
    tool = detect_venv_tool(project)
    if not tool:
        log("no uv/poetry project detected — skipping venv update")
        return
    log(f"venv tool: {tool}")
    try_update_venv_tool(tool, dry_run=dry_run)

    try:
        constraint = project_python.python_constraint(project)
        if constraint is not None and not project_python.satisfies(target_version, constraint):
            warn(f"Python {target_version} is outside requires-python {constraint!r} — venv left alone")
            return
    except project_python.PyprojectError as error:
        raise Abort(f"{error} — fix it, then re-run")
    except project_python.ConstraintError as error:
        warn(f"requires-python not understood ({error}) — not checked")

    venv = project / ".venv"
    current_version = read_running_venv_version(venv)
    if current_version and release_version_parts(current_version) >= release_version_parts(target_version):
        log(f"venv already on {current_version} — skipping")
        return
    base_install = read_venv_base_install(venv)
    patch_update_keeps_venv = (
        current_version
        and minor_version_tag(current_version) == minor_version_tag(target_version)
        and base_install
        and same_directory(base_install, Path(python_executable).parent)
    )
    if patch_update_keeps_venv:
        log(f"venv on {current_version} runs from {base_install} — the patch update carries it, no rebuild")
        return

    report_missing_wheels(project, tool, target_version)
    log(f"updating venv: {current_version or 'none'} → {target_version}")

    if tool == "uv":
        sync_uv_venv(project, python_executable, dry_run=dry_run)
    else:
        sync_poetry_venv(project, python_executable, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Set the OS default Python and rebuild the project venv "
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

    project = Path(args.project).resolve()
    running_version = ".".join(map(str, sys.version_info[:3]))
    target_version = args.target or running_version
    if minor_version_tag(target_version) != minor_version_tag(running_version):
        python_executable = f"<python {target_version}>"
    else:
        python_executable = sys.executable

    try:
        if sys.prefix != sys.base_prefix:
            raise Abort(
                f"running from a venv ({sys.prefix}) — rebuilding a venv with its own interpreter "
                f"cannot work. Launch with a base interpreter, or just run update_python.ps1."
            )

        if os.environ.pop("VIRTUAL_ENV", None):
            log("ignoring inherited VIRTUAL_ENV")

        log(f"target: Python {target_version} ({python_executable})")
        set_os_default_python(args.os_default or target_version, dry_run=args.dry_run)
        rebuild_project_venv(project, target_version, python_executable, dry_run=args.dry_run)

    except Abort as error:
        warn(str(error))
        return 1

    log("dry run done — nothing was changed" if args.dry_run else "done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
