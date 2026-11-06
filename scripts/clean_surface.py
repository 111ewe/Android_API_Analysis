"""Step 3a: 把 API 35 / 36 / 37 的原始签名文件，清洗成干净的版本级 API 清单。

和 build_diff.py 的分工：
  build_diff.py  只输出"变动"，用来回答"变了什么"
  本脚本        输出"全量"，用来回答"这一版到底有哪些 API、长什么样"

输入：raw/api35.txt, api36.txt, api37.txt（metalava 签名快照，勿改）
输出：
  data/clean/api_surface_35.csv   35 版全量 API
  data/clean/api_surface_36.csv   36 版全量 API
  data/clean/api_surface_37.csv   37 版全量 API
  data/clean/api_surface_all.csv  一个 API 一行，带三版存在性（跨版本对比用这张）
  data/clean/surface_log.json     清洗动作计数（每个数字都可以被 verify 复算）

清洗九条规则（每条都在 surface_log.json 里记了触发次数）：
  1. 剥注解：@FlaggedApi / @Deprecated / @Nullable / @NonNull 等全部剥掉，
     但 `@interface` 是"注解类型声明"的关键字，必须保留，否则整类丢失（踩过）。
  2. 类级 @FlaggedApi 下传：整个类带开关时，成员行本身没有注解，要继承。
  3. 类型名归一：java.util.Map -> Map（只吃掉小写开头的包名段，保留嵌套类名）。
  4. 空白归一：`f( int)` -> `f(int)`、`a,b` -> `a, b` 统一。
     API 37 的 metalava 是新一代（Signature format 6.0），和 35/36（2.0）写法不同，
     不归一的话会凭空造出一堆"签名改变"。
  5. 多行签名拼接：一条声明跨行时按括号配平拼回去。
  6. 行尾 // 注释剥离（不动字符串里的 //）。
  7. 排除 android.R 资源编号类（每次构建都变，不属于 API 演化）。
  8. 同版本内同 key 去重（保留第一次出现，记 line_no 可回溯）。
  9. 字段字面量值单独拆成 value 列（常量值分析要用，且值不能被归一化改写）。

用法：python scripts/clean_surface.py
"""
# 让类型标注不在运行时求值：这样 dict[str, Rec] 这类写法在 Python 3.9 上也能跑
from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "raw")
OUT = os.environ.get("CLEAN_OUT") or os.path.join(ROOT, "data", "clean")

VERSIONS = ("35", "36", "37")

CLASS_DECL = re.compile(
    r"^(?:public|protected|private)?\s*(?:final\s+|abstract\s+|static\s+|sealed\s+|strictfp\s+)*"
    r"(class|interface|enum|@interface)\s+([\w.$]+)"
)
# @interface 必须排除在注解之外，否则类声明被啃掉
ANNOTATION = re.compile(r"@(?!interface\b)[\w.]+(?:\((?:[^()]|\([^()]*\))*\))?")
QUALIFIED = re.compile(r"(?<![\w.])(?:[a-z_]\w*\.)+([A-Z]\w*)")
FLAGGED = re.compile(r'@FlaggedApi\(\s*"([^"]+)"\s*\)')
MEMBER_KINDS = ("ctor", "field", "method", "enum_constant")
# 字符串字面量：归一化时必须先抠出来保护，否则 "android.permission.X"
# 会被当成全限定类名吃掉前半截，变成 "X"——常量值被悄悄改写。
STR_LIT = re.compile(r'"(?:[^"\\]|\\.)*"')

MODS = {
    "public", "protected", "private", "static", "final", "abstract",
    "default", "synchronized", "native", "transient", "volatile",
    "strictfp", "sealed",
}


