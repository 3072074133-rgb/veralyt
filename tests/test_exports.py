from app.exports import REPORT_TEMPLATE, _excel_text


def test_report_template_escapes_untrusted_content() -> None:
    rendered = REPORT_TEMPLATE.render(
        title="<script>alert(1)</script>",
        summary="<img src=x onerror=alert(1)>",
        updated_at="2026-09-09",
        metrics=[],
        findings=[],
        warnings=[],
        evidence=None,
        charts=[],
        calculations=[],
    )
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert "<img src=x" not in rendered
    assert "&lt;img src=x onerror=alert(1)&gt;" in rendered


def test_excel_formula_text_is_neutralized() -> None:
    assert _excel_text("=1+1") == "'=1+1"
