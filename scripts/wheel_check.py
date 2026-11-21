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

PYPI_TIMEOUT_SECONDS = 10


@dataclass
class Gap:
    name: str
    locked: str
    latest: str | None
    latest_fits: bool = False


def target_tags(target_version: str) -> tuple[str, str]:
    major, minor = target_version.split(".")[:2]
    host_platform_tag = re.sub(r"[-.]", "_", sysconfig.get_platform())
    return f"cp{major}{minor}", host_platform_tag


def wheel_fits(filename: str, python_tag: str, platform_tag: str) -> bool:
    filename_parts = filename[:-len(".whl")].split("-")
    if len(filename_parts) < 5:
        return False
    wheel_python_tags, wheel_abi_tags, wheel_platform_tags = (
        set(compressed_tags.split(".")) for compressed_tags in filename_parts[-3:]
    )
    if not wheel_platform_tags & {platform_tag, "any"}:
        return False
    target_minor = int(python_tag[3:])
    abi_independent_python_tags = {"py3", f"py3{target_minor}", python_tag}
    for wheel_python_tag in wheel_python_tags:
        for wheel_abi_tag in wheel_abi_tags:
            if wheel_abi_tag == "none" and wheel_python_tag in abi_independent_python_tags:
                return True
            if wheel_abi_tag == python_tag and wheel_python_tag == python_tag:
                return True
            if wheel_abi_tag == "abi3" and re.fullmatch(r"cp3\d+", wheel_python_tag):
                minimum_minor = int(wheel_python_tag[3:])
                if minimum_minor <= target_minor:
                    return True
    return False


_MARKER_TOKEN_PATTERN = re.compile(
    r"\s*(\(|\)|not\s+in\b|in\b|and\b|or\b|===|==|!=|~=|<=|>=|<|>|"
    r'''"[^"]*"|'[^']*'|[A-Za-z_][A-Za-z0-9_.]*)'''
)
_MARKER_COMPARISON_OPERATORS = {"===", "==", "!=", "~=", "<=", ">=", "<", ">", "in", "not in"}
_MARKER_VERSION_VARIABLES = {"python_version", "python_full_version", "implementation_version"}


def marker_env(target_version: str) -> dict[str, str]:
    return {
        "python_version": ".".join(target_version.split(".")[:2]),
        "python_full_version": target_version,
        "implementation_version": target_version,
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "sys_platform": sys.platform,
        "platform_system": platform.system(),
        "platform_machine": platform.machine(),
        "platform_release": platform.release(),
        "platform_version": platform.version(),
        "os_name": os.name,
    }


def _matches_version_comparison(left_version: str, operator: str, right_version: str) -> bool:
    left_parts = tuple(int(part) for part in re.findall(r"\d+", left_version))
    right_parts = tuple(int(part) for part in re.findall(r"\d+", right_version))
    compatible_prefix_length = len(right_parts) - 1
    length = max(len(left_parts), len(right_parts))
    left = left_parts + (0,) * (length - len(left_parts))
    right = right_parts + (0,) * (length - len(right_parts))
    if operator == "~=":
        return left >= right and left[:compatible_prefix_length] == right[:compatible_prefix_length]
    return {
        "==": left == right,
        "===": left == right,
        "!=": left != right,
        "<": left < right,
        "<=": left <= right,
        ">": left > right,
        ">=": left >= right,
    }.get(operator, True)


def _matches_marker_comparison(left_operand: str, operator: str, right_operand: str, env: dict[str, str]) -> bool:
    def resolve_operand(operand: str) -> tuple[str | None, str | None]:
        if operand[0] in "\"'":
            return operand[1:-1], None
        return env.get(operand), operand

    left, left_variable = resolve_operand(left_operand)
    right, right_variable = resolve_operand(right_operand)
    if left is None or right is None:
        return True
    if operator in ("in", "not in"):
        return (left in right) == (operator == "in")
    if {left_variable, right_variable} & _MARKER_VERSION_VARIABLES:
        return _matches_version_comparison(left, operator, right)
    if operator == "==":
        return left == right
    if operator == "!=":
        return left != right
    return True


