"""统计 API 35→36 和 36→37 中，各 Package 新增 API 声明的数量。"""

import csv
from collections import Counter
from pathlib import Path

# 根据脚本位置寻找项目根目录，不依赖运行时的工作目录。
# 项目结构：
#   data/clean/api_events.csv
#   analysis/program/API_Added_Package.py
#   analysis/analysisData/API_Added_Package_All.csv
#   analysis/analysisData/API_Added_Package_Top10.csv
PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUT_FILE = PROJECT_ROOT / "data" / "clean" / "api_events.csv"
OUTPUT_DIR = PROJECT_ROOT / "analysis" / "analysisData"

PERIODS = {"35->36": "35-36", "36->37": "36-37"}
TOP_N = 10


def main():
    if not INPUT_FILE.is_file():
        raise FileNotFoundError(f"找不到输入数据：{INPUT_FILE}")

    # 一条符合条件的 Added 事件，按其 package 计数一次。
    counts = {period: Counter() for period in PERIODS}

    with INPUT_FILE.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"period", "change", "package", "count_structural"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"输入 CSV 缺少字段：{sorted(missing)}")

        for row in reader:
            period = row["period"].strip()
            change = row["change"].strip().lower()
            if period not in PERIODS or change != "added":
                continue
            if row["count_structural"].strip() != "1":
                continue

            package = row["package"].strip()
            if not package:
                raise ValueError(f"找到 Package 为空的新增事件：{row.get('api_key', '')}")
            counts[period][package] += 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    all_file = OUTPUT_DIR / "API_Added_Package_All.csv"
    top_file = OUTPUT_DIR / "API_Added_Package_Top10.csv"

    # 分别按每个版本区间的新增数量降序排列；相同数量按包名排序。
    sorted_packages = {
        period: sorted(package_counts.items(), key=lambda item: (-item[1], item[0]))
        for period, package_counts in counts.items()
    }

    with all_file.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["period", "package", "added_count"])
        for period, label in PERIODS.items():
            for package, n in sorted_packages[period]:
                writer.writerow([label, package, n])

    with top_file.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["period", "rank", "package", "added_count"])
        for period, label in PERIODS.items():
            for rank, (package, n) in enumerate(sorted_packages[period][:TOP_N], 1):
                writer.writerow([label, rank, package, n])

    print("新增 API Package 统计完成：")
    for period, label in PERIODS.items():
        total = sum(counts[period].values())
        print(f"  {label}: {total} 条 Added 事件，覆盖 {len(counts[period])} 个 Package")
    print(f"全部 Package：{all_file}")
    print(f"每期 Top {TOP_N}：{top_file}")


if __name__ == "__main__":
    main()
