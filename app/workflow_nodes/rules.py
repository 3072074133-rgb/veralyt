"""Pure request classification rules."""
from __future__ import annotations
import re

def normalize_intent_text(value: str) -> str:
    normalized = re.sub(r"[\s，。！？,.!?、；;：:]", "", value).casefold()
    return re.sub(r"^(请|麻烦|帮我)", "", normalized)

def is_retry_analysis_request(question: str) -> bool:
    normalized = normalize_intent_text(question)
    return bool(re.fullmatch(r"(?:(?:重新|再|继续)(?:分析|计算|统计|汇总)(?:一下|一次|一遍|数据|这份数据|当前数据)?|重做分析|(?:重新|再)跑(?:一下|一次|一遍)?)", normalized))

def is_generic_analysis_request(question: str) -> bool:
    normalized = normalize_intent_text(question)
    return bool(re.fullmatch(r"(?:分析|分析一下|帮我分析|看看数据|查看数据)", normalized))


def is_contextual_followup(question: str, has_previous_result: bool) -> bool:
    """Recognize short follow-ups that rely on the existing analysis context."""
    if not has_previous_result:
        return False
    normalized = normalize_intent_text(question)
    if not normalized or re.search(r"^(你好|您好|谢谢|感谢|再见|停止|取消|不用了)$", normalized):
        return False
    return len(normalized) >= 2


def has_explicit_query_action(question: str) -> bool:
    return bool(re.search(r"分析|计算|统计|汇总|筛选|排序|比较|对比|同比|环比|趋势|异常|对账|预测|查询|查找", question))