def marker_applies(marker: str, env: dict[str, str]) -> bool:
    tokens = []
    token_start = 0
    marker = marker.strip()
    while token_start < len(marker):
        token = _MARKER_TOKEN_PATTERN.match(marker, token_start)
        if token is None:
            return True
        tokens.append(re.sub(r"\s+", " ", token.group(1)))
        token_start = token.end()

    token_index = 0

    def next_token() -> str | None:
        return tokens[token_index] if token_index < len(tokens) else None

    def parse_disjunction() -> bool:
        nonlocal token_index
        matches = parse_conjunction()
        while next_token() == "or":
            token_index += 1
            right_matches = parse_conjunction()
            matches = matches or right_matches
        return matches

    def parse_conjunction() -> bool:
        nonlocal token_index
        matches = parse_atom()
        while next_token() == "and":
            token_index += 1
            right_matches = parse_atom()
            matches = matches and right_matches
        return matches

    def parse_atom() -> bool:
        nonlocal token_index
        if next_token() == "(":
            token_index += 1
            matches = parse_disjunction()
            if next_token() != ")":
                raise ValueError("unbalanced parentheses")
            token_index += 1
            return matches
        if token_index + 3 > len(tokens):
            raise ValueError("truncated comparison")
        left, operator, right = tokens[token_index:token_index + 3]
        if operator not in _MARKER_COMPARISON_OPERATORS:
            raise ValueError(f"{operator!r} is not a comparison")
        token_index += 3
        return _matches_marker_comparison(left, operator, right, env)

    try:
        matches = parse_disjunction()
        if token_index != len(tokens):
            return True
        return matches
    except (ValueError, IndexError):
        return True


def _locked_poetry_registry_packages(lock_path: Path, env: dict[str, str]) -> list[tuple[str, str, list[str]]]:
    with lock_path.open("rb") as lock_file:
        lock = tomllib.load(lock_file)
    packages = []
    for package in lock.get("package", []):
        source_type = package.get("source", {}).get("type")
        if source_type in ("directory", "file", "git", "url"):
            continue
        markers = package.get("markers")
        if isinstance(markers, str) and not marker_applies(markers, env):
            continue
        if isinstance(markers, dict) and not any(
            marker_applies(group_marker, env) for group_marker in markers.values()
        ):
            continue
        filenames = [distribution["file"] for distribution in package.get("files", [])]
        packages.append((package["name"], package["version"], filenames))
    return packages


def _locked_uv_registry_packages(lock_path: Path) -> list[tuple[str, str, list[str]]]:
    with lock_path.open("rb") as lock_file:
        lock = tomllib.load(lock_file)
    packages = []
    for package in lock.get("package", []):
        if "registry" not in package.get("source", {}):
            continue
        urls = [wheel["url"] for wheel in package.get("wheels", []) if "url" in wheel]
        source_distribution = package.get("sdist", {})
        if "url" in source_distribution:
            urls.append(source_distribution["url"])
        filenames = [url.rsplit("/", 1)[-1] for url in urls]
        packages.append((package["name"], package["version"], filenames))
    return packages


def _pypi_latest(name: str) -> tuple[str, list[str]] | None:
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        with urllib.request.urlopen(url, timeout=PYPI_TIMEOUT_SECONDS) as response:
            release = json.load(response)
    except (OSError, ValueError):
        return None
    return release["info"]["version"], [distribution["filename"] for distribution in release.get("urls", [])]


def find_gaps(project: Path, tool: str, target: str) -> list[Gap]:
    lock_path = project / ("uv.lock" if tool == "uv" else "poetry.lock")
    if not lock_path.exists():
        return []
    python_tag, platform_tag = target_tags(target)
    if tool == "uv":
        locked_packages = _locked_uv_registry_packages(lock_path)
    else:
        locked_packages = _locked_poetry_registry_packages(lock_path, marker_env(target))

    gaps = []
    for name, locked_version, filenames in locked_packages:
        locked_wheels = [filename for filename in filenames if filename.endswith(".whl")]
        if not locked_wheels or any(wheel_fits(wheel, python_tag, platform_tag) for wheel in locked_wheels):
            continue
        latest_release = _pypi_latest(name)
        if latest_release is None:
            gaps.append(Gap(name, locked_version, latest=None))
            continue
        latest_version, latest_filenames = latest_release
        latest_has_compatible_wheel = any(
            wheel_fits(filename, python_tag, platform_tag)
            for filename in latest_filenames
            if filename.endswith(".whl")
        )
        gaps.append(Gap(name, locked_version, latest_version, latest_fits=latest_has_compatible_wheel))
    return gaps
