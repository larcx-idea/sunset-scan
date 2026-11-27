import datetime as dt
import json
from pathlib import Path

from sunset_scan.registry import load_bundle
from sunset_scan.scanner import IgnoreConfig, glob_match, is_failing, scan

TODAY = dt.date(2026, 11, 27)


def A(id, effective, affected, kind="shutdown", **extra):
    adv = {"id": id, "title": id, "vendor": "Test", "kind": kind, "effective": effective,
           "source": "https://example.com", "affected": affected}
    adv.update(extra)
    return adv


REG = [
    A("t/gpt-4o", "2027-01-14", [{"type": "model", "ids": ["gpt-4o", "gpt-4o-2024-08-06"]}], replacement="gpt-6-luna"),
    A("t/gpt-4o-mini", "2027-01-14", [{"type": "model", "ids": ["gpt-4o-mini"]}]),
    A("t/gpt-4", "2026-10-23", [{"type": "model", "ids": ["gpt-4"]}]),
    A("t/o1", "2026-10-23", [{"type": "model", "ids": ["o1"], "quoted": True}]),
    A("t/pg", "2025-09-29", [{"type": "image", "ids": ["bitnami/postgresql"]}]),
    A("t/bitnami-any", "2025-09-29", [{"type": "pattern", "pattern": r"(?<![A-Za-z0-9_.\-/])bitnami/[a-z0-9-]+"}],
      fallback=True),
    A("t/graph-v21", "2027-01-21", [{"type": "pattern", "pattern": r"""["']v21\.0["']""", "context": ["facebook"]}]),
    A("t/runner", "2025-04-15", [{"type": "literal", "ids": ["ubuntu-20.04"], "files": [".github/workflows/*"]}]),
    A("t/assistants", "2026-08-26", [{"type": "literal", "ids": ["beta.assistants"]}]),
    A("t/fb-sdk-24", "2026-10-06", [{"type": "pypi", "ids": ["facebook-business"], "versions": ">=24.0.0, <25.0.0"},
                                    {"type": "npm", "ids": ["facebook-nodejs-business-sdk"],
                                     "versions": ">=24.0.0, <25.0.0"}]),
    A("t/plugin-react", "2026-02-06", [{"type": "npm", "ids": ["eslint-plugin-react"], "versions": "<=7.37.5"}],
      kind="upgrade-blocker", blocks={"ecosystem": "npm", "package": "eslint", "versions": ">=10.0.0"}),
]


def write(root: Path, files: dict):
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def run(root, registry=REG, **kw):
    return scan(root, registry, {}, today=TODAY, **kw)


def ids(result):
    return {f.id for f in result.findings}


def test_model_boundaries(tmp_path):
    write(tmp_path, {
        "a.py": 'm1 = "gpt-4o"\nm2 = "gpt-4o-mini"\nm3 = "gpt-4.1"\nm4 = "gpt-4-turbo"\n'
                'm5 = "ft:gpt-4o-2024-08-06:acme::abc123"\nm6 = "openai/gpt-4"\n',
    })
    r = run(tmp_path)
    by = {f.id: f for f in r.findings}
    assert [loc.line for loc in by["t/gpt-4o"].locations] == [1, 5]
    assert [loc.line for loc in by["t/gpt-4o-mini"].locations] == [2]
    assert [loc.line for loc in by["t/gpt-4"].locations] == [6]  # not gpt-4.1, not gpt-4-turbo


def test_quoted_short_model_ids(tmp_path):
    write(tmp_path, {"a.py": 'o1 = compute()\nmodel = "o1"\n'})
    r = run(tmp_path)
    (f,) = [f for f in r.findings if f.id == "t/o1"]
    assert [loc.line for loc in f.locations] == [2]


def test_images_and_fallback(tmp_path):
    write(tmp_path, {
        "values.yaml": "image:\n  repository: bitnami/postgresql\nother: bitnami/kafka:3\n"
                       "legacy: bitnamilegacy/postgresql\nha: bitnami/postgresql-ha\n",
    })
    r = run(tmp_path)
    pg = next(f for f in r.findings if f.id == "t/pg")
    assert [loc.line for loc in pg.locations] == [2]
    anyb = next(f for f in r.findings if f.id == "t/bitnami-any")
    # line 2 is claimed by the specific advisory; legacy is not bitnami/
    assert [loc.line for loc in anyb.locations] == [3, 5]


