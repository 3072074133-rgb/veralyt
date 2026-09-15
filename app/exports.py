from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Any

import matplotlib
import xlsxwriter
from jinja2 import Environment, select_autoescape

from .ingestion import task_dir
from .models import EvidenceRecord, TaskSnapshot
from .repository import repository
from .report_document import evidence_ids, report_sections, report_charts

matplotlib.use("Agg")
from matplotlib import pyplot as plt  # noqa: E402

from .chart_builder import ordered_chart_rows

REPORT_TEMPLATE = Environment(
    autoescape=select_autoescape(default=True, default_for_string=True),
).from_string("""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{{ title }}</title>
<style>
body{font-family:"Microsoft YaHei UI",sans-serif;color:#243238;margin:36px auto;max-width:1000px;line-height:1.65}
h1{font-size:24px}h2{font-size:16px;border-bottom:1px solid #dce5e8;padding-bottom:8px;margin-top:28px}
.summary{background:#e8f3f5;padding:16px;border-left:4px solid #28778b}.metrics{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid #dce5e8}
.metric{padding:12px;border-right:1px solid #dce5e8}.metric:last-child{border:0}.metric small{display:block;color:#6b7a80}.metric strong{font-size:20px}
table{width:100%;border-collapse:collapse;font-size:12px}th,td{padding:8px;border:1px solid #dce5e8;text-align:left}th{background:#f2f6f7}
img{max-width:100%;height:auto}.muted{color:#6b7a80}p,li{white-space:pre-wrap;overflow-wrap:anywhere}.table-wrap{overflow:auto}body{padding:0 16px;box-sizing:border-box}@media(max-width:600px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}@media print{body{margin:12mm}}
</style></head><body><h1>{{ title }}</h1><p class="muted">生成时间：{{ updated_at }}</p>
<div class="summary">{{ summary }}</div>
{% for section in sections or [] %}<section><h2>{{ section.title }}</h2>{% for block in section.blocks %}
{% if block.kind == 'paragraph' %}<p>{{ block.text }}</p>
{% elif block.kind == 'list' %}<ul>{% for item in block['items'] %}<li>{{ item.text }}{% if item.refs %}<small> [{{ item.refs|join('、') }}]</small>{% endif %}</li>{% endfor %}</ul>
{% elif block.kind == 'metrics' %}<div class="metrics">{% for m in block.metrics %}<div class="metric"><small>{{ m.label }}</small><strong>{{ m.value }}</strong><div>{{ m.change or '' }}</div><small>{{ m.refs|join('、') }}</small></div>{% endfor %}</div>
{% elif block.kind == 'table' %}<div class="table-wrap"><table><thead><tr>{% for c in block.columns %}<th>{{ c }}</th>{% endfor %}</tr></thead><tbody>{% for row in block.rows %}<tr>{% for c in block.columns %}<td>{{ row.get(c, '') }}</td>{% endfor %}</tr>{% endfor %}</tbody></table></div>
{% elif block.kind == 'chart' %}<figure>{% if block.image %}<img src="data:image/png;base64,{{ block.image }}" alt="{{ block.chart.title }}">{% else %}<p>图表无法生成，请查看证据。</p>{% endif %}<figcaption>{{ block.chart.title }}</figcaption></figure>{% endif %}
{% if block.refs %}<small class="muted">证据：{{ block.refs|join('、') }}</small>{% endif %}{% if block.error %}<p>{{ block.error }}</p>{% endif %}
{% endfor %}</section>{% endfor %}
{% if warnings %}<h2>风险提示</h2><ul>{% for w in warnings %}<li>{{ w }}</li>{% endfor %}</ul>{% endif %}
{% if calculations %}<h2>计算明细</h2><table><thead><tr><th>步骤</th><th>工具</th><th>状态</th><th>结果行数</th><th>证据</th></tr></thead><tbody>{% for item in calculations %}<tr><td>{{ item.title }}</td><td>{{ item.tool_name }}</td><td>{{ item.status }}</td><td>{{ item.row_count }}</td><td>{{ item.evidence_refs|join('、') }}</td></tr>{% endfor %}</tbody></table>{% endif %}
{% for item in evidence or [] %}<details><summary>证据明细：{{ item.id }} · {{ item.title }}</summary><div class="table-wrap"><table><thead><tr>{% for c in item.columns %}<th>{{ c }}</th>{% endfor %}</tr></thead><tbody>{% for row in item.rows %}<tr>{% for c in item.columns %}<td>{{ row.get(c, '') }}</td>{% endfor %}</tr>{% endfor %}</tbody></table></div></details>{% endfor %}
</body></html>""")


