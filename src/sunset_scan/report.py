"""Render scan results as text, JSON, Markdown (for issues) or SARIF (for code scanning)."""

from __future__ import annotations

import hashlib
import json
import os
import sys

from . import __version__
from .scanner import Finding, ScanResult

REPO_URL = "https://github.com/larcx-idea/sunset-scan"
BUCKET_ORDER = ("broken", "expired", "urgent", "soon", "later", "future", "blocker")
BUCKET_TITLES = {
    "broken": "broken",
    "expired": "past the date but may still run",
    "urgent": "within 30 days",
    "soon": "within 90 days",
    "later": "within a year",
    "future": "later",
    "blocker": "upgrade blockers",
}


def when_label(f: Finding) -> tuple[str, str]:
    d = f.days_left
    if not f.high:
        # Every match is in a comment, docs, a tokenizer call, an unclear context or a
        # package whose version is unknown: ask for a look instead of declaring it broken.
        if f.status == "conflict":
            return "CHECK", "now"
        if f.status == "blocker":
            return "CHECK", "on upgrade"
        if d is not None and d < 0:
            return "CHECK", f"{-d} day{'s' if d != -1 else ''} ago"
        if d == 0:
            return "CHECK", "today"
    if f.status == "conflict":
        return "BROKEN", "now"
    if f.status == "blocker":
        return "BLOCKER", "on upgrade"
    if f.status == "past":
        n = -d
        # Only shutdowns and removals are called BROKEN. Unmaintained software keeps running
        # without fixes, and an expired API version may still answer (often upgraded).
        label = {"unmaintained": "EOL", "behavior-change": "CHANGED"}.get(f.advisory.get("kind"), "BROKEN")
        return label, f"{n} day{'s' if n != 1 else ''} ago"
    if d == 0:
        return "TODAY", "today"
    label = {"urgent": "URGENT", "soon": "SOON", "later": "LATER", "future": "LATER"}[f.bucket]
    if not f.high:
        label = "CHECK"
    return label, f"{d} day{'s' if d != 1 else ''} left"


def fix_lines(f: Finding, today) -> tuple[list[str], list[str]]:
    """(what to do, notes about the vendor's suggestion) for one finding."""
    fixes = list(f.suggested or f.replacements)
    notes = []
    vendor = f.advisory.get("vendor") or "The vendor"
    swapped = any(r not in fixes for r in f.replacements)
    for tok, other_id, date in f.retiring:
        past = bool(date) and str(date) < today.isoformat()
        if swapped:
            when = f"which itself shut down on {date}" if past else f"which itself retires on {date}"
            notes.append(f"{vendor} suggested {tok}, {when} ({other_id})")
        else:
            notes.append(f"{tok} {'itself shut down on' if past else 'is itself retiring on'} {date} ({other_id})")
    return fixes, notes


def summarize(findings: list[Finding]) -> dict:
    """Bucket counts cover high-confidence findings; low-confidence ones are counted separately."""
    counts = {b: 0 for b in BUCKET_ORDER}
    for f in findings:
        if f.high:
            counts[f.bucket] += 1
    return {
        "findings": len(findings),
        "high_confidence": sum(1 for f in findings if f.high),
        "low_confidence_only": sum(1 for f in findings if not f.high),
        **counts,
    }


# ------------------------------------------------------------------ text

class _Style:
    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return "\033[" + ";".join(codes) + "m" + text + "\033[0m"


def color_enabled(mode: str, stream=None) -> bool:
    stream = stream or sys.stdout
    if mode == "always":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    return hasattr(stream, "isatty") and stream.isatty()


_BUCKET_CODES = {
    "broken": ("1", "31"),
    "expired": ("1", "33"),
    "urgent": ("1", "31"),
    "soon": ("1", "33"),
    "later": ("36",),
    "future": ("34",),
    "blocker": ("1", "35"),
}


