import duckdb
import pytest

from app.analysis_tools import ToolError, _validate_sql, execute_sql
from app.config import settings
from app.ingestion import task_dir
from app.models import DatasetColumn, DatasetInfo
from app.repository import repository


def test_select_and_cte_from_allowed_tables() -> None:
    assert _validate_sql('SELECT SUM("收入") FROM "data_1"', {"data_1"}) == {"data_1"}
    assert _validate_sql(
        'WITH grouped AS (SELECT SUM("收入") total FROM "data_1") SELECT * FROM grouped',
        {"data_1"},
    ) == {"data_1"}
    assert _validate_sql("SELECT 'delete' AS label FROM data_1", {"data_1"}) == {"data_1"}


@pytest.mark.parametrize("sql", [
    "SELECT 999 AS revenue",
    "SELECT random() FROM data_1",
    "DELETE FROM data_1",
    "SELECT * FROM read_csv('outside.csv')",
    "SELECT * FROM other_table",
])
def test_sql_safety_boundary_rejects_invalid_access(sql: str) -> None:
    with pytest.raises(ToolError):
        _validate_sql(sql, {"data_1"})


def test_execute_sql_supports_model_generated_multi_table_query(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(repository, "db_path", tmp_path / "app.sqlite")
    repository.initialize()
    task_id = repository.create_task()
    work = task_dir(task_id) / "work"
    work.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(work / "analysis.duckdb")) as connection:
        connection.execute("CREATE TABLE orders(id INTEGER, customer_id INTEGER, amount DOUBLE)")
        connection.execute("INSERT INTO orders VALUES (1, 10, 100), (2, 20, 50)")
        connection.execute("CREATE TABLE customers(customer_id INTEGER, segment VARCHAR)")
        connection.execute("INSERT INTO customers VALUES (10, 'A'), (20, 'B')")
    orders = DatasetInfo(
        id="orders", file_id="file", table_name="orders", display_name="订单", row_count=2,
        columns=[DatasetColumn(name="customer_id", display_name="customer_id", data_type="Int64", null_count=0)],
    )
    customers = DatasetInfo(
        id="customers", file_id="file", table_name="customers", display_name="客户", row_count=2,
        columns=[DatasetColumn(name="customer_id", display_name="customer_id", data_type="Int64", null_count=0)],
    )

    result = execute_sql(
        task_id,
        [orders, customers],
        "SELECT c.segment AS segment, SUM(o.amount) AS total "
        "FROM orders o JOIN customers c ON o.customer_id=c.customer_id "
        "GROUP BY c.segment ORDER BY total DESC",
        "分群收入",
    )

    assert result.tool_name == "execute_sql"
    assert result.rows == [{"segment": "A", "total": "100"}, {"segment": "B", "total": "50"}]
    assert len(result.evidence_ids) == 1


def test_selected_dataset_scope_cannot_be_bypassed() -> None:
    with pytest.raises(ToolError, match="未授权"):
        _validate_sql("SELECT * FROM customers", {"orders"})