def test_context_and_file_globs(tmp_path):
    write(tmp_path, {
        "fb.py": 'import facebook\nVERSION = "v21.0"\n',
        "other.py": 'VERSION = "v21.0"\n',
        ".github/workflows/ci.yml": "runs-on: ubuntu-20.04\n",
        "Dockerfile": "FROM ubuntu-20.04-base\n",
    })
    r = run(tmp_path)
    by = {f.id: f for f in r.findings}
    assert [loc.path for loc in by["t/graph-v21"].locations] == ["fb.py"]
    assert [loc.path for loc in by["t/runner"].locations] == [".github/workflows/ci.yml"]


def test_comments_docs_and_inline_ignores(tmp_path):
    write(tmp_path, {
        "a.ts": '// we used gpt-4o before\nconst m = "gpt-4o"; // sunset-scan: ignore\n'
                '// sunset-scan: ignore-next-line\nconst n = "gpt-4o";\nconst k = "x"; # gpt-4o\n',
        "README.md": "Uses gpt-4o.\n",
    })
    r = run(tmp_path)
    f = next(f for f in r.findings if f.id == "t/gpt-4o")
    assert {(loc.path, loc.line, loc.reason) for loc in f.locations} == {
        ("a.ts", 1, "comment"), ("a.ts", 5, "comment"), ("README.md", 1, "docs")}
    assert not f.high
    assert not is_failing(f, 365, strict=False, fail_on_blockers=False)
    assert is_failing(f, 365, strict=True, fail_on_blockers=False)
    r2 = run(tmp_path, include_docs=False)
    assert all(loc.path != "README.md" for f in r2.findings for loc in f.locations)


def test_sunsetignore_paths_and_snoozes(tmp_path):
    write(tmp_path, {
        "src/a.py": 'm = "gpt-4o"\nn = "gpt-4o-mini"\n',
        "fixtures/b.py": 'm = "gpt-4"\n',
        ".sunsetignore": "fixtures/\nid: t/gpt-4o until 2027-06-01\nid: t/gpt-4o-mini until 2026-01-01\n"
                         "# snoozing bitnami/postgresql must not itself be a finding\nid: t/pg until 2027-01-01\n",
    })
    cfg = IgnoreConfig.load(tmp_path, TODAY)
    r = run(tmp_path, ignore=cfg)
    assert ids(r) == {"t/gpt-4o-mini"}  # snooze expired -> reported again
    assert any("expired" in n for n in r.notes)


def test_glob_match():
    assert glob_match("node_modules", "a/node_modules/x.js")
    assert glob_match("tests/fixtures/", "tests/fixtures/deep/x.py")
    assert glob_match("/src/*.py", "src/a.py")
    assert not glob_match("/src/*.py", "lib/src/a.py")
    assert glob_match("**/legacy/**", "a/legacy/b.py")
    assert glob_match("*.md", "docs/README.md")


def test_default_excludes(tmp_path):
    write(tmp_path, {"node_modules/x/index.js": 'm = "gpt-4o"\n', "dist/app.js": 'm = "gpt-4o"\n'})
    assert run(tmp_path).findings == []
    assert ids(run(tmp_path, default_excludes=False)) == {"t/gpt-4o"}


def test_packages_from_manifests_and_lockfiles(tmp_path):
    write(tmp_path, {
        "requirements.txt": "requests==2.32.3\nfacebook-business==24.0.1  # ads\n",
        "svc/poetry.lock": '[[package]]\nname = "facebook-business"\nversion = "23.0.0"\n',
        "web/package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {
            "": {}, "node_modules/facebook-nodejs-business-sdk": {"version": "24.0.2"}}}),
        "app/yarn.lock": '"facebook-nodejs-business-sdk@^24.0.0":\n  version "24.0.0"\n',
        "pnpm/pnpm-lock.yaml": "packages:\n  /facebook-nodejs-business-sdk@24.1.0:\n    resolution: {}\n",
        "pipenv/Pipfile.lock": json.dumps({"default": {"facebook-business": {"version": "==24.0.0"}}}),
    })
    r = run(tmp_path)
    f = next(f for f in r.findings if f.id == "t/fb-sdk-24")
    assert sorted(loc.path for loc in f.locations) == [
        "app/yarn.lock", "pipenv/Pipfile.lock", "pnpm/pnpm-lock.yaml", "requirements.txt", "web/package-lock.json"]
    assert f.status == "past" and f.days_left == -52


