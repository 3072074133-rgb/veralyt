"""Repeatable isolated benchmark, usable against an archived baseline checkout."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack, nullcontext
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from unittest.mock import patch
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--real', action='store_true')
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--scenarios', nargs='+', default=['conversation', 'metric', 'multi_table', 'long_history', 'evidence', 'snapshots'])
    args = parser.parse_args()
    if args.repeats < 5:
        parser.error('at least five repetitions are required')
    directory = tempfile.TemporaryDirectory(prefix='analyse-benchmark-')
    os.environ['ANALYSE_AGENT_DATA_DIR'] = directory.name
    sys.path.insert(0, str(args.project.resolve()))
    from app import workflow as w
    from app.config import settings
    from app.financial_reports import query_financial_report
    from app.ingestion import ingest_file
    from app.models import AnalysisState, ConversationMemory, EvidenceRecord, IntentDecision, UploadedFile
    from app.repository import repository
    repository.initialize()
    samples = []
    original_connect = repository.connect
    original_chat = w.llm.client.chat
    original_loads = json.loads
    question_map = {'conversation': '你好', 'metric': '净利润率是多少', 'multi_table': '分析报表',
                    'long_history': '分析报表'}

    def seed():
        task = repository.create_task()
        file = UploadedFile(id=str(uuid.uuid4()), original_name=args.fixture.name,
                            size=args.fixture.stat().st_size, status='ready')
        repository.add_file(task, file, str(args.fixture.resolve()))
        datasets = ingest_file(task, file, args.fixture.resolve())
        run = repository.start_execution(task, '分析报表')
        evidence = query_financial_report(task, datasets, run)
        repository.finish_execution(run, 'completed')
        return task, datasets, evidence

    def save():
        groups = {}
        for scenario in args.scenarios:
            for condition in {sample['condition'] for sample in samples if sample['scenario'] == scenario}:
                selected = [s for s in samples if s['scenario'] == scenario and s['condition'] == condition]
                times = [s['elapsed_ms'] for s in selected]
                groups[f'{scenario}/{condition}'] = {
                    'count': len(times), 'median_ms': round(statistics.median(times), 2),
                    'min_ms': round(min(times), 2), 'max_ms': round(max(times), 2),
                    'median_model_calls': statistics.median(s['counters']['model_calls'] for s in selected),
                    'median_evidence_selects': statistics.median(s['counters']['evidence_selects'] for s in selected),
                    'median_json_input_chars': statistics.median(s['counters']['json_input_chars'] for s in selected),
                }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'project': str(args.project.resolve()), 'real_model': args.real,
            'model': settings.ollama_model, 'data_directory': directory.name, 'groups': groups,
            'samples': samples}, ensure_ascii=False, indent=2), encoding='utf-8')

    for scenario in args.scenarios:
        conditions = ['cold', 'warm'] if args.real and scenario in question_map else ['local']
        for condition in conditions:
            for repetition in range(args.repeats):
                task, datasets, evidence = seed()
                if scenario in {'evidence', 'snapshots'}:
                    # Unrelated history is deliberately much larger than the current result.
                    for index in range(100):
                        repository.add_evidence(EvidenceRecord(id=f'old_{uuid.uuid4().hex}', task_id=task,
                            title='historical', source='query_data', columns=['amount'],
                            rows=[{'amount': index}] * 200))
                if scenario == 'long_history':
                    repository.save_conversation_memory(task, ConversationMemory(task_goal='分析报表'), 0, expected_version=None)
                    for index in range(20):
                        repository.add_message(task, 'user' if index % 2 == 0 else 'assistant',
                            '请保留财务报表原始口径。' * 250)
                question = question_map.get(scenario, '分析报表')
                repository.add_message(task, 'user', question)
                run = repository.start_execution(task, question, status='queued')
                if condition == 'cold':
                    w.llm.client.generate(model=settings.ollama_model, keep_alive=0)
                elif condition == 'warm':
                    w.llm.client.generate(model=settings.ollama_model, prompt='', keep_alive='5m')
                counters = Counter(model_calls=0, evidence_selects=0, json_input_chars=0)
                def trace(sql):
                    lower = sql.lower()
                    if lower.lstrip().startswith('select') and 'from evidence' in lower:
                        counters['evidence_selects'] += 1
                def connect():
                    connection = original_connect()
                    connection.set_trace_callback(trace)
                    return connection
                def loads(value, *a, **kw):
                    counters['json_input_chars'] += len(value)
                    return original_loads(value, *a, **kw)
                def chat(**kwargs):
                    counters['model_calls'] += 1
                    return original_chat(**kwargs)
                def structured(prompt, payload, model, **kwargs):
                    counters['model_calls'] += 1
                    if prompt == 'conversation_summarizer':
                        return ConversationMemory(task_goal='分析报表')
                    if prompt == 'intent_classifier':
                        if scenario == 'conversation':
                            return IntentDecision(route='conversation', reply='你好')
                        if scenario == 'metric':
                            return IntentDecision(route='derived_metric', metric='净利润率')
                        return IntentDecision(route='analysis')
                    raise AssertionError(f'unexpected model call: {prompt}')
                started = time.perf_counter()
                with ExitStack() as stack:
                    stack.enter_context(patch.object(repository, 'connect', connect))
                    stack.enter_context(patch.object(json, 'loads', loads))
                    stack.enter_context(patch.object(w.llm.client, 'chat', chat) if args.real else patch.object(w.llm, 'structured', structured))
                    if scenario == 'evidence':
                        state = AnalysisState(task_id=task, run_id=run, user_question=question,
                            tool_results=[evidence.model_dump(mode='json')])
                        scope = repository.evidence_scope(task) if hasattr(repository, 'evidence_scope') else nullcontext()
                        with scope:
                            for limit in (12, 8, 4):
                                w._draft_context(state, row_limit=limit)
                    elif scenario == 'snapshots':
                        for _ in range(20):
                            repository.get_task(task)
                    else:
                        w.run_analysis(run)
                elapsed = (time.perf_counter() - started) * 1000
                record = repository.get_run_by_id(run)
                samples.append({'scenario': scenario, 'condition': condition, 'repetition': repetition + 1,
                    'elapsed_ms': round(elapsed, 2), 'counters': dict(counters), 'status': record.status,
                    'error': record.error})
                save()
                print(f'{scenario}/{condition} {repetition + 1}: {elapsed:.1f} ms, {record.status}', flush=True)
    w.graph.checkpointer.conn.close()
    directory.cleanup()


if __name__ == '__main__':
    main()
