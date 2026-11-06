"""Step 1: 下载 API 35/36/37 的官方 API 签名文件（AOSP current.txt + removed.txt）

数据源：AOSP frameworks/base/core/api/
  - current.txt 是当前公开 API 快照（metalava 生成）
  - removed.txt 是 Google 自己记录的"已被删除的公开 API"清单
    → 用来交叉验证我们算出来的"删除"到底是真删还是文件边界变化
    → 缺这个文件，"删除"就无法定性，所以这里把它当必需输入，不是可选

  - API 35 -> android-15.0.0_r1
  - API 36 -> android-16.0.0_r1
  - API 37 -> android-17.0.0_r1

关于换行符（踩过的坑）：
  用文本模式 open(..., "w") 写，Windows 会把每个 \n 换成 \r\n，
  六份文件各多出几千到六万多个字节，sha256 就跟官方对不上了
  （api35 官方 4,382,010 字节，文本模式写出来是 4,444,548，多的 62,538 正好是行数）。
  所以这里一律二进制写入，raw/ 里就是官方原字节，可以直接拿 sha256 去比。

关于完整性判断（也是踩过的坑）：
  以前只要"文件存在且大于 20,000 字节"就跳过，这等于什么都没验。
  现在把六个文件的大小和 sha256 写死在 EXPECTED 里（这些标签是不可变的 commit tag，
  内容不会变），已存在的文件必须逐字节对上才算数，对不上就重下。
  这样既能离线复现，又能发现文件被截断、被 CRLF 污染或被人改过。

用法：
  python download_api.py            缺什么补什么，已有的做哈希校验
  python download_api.py --force    六份全部重新下载（run_all.py --refetch 会传这个）
输出：raw/api{35,36,37}.txt / raw/removed{35,36,37}.txt
"""
import base64
import hashlib
import os
import sys
import urllib.request

BASE = "https://android.googlesource.com/platform/frameworks/base/+/"
TAGS = {
    35: "android-15.0.0_r1",
    36: "android-16.0.0_r1",
    37: "android-17.0.0_r1",
}
FILES = {"api": "core/api/current.txt", "removed": "core/api/removed.txt"}

# (前缀, API level) -> (字节数, sha256)
# 来源：直接从上面三个标签的 ?format=TEXT 取原字节算出来的；标签不可变，值是固定的。
EXPECTED = {
    ("api", 35): (4_382_010, "a07b0a1df1b7aeeb42fd8859c07b5627ff4f7930948771ee545b004f46e73c6e"),
    ("api", 36): (4_540_786, "1f84aa28d5e59cd4b24087032bdd6880d87570dda64a8a6474817fb043b39b16"),
    ("api", 37): (4_657_449, "116c793ecd67c793fe3d51c0cbfa158dff2e7f2d8defcd1b9fc60d829547e18a"),
    ("removed", 35): (28_778, "28c869a9855aca656790594a7028220e6f45e4d9f67b12b75ec044f9eadd2bed"),
    ("removed", 36): (29_138, "13b89961ee12fb1ce8d7bbcbae3bc8b85135c304833ce41459337f00d58d9134"),
    ("removed", 37): (32_858, "23d84564ea1586e9f38d15ad0d58e7550e45b5f3f1a9ea5b3bcca5d66afa30e5"),
}

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(HERE, "raw")

REMOVED_HINT = (
    "removed*.txt 是判断「删除」真伪的唯一外部依据，"
    "缺了它整条链的删除定性都不可信。"
)


def fetch(tag: str, path: str) -> bytes:
    url = f"{BASE}{tag}/{path}?format=TEXT"
    req = urllib.request.Request(url, headers={"User-Agent": "python-urllib"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    # googlesource 的 ?format=TEXT 返回 base64，解码后是文件原字节
    return base64.b64decode(raw)


def is_intact(path: str, prefix: str, level: int) -> bool:
    """文件存在、大小对、sha256 对，三者全中才算完整。"""
    if not os.path.exists(path):
        return False
    want_size, want_hash = EXPECTED[(prefix, level)]
    with open(path, "rb") as f:
        data = f.read()
    return len(data) == want_size and hashlib.sha256(data).hexdigest() == want_hash


def main() -> int:
    force = any(a in ("--force", "--refetch") for a in sys.argv[1:])
    os.makedirs(RAW, exist_ok=True)

    bad = []
    for level, tag in TAGS.items():
        for prefix, path in FILES.items():
            out = os.path.join(RAW, f"{prefix}{level}.txt")
            want_size, want_hash = EXPECTED[(prefix, level)]

            if not force and is_intact(out, prefix, level):
                print(f"[skip] {prefix}{level}.txt 已存在且哈希正确  {want_size:,} 字节")
                continue

            if os.path.exists(out) and not force:
                print(f"[fix ] {prefix}{level}.txt 现有 {os.path.getsize(out):,} 字节"
                      f"≠ 预期 {want_size:,}，重新下载")

            print(f"[fetch] {prefix}{level}.txt <- {tag}/{path}")
            try:
                data = fetch(tag, path)
            except Exception as e:                                     # noqa: BLE001
                print(f"[FAIL] {prefix}{level}.txt 下载失败：{type(e).__name__}: {e}")
                if prefix == "removed":
                    print(f"       {REMOVED_HINT}")
                bad.append(f"{prefix}{level}.txt")
                continue

            got_hash = hashlib.sha256(data).hexdigest()
            if len(data) != want_size or got_hash != want_hash:
                print(f"[FAIL] {prefix}{level}.txt 与预期不符："
                      f"拿到 {len(data):,} 字节 / {got_hash[:16]}，"
                      f"预期 {want_size:,} 字节 / {want_hash[:16]}")
                print("       标签是不可变的，对不上说明取到的不是同一份文件，不写入。")
                bad.append(f"{prefix}{level}.txt")
                continue

            with open(out, "wb") as f:      # 二进制写，不做换行转换
                f.write(data)
            print(f"[ok]   {prefix}{level}.txt  {len(data):,} 字节  sha256={got_hash[:16]}")

    if bad:
        print(f"\n!! 有 {len(bad)} 个文件没拿到或校验不过：{', '.join(bad)}")
        print("!! 缺文件时不要继续跑后面的步骤，否则「删除」的定性会静默失真。")
        return 1

    print(f"\n六份原文全部就位，{len(EXPECTED)} 个哈希均已校验")
    return 0


if __name__ == "__main__":
    sys.exit(main())