def export_excel(snapshot: TaskSnapshot) -> Path:
    if snapshot.result is None:
        raise ValueError("任务尚未生成分析结果")
    output = task_dir(snapshot.id) / "exports" / (snapshot.active_run_id or "legacy")
    output.mkdir(parents=True, exist_ok=True)
    path = output / "分析结果.xlsx"
    evidence = _referenced_evidence(snapshot)
    sections = _reading_sections(snapshot, evidence)
    with xlsxwriter.Workbook(path, {"strings_to_formulas": False, "strings_to_urls": False}) as workbook:
        workbook.set_properties({"title": snapshot.title, "subject": "数据分析结果"})
        header = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#28778B"})
        section = workbook.add_format({"bold": True, "font_color": "#243238", "bg_color": "#E8F3F5", "bottom": 1, "bottom_color": "#28778B"})
        normal = workbook.add_format({"font_color": "#243238", "text_wrap": True, "valign": "top"})
        muted = workbook.add_format({"font_color": "#6B7A80", "text_wrap": True})
        summary = workbook.add_worksheet("分析摘要")
        summary.set_column("A:A", 24)
        summary.set_column("B:B", 90)
        summary.set_column("C:C", 35)
        summary.merge_range("A1:B1", snapshot.result.title, header)
        summary.write("A3", "分析摘要", section)
        summary.write("B3", _excel_text(snapshot.result.summary), normal)
        row = 5
        for chapter in sections:
            summary.write(row, 0, _excel_text(chapter['title']), section)
            row += 1
            for block in chapter['blocks']:
                entries = []
                if block['kind'] == 'paragraph':
                    entries = [('', block['text'], block['refs'])]
                elif block['kind'] == 'list':
                    entries = [('', item['text'], item['refs']) for item in block['items']]
                elif block['kind'] == 'metrics':
                    entries = [(item['label'], f"{item['value']} {item.get('change') or ''}", item['refs'])
                               for item in block['metrics']]
                elif block['kind'] == 'table':
                    summary.write_row(row, 0, [_excel_text(c) for c in block['columns']], section)
                    row += 1
                    for values in block['rows']:
                        summary.write_row(row, 0, [_excel_value(values.get(c)) for c in block['columns']], normal)
                        row += 1
                    entries = [('证据', ', '.join(block['refs']), [])]
                elif block['kind'] == 'chart':
                    summary.write(row, 0, _excel_text(block['chart']['title']), normal)
                    if block.get('image'):
                        summary.insert_image(row + 1, 0, 'chart.png', {'image_data': io.BytesIO(base64.b64decode(block['image'])),
                                                                    'x_scale': 0.6, 'y_scale': 0.6})
                        row += 22
                    entries = [('图表证据', ', '.join(block['refs']), [])]
                if block.get('error'):
                    entries.append(('', block['error'], []))
                for label, text, refs in entries:
                    summary.write(row, 0, _excel_text(label), normal)
                    # Excel limits each cell to 32,767 characters; preserve long prose across rows.
                    chunks = [text[start:start + 30000] for start in range(0, len(text), 30000)] or ['']
                    for chunk in chunks:
                        summary.write(row, 1, _excel_text(chunk), normal)
                        summary.write(row, 2, ', '.join(refs), muted)
                        summary.set_row(row, min(409, max(30, (len(chunk) // 65 + chunk.count('\n') + 1) * 17)))
                        row += 1
                row += 1
            row += 1
        if snapshot.result.calculation_details:
            calculations = workbook.add_worksheet("计算明细")
            calculations.set_column('A:C', 45)
            calculations.write_row(0, 0, ['步骤', '工具 / 状态 / 行数 / 证据', 'SQL'], section)
            for detail_row, detail in enumerate(snapshot.result.calculation_details, 1):
                calculations.write(detail_row, 0, _excel_text(detail.title), normal)
                description = (
                    f"{detail.tool_name} / {detail.status} / {detail.row_count} 行 / "
                    f"{', '.join(detail.evidence_refs) or '无证据'}"
                )
                calculations.write(detail_row, 1, _excel_text(description), normal)
                calculations.write(detail_row, 2, _excel_text(detail.query), normal)
        row += 2
        summary.write(row, 0, "口径与风险", section)
        for note in [*snapshot.result.assumptions, *snapshot.result.warnings]:
            row += 1
            summary.write(row, 1, _excel_text(note), muted)
        for index, item in enumerate(evidence, start=1):
            sheet = workbook.add_worksheet(_sheet_name(f"证据{index}"))
            sheet.freeze_panes(1, 0)
            for col, name in enumerate(item.columns):
                sheet.write(0, col, _excel_text(name), header)
                sheet.set_column(col, col, min(max(len(name) + 4, 12), 28))
            for row_index, record in enumerate(item.rows, start=1):
                for col, name in enumerate(item.columns):
                    value = record.get(name)
                    sheet.write(row_index, col, _excel_value(value), normal if isinstance(value, str) else None)
        method = workbook.add_worksheet("口径说明")
        method.set_column("A:A", 18)
        method.set_column("B:B", 70)
        method.write_row("A1", ["项目", "说明"], header)
        method.write_row("A2", ["数据文件", "、".join(item.original_name for item in snapshot.files)], normal)
        method.write_row("A3", ["分析问题", next((m.content for m in reversed(snapshot.messages) if m.role == "user"), "")], normal)
        method.write_row("A4", ["证据数量", len(evidence)], normal)
    return path


def export_html(snapshot: TaskSnapshot) -> Path:
    if snapshot.result is None:
        raise ValueError("任务尚未生成分析结果")
    output = task_dir(snapshot.id) / "exports" / (snapshot.active_run_id or "legacy")
    output.mkdir(parents=True, exist_ok=True)
    path = output / "分析报告.html"
    evidence = _referenced_evidence(snapshot)
    path.write_text(
        REPORT_TEMPLATE.render(
            title=snapshot.result.title, summary=snapshot.result.summary,
            updated_at=snapshot.updated_at, metrics=snapshot.result.metrics,
            findings=snapshot.result.findings,
            warnings=[*snapshot.result.assumptions, *snapshot.result.warnings],
            evidence=evidence,
            sections=_reading_sections(snapshot, evidence),
            charts=_render_charts(snapshot, evidence),
            calculations=snapshot.result.calculation_details,
        ),
        encoding="utf-8",
    )
    return path


def _render_charts(snapshot: TaskSnapshot, evidence: list[EvidenceRecord]) -> list[dict[str, str]]:
    if snapshot.result is None:
        return []
    rendered: list[dict[str, str]] = []
    for spec in report_charts(snapshot.result):
        image = _render_chart(spec, evidence)
        if image:
            rendered.append({"title": spec.title, "image": image})
    return rendered


def _render_chart(spec: Any, evidence: list[EvidenceRecord]) -> str | None:
    data = next((item for item in evidence if item.id == spec.dataset_ref), None)
    if data is None or not data.rows or not spec.series:
        return None
    rows = ordered_chart_rows(spec, data.rows)
    categories = [str(row.get(spec.category_field, "")) for row in rows]
    try:
        series_values = [
            (series.name, [float(row.get(series.field, 0) or 0) for row in rows])
            for series in spec.series
        ]
    except (TypeError, ValueError):
        return None
    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    figure, axis = plt.subplots(figsize=(9, 3.4), dpi=140)
    colors = ["#28778b", "#55a06f", "#c58a2f", "#b64c46"]
    if spec.chart_type == "scatter" and spec.x_field and spec.y_field:
        try:
            x_values = [float(row.get(spec.x_field, 0) or 0) for row in rows]
            y_values = [float(row.get(spec.y_field, 0) or 0) for row in rows]
        except (TypeError, ValueError):
            plt.close(figure)
            return None
        axis.scatter(x_values, y_values, color=colors[0], alpha=0.75, s=24)
        axis.set_xlabel(spec.x_field)
        axis.set_ylabel(spec.y_field)
        if spec.label_field and len(rows) <= 30:
            for row, x_value, y_value in zip(rows, x_values, y_values):
                axis.annotate(str(row.get(spec.label_field, "")), (x_value, y_value), fontsize=7)
    elif spec.chart_type == "pie":
        axis.pie(series_values[0][1], labels=categories, autopct="%1.1f%%", colors=colors)
    elif spec.chart_type == "line":
        for index, (name, values) in enumerate(series_values):
            axis.plot(categories, values, color=colors[index % len(colors)], linewidth=2, marker="o", label=name)
        if len(series_values) > 1:
            axis.legend()
    elif spec.chart_type == "waterfall":
        values = series_values[0][1]
        running = 0.0
        bases = []
        for value in values:
            bases.append(running if value >= 0 else running + value)
            running += value
        axis.bar(categories, values, bottom=bases, color=["#27805c" if value >= 0 else "#b64c46" for value in values])
    elif spec.orientation == "horizontal":
        name, values = series_values[0]
        axis.barh(categories[::-1], values[::-1], color=colors[0], label=name)
    else:
        width = 0.8 / max(len(series_values), 1)
        positions = list(range(len(categories)))
        for index, (name, values) in enumerate(series_values):
            offsets = [position - 0.4 + width / 2 + index * width for position in positions]
            axis.bar(offsets, values, width=width, color=colors[index % len(colors)], label=name)
        axis.set_xticks(positions, categories)
        if len(series_values) > 1:
            axis.legend()
    axis.set_title(spec.title, loc="left", fontsize=12)
    axis.grid(axis="y", alpha=0.2)
    if spec.chart_type not in {"pie", "scatter"}:
        axis.tick_params(axis="x", labelrotation=35 if len(categories) > 6 else 0)
    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(figure)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _excel_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _excel_value(value: Any) -> Any:
    return _excel_text(value) if isinstance(value, str) else value


def _sheet_name(name: str) -> str:
    return name.replace("[", "(").replace("]", ")").replace("*", "_").replace("?", "_")[:31]


def _referenced_evidence(snapshot: TaskSnapshot) -> list[EvidenceRecord]:
    if snapshot.result is None:
        return []
    referenced = evidence_ids(snapshot.result)
    return [
        item for item in repository.list_evidence(snapshot.id)
        if item.id in referenced
    ]


def _reading_sections(snapshot: TaskSnapshot, evidence: list[EvidenceRecord]) -> list[dict]:
    if snapshot.result is None:
        return []
    lookup = {item.id: item for item in evidence}
    sections = []
    for chapter in report_sections(snapshot.result):
        payload = chapter.model_dump(mode='json')
        for block, source in zip(payload['blocks'], chapter.blocks, strict=True):
            block['refs'] = sorted(evidence_ids(source))
            for item in [*block['items'], *block['metrics']]:
                item['refs'] = sorted(evidence_ids(item))
            if source.kind == 'table':
                data = lookup.get(source.dataset_ref)
                block['columns'] = source.columns or (data.columns if data else [])
                block['rows'] = data.rows if data else []
                if data is None:
                    block['error'] = '引用的数据证据不可用。'
            if source.chart:
                block['image'] = _render_chart(source.chart, evidence)
        sections.append(payload)
    return sections
