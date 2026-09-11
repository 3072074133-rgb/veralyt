"""Answer overdue rankings without fabricating missing invoice-age inputs."""
from decimal import Decimal
import re

from .models import AnalysisDraft, EvidencePointer, Finding


def is_overdue_ranking(question):
    return '应收账款' in question and '逾期' in question and bool(re.search('最高|最多|排名', question))


def query_overdue(task_id, dataset, run_id):
    from .analysis_tools import query_data
    table = '"' + dataset.table_name.replace('"', '""') + '"'
    sql = f'''WITH clients AS (
        SELECT "客户名称", SUM(TRY_CAST("逾期余额" AS DECIMAL(24,2))) AS "逾期金额"
        FROM {table} WHERE "报表行类型" = 'detail' GROUP BY "客户名称"
    ) SELECT *, DENSE_RANK() OVER (ORDER BY "逾期金额" DESC) AS "排名"
      FROM clients WHERE "逾期金额" > 0 ORDER BY "逾期金额" DESC, "客户名称"'''
    result = query_data(task_id, [dataset], sql, '应收客户逾期金额排名', run_id)
    result.arguments['strategy'] = 'overdue_ranking'
    return result


def overdue_draft(result):
    eid = result['evidence_ids'][0]
    findings = []
    for index, row in enumerate(result['rows']):
        if Decimal(str(row['排名'])) != 1:
            continue
        findings.append(Finding(title='逾期金额最高客户',
            detail=f"{row['客户名称']}的逾期金额为{row['逾期金额']}。",
            evidence_refs=[eid], evidence_pointers=[EvidencePointer(evidence_id=eid,
                row_index=index, field=field, raw_value=str(row[field])) for field in ['客户名称', '逾期金额']]))
    return AnalysisDraft(title='应收账款逾期分析',
        summary='已按原表逾期余额汇总客户排名，最高金额客户见下方结果。' if findings else '原表未发现逾期余额为正的客户。',
        summary_evidence_refs=[eid], findings=findings,
        warnings=['账龄汇总尚未完成：当前表缺少逐笔应收形成日期。请补充形成日期和统计截止日；若要计算逾期天数，请明确截止日，不能将到期日直接当作应收形成日期。'],
        assumptions=['逾期金额沿用原表口径，未按今天重新计算；排除合计及说明行，并保留并列最高客户。'])
