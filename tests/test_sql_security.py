import pytest

from app import analysis_tools
from app.analysis_tools import ToolError, _validate_sql, query_from_spec
from app.models import DatasetColumn, DatasetInfo, QueryJoin, QueryMeasure, QuerySpec, ToolExecutionResult


def test_select_from_allowed_table() -> None:
    _validate_sql('SELECT SUM("收入") FROM "data_1"', {"data_1"})


def test_missing_column_feedback_locates_real_row_without_executing_query(tmp_path, monkeypatch):
    import duckdb

    monkeypatch.setattr(analysis_tools.settings, "data_dir", tmp_path)
    work = tmp_path / "tasks" / "task" / "work"
    work.mkdir(parents=True)
    with duckdb.connect(str(work / "analysis.duckdb")) as connection:
        connection.execute('CREATE TABLE balance AS SELECT ? AS "项目", 187000 AS "月末余额"', ["负债合计"])
    table = DatasetInfo(id="balance", file_id="f", table_name="balance", display_name="资产负债表",
                        row_count=1, columns=[
                            DatasetColumn(name="项目", display_name="项目", data_type="String", null_count=0),
                            DatasetColumn(name="月末余额", display_name="月末余额", data_type="Int64", null_count=0),
                        ])
    spec = QuerySpec(dataset_id="balance", measures=[
        QueryMeasure(field="balance::负债合计", aggregation="sum", alias="负债")])
    original = spec.model_dump()
    monkeypatch.setattr(analysis_tools, "query_data", lambda *args: pytest.fail("Invalid query executed"))
    with pytest.raises(ToolError) as caught:
        query_from_spec("task", [table], spec)
    message = str(caught.value)
    assert '"matched_column": "项目"' in message
    assert '"field": "balance::项目", "operator": "eq", "value": "负债合计"' in message
    assert '"月末余额": 187000' in message
    assert spec.model_dump() == original
    assert analysis_tools._row_value_feedback("task", table, "负债") == ""
    assert analysis_tools._row_value_feedback("task", table, "' OR 1=1 --") == ""


def test_all_field_errors_are_reported_with_only_related_catalog():
    table = DatasetInfo(id='balance', file_id='f', table_name='balance', display_name='资产负债表', row_count=1,
                        columns=[DatasetColumn(name='项目', display_name='项目', data_type='String', null_count=0)])
    unrelated = table.model_copy(update={'id': 'unrelated', 'display_name': '不相关报表'})
    spec = QuerySpec(dataset_id='balance', dimensions=['资产负债表::项目'],
                     measures=[QueryMeasure(field='资产负债表::负债合计', aggregation='sum', alias='total')],
                     filters=[{'field': '缺失筛选列', 'operator': 'eq', 'value': 'x'}])
    original = spec.model_dump()
    with pytest.raises(ToolError) as caught:
        query_from_spec('task', [table, unrelated], spec)
    message = str(caught.value)
    assert all(path in message for path in ['dimensions[0]', 'measures[0].field', 'filters[0].field'])
    assert "不存在字段 '负债合计'" in message
    assert '不相关报表' not in message
    assert '单表查询可以直接填写字段名' in message
    assert spec.model_dump() == original


