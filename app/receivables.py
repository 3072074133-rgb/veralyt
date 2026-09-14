"""Execute the overdue ranking tool selected by the model."""


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
