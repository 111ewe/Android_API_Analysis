"""Generate five report-ready figures from the existing API evolution summary CSVs.

Expected project structure:
    analysis/
      program/API_Generate_Figures.py
      analysisData/API_Overall_quantity.csv
      analysisData/API_Added_Type.csv
      analysisData/API_Added_Package_Top10.csv
      analysisData/API_Added_Package_All.csv
      analysisData/API_Added_Flagged.csv
      figures/                  # created automatically

Run from any working directory:
    python analysis/program/API_Generate_Figures.py

Dependency: matplotlib (pip install matplotlib)
"""

from collections import defaultdict
import csv
import os
from pathlib import Path
import tempfile

import matplotlib
matplotlib.use("Agg")  # Generate PNGs without opening a window.
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator


# Paths are relative to the location of this Python script, not your current terminal folder.
ANALYSIS_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ANALYSIS_DIR / "analysisData"
FIGURES_DIR = ANALYSIS_DIR / "figures"

# One shared style for report-ready figures.
plt.rcParams.update({
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.titleweight": "bold",
    "axes.titlesize": 14,
    "axes.labelsize": 10,
    "font.size": 10,
    "figure.dpi": 120,
    "savefig.dpi": 220,
})


def load_csv(filename, expected_columns):
    """Read a table and stop with a helpful message if fields are missing."""
    path = DATA_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing input: {path}\n"
            "Please run the statistics programs first and put their CSVs under analysis/analysisData/."
        )
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = set(expected_columns) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{filename}: missing columns: {', '.join(sorted(missing))}")
        rows = list(reader)
    if not rows:
        raise ValueError(f"{filename} is empty")
    return rows


def numeric(row, column):
    """Parse a count from one CSV cell."""
    try:
        return int(row[column])
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Invalid integer {column}={row.get(column)!r}, period={row.get('period')}") from exc


def save_png(fig, filename):
    """Write to a temporary PNG stream, then replace the output on each run.

    Using an already opened binary stream avoids some Windows/Pillow path-based
    save errors. os.replace() overwrites an existing file atomically, provided
    no other application locks the destination file.
    """
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    dest = FIGURES_DIR / filename
    temporary = None
    try:
        # Create a file in the SAME directory to make replacement reliable.
        with tempfile.NamedTemporaryFile(
            mode="w+b", dir=FIGURES_DIR, prefix="_plot_", suffix=".png", delete=False
        ) as stream:
            temporary = Path(stream.name)
            fig.savefig(stream, format="png", bbox_inches="tight", facecolor="white")
            stream.flush()

        # Replace the old PNG if it exists; create it otherwise.
        try:
            os.replace(temporary, dest)
        except OSError as exc:
            raise OSError(
                f"Could not replace {dest}. Close the image in Photos, "
                "PowerPoint, Word, or other programs, and check folder permissions."
            ) from exc
        print(f"Generated (overwritten if existing): {dest.relative_to(ANALYSIS_DIR)}")
    finally:
        plt.close(fig)
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def plot_overall(rows, periods):
    """Stacked bars showing the four change-event types in each period."""
    lookup = {r["period"]: r for r in rows if r["period"] in periods}

    removed_label = (
        f"Removed (35→36: {numeric(lookup[periods[0]], 'removed')}, "
        f"36→37: {numeric(lookup[periods[1]], 'removed')})"
    )
    changed_label = (
        f"Changed (35→36: {numeric(lookup[periods[0]], 'changed')}, "
        f"36→37: {numeric(lookup[periods[1]], 'changed')})"
    )

    series = [
        ("Added", "added", "#467AB9"),
        ("Deprecated", "deprecated", "#E7A449"),
        (removed_label, "removed", "#77AD9A"),
        (changed_label, "changed", "#B185BD"),
    ]
    x = list(range(len(periods)))
    # Reserve a dedicated top band for the title and the two-row legend.
    # This prevents legend entries from overlapping either total label.
    fig = plt.figure(figsize=(10.6, 6.1))
    ax = fig.add_axes([0.09, 0.16, 0.86, 0.57])
    bottoms = [0] * len(periods)
    for label, col, color in series:
        vals = [numeric(lookup[p], col) for p in periods]
        ax.bar(x, vals, bottom=bottoms, width=0.52, color=color, label=label)
        # Label large segments inside bars only.
        for i, val in enumerate(vals):
            if val >= 65:
                ax.text(
                    x[i], bottoms[i] + val / 2, f"{val:,}",
                    ha="center", va="center", color="white", weight="bold"
                )
        bottoms = [b + v for b, v in zip(bottoms, vals)]

    for i, period in enumerate(periods):
        csv_total = numeric(lookup[period], "total")
        if csv_total != bottoms[i]:
            raise ValueError(f"Overall count does not match total in {period}")
        ax.annotate(
            f"Total: {csv_total:,}", (x[i], csv_total), xytext=(0, 10),
            textcoords="offset points", ha="center", weight="bold", fontsize=11
        )

    ax.set_xticks(x, [p.replace("-", " → ") for p in periods])
    ax.set_ylabel("Number of change events")
    ax.set_ylim(0, max(bottoms) * 1.18)
    fig.suptitle("Overall Android API Changes", fontsize=16, fontweight="bold", y=0.982)
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(
        handles, labels, loc="upper center", bbox_to_anchor=(0.5, 0.91),
        frameon=False, ncol=2, fontsize=10, columnspacing=1.3,
        handlelength=2.0, labelspacing=0.7,
    )
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(axis="y", alpha=0.13)
    ax.set_axisbelow(True)
    fig.text(
        0.5, 0.012,
        "Total is the sum of change-event categories; it is not a count of distinct APIs.",
        ha="center", va="bottom", fontsize=8, color="#555555"
    )
    # Axes position is set explicitly above; do not call tight_layout here.
    save_png(fig, "01_Overall_API_Changes.png")


