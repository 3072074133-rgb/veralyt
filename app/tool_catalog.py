"""Planner-facing contracts for the executable analysis tools."""

from .financial_reports import FIELDS


TOOL_DESCRIPTIONS = [
    {
        "name": "profile_table",
        "display_name": "数据表字段画像",
        "required_fields": [],
        "inputs": "使用 dataset_id；未填写时使用 dataset_ids 第一张表。",
        "behavior": "返回已保存的字段名称、类型、单位、空值数和示例值，不重新计算业务指标。",
        "limitations": "不能代替金额汇总、比例计算或逾期排名。",
    },
    {
        "name": "query_data",
        "display_name": "通用结构化汇总、筛选与排序",
        "required_fields": "没有固定业务字段；维度、指标、筛选和关联字段必须在所选表中存在。",
        "inputs": "dataset_id 或非空 dataset_ids；主表为 dataset_id，未填写时为列表第一张，其余为可关联表。执行时由模型生成完整 QueryDecision。",
        "behavior": "按维度分组，对指标执行 sum/average/min/max/count，支持筛选、排序、行数限制。关联字段和连接方式由模型自主选择，无须预先确认关系；空结果正常返回。",
        "limitations": "order_by 仅为输出维度或指标别名，方向由 descending 指定。不会自动排除合计行、识别应收应付或计算逾期天数。",
        "examples": "供应商应付逾期汇总可按真实供应商字段分组、汇总真实逾期余额字段；若数据含合计行，由模型明确选择明细筛选条件。",
    },
    {
        "name": "query_overdue",
        "display_name": "应收账款客户逾期余额固定排名（仅客户）",
        "required_fields": ["客户名称", "逾期余额", "报表行类型"],
        "inputs": "dataset_id 或 dataset_ids 第一张表：含上述精确字段名的应收客户明细表。",
        "behavior": "仅取报表行类型=detail，按客户名称汇总逾期余额（DECIMAL(24,2)），仅保留汇总金额>0的客户，按金额降序及客户名称排序，并生成 DENSE_RANK 并列排名。输出客户名称、逾期金额、排名。",
        "limitations": "字段、筛选、聚合和排序均固定；不计算回款率，不依据到期日重算逾期，不支持供应商名称替代客户名称。不是通用逾期工具，也不是供应商应付账款工具。",
        "alternative": "供应商应付账款排名或不同字段、筛选、口径需求使用 query_data，由模型指定参数。",
    },
    {
        "name": "query_financial_report",
        "display_name": "固定模板财务报表项目提取与勾稽核对",
        "required_fields": {"common": ["来源行号", "报表行类型"], "by_sheet_name": FIELDS},
        "inputs": "dataset_id 或非空 dataset_ids，接收两者合并去重的完整表列表；工作表名（无来源信息时用显示名）须匹配 by_sheet_name，每种报表限一张。第一列作为项目名称。",
        "behavior": "展开各模板预设金额列，排除 note 行和空金额，输出报表、项目、口径、金额、来源行号、类型；明细表按 detail 行汇总并与合计核对，按所选报表组合执行固定财务勾稽。",
        "limitations": "不是任意 Excel 分析工具。固定核对所需项目（如资产总计、负债和所有者权益总计、利润总额、所得税费用、净利润、合计）须存在且唯一、金额有效；缺项会报错。应付账款模板可提取和核对，但不生成供应商排名或付款率。",
        "alternative": "自定义列名、非标准模板或自选汇总排名使用 query_data。",
    },
    {
        "name": "query_department_profit",
        "display_name": "英文分段部门报表净利润提取",
        "required_fields": ["Department", "Sum"],
        "inputs": "dataset_id 或 dataset_ids 第一张表：英文分段层级报表。",
        "behavior": "按原始行顺序识别 Department 中含 Revenue/Income 后接左括号的部门段标题，提取该段内标签为 Net Profit 的 Sum 数值，按净利润降序输出部门、净利润。",
        "limitations": "不适用于普通部门流水表或只有中文部门/利润列的表；不重新计算收入减成本。",
        "alternative": "普通部门分组汇总使用 query_data。",
    },
]
