import json
import re
import time


def text_stream(prompt_name, diagnostics):
    field = {'intent_classifier': 'reply', 'result_reviewer': 'answer', 'draft_writer': 'summary'}.get(prompt_name)
    if not field or diagnostics is None:
        return None
    content = ''
    sent = ''
    last = 0.0
    diagnostics({'stream_text': ''})

    def receive(delta):
        nonlocal content, sent, last
        content += delta
        match = re.search(r'"' + field + r'"\s*:\s*"', content)
        if not match:
            return
        fragment = content[match.end():]
        escaped = False
        end = len(fragment)
        for index, char in enumerate(fragment):
            if char == '"' and not escaped:
                end = index
                break
            escaped = char == '\\' and not escaped
        fragment = fragment[:end]
        # A transport chunk may end inside an escaped character or Unicode escape.
        for trim in range(min(6, len(fragment)) + 1):
            try:
                value = json.loads('"' + (fragment[:-trim] if trim else fragment) + '"')
                break
            except json.JSONDecodeError:
                value = ''
        if value != sent and (time.monotonic() - last >= .1 or end < len(content[match.end():])):
            diagnostics({'stream_text': value})
            sent, last = value, time.monotonic()
    return receive
