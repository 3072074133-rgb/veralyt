"""Replay the same saved report input, without changing the task."""
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.workflow import graph, _draft_context
from app.models import AnalysisState, GeneratedAnalysisDraft
from app.report_generation import generate_report, resolve_citations
from app.llm import llm
from app.evidence_validation import validate_claim


run_id = '4e3e6f91-e58d-4f0a-9d23-92ef4ff04a4a'
saved = next(item for item in graph.get_state_history({'configurable': {'thread_id': run_id}}) if 'draft' in item.next)
state = AnalysisState.model_validate(saved.values)
context = _draft_context(state)
stats = []
def diagnostics(value):
    if 'output_attempt' in value:
        item = value['output_attempt']
        stats.append({key: item.get(key) for key in ['attempt', 'prompt_eval_count', 'eval_count', 'configured_context_tokens', 'done_reason']})

started = time.perf_counter()
draft = generate_report(llm, 'draft_writer', context, GeneratedAnalysisDraft,
                        thinking=False, diagnostics=diagnostics, ignored_response_fields={'calculation_details'})
elapsed = time.perf_counter() - started
draft = GeneratedAnalysisDraft.model_validate(resolve_citations(draft.model_dump(mode='json'), context['evidence_catalog']))
issues = validate_claim(state, 'summary', draft.summary_evidence_refs, draft.summary_evidence_pointers)
for group in ['metrics', 'findings', 'insights']:
    for index, claim in enumerate(getattr(draft, group)):
        issues.extend(validate_claim(state, f'{group}[{index}]', claim.evidence_refs, claim.evidence_pointers))
result = {'run_id': run_id, 'duration_seconds': elapsed, 'requests': stats,
          'validation_issues': [issue.model_dump(mode='json') for issue in issues],
          'draft': draft.model_dump(mode='json')}
path = Path('data/verification/report-output-benchmark.json')
path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({key: value for key, value in result.items() if key != 'draft'}, ensure_ascii=True))