@pytest.mark.parametrize('joined,order_by,descending,expected', [
    (False, 'name', False, ['a', 'b']),
    (False, 'left::name', True, ['b', 'a']),
    (False, 'total', True, ['a', 'b']),
    (True, 'right::name', False, ['b', 'a']),
    (True, 'left::name', False, ['a', 'b']),
    (True, 'total', True, ['a', 'b']),
])
def test_sort_translation_executes_without_changing_spec(monkeypatch, joined, order_by, descending, expected):
    import duckdb
    def dataset(identifier):
        return DatasetInfo(id=identifier, file_id='f', table_name=identifier + '_table',
                           display_name=identifier, row_count=2, columns=[
                               DatasetColumn(name=name, display_name=name, data_type='String', null_count=0)
                               for name in ['id', 'name', 'amount']])
    tables = [dataset('left'), dataset('right')]
    spec = QuerySpec(dataset_id='left',
                     dimensions=['left::name', 'right::name'] if joined else [order_by if order_by != 'total' else 'name'],
                     measures=[QueryMeasure(field='left::amount', aggregation='sum', alias='total')],
                     joins=[QueryJoin(right_dataset_id='right', left_field='id', right_field='id', join_type='inner')] if joined else [],
                     order_by=order_by, descending=descending)
    original = spec.model_dump()
    def execute(task_id, datasets, sql, title, run_id):
        with duckdb.connect() as connection:
            connection.execute("CREATE TABLE left_table AS SELECT * FROM (VALUES (1, 'a', 20), (2, 'b', 10)) t(id,name,amount)")
            connection.execute("CREATE TABLE right_table AS SELECT * FROM (VALUES (1, 'z', 0), (2, 'y', 0)) t(id,name,amount)")
            rows = connection.execute(sql).fetchall()
        assert [row[0] for row in rows] == expected
        return ToolExecutionResult(result_id='r', tool_name='query_data', status='success', summary='ok')
    monkeypatch.setattr(analysis_tools, 'query_data', execute)
    query_from_spec('task', tables, spec)
    assert spec.model_dump() == original


def test_unknown_table_label_feedback_preserves_specific_cause():
    table = DatasetInfo(id='cash-id', file_id='f', table_name='cash', display_name='现金流量表',
                        row_count=1, columns=[DatasetColumn(name='项目', display_name='项目', data_type='String', null_count=0)])
    with pytest.raises(ToolError) as error:
        query_from_spec('task', [table], QuerySpec(dataset_id='cash-id', dimensions=['现金流量表::项目']))
    text = str(error.value)
    assert '无法识别表标识' in text
    assert 'cash-id' in text and '现金流量表' in text
    assert '数据集ID::字段名' in text


def test_unconfirmed_different_business_fields_join_returns_empty(monkeypatch):
    import duckdb
    left = DatasetInfo(id='cash', file_id='f', table_name='cash', display_name='cash', row_count=1,
                       columns=[DatasetColumn(name='项目', display_name='项目', data_type='String', null_count=0)])
    right = DatasetInfo(id='clients', file_id='f', table_name='clients', display_name='clients', row_count=1,
                        columns=[DatasetColumn(name='客户名称', display_name='客户名称', data_type='String', null_count=0)])
    def execute(task_id, datasets, sql, title, run_id):
        with duckdb.connect() as connection:
            connection.execute('CREATE TABLE cash AS SELECT ? AS 项目', ['经营现金流'])
            connection.execute('CREATE TABLE clients AS SELECT ? AS 客户名称', ['客户A'])
            rows = connection.execute(sql).fetchall()
        assert 'INNER JOIN' in sql
        assert rows == []
        return ToolExecutionResult(result_id='r', tool_name='query_data', status='success', summary='empty', rows=[])
    monkeypatch.setattr(analysis_tools, 'query_data', execute)
    result = query_from_spec('task', [left, right], QuerySpec(dataset_id='cash', dimensions=['cash::项目'],
        joins=[QueryJoin(right_dataset_id='clients', left_field='项目', right_field='客户名称', join_type='inner')]))
    assert result.status == 'success' and result.rows == []


def test_sort_error_explains_exact_fields_without_rewriting(monkeypatch):
    dataset = DatasetInfo(
        id="dataset", file_id="file", table_name="data_1", display_name="receivables", row_count=1,
        columns=[DatasetColumn(name=name, display_name=name, data_type="String", null_count=0)
                 for name in ["客户名称", "逾期余额"]],
    )
    spec = QuerySpec(dataset_id="dataset", dimensions=["客户名称"],
                     measures=[QueryMeasure(field="逾期余额", aggregation="sum", alias="总逾期金额")],
                     order_by="总逾期金额 DESC", descending=True)
    def unexpected_query(*args):
        pytest.fail("Invalid sort must not execute SQL")
    monkeypatch.setattr(analysis_tools, "query_data", unexpected_query)
    with pytest.raises(ToolError) as error:
        query_from_spec("task", [dataset], spec)
    assert "order_by='总逾期金额 DESC'" in str(error.value)
    assert "['客户名称', '总逾期金额']" in str(error.value)
    assert "descending=true" in str(error.value)
    assert spec.order_by == "总逾期金额 DESC"