@dataclass
class Rec:
    api_version: str
    package: str
    cls: str
    cls_kind: str          # class / interface / enum / @interface，成员行为空
    kind: str              # class / field / method / ctor / enum_constant
    member: str
    params: str
    throws: str            # 方法尾部 throws 子句（不含关键字），无则空
    modifiers: str
    type: str
    value: str             # 字段字面量 / 注解成员的 default 默认值
    deprecated: bool
    flagged: str
    signature: str
    raw_signature: str
    line_no: int

    @property
    def key(self) -> str:
        head = f"{self.package}.{self.cls}"
        if self.kind == "class":
            return f"{head}#class"
        if self.kind in ("field", "enum_constant"):
            return f"{head}#{self.kind}:{self.member}"
        return f"{head}#{self.kind}:{self.member}({self.params})"

    @property
    def parent_class(self) -> str:
        return self.cls.rsplit(".", 1)[0] if "." in self.cls else ""


def strip_line_comment(s: str) -> str:
    """去行尾 // 注释，但跳过字符串字面量里的 //（如 "http://"）。"""
    in_str = False
    i = 0
    while i < len(s) - 1:
        ch = s[i]
        if ch == '"' and (i == 0 or s[i - 1] != "\\"):
            in_str = not in_str
        elif not in_str and ch == "/" and s[i + 1] == "/":
            return s[:i].rstrip()
        i += 1
    return s.rstrip()


def strip_annotations(text: str) -> str:
    """剥掉注解，用括号配平扫描而不是正则。

    正则版本在 `@Discouraged(message="...(e.g. `R.foo.bar`) ...")` 上会漏：
    参数里含有括号和引号，`[^()]` 只能配平一层，剩下的半截注解会留在签名里，
    直接把 api_key 污染成 `method:(message="Use of this function...")getIdentifier(...)`。
    实测 Resources.getIdentifier / getValue 两条中招。

    `@interface` 是注解类型声明的关键字，必须原样保留。
    """
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] != "@":
            out.append(text[i])
            i += 1
            continue
        if text.startswith("@interface", i):
            out.append("@interface")
            i += len("@interface")
            continue
        j = i + 1
        while j < n and (text[j].isalnum() or text[j] in "._$"):
            j += 1
        if j == i + 1:                     # 孤立的 @
            out.append("@")
            i += 1
            continue
        k = j
        while k < n and text[k] in " \t":
            k += 1
        end = j
        if k < n and text[k] == "(":       # 有参数：按括号配平跳过，忽略字符串里的括号
            depth, in_str, p = 0, False, k
            while p < n:
                ch = text[p]
                if in_str:
                    if ch == "\\":
                        p += 2
                        continue
                    if ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            p += 1
                            break
                p += 1
            end = p
        out.append(" ")
        i = end
    return re.sub(r"\s+", " ", "".join(out)).strip()


