from pathlib import Path

import pytest

from sunset_scan import yamlish
from sunset_scan.yamlish import YamlError

REPO = Path(__file__).resolve().parents[1]


def test_mappings_sequences_and_scalars():
    doc = yamlish.loads(
        """
# comment
schema_version: 1
advisories:
  - id: openai/gpt-5   # trailing comment
    title: 'It''s quoted: fine'
    flag: true
    nothing: null
    ids: [gpt-5, "gpt-5-2025-08-07", 'x, y']
    nested:
      key: value
    list:
    - a
    - b
"""
    )
    assert doc["schema_version"] == 1
    adv = doc["advisories"][0]
    assert adv["id"] == "openai/gpt-5"
    assert adv["title"] == "It's quoted: fine"
    assert adv["flag"] is True and adv["nothing"] is None
    assert adv["ids"] == ["gpt-5", "gpt-5-2025-08-07", "x, y"]
    assert adv["nested"] == {"key": "value"}
    assert adv["list"] == ["a", "b"]


def test_dates_stay_strings_and_hash_inside_values():
    doc = yamlish.loads("effective: 2026-12-11\nurl: https://example.com/a#frag\nre: 'a #b'\n")
    assert doc == {"effective": "2026-12-11", "url": "https://example.com/a#frag", "re": "a #b"}


def test_block_scalars():
    doc = yamlish.loads("a: >-\n  one\n  two\n\n  three\nb: |\n  x\n    y\nc: done\n")
    assert doc["a"] == "one two\nthree"
    assert doc["b"] == "x\n  y\n"
    assert doc["c"] == "done"


@pytest.mark.parametrize(
    "text, fragment",
    [
        ("a:\n\tb: 1\n", "tabs"),
        ("a: &anchor x\n", "unsupported"),
        ("a: {b: 1}\n", "flow mappings"),
        ("a: 1\na: 2\n", "duplicate"),
        ("a: b: c\n", "quoted"),
        ("a: 'open\n", "single-quoted"),
        ("a:\n  - [x, [y]]\n", "nested flow"),
    ],
)
def test_rejects_unsupported_yaml(text, fragment):
    with pytest.raises(YamlError) as exc:
        yamlish.loads(text)
    assert fragment in str(exc.value)


def test_error_has_line_number():
    with pytest.raises(YamlError) as exc:
        yamlish.loads("ok: 1\nbad: {x: 1}\n")
    assert exc.value.line == 2


def test_dump_round_trip():
    data = {
        "schema_version": 1,
        "advisories": [
            {
                "id": "x/y",
                "title": "Needs: quoting",
                "summary": "word " * 40,
                "pattern": r"""["'`]v20\.0["'`]""",
                "ids": ["a", "b,c", "true", "1.5", "2026-01-01"],
                "flag": False,
                "affected": [{"type": "model", "ids": ["gpt-4o"]}],
                "empty": [],
            }
        ],
    }
    data["advisories"][0]["summary"] = data["advisories"][0]["summary"].strip()
    text = yamlish.dumps(data, header="generated")
    assert text.startswith("# generated\n")
    assert yamlish.loads(text) == data


def test_every_registry_file_parses_and_round_trips():
    files = sorted((REPO / "registry").rglob("*.yaml"))
    assert files, "registry files missing"
    for f in files:
        doc = yamlish.loads(f.read_text())
        assert doc["schema_version"] == 1
        assert yamlish.loads(yamlish.dumps(doc)) == doc, f
