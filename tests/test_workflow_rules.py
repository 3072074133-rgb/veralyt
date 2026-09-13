from app.workflow_nodes.rules import normalize_intent_text, is_retry_analysis_request, is_generic_analysis_request

def test_normalize_intent_text():
    assert normalize_intent_text(" please analyze ") == "pleaseanalyze"

def test_retry_analysis_request():
    assert is_retry_analysis_request("请重新分析一下")
    assert not is_retry_analysis_request("分析销售额")

def test_generic_analysis_request():
    assert is_generic_analysis_request("帮我分析")
    assert not is_generic_analysis_request("分析销售额")