def plot_added_types(rows, periods):
    kinds = [("Field", "field"), ("Method", "method"), ("Class", "class"),
             ("Constructor", "ctor"), ("Enum constant", "enum_constant")]
    lookup = {r["period"]: r for r in rows if r["period"] in periods}
    fig, ax = plt.subplots(figsize=(10, 5.3))
    group_centers = list(range(len(kinds)))
    width = 0.34
    colors = ["#467AB9", "#E7A449"]
    for i, period in enumerate(periods):
        vals = [numeric(lookup[period], field) for _, field in kinds]
        if sum(vals) != numeric(lookup[period], "total"):
            raise ValueError(f"Added kind distribution does not sum to total in {period}")
        x = [j + (i - 0.5) * width for j in group_centers]
        bars = ax.bar(x, vals, width, label=period.replace("-", " → "), color=colors[i])
        for bar, val in zip(bars, vals):
            # Label every bar, including small values like constructor / enum constant.
            y = bar.get_height()
            ax.annotate(
                str(val),
                (bar.get_x() + bar.get_width() / 2, y),
                xytext=(0, 5),
                textcoords="offset points",
                ha="center",
                fontsize=9,
            )
    ax.set_xticks(group_centers, [k for k, _ in kinds])
    ax.set_ylabel("Added API declarations")
    ax.set_title("Added APIs by Declaration Type")
    ax.legend(frameon=False)
    ax.set_ylim(0, max(numeric(r, "field") for r in lookup.values()) * 1.2)
    ax.grid(axis="y", alpha=0.13)
    ax.set_axisbelow(True)
    fig.tight_layout()
    save_png(fig, "02_Added_API_Types.png")


