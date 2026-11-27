from sunset_scan.registry import validate
from sunset_scan.upstream import convert_endoflife, convert_litellm, drift

EOL_DOC = {"result": {
    "links": {"html": "https://endoflife.date/claude",
              "releasePolicy": "https://platform.claude.com/docs/en/about-claude/model-deprecations"},
    "releases": [
        {"name": "claude-old-1", "eolFrom": "2026-02-19", "eoasFrom": "2025-12-19",
         "custom": {"recommendedReplacement": "claude-new-2"}},
        # no deprecation date: an "earliest retirement" guarantee, not a shutdown -> skipped
        {"name": "claude-new-2", "eolFrom": "2027-09-22", "eoasFrom": None, "custom": {"recommendedReplacement": "N/A"}},
        {"name": "claude-curated", "eolFrom": "2026-01-05", "eoasFrom": "2025-06-30", "custom": {}},
    ],
}}


def test_convert_endoflife_only_announced_and_uncovered():
    advs = convert_endoflife(EOL_DOC, "claude", covered={"claude-curated"}, taken_ids=set())
    assert [a["id"] for a in advs] == ["anthropic/claude-old-1"]
    a = advs[0]
    assert a["effective"] == "2026-02-19" and a["announced"] == "2025-12-19"
    assert a["replacement"] == "claude-new-2" and a["origin"] == "endoflife.date"
    assert [i for i in validate(advs) if i.level == "error"] == []


def test_convert_endoflife_aliases():
    doc = {"result": {"links": {}, "releases": [
        {"name": "gpt-x", "eolFrom": "2027-01-14", "eoasFrom": "2026-10-14",
         "custom": {"aliases": "gpt-x-2024-01-01, gpt-x-2024-02-02", "recommendedReplacement": "gpt-y"}}]}}
    (a,) = convert_endoflife(doc, "openai-api-models", covered={"gpt-x-2024-02-02"}, taken_ids=set())
    assert a["affected"][0]["ids"] == ["gpt-x", "gpt-x-2024-01-01"]


def test_convert_litellm_filters_and_merges():
    data = {
        "sample_spec": {"deprecation_date": "date"},
        "gemini/gemini-9-flash": {"litellm_provider": "gemini", "deprecation_date": "2026-12-01"},
        "gemini-9-flash": {"litellm_provider": "vertex_ai-language-models", "deprecation_date": "2026-10-20"},
        "azure/gpt-4o": {"litellm_provider": "azure", "deprecation_date": "2026-01-01"},
        "together_ai/meta/llama": {"litellm_provider": "together_ai", "deprecation_date": "2026-01-01"},
        "xai/grok-old": {"litellm_provider": "xai", "deprecation_date": "2024-01-01"},
        "xai/grok-covered": {"litellm_provider": "xai", "deprecation_date": "2026-05-01"},
    }
    advs = convert_litellm(data, covered={"grok-covered"}, taken_ids=set())
    assert [a["id"] for a in advs] == ["google/gemini-9-flash"]
    a = advs[0]
    assert a["effective"] == "2026-10-20"
    assert "Gemini API" in a["summary"] and "Vertex AI" in a["summary"]
    assert a["affected"] == [{"type": "model", "ids": ["gemini-9-flash"]}]
    assert [i for i in validate(advs) if i.level == "error"] == []


def test_drift_detection():
    curated = [{"id": "x/claude-old-1", "effective": "2026-03-01",
                "affected": [{"type": "model", "ids": ["claude-old-1"]}]}]
    msgs = drift(curated, {"claude": EOL_DOC})
    assert len(msgs) == 1 and "2026-02-19" in msgs[0]
