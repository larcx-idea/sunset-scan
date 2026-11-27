"""Load, validate and bundle sunset advisories.

Advisories are authored as YAML under ``registry/`` (one file per topic, each
holding a list) and compiled into ``sunset_scan/data/registry.json``, which is
what the installed CLI reads. See CONTRIBUTING.md for the schema.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import yamlish
from .versions import parse_range

SCHEMA_VERSION = 1
KINDS = ("shutdown", "unmaintained", "removed", "upgrade-blocker", "behavior-change")
TYPES = ("model", "image", "literal", "pattern", "npm", "pypi")
ECOSYSTEMS = ("npm", "pypi")
ADVISORY_KEYS = {
    "id", "title", "vendor", "kind", "effective", "announced", "replacement", "summary",
    "source", "references", "origin", "last_verified", "blocks", "affected", "fallback",
}
REQUIRED_KEYS = ("id", "title", "vendor", "kind", "effective", "source", "affected")
AFFECTED_KEYS = {"type", "ids", "pattern", "flags", "versions", "context", "files", "replacement", "note", "quoted"}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9._+-]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class Issue:
    level: str  # "error" | "warning" | "info"
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level}: {self.where}: {self.message}"


class RegistryError(Exception):
    pass


def parse_date(value) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    if not isinstance(value, str) or not DATE_RE.match(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


# ------------------------------------------------------------------ loading

def load_yaml_file(path: Path) -> tuple[list[dict], list[Issue]]:
    issues: list[Issue] = []
    try:
        doc = yamlish.loads(path.read_text(encoding="utf-8"))
    except yamlish.YamlError as exc:
        return [], [Issue("error", str(path), f"YAML: {exc}")]
    if not isinstance(doc, dict) or "advisories" not in doc:
        return [], [Issue("error", str(path), "file must be a mapping with 'schema_version' and 'advisories'")]
    if doc.get("schema_version") != SCHEMA_VERSION:
        issues.append(Issue("error", str(path), f"schema_version must be {SCHEMA_VERSION}"))
    advs = doc.get("advisories") or []
    if not isinstance(advs, list):
        return [], issues + [Issue("error", str(path), "'advisories' must be a list")]
    out = []
    for adv in advs:
        if isinstance(adv, dict):
            adv = dict(adv)
            adv["_file"] = str(path)
            out.append(adv)
        else:
            issues.append(Issue("error", str(path), "each advisory must be a mapping"))
    return out, issues


def load_yaml_dir(root: Path) -> tuple[list[dict], list[Issue]]:
    root = Path(root)
    files = [root] if root.is_file() else sorted(p for p in root.rglob("*") if p.suffix in (".yaml", ".yml"))
    advisories: list[dict] = []
    issues: list[Issue] = []
    for f in files:
        a, i = load_yaml_file(f)
        advisories.extend(a)
        issues.extend(i)
    return advisories, issues


def bundled_path():
    return resources.files("sunset_scan").joinpath("data/registry.json")


def load_bundle(path=None) -> dict:
    src = Path(path) if path else bundled_path()
    try:
        return json.loads(src.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RegistryError(f"registry bundle not found: {src}") from exc


def load_registry(spec: str | None = None) -> tuple[list[dict], dict]:
    """Return (advisories, meta). ``spec`` may be None (bundled), a .json bundle, a YAML file or a directory."""
    if spec is None:
        bundle = load_bundle()
        return bundle["advisories"], {k: v for k, v in bundle.items() if k != "advisories"}
    p = Path(spec)
    if not p.exists():
        raise RegistryError(f"registry not found: {spec}")
    if p.is_file() and p.suffix == ".json":
        bundle = load_bundle(p)
        return bundle["advisories"], {k: v for k, v in bundle.items() if k != "advisories"}
    advisories, issues = load_yaml_dir(p)
    errors = [i for i in issues if i.level == "error"]
    errors += [i for i in validate(advisories) if i.level == "error"]
    if errors:
        raise RegistryError("registry has errors:\n" + "\n".join(str(e) for e in errors[:20]))
    bundle = compile_bundle(advisories)
    return bundle["advisories"], {k: v for k, v in bundle.items() if k != "advisories"}


# --------------------------------------------------------------- validation

def _str_list(v) -> bool:
    return isinstance(v, list) and len(v) > 0 and all(isinstance(x, str) and x.strip() for x in v)


def _https(v) -> bool:
    return isinstance(v, str) and v.startswith("https://") and " " not in v


def model_id_index(advisories: list[dict]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for adv in advisories:
        for aff in adv.get("affected") or []:
            if isinstance(aff, dict) and aff.get("type") == "model":
                for ident in aff.get("ids") or []:
                    index.setdefault(ident, []).append(adv)
    return index


_REPL_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:\-]*")


def retiring_replacements(text: str | None, index: dict[str, list[dict]], exclude_id: str | None = None):
    """Yield (token, advisory) for replacement model ids that are themselves retiring."""
    if not text:
        return
    seen = set()
    for tok in _REPL_TOKEN.findall(text):
        for adv in index.get(tok, []):
            if adv.get("id") != exclude_id and (tok, adv.get("id")) not in seen:
                seen.add((tok, adv.get("id")))
                yield tok, adv


def validate(advisories: list[dict], today: dt.date | None = None) -> list[Issue]:
    today = today or dt.date.today()
    issues: list[Issue] = []
    seen_ids: dict[str, str] = {}
    ident_owner: dict[tuple[str, str], str] = {}

    for n, adv in enumerate(advisories):
        aid = adv.get("id") if isinstance(adv.get("id"), str) else f"#{n}"
        where = f"{adv.get('_file', '?')}: {aid}"

        def err(msg, where=where):
            issues.append(Issue("error", where, msg))

        def warn(msg, where=where):
            issues.append(Issue("warning", where, msg))

        for key in adv:
            if key not in ADVISORY_KEYS and key != "_file":
                err(f"unknown field {key!r}")
        for key in REQUIRED_KEYS:
            if adv.get(key) in (None, "", []):
                err(f"missing required field {key!r}")

        if isinstance(adv.get("id"), str):
            if not ID_RE.match(adv["id"]):
                err("id must look like 'vendor/slug' (lowercase letters, digits, . + - _)")
            if adv["id"] in seen_ids:
                err(f"duplicate id (also in {seen_ids[adv['id']]})")
            seen_ids[adv["id"]] = adv.get("_file", "?")

        for key in ("title", "vendor", "replacement", "summary", "origin"):
            if key in adv and adv[key] is not None and not isinstance(adv[key], str):
                err(f"{key} must be a string")
        if isinstance(adv.get("title"), str) and len(adv["title"]) > 140:
            warn("title is longer than 140 characters")

        if adv.get("kind") is not None and adv.get("kind") not in KINDS:
            err(f"kind must be one of {', '.join(KINDS)}")

        eff = parse_date(adv.get("effective"))
        if adv.get("effective") is not None and eff is None:
            err("effective must be a date like 2026-12-11")
        elif eff and not (dt.date(2000, 1, 1) <= eff <= dt.date(2100, 1, 1)):
            err("effective date is out of range")
        if "announced" in adv:
            ann = parse_date(adv["announced"])
            if ann is None:
                err("announced must be a date like 2026-06-11")
            elif eff and ann > eff:
                err("announced is after effective")
        if "last_verified" in adv:
            lv = parse_date(adv["last_verified"])
            if lv is None:
                err("last_verified must be a date")
            elif lv > today + dt.timedelta(days=1):
                err("last_verified is in the future")

        if adv.get("source") is not None and not _https(adv.get("source")):
            err("source must be an https:// URL")
        if "references" in adv and not (
            isinstance(adv["references"], list) and all(_https(u) for u in adv["references"])
        ):
            err("references must be a list of https:// URLs")
        if "fallback" in adv and not isinstance(adv["fallback"], bool):
            err("fallback must be true or false")

        blocks = adv.get("blocks")
        if adv.get("kind") == "upgrade-blocker":
            if not isinstance(blocks, dict):
                err("upgrade-blocker advisories need 'blocks: {ecosystem, package, versions}'")
            else:
                if blocks.get("ecosystem") not in ECOSYSTEMS:
                    err(f"blocks.ecosystem must be one of {', '.join(ECOSYSTEMS)}")
                if not isinstance(blocks.get("package"), str):
                    err("blocks.package is required")
                try:
                    parse_range(blocks.get("versions") or "")
                except ValueError as exc:
                    err(f"blocks.versions: {exc}")
        elif blocks is not None:
            err("'blocks' is only valid for kind: upgrade-blocker")

        affected = adv.get("affected")
        if affected is not None and not isinstance(affected, list):
            err("affected must be a list")
            affected = []
        for k, aff in enumerate(affected or []):
            if not isinstance(aff, dict):
                err(f"affected[{k}] must be a mapping")
                continue
            for key in aff:
                if key not in AFFECTED_KEYS:
                    err(f"affected[{k}]: unknown field {key!r}")
            typ = aff.get("type")
            if typ not in TYPES:
                err(f"affected[{k}].type must be one of {', '.join(TYPES)}")
                continue
            if typ == "pattern":
                pat = aff.get("pattern")
                if not isinstance(pat, str) or not pat:
                    err(f"affected[{k}]: pattern type needs 'pattern'")
                else:
                    flags = aff.get("flags", "")
                    if not isinstance(flags, str) or set(flags) - set("im"):
                        err(f"affected[{k}].flags may only contain 'i' and 'm'")
                    try:
                        re.compile(pat)
                    except re.error as exc:
                        err(f"affected[{k}]: bad regex: {exc}")
                if "ids" in aff:
                    err(f"affected[{k}]: pattern type uses 'pattern', not 'ids'")
            else:
                if not _str_list(aff.get("ids")):
                    err(f"affected[{k}]: {typ} type needs a non-empty 'ids' list")
                elif typ in ("model", "image"):
                    for ident in aff["ids"]:
                        if any(ch.isspace() for ch in ident):
                            err(f"affected[{k}]: {typ} id {ident!r} contains whitespace")
                        owner = ident_owner.get((typ, ident))
                        if owner and owner != aid:
                            warn(f"{typ} id {ident!r} is also listed by {owner}")
                        ident_owner[(typ, ident)] = aid
            if "versions" in aff:
                if typ not in ("npm", "pypi"):
                    err(f"affected[{k}]: 'versions' only applies to npm/pypi")
                else:
                    try:
                        parse_range(aff["versions"])
                    except ValueError as exc:
                        err(f"affected[{k}].versions: {exc}")
            for key in ("context", "files"):
                if key in aff and not _str_list(aff[key]):
                    err(f"affected[{k}].{key} must be a non-empty list of strings")
            if "quoted" in aff and (typ != "model" or not isinstance(aff["quoted"], bool)):
                err(f"affected[{k}].quoted is a boolean for model entries only")

    index = model_id_index(advisories)
    for adv in advisories:
        texts = [adv.get("replacement")] + [a.get("replacement") for a in adv.get("affected") or [] if isinstance(a, dict)]
        for text in texts:
            for tok, other in retiring_replacements(text, index, exclude_id=adv.get("id")):
                issues.append(Issue(
                    "info", f"{adv.get('_file', '?')}: {adv.get('id')}",
                    f"replacement {tok!r} is itself retiring ({other.get('id')}, {other.get('effective')})",
                ))
    return issues


# --------------------------------------------------------------- links

def _probe_once(url: str, timeout: float, opener=None) -> tuple[int | None, str | None]:
    """(HTTP status, network error) for one HEAD-then-GET attempt."""
    opener = opener or urllib.request.urlopen
    headers = {"User-Agent": "sunset-scan-linkcheck/0.1 (+https://github.com/larcx-idea/sunset-scan)"}
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method, headers=headers)
        try:
            with opener(req, timeout=timeout) as resp:
                return resp.status, None
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and (exc.code in (400, 403, 404, 405, 429) or exc.code >= 500):
                continue  # some servers reject HEAD; try GET
            return exc.code, None
        except Exception as exc:  # network errors, timeouts
            if method == "HEAD":
                continue
            return None, str(exc)
    return None, "unreachable"


def probe_url(url: str, timeout: float = 15.0, attempts: int = 3, backoff: float = 2.0,
              opener=None, sleep=None) -> tuple[int | None, str | None]:
    """Probe a URL, retrying server errors (5xx), rate limits and network failures."""
    import time

    sleep = sleep or time.sleep
    status, error = None, "unreachable"
    for n in range(attempts):
        status, error = _probe_once(url, timeout, opener)
        transient = status is None or status >= 500 or status == 429
        if not transient:
            return status, error
        if n + 1 < attempts:
            sleep(backoff * (n + 1))
    return status, error


def check_links(advisories: list[dict], timeout: float = 15.0, workers: int = 8, attempts: int = 3,
                opener=None, sleep=None) -> list[Issue]:
    urls: dict[str, list[str]] = {}
    for adv in advisories:
        for u in [adv.get("source")] + list(adv.get("references") or []):
            if isinstance(u, str):
                urls.setdefault(u, []).append(adv.get("id", "?"))

    def probe(url: str):
        status, error = probe_url(url, timeout=timeout, attempts=attempts, opener=opener, sleep=sleep)
        return url, status, error

    issues = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for url, status, error in pool.map(probe, sorted(urls)):
            ids = ", ".join(sorted(set(urls[url]))[:3])
            if status is not None and 200 <= status < 400:
                continue
            if status in (401, 403, 429, 999):
                issues.append(Issue("warning", ids, f"could not verify {url} (HTTP {status}; site blocks bots?)"))
            elif status is not None and status >= 500:
                # Still failing after retries, but a server error says nothing about whether
                # the page moved; flag it without failing CI on someone else's outage.
                issues.append(Issue("warning", ids, f"could not verify {url} (HTTP {status} after {attempts} tries)"))
            elif status is not None:
                issues.append(Issue("error", ids, f"broken source link {url} (HTTP {status})"))
            else:
                issues.append(Issue("error", ids, f"unreachable source link {url} ({error}, {attempts} tries)"))
    return issues


# --------------------------------------------------------------- bundling

def compile_bundle(advisories: list[dict]) -> dict:
    cleaned = []
    for adv in advisories:
        cleaned.append({k: v for k, v in adv.items() if not k.startswith("_")})
    cleaned.sort(key=lambda a: (str(a.get("effective")), a.get("id", "")))
    verified = [a.get("last_verified") for a in cleaned if isinstance(a.get("last_verified"), str)]
    origins: dict[str, int] = {}
    for a in cleaned:
        origins[a.get("origin", "curated")] = origins.get(a.get("origin", "curated"), 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "registry_date": max(verified) if verified else None,
        "count": len(cleaned),
        "origins": dict(sorted(origins.items())),
        "advisories": cleaned,
    }


def dump_bundle(bundle: dict) -> str:
    return json.dumps(bundle, indent=1, ensure_ascii=False, sort_keys=False) + "\n"
