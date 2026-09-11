from app.chart_builder import build_chart_spec, build_kpis, deduplicate_charts, ordered_chart_rows
from app.models import ChartSeries, ChartSpec, EvidenceRecord


def _evidence(columns, rows, evidence_id="ev_chart") -> EvidenceRecord:
    return EvidenceRecord(
        id=evidence_id,
        task_id="task",
        title="测试证据",
        source="query_data",
        columns=columns,
        rows=rows,
    )


def test_temporal_evidence_builds_sorted_line_chart() -> None:
    evidence = _evidence(
        ["期间", "收入"],
        [{"期间": "2026-10", "收入": "80"}, {"期间": "2026-2", "收入": "120"}],
    )

    spec = build_chart_spec(evidence, "查看收入趋势")

    assert spec is not None
    assert spec.chart_type == "line"
    assert spec.sort_order == "category_asc"
    assert [row["期间"] for row in ordered_chart_rows(spec, evidence.rows)] == ["2026-2", "2026-10"]


def test_large_ranking_uses_horizontal_bar_and_top_twenty() -> None:
    evidence = _evidence(
        ["部门", "金额"],
        [{"部门": f"部门{index}", "金额": str(index)} for index in range(25)],
    )

    spec = build_chart_spec(evidence, "部门金额排名")

    assert spec is not None
    assert spec.chart_type == "bar" and spec.orientation == "horizontal"
    rows = ordered_chart_rows(spec, evidence.rows)
    assert len(rows) == 20 and rows[0]["金额"] == "24"


def test_percentage_column_is_not_preferred_over_absolute_measure() -> None:
    evidence = _evidence(
        ["部门", "绝对金额贡献百分比", "金额"],
        [
            {"部门": "销售", "绝对金额贡献百分比": "60", "金额": "600"},
            {"部门": "研发", "绝对金额贡献百分比": "40", "金额": "400"},
        ],
    )

    spec = build_chart_spec(evidence, "查看部门构成")

    assert spec is not None
    assert spec.series[0].field == "金额"


def test_composition_with_negative_values_does_not_build_a_pie() -> None:
    evidence = _evidence(
        ["部门", "利润"],
        [{"部门": "销售", "利润": 100}, {"部门": "研发", "利润": -20}],
    )

    spec = build_chart_spec(evidence, "查看部门利润构成")

    assert spec is not None
    assert spec.chart_type == "bar"


def test_duplicate_chart_data_is_removed() -> None:
    evidence = _evidence(["部门", "金额"], [{"部门": "销售", "金额": "10"}])
    first = ChartSpec(
        id="one", title="图一", chart_type="bar", dataset_ref=evidence.id,
        category_field="部门", series=[ChartSeries(name="金额", field="金额")],
    )
    second = first.model_copy(update={"id": "two", "title": "图二"})

    assert [item.id for item in deduplicate_charts([first, second], {evidence.id: evidence})] == ["one"]


def test_scalar_evidence_becomes_kpis_without_chart() -> None:
    evidence = _evidence(
        ["收入合计", "平均值", "记录数"],
        [{"收入合计": "1200", "平均值": "600", "记录数": 2}],
    )

    assert build_chart_spec(evidence, "查看收入") is None
    metrics = build_kpis([evidence])
    assert [metric.label for metric in metrics] == ["收入合计", "平均值"]
    assert all(metric.change is None and metric.direction == "neutral" for metric in metrics)


def test_relationship_request_builds_deterministic_scatter() -> None:
    evidence = _evidence(
        ["客户", "收入", "利润"],
        [{"客户": f"客户{index}", "收入": index, "利润": index * 2} for index in range(250)],
    )

    spec = build_chart_spec(evidence, "查看收入与利润的相关关系")

    assert spec is not None and spec.chart_type == "scatter"
    assert spec.x_field == "收入" and spec.y_field == "利润" and spec.label_field == "客户"
    rows = ordered_chart_rows(spec, evidence.rows)
    assert len(rows) == 200 and rows[0]["客户"] == "客户0" and rows[-1]["客户"] == "客户249"
