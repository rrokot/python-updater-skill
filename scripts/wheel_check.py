"""Which locked packages have no wheel for the target Python, before a rebuild finds out.

Rebuilding a venv on a new minor re-resolves the lock. A compiled package with no wheel
for the new interpreter is then built from source, and on a machine without a C or Rust
toolchain that fails halfway through the rebuild (PyYAML on 3.15 did). This looks ahead:
the lock lists the files of each pinned release, PyPI says whether its latest release
does better.

Stdlib only, like the rest of phase 2. Reads only, so the dry run does all of it too.
"""
import json
import os
import platform
import re
import sys
import sysconfig
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PYPI_TIMEOUT = 10


@dataclass
class Gap:
    name: str
    locked: str
    latest: str | None        # None: PyPI could not be asked
    latest_fits: bool = False


def target_tags(target: str) -> tuple[str, str]:
    """(cpXY, platform) the target interpreter accepts wheels for, e.g. ("cp315", "win_amd64").

    The platform is that of the interpreter running this. The dry run may be on an older
    Python than the target, but it is on the same machine and of the same bitness.
    """
    major, minor = target.split(".")[:2]
    plat = re.sub(r"[-.]", "_", sysconfig.get_platform())
    return f"cp{major}{minor}", plat


def wheel_fits(filename: str, cp: str, plat: str) -> bool:
    parts = filename[:-len(".whl")].split("-")
    if len(parts) < 5:
        return False
    py_tags, abi_tags, plat_tags = (set(p.split(".")) for p in parts[-3:])
    if not plat_tags & {plat, "any"}:
        return False
    minor = int(cp[3:])
    for py in py_tags:
        for abi in abi_tags:
            if abi == "none" and py in ("py3", f"py3{minor}", cp):
                return True
            # Stable ABI: built for 3.N, loads on every later 3.x.
            if abi == "abi3" and re.fullmatch(r"cp3\d+", py) and int(py[3:]) <= minor:
                return True
            # Exact ABI only — cp315t is the free-threaded build, not this one.
            if abi == cp and py == cp:
                return True
    return False


# ── PEP 508 markers ──────────────────────────────────────────────────────────

_TOKEN = re.compile(r"""\s*(\(|\)|not\s+in\b|in\b|and\b|or\b|===|==|!=|~=|<=|>=|<|>|"[^"]*"|'[^']*'|[A-Za-z_][A-Za-z0-9_.]*)""")
_OPS = {"===", "==", "!=", "~=", "<=", ">=", "<", ">", "in", "not in"}
_VERSION_VARS = {"python_version", "python_full_version", "implementation_version"}


def marker_env(target: str) -> dict[str, str]:
    return {
        "python_version": ".".join(target.split(".")[:2]),
        "python_full_version": target,
        "implementation_version": target,
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "sys_platform": sys.platform,
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "os_name": os.name,
    }


def _version_cmp(a: str, op: str, b: str) -> bool:
    x = tuple(int(v) for v in re.findall(r"\d+", a))
    y = tuple(int(v) for v in re.findall(r"\d+", b))
    kept = len(y) - 1  # for ~=: everything but the last component of the right side
    n = max(len(x), len(y))
    x, y = x + (0,) * (n - len(x)), y + (0,) * (n - len(y))
    if op == "~=":
        return x >= y and x[:kept] == y[:kept]
    return {"==": x == y, "===": x == y, "!=": x != y, "<": x < y,
            "<=": x <= y, ">": x > y, ">=": x >= y}.get(op, True)


def _compare(lhs, op: str, rhs, env: dict) -> bool:
    def value(tok):
        if tok[0] in "\"'":
            return tok[1:-1], None
        return env.get(tok), tok
    left, lvar = value(lhs)
    right, rvar = value(rhs)
    if left is None or right is None:
        # `extra`, or a variable this module does not model: assume the package is
        # installed. A needless warning is cheaper than a missed one.
        return True
    if op in ("in", "not in"):
        return (left in right) == (op == "in")
    if {lvar, rvar} & _VERSION_VARS:
        return _version_cmp(left, op, right)
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    return True


