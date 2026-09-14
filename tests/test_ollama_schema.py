from copy import deepcopy

from app.llm import _ollama_schema
from app.models import GeneratedAnalysisDraft


def test_report_schema_preserves_business_fields_and_required_properties():
    original = GeneratedAnalysisDraft.model_json_schema()
    snapshot = deepcopy(original)
    cleaned = _ollama_schema(original)

    def check(source, result):
        if isinstance(source, dict):
            if "properties" in source:
                assert result["properties"].keys() == source["properties"].keys()
                assert set(result.get("required", [])) <= result["properties"].keys()
            for key in ("properties", "$defs"):
                for name, schema in source.get(key, {}).items():
                    check(schema, result[key][name])

    check(original, cleaned)
    assert "title" in cleaned["$defs"]["Insight"]["properties"]
    assert "title" not in cleaned["$defs"]["Insight"]
    assert original == snapshot


def test_schema_keyword_names_are_valid_property_and_definition_names():
    names = ["title", "default", "minimum", "maximum", "minLength", "maxLength"]
    schema = {
        "title": "Annotation",
        "type": "object",
        "properties": {name: {"type": "string", "title": "Annotation"} for name in names},
        "required": names,
        "$defs": {"title": {"type": "string", "title": "Annotation"}},
    }
    cleaned = _ollama_schema(schema)
    assert list(cleaned["properties"]) == names
    assert cleaned["required"] == names
    assert cleaned["$defs"]["title"] == {"type": "string"}
    assert all(value == {"type": "string"} for value in cleaned["properties"].values())


def test_literal_values_are_not_schema_annotations():
    literal = {"title": "Business value", "minimum": 10}
    schema = {"const": literal, "enum": [literal]}
    assert _ollama_schema(schema) == schema
