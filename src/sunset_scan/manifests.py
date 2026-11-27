"""Find npm and PyPI packages (and their versions) in manifests and lockfiles."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath

from .versions import base_version

LOCKFILES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock",
    "poetry.lock", "uv.lock", "pdm.lock", "Pipfile.lock",
    "Cargo.lock", "Gemfile.lock", "composer.lock", "go.sum",
}
_NPM_FILES = {"package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"}
_PY_FILES = {"pyproject.toml", "poetry.lock", "uv.lock", "pdm.lock", "Pipfile", "Pipfile.lock"}


@dataclass
class PackageRef:
    ecosystem: str
    name: str
    version: str | None  # resolved version (lockfile / exact pin)
    spec: str | None  # declared range, e.g. "^7.33.2"
    path: str
    line: int

    @property
    def effective_version(self) -> str | None:
        return self.version or base_version(self.spec)

    @property
    def describe(self) -> str:
        if self.version:
            return f"{self.name} {self.version}"
        if self.spec:
            return f"{self.name} {self.spec}"
        return self.name


def normalize(ecosystem: str, name: str) -> str:
    if ecosystem == "pypi":
        return re.sub(r"[-_.]+", "-", name).lower()
    return name


def manifest_kind(relpath: str) -> str | None:
    p = PurePosixPath(relpath)
    name = p.name
    if name in _NPM_FILES:
        return "npm"
    if name in _PY_FILES:
        return "pypi"
    low = name.lower()
    if (low.startswith(("requirements", "constraints")) and low.endswith((".txt", ".in"))) or (
        p.parent.name == "requirements" and low.endswith((".txt", ".in"))
    ):
        return "pypi"
    return None


def _line_match(text: str, pattern: str, after: str | None = None, flags: int = 0) -> int:
    """1-based line of the first match of ``pattern``, searching below the line matching ``after``.

    Falls back to searching the whole file, then to line 1.
    """
    rx = re.compile(pattern, flags)
    lines = text.splitlines()
    start = 0
    if after:
        hdr = re.compile(after)
        start = next((i for i, line in enumerate(lines) if hdr.search(line)), 0)
    for lo in ([start, 0] if start else [0]):
        for i in range(lo, len(lines)):
            if rx.search(lines[i]):
                return i + 1
    return 1


def _name_rx(name: str) -> str:
    """A PyPI name with any of - _ . between parts, not followed by more name characters."""
    parts = [re.escape(p) for p in re.split(r"[-_.]+", name)]
    return r"[-_.]+".join(parts) + r"(?![A-Za-z0-9_.\-])"


def _toml_key_rx(name: str) -> str:
    return r"^\s*[\"']?" + _name_rx(name) + r"[\"']?\s*="


def _split_npm_descriptor(desc: str) -> tuple[str, str]:
    desc = desc.strip().strip('"').strip("'")
    if desc.startswith("@"):
        at = desc.find("@", 1)
    else:
        at = desc.find("@")
    if at <= 0:
        return desc, ""
    return desc[:at], desc[at + 1 :]


def _parse_package_json(text: str, path: str, problems: list[str]) -> list[PackageRef]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        problems.append(f"{path} (invalid JSON: {exc})")
        return []
    if not isinstance(data, dict):
        problems.append(f"{path} (not a JSON object)")
        return []
    out = []
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = data.get(section) or {}
        if not isinstance(deps, dict):
            continue
        for name, spec in deps.items():
            orig = name
            spec = spec if isinstance(spec, str) else None
            if spec and spec.startswith("npm:"):
                name, spec = _split_npm_descriptor(spec[4:])
            line =_line_match(text, r'"' + re.escape(orig) + r'"\s*:', after=r'"' + section + r'"\s*:')
            out.append(PackageRef("npm", name, None, spec, path, line))
    return out




def _parse_package_lock(text: str, path: str, problems: list[str]) -> list[PackageRef]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        problems.append(f"{path} (invalid JSON: {exc})")
        return []
    if not isinstance(data, dict):
        problems.append(f"{path} (not a JSON object)")
        return []
    key_lines: dict[str, int] = {}
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r'^\s*"([^"]+)"\s*:\s*\{', line)
        if m and m.group(1) not in key_lines:
            key_lines[m.group(1)] = i
    out = []
    packages = data.get("packages")
    if isinstance(packages, dict):
        for key, meta in packages.items():
            if not key or "node_modules/" not in key or not isinstance(meta, dict):
                continue
            name = key.rsplit("node_modules/", 1)[1]
            out.append(PackageRef("npm", name, meta.get("version"), None, path, key_lines.get(key, 1)))
        return out

    def walk(deps):
        for name, meta in (deps or {}).items():
            if isinstance(meta, dict):
                out.append(PackageRef("npm", name, meta.get("version"), None, path, key_lines.get(name, 1)))
                walk(meta.get("dependencies"))

    walk(data.get("dependencies"))
    return out


def _parse_yarn_lock(text: str, path: str) -> list[PackageRef]:
    out = []
    lines = text.splitlines()
    current: list[tuple[str, str]] = []
    header_line = 0
    for i, line in enumerate(lines, 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith((" ", "\t")) and line.rstrip().endswith(":"):
            current = []
            header_line = i
            for desc in line.rstrip()[:-1].split(","):
                name, rng = _split_npm_descriptor(desc)
                if rng.startswith("npm:"):
                    rng = rng[4:]
                if name and name != "__metadata":
                    current.append((name, rng))
            continue
        m = re.match(r'^\s+version:?\s+"?([^"\s]+)"?', line)
        if m and current:
            seen = set()
            for name, _rng in current:
                if name not in seen:
                    seen.add(name)
                    out.append(PackageRef("npm", name, m.group(1), None, path, header_line))
            current = []
    return out


_PNPM_RE = re.compile(r"""^\s{2,4}['"]?/?(@?[A-Za-z0-9._\-]+(?:/[A-Za-z0-9._\-]+)?)[@/](\d+\.\d+\.\d+[^:'"(\s_]*)""")


