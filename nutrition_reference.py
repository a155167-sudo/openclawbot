"""Taiwan-first nutrition references with exact identity and unit boundaries.

TFDA composition values in this catalog are published per 100 grams.  A package
or unit weight expressed in grams is not evidence of millilitres or density.
``cc`` normalizes to ``ml``. An explicitly approved soy-only derived ml row
retains the original per100g values and density provenance; no generic conversion.
"""

import json
import math
from pathlib import Path
import re
import unicodedata


DATA_PATH = Path(__file__).parent / "nutrition_reference_data.json"
_UNIT_ALIASES = {
    "g": "g",
    "克": "g",
    "公克": "g",
    "ml": "ml",
    "毫升": "ml",
    "cc": "ml",
}
_SOURCE_FIELDS = (
    "card_note",
    "density_g_per_ml",
    "density_policy",
    "original_basis_amount",
    "original_basis_unit",
    "original_nutrition",
    "food_code",
    "source_row_id",
    "name",
    "state",
    "source_url",
    "basis_amount",
    "basis_unit",
    "source_original_basis",
    "publisher",
    "source_product",
    "source_type",
    "source_note",
    "reference_label",
    "source_label",
    "dataset_url",
    "metadata_url",
    "dataset_version",
    "retrieved_at",
    "archive_sha256",
    "evidence_sha256",
    "selected_rows_sha256",
    "reference_scope",
    "preparation_assumption",
    "variety_assumption",
    "version_assumption",
)


def _key(value):
    return "".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def _unit(value):
    return _UNIT_ALIASES.get(_key(value), _key(value))


def _valid_amount(amount):
    return (
        not isinstance(amount, bool)
        and isinstance(amount, (float, int))
        and math.isfinite(amount)
        and 0 < amount <= 10000
    )


def _document():
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def _matching_items(food_name, document):
    name = _key(food_name)
    if not name:
        return []
    return [
        item
        for item in document["items"]
        if name in {_key(alias) for alias in item["aliases"]}
    ]


_STATE_TERMS = (
    ("尚未烹煮", ("preparation", "raw")),
    ("還沒煮", ("preparation", "raw")),
    ("未烹調", ("preparation", "raw")),
    ("未煮", ("preparation", "raw")),
    ("生鮮", ("preparation", "raw")),
    ("熟重", ("weight_basis", "cooked")),
    ("煮熟重量", ("weight_basis", "cooked")),
    ("烹煮後重量", ("weight_basis", "cooked")),
    ("乾燥重量", ("weight_basis", "dry")),
    ("乾重", ("weight_basis", "dry")),
    ("未煮重", ("weight_basis", "dry")),
    ("去皮", ("skin", "removed")),
    ("削皮", ("skin", "removed")),
    ("不帶皮", ("skin", "removed")),
    ("未去皮", ("skin", "included")),
    ("帶皮", ("skin", "included")),
    ("含皮", ("skin", "included")),
    ("連皮", ("skin", "included")),
    ("去膜", ("membrane", "removed")),
    ("不帶膜", ("membrane", "removed")),
    ("帶膜", ("membrane", "included")),
    ("含膜", ("membrane", "included")),
    ("水煮", ("method", "boiled")),
    ("汆燙", ("method", "blanched")),
    ("清蒸", ("method", "steamed")),
    ("蒸熟", ("method", "steamed")),
    ("蒸", ("method", "steamed")),
    ("烘烤", ("method", "roasted")),
    ("烤", ("method", "roasted")),
    ("油炸", ("method", "fried")),
    ("炸", ("method", "fried")),
    ("煮熟", ("preparation", "cooked")),
    ("熟", ("preparation", "cooked")),
    ("生", ("preparation", "raw")),
)
_VARIETY_RE = re.compile(
    r"(?:黃肉|紫肉|紅肉|白肉|綠肉|黃皮|紫皮|紅皮|白皮|綠皮|"
    r"西洋種|雜交種|普遍系|大果|小果)"
)


def _add_attribute(profile, attribute, value):
    previous = profile.get(attribute)
    if previous is not None and previous != value:
        profile["conflict"] = True
    profile[attribute] = value


