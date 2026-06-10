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

## How the script works

`scripts/update_python.py` runs these steps:

| Step | Action | Command |
| ---- | ------ | ------- |
| 0 | Update the manager itself | `winget upgrade --id Python.PythonInstallManager` |
| 1 | Find latest stable official | `pymanager list --online -f=json` |
| 2 | Install it if missing/behind | `pymanager install <minor> -y` |
| 3 | Make it the OS default | writes `default_tag` to `%AppData%\Python\pymanager.json` |
| 4 | Recreate the project venv if older | uv / poetry on the system interpreter |

For step 4 the manager is auto-detected (`uv.lock`/`[tool.uv]` → uv,
`poetry.lock`/`[tool.poetry]` → poetry) and the venv is rebuilt only when its
current Python is older than the target:
- uv: `uv sync --python <exe> --no-managed-python --no-python-downloads`
  (falls back to `uv venv --clear`).
- poetry: `poetry env use <exe>` then `poetry install`.

Pre-releases and free-threaded builds are excluded.

## Usage

Invoke it **from the project directory** so `--project` defaults to the right
place, running the script by its path inside this skill. Use `py`, not `python`
(on Windows `python` may be missing or a Store stub). Below, `<skill>` is this
skill's directory.

```bash
# from the project root (uv or poetry project):

# Latest stable: update the manager, set the OS default, rebuild this project's venv
py <skill>/scripts/update_python.py

# Pin a minor series
py <skill>/scripts/update_python.py 3.13

# Target a different project, preview only
py <skill>/scripts/update_python.py -p ../service --dry-run
```

### Options

| Flag                    | Meaning                                                      |
| ----------------------- | ----------------------------------------------------------- |
| `version` (positional)  | Target minor, e.g. `3.13`. Default: latest stable.          |
| `-p`, `--project`       | Project directory. Default: current directory.              |
| `--dry-run`             | Print the planned actions without changing anything.        |

Everything else is automatic: the venv manager is auto-detected, only stable
releases are considered, the new version is made the OS default, the install
manager is updated, and the legacy launcher is removed if found.

## Guidance for the agent

- **Run `--dry-run` first** and show the user the plan before applying.
- Note that a real run **changes the OS default Python** and removes the legacy
  launcher; make sure that is what the user wants before applying.
- After a real run, the new default takes effect in fresh shells; suggest
  reopening the terminal and running the project's tests.
- This script is Windows-focused (Python Install Manager). On other platforms it
  warns and the `py` steps will not work.