def test_upgrade_blocker_vs_conflict(tmp_path):
    write(tmp_path, {"package.json": json.dumps({"devDependencies": {
        "eslint": "^9.39.0", "eslint-plugin-react": "^7.37.5"}})})
    f = next(f for f in run(tmp_path).findings if f.id == "t/plugin-react")
    assert f.status == "blocker" and f.days_left is None and f.bucket == "blocker"
    assert not is_failing(f, 30, strict=False, fail_on_blockers=False)
    assert is_failing(f, 30, strict=False, fail_on_blockers=True)

    write(tmp_path, {"package.json": json.dumps({"devDependencies": {
        "eslint": "^10.1.0", "eslint-plugin-react": "^7.37.5"}})})
    f = next(f for f in run(tmp_path).findings if f.id == "t/plugin-react")
    assert f.status == "conflict" and "eslint ^10.1.0" in f.conflict and f.bucket == "broken"

    write(tmp_path, {"package.json": json.dumps({"devDependencies": {"eslint-plugin-react": "^7.38.0"}})})
    assert "t/plugin-react" not in ids(run(tmp_path))  # fixed version is outside the affected range


def test_days_left_buckets_and_sorting(tmp_path):
    write(tmp_path, {"a.py": 'a = "gpt-4o"\nb = "gpt-4"\nclient.beta.assistants.create()\n'})
    r = run(tmp_path)
    assert [f.id for f in r.findings] == ["t/assistants", "t/gpt-4", "t/gpt-4o"]
    f = r.findings[-1]
    assert f.days_left == 48 and f.bucket == "soon" and f.status == "upcoming"
    assert f.replacements == ["gpt-6-luna"]
    assert not is_failing(f, 30, False, False) and is_failing(f, 60, False, False)


def test_real_registry_retiring_replacement_note(tmp_path):
    write(tmp_path, {"a.py": 'model = "gpt-4-32k"\n'})
    bundle = load_bundle()
    r = scan(tmp_path, bundle["advisories"], bundle, today=TODAY)
    f = next(f for f in r.findings if f.id == "openai/gpt-4-32k")
    assert f.replacements == ["gpt-4o"]
    assert ("gpt-4o", "openai/gpt-4o", "2027-01-14") in f.retiring


def test_binary_and_lockfile_text_skipped(tmp_path):
    (tmp_path / "img.png").write_bytes(b"\x89PNG gpt-4o")
    (tmp_path / "blob.dat").write_bytes(b"gpt-4o\x00\x00")
    write(tmp_path, {"yarn.lock": '"gpt-4o@1":\n  version "1.0.0"\n'})
    assert run(tmp_path).findings == []


# ---------------------------------------------------------------- review regressions

def test_invalid_snooze_date_never_becomes_permanent(tmp_path):
    """`until 2027-02-30` (no such day) used to silence the advisory forever."""
    write(tmp_path, {
        "a.py": 'm = "gpt-4o"\nn = "gpt-4o-mini"\nk = "gpt-4"\n',
        ".sunsetignore": "id: t/gpt-4o until 2027-02-30\nid: t/gpt-4o-mini until next-week\n"
                         "id: t/gpt4 until 2027-06-01\n",
    })
    cfg = IgnoreConfig.load(tmp_path, TODAY)
    r = run(tmp_path, ignore=cfg)
    assert {"t/gpt-4o", "t/gpt-4o-mini", "t/gpt-4"} <= ids(r)
    assert any("2027-02-30" in n and "NOT snoozed" in n for n in r.notes)
    assert any("next-week" in n and "NOT snoozed" in n for n in r.notes)
    assert any("'t/gpt4'" in n and "does not match any advisory" in n for n in r.notes)


