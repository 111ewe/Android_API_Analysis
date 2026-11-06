"""一键跑完整条链：抓数据 -> 解析比对 -> 清洗。

四步对应四个脚本，各自也能单独跑：
  1. download_api.py    从 AOSP 下载 api35/36/37.txt + removed*.txt，并逐字节校验
  2. build_diff.py      把原文解析成条目，算出两期变动 -> data/diff_*.csv
  3. clean_surface.py   解析成三版全量清单 -> data/clean/api_surface_*.csv
  4. clean_data.py      清洗变动表（去重/修签名/标噪声/复核假删除）-> api_events.csv / api_unique.csv

用法：
  python scripts/run_all.py            正常跑（六份原文齐全且校验通过时不会联网）
  python scripts/run_all.py --refetch  六份原文全部重新下载

第 1 步总是会执行，因为"文件在"不等于"文件对"：它会对六份原文做大小和 sha256 校验，
缺的、损坏的、被 CRLF 污染的都会被重下；六个文件都完整时不联网，所以断网也能复现。
removed*.txt 是判断"删除"真伪的唯一外部依据，缺任何一个都会直接停下，不静默继续。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def run(script, *extra):
    path = os.path.join(HERE, script)
    print("\n" + "=" * 70)
    print("运行", script, *(("参数", *extra) if extra else ()))
    print("=" * 70)
    r = subprocess.run([sys.executable, path, *extra], cwd=ROOT)
    if r.returncode != 0:
        print(f"!! {script} 失败（返回码 {r.returncode}），后续步骤不再执行")
        sys.exit(r.returncode)


def main():
    refetch = "--refetch" in sys.argv
    # --refetch 会把 --force 真正传下去，强制重下六份；
    # 平时也走这个脚本，让它做哈希校验（六份都在且完整时不会联网）。
    run("download_api.py", *(["--force"] if refetch else []))
    run("build_diff.py")
    run("clean_surface.py")
    run("clean_data.py")

    print("\n" + "=" * 70)
    print("全部完成，结果在 data/ 与 data/clean/")
    print("=" * 70)


if __name__ == "__main__":
    main()
