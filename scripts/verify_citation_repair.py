"""Replay a saved draft against Ollama without modifying task data."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.evidence_validation import validate_claim
from app.llm import llm
from app.models import AnalysisState, GeneratedAnalysisDraft
from app.report_generation import compact_evidence, generate_report, resolve_citations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('run_id')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    with sqlite3.connect(settings.metadata_db.resolve().as_uri() + '?mode=ro', uri=True) as db:
        task_id, raw = db.execute('SELECT task_id,result_json FROM execution_runs WHERE id=?', (args.run_id,)).fetchone()
        evidence = {
            row[0]: SimpleNamespace(id=row[0], title=row[1], source=row[2], columns=json.loads(row[3]), rows=json.loads(row[4]))
            for row in db.execute('SELECT id,title,source,columns_json,rows_json FROM evidence WHERE run_id=?', (args.run_id,))
        }
    draft = GeneratedAnalysisDraft.model_validate_json(raw)
    state = AnalysisState(task_id=task_id, run_id=args.run_id, user_question='重新分析数据，并生成报告',
                          tool_results=[{'status': 'success', 'evidence_ids': list(evidence)}])
    def validate(value):
        with patch('app.evidence_validation.repository.get_evidence', side_effect=lambda task, eid: evidence.get(eid)):
            issues = validate_claim(state, 'summary', value.summary_evidence_refs, value.summary_evidence_pointers)
            for group in ('metrics', 'findings', 'insights'):
                for index, claim in enumerate(getattr(value, group)):
                    issues.extend(validate_claim(state, f'{group}[{index}]', claim.evidence_refs, claim.evidence_pointers))
            return [issue.model_dump(mode='json') for issue in issues]
    catalog = [compact_evidence(item) for item in evidence.values()]
    before = validate(draft)
    print(f'Initial errors: {len(before)}', flush=True)
    repaired = generate_report(llm, 'draft_writer', {
        'user_question': state.user_question, 'evidence_catalog': catalog,
        'previous_draft': draft.model_dump(mode='json'),
        'revision_feedback': {'structural_validation': {'issues': before}},
    }, GeneratedAnalysisDraft, thinking=False)
    repaired = GeneratedAnalysisDraft.model_validate(resolve_citations(repaired.model_dump(mode='json'), catalog))
    after = validate(repaired)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'before': before, 'after': after, 'draft': repaired.model_dump(mode='json')}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Remaining errors: {len(after)}; output: {args.output}', flush=True)


if __name__ == '__main__':
    main()
