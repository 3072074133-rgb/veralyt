"""Read reports without rewriting historical persisted content."""
from .models import AnalysisDraft, ReportBlock, ReportSection, ReportText


def report_sections(draft: AnalysisDraft) -> list[ReportSection]:
    if draft.report_schema_version == 2:
        return draft.sections
    sections = []
    if draft.insights:
        blocks = []
        for index, insight in enumerate(draft.insights):
            blocks.append(ReportBlock(
                id=f"legacy-insight-{index}", kind="paragraph",
                text="\n".join(filter(None, [insight.title, insight.conclusion, insight.significance,
                                            f"建议：{insight.action}" if insight.action else None])),
                evidence_refs=insight.evidence_refs, evidence_pointers=insight.evidence_pointers,
            ))
        sections.append(ReportSection(id="legacy-insights", title="关键结论", blocks=blocks))
    if draft.metrics:
        sections.append(ReportSection(id="legacy-metrics", title="关键指标", blocks=[
            ReportBlock(id="legacy-metric-group", kind="metrics", metrics=draft.metrics)]))
    if draft.charts:
        sections.append(ReportSection(id="legacy-charts", title="主要图表", blocks=[
            ReportBlock(id=f"legacy-chart-{index}", kind="chart", chart=chart)
            for index, chart in enumerate(draft.charts)]))
    if draft.findings:
        sections.append(ReportSection(id="legacy-findings", title="关键发现", blocks=[
            ReportBlock(id="legacy-finding-list", kind="list", items=[
                ReportText(text=f"{item.title}\n{item.detail}", evidence_refs=item.evidence_refs,
                           evidence_pointers=item.evidence_pointers) for item in draft.findings])]))
    return sections


def report_charts(draft: AnalysisDraft):
    return [block.chart for section in report_sections(draft) for block in section.blocks if block.chart]


def evidence_ids(value) -> set[str]:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, list):
        return set().union(*(evidence_ids(item) for item in value))
    if not isinstance(value, dict):
        return set()
    own = set(value.get("evidence_refs", [])) | set(value.get("summary_evidence_refs", []))
    own.update(value[key] for key in ("dataset_ref", "evidence_id") if value.get(key))
    return own | set().union(*(evidence_ids(item) for item in value.values()))


def report_text(draft: AnalysisDraft) -> str:
    parts = [draft.summary]
    for section in report_sections(draft):
        parts.append(section.title)
        for block in section.blocks:
            if block.text:
                parts.append(block.text)
            parts.extend(item.text for item in block.items)
    return "\n".join(parts)
