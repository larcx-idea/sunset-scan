"""Tiny version parsing and range matching (no packaging dependency).

Ranges are comma-separated comparators, e.g. ``">=20.0.0, <21.0.0"`` or
``"<=7.37.5"``. Versions compare numerically on their leading dotted digits;
pre-release suffixes are ignored, which is good enough for "is this major line
affected" questions.
"""

from __future__ import annotations

import re

_NUM = re.compile(r"(\d+(?:\.\d+)*)")
_CMP = re.compile(r"^\s*(<=|>=|==|=|<|>|!=)?\s*v?([0-9][0-9A-Za-z.\-+]*)\s*$")


def parse_version(text: str | None) -> tuple[int, ...] | None:
    if not text:
        return None
    m = _NUM.search(str(text))
    if not m:
        return None
    parts = tuple(int(p) for p in m.group(1).split("."))
    while len(parts) < 3:
        parts += (0,)
    return parts


def base_version(spec: str | None) -> str | None:
    """Best-effort lower bound of a dependency spec: '^7.33.2' -> '7.33.2'."""
    if not spec:
        return None
    spec = spec.strip()
    if spec.startswith(("git", "http", "file:", "link:", "workspace:", "npm:", "github:")):
        return None
    first = re.split(r"\s*(?:\|\||,|\s)\s*", spec)[0]
    m = re.search(r"\d+(?:\.\d+)*", first)
    return m.group(0) if m else None


def parse_range(text: str) -> list[tuple[str, tuple[int, ...]]]:
    out = []
    for part in str(text).split(","):
        if not part.strip():
            continue
        m = _CMP.match(part)
        if not m:
            raise ValueError(f"bad version comparator {part.strip()!r}")
        op = m.group(1) or "=="
        if op == "=":
            op = "=="
        v = parse_version(m.group(2))
        if v is None:
            raise ValueError(f"bad version {m.group(2)!r}")
        out.append((op, v))
    if not out:
        raise ValueError("empty version range")
    return out


def satisfies(version: str | tuple | None, rng: str | None) -> bool:
    """True if ``version`` is inside ``rng``. Unknown versions are treated as affected."""
    if not rng:
        return True
    v = version if isinstance(version, tuple) else parse_version(version)
    if v is None:
        return True
    width = max(len(v), 3)
    for op, bound in parse_range(rng):
        n = max(width, len(bound))
        a = v + (0,) * (n - len(v))
        b = bound + (0,) * (n - len(bound))
        ok = {
            "<": a < b,
            "<=": a <= b,
            ">": a > b,
            ">=": a >= b,
            "==": a == b,
            "!=": a != b,
        }[op]
        if not ok:
            return False
    return True
