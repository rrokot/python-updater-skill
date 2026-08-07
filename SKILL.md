---
name: update-venv-python
description: Update the Python interpreter on Windows to the latest official release using the Python Install Manager (py), make it the OS default, and recreate the active project's virtualenv (poetry or uv) on it. Use when the user wants to upgrade/bump Python to the latest version, update the system/default Python, or migrate a poetry/uv project's venv to a newer Python. Triggers include "обнови python", "поставь последнюю версию python", "update python to latest", "пересоздай venv на новой версии python", "сделай новый python дефолтным".
---

# Update Python (Python Install Manager + venv)

Installs the latest official Python on Windows via the Python Install Manager,
makes it the OS default, and rebuilds the project's uv/poetry venv on it.

## Usage

Run it **from the project directory**. `scripts/update_python.ps1` is the entry
point — do not invoke `scripts/update_python.py` directly, it is only the second
half and installs nothing. Below, `<skill>` is this skill's directory.

```powershell
& "<skill>\scripts\update_python.ps1"                  # current directory
& "<skill>\scripts\update_python.ps1" -p ..\service    # another project
```

`-p` / `-Project` is the only option. The rest is automatic: venv manager
auto-detected (uv or poetry), stable releases only, OS default updated, install
manager updated, legacy launcher removed.

## Guidance for the agent

- Changes apply immediately and there is no dry-run mode. **Confirm with the user
  before running** — a real run changes the OS default Python.
- If it exits 1 with a list of PIDs, those processes hold the install being
  replaced. Relay the list and ask the user to close them; do not kill them
  yourself. Then re-run — the script is idempotent.
- That list can be incomplete (elevated processes, other bitness). If the install
  still fails with `files are still in use`, closing the IDE and any terminals is
  the practical next step.
- Rebuilding the venv can fail on a locked `.venv`; Windows cannot move a
  directory that has an open file inside it. Same fix: close whatever holds it.
- `Unable to find installation candidates` during the rebuild is a lock-file
  problem, not a Python one — `poetry update --no-cache <pkg>`.
- After a real run the new default applies in fresh shells; suggest reopening the
  terminal and running the project's tests.
- Windows only (Python Install Manager, PowerShell, winget).
