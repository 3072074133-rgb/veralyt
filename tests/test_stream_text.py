from app.stream_text import text_stream


def test_stream_extracts_answer_without_internal_json():
    events = []
    stream = text_stream('intent_classifier', events.append)
    stream('{"route":"conversation","reply":"Hello')
    assert events[-1]['stream_text'] == 'Hello'
    stream('\\nworld","reason":"private"}')
    assert events[-1]['stream_text'] == 'Hello\nworld'


def test_stream_handles_split_unicode_escape():
    events = []
    stream = text_stream('result_reviewer', events.append)
    stream('{"answer":"\\u4f')
    stream('60\\u597d"}')
    assert events[-1]['stream_text'] == '你好'
    assert text_stream('analysis_planner', events.append) is None