def marker_applies(marker: str, env: dict) -> bool:
    """Evaluate a PEP 508 marker for env. Anything unparseable counts as applying."""
    tokens, pos = [], 0
    marker = marker.strip()
    while pos < len(marker):
        m = _TOKEN.match(marker, pos)
        if not m:
            return True
        tokens.append(re.sub(r"\s+", " ", m.group(1)))
        pos = m.end()
        while pos < len(marker) and marker[pos].isspace():
            pos += 1

    i = 0

    def peek():
        return tokens[i] if i < len(tokens) else None

    def expr():
        nonlocal i
        result = term()
        while peek() == "or":
            i += 1
            rhs = term()
            result = result or rhs
        return result

    def term():
        nonlocal i
        result = atom()
        while peek() == "and":
            i += 1
            rhs = atom()
            result = result and rhs
        return result

    def atom():
        nonlocal i
        if peek() == "(":
            i += 1
            result = expr()
            if peek() != ")":
                raise ValueError("unbalanced parentheses")
            i += 1
            return result
        if i + 3 > len(tokens):
            raise ValueError("truncated comparison")
        lhs, op, rhs = tokens[i:i + 3]
        if op not in _OPS:
            raise ValueError(f"{op!r} is not a comparison")
        i += 3
        return _compare(lhs, op, rhs, env)

    try:
        result = expr()
        if i != len(tokens):
            return True
        return result
    except (ValueError, IndexError):
        return True


# ── lock files ───────────────────────────────────────────────────────────────

def _locked_poetry(project: Path, env: dict) -> list[tuple[str, str, list[str]]]:
    with open(project / "poetry.lock", "rb") as f:
        lock = tomllib.load(f)
    out = []
    for p in lock.get("package", []):
        # A path, git or url dependency is built from what it points at, not from PyPI.
        if p.get("source", {}).get("type") in ("directory", "file", "git", "url"):
            continue
        markers = p.get("markers")
        if isinstance(markers, str) and not marker_applies(markers, env):
            continue
        # Lock 2.1 may key markers by dependency group: installed if any group installs it.
        if isinstance(markers, dict) and not any(marker_applies(m, env) for m in markers.values()):
            continue
        out.append((p["name"], p["version"], [f["file"] for f in p.get("files", [])]))
    return out


def _locked_uv(project: Path, env: dict) -> list[tuple[str, str, list[str]]]:
    with open(project / "uv.lock", "rb") as f:
        lock = tomllib.load(f)
    out = []
    for p in lock.get("package", []):
        if "registry" not in p.get("source", {}):
            continue  # the project itself, or a path/git dependency
        urls = [w["url"] for w in p.get("wheels", []) if "url" in w]
        if "sdist" in p and "url" in p["sdist"]:
            urls.append(p["sdist"]["url"])
        # uv.lock puts markers on dependency edges, not on packages, so a package that
        # only another platform installs is still listed here.
        out.append((p["name"], p["version"], [u.rsplit("/", 1)[-1] for u in urls]))
    return out


def _pypi_latest(name: str) -> tuple[str, list[str]] | None:
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        with urllib.request.urlopen(url, timeout=PYPI_TIMEOUT) as r:
            data = json.load(r)
    except (OSError, ValueError):
        return None
    return data["info"]["version"], [u["filename"] for u in data.get("urls", [])]


def find_gaps(project: Path, tool: str, target: str) -> list[Gap]:
    """Locked packages that ship wheels, but none the target can install.

    A package with no wheels at all is left out: that is almost always pure Python,
    which builds anywhere, and no later release would change it.
    """
    lock = project / ("uv.lock" if tool == "uv" else "poetry.lock")
    if not lock.exists():
        return []
    env = marker_env(target)
    cp, plat = target_tags(target)
    locked = _locked_uv(project, env) if tool == "uv" else _locked_poetry(project, env)

    gaps = []
    for name, version, files in locked:
        wheels = [f for f in files if f.endswith(".whl")]
        if not wheels or any(wheel_fits(w, cp, plat) for w in wheels):
            continue
        latest = _pypi_latest(name)
        if latest is None:
            gaps.append(Gap(name, version, None))
            continue
        latest_version, latest_files = latest
        fits = any(wheel_fits(f, cp, plat) for f in latest_files if f.endswith(".whl"))
        gaps.append(Gap(name, version, latest_version, fits))
    return gaps