def _request_state_profile(food_state):
    """Parse only explicit state vocabulary; unknown text is not evidence.

    Terms are matched longest-first and consumed.  In particular, the single
    character ``生`` is never found by substring search, so ``花生`` cannot be
    reinterpreted as a raw-state assertion; likewise only explicit ``帶皮`` or
    ``含皮`` means skin-on.
    """

    original = _key(food_state)
    profile = {"conflict": False, "unknown": False, "varieties": set()}
    if not original:
        return profile
    remaining = original
    for term, (attribute, value) in sorted(
        _STATE_TERMS, key=lambda entry: len(entry[0]), reverse=True
    ):
        if term in {"生", "熟", "蒸", "烤", "炸"}:
            continue
        if term in remaining:
            _add_attribute(profile, attribute, value)
            if attribute == "method":
                _add_attribute(profile, "preparation", "cooked")
            if attribute == "weight_basis" and value == "cooked":
                _add_attribute(profile, "preparation", "cooked")
            remaining = remaining.replace(term, "")
    for variety in _VARIETY_RE.findall(remaining):
        profile["varieties"].add(variety)
        remaining = remaining.replace(variety, "")
    # Single-character state terms must be complete comma-like components.
    components = [part for part in re.split(r"[,，、;/；|]+", remaining) if part]
    for component in components[:]:
        if component in {"生", "熟"}:
            _add_attribute(profile, "preparation", "raw" if component == "生" else "cooked")
            components.remove(component)
        elif component in {"蒸", "烤", "炸"}:
            method = {"蒸": "steamed", "烤": "roasted", "炸": "fried"}[component]
            _add_attribute(profile, "method", method)
            _add_attribute(profile, "preparation", "cooked")
            components.remove(component)
    residue = "".join(components)
    for label in ("樣品狀態", "食品狀態", "品種", "狀態", ":", "：", "(", ")", "（", "）"):
        residue = residue.replace(label, "")
    profile["unknown"] = bool(residue) or not any(
        key in profile for key in ("preparation", "weight_basis", "skin", "membrane", "method")
    ) and not profile["varieties"]
    return profile


def _source_state_profile(item):
    state = _key(item.get("state", ""))
    aliases = [_key(alias) for alias in item.get("aliases", [])]
    # Only affirmative source identity/state and the already-authorized aliases
    # establish facts. Assumption prose often names exclusions (for example
    # "不可套用...去膜花生") and must never be parsed as positive metadata.
    scope = _key("；".join([
        str(item.get("name", "")), str(item.get("state", "")), *item.get("aliases", [])
    ]))
    profile: dict[str, object] = {"varieties": set(_VARIETY_RE.findall(scope))}

    if (re.search(r"樣品狀態[:：]生(?:[,，;；]|$)", state)
            or state.startswith("生")
            or any(alias.startswith("生") or "(生)" in alias or "（生）" in alias
                   for alias in aliases)):
        profile["preparation"] = "raw"
    if (re.search(r"樣品狀態[:：]熟(?:[,，;；]|$)", state)
            or any(term in scope for term in ("水煮", "煮熟", "蒸10分鐘", "茶葉蛋"))
            or any("熟" in alias for alias in aliases)):
        profile["preparation"] = "cooked"

    if any(term in scope for term in ("去皮", "削皮", "去蒂及皮", "去皮及")):
        profile["skin"] = "removed"
    elif any(term in scope for term in ("帶皮", "含皮", "連皮")):
        profile["skin"] = "included"
    if any(term in scope for term in ("去膜", "去皮膜")):
        profile["membrane"] = "removed"
    elif any(term in scope for term in ("帶膜", "含膜")):
        profile["membrane"] = "included"

    if any(term in scope for term in ("乾重", "乾麵", "燕麥乾", "乾燕麥", "乾冬粉")):
        profile["weight_basis"] = "dry"
    elif profile.get("preparation") == "cooked":
        profile["weight_basis"] = "cooked"

    methods = {
        "boiled": ("水煮",), "blanched": ("汆燙",),
        "steamed": ("清蒸", "蒸10分鐘", "鯖魚(蒸)"),
        "roasted": ("烘烤",), "fried": ("油炸",),
    }
    for method, terms in methods.items():
        if any(term in scope for term in terms):
            profile["method"] = method
            profile["preparation"] = "cooked"
            break
    return profile


def _profile_matches_source(requested, source):
    if requested["unknown"] or requested["conflict"]:
        return False
    for attribute in ("preparation", "weight_basis", "skin", "membrane", "method"):
        if attribute in requested and requested[attribute] != source.get(attribute):
            return False
    return requested["varieties"].issubset(source["varieties"])


def _evidence_state(request, item):
    evidence = _key(request.get("food_name_evidence", ""))
    if not evidence:
        return ""
    identities = {
        _key(request.get("food_name", "")), _key(item.get("name", "")),
        *(_key(alias) for alias in item.get("aliases", [])),
    }
    for identity in sorted((value for value in identities if value), key=len, reverse=True):
        evidence = evidence.replace(identity, "")
    evidence = re.sub(
        r"\d+(?:\.\d+)?(?:公斤|千克|kg|公克|克|g|毫升|ml|cc)?", "", evidence
    )
    return evidence.strip(" ,，、;/；|()（）")


def _state_compatible(request, item):
    states = []
    food_state = request.get("food_state")
    if food_state is not None and _key(food_state):
        states.append(food_state)
    evidence_state = _evidence_state(request, item)
    if evidence_state:
        states.append(evidence_state)
    if not states:
        # Existing exact aliases were separately source-scoped and remain the
        # compatibility path for legacy structured callers without state evidence.
        return True
    source = _source_state_profile(item)
    return all(
        _profile_matches_source(_request_state_profile(state), source)
        for state in states
    )


