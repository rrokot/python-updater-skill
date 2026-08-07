---
name: update-venv-python
description: Update the Python interpreter on Windows to the latest official release using the Python Install Manager (py), make it the OS default, and recreate the active project's virtualenv (poetry or uv) on it. Use when the user wants to upgrade/bump Python to the latest version, update the system/default Python, or migrate a poetry/uv project's venv to a newer Python. Triggers include "обнови python", "поставь последнюю версию python", "update python to latest", "пересоздай venv на новой версии python", "сделай новый python дефолтным".
---

# Update Python (Python Install Manager + venv)

Updates Python on Windows via the **Python Install Manager** (`py` / `pymanager`)
and recreates the active project's virtualenv on the new interpreter.

The Python Install Manager installs **official python.org builds**, so it tracks
the true latest release. (uv's bundled python-build-standalone catalog lags the
official releases by days, so it is *not* used to pick or install Python here —
only to build the venv.)

## When to use

- Upgrade the system Python to the latest official release and make it default.
- Bump a project's venv to a newer Python.
- Install/pin a specific Python minor and rebuild the project venv on it.

## Prerequisites (Windows)

The script handles the manager itself:

- **Missing?** It installs the Python Install Manager via
  `winget install --id Python.PythonInstallManager -e` (requires winget).
- **Legacy launcher present?** It removes the old 'Python Launcher'
  (`winget uninstall --id Python.Launcher -e`) so plain `py` resolves to the new
  manager too. The script itself always drives the manager via the unambiguous
  `pymanager` command, so it never depends on `py`.
- **Outdated?** Step 0 runs `winget upgrade` on the manager.

## How the scripts work

The work is split across two phases because **the interpreter cannot be installed
from a Python that the install replaces**. pymanager keeps one directory per minor
tag (`…\Python\pythoncore-3.14-64`), so a patch bump deletes that directory — and
Windows refuses while its files are open. A script started with `py` runs out of
exactly that directory, and so does any venv built on it (it loads the base
install's `pythonXY.dll`), which is why the install aborts with
`Unable to remove previous install because files are still in use`.

**Phase 1 — `scripts/update_python.ps1` (PowerShell, the entry point):**

| Step | Action | Command |
| ---- | ------ | ------- |
| 0 | Update the manager itself | `winget upgrade --id Python.PythonInstallManager` |
| 1 | Find latest stable official | `pymanager list --online -f=json` |
| 2 | Refuse if the install to be replaced is in use | lists blocking PIDs and exits 1 |
| 3 | Install it if missing/behind | `pymanager install <version> -y` |

Step 2 looks for interpreters under the install root, venvs whose `pyvenv.cfg`
`home` points into it, and processes that have the install's `pythonXY.dll`
loaded under a name of their own. The list can be incomplete — processes running
elevated or at the other bitness cannot always be inspected — so a clean result
is not a guarantee, just the best available check. It never kills anything: it
prints the PIDs and command lines, and the user decides.

**Phase 2 — `scripts/update_python.py`, launched with the freshly installed
interpreter, so nothing it does holds a doomed install:**

| Step | Action | Command |
| ---- | ------ | ------- |
| 4 | Make it the OS default | writes `default_tag` to `%AppData%\Python\pymanager.json` |
| 5 | Recreate the project venv if older | uv / poetry on the system interpreter |

Phase 2 installs nothing and never calls `pymanager list`: it takes its target
from the interpreter running it (`sys.version_info`, `sys.executable`), so it
cannot disagree with phase 1 and does not depend on the catalog a second time.

For step 5 the manager is auto-detected (`uv.lock`/`[tool.uv]` → uv,
`poetry.lock`/`[tool.poetry]` → poetry) and the venv is rebuilt only when its
current Python is older than the target:
- uv: `uv sync --python <exe> --no-managed-python --no-python-downloads`; if that
  fails, `.venv` is moved to `.venv_old` and the sync is retried.
- poetry: `poetry env use <exe>` then `poetry install`, after moving `.venv` aside
  and clearing any stale env for the project in Poetry's cache.

Moving `.venv` aside only helps when the directory is free. Windows will not rename
a directory that still has an open file inside it, so a venv the IDE is actually
holding fails here with the OS error — close the holder and re-run.

Pre-releases and free-threaded builds are excluded.

`update_python.py` is not a standalone entry point: it makes *the interpreter it
was launched with* the OS default and rebuilds the venv on it. It refuses to run
from inside a venv, since a venv cannot rebuild itself with its own interpreter.

## Usage

Invoke the **PowerShell** script, not the Python one — that is the whole point of
the split. Run it **from the project directory** so `-Project` defaults to the
right place. Below, `<skill>` is this skill's directory.

```powershell
# from the project root (uv or poetry project):

# Latest stable: update the manager, set the OS default, rebuild this project's venv
& "<skill>\scripts\update_python.ps1"

# Target a different project
& "<skill>\scripts\update_python.ps1" -p ..\service
```

### Options

| Flag             | Meaning                                       |
| ---------------- | --------------------------------------------- |
| `-p`, `-Project` | Project directory. Default: current directory. |

Everything else is automatic: the venv manager is auto-detected, only stable
releases are considered, the new version is made the OS default, the install
manager is updated, and the legacy launcher is removed if found.

## Guidance for the agent

- This script applies changes immediately — there is no dry-run mode. Before running, confirm with the user that they want to change the OS default Python.
- Note that a real run **changes the OS default Python** and removes the legacy
  launcher; make sure that is what the user wants before applying.
- Always start from the `.ps1`. `update_python.py` no longer installs anything, so
  running it directly only sets the default to whatever interpreter you launched it
  with — which is rarely what was wanted.
- If phase 1 exits with a list of blocking processes, relay that list and ask the
  user to stop them; do not kill them yourself — they are usually the user's own
  running apps. Re-run the `.ps1` afterwards; it is idempotent.
- If the install still fails with `files are still in use` after a clean blocker
  check, an unlistable process (elevated, other bitness) is holding it — closing
  the IDE and any terminals is the practical next step.
- After a real run, the new default takes effect in fresh shells; suggest
  reopening the terminal and running the project's tests.
- A rebuilt venv installs from the lock file, so a lock entry with no wheel for
  the new interpreter surfaces here (`Unable to find installation candidates`).
  That is a lock problem, not a Python one — `poetry update --no-cache <pkg>`.
- These scripts are Windows-only (Python Install Manager, PowerShell, winget).
