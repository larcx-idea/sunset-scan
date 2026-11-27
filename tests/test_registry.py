import datetime as dt
import json
from pathlib import Path

from sunset_scan.registry import (
    compile_bundle, dump_bundle, load_bundle, load_registry, load_yaml_dir, validate,
)

REPO = Path(__file__).resolve().parents[1]
TODAY = dt.date(2026, 11, 27)


def good(**over):
    adv = {
        "id": "acme/widget-v1",
        "title": "Acme widget v1 shutdown",
        "vendor": "Acme",
        "kind": "shutdown",
        "effective": "2027-01-01",
        "source": "https://example.com/deprecations",
        "affected": [{"type": "literal", "ids": ["/v1/widgets"]}],
    }
    adv.update(over)
    return adv


def errors(advs):
    return [i for i in validate(advs, today=TODAY) if i.level == "error"]


def test_repo_registry_is_valid():
    advisories, issues = load_yaml_dir(REPO / "registry")
    issues += validate(advisories, today=TODAY)
    assert [str(i) for i in issues if i.level == "error"] == []
    assert len(advisories) >= 150
    curated = [a for a in advisories if a.get("origin", "curated") == "curated"]
    assert len(curated) >= 140
    assert all(a["source"].startswith("https://") for a in advisories)


def test_bundle_matches_yaml():
    advisories, _ = load_yaml_dir(REPO / "registry")
    expected = dump_bundle(compile_bundle(advisories))
    actual = (REPO / "src/sunset_scan/data/registry.json").read_text()
    assert actual == expected, "bundle out of date: run `sunset-scan registry build`"


def test_bundled_registry_loads():
    bundle = load_bundle()
    assert bundle["schema_version"] == 1
    assert bundle["count"] == len(bundle["advisories"])
    ids = [a["id"] for a in bundle["advisories"]]
    assert len(ids) == len(set(ids))
    for must in ("openai/gpt-4o-mini", "meta/graph-api-v21.0", "kubernetes/ingress-nginx",
                 "minio/community-edition", "bitnami/postgresql", "npm/enzyme", "npm/eslint-plugin-react-eslint-10"):
        assert must in ids


def test_valid_advisory_passes():
    assert errors([good()]) == []


def test_catches_common_mistakes():
    cases = {
        "id must look like": good(id="Bad Id"),
        "effective must be a date": good(effective="next year"),
        "source must be an https": good(source="http://example.com"),
        "unknown field": good(severity="high"),
        "bad regex": good(affected=[{"type": "pattern", "pattern": "("}]),
        "need 'blocks": good(kind="upgrade-blocker"),
        "only applies to npm/pypi": good(affected=[{"type": "model", "ids": ["x"], "versions": "<1"}]),
        "kind must be one of": good(kind="sunset"),
        "missing required field 'affected'": good(affected=[]),
        "announced is after effective": good(announced="2027-06-01"),
        "contains whitespace": good(affected=[{"type": "model", "ids": ["gpt 4"]}]),
    }
    for fragment, adv in cases.items():
        msgs = " | ".join(str(e) for e in errors([adv]))
        assert fragment in msgs, (fragment, msgs)


def test_duplicate_ids():
    msgs = " ".join(str(e) for e in errors([good(), good()]))
    assert "duplicate id" in msgs


def test_replacement_chain_is_reported_as_info():
    a = good(id="acme/a", affected=[{"type": "model", "ids": ["model-a"]}], replacement="model-b")
    b = good(id="acme/b", affected=[{"type": "model", "ids": ["model-b"]}], effective="2027-03-01")
    infos = [i for i in validate([a, b], today=TODAY) if i.level == "info"]
    assert len(infos) == 1 and "model-b" in infos[0].message


def test_load_registry_from_directory(tmp_path):
    (tmp_path / "x.yaml").write_text(
        "schema_version: 1\nadvisories:\n"
        "  - id: acme/widget-v1\n    title: Widget\n    vendor: Acme\n    kind: shutdown\n"
        "    effective: 2027-01-01\n    source: https://example.com\n"
        "    affected:\n      - type: literal\n        ids: [/v1/widgets]\n"
    )
    advisories, meta = load_registry(str(tmp_path))
    assert [a["id"] for a in advisories] == ["acme/widget-v1"]
    assert meta["count"] == 1


def test_bundle_is_deterministic():
    advisories, _ = load_yaml_dir(REPO / "registry")
    assert dump_bundle(compile_bundle(advisories)) == dump_bundle(compile_bundle(list(reversed(advisories))))
    assert json.loads(dump_bundle(compile_bundle(advisories)))["registry_date"] == "2026-11-27"


# ---------------------------------------------------------------- link checking

class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(script):
    """Fake urlopen: pops one outcome per call (an int status, or an exception to raise)."""
    import urllib.error

    calls = []

    def opener(req, timeout=None):
        calls.append(req.get_method())
        outcome = script.pop(0) if script else 200
        if isinstance(outcome, int) and outcome >= 400:
            raise urllib.error.HTTPError(req.full_url, outcome, "x", {}, None)
        if isinstance(outcome, Exception):
            raise outcome
        return _Resp(outcome)

    return opener, calls


def test_link_check_retries_transient_server_errors():
    """A one-off 503 from GitHub used to fail registry CI (review finding)."""
    from sunset_scan.registry import check_links

    opener, calls = _opener([503, 503, 200])  # HEAD 503 -> GET 503 -> retry HEAD 200
    assert check_links([good()], opener=opener, sleep=lambda s: None, workers=1) == []
    assert calls == ["HEAD", "GET", "HEAD"]

    opener, _ = _opener([TimeoutError("timed out"), TimeoutError("timed out"), 200])
    assert check_links([good()], opener=opener, sleep=lambda s: None, workers=1) == []


def test_link_check_persistent_5xx_warns_and_404_errors():
    from sunset_scan.registry import check_links

    opener, _ = _opener([503] * 10)
    (issue,) = check_links([good()], opener=opener, sleep=lambda s: None, workers=1)
    assert issue.level == "warning" and "503" in issue.message
    opener, _ = _opener([404, 404])
    (issue,) = check_links([good()], opener=opener, sleep=lambda s: None, workers=1)
    assert issue.level == "error" and "404" in issue.message


def test_meta_version_expiries_are_behavior_changes():
    """Meta upgrades calls to expired versions; the report must not call them BROKEN."""
    advs = {a["id"]: a for a in load_bundle()["advisories"]}
    for aid in ("meta/graph-api-v20.0", "meta/marketing-api-v24.0", "meta/graph-api-legacy-versions"):
        assert advs[aid]["kind"] == "behavior-change", aid
    assert advs["meta/commerce-order-management-api"]["kind"] == "shutdown"