def render_text(result: ScanResult, color: bool = False, max_locations: int = 8,
                exit_code: int | None = None, fail_within: int | None = None,
                would_fail: int | None = None, exit_reason: str | None = None,
                fail_on_blockers: bool = False) -> str:
    st = _Style(color)
    out: list[str] = []
    meta = result.registry_meta
    reg = f"registry {meta.get('registry_date')}" if meta.get("registry_date") else "registry"
    out.append(
        st("Sunset Scan", "1") + f" {__version__}  ·  {os.path.basename(os.path.abspath(result.root)) or result.root}"
        f"  ·  {result.files_scanned} files, {result.packages_seen} package entries"
        f"  ·  {result.advisory_count} advisories ({reg})  ·  today {result.today.isoformat()}"
    )
    out.append("")
    if not result.findings:
        out.append(st("  Nothing in this repo matches a known sunset. ", "32") + "Nice.")
    for f in result.findings:
        adv = f.advisory
        label, when = when_label(f)
        codes = _BUCKET_CODES[f.bucket] if f.high else ("2",)
        head = st(f" {label:<7}", *codes) + " " + st(f"{when:<14}", *codes) + " " + st(adv["title"], "1")
        out.append(head)
        bits = [str(adv.get("effective")), adv.get("vendor", ""), adv.get("kind", ""), adv["id"]]
        if adv.get("origin") and adv.get("origin") != "curated":
            bits.append(f"via {adv['origin']}")
        out.append(" " * 24 + st(" · ".join(b for b in bits if b), "2"))
        if f.conflict:
            out.append(" " * 24 + st("conflict: ", "1", "31") + f"{f.conflict} (blocked range {adv['blocks']['package']} {adv['blocks']['versions']})")
        elif f.status == "blocker":
            blk = adv.get("blocks") or {}
            out.append(" " * 24 + st("blocks upgrade to ", "35") + f"{blk.get('package')} {blk.get('versions')}")
        width = min(46, max(len(f"{loc.path}:{loc.line}") for loc in f.locations[:max_locations]) + 2)
        for loc in f.locations[:max_locations]:
            where = f"{loc.path}:{loc.line}"
            tag = f" ({loc.reason})" if loc.reason else ""
            what = loc.detail or loc.match
            line = " " * 24 + f"{where:<{width}}" + what
            out.append(st(line + tag, "2") if loc.confidence == "low" else line)
            for other in loc.also:
                out.append(" " * 26 + st(f"same text also matches {other}; which applies depends on what this "
                                         "code calls", "33"))
        if len(f.locations) > max_locations:
            out.append(" " * 24 + st(f"... and {len(f.locations) - max_locations} more", "2"))
        fixes, fnotes = fix_lines(f, result.today)
        for r in fixes:
            out.append(" " * 24 + st("fix: ", "32") + r)
        for n in fnotes:
            out.append(" " * 24 + st("note: ", "33") + n)
        out.append(" " * 24 + st("src: " + adv.get("source", ""), "2"))
        out.append("")

    s = summarize(result.findings)
    if result.findings:
        parts = [f"{s[b]} {BUCKET_TITLES[b]}" for b in BUCKET_ORDER if s[b]]
        if s["low_confidence_only"]:
            parts.append(st(f"{s['low_confidence_only']} to check (comments, docs or unclear matches)", "2"))
        line = f" {s['findings']} finding{'s' if s['findings'] != 1 else ''}: " + " · ".join(parts)
        out.append(line)
    for note in result.notes:
        out.append(st(f" note: {note}", "33"))
    if exit_code is not None and result.findings:
        out.append(exit_line(st, exit_code, fail_within, would_fail, exit_reason, fail_on_blockers,
                             s["low_confidence_only"]))
        out.append(st(' snooze one: add "id: <advisory-id> until YYYY-MM-DD" to .sunsetignore', "2"))
    return "\n".join(out) + "\n"


def exit_line(st, exit_code: int, fail_within: int | None, would_fail: int | None, exit_reason: str | None,
              fail_on_blockers: bool, low_only: int) -> str:
    what = f"past the date or due within {fail_within} days" + (" or blocking an upgrade" if fail_on_blockers else "")
    n = would_fail if would_fail is not None else (1 if exit_code else 0)
    things = f"{n} finding{'s' if n != 1 else ''} {'are' if n != 1 else 'is'}"
    if exit_code:
        return st(f" exit {exit_code}: ", "1", "31") + f"{things} {what} (--fail-within {fail_within})."
    if n:
        # The scan found failing things but this run still exits 0 (--no-fail, or the demo).
        return (st(" exit 0 ", "1", "33") + f"({exit_reason or 'report only'}): {things} {what}; "
                "a normal run would exit 1.")
    msg = f"nothing high-confidence is {what}."
    if low_only:
        msg += (f" {low_only} low-confidence match{'es' if low_only != 1 else ''} "
                f"{'still need' if low_only != 1 else 'still needs'} a look.")
    return st(" exit 0: ", "1", "32") + msg