def plot_packages(rows, periods, all_rows):
    by_period = defaultdict(list)
    for r in rows:
        if r["period"] in periods:
            by_period[r["period"]].append(r)
    by_all = defaultdict(list)
    for r in all_rows:
        if r["period"] in periods:
            by_all[r["period"]].append(r)
    for i, period in enumerate(periods, start=3):
        ranked = sorted(by_period[period], key=lambda r: (-numeric(r, "added_count"), r["package"]))[:10]
        if not ranked:
            raise ValueError(f"No top-10 packages for {period}")
        # Check ranking data agrees with the full summary for each package.
        all_map = {r["package"]: numeric(r, "added_count") for r in by_all[period]}
        for r in ranked:
            if all_map.get(r["package"]) != numeric(r, "added_count"):
                raise ValueError(f"Top10 and All package counts disagree for {period}: {r['package']}")
        ranked.reverse()  # Largest bar at the top of a horizontal plot.
        names = [r["package"] for r in ranked]
        counts = [numeric(r, "added_count") for r in ranked]
        fig, ax = plt.subplots(figsize=(10.5, 6))
        bars = ax.barh(names, counts, color="#467AB9")
        for bar, val in zip(bars, counts):
            ax.annotate(f"{val:,}", (bar.get_width(), bar.get_y() + bar.get_height() / 2),
                        xytext=(6, 0), textcoords="offset points", va="center", fontsize=9)
        ax.set_xlim(0, max(counts) * 1.19)
        ax.set_xlabel("Added API declarations")
        ax.set_title(f"Top 10 Packages with Added APIs: {period.replace('-', ' → ')}")
        ax.grid(axis="x", alpha=0.13)
        ax.set_axisbelow(True)
        fig.tight_layout()
        save_png(fig, f"{i:02d}_Added_Packages_Top10_{period}.png")


def plot_flagged(rows, periods, added_type_rows):
    # Input includes an optional Total row; use individual periods for transparent validation.
    filtered = [r for r in rows if r["period"] in periods]
    lookup = {r["period"]: r for r in filtered}
    if len(lookup) != len(periods):
        raise ValueError("Feature Flag CSV must contain one row per period")
    yes = sum(numeric(lookup[p], "flagged_yes") for p in periods)
    no = sum(numeric(lookup[p], "flagged_no") for p in periods)
    overall = yes + no
    for p in periods:
        expected = numeric(next(r for r in added_type_rows if r["period"] == p), "total")
        if numeric(lookup[p], "total") != expected:
            raise ValueError(f"Flag counts do not match added counts in {p}")
        if numeric(lookup[p], "flagged_yes") + numeric(lookup[p], "flagged_no") != expected:
            raise ValueError(f"Flag values do not sum to total in {p}")
    fig, ax = plt.subplots(figsize=(7.8, 5.8))
    wedges, _, labels = ax.pie([yes, no], labels=None, startangle=90,
                               counterclock=False, colors=["#467AB9", "#E7A449"],
                               autopct=lambda pct: f"{pct:.1f}%", pctdistance=0.72,
                               wedgeprops={"linewidth": 2, "edgecolor": "white"},
                               textprops={"fontsize": 11})
    ax.legend(wedges, [f"Flagged: Yes — {yes:,}", f"Flagged: No — {no:,}"],
              loc="center left", bbox_to_anchor=(1, 0.5), frameon=False)
    ax.set_title(f"Feature Flags Among Added APIs (n = {overall:,})", pad=15)
    ax.axis("equal")
    fig.tight_layout()
    save_png(fig, "05_Added_API_Feature_Flags.png")


def main():
    overall = load_csv("API_Overall_quantity.csv", ["period", "added", "removed", "changed", "deprecated", "total"])
    types = load_csv("API_Added_Type.csv", ["period", "field", "method", "class", "ctor", "enum_constant", "total"])
    pkg_top = load_csv("API_Added_Package_Top10.csv", ["period", "rank", "package", "added_count"])
    pkg_all = load_csv("API_Added_Package_All.csv", ["period", "package", "added_count"])
    flagged = load_csv("API_Added_Flagged.csv", ["period", "flagged_yes", "flagged_no", "total"])

    periods = [r["period"] for r in overall]
    if len(periods) != 2 or len(set(periods)) != 2:
        raise ValueError("Expected exactly two unique periods in overall quantity table")
    # Sanity-check that the related tables agree on the number of added events.
    for period in periods:
        overall_count = numeric(next(r for r in overall if r["period"] == period), "added")
        type_count = numeric(next(r for r in types if r["period"] == period), "total")
        package_count = sum(numeric(r, "added_count") for r in pkg_all if r["period"] == period)
        if overall_count != type_count or overall_count != package_count:
            raise ValueError(f"Added counts disagree across CSV files in {period}")

    FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    plot_overall(overall, periods)
    plot_added_types(types, periods)
    plot_packages(pkg_top, periods, pkg_all)
    plot_flagged(flagged, periods, types)
    print("Success: 5 figures saved to analysis/figures/.")


if __name__ == "__main__":
    main()
