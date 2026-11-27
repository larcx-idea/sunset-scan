"""Import model retirement dates that other projects already maintain.

The registry stays a thin aggregation layer: people hand-curate only what no one
else tracks (API versions, archived infrastructure, image catalogs, endpoint
shutdowns), and this module imports the rest:

* endoflife.date ``claude`` and ``openai-api-models`` products (MIT licensed)
* LiteLLM's ``deprecation_date`` field in model_prices_and_context_window.json (MIT)

Curated advisories always win: an identifier that a curated advisory already
lists is never imported again. The importer also reports *drift* where a curated
date disagrees with upstream, which is how moved vendor dates get noticed.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

from . import yamlish

ENDOFLIFE_URL = "https://endoflife.date/api/v1/products/{}"
ENDOFLIFE_PRODUCTS = {"claude": ("anthropic", "Anthropic"), "openai-api-models": ("openai", "OpenAI")}
LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
LITELLM_PAGE = "https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json"
# litellm_provider -> (id prefix, vendor label, key prefix to strip, provider page)
LITELLM_PROVIDERS = {
    "gemini": ("google", "Google (Gemini API)", "gemini/", "https://ai.google.dev/gemini-api/docs/deprecations"),
    "vertex_ai-language-models": ("google", "Google (Vertex AI)", "vertex_ai/", "https://cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions"),
    "vertex_ai-embedding-models": ("google", "Google (Vertex AI)", "vertex_ai/", "https://cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions"),
    "anthropic": ("anthropic", "Anthropic", "anthropic/", "https://platform.claude.com/docs/en/about-claude/model-deprecations"),
    "xai": ("xai", "xAI", "xai/", "https://docs.x.ai/docs/models"),
    "mistral": ("mistral", "Mistral AI", "mistral/", "https://docs.mistral.ai/getting-started/models/"),
    "codestral": ("mistral", "Mistral AI", "codestral/", "https://docs.mistral.ai/getting-started/models/"),
    "deepseek": ("deepseek", "DeepSeek", "deepseek/", "https://api-docs.deepseek.com/"),
    "moonshot": ("moonshot", "Moonshot AI", "moonshot/", "https://platform.moonshot.ai/"),
    "cerebras": ("cerebras", "Cerebras", "cerebras/", "https://inference-docs.cerebras.ai/"),
}
EMPTY_REPLACEMENTS = {"", "n/a", "na", "-", "—", "none", "null"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@\-]*$")


def fetch_json(url: str, timeout: float = 60.0):
    req = urllib.request.Request(url, headers={"User-Agent": "sunset-scan-registry-sync/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9._+-]+", "-", text.lower()).strip("-.")


def covered_identifiers(advisories: list[dict]) -> set[str]:
    out = set()
    for adv in advisories:
        for aff in adv.get("affected") or []:
            if aff.get("type") == "model":
                out.update(aff.get("ids") or [])
    return out


def convert_endoflife(doc: dict, product: str, covered: set[str], taken_ids: set[str]) -> list[dict]:
    result = doc["result"]
    prefix, vendor = ENDOFLIFE_PRODUCTS[product]
    links = result.get("links") or {}
    html = links.get("html") or f"https://endoflife.date/{product}"
    policy = links.get("releasePolicy") or html
    out = []
    for rel in result.get("releases") or []:
        eol, eoas = rel.get("eolFrom"), rel.get("eoasFrom")
        # Only announced retirements. Entries without a deprecation date carry an
        # "earliest possible retirement" guarantee, not a shutdown date.
        if not eol or not eoas:
            continue
        custom = rel.get("custom") or {}
        ids = [rel["name"]] + [a.strip() for a in str(custom.get("aliases") or "").split(",")]
        ids = [i for i in dict.fromkeys(ids) if i and SAFE_ID.match(i) and i not in covered]
        if not ids:
            continue
        aid = f"{prefix}/{_slug(rel['name'])}"
        if aid in taken_ids:
            continue
        taken_ids.add(aid)
        adv = {
            "id": aid,
            "title": f"{vendor} API retirement: {rel['name']}",
            "vendor": vendor,
            "kind": "shutdown",
            "effective": eol,
        }
        if eoas <= eol:
            adv["announced"] = eoas
        repl = str(custom.get("recommendedReplacement") or "").strip()
        if repl.lower() not in EMPTY_REPLACEMENTS:
            adv["replacement"] = repl
        adv["summary"] = f"Imported from endoflife.date ({product}): deprecated {eoas}, retires {eol}."
        adv["source"] = policy
        adv["references"] = [html]
        adv["origin"] = "endoflife.date"
        adv["affected"] = [{"type": "model", "ids": ids}]
        out.append(adv)
    return out


def convert_litellm(data: dict, covered: set[str], taken_ids: set[str], min_date: str = "2025-01-01") -> list[dict]:
    groups: dict[str, dict] = {}
    for key, meta in data.items():
        if not isinstance(meta, dict):
            continue
        date = meta.get("deprecation_date")
        prov = meta.get("litellm_provider")
        if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or date < min_date:
            continue
        if prov not in LITELLM_PROVIDERS:
            continue
        prefix, vendor, strip, page = LITELLM_PROVIDERS[prov]
        bare = key[len(strip):] if key.startswith(strip) else key
        if "/" in bare or not SAFE_ID.match(bare) or bare in covered:
            continue
        g = groups.setdefault(bare, {"prefix": prefix, "dates": {}, "pages": [], "vendors": []})
        g["dates"][vendor] = min(date, g["dates"].get(vendor, date))
        if page not in g["pages"]:
            g["pages"].append(page)
        if vendor not in g["vendors"]:
            g["vendors"].append(vendor)
    out = []
    for bare in sorted(groups):
        g = groups[bare]
        aid = f"{g['prefix']}/{_slug(bare)}"
        if aid in taken_ids:
            continue
        taken_ids.add(aid)
        effective = min(g["dates"].values())
        per = "; ".join(f"{v}: {d}" for v, d in sorted(g["dates"].items(), key=lambda kv: kv[1]))
        vendor = g["vendors"][0].split(" (")[0]
        out.append({
            "id": aid,
            "title": f"{vendor} model {bare} deprecated (dates via LiteLLM)",
            "vendor": vendor,
            "kind": "shutdown",
            "effective": effective,
            "summary": f"LiteLLM's model map lists deprecation dates ({per}). The earliest is used. "
                       "These are community-maintained; confirm on the provider's page.",
            "source": LITELLM_PAGE,
            "references": g["pages"],
            "origin": "litellm",
            "affected": [{"type": "model", "ids": [bare]}],
        })
    return out


def drift(curated: list[dict], eol_docs: dict[str, dict]) -> list[str]:
    """Curated model ids whose date disagrees with endoflife.date."""
    upstream: dict[str, tuple[str, str]] = {}
    for product, doc in eol_docs.items():
        for rel in doc["result"].get("releases") or []:
            if not rel.get("eolFrom") or not rel.get("eoasFrom"):
                continue
            custom = rel.get("custom") or {}
            for ident in [rel["name"]] + [a.strip() for a in str(custom.get("aliases") or "").split(",")]:
                if ident:
                    upstream.setdefault(ident, (rel["eolFrom"], product))
    msgs = []
    for adv in curated:
        for aff in adv.get("affected") or []:
            if aff.get("type") != "model":
                continue
            for ident in aff.get("ids") or []:
                if ident in upstream and upstream[ident][0] != str(adv.get("effective")):
                    msgs.append(f"{adv['id']}: {ident} is {adv.get('effective')} here but "
                                f"{upstream[ident][0]} on endoflife.date/{upstream[ident][1]}")
    return msgs


HEADER = ("Generated by `sunset-scan registry sync` from {src}.\n"
          "Do not edit by hand: fix the data upstream, or add a curated advisory\n"
          "(identifiers listed in registry/curated are never imported).")


def sync(registry_dir: Path, from_dir: Path | None = None) -> tuple[dict[str, int], list[str]]:
    from .registry import load_yaml_dir

    registry_dir = Path(registry_dir)
    curated, _ = load_yaml_dir(registry_dir / "curated")
    covered = covered_identifiers(curated)
    taken = {a.get("id") for a in curated}

    def get(name: str, url: str):
        if from_dir:
            return json.loads((Path(from_dir) / name).read_text(encoding="utf-8"))
        return fetch_json(url)

    eol_docs = {p: get(f"endoflife-{p}.json", ENDOFLIFE_URL.format(p)) for p in ENDOFLIFE_PRODUCTS}
    eol_advs: list[dict] = []
    for product, doc in eol_docs.items():
        eol_advs += convert_endoflife(doc, product, covered, taken)
    covered |= covered_identifiers(eol_advs)
    lite_advs = convert_litellm(get("litellm.json", LITELLM_URL), covered, taken)

    out_dir = registry_dir / "imported"
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, advs, src in (
        ("endoflife-date.yaml", eol_advs, "https://endoflife.date (products: " + ", ".join(ENDOFLIFE_PRODUCTS) + ")"),
        ("litellm.yaml", lite_advs, LITELLM_PAGE),
    ):
        advs.sort(key=lambda a: (a["effective"], a["id"]))
        (out_dir / name).write_text(
            yamlish.dumps({"schema_version": 1, "advisories": advs}, header=HEADER.format(src=src)), encoding="utf-8")
    return {"endoflife.date": len(eol_advs), "litellm": len(lite_advs)}, drift(curated, eol_docs)
