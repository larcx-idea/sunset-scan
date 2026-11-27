import json
from pathlib import Path

from sunset_scan.cli import main

REPO = Path(__file__).resolve().parents[1]
TODAY = ["--today", "2026-11-27"]


def test_demo_end_to_end(capsys):
    assert main(["demo", "--color", "never", *TODAY]) == 0
    out = capsys.readouterr().out
    assert "src/classify.ts:9" in out and "gpt-4o-mini" in out and "48 days left" in out
    assert "Meta Commerce Order Management API" in out and "31 days ago" in out
    assert "facebook-business 24.0.1" in out
    assert "blocks upgrade to eslint >=10.0.0" in out
    # The demo exits 0, so its text must not claim "exit 1" (review finding).
    assert "exit 1:" not in out and "exit 0 (the demo always exits 0" in out and "a normal run would exit 1" in out
    # Kind-aware labels: unmaintained is EOL and an expired Meta version is CHANGED, not BROKEN.
    assert " EOL " in out and "Ingress NGINX" in out
    assert "CHANGED 52 days ago    Meta Marketing API v24.0" in out
    assert "same text also matches meta/graph-api-v24.0" in out
    assert main(["demo", "--color", "never", "--exit-code", *TODAY]) == 1
    assert "exit 1:" in capsys.readouterr().out


def test_clean_repo_exits_zero(tmp_path, capsys):
    (tmp_path / "app.py").write_text('print("hello")\n')
    assert main([str(tmp_path), "--color", "never", *TODAY]) == 0
    assert "Nothing in this repo matches" in capsys.readouterr().out


def test_zero_files_is_not_reported_as_clean(tmp_path, capsys):
    # A parent .sunsetignore in the git repo that excludes the scanned folder must not read as "all clear".
    (tmp_path / ".git").mkdir()
    (tmp_path / ".sunsetignore").write_text("app/\n")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "a.py").write_text('MODEL = "gpt-4o-mini"\n')
    assert main([str(tmp_path / "app"), "--color", "never", *TODAY]) == 0
    out = capsys.readouterr().out
    assert "No files were scanned" in out and "Nothing in this repo matches" not in out


def test_demo_app_self_scan_ignores_repo_root_ignore(capsys):
    # The CI self-test scans the bundled demo app inside this git repo; the root .sunsetignore skips it,
    # so the demo app carries its own empty .sunsetignore.
    assert main([str(REPO / "src" / "sunset_scan" / "demo_app"), "--color", "never", "--no-fail", *TODAY]) == 0
    assert "gpt-4o-mini" in capsys.readouterr().out


def test_fail_within_and_strict(tmp_path, capsys):
    (tmp_path / "a.py").write_text('MODEL = "gpt-4o-mini"  # 48 days left on 2026-11-27\n')
    assert main([str(tmp_path), *TODAY]) == 0
    assert main([str(tmp_path), "--fail-within", "60", *TODAY]) == 1
    assert main([str(tmp_path), "--fail-within", "60", "--no-fail", *TODAY]) == 0
    (tmp_path / "a.py").write_text("# we migrated off gpt-4o-mini last year\n")
    assert main([str(tmp_path), "--fail-within", "60", *TODAY]) == 0
    assert main([str(tmp_path), "--fail-within", "60", "--strict", *TODAY]) == 1
    capsys.readouterr()


def test_json_sarif_markdown_reports(tmp_path, capsys):
    (tmp_path / "a.py").write_text('MODEL = "gpt-5-mini"\n')
    out_dir = tmp_path / "out"
    code = main([str(tmp_path), "--format", "json", "--report", f"sarif={out_dir}/r.sarif",
                 "--report", f"markdown={out_dir}/r.md", *TODAY])
    assert code == 1  # 14 days left, inside the default 30-day window
    doc = json.loads(capsys.readouterr().out)
    (f,) = doc["findings"]
    assert f["id"] == "openai/gpt-5-mini" and f["days_left"] == 14 and f["replacements"] == ["gpt-6.1-terra"]
    assert f["locations"][0]["path"] == "a.py" and f["locations"][0]["line"] == 1
    assert doc["exit_code"] == 1

    sarif = json.loads((out_dir / "r.sarif").read_text())
    assert sarif["version"] == "2.1.0"
    res = sarif["runs"][0]["results"][0]
    assert res["ruleId"] == "openai/gpt-5-mini" and res["level"] == "error"
    assert res["locations"][0]["physicalLocation"]["region"]["startLine"] == 1

    md = (out_dir / "r.md").read_text()
    assert md.startswith("<!-- sunset-scan:report -->") and "| **URGENT** 14 days left |" in md


def test_within_filter(tmp_path, capsys):
    (tmp_path / "a.py").write_text('A = "gpt-5-mini"\nB = "whisper-1"\n')
    main([str(tmp_path), "--format", "json", "--within", "30", *TODAY])
    doc = json.loads(capsys.readouterr().out)
    assert [f["id"] for f in doc["findings"]] == ["openai/gpt-5-mini"]