def _parse_pnpm_lock(text: str, path: str) -> list[PackageRef]:
    out, seen = [], set()
    for i, line in enumerate(text.splitlines(), 1):
        m = _PNPM_RE.match(line)
        if m and line.rstrip().endswith(":"):
            key = (m.group(1), m.group(2))
            if key not in seen:
                seen.add(key)
                out.append(PackageRef("npm", m.group(1), m.group(2), None, path, i))
    return out


_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)\s*(\[[^\]]*\])?\s*([^;#]*)")


def _pep508(req: str) -> tuple[str, str | None, str | None] | None:
    m = _REQ_RE.match(req)
    if not m:
        return None
    name, spec = m.group(1), m.group(3).strip().rstrip("\\").strip() or None
    version = None
    if spec:
        pin = re.match(r"^===?\s*([0-9][^\s,;]*)$", spec)
        if pin:
            version = pin.group(1)
    return name, version, spec


def _parse_requirements(text: str, path: str, problems: list[str]) -> list[PackageRef]:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith(("#", "-", "git+", "http:", "https:", "file:", ".", "/")):
            continue
        parsed = _pep508(s)
        if parsed:
            name, version, spec = parsed
            out.append(PackageRef("pypi", name, version, spec, path, i))
        else:
            problems.append(f"{path}:{i} ({s[:60]!r} is not a requirement line)")
    return out


def _parse_pyproject(text: str, path: str, problems: list[str]) -> list[PackageRef]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        problems.append(f"{path} (invalid TOML: {exc})")
        return []
    reqs: list[tuple[str, str]] = []  # (requirement, header regex of the table it lives in)
    project = data.get("project") or {}
    reqs += [(r, r"^\s*\[project\]") for r in project.get("dependencies") or [] if isinstance(r, str)]
    for group in (project.get("optional-dependencies") or {}).values():
        reqs += [(r, r"^\s*\[project") for r in group or [] if isinstance(r, str)]
    for group in (data.get("dependency-groups") or {}).values():
        reqs += [(r, r"^\s*\[dependency-groups\]") for r in group or [] if isinstance(r, str)]
    out = []
    for r, header in reqs:
        parsed = _pep508(r)
        if parsed:
            name, version, spec = parsed
            line = _line_match(text, r"[\"']\s*" + _name_rx(name), after=header, flags=re.I)
            out.append(PackageRef("pypi", name, version, spec, path, line))
    poetry = (data.get("tool") or {}).get("poetry") or {}
    tables = [(poetry.get("dependencies"), r"^\s*\[tool\.poetry\.dependencies\]"),
              (poetry.get("dev-dependencies"), r"^\s*\[tool\.poetry\.dev-dependencies\]")]
    tables += [(g.get("dependencies"), r"^\s*\[tool\.poetry\.group\." + re.escape(gname) + r"\.dependencies\]")
               for gname, g in (poetry.get("group") or {}).items() if isinstance(g, dict)]
    for table, header in tables:
        if not isinstance(table, dict):
            continue
        for name, spec in table.items():
            if name == "python":
                continue
            if isinstance(spec, dict):
                spec = spec.get("version")
            spec = spec if isinstance(spec, str) else None
            version = spec if spec and re.fullmatch(r"\d[\w.]*", spec) else None
            line = _line_match(text, _toml_key_rx(name), after=header, flags=re.I)
            out.append(PackageRef("pypi", name, version, spec, path, line))
    return out


def _parse_toml_lock(text: str, path: str, problems: list[str]) -> list[PackageRef]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        problems.append(f"{path} (invalid TOML: {exc})")
        return []
    name_lines: dict[str, int] = {}
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r'^name\s*=\s*"([^"]+)"', line)
        if m and m.group(1) not in name_lines:
            name_lines[m.group(1)] = i
    out = []
    for pkg in data.get("package") or []:
        if isinstance(pkg, dict) and pkg.get("name"):
            name = pkg["name"]
            out.append(PackageRef("pypi", name, pkg.get("version"), None, path, name_lines.get(name, 1)))
    return out


def _parse_pipfile_lock(text: str, path: str, problems: list[str]) -> list[PackageRef]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        problems.append(f"{path} (invalid JSON: {exc})")
        return []
    if not isinstance(data, dict):
        problems.append(f"{path} (not a JSON object)")
        return []
    out = []
    for section in ("default", "develop"):
        for name, meta in (data.get(section) or {}).items():
            version = None
            if isinstance(meta, dict) and isinstance(meta.get("version"), str):
                version = meta["version"].lstrip("=")
            line = _line_match(text, r'"' + re.escape(name) + r'"\s*:', after=r'"' + section + r'"\s*:')
            out.append(PackageRef("pypi", name, version, None, path, line))
    return out


def _parse_pipfile(text: str, path: str, problems: list[str]) -> list[PackageRef]:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        problems.append(f"{path} (invalid TOML: {exc})")
        return []
    out = []
    for section in ("packages", "dev-packages"):
        for name, spec in (data.get(section) or {}).items():
            if isinstance(spec, dict):
                spec = spec.get("version")
            spec = spec if isinstance(spec, str) and spec != "*" else None
            version = spec[2:] if spec and spec.startswith("==") else None
            line = _line_match(text, _toml_key_rx(name), after=r"^\s*\[" + re.escape(section) + r"\]", flags=re.I)
            out.append(PackageRef("pypi", name, version, spec, path, line))
    return out


def parse_manifest(relpath: str, text: str, problems: list[str] | None = None) -> list[PackageRef]:
    """Packages declared in a manifest or lockfile. Parse problems are appended to ``problems``."""
    problems = problems if problems is not None else []
    name = PurePosixPath(relpath).name
    if name == "package.json":
        return _parse_package_json(text, relpath, problems)
    if name in ("package-lock.json", "npm-shrinkwrap.json"):
        return _parse_package_lock(text, relpath, problems)
    if name == "yarn.lock":
        return _parse_yarn_lock(text, relpath)
    if name == "pnpm-lock.yaml":
        return _parse_pnpm_lock(text, relpath)
    if name == "pyproject.toml":
        return _parse_pyproject(text, relpath, problems)
    if name in ("poetry.lock", "uv.lock", "pdm.lock"):
        return _parse_toml_lock(text, relpath, problems)
    if name == "Pipfile.lock":
        return _parse_pipfile_lock(text, relpath, problems)
    if name == "Pipfile":
        return _parse_pipfile(text, relpath, problems)
    if manifest_kind(relpath) == "pypi":
        return _parse_requirements(text, relpath, problems)
    return []