def test_cte_from_allowed_table() -> None:
    _validate_sql('WITH grouped AS (SELECT SUM("收入") AS total FROM "data_1") SELECT * FROM grouped', {"data_1"})


def test_query_requires_physical_task_table() -> None:
    with pytest.raises(ToolError, match="必须引用"):
        _validate_sql("SELECT 999 AS revenue", {"data_1"})


def test_nondeterministic_query_is_rejected() -> None:
    with pytest.raises(ToolError, match="禁止"):
        _validate_sql("SELECT random() FROM data_1", {"data_1"})


def test_structured_query_escapes_values_and_rejects_unknown_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    dataset = DatasetInfo(
        id="dataset", file_id="file", table_name="data_1", display_name="收入",
        row_count=1,
        columns=[
            DatasetColumn(name="部门", display_name="部门", data_type="String", null_count=0),
            DatasetColumn(name="收入", display_name="收入", data_type="Float64", null_count=0),
        ],
    )
    captured: dict[str, str] = {}

    def fake_query(task_id, datasets, sql, title, run_id):
        captured["sql"] = sql
        return ToolExecutionResult(result_id="r", tool_name="query_data", status="success", summary=title)

    monkeypatch.setattr(analysis_tools, "query_data", fake_query)
    query_from_spec(
        "task", [dataset],
        QuerySpec(
            dataset_id="dataset",
            dimensions=["部门"],
            measures=[QueryMeasure(field="收入", aggregation="sum", alias="收入合计")],
            filters=[{"field": "部门", "operator": "eq", "value": "A' OR 1=1 --"}],
        ),
    )
    assert "'A'' OR 1=1 --'" in captured["sql"]
    with pytest.raises(ToolError, match="指标"):
        query_from_spec(
            "task", [dataset],
            QuerySpec(dataset_id="dataset", measures=[QueryMeasure(field="不存在", aggregation="sum", alias="不存在合计")]),
        )


def test_structured_query_supports_confirmed_multi_table_join(monkeypatch: pytest.MonkeyPatch) -> None:
    left = DatasetInfo(
        id="left", file_id="file", table_name="left_table", display_name="订单",
        row_count=1, columns=[DatasetColumn(name="客户ID", display_name="客户ID", data_type="String", null_count=0)],
    )
    right = DatasetInfo(
        id="right", file_id="file", table_name="right_table", display_name="客户",
        row_count=1, columns=[DatasetColumn(name="客户ID", display_name="客户ID", data_type="String", null_count=0), DatasetColumn(name="等级", display_name="等级", data_type="String", null_count=0)],
    )
    captured: dict[str, str] = {}
    def fake_query(_task, _datasets, sql, _title, _run):
        captured["sql"] = sql
        return ToolExecutionResult(result_id="r", tool_name="query_data", status="success", summary="ok")
    monkeypatch.setattr(analysis_tools, "query_data", fake_query)
    query_from_spec(
        "task", [left, right],
        QuerySpec(
            dataset_id="left", dimensions=["right::等级"], measures=[QueryMeasure(field="left::客户ID", aggregation="count", alias="客户数")],
            joins=[QueryJoin(right_dataset_id="right", left_field="客户ID", right_field="客户ID", join_type="left")],
        ),
    )
    assert "LEFT JOIN" in captured["sql"] and '"right_table"' in captured["sql"]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE data_1",
        "SELECT * FROM secret_table",
        "ATTACH 'other.db' AS other",
        "COPY data_1 TO 'out.csv'",
        "SELECT http_get('https://example.com/private')",
        "SELECT * FROM read_csv('C:/secret.csv')",
        "SELECT * FROM data_1; SELECT * FROM data_1",
    ],
)
def test_dangerous_sql_is_rejected(sql: str) -> None:
    with pytest.raises(ToolError):
        _validate_sql(sql, {"data_1"})
