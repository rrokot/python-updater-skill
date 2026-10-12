# python-updater-skill

A [Claude Code](https://claude.com/claude-code) skill for Windows that updates Python to
the latest stable release, makes it the default, and rebuilds the project's uv or Poetry
virtualenv on it.

Ask Claude something like *"update Python to the latest version"* or *"recreate the venv on
the new Python"* and it runs the skill for the current project.

## Requirements

- Windows with PowerShell 5.1+ and `winget`
- A project managed by [uv](https://docs.astral.sh/uv/) or [Poetry](https://python-poetry.org/)
  (without one, only Python itself is updated)

The [Python Install Manager](https://docs.python.org/3/using/windows.html#python-install-manager)
(`pymanager` / `py`) is installed through winget if it is missing.

## Installation

Clone into your Claude Code skills directory:

```powershell
git clone https://github.com/rrokot/python-updater-skill "$HOME\.claude\skills\update-venv-python"
```

Update later with `git pull` in that directory.

## Usage

Through Claude: ask to update Python in the project. Claude confirms before running,
because the update changes the system default Python.

Or run the script yourself from the project directory:

```powershell
& "$HOME\.claude\skills\update-venv-python\scripts\update_python.ps1"
& "$HOME\.claude\skills\update-venv-python\scripts\update_python.ps1" -p ..\service
```

`-p` / `-Project` sets the project directory; the default is the current one.

## What it does

1. Installs or upgrades the Python Install Manager and removes the legacy `py` launcher.
2. Finds the latest stable CPython and installs it, unless it is already installed.
   If the project's `requires-python` excludes it, also installs the newest release the
   project accepts (for `>=3.14, <3.15`: the latest 3.14 patch).
3. Makes the latest release the default Python for new terminals.
4. Detects uv or Poetry from `uv.lock`, `poetry.lock` or `pyproject.toml` and upgrades
   the tool itself.
5. Warns about locked packages that have no wheel for the target Python and would have
   to be built from source.
6. Rebuilds the project's `.venv` on the target Python if it is on an older version,
   with `uv sync --upgrade` or `poetry env use` + `poetry update`. A venv on an older
   patch of the same minor needs no rebuild: the install is updated in place.

The script can be run again safely: finished steps are skipped.

## Notes

- `-WhatIf` is a dry run: it reports what would be installed, removed and rebuilt, and
  changes nothing.
- If it stops with a list of process IDs, those processes are running from the Python
  being replaced. Close them (usually the IDE and terminals) and run it again.
- If `.venv` cannot be moved or deleted, something still has files open in it; close
  the IDE and run again.
- `Unable to find installation candidates` during `poetry install` is a lock file issue,
  not a Python one: run `poetry update --no-cache <package>`.
- The new default applies in new terminals; reopen the terminal and run the project's tests.

## License

[MIT](LICENSE)
