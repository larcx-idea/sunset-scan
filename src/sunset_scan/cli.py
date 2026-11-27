"""Command-line interface: ``sunset-scan [scan] PATH``, ``list``, ``show``, ``demo``, ``registry``."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from importlib import resources
from pathlib import Path

from . import __version__
from .registry import (
    RegistryError, check_links, compile_bundle, dump_bundle, load_registry, load_yaml_dir, parse_date, validate,
)
from .report import RENDERERS, color_enabled, render_text
from .scanner import IgnoreConfig, is_failing, scan

COMMANDS = {"scan", "list", "show", "demo", "registry"}
EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2


def _date(value: str) -> dt.date:
    d = parse_date(value)
    if d is None:
        raise argparse.ArgumentTypeError("expected a date like 2026-12-01")
    return d


def _report_spec(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("expected FORMAT=PATH, e.g. sarif=sunset.sarif")
    fmt, path = value.split("=", 1)
    if fmt not in RENDERERS and fmt != "text":
        raise argparse.ArgumentTypeError(f"unknown format {fmt!r}")
    return fmt, path


def _add_scan_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--format", choices=["text", "json", "markdown", "sarif"], default="text",
                   help="output format on stdout (default: text)")
    p.add_argument("--output", "-o", metavar="FILE", help="write the --format output to FILE instead of stdout")
    p.add_argument("--report", action="append", type=_report_spec, default=[], metavar="FORMAT=PATH",
                   help="also write a report, e.g. --report sarif=sunset.sarif --report markdown=sunset.md")
    p.add_argument("--fail-within", type=int, default=30, metavar="DAYS",
                   help="exit 1 if anything is already past its date or breaks within DAYS (default: 30)")
    p.add_argument("--no-fail", action="store_true",
                   help="always exit 0 when the scan completes (the report still says what would fail)")
    p.add_argument("--fail-on-blockers", action="store_true", help="also exit 1 for upgrade blockers")
    p.add_argument("--strict", action="store_true", help="let low-confidence matches (comments, docs, tokenizer calls, unknown versions) fail the run too")
    p.add_argument("--within", type=int, metavar="DAYS", help="only show findings due within DAYS (and broken ones)")
    p.add_argument("--today", type=_date, metavar="YYYY-MM-DD", help="pretend today is this date")
    p.add_argument("--registry", metavar="PATH", help="use a registry directory/YAML/JSON instead of the bundled one")
    p.add_argument("--ignore-file", metavar="FILE",
                   help="ignore file (default: PATH/.sunsetignore, else the nearest one above PATH in the git repo)")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB", help="skip paths matching GLOB")
    p.add_argument("--ignore-id", action="append", default=[], metavar="ID", help="skip an advisory id")
    p.add_argument("--no-docs", action="store_true", help="skip .md/.rst/.txt files entirely")
    p.add_argument("--no-default-excludes", action="store_true", help="also scan node_modules, vendor, dist, ...")
    p.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    p.add_argument("--max-locations", type=int, default=8, metavar="N", help="locations shown per finding (text)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sunset-scan",
        description="Find things in your repo that stop working on a date: retiring AI models, expiring API "
                    "versions, archived infrastructure, paywalled images. Prints days left, file:line and the fix.",
    )
    parser.add_argument("--version", action="version", version=f"sunset-scan {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_scan = sub.add_parser("scan", help="scan a directory (default command)")
    p_scan.add_argument("path", nargs="?", default=".")
    _add_scan_options(p_scan)

    p_demo = sub.add_parser("demo", help="scan the bundled sample app to see what a report looks like")
    _add_scan_options(p_demo)
    p_demo.add_argument("--exit-code", action="store_true", help="return the real exit code instead of 0")

    p_list = sub.add_parser("list", help="list advisories in the registry (a sunset calendar)")
    p_list.add_argument("--all", action="store_true", help="include advisories whose date has passed")
    p_list.add_argument("--within", type=int, metavar="DAYS", help="only advisories due within DAYS")
    p_list.add_argument("--vendor", help="filter by vendor (case-insensitive substring)")
    p_list.add_argument("--search", help="filter by id/title/identifier substring")
    p_list.add_argument("--today", type=_date, metavar="YYYY-MM-DD")
    p_list.add_argument("--registry", metavar="PATH")
    p_list.add_argument("--format", choices=["text", "json"], default="text")

    p_show = sub.add_parser("show", help="show one advisory in full")
    p_show.add_argument("id")
    p_show.add_argument("--registry", metavar="PATH")

    p_reg = sub.add_parser("registry", help="registry maintenance: validate / build")
    reg_sub = p_reg.add_subparsers(dest="registry_command")
    p_val = reg_sub.add_parser("validate", help="validate registry YAML (schema, dates, regexes, links)")
    p_val.add_argument("path", nargs="?", default="registry")
    p_val.add_argument("--check-links", action="store_true", help="also fetch every source URL")
    p_val.add_argument("--today", type=_date, metavar="YYYY-MM-DD")
    p_val.add_argument("--warnings-as-errors", action="store_true")
    p_val.add_argument("--verbose", "-v", action="store_true", help="also list informational notes")
    p_build = reg_sub.add_parser("build", help="compile registry YAML into the bundled JSON")
    p_build.add_argument("path", nargs="?", default="registry")
    p_build.add_argument("--out", default="src/sunset_scan/data/registry.json")
    p_build.add_argument("--check", action="store_true", help="exit 1 if the bundle is out of date")
    p_sync = reg_sub.add_parser("sync", help="import model retirements from endoflife.date and LiteLLM")
    p_sync.add_argument("path", nargs="?", default="registry")
    p_sync.add_argument("--from-dir", metavar="DIR", help="read previously downloaded upstream JSON from DIR")
    return parser


# ------------------------------------------------------------------ commands

def _run_scan(args, root: Path, demo: bool = False) -> int:
    today = args.today or dt.date.today()
    try:
        advisories, meta = load_registry(args.registry)
    except RegistryError as exc:
        print(f"sunset-scan: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if not root.exists():
        print(f"sunset-scan: path not found: {root}", file=sys.stderr)
        return EXIT_ERROR
    ignore = IgnoreConfig.load(root if root.is_dir() else root.parent, today, args.ignore_file,
                               extra_globs=args.exclude, extra_ids=args.ignore_id, search_parents=not demo)
    result = scan(root, advisories, meta, today=today, ignore=ignore, include_docs=not args.no_docs,
                  default_excludes=not args.no_default_excludes)
    if args.within is not None:
        result.findings = [
            f for f in result.findings
            if f.status in ("past", "conflict") or (f.days_left is not None and f.days_left <= args.within)
        ]
    # What would fail a normal run, even when --no-fail (or the demo) makes this one exit 0,
    # so the report never claims "nothing is broken" when something is.
    failing = [f for f in result.findings if is_failing(f, args.fail_within, args.strict, args.fail_on_blockers)]
    code = EXIT_FINDINGS if failing and not args.no_fail else EXIT_OK
    exit_reason = "--no-fail" if args.no_fail else None
    if demo and not getattr(args, "exit_code", False):
        exit_reason = exit_reason or "the demo always exits 0; add --exit-code for the real code"
        code_shown = EXIT_OK
    else:
        code_shown = code

    def render(fmt: str, stream_is_tty: bool) -> str:
        if fmt == "text":
            use_color = stream_is_tty and color_enabled(args.color)
            return render_text(result, color=use_color, max_locations=args.max_locations,
                               exit_code=code_shown, fail_within=args.fail_within, would_fail=len(failing),
                               exit_reason=exit_reason, fail_on_blockers=args.fail_on_blockers)
        if fmt == "json":
            return RENDERERS["json"](result, exit_code=code_shown)
        return RENDERERS[fmt](result)

    for fmt, path in args.report:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(render(fmt, False), encoding="utf-8")
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(render(args.format, False), encoding="utf-8")
    else:
        sys.stdout.write(render(args.format, args.color == "always" or sys.stdout.isatty()))
    return code


def cmd_demo(args) -> int:
    ref = resources.files("sunset_scan").joinpath("demo_app")
    with resources.as_file(ref) as demo_root:
        if args.format == "text" and not args.output:
            print("Demo: scanning the bundled sample app (a small agency repo with an OpenAI feature, Meta Graph API "
                  "reports and a Helm chart).\nIn your own repo, run: sunset-scan .\n")
        code = _run_scan(args, Path(demo_root), demo=True)
    if code == EXIT_ERROR or args.exit_code:
        return code
    return EXIT_OK


def _advisory_matches(adv: dict, needle: str) -> bool:
    hay = [adv.get("id", ""), adv.get("title", "")]
    for aff in adv.get("affected") or []:
        hay += aff.get("ids") or []
        if aff.get("pattern"):
            hay.append(aff["pattern"])
    return any(needle in h.lower() for h in hay)


def cmd_list(args) -> int:
    today = args.today or dt.date.today()
    try:
        advisories, meta = load_registry(args.registry)
    except RegistryError as exc:
        print(f"sunset-scan: {exc}", file=sys.stderr)
        return EXIT_ERROR
    rows = []
    for adv in advisories:
        eff = parse_date(adv.get("effective"))
        days = (eff - today).days if eff else None
        if not args.all and days is not None and days < 0:
            continue
        if args.within is not None and (days is None or days > args.within):
            continue
        if args.vendor and args.vendor.lower() not in adv.get("vendor", "").lower():
            continue
        if args.search and not _advisory_matches(adv, args.search.lower()):
            continue
        rows.append((days, adv))
    rows.sort(key=lambda r: (r[0] if r[0] is not None else 10**6, r[1]["id"]))
    if args.format == "json":
        print(json.dumps([dict(adv, days_left=d) for d, adv in rows], indent=2))
        return EXIT_OK
    print(f"{len(rows)} advisories (registry {meta.get('registry_date')}, today {today.isoformat()})\n")
    for days, adv in rows:
        when = "today" if days == 0 else (f"{days}d" if days > 0 else f"{-days}d ago")
        print(f"  {adv.get('effective')}  {when:>9}  {adv['id']:<44} {adv['title']}")
    return EXIT_OK


def cmd_show(args) -> int:
    try:
        advisories, _ = load_registry(args.registry)
    except RegistryError as exc:
        print(f"sunset-scan: {exc}", file=sys.stderr)
        return EXIT_ERROR
    for adv in advisories:
        if adv["id"] == args.id:
            print(f"{adv['id']}: {adv['title']}")
            for key in ("vendor", "kind", "effective", "announced", "replacement", "source", "origin", "last_verified"):
                if adv.get(key):
                    print(f"  {key}: {adv[key]}")
            if adv.get("blocks"):
                b = adv["blocks"]
                print(f"  blocks: {b['ecosystem']} {b['package']} {b['versions']}")
            if adv.get("summary"):
                print(f"  summary: {adv['summary'].strip()}")
            for aff in adv.get("affected") or []:
                what = ", ".join(aff.get("ids") or []) or aff.get("pattern", "")
                extra = []
                if aff.get("versions"):
                    extra.append(f"versions {aff['versions']}")
                if aff.get("context"):
                    extra.append("context: " + ", ".join(aff["context"]))
                if aff.get("files"):
                    extra.append("files: " + ", ".join(aff["files"]))
                if aff.get("replacement"):
                    extra.append(f"replacement: {aff['replacement']}")
                print(f"  - {aff['type']}: {what}" + (f"  ({'; '.join(extra)})" if extra else ""))
            for ref in adv.get("references") or []:
                print(f"  see also: {ref}")
            return EXIT_OK
    print(f"sunset-scan: no advisory with id {args.id!r}", file=sys.stderr)
    return EXIT_ERROR


def cmd_registry(args) -> int:
    if args.registry_command == "validate":
        advisories, issues = load_yaml_dir(Path(args.path))
        issues += validate(advisories, today=args.today)
        if args.check_links:
            issues += check_links(advisories)
        errors = [i for i in issues if i.level == "error"]
        warnings = [i for i in issues if i.level == "warning"]
        infos = [i for i in issues if i.level == "info"]
        for i in errors + warnings + (infos if args.verbose else []):
            print(i)
        kinds: dict[str, int] = {}
        for a in advisories:
            kinds[a.get("origin", "curated")] = kinds.get(a.get("origin", "curated"), 0) + 1
        origin = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items()))
        print(f"{len(advisories)} advisories ({origin}); {len(errors)} errors, {len(warnings)} warnings, "
              f"{len(infos)} notes (replacements that are themselves retiring; --verbose to list)")
        if errors or (args.warnings_as_errors and warnings):
            return EXIT_FINDINGS
        return EXIT_OK
    if args.registry_command == "build":
        advisories, issues = load_yaml_dir(Path(args.path))
        errors = [i for i in issues + validate(advisories) if i.level == "error"]
        if errors:
            for e in errors:
                print(e, file=sys.stderr)
            return EXIT_FINDINGS
        text = dump_bundle(compile_bundle(advisories))
        out = Path(args.out)
        if args.check:
            current = out.read_text(encoding="utf-8") if out.exists() else ""
            if current != text:
                print(f"{out} is out of date; run: sunset-scan registry build", file=sys.stderr)
                return EXIT_FINDINGS
            print(f"{out} is up to date ({len(advisories)} advisories)")
            return EXIT_OK
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"wrote {out} ({len(advisories)} advisories)")
        return EXIT_OK
    if args.registry_command == "sync":
        from .upstream import sync

        try:
            counts, drift_msgs = sync(Path(args.path), Path(args.from_dir) if args.from_dir else None)
        except OSError as exc:
            print(f"sunset-scan: sync failed: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print("imported: " + ", ".join(f"{v} from {k}" for k, v in counts.items()))
        for msg in drift_msgs:
            print(f"drift: {msg}")
        print(f"{len(drift_msgs)} curated dates disagree with endoflife.date (review them; curated data wins)")
        return EXIT_OK
    print("usage: sunset-scan registry {validate,build,sync} [path]", file=sys.stderr)
    return EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version")):
        argv = ["scan"] + argv
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return _run_scan(args, Path(args.path))
        if args.command == "demo":
            return cmd_demo(args)
        if args.command == "list":
            return cmd_list(args)
        if args.command == "show":
            return cmd_show(args)
        if args.command == "registry":
            return cmd_registry(args)
    except BrokenPipeError:  # e.g. piped into `head`
        return EXIT_OK
    parser.print_help()
    return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