def test_list_and_show(capsys):
    assert main(["list", "--within", "20", "--vendor", "openai", *TODAY]) == 0
    out = capsys.readouterr().out
    assert "openai/gpt-5-mini" in out and "openai/whisper-1" not in out
    assert main(["show", "kubernetes/ingress-nginx"]) == 0
    out = capsys.readouterr().out
    assert "Gateway API" in out and "nginx.ingress.kubernetes.io/" in out
    assert main(["show", "nope/nothing"]) == 2


def test_registry_commands(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(REPO)
    assert main(["registry", "validate", "registry", *TODAY]) == 0
    assert main(["registry", "build", "--check"]) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("schema_version: 1\nadvisories:\n  - id: Bad\n    title: x\n")
    assert main(["registry", "validate", str(bad)]) == 1
    assert "missing required field" in capsys.readouterr().out


def test_custom_registry_and_ignore_id(tmp_path, capsys):
    reg = tmp_path / "reg"
    reg.mkdir()
    (reg / "r.yaml").write_text(
        "schema_version: 1\nadvisories:\n"
        "  - id: acme/widgets-v1\n    title: Acme widgets v1 shutdown\n    vendor: Acme\n    kind: shutdown\n"
        "    effective: 2026-12-01\n    source: https://example.com\n"
        "    affected:\n      - type: literal\n        ids: [api.acme.test/v1/widgets]\n"
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.js").write_text('fetch("https://api.acme.test/v1/widgets")\n')
    assert main([str(repo), "--registry", str(reg), *TODAY]) == 1
    assert main([str(repo), "--registry", str(reg), "--ignore-id", "acme/widgets-v1", *TODAY]) == 0
    capsys.readouterr()


def test_self_scan_is_clean(capsys):
    """Dogfood: this repo's own .sunsetignore keeps the registry and fixtures out of the scan."""
    code = main([str(REPO), "--format", "json", "--strict", *TODAY])
    doc = json.loads(capsys.readouterr().out)
    assert doc["findings"] == [] and code == 0, [f["id"] for f in doc["findings"]]


def test_no_fail_never_claims_nothing_is_broken(tmp_path, capsys):
    """--no-fail (the Action's fail-within: never) used to print "nothing ... is broken"."""
    (tmp_path / "a.py").write_text('MODEL = "gpt-3.5-turbo"\n')
    assert main([str(tmp_path), "--no-fail", "--color", "never", *TODAY]) == 0
    out = capsys.readouterr().out
    assert "nothing high-confidence" not in out
    assert "exit 0 (--no-fail): 1 finding is past the date or due within 30 days; a normal run would exit 1." in out
    assert main([str(tmp_path), "--no-fail", "--format", "json", *TODAY]) == 0
    assert json.loads(capsys.readouterr().out)["exit_code"] == 0


def test_demo_ignores_sunsetignore_above_it(tmp_path, capsys, monkeypatch):
    """In a git checkout the repo-root .sunsetignore lists the demo app; the demo must not use it."""
    assert (REPO / ".sunsetignore").read_text().count("src/sunset_scan/demo_app/")
    if not (REPO / ".git").exists():  # simulate a git checkout: parents would be searched
        monkeypatch.setattr("sunset_scan.scanner.find_ignore_file", lambda root: REPO / ".sunsetignore")
    assert main(["demo", "--color", "never", "--format", "json", *TODAY]) == 0
    assert len(json.loads(capsys.readouterr().out)["findings"]) > 10


def test_markdown_report_fits_in_a_github_issue(tmp_path, capsys):
    reg = tmp_path / "reg.yaml"
    entries = []
    for i in range(400):
        entries.append(
            f"  - id: acme/widget-{i}\n    title: Acme widget {i} API shutdown with a fairly long title for size\n"
            f"    vendor: Acme\n    kind: shutdown\n    effective: 2026-12-0{1 + i % 9}\n"
            f"    replacement: Use the widget {i} v2 API, which keeps the same fields but renames a few of them\n"
            f"    source: https://example.com/acme/deprecations/widget-{i}\n"
            f"    affected:\n      - type: literal\n        ids: [acme_widget_{i}_v1]\n")
    reg.write_text("schema_version: 1\nadvisories:\n" + "".join(entries))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text("".join(f"x{i} = acme_widget_{i}_v1()\n" for i in range(400)))
    md = tmp_path / "r.md"
    main([str(repo), "--registry", str(reg), "--report", f"markdown={md}", "--color", "never", *TODAY])
    capsys.readouterr()
    text = md.read_text()
    assert len(text) < 65_536
    assert "more findings not shown" in text
    assert text.rstrip().endswith("(https://github.com/larcx-idea/sunset-scan).")  # the footer survives