def test_sunsetignore_inline_comment_on_path(tmp_path):
    write(tmp_path, {"fixtures/b.py": 'm = "gpt-4"\n', ".sunsetignore": "fixtures/   # test data\n"})
    assert run(tmp_path, ignore=IgnoreConfig.load(tmp_path, TODAY)).findings == []


def test_parent_sunsetignore_applies_to_subdirectory_scan(tmp_path):
    """The Action's `path: services/api` used to ignore the repo-root .sunsetignore."""
    (tmp_path / ".git").mkdir()
    write(tmp_path, {
        ".sunsetignore": "services/api/legacy/\nid: t/gpt-4\n",
        "services/api/legacy/old.py": 'm = "gpt-4o"\n',
        "services/api/app.py": 'm = "gpt-4"\nn = "gpt-4o-mini"\n',
    })
    sub = tmp_path / "services" / "api"
    r = run(sub, ignore=IgnoreConfig.load(sub, TODAY))
    assert ids(r) == {"t/gpt-4o-mini"}
    # Outside a git checkout, files above the scanned directory are never used.
    (tmp_path / ".git").rmdir()
    assert IgnoreConfig.load(sub, TODAY).source is None


def test_short_quoted_ids_need_ai_context(tmp_path):
    """`species = ["o1","o2","o3"]` used to be reported as BROKEN and fail CI."""
    write(tmp_path, {"a.py": 'species = ["o1","o2","o3"]\n'})
    r = run(tmp_path)
    (f,) = [f for f in r.findings if f.id == "t/o1"]
    assert not f.high and f.locations[0].reason == "no AI context"
    assert not is_failing(f, 365, strict=False, fail_on_blockers=False)
    write(tmp_path, {"a.py": 'client.chat.completions.create(model="o1")\n'})
    (f,) = [f for f in run(tmp_path).findings if f.id == "t/o1"]
    assert f.high


def test_tokenizer_calls_are_low_confidence(tmp_path):
    """tiktoken.encoding_for_model("gpt-4") keeps working after the API shutdown."""
    write(tmp_path, {
        "tok.py": 'import tiktoken\nenc = tiktoken.encoding_for_model("gpt-4")\n',
        "tok.ts": 'const enc = encodingForModel( "gpt-4o" );\n',
    })
    r = run(tmp_path)
    for f in r.findings:
        assert not f.high and {loc.reason for loc in f.locations} == {"tokenizer"}
        assert not is_failing(f, 365, strict=False, fail_on_blockers=False)
    assert ids(r) == {"t/gpt-4", "t/gpt-4o"}


def test_unknown_package_versions_are_low_confidence(tmp_path):
    """`"@actions/cache": "file:../cache"` used to be BROKEN against `<4.0.0`."""
    for spec in ("file:../sdk", "workspace:*", "git+https://github.com/acme/sdk.git", "*", "latest"):
        write(tmp_path, {"package.json": json.dumps({"dependencies": {"facebook-nodejs-business-sdk": spec}})})
        (f,) = [f for f in run(tmp_path).findings if f.id == "t/fb-sdk-24"]
        assert not f.high, spec
        assert f.locations[0].reason == "version unknown"
        assert not is_failing(f, 365, strict=False, fail_on_blockers=False)
    write(tmp_path, {"package.json": json.dumps({"dependencies": {"facebook-nodejs-business-sdk": "^24.0.0"}})})
    (f,) = [f for f in run(tmp_path).findings if f.id == "t/fb-sdk-24"]
    assert f.high


def test_lockfile_version_beats_declared_range(tmp_path):
    write(tmp_path, {
        "package.json": json.dumps({"devDependencies": {"eslint-plugin-react": "^7.37.0"}}),
        "package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {
            "": {}, "node_modules/eslint-plugin-react": {"version": "7.38.0"}}}),
    })
    assert "t/plugin-react" not in ids(run(tmp_path))


