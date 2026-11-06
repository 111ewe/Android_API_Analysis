# Added & Changed｜Android API 演化统计工作说明

> **工作范围：** Android API 35 → 36、36 → 37 的 **Added（新增）** 和 **Changed（修改）**。  
> **本文件用途：** 说明本部分做了什么、程序如何运行、结果在哪里；详细数据和结论见单独的分析报告。

## 1. 数据从哪里来

前一位组员已完成 API 数据抓取、清洗和版本比较。本部分**不重复抓取**，主要读取：

`data/clean/api_events.csv`

它是一张 **API 变化记录表**：每一行记录某个 API 在相邻版本间的一次变化。统计时主要使用 `period`（版本区间）、`change`（变化类型）、`kind`（声明类型）、`package`（所属包）、`flagged`（功能开关标记）和 `count_structural`（是否计入结构变化统计）。

本部分统计 Added 时保留 `count_structural = 1` 的记录；Changed 也按这一条件核对。整体汇总表还包含 Removed 和 Deprecated，但这两类的进一步分析由另一位组员负责。

## 2. 文件放在哪里

```text
项目根目录/
├── data/clean/
│   └── api_events.csv           # 输入数据
└── analysis/
    ├── program/                 # Python 统计与绘图程序
    ├── analysisData/            # 程序生成的 CSV 统计表
    ├── figures/                 # 程序生成的 PNG 图表
    └── analysis(added,changed).md
```

## 3. 已完成的程序

| `analysis/program/` 中的文件 | 作用 |
| --- | --- |
| `API_Overall_quantity.py` | 汇总两期的 Added、Removed、Changed、Deprecated 数量。 |
| `API_Added_Type.py` | 统计新增 API 的 Class、Method、Field 等类型。 |
| `API_Added_Package.py` | 统计各 Package 的新增数量，输出全部结果和每期 Top 10。 |
| `API_Added_Flagged.py` | 统计新增 API 的 `flagged=yes/no` 数量。 |
| `API_Generate_Figures.py` | 读取以上统计生成的 **5 个 CSV**，输出 **5 张 PNG 图**。 |

处理顺序是：**`api_events.csv` → 统计程序 → `analysisData/` → 绘图程序 → `figures/`**。

## 4. 如何重新运行

在**项目根目录**打开终端，依次执行：

```bash
python analysis/program/API_Overall_quantity.py
python analysis/program/API_Added_Type.py
python analysis/program/API_Added_Package.py
python analysis/program/API_Added_Flagged.py
python analysis/program/API_Generate_Figures.py
```

绘图程序需要 `matplotlib`，未安装时先运行 `python -m pip install matplotlib`。所有程序使用相对目录定位文件；重新运行会更新对应的统计表和图片。

## 5. Changed 如何核对

Changed 没有单独编写统计脚本。可以在 `api_events.csv` 中筛选 `change=changed` 且 `count_structural=1`，再对照 `api_surface_36.csv` 和 `api_surface_37.csv` 的声明。

本次核对到的记录是 `AccessibilityNodeInfo.ExtraRenderingInfo`：它在 API 37 的类声明中增加了 `implements android.os.Parcelable`。这里确认的是**声明变化**，不代表已验证全部运行时行为。

## 6. 交接说明

- **统计数据**：`analysis/analysisData/`。
- **图表**：`analysis/figures/`。
- **数据解释和研究结论**：见单独的分析报告，本文件不重复展开。

后续组员可直接使用以上结果进行报告整合。Removed / Deprecated 的深入统计，以及 API 设计不一致案例，不在本部分工作范围内。
