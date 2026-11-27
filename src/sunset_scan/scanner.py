"""Walk a repository and match it against sunset advisories."""

from __future__ import annotations

import bisect
import datetime as dt
import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .manifests import LOCKFILES, PackageRef, manifest_kind, normalize, parse_manifest
from .registry import model_id_index, parse_date, retiring_replacements
from .versions import parse_version, satisfies

DEFAULT_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "bower_components", "jspm_packages", "vendor",
    ".venv", "venv", ".tox", ".nox", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "site-packages", "dist", "build", ".next", ".nuxt", ".svelte-kit", ".output", ".turbo",
    ".parcel-cache", ".cache", "coverage", ".terraform", ".gradle", "target", ".idea", ".vscode",
    ".yarn", ".pnpm-store", "Pods", "DerivedData", ".eggs",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff", ".svgz", ".pdf",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".war", ".class", ".so", ".dylib",
    ".dll", ".exe", ".bin", ".o", ".a", ".pyc", ".pyo", ".whl", ".egg", ".woff", ".woff2", ".ttf",
    ".otf", ".eot", ".mp3", ".mp4", ".mov", ".avi", ".webm", ".wav", ".flac", ".ogg", ".psd",
    ".sketch", ".fig", ".db", ".sqlite", ".sqlite3", ".parquet", ".feather", ".npy", ".npz", ".pkl",
    ".h5", ".onnx", ".pt", ".safetensors", ".lockb", ".map", ".ipynb_checkpoints",
}
DOC_EXT = {".md", ".mdx", ".markdown", ".rst", ".adoc", ".txt"}
MAX_TEXT_BYTES = 2_000_000
MAX_MANIFEST_BYTES = 60_000_000
IGNORE_MARK = "sunset-scan: ignore"
IGNORE_MARK_COMPACT = "sunset-scan:ignore"


# ------------------------------------------------------------------ ignores

def glob_match(pattern: str, relpath: str) -> bool:
    """gitignore-flavoured glob: bare names match any path component; '/x' anchors; 'dir/' matches below."""
    parts = relpath.split("/")
    pat = pattern.strip().rstrip("/")
    if not pat:
        return False
    anchored = pat.startswith("/")
    pat = pat.lstrip("/")
    if "/" not in pat and not anchored:
        return any(fnmatch.fnmatchcase(p, pat) for p in parts)
    prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    candidates = [pat] + ([pat[3:]] if pat.startswith("**/") else [])
    return any(fnmatch.fnmatchcase(pre, c) for pre in prefixes for c in candidates)


_ID_LINE = re.compile(r"^id:\s*(\S+)(?:\s+until\s+(\S+))?\s*(?:#.*)?$")


def find_ignore_file(root: Path) -> Path | None:
    """``.sunsetignore`` in ``root``, else in a parent directory up to the git repository root.

    Parents are only searched when ``root`` is inside a git checkout, so a stray file
    above an unrelated directory is never picked up.
    """
    root = Path(root).resolve()
    here = root / ".sunsetignore"
    if here.is_file():
        return here
    chain = [root, *root.parents]
    git_root = next((d for d in chain if (d / ".git").exists()), None)
    if git_root is None:
        return None
    for d in chain[1:]:
        if (d / ".sunsetignore").is_file():
            return d / ".sunsetignore"
        if d == git_root:
            break
    return None