def _source(item, version):
    source = {key: item[key] for key in _SOURCE_FIELDS if key in item}
    source["source_name"] = item["name"]
    source.update(type="reference", version=version)
    return source


def reference_capability(food_name, unit):
    """Describe exact lookup capability without estimating or converting units.

    The status is one of ``exact``, ``unit_mismatch``, ``ambiguous_reference``,
    or ``missing``.  Source
    basis and provenance are returned for recognized foods so callers can show
    a unit warning before a separately labeled per-100-requested-unit AI fallback.
    A mismatch never authorizes treating grams as millilitres.
    """

    document = _document()
    matches = _matching_items(food_name, document)
    if not matches:
        return {
            "status": "missing",
            "food_exists": False,
            "available_units": [],
            "source_basis": None,
            "source": None,
        }

    available_units = sorted({item["basis_unit"] for item in matches})
    requested_unit = _unit(unit)
    compatible = [item for item in matches if item["basis_unit"] == requested_unit]
    # Independently sourced g and ml rows may share a name. Only multiple
    # candidates for the requested unit are ambiguous; never convert units.
    if len(compatible) > 1 or (not compatible and len(matches) > 1):
        return {
            "status": "ambiguous_reference",
            "food_exists": True,
            "available_units": available_units,
            "source_basis": None,
            "source": None,
        }
    selected = compatible[0] if compatible else matches[0]
    status = "exact" if len(compatible) == 1 else "unit_mismatch"
    return {
        "status": status,
        "food_exists": True,
        "available_units": available_units,
        "source_basis": {
            "amount": selected["basis_amount"],
            "unit": selected["basis_unit"],
        },
        "source": _source(selected, document["version"]),
    }


def find_reference_nutrition(request):
    """Return an official per-basis result, mismatch signal, or ``None``.

    Nutrition remains on the published ``basis_amount``/``basis_unit``.  The
    semantic pipeline can therefore scale deterministically in application
    code.  This function never performs a mass/volume conversion.
    """

    if not isinstance(request, dict) or not _valid_amount(request.get("amount")):
        return None
    document = _document()
    matches = _matching_items(request.get("food_name", ""), document)
    if not matches:
        return None
    unit = _unit(request.get("unit", ""))
    compatible = [item for item in matches if item["basis_unit"] == unit]
    if len(compatible) > 1 or (not compatible and len(matches) > 1):
        return {
            "status": "ambiguous_reference",
            "food_exists": True,
            "available_units": sorted({item["basis_unit"] for item in matches}),
        }
    selected = compatible[0] if compatible else matches[0]
    if not _state_compatible(request, selected):
        return {
            "status": "state_mismatch",
            "food_exists": True,
            "available_units": sorted({item["basis_unit"] for item in matches}),
        }
    if len(compatible) != 1:
        return {
            "status": "unit_mismatch",
            "food_exists": True,
            "available_units": sorted({item["basis_unit"] for item in matches}),
        }
    item = compatible[0]
    return {
        "status": "matched",
        "food_name": request["food_name"],
        "basis_amount": item["basis_amount"],
        "basis_unit": item["basis_unit"],
        "nutrition": dict(item["nutrition"]),
        "source": _source(item, document["version"]),
    }


def is_fixed_core_request(request):
    """Nutrition authority policy only; this does not classify message intent."""
    name = _key(request.get("food_name", ""))
    if any(term in name for term in ("無糖豆漿", "無加糖豆漿", "豆漿(無糖)")):
        return True
    if re.fullmatch(r"(?:熟|煮熟)?(?:五穀飯|五穀米飯)", name):
        return True
    if re.fullmatch(r"(?:熟|水煮|煮熟)?(?:白飯|白米飯)", name):
        return True
    modifiers = r"(?:生|熟|清蒸|蒸熟|蒸|烤|水煮|帶皮|去皮|黃肉|紅肉|紫肉)*"
    return bool(re.fullmatch(modifiers + r"(?:地瓜|甘藷|番薯|蕃薯)" + modifiers, name.replace("(", "").replace(")", "")))


def resolve_reference(request):
    """Return nutrients scaled to the requested same-unit amount, or ``None``.

    This is the legacy total-nutrition API.  ``find_reference_nutrition`` is the
    per-basis API used by the round-3 semantic pipeline.
    """

    reference = find_reference_nutrition(request)
    if not reference or reference.get("status") != "matched":
        return None
    amount = request["amount"]
    ratio = amount / reference["basis_amount"]
    nutrition = {
        key: None if value is None else value * ratio
        for key, value in reference["nutrition"].items()
    }
    state = reference["source"]["state"]
    label = reference["source"]["reference_label"]
    unit = reference["basis_unit"]
    return {
        "food_name": request["food_name"],
        "amount": amount,
        "unit": unit,
        "portion_assumption": f"{amount:g}{unit}；{state}；{label}",
        "nutrition": nutrition,
        "source": reference["source"],
    }