# ------------------------------------------------------------------ json

def finding_dict(f: Finding) -> dict:
    adv = f.advisory
    return {
        "id": adv["id"],
        "title": adv["title"],
        "vendor": adv.get("vendor"),
        "kind": adv.get("kind"),
        "effective": adv.get("effective"),
        "days_left": f.days_left,
        "status": f.status,
        "bucket": f.bucket,
        "confidence": "high" if f.high else "low",
        "replacements": f.replacements,
        "suggested_fix": f.suggested or f.replacements,
        "retiring_replacements": [{"id": t, "advisory": a, "effective": d} for t, a, d in f.retiring],
        "conflict": f.conflict,
        "blocks": adv.get("blocks"),
        "source": adv.get("source"),
        "origin": adv.get("origin", "curated"),
        "locations": [
            {
                "path": loc.path, "line": loc.line, "column": loc.col, "match": loc.match,
                "snippet": loc.snippet, "confidence": loc.confidence, "reason": loc.reason, "detail": loc.detail,
                "also_matches": loc.also,
            }
            for loc in f.locations
        ],
    }


def render_json(result: ScanResult, exit_code: int | None = None) -> str:
    doc = {
        "tool": "sunset-scan",
        "version": __version__,
        "today": result.today.isoformat(),
        "root": result.root,
        "registry": result.registry_meta,
        "files_scanned": result.files_scanned,
        "summary": summarize(result.findings),
        "exit_code": exit_code,
        "notes": result.notes,
        "findings": [finding_dict(f) for f in result.findings],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# ------------------------------------------------------------------ markdown

def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _md_row(f: Finding, today, max_locations: int = 3) -> str:
    adv = f.advisory
    label, when = when_label(f)
    status = f"**{label}** {when}"
    what = f"**{_md(adv['title'])}**<br>`{adv['id']}`"
    if f.conflict:
        what += f"<br>conflict: {_md(f.conflict)}"
    elif f.status == "blocker":
        blk = adv.get("blocks") or {}
        what += f"<br>blocks upgrade to {_md(blk.get('package'))} {_md(blk.get('versions'))}"
    locs = []
    for loc in f.locations[:max_locations]:
        tag = f" ({loc.reason})" if loc.reason else ""
        locs.append(f"`{loc.path}:{loc.line}` {_md(loc.detail or loc.match)}{tag}")
        for other in loc.also:
            locs.append(f"also matches {_md(other)}; check which applies")
    if len(f.locations) > max_locations:
        locs.append(f"and {len(f.locations) - max_locations} more")
    fixes, notes = fix_lines(f, today)
    fix = "<br>".join(_md(r) for r in fixes) or "-"
    for n in notes:
        fix += f"<br>note: {_md(n)}"
    return f"| {status} | {adv.get('effective')} | {what} | {'<br>'.join(locs)} | {fix} | [source]({adv.get('source')}) |"


# GitHub rejects issue bodies over 65,536 characters; stay well under it.
MARKDOWN_MAX_CHARS = 60_000


def render_markdown(result: ScanResult, max_chars: int = MARKDOWN_MAX_CHARS) -> str:
    high = [f for f in result.findings if f.high]
    low = [f for f in result.findings if not f.high]
    s = summarize(result.findings)
    out = ["<!-- sunset-scan:report -->"]
    if high:
        n_soon = sum(1 for f in high if f.bucket in ("broken", "expired", "urgent", "soon"))
        out.append(f"## Sunset Scan: {len(high)} thing{'s' if len(high) != 1 else ''} in this repo stop working"
                   + (f" ({n_soon} broken or within 90 days)" if n_soon else ""))
    else:
        out.append("## Sunset Scan: nothing in this repo matches a known sunset")
    meta = result.registry_meta
    out.append("")
    out.append(
        f"Scanned on {result.today.isoformat()} against {result.advisory_count} advisories"
        + (f" (registry {meta.get('registry_date')})" if meta.get("registry_date") else "")
        + f" with sunset-scan {__version__}. "
        + " · ".join([f"{s[b]} {BUCKET_TITLES[b]}" for b in BUCKET_ORDER if s[b]]
                     + ([f"{s['low_confidence_only']} to check"] if s["low_confidence_only"] else []))
    )
    header = "| When | Date | What | Where | Do this | Source |\n|---|---|---|---|---|---|"
    tail = [f"> note: {_md(note)}" for note in result.notes]
    tail += [
        "Snooze a finding by adding `id: <advisory-id> until YYYY-MM-DD` to `.sunsetignore`, "
        "or add `sunset-scan: ignore` to the line. "
        f"Wrong or missing data? [Open an issue or PR on the registry]({REPO_URL}).",
    ]
    budget = max_chars - sum(len(x) + 2 for x in out + tail) - 400  # room for headers and the cut-off line
    shown = 0
    omitted = 0

    def rows(fs: list[Finding]) -> list[str]:
        nonlocal budget, shown, omitted
        kept = []
        for f in fs:
            row = _md_row(f, result.today)
            if len(row) + 1 > budget:
                omitted += 1
                continue
            budget -= len(row) + 1
            kept.append(row)
            shown += 1
        return kept

    if high:
        out += ["", header] + rows(high)
    if low:
        low_rows = rows(low)
        if low_rows:
            out += ["", f"<details><summary>{len(low)} low-confidence match{'es' if len(low) != 1 else ''} "
                        "(only in comments or docs, tokenizer calls, short ids in files with no AI context, or "
                        "unknown package versions)</summary>", "", header] + low_rows + ["", "</details>"]
    if omitted:
        out += ["", f"**{omitted} more finding{'s' if omitted != 1 else ''} not shown** to keep this under "
                    "GitHub's size limit. Run `sunset-scan .` locally or use `--format json` for the full list."]
    for x in tail:
        out += ["", x]
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ sarif

def _sarif_level(f: Finding) -> str:
    if not f.high:
        return "note"
    return {"broken": "error", "expired": "warning", "urgent": "error", "soon": "warning",
            "later": "warning"}.get(f.bucket, "note")


def render_sarif(result: ScanResult) -> str:
    rules, index = [], {}
    results = []
    for f in result.findings:
        adv = f.advisory
        if adv["id"] not in index:
            index[adv["id"]] = len(rules)
            fix = "; ".join(f.suggested or f.replacements) or "See the source link."
            rules.append({
                "id": adv["id"],
                "name": adv["id"].replace("/", "-"),
                "shortDescription": {"text": adv["title"]},
                "fullDescription": {"text": (adv.get("summary") or adv["title"]).strip()},
                "helpUri": adv.get("source"),
                "help": {
                    "text": f"Effective {adv.get('effective')}. Fix: {fix} Source: {adv.get('source')}",
                    "markdown": f"Effective **{adv.get('effective')}**. Fix: {fix}\n\n[Source]({adv.get('source')})",
                },
                "defaultConfiguration": {"level": _sarif_level(f)},
                "properties": {"tags": ["sunset", adv.get("kind", ""), adv.get("vendor", "")]},
            })
        label, when = when_label(f)
        msg = f"{adv['title']} ({adv.get('effective')}, {when})."
        if f.suggested or f.replacements:
            msg += " Fix: " + "; ".join(f.suggested or f.replacements)
        for loc in f.locations:
            fp = hashlib.sha256(f"{adv['id']}|{loc.path}|{loc.snippet}".encode()).hexdigest()[:32]
            results.append({
                "ruleId": adv["id"],
                "ruleIndex": index[adv["id"]],
                "level": "note" if loc.confidence == "low" else _sarif_level(f),
                "message": {"text": msg},
                "locations": [{
                    "physicalLocation": {
                        "artifactLocation": {"uri": loc.path, "uriBaseId": "%SRCROOT%"},
                        "region": {"startLine": loc.line, "startColumn": loc.col},
                    }
                }],
                "partialFingerprints": {"sunsetScan/v1": fp},
            })
    doc = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {
                "name": "sunset-scan", "version": __version__, "informationUri": REPO_URL, "rules": rules,
            }},
            "results": results,
        }],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


RENDERERS = {"json": render_json, "markdown": render_markdown, "sarif": render_sarif}