def test_manifest_line_numbers(tmp_path):
    """Poetry entries used to be reported at the [project] line; similar names were confused."""
    write(tmp_path, {
        "pyproject.toml": '[project]\nname = "x"\ndependencies = [\n  "facebook-business-extras>=1",\n'
                          '  "facebook-business==24.0.1",\n]\n\n[tool.poetry.dependencies]\npython = "^3.11"\n'
                          'facebook_business = "24.0.2"\n',
        "web/package.json": '{\n  "devDependencies": {\n    "eslint-plugin-react-hooks": "^5.0.0",\n'
                            '    "eslint-plugin-react": "^7.37.5"\n  }\n}\n',
    })
    r = run(tmp_path)
    fb = next(f for f in r.findings if f.id == "t/fb-sdk-24")
    assert sorted(loc.line for loc in fb.locations) == [5, 10]
    pr = next(f for f in r.findings if f.id == "t/plugin-react")
    assert [loc.line for loc in pr.locations] == [4]


def test_same_text_with_contradictory_statuses_is_cross_referenced(tmp_path):
    reg = REG + [
        A("t/mkt-v24", "2026-10-06", [{"type": "pattern", "pattern": r"""api_version\s*=\s*["']v24\.0["']"""}],
          kind="behavior-change"),
        A("t/graph-v24", "2028-02-18", [{"type": "pattern", "pattern": r"""["']v24\.0["']"""}]),
    ]
    write(tmp_path, {"a.py": 'Api.init(api_version="v24.0")\nother = "x"\n'})
    by = {f.id: f for f in run(tmp_path, registry=reg).findings}
    assert by["t/mkt-v24"].bucket == "expired" and by["t/graph-v24"].bucket == "future"
    assert by["t/mkt-v24"].locations[0].also == ["t/graph-v24 (2028-02-18)"]
    assert by["t/graph-v24"].locations[0].also == ["t/mkt-v24 (2026-10-06)"]


def test_soft_kinds_are_expired_not_broken_but_still_fail(tmp_path):
    reg = [A("t/nginx", "2026-03-24", [{"type": "literal", "ids": ["ingress-nginx/controller"]}], kind="unmaintained")]
    write(tmp_path, {"values.yaml": "image: ingress-nginx/controller\n"})
    (f,) = run(tmp_path, registry=reg).findings
    assert f.status == "past" and f.bucket == "expired"
    assert is_failing(f, 30, strict=False, fail_on_blockers=False)


def test_real_registry_suggests_a_replacement_that_still_works(tmp_path):
    """o1-mini -> o4-mini and chatgpt-4o-latest -> gpt-5.1-chat-latest point at retired models."""
    from sunset_scan.registry import model_id_index, retiring_replacements

    write(tmp_path, {"a.py": 'a = "o1-mini"\nb = "chatgpt-4o-latest"\nc = "gpt-4-32k"\n'})
    bundle = load_bundle()
    r = scan(tmp_path, bundle["advisories"], bundle, today=TODAY)
    index = model_id_index(bundle["advisories"])
    by = {f.id: f for f in r.findings}
    for aid, stale in (("openai/o1-mini", "o4-mini"), ("openai/chatgpt-4o-latest", "gpt-5.1-chat-latest"),
                       ("openai/gpt-4-32k", "gpt-4o")):
        f = by[aid]
        assert f.replacements == [stale]  # the vendor's original text is kept
        assert f.suggested and f.suggested[0] != stale
        assert not list(retiring_replacements(f.suggested[0], index)), f.suggested


def test_utf16_unreadable_binary_and_bad_manifests_are_not_silent(tmp_path):
    (tmp_path / "win.ps1").write_bytes('$m = "gpt-4o"\r\n'.encode("utf-16"))
    (tmp_path / "blob.dat").write_bytes(b"\x00\x01\x02gpt-4o\x00\xff" * 10)
    write(tmp_path, {"package.json": '{"dependencies": {', "requirements.txt": "<<<<<<< HEAD\nrequests==2.0\n"})
    secret = tmp_path / "locked.py"
    secret.write_text('m = "gpt-4o"\n')
    secret.chmod(0)
    try:
        r = run(tmp_path)
    finally:
        secret.chmod(0o644)
    f = next(f for f in r.findings if f.id == "t/gpt-4o")
    assert [loc.path for loc in f.locations] == ["win.ps1"]
    joined = "\n".join(r.notes)
    assert "looked binary" in joined and "blob.dat" in joined
    assert "package.json (invalid JSON" in joined
    assert "requirements.txt:1" in joined
    import os
    if os.geteuid() != 0:  # root can read anything
        assert "could not be read" in joined and "locked.py" in joined
