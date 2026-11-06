"""按 Android API 版本区间，统计新增 API 的类型数量。"""

import csv
from collections import Counter
from pathlib import Path

# 无论从哪个工作目录运行，都以本程序所在位置定位项目根目录。
# 项目结构：
#   data/clean/api_events.csv
#   analysis/program/API_Added_Type.py
#   analysis/analysisData/API_Added_Type.csv
PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_FILE = PROJECT_ROOT / "data" / "clean" / "api_events.csv"
OUTPUT_FILE = PROJECT_ROOT / "analysis" / "analysisData" / "API_Added_Type.csv"

# 原始 CSV 中的 period 值以及输出表格的显示方式。
PERIODS = {"35->36": "35-36", "36->37": "36-37"}

# 直接采用 api_events.csv 的 kind 原始取值。
API_KINDS = ("field", "method", "class", "ctor", "enum_constant")


def main():
    if not INPUT_FILE.is_file():
        raise FileNotFoundError(f"找不到输入文件：{INPUT_FILE}")

    counts = {period: Counter() for period in PERIODS}

    with INPUT_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required_columns = {"period", "change", "kind", "count_structural"}
        missing_columns = required_columns - set(reader.fieldnames or [])
        if missing_columns:
            raise ValueError(f"输入 CSV 缺少必要字段：{sorted(missing_columns)}")

        for row in reader:
            period = row["period"].strip()
            change = row["change"].strip().lower()
            kind = row["kind"].strip().lower()

            # 只分析两个版本区间内计入结构变化的 Added 事件。
            if period not in PERIODS or change != "added":
                continue
            if row["count_structural"].strip() != "1":
                continue

            # 防止数据出现新的 kind 而被漏算。
            if kind not in API_KINDS:
                raise ValueError(f"遇到未预期的新增 API 类型：{kind!r}")
            counts[period][kind] += 1

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["period", *API_KINDS, "total"])
        for period, label in PERIODS.items():
            values = [counts[period][kind] for kind in API_KINDS]
            writer.writerow([label, *values, sum(values)])

    print(f"新增 API 类型统计完成：{OUTPUT_FILE}")
    for period, label in PERIODS.items():
        print(f"{label}: {sum(counts[period].values())} 条新增 API 声明")


if __name__ == "__main__":
    main()
