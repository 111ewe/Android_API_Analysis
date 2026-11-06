"""统计各 Android API 版本区间新增 API 的 Feature Flag 标记情况。"""

import csv
from collections import Counter
from pathlib import Path

# 使用本程序所在位置寻找项目根目录，与运行命令时的工作目录无关。
# Android-API-Analysis/
# ├── data/clean/api_events.csv
# └── analysis/
#     ├── program/API_Added_Flagged.py
#     └── analysisData/API_Added_Flagged.csv
PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_FILE = PROJECT_ROOT / "data" / "clean" / "api_events.csv"
OUTPUT_FILE = PROJECT_ROOT / "analysis" / "analysisData" / "API_Added_Flagged.csv"

PERIODS = {"35->36": "35-36", "36->37": "36-37"}
FLAGS = ("yes", "no")


def main():
    if not INPUT_FILE.is_file():
        raise FileNotFoundError(f"找不到输入数据：{INPUT_FILE}")

    counts = {period: Counter() for period in PERIODS}

    with INPUT_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required_columns = {"period", "change", "flagged", "count_structural"}
        missing_columns = required_columns - set(reader.fieldnames or [])
        if missing_columns:
            raise ValueError(f"输入文件缺少字段：{sorted(missing_columns)}")

        for row in reader:
            period = row["period"].strip()
            change = row["change"].strip().lower()
            flagged = row["flagged"].strip().lower()

            # 只看新增事件；保持与前两张 Added 统计表一致的计数口径。
            if period not in PERIODS or change != "added":
                continue
            if row["count_structural"].strip() != "1":
                continue
            if flagged not in FLAGS:
                raise ValueError(f"出现未知的 flagged 值：{flagged!r}，请检查源数据")

            counts[period][flagged] += 1

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["period", "flagged_yes", "flagged_no", "total"])
        for period, label in PERIODS.items():
            yes, no = (counts[period][flag] for flag in FLAGS)
            writer.writerow([label, yes, no, yes + no])

        # 这行用于画合并两个版本区间的饼图。
        all_yes = sum(counts[period]["yes"] for period in PERIODS)
        all_no = sum(counts[period]["no"] for period in PERIODS)
        writer.writerow(["Total", all_yes, all_no, all_yes + all_no])

    print(f"Feature Flag 统计完成：{OUTPUT_FILE}")
    for period, label in PERIODS.items():
        yes = counts[period]["yes"]
        no = counts[period]["no"]
        print(f"{label}: flagged=yes {yes}, flagged=no {no}, total {yes + no}")


if __name__ == "__main__":
    main()
