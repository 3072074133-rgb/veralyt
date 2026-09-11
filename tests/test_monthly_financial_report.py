from pathlib import Path
import uuid

import pytest

from app.config import settings
from app.ingestion import _read_xlsx, ingest_file
from app.models import UploadedFile, AnalysisState
from app.repository import repository
from app.evidence_validation import numeric_tokens

FIXTURE = Path(__file__).parent / 'fixtures' / 'monthly_financial_report.xlsx'


def test_statement_headers_and_original_rows():
    result = _read_xlsx(FIXTURE)
    assert len(result.regions) == 6
    assert all(r.header_start_row == r.header_end_row == 4 for r in result.regions)
    expense = next(r.frame for r in result.regions if r.sheet_name == '费用明细')
    assert expense.filter(expense['报表行类型'] == 'detail').height == 7
    assert expense.filter(expense['费用项目'] == '借款利息')['来源行号'][0] == 11
    balance = next(r.frame for r in result.regions if r.sheet_name == '资产负债表')
    assert {'短期借款', '实收资本', '未分配利润'} <= set(balance['项目'])


def test_numeric_column_does_not_corrupt_percent():
    assert numeric_tokens('贡献为100%，金额5000', ignored_terms={'0'}) == ['100%', '5000']


def test_unchanged_validation_stops_retry(monkeypatch):
    from types import SimpleNamespace
    from app.workflow import reflect_node, route_reflection
    monkeypatch.setattr(repository, 'update_task', lambda *args, **kwargs: None)
    monkeypatch.setattr('app.workflow.begin_node', lambda *args: SimpleNamespace(
        complete=lambda output: output, fail=lambda error: pytest.fail(str(error))))
    state = AnalysisState(task_id='task', run_id='run', user_question='分析报表', revision_round=1,
        validation={'passed': False, 'issues': [{'code': 'unsupported_cell_value', 'message': '金额无证据',
                                                'severity': 'error', 'target': 'summary'}]},
        reflection={'verdict': 'revise', 'route': 'rewrite', 'reason': '需复核',
                    'issues': [{'problem': '金额无证据'}]})
    output = reflect_node(state)
    state.reflection = output['reflection']
    assert route_reflection(state) == 'finish'


def test_native_table_header_wins_over_numeric_business_row(tmp_path):
    from openpyxl import Workbook
    from openpyxl.worksheet.table import Table
    path = tmp_path / 'native.xlsx'
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(['费用项目', '归属科目', '金额', '凭证'])
    sheet.append(['租金', '管理费用', 5000, 'FY-001'])
    sheet.append(['利息', '财务费用', 2000, 'FY-002'])
    sheet.add_table(Table(displayName='Expenses', ref='A1:D3'))
    workbook.save(path)
    region = _read_xlsx(path).regions[0]
    assert region.header_end_row == 1
    assert region.frame.height == 2


@pytest.fixture
def imported_report(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    monkeypatch.setattr(repository, 'db_path', tmp_path / 'app.sqlite')
    repository.initialize()
    task_id = repository.create_task()
    file = UploadedFile(id=str(uuid.uuid4()), original_name=FIXTURE.name,
                        size=FIXTURE.stat().st_size, status='ready')
    repository.add_file(task_id, file, str(FIXTURE))
    datasets = ingest_file(task_id, file, FIXTURE)
    yield task_id, datasets


def test_financial_workflow_end_to_end(imported_report, monkeypatch):
    from app.models import IntentDecision
    monkeypatch.setattr('app.workflow.llm.structured', lambda *args, **kwargs: IntentDecision(route='analysis'))
    from app.workflow import build_graph
    task_id, datasets = imported_report
    run = repository.start_execution(task_id, '分析报表')
    graph = build_graph()
    result = graph.invoke(AnalysisState(task_id=task_id, run_id=run, user_question='分析报表',
                          datasets=[d.model_dump(mode='json') for d in datasets]),
                          config={'configurable': {'thread_id': run}})
    assert result['final_status'] in {'completed', 'completed_with_warnings'}, result.get('error')
    assert result['validation']['passed'], result['validation']
    assert result['revision_round'] == 1
    assert len(result['plan']['steps'][0]['dataset_ids']) == 5
    rows = result['tool_results'][0]['rows']
    def amount(sheet, item, field):
        return float(next(r['金额'] for r in rows if (r['报表'], r['项目'], r['口径']) == (sheet, item, field)))
    assert amount('利润表', '营业收入', '本月金额') == 300000
    assert amount('利润表', '营业成本', '本月金额') == 120000
    assert amount('利润表', '净利润', '本月金额') == 81000
    assert amount('资产负债表', '资产总计', '月末余额') == 638000
    assert amount('现金流量表', '月末现金及现金等价物余额', '本月金额') == 323000
    assert amount('应收账款', '明细汇总', '月初余额') == 120000
    assert amount('应付账款', '明细汇总', '月初余额') == 80000
    assert all(float(r['金额']) == 0 for r in rows if r['报表'] == '勾稽核对')
    assert any('月末现金' in m['label'] for m in result['draft']['metrics'])


def test_false_claim_is_still_rejected(imported_report):
    from app.financial_reports import query_financial_report, financial_draft
    from app.evidence_validation import validate_claim
    task_id, datasets = imported_report
    result = query_financial_report(task_id, datasets)
    draft = financial_draft(result.model_dump(mode='json'))
    metric = draft.metrics[0]
    state = AnalysisState(task_id=task_id, run_id='test', user_question='分析报表',
                          tool_results=[result.model_dump(mode='json')])
    assert validate_claim(state, 'metric', '金额99999999', metric.evidence_refs,
                          metric.evidence_pointers, require_evidence=True)


def test_inconsistent_source_is_reported(imported_report):
    import duckdb
    from app.financial_reports import query_financial_report
    from app.ingestion import task_dir
    task_id, datasets = imported_report
    ar = next(d for d in datasets if d.display_name == '应收账款')
    with duckdb.connect(str(task_dir(task_id) / 'work' / 'analysis.duckdb')) as connection:
        connection.execute(f'UPDATE "{ar.table_name}" SET "月初余额"=50001 WHERE "来源行号"=5')
    result = query_financial_report(task_id, datasets)
    assert any('明细与合计' in warning for warning in result.warnings)
    assert any('与主表' in warning for warning in result.warnings)


def test_generic_group_count_has_real_evidence(imported_report):
    from app.analysis_tools import query_data
    from app.workflow import _generic_overview_draft, _finalize_draft, validate_node
    task_id, datasets = imported_report
    result = query_data(task_id, datasets, f'SELECT \'A\' AS 类别, 100 AS 金额, '
                        f'100 AS 贡献百分比, 1 AS 结果分组数 FROM "{datasets[0].table_name}" LIMIT 1')
    run = repository.start_execution(task_id, '分析报表')
    state = AnalysisState(task_id=task_id, run_id=run, user_question='分析报表',
                          tool_results=[result.model_dump(mode='json')])
    draft = _finalize_draft(state, _generic_overview_draft(state))
    state.draft = draft.model_dump(mode='json')
    assert validate_node(state)['validation']['passed']
