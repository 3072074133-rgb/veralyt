"""Generate the synthetic financial workbook used by integration tests."""

from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font


OUTPUT = Path(__file__).parents[1] / "tests" / "fixtures" / "monthly_financial_report.xlsx"


SHEETS: dict[str, tuple[list[str], list[list[object]]]] = {
    "利润表": (
        ["项目", "本月金额", "上月金额"],
        [
            ["营业收入", 300000, 280000],
            ["营业成本", 120000, 115000],
            ["销售费用", 25000, 23000],
            ["管理费用", 45000, 41000],
            ["财务费用", 2000, 3000],
            ["所得税费用", 27000, 24500],
            ["净利润", 81000, 73500],
        ],
    ),
    "资产负债表": (
        ["项目", "期末余额", "期初余额"],
        [
            ["货币资金", 323000, 300000],
            ["应收账款", 140000, 120000],
            ["固定资产原值", 230000, 200000],
            ["应付账款", 70000, 80000],
            ["短期借款", 90000, 100000],
            ["实收资本", 250000, 250000],
            ["未分配利润", 70000, -11000],
        ],
    ),
    "现金流量表": (
        ["项目", "本月金额", "上月金额"],
        [
            ["经营活动现金流量净额", 65000, 59000],
            ["投资活动现金流量净额", -30000, -20000],
            ["筹资活动现金流量净额", -12000, 10000],
            ["现金及现金等价物净增加额", 23000, 49000],
        ],
    ),
    "费用明细": (
        ["费用项目", "归属科目", "金额", "凭证"],
        [
            ["市场推广", "销售费用", 15000, "FY-001"],
            ["销售差旅", "销售费用", 10000, "FY-002"],
            ["员工薪酬", "管理费用", 20000, "FY-003"],
            ["办公租金", "管理费用", 10000, "FY-004"],
            ["咨询服务", "管理费用", 8000, "FY-005"],
            ["办公费用", "管理费用", 7000, "FY-006"],
            ["借款利息", "财务费用", 2000, "FY-007"],
            ["所得税", "所得税费用", 27000, "FY-008"],
        ],
    ),
    "应收账款": (
        ["客户", "应收余额", "逾期余额"],
        [
            ["远航商贸", 50000, 50000],
            ["星河零售", 45000, 0],
            ["瑞丰科技", 45000, 0],
            ["合计", 140000, 50000],
        ],
    ),
    "应付账款": (
        ["供应商", "应付余额", "逾期余额"],
        [
            ["博远咨询", 30000, 30000],
            ["启明技术", 20000, 0],
            ["云联服务", 20000, 0],
            ["合计", 70000, 30000],
        ],
    ),
}


def generate(output: Path = OUTPUT) -> Path:
    workbook = Workbook()
    workbook.remove(workbook.active)

    for sheet_name, (headers, rows) in SHEETS.items():
        sheet = workbook.create_sheet(sheet_name)
        sheet.append([f"Veralyt 合成测试数据：{sheet_name}"])
        sheet.append(["期间：2026年8月（所有名称与金额均为虚构）"])
        sheet.append([])
        sheet.append(headers)
        for cell in sheet[4]:
            cell.font = Font(bold=True)
        for row in rows:
            sheet.append(row)
        for column in sheet.columns:
            width = max(len(str(cell.value or "")) for cell in column) + 2
            sheet.column_dimensions[column[0].column_letter].width = min(width, 36)

    output.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output)
    return output


if __name__ == "__main__":
    print(generate())