def normalize(text: str) -> str:
    """比对口径的归一化，必须和 build_diff.normalize 完全一致，
    否则两套解析算出来的 api_key 会对不上（key 里含参数类型，逗号后有无空格就是两个 key）。
    可读性由 raw_signature 列负责，这里只管"同一个 API 只有一种写法"。

    额外做一件事：字符串字面量先抠出来占位，归一化完再放回。
    否则 `= "android.permission.X"` 里的包名会被 QUALIFIED 当类型名吃掉，
    常量值从 "android.permission.X" 变成 "X"——这是"值被改写"，不是"写法差异"。
    """
    lits = []

    def _mask(m):
        lits.append(m.group(0))
        return f"\x00{len(lits) - 1}\x00"

    text = STR_LIT.sub(_mask, text)
    text = QUALIFIED.sub(r"\1", text)
    text = re.sub(r"\s*,\s*", ",", text)       # 逗号后不留空格，与 build_diff 对齐
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\[\s+", "[", text)
    text = re.sub(r"\s+\]", "]", text)
    text = re.sub(r"\s+", " ", text).strip()
    for i, s in enumerate(lits):
        text = text.replace(f"\x00{i}\x00", s)
    return text


def normalize_decl(text: str) -> str:
    """类声明：extends/implements 后面的类型排序，避免只剩顺序差异。"""
    t = normalize(strip_annotations(text))
    toks = t.split()
    idx = [i for i, w in enumerate(toks) if w in ("extends", "implements")]
    if not idx:
        return t
    cut = min(idx)
    return " ".join(toks[:cut]) + " :: " + " ".join(sorted(toks[cut:]))


def split_params(s: str) -> list:
    parts, depth, cur = [], 0, []
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return [p for p in parts if p]


def peek_mods(prefix: str):
    """拆出开头的修饰符，剩下的就是类型。"""
    toks = prefix.split()
    mods = []
    while toks and toks[0] in MODS:
        mods.append(toks.pop(0))
    return " ".join(mods), " ".join(toks)


def is_resource_class(cls: str) -> bool:
    return cls == "R" or cls.startswith("R.")


class Cleaner:
    def __init__(self, version: str):
        self.version = version
        self.recs: dict[str, Rec] = {}
        self.log = Counter()
        self.class_flag: dict[tuple, str] = {}

    # ---------- 单条成员 ----------
    def add_member(self, text: str, package: str, cls: str, line_no: int) -> None:
        if not cls or is_resource_class(cls):
            if is_resource_class(cls):
                self.log["resource_class_skipped"] += 1
            return
        raw = text.strip().rstrip(";").strip()
        kind, _, rest = raw.partition(" ")
        if kind not in MEMBER_KINDS:
            self.log["unparsable_member"] += 1
            return

        deprecated = "@Deprecated" in rest
        fm = FLAGGED.search(rest)
        flag = fm.group(1) if fm else ""
        if not flag and (package, cls) in self.class_flag:
            flag = self.class_flag[(package, cls)]
            self.log["flag_inherited_from_class"] += 1

        self.log["annotation_markers_removed"] += max(0, rest.count("@") - rest.count("@interface"))
        debared = strip_annotations(rest)
        raw_sig = re.sub(r"\s+", " ", debared).strip()
        sig = normalize(debared)
        if sig != raw_sig:
            self.log["normalized"] += 1

        if kind in ("method", "ctor"):
            lp, rp = sig.find("("), sig.rfind(")")
            if lp < 0 or rp < lp:
                self.log["unparsable_member"] += 1
                return
            head = sig[:lp].strip()
            params = split_params(sig[lp + 1: rp])
            # 尾部可能带 throws 子句（普通方法）或 default 默认值（注解成员）。
            # 两者都只拆成字段，signature 保持原样，否则签名无法从字段重建。
            tail = sig[rp + 1:].strip()
            throws, default = "", ""
            mt = re.search(r"throws\s+(.+)$", tail)
            if mt:
                throws = mt.group(1).strip()
                self.log["method_with_throws"] += 1
            md = re.search(r"default\s+(.+)$", tail)
            if md:
                default = md.group(1).strip()
                self.log["annotation_member_with_default"] += 1
            name = (head.split()[-1] if head.split() else "").split("<")[0].strip()
            prefix = head[:-len(name)].strip() if name else head
            modifiers, rtype = peek_mods(prefix)
            rec = Rec(self.version, package, cls, "", kind, name, ",".join(params),
                      throws, modifiers, rtype, default, deprecated, flag, sig, raw_sig, line_no)
        else:
            m = re.search(r"\s*=\s*(.+)$", sig)
            value = m.group(1).strip() if m else ""
            no_default = sig[:m.start()].strip() if m else sig
            toks = no_default.split()
            if not toks:
                self.log["unparsable_member"] += 1
                return
            name = toks[-1]
            prefix = no_default[:-len(name)].strip()
            modifiers, ftype = peek_mods(prefix)
            if value:
                self.log["field_with_literal"] += 1
            rec = Rec(self.version, package, cls, "", kind, name, "",
                      "", modifiers, ftype, value, deprecated, flag, sig, raw_sig, line_no)

        if rec.key in self.recs:
            self.log["duplicate_key_removed"] += 1
            return
        self.recs[rec.key] = rec

    # ---------- 单条类声明 ----------
    def add_class(self, text: str, package: str, line_no: int):
        """登记一个类声明，返回类名（供调用方更新"当前类"状态）。"""
        stripped = strip_annotations(text).strip()
        cm = CLASS_DECL.match(stripped)
        if not cm:
            self.log["unparsable_class"] += 1
            return None
        cls_kind, cls = cm.group(1), cm.group(2)
        fm = FLAGGED.search(text)
        flag = fm.group(1) if fm else ""
        # 类级 @Deprecated 必须在这里读出来。之前这一格写死 False，导致
        # "整个类被废弃"这件事在表里完全消失（成员行本身不带注解，不会替类说话）。
        deprecated = "@Deprecated" in text
        if flag:
            self.class_flag[(package, cls)] = flag
        if is_resource_class(cls):
            self.log["resource_class_skipped"] += 1
            return cls

        self.log["annotation_markers_removed"] += max(0, text.count("@") - text.count("@interface"))
        sig = normalize_decl(text)
        raw_sig = re.sub(r"\s+", " ", stripped).strip()
        if sig != raw_sig:
            self.log["normalized"] += 1
        if cls_kind == "@interface":
            self.log["annotation_type_kept"] += 1

        # 类行：modifiers 取 kind 关键字之前的部分，type 存 class kind
        head = sig.split(" :: ")[0]
        pre = head.split(cls_kind)[0].strip()
        modifiers, extra = peek_mods(pre)
        if extra:
            modifiers = (modifiers + " " + extra).strip()

        rec = Rec(self.version, package, cls, cls_kind, "class", cls, "",
                  "", modifiers, cls_kind, "", deprecated, flag, sig, raw_sig, line_no)
        if rec.key in self.recs:
            self.log["duplicate_key_removed"] += 1
            return cls
        self.recs[rec.key] = rec
        return cls

    # ---------- 文件扫描 ----------
    def run(self, path: str) -> dict:
        lines = open(path, encoding="utf-8").read().splitlines()
        package, cls = "", ""
        buf, buf_kind, buf_line = "", "", 0

        for i, raw in enumerate(lines, 1):
            if not raw.strip():
                continue
            indent = len(raw) - len(raw.lstrip())
            s = strip_line_comment(raw.strip())
            if not s or s.startswith("//"):
                continue

            if buf:
                buf += " " + s
                if buf_kind == "class":
                    if s.endswith("{"):
                        cls = self.add_class(buf[:-1], package, buf_line) or cls
                        buf = ""
                    continue
                head = buf.split(" ", 1)[0]
                if head in ("method", "ctor"):
                    if buf.count("(") <= buf.count(")") and s.endswith(";"):
                        self.add_member(buf[:-1], package, cls, buf_line)
                        self.log["multiline_joined"] += 1
                        buf = ""
                else:
                    if s.endswith(";"):
                        self.add_member(buf[:-1], package, cls, buf_line)
                        self.log["multiline_joined"] += 1
                        buf = ""
                continue

            if indent == 0 and s.startswith("package "):
                package = s[len("package "):].rstrip("{").strip()
                cls = ""
                continue
            if indent == 0 and s == "}":
                package, cls = "", ""
                continue
            if indent == 2 and s == "}":
                cls = ""
                continue
            if indent == 2:
                if s.endswith("{"):
                    cls = self.add_class(s[:-1], package, i) or cls
                else:
                    buf, buf_kind, buf_line = s, "class", i
                continue
            if s.startswith(MEMBER_KINDS):
                if s.endswith(";"):
                    raw_pkg, raw_cls = package, cls
                    self.add_member(s[:-1], raw_pkg, raw_cls, i)
                else:
                    buf, buf_kind, buf_line = s, "member", i
                continue
        return self.recs


def write_csv(path: str, recs: list) -> None:
    cols = ["api_version", "api_key", "package", "class", "parent_class", "class_kind",
            "kind", "member", "params", "throws_clause", "modifiers", "type", "value",
            "deprecated", "flagged", "flag_name", "signature", "raw_signature", "line_no"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in recs:
            w.writerow([
                r.api_version, r.key, r.package, r.cls, r.parent_class, r.cls_kind,
                r.kind, r.member, r.params, r.throws, r.modifiers, r.type, r.value,
                "yes" if r.deprecated else "no",
                "yes" if r.flagged else "no", r.flagged,
                r.signature, r.raw_signature, r.line_no,
            ])


def write_all(surfaces: dict) -> int:
    """一个 API 一行：三版都在，缺的留空。用于"这个 API 什么时候来、什么时候走"。"""
    keys = []
    seen = set()
    for v in VERSIONS:
        for k in surfaces[v]:
            if k not in seen:
                seen.add(k)
                keys.append(k)

    cols = ["api_key", "package", "class", "kind", "member", "params", "type",
            "in_35", "in_36", "in_37", "first_api", "last_api", "n_versions",
            "flagged_any", "flag_name", "deprecated_37", "signature_37"]
    with open(os.path.join(OUT, "api_surface_all.csv"), "w",
              encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for k in keys:
            present = [v for v in VERSIONS if k in surfaces[v]]
            anchor = surfaces[present[-1]][k]
            w.writerow([
                k, anchor.package, anchor.cls, anchor.kind, anchor.member,
                anchor.params, anchor.type,
                "yes" if "35" in present else "no",
                "yes" if "36" in present else "no",
                "yes" if "37" in present else "no",
                present[0], present[-1], len(present),
                "yes" if any(surfaces[v][k].flagged for v in present) else "no",
                next((surfaces[v][k].flagged for v in present if surfaces[v][k].flagged), ""),
                "yes" if ("37" in present and surfaces["37"][k].deprecated) else "no",
                surfaces["37"][k].signature if "37" in present else "",
            ])
    return len(keys)


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    surfaces, logs = {}, {}

    print("=== 解析并清洗三个版本 ===")
    for v in VERSIONS:
        c = Cleaner(v)
        recs = c.run(os.path.join(RAW, f"api{v}.txt"))
        surfaces[v] = recs
        logs[v] = dict(c.log)
        lst = list(recs.values())
        kinds = Counter(r.kind for r in lst)
        print(f"  API{v}: {len(recs):,} 条  "
              f"类 {kinds['class']:,} / 方法 {kinds['method']:,} / 字段 {kinds['field']:,} "
              f"/ 构造 {kinds['ctor']:,} / 枚举常量 {kinds['enum_constant']:,}"
              f"  开关 {sum(1 for r in lst if r.flagged):,}")
        write_csv(os.path.join(OUT, f"api_surface_{v}.csv"), lst)
        print(f"       -> data/clean/api_surface_{v}.csv")

    n_all = write_all(surfaces)
    print(f"  -> data/clean/api_surface_all.csv（唯一 API {n_all:,} 个）")

    # ---------- 与 build_diff 的解析结果对账 ----------
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from build_diff import parse_file  # noqa: E402

    print("\n=== 与 build_diff 的解析器对账（两套独立实现）===")
    ok = True
    for v in VERSIONS:
        ref = parse_file(os.path.join(RAW, f"api{v}.txt"))
        mine = set(surfaces[v])
        same = mine == set(ref)
        print(f"  API{v}: 本脚本 {len(mine):,} / build_diff {len(ref):,}  键集合一致: {'✅' if same else '❌'}")
        if not same:
            ok = False
            print("     仅本脚本有:", list(mine - set(ref))[:5])
            print("     仅 build_diff 有:", list(set(ref) - mine)[:5])

    log = {
        "versions": {v: {"total": len(surfaces[v]),
                         "by_kind": dict(Counter(r.kind for r in surfaces[v].values())),
                         "flagged": sum(1 for r in surfaces[v].values() if r.flagged),
                         "deprecated": sum(1 for r in surfaces[v].values() if r.deprecated),
                         "with_literal": sum(1 for r in surfaces[v].values() if r.value)}
                     for v in VERSIONS},
        "unique_api": n_all,
        "cleaning": logs,
        "cross_check_build_diff": "pass" if ok else "FAIL",
    }
    with open(os.path.join(OUT, "surface_log.json"), "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    print("\n=== 清洗动作（三版合计）===")
    tot = Counter()
    for v in VERSIONS:
        tot.update(logs[v])
    for k, n in tot.most_common():
        print(f"  {k:<32} {n:,}")
    print(f"\n对账结果: {'全部通过' if ok else '有不一致，需要查'}")
    print("产物: data/clean/api_surface_{35,36,37,all}.csv + surface_log.json")


if __name__ == "__main__":
    main()
