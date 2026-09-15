from app.llm import _report_output_schema, _ollama_schema
from app.models import GeneratedAnalysisDraft


def test_report_schema_omits_only_reconstructable_metadata():
    original = _ollama_schema(GeneratedAnalysisDraft.model_json_schema())
    schema = _report_output_schema(original)
    assert 'delivery' in original['properties']
    assert 'delivery' not in schema['properties']
    assert 'summary_evidence_refs' not in schema['properties']
    pointer = schema['$defs']['EvidencePointer']
    assert 'citation_id' in pointer['required']
    assert 'row_index' not in pointer['properties']
    assert {'formula', 'input_pointers', 'source_type'} <= pointer['properties'].keys()
    assert {'summary', 'metrics', 'findings', 'insights', 'charts'} <= schema['properties'].keys()
