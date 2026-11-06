"""统计 Android API 35→36、36→37 的整体变化事件数量。"""

import csv
from collections import Counter
from pathlib import Path

# 本文件放在 项目根目录/analysis/program/ 下。
# 根据本文件的位置找到项目根目录，避免依赖运行时所在的文件夹。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_FILE = PROJECT_ROOT / "data" / "clean" / "api_events.csv"
OUTPUT_FILE = PROJECT_ROOT / "analysis" / "analysisData" / "API_Overall_quantity.csv"

# 原始 CSV 中的版本名称 -> 输出 CSV 中的版本名称
PERIODS = {"35->36": "35-36", "36->37": "36-37"}
CHANGE_TYPES = ("added", "removed", "changed", "deprecated")


def main():
    if not INPUT_FILE.is_file():
        raise FileNotFoundError(f"找不到原始数据文件：{INPUT_FILE}")

    # 分别为两个版本区间建立计数器。
    counts = {period: Counter() for period in PERIODS}

    with INPUT_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            period = row["period"].strip()
            change = row["change"].strip().lower()

            # 只统计本次研究的两个版本区间、四种变化类型。
            if period not in counts or change not in CHANGE_TYPES:
                continue

            # 新增、移除、修改：只统计清洗后计入结构变化的事件。
            # 废弃（deprecated）：单独计数，不使用 count_structural 过滤。
            if change != "deprecated" and row["count_structural"].strip() != "1":
                continue

            counts[period][change] += 1

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    with OUTPUT_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["period", "added", "removed", "changed", "deprecated", "total"])

        for period, label in PERIODS.items():
            values = [counts[period][change] for change in CHANGE_TYPES]
            # total 是四类变化事件之和，不是去重后的 API 个数。
            writer.writerow([label, *values, sum(values)])

    print(f"统计完成，已生成：{OUTPUT_FILE}")


if __name__ == "__main__":
    main()
