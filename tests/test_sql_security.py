import pytest

from app import analysis_tools
from app.analysis_tools import ToolError, _validate_sql, query_from_spec
from app.models import DatasetColumn, DatasetInfo, QueryJoin, QueryMeasure, QuerySpec, ToolExecutionResult


def test_select_from_allowed_table() -> None:
    _validate_sql('SELECT SUM("收入") FROM "data_1"', {"data_1"})


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
            measures=[QueryMeasure(field="收入", aggregation="sum")],
            filters=[{"field": "部门", "operator": "eq", "value": "A' OR 1=1 --"}],
        ),
    )
    assert "'A'' OR 1=1 --'" in captured["sql"]
    with pytest.raises(ToolError, match="指标"):
        query_from_spec(
            "task", [dataset],
            QuerySpec(dataset_id="dataset", measures=[QueryMeasure(field="不存在")]),
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
            dataset_id="left", dimensions=["right::等级"], measures=[QueryMeasure(field="left::客户ID", aggregation="count")],
            joins=[QueryJoin(right_dataset_id="right", left_field="客户ID", right_field="客户ID")],
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