@dataclass
class IgnoreConfig:
    path_globs: list[str] = field(default_factory=list)  # from the ignore file (relative to its directory)
    extra_globs: list[str] = field(default_factory=list)  # from --exclude (relative to the scanned root)
    ids: dict[str, dt.date | None] = field(default_factory=dict)
    expired: list[tuple[str, dt.date]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # lines that could not be understood
    source: str | None = None
    prefix: str = ""  # scanned root relative to the ignore file's directory

    @classmethod
    def load(cls, root: Path, today: dt.date, file: str | None = None,
             extra_globs=(), extra_ids=(), search_parents: bool = True) -> "IgnoreConfig":
        cfg = cls(extra_globs=list(extra_globs))
        for i in extra_ids:
            cfg.ids[i] = None
        if file:
            path: Path | None = Path(file)
        elif search_parents:
            path = find_ignore_file(Path(root))
        else:
            path = Path(root) / ".sunsetignore"
        if path is None or not path.is_file():
            if file:
                cfg.problems.append(f"ignore file {file} was not found; nothing was ignored or snoozed")
            return cfg
        cfg.source = str(path)
        # Globs in the file are relative to the file's directory, like .gitignore.
        try:
            rel = Path(root).resolve().relative_to(path.resolve().parent)
            cfg.prefix = "" if str(rel) == "." else rel.as_posix()
        except ValueError:
            cfg.prefix = ""  # an ignore file elsewhere: match relative to the scanned root
        name = path.name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            cfg.problems.append(f"could not read {path}: {exc}; nothing from it was applied")
            return cfg
        for n, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("id:"):
                m = _ID_LINE.match(line)
                if not m:
                    cfg.problems.append(
                        f"{name}:{n}: could not read {line!r} (expected 'id: <advisory-id> until YYYY-MM-DD'); "
                        "nothing was snoozed by this line")
                    continue
                aid, until_text = m.group(1), m.group(2)
                until = parse_date(until_text) if until_text else None
                if until_text and until is None:
                    # A typo must never turn a time-limited snooze into a permanent one.
                    cfg.problems.append(
                        f"{name}:{n}: 'until {until_text}' is not a real date (YYYY-MM-DD), "
                        f"so {aid} is NOT snoozed; fix the date")
                    continue
                if until and until < today:
                    cfg.expired.append((aid, until))
                else:
                    cfg.ids[aid] = until
            else:
                glob = re.sub(r"\s+#.*$", "", line)
                if glob:
                    cfg.path_globs.append(glob)
        return cfg

    def path_ignored(self, relpath: str) -> bool:
        full = f"{self.prefix}/{relpath}" if self.prefix else relpath
        return any(glob_match(g, full) for g in self.path_globs) or any(
            glob_match(g, relpath) for g in self.extra_globs)


# ------------------------------------------------------------------ results

@dataclass
class Location:
    path: str
    line: int
    col: int
    match: str
    snippet: str
    confidence: str = "high"  # high | low
    reason: str | None = None  # why low: comment | docs | tokenizer | no AI context | version unknown
    detail: str | None = None
    fallback: bool = False
    also: list[str] = field(default_factory=list)  # other advisories matching the same text, with another status


# Kinds whose date passing does not necessarily make calls fail: unmaintained software
# keeps running without fixes, and an expired API version is often upgraded server-side.
SOFT_KINDS = ("unmaintained", "behavior-change")


@dataclass
class Finding:
    advisory: dict
    locations: list[Location]
    status: str  # past | upcoming | conflict | blocker
    days_left: int | None
    replacements: list[str]  # the vendor's suggested replacement, verbatim
    conflict: str | None = None
    retiring: list[tuple[str, str, str]] = field(default_factory=list)  # (token, advisory id, date)
    suggested: list[str] = field(default_factory=list)  # replacements with retiring ids swapped for their successors

    @property
    def id(self) -> str:
        return self.advisory["id"]

    @property
    def high(self) -> bool:
        return any(loc.confidence == "high" for loc in self.locations)

    @property
    def bucket(self) -> str:
        if self.status == "conflict":
            return "broken"
        if self.status == "past":
            return "expired" if self.advisory.get("kind") in SOFT_KINDS else "broken"
        if self.status == "blocker":
            return "blocker"
        d = self.days_left
        if d is None:
            return "later"
        if d <= 30:
            return "urgent"
        if d <= 90:
            return "soon"
        if d <= 365:
            return "later"
        return "future"

    def sort_key(self):
        order = {"past": 0, "conflict": 0, "upcoming": 1, "blocker": 2}[self.status]
        days = self.days_left if self.days_left is not None else 10**6
        return (order, days, self.id)


@dataclass
class ScanResult:
    root: str
    today: dt.date
    findings: list[Finding]
    files_scanned: int
    packages_seen: int
    registry_meta: dict
    advisory_count: int
    notes: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ rules

_ID_CHARS = r"A-Za-z0-9_"


def _alt(ids):
    return "|".join(re.escape(i) for i in sorted(set(ids), key=len, reverse=True))


def _literal_regex(ids) -> re.Pattern:
    parts = []
    for ident in sorted(set(ids), key=len, reverse=True):
        p = re.escape(ident)
        if re.match(r"[A-Za-z0-9_]", ident[0]):
            p = rf"(?<![{_ID_CHARS}])" + p
        if re.match(r"[A-Za-z0-9_]", ident[-1]):
            p = p + rf"(?![{_ID_CHARS}])"
        parts.append(p)
    return re.compile("|".join(parts))


def compile_affected(aff: dict) -> re.Pattern | None:
    typ = aff["type"]
    if typ == "model":
        if aff.get("quoted"):
            return re.compile(r"(?<=[\"'`])(?:" + _alt(aff["ids"]) + r")(?=[\"'`])")
        return re.compile(r"(?<![A-Za-z0-9_.\-])(?:" + _alt(aff["ids"]) + r")(?![A-Za-z0-9_\-]|\.[A-Za-z0-9])")
    if typ == "image":
        return re.compile(r"(?<![A-Za-z0-9_.\-])(?:" + _alt(aff["ids"]) + r")(?![A-Za-z0-9_\-/]|\.[A-Za-z0-9])")
    if typ == "literal":
        return _literal_regex(aff["ids"])
    if typ == "pattern":
        flags = 0
        if "i" in aff.get("flags", ""):
            flags |= re.IGNORECASE
        if "m" in aff.get("flags", ""):
            flags |= re.MULTILINE
        return re.compile(aff["pattern"], flags)
    return None


def required_literal(pattern: str, flags: int = 0) -> str | None:
    """Longest literal run every match of ``pattern`` must contain (for cheap prefiltering)."""
    try:
        from re import _constants as C, _parser
        parsed = list(_parser.parse(pattern, flags))
    except Exception:  # internal API changed or odd pattern: just skip the prefilter
        return None
    runs, cur = [], []
    for op, av in parsed:
        if op is C.LITERAL:
            cur.append(chr(av))
        elif op in (C.ASSERT, C.ASSERT_NOT, C.AT):
            continue  # zero-width
        else:
            runs.append("".join(cur))
            cur = []
    runs.append("".join(cur))
    runs = [r for r in runs if len(r) >= 3]
    if not runs:
        return None
    # Rarer is better: a run with digits or punctuation ("v21.0", "@v3") beats a plain word ("version").
    return max(runs, key=lambda r: len(r) + (6 if any(not ch.isalpha() for ch in r) else 0))


# Characters a model id is made of. A bounded occurrence of a model id in text is
# exactly one maximal run of these characters (minus trailing dots), so a set of
# runs per file answers "could this id be here" with one hash lookup.
_MODEL_RUN = re.compile(r"[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*")
_MODEL_ID_OK = re.compile(r"^[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*$")


def model_tokens(text: str) -> set[str]:
    return set(_MODEL_RUN.findall(text))


@dataclass
class TextRule:
    advisory: dict
    affected: dict
    regex: re.Pattern
    kind: str  # model | image | literal | pattern
    token_ids: list[str]  # ids checked against the per-file token set
    substr_ids: list[str]  # ids checked with a substring test
    required: str | None  # pattern prefilter
    required_ci: bool
    context: list[str] | None
    files: list[str] | None

    def may_match(self, text: str, lower_fn) -> bool:
        if self.kind in ("model", "image", "literal"):
            return any(i in text for i in self.substr_ids)
        if self.required:
            return self.required.lower() in lower_fn() if self.required_ci else self.required in text
        return True


def build_text_rules(advisories: list[dict]) -> list[TextRule]:
    rules = []
    for adv in advisories:
        for aff in adv.get("affected") or []:
            rx = compile_affected(aff)
            if rx is None:
                continue
            typ = aff["type"]
            token_ids, substr_ids, required, ci = [], [], None, False
            if typ == "model":
                for ident in aff["ids"]:
                    (token_ids if _MODEL_ID_OK.match(ident) else substr_ids).append(ident)
            elif typ in ("image", "literal"):
                substr_ids = list(aff["ids"])
            else:
                required = required_literal(rx.pattern, rx.flags)
                ci = bool(rx.flags & re.IGNORECASE)
            ctx = [c.lower() for c in aff.get("context") or []] or None
            rules.append(TextRule(adv, aff, rx, typ, token_ids, substr_ids, required, ci, ctx, aff.get("files")))
    return rules


# Very short model ids (two characters) also appear as ordinary strings. Without one of
# these words somewhere in the file, a quoted match is only a "check this".
AI_CONTEXT_WORDS = ("openai", "model", "llm", "gpt", "completion", "anthropic", "reasoning", "prompt")
# Tokenizer lookups such as tiktoken.encoding_for_model(<model id>) run locally and keep
# working after the model's API shutdown.
_TOKENIZER_CALL = re.compile(r"(?:encoding_for_model|encodingForModel)\s*\(\s*[\"'`]?\s*$")


def _in_comment(line: str, col: int) -> bool:
    prefix = line[:col]
    s = prefix.lstrip()
    if s.startswith(("#", "//", "/*", "* ", "*/", "-- ", "<!--", ";", "rem ", "REM ")) or s in ("*", "--"):
        return True
    for marker in (" # ", " //", "\t#", "\t//", " /*", " <!--"):
        idx = prefix.find(marker)
        if idx != -1:
            before = prefix[:idx]
            if before.count('"') % 2 == 0 and before.count("'") % 2 == 0 and before.count("`") % 2 == 0:
                return True
    return False


def _is_text_candidate(path: Path) -> bool:
    name = path.name
    if name in LOCKFILES or name == ".sunsetignore":
        return False
    low = name.lower()
    if low.endswith((".min.js", ".min.css", ".min.mjs")):
        return False
    return path.suffix.lower() not in BINARY_EXT


def _walk(root: Path, ignore: IgnoreConfig, default_excludes: bool):
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        keep = []
        for d in sorted(dirnames):
            rel = f"{rel_dir}/{d}" if rel_dir else d
            if default_excludes and (d in DEFAULT_EXCLUDE_DIRS or d.endswith(".egg-info")):
                continue
            if ignore.path_ignored(rel):
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            rel = f"{rel_dir}/{f}" if rel_dir else f
            if ignore.path_ignored(rel):
                continue
            yield Path(dirpath) / f, rel


def _decode(data: bytes) -> str | None:
    """Decode file bytes as text; None if they look binary. Handles UTF-16 with or without a BOM."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return None
    sample = data[:8192]
    if b"\x00" not in sample:
        return data.decode("utf-8", errors="replace")
    even, odd = sample[0::2], sample[1::2]
    if len(sample) >= 4:
        if odd.count(0) >= 0.9 * len(odd) and even.count(0) <= 0.1 * len(even):
            return data.decode("utf-16-le", errors="replace")
        if even.count(0) >= 0.9 * len(even) and odd.count(0) <= 0.1 * len(odd):
            return data.decode("utf-16-be", errors="replace")
    return None


def _examples(paths: list[str], n: int = 3) -> str:
    shown = ", ".join(paths[:n])
    return shown + (f" and {len(paths) - n} more" if len(paths) > n else "")


def _prefer_lockfiles(packages: list[PackageRef]) -> list[PackageRef]:
    """Drop declared-range-only entries when a lockfile at or above them resolved the package.

    ``"eslint-plugin-react": "^7.37.0"`` with a lockfile that installed 7.38.0 must not be
    judged by the range's lower bound.
    """
    resolved: dict[tuple[str, str], list[str]] = {}
    for ref in packages:
        if ref.version and ref.path.rsplit("/", 1)[-1] in LOCKFILES:
            d = ref.path.rsplit("/", 1)[0] if "/" in ref.path else ""
            resolved.setdefault((ref.ecosystem, normalize(ref.ecosystem, ref.name)), []).append(d)
    out = []
    for ref in packages:
        dirs = resolved.get((ref.ecosystem, normalize(ref.ecosystem, ref.name)))
        if ref.version is None and dirs:
            d = ref.path.rsplit("/", 1)[0] if "/" in ref.path else ""
            if any(ld == "" or d == ld or d.startswith(ld + "/") for ld in dirs):
                continue
        out.append(ref)
    return out


def _replacement_for(token: str, adv: dict) -> str | None:
    for aff in adv.get("affected") or []:
        if token in (aff.get("ids") or []) and aff.get("replacement"):
            return aff["replacement"]
    return adv.get("replacement")


def suggest_replacement(text: str, index: dict[str, list[dict]], exclude_id: str | None = None) -> str:
    """Swap model ids in a replacement that are themselves retiring for their own replacement.

    Follows chains (A -> B, where B retires too -> C) so the first thing a user reads
    is something that still works.
    """
    seen = {exclude_id}
    out = text
    for _ in range(6):
        changed = False
        for tok, other in list(retiring_replacements(out, index)):
            if other.get("id") in seen:
                continue
            nxt = _replacement_for(tok, other)
            if not nxt:
                continue
            seen.add(other.get("id"))
            out = re.sub(rf"(?<![A-Za-z0-9._:\-]){re.escape(tok)}(?![A-Za-z0-9._:\-])",
                         lambda _m, nxt=nxt: nxt, out, count=1)
            changed = True
            break
        if not changed:
            break
    return out


def _overlap(a: Location, b: Location) -> bool:
    a0, a1 = a.col, a.col + max(len(a.match), 1)
    b0, b1 = b.col, b.col + max(len(b.match), 1)
    return a0 < b1 and b0 < a1


def scan(root, advisories: list[dict], registry_meta: dict | None = None, today: dt.date | None = None,
         ignore: IgnoreConfig | None = None, include_docs: bool = True,
         default_excludes: bool = True) -> ScanResult:
    root = Path(root)
    today = today or dt.date.today()
    ignore = ignore or IgnoreConfig()
    notes: list[str] = []
    if ignore.source and ignore.prefix:
        notes.append(f"using {ignore.source}; its paths are matched relative to {Path(ignore.source).parent}")
    notes.extend(ignore.problems)
    for aid, until in ignore.expired:
        notes.append(f"snooze for {aid} expired on {until.isoformat()}; it is being reported again")
    known = {a["id"] for a in advisories}
    for aid in ignore.ids:
        if aid not in known:
            notes.append(f"ignored id {aid!r} does not match any advisory (typo?); nothing was snoozed by it")

    active = [a for a in advisories if a["id"] not in ignore.ids]
    rules = build_text_rules(active)
    order = {id(r): n for n, r in enumerate(rules)}
    by_token: dict[str, list[TextRule]] = {}
    for r in rules:
        for ident in r.token_ids:
            by_token.setdefault(ident, []).append(r)
    unindexed = [r for r in rules if not r.token_ids or r.substr_ids]
    package_affected = [
        (adv, aff) for adv in active for aff in adv.get("affected") or [] if aff["type"] in ("npm", "pypi")
    ]

    hits: dict[str, dict[tuple[str, int], Location]] = {}
    matched_aff: dict[str, list[dict]] = {}
    packages: list[PackageRef] = []
    files_scanned = 0
    unreadable: list[str] = []
    binary: list[str] = []
    too_large: list[str] = []
    bad_manifests: list[str] = []

    def add(adv, aff, loc: Location):
        bucket = hits.setdefault(adv["id"], {})
        key = (loc.path, loc.line)
        if key not in bucket or (bucket[key].confidence == "low" and loc.confidence == "high"):
            bucket[key] = loc
        lst = matched_aff.setdefault(adv["id"], [])
        if aff not in lst:
            lst.append(aff)

    entries = [(root, root.name)] if root.is_file() else list(_walk(root, ignore, default_excludes))

    for path, rel in entries:
        try:
            size = path.stat().st_size
        except OSError:
            unreadable.append(rel)
            continue
        mkind = manifest_kind(rel)
        if mkind and size <= MAX_MANIFEST_BYTES:
            problems: list[str] = []
            try:
                packages.extend(parse_manifest(rel, path.read_text(encoding="utf-8", errors="replace"), problems))
            except Exception as exc:  # never let one odd manifest kill the scan
                bad_manifests.append(f"{rel} ({exc.__class__.__name__}: {str(exc)[:80]})")
            bad_manifests.extend(problems)
        if not _is_text_candidate(path):
            continue
        if size > MAX_TEXT_BYTES:
            too_large.append(rel)
            continue
        try:
            data = path.read_bytes()
        except OSError:
            unreadable.append(rel)
            continue
        is_doc = path.suffix.lower() in DOC_EXT and not mkind
        if is_doc and not include_docs:
            continue
        text = _decode(data)
        if text is None:
            binary.append(rel)
            continue
        files_scanned += 1
        cache: dict = {}

        def lower_fn(cache=cache, text=text):
            if "lower" not in cache:
                cache["lower"] = text.lower()
            return cache["lower"]

        indexed = {id(r): r for tok in model_tokens(text) & by_token.keys() for r in by_token[tok]}
        candidates = sorted(
            list(indexed.values()) + [r for r in unindexed if id(r) not in indexed and r.may_match(text, lower_fn)],
            key=lambda r: order[id(r)],
        )
        line_starts = None
        lines = None
        for rule in candidates:
            if rule.files and not any(glob_match(g, rel) for g in rule.files):
                continue
            if rule.context and not any(c in lower_fn() for c in rule.context):
                continue
            for m in rule.regex.finditer(text):
                if line_starts is None:
                    line_starts = [0] + [i + 1 for i, ch in enumerate(text) if ch == "\n"]
                    lines = text.split("\n")
                ln = bisect.bisect_right(line_starts, m.start()) - 1
                col = m.start() - line_starts[ln] + (len(m.group(0)) - len(m.group(0).lstrip()))
                line = lines[ln]
                if IGNORE_MARK in line or IGNORE_MARK_COMPACT in line.replace(" ", ""):
                    continue
                if ln > 0 and "sunset-scan: ignore-next-line" in lines[ln - 1]:
                    continue
                if is_doc:
                    reason = "docs"
                elif _in_comment(line, col):
                    reason = "comment"
                elif rule.kind == "model" and _TOKENIZER_CALL.search(line[:col]):
                    reason = "tokenizer"
                elif rule.kind == "model" and rule.affected.get("quoted") and not any(
                        w in lower_fn() for w in AI_CONTEXT_WORDS):
                    reason = "no AI context"
                else:
                    reason = None
                add(rule.advisory, rule.affected, Location(
                    path=rel, line=ln + 1, col=col + 1, match=m.group(0).strip()[:120],
                    snippet=line.strip()[:200], confidence="low" if reason else "high", reason=reason,
                    fallback=bool(rule.advisory.get("fallback")),
                ))

    packages = _prefer_lockfiles(packages)
    by_name: dict[tuple[str, str], list[PackageRef]] = {}
    for ref in packages:
        by_name.setdefault((ref.ecosystem, normalize(ref.ecosystem, ref.name)), []).append(ref)

    for adv, aff in package_affected:
        for ident in aff["ids"]:
            for ref in by_name.get((aff["type"], normalize(aff["type"], ident)), []):
                unknown = bool(aff.get("versions")) and parse_version(ref.effective_version) is None
                if not unknown and not satisfies(ref.effective_version, aff.get("versions")):
                    continue
                add(adv, aff, Location(
                    path=ref.path, line=ref.line, col=1, match=ref.name, snippet=ref.describe,
                    detail=ref.describe + (f", affected {aff['versions']}" if unknown else ""),
                    fallback=bool(adv.get("fallback")),
                    confidence="low" if unknown else "high", reason="version unknown" if unknown else None,
                ))

    # drop catch-all (fallback) hits where a specific advisory matched the same line
    specific = {
        (loc.path, loc.line) for bucket in hits.values() for loc in bucket.values() if not loc.fallback
    }
    index = model_id_index(advisories)
    adv_by_id = {a["id"]: a for a in active}
    findings: list[Finding] = []
    for aid, bucket in hits.items():
        locs = [loc for loc in bucket.values() if not (loc.fallback and (loc.path, loc.line) in specific)]
        if not locs:
            continue
        locs.sort(key=lambda loc: (loc.confidence != "high", loc.path, loc.line))
        adv = adv_by_id[aid]
        eff = parse_date(adv.get("effective"))
        days = (eff - today).days if eff else None
        conflict = None
        if adv.get("kind") == "upgrade-blocker":
            blk = adv.get("blocks") or {}
            refs = by_name.get((blk.get("ecosystem"), normalize(blk.get("ecosystem", ""), blk.get("package", ""))), [])
            hit = next((r for r in refs if r.effective_version and satisfies(r.effective_version, blk.get("versions"))), None)
            if hit:
                status = "conflict"
                conflict = f"{hit.describe} in {hit.path}"
                days = min(days, 0) if days is not None else 0
            else:
                status = "blocker"
                days = None
        else:
            status = "past" if days is not None and days < 0 else "upcoming"
        repls: list[str] = []
        for aff in matched_aff.get(aid, []):
            r = aff.get("replacement") or adv.get("replacement")
            if r and r not in repls:
                repls.append(r)
        if not repls and adv.get("replacement"):
            repls.append(adv["replacement"])
        retiring = []
        for r in repls:
            for tok, other in retiring_replacements(r, index, exclude_id=aid):
                retiring.append((tok, other["id"], other.get("effective", "")))
        suggested: list[str] = []
        for r in repls:
            s = suggest_replacement(r, index, exclude_id=aid)
            if s not in suggested:
                suggested.append(s)
        findings.append(Finding(adv, locs, status, days, repls, conflict, retiring, suggested))

    # One piece of text matched by advisories with different statuses (e.g. one version
    # string that is both an expired Marketing API version and a Graph API version with
    # years left):
    # say so on the location instead of presenting two unrelated, contradictory verdicts.
    by_line: dict[tuple[str, int], list[tuple[Finding, Location]]] = {}
    for f in findings:
        for loc in f.locations:
            by_line.setdefault((loc.path, loc.line), []).append((f, loc))
    for pairs in by_line.values():
        if len(pairs) < 2:
            continue
        for f, loc in pairs:
            for g, other in pairs:
                if g is f or g.bucket == f.bucket or not _overlap(loc, other):
                    continue
                tag = f"{g.id} ({g.advisory.get('effective')})"
                if tag not in loc.also:
                    loc.also.append(tag)

    findings.sort(key=lambda f: f.sort_key())
    if unreadable:
        notes.append(f"{len(unreadable)} file{'s' if len(unreadable) != 1 else ''} could not be read and "
                     f"{'were' if len(unreadable) != 1 else 'was'} not checked: {_examples(unreadable)}")
    if binary:
        notes.append(f"{len(binary)} file{'s' if len(binary) != 1 else ''} looked binary and "
                     f"{'were' if len(binary) != 1 else 'was'} not checked: {_examples(binary)}")
    if too_large:
        notes.append(f"{len(too_large)} file{'s' if len(too_large) != 1 else ''} over "
                     f"{MAX_TEXT_BYTES // 1_000_000} MB {'were' if len(too_large) != 1 else 'was'} not checked "
                     f"for identifiers: {_examples(too_large)}")
    for problem in bad_manifests[:5]:
        notes.append(f"could not fully parse {problem}; packages in it may be missing from this report")
    if len(bad_manifests) > 5:
        notes.append(f"... and {len(bad_manifests) - 5} more manifest problems")
    meta = dict(registry_meta or {})
    reg_date = parse_date(meta.get("registry_date"))
    if reg_date and (today - reg_date).days > 60:
        notes.append(
            f"the bundled registry is {(today - reg_date).days} days old; upgrade sunset-scan for newer advisories"
        )
    return ScanResult(
        root=str(root), today=today, findings=findings, files_scanned=files_scanned,
        packages_seen=len(packages), registry_meta=meta, advisory_count=len(advisories), notes=notes,
    )


def is_failing(f: Finding, fail_within: int | None, strict: bool, fail_on_blockers: bool) -> bool:
    if not strict and not f.high:
        return False
    if f.status in ("past", "conflict"):
        return fail_within is not None
    if f.status == "blocker":
        return fail_on_blockers
    return fail_within is not None and f.days_left is not None and f.days_left <= fail_within
