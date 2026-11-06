"""Step 2: 解析 + 比对 API 35/36/37，输出结构化差异数据。

口径（全组必须统一，写报告时要写明）：
1. 一个 API 的唯一身份证 key：
   - 类   : <package>.<class>#class
   - 字段 : <package>.<class>#field:<name>
   - 枚举 : <package>.<class>#enum_constant:<name>
   - 构造 : <package>.<class>#ctor:(<参数类型,逗号分隔>)
   - 方法 : <package>.<class>#method:<name>(<参数类型,逗号分隔>)
2. 变动只分 4 种：
   - added     : 新版本有、老版本没有
   - removed   : 老版本有、新版本没有
   - changed   : 同一个 key 但签名变了（返回值/修饰符/字段类型变）
   - deprecated: 同一个 key 签名没变，但新版本被打上 @Deprecated
3. "参数列表变了" = 删除旧方法 + 新增新方法（因为 key 里含参数），不算 changed。
4. 新增/删除的类，只记一条类级记录，其成员不再单独计入（避免重复计数）。
5. modifier_only 列：签名差异**只**来自 final/abstract 这类修饰符时标 yes。
   实测 API 36→37 的 21 条"签名改变"全是这一类：18 条是注解类型的成员
   在新版 metalava 里不再打印 abstract（渲染差异），3 条是 SpannedString
   这个 final 类的方法上多余的 final 被去掉（语义无变化）。
   真正的签名变化要用 modifier_only=no 过滤后才是。
6. 类声明本身的变化也算 changed（新增接口、加 final 等），
   比较前会把 extends/implements 的顺序排好，避免只剩顺序差异也算变化。

用法：python scripts/build_diff.py
输出：data/diff_35_36.csv, data/diff_36_37.csv, data/stats.json
"""
# 让类型标注不在运行时求值：这样 Member | None 这类写法在 Python 3.9 上也能跑
# （PEP 604 的联合类型语法是 3.10 才有的，不开这个开关 3.9 会在 import 时就报错）
from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "raw")
DATA = os.environ.get("DIFF_OUT") or os.path.join(ROOT, "data")

CLASS_DECL = re.compile(
    r"^(?:public|protected|private)?\s*(?:final\s+|abstract\s+|static\s+|sealed\s+|strictfp\s+)*"
    r"(class|interface|enum|@interface)\s+([\w.$]+)"
)
# 注意 @interface 是"注解类型声明"的关键字，不能被当成注解剥掉，否则整类丢失
ANNOTATION = re.compile(r"@(?!interface\b)[\w.]+(?:\((?:[^()]|\([^()]*\))*\))?")
# 把 a.b.C / a.b.C.D 归一成 C / C.D（只吃掉小写开头的包名段，保留嵌套类）
# (?<![\w.]) 是必需的：否则 "Notification.ProgressStyle" 会被从 "otification."
# 处开始匹配，截成 "NProgressStyle"——名字被啃掉一截，是隐蔽的假匹配来源。
QUALIFIED = re.compile(r"(?<![\w.])(?:[a-z_]\w*\.)+([A-Z]\w*)")
MEMBER_KINDS = ("ctor", "field", "method", "enum_constant")
FLAGGED = re.compile(r'@FlaggedApi\(\s*"([^"]+)"\s*\)')


def normalize(text: str) -> str:
    """把"同一个签名"的不同写法拉平成同一个字符串。

    已知的三种写法差异（都纯属排版，不是语义变化）：
      1. API 35/36 写全限定名，API 37 写短名：java.util.Map<java.lang.String,...> vs Map<String,...>
      2. 注解被剥掉后残留空格：public void f( int) vs public void f(int)
      3. 逗号后有无空格：f(int,char) vs f(int, char)
    不做第 2、3 步的话，"签名改变"会被大量排版差异污染（实测 13 条里 10 条是假的）。

    字符串字面量必须先抠出来占位再归一化：否则 `= "android.permission.X"` 里的
    包名会被当成全限定类名吃掉，两版常量值都变成 `"X"`——既改了值，
    又会把"两版值不同"这种真变化掩盖掉。
    """
    lits = []

    def _mask(m):
        lits.append(m.group(0))
        return f"\x00{len(lits) - 1}\x00"

    text = re.sub(r'"(?:[^"\\]|\\.)*"', _mask, text)
    text = QUALIFIED.sub(r"\1", text)
    text = re.sub(r"\s*,\s*", ",", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\[\s+", "[", text)
    text = re.sub(r"\s+\]", "]", text)
    text = re.sub(r"\s+", " ", text).strip()
    for i, s in enumerate(lits):
        text = text.replace(f"\x00{i}\x00", s)
    return text


def normalize_decl(text: str) -> str:
    """类声明的归一化：把 extends/implements 后面的类型排序。

    metalava 换版本后会把 extends 列表换个顺序（实测 36→37 有 4 条），
    那只是书写顺序，不是 API 变化，排一下序就不会误报。
    但"多实现了一个 Parcelable"这种真变化会保留下来。
    """
    t = normalize(strip_annotations(text))
    toks = t.split()
    idx = [i for i, w in enumerate(toks) if w in ("extends", "implements")]
    if not idx:
        return t
    cut = min(idx)
    return " ".join(toks[:cut]) + " :: " + " ".join(sorted(toks[cut:]))


def is_resource_class(cls: str) -> bool:
    """android.R 及其内部类只是资源 ID 编号，每次构建都变，不属于 API 演化。"""
    return cls == "R" or cls.startswith("R.")



@dataclass
class Member:
    package: str
    cls: str
    kind: str
    name: str
    params: tuple = ()
    signature: str = ""
    deprecated: bool = False
    flag: str = ""
    modifier_only: bool = False
    # 仅对 change=removed 有意义，见 classify_removals()
    removed_status: str = ""
    # 未归一化的原文签名（只剥注解和行尾注释），用于审计"两种写法是否同一 API"
    raw_sig: str = ""

    @property
    def key(self) -> str:
        head = f"{self.package}.{self.cls}"
        if self.kind == "class":
            return f"{head}#class"
        if self.kind in ("field", "enum_constant"):
            return f"{head}#{self.kind}:{self.name}"
        return f"{head}#{self.kind}:{self.name}({','.join(self.params)})"


def strip_line_comment(s: str) -> str:
    """去掉行尾 // 注释，但不动字符串字面量里的 //（如 "http://"）。"""
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

    正则 `@[\\w.]+(?:\\([^()]|\\([^()]*\\))*\\)?` 只能配平一层嵌套，
    遇到 `@Discouraged(message="...(e.g. `getIdentifier("bar","foo",null)`)...")`
    这种参数里带括号的注解会剥不干净，残留的 `(message="...")` 会把 api_key 污染成
    `method:(message="Use of this function...")getIdentifier(String,String,String)`。
    实测 `Resources.getIdentifier` 和 `Resources.getValue(String,...)` 两条中招
    （三版都脏、key 都一样，所以没在 diff 里冒出来，但按 key 查会查不到）。

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
        if j == i + 1:
            out.append("@")
            i += 1
            continue
        k = j
        while k < n and text[k] in " \t":
            k += 1
        end = j
        if k < n and text[k] == "(":
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


def split_params(s: str) -> tuple:
    """按顶层逗号切参数，忽略泛型尖括号里的逗号。"""
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
    return tuple(p for p in parts if p)


def parse_member_line(raw: str, package: str, cls: str) -> Member | None:
    text = raw.strip().rstrip(";").strip()
    kind, _, rest = text.partition(" ")
    if kind not in MEMBER_KINDS:
        return None
    deprecated = "@Deprecated" in rest
    fm = FLAGGED.search(rest)
    flag = fm.group(1) if fm else ""
    debared = strip_annotations(rest)
    clean = normalize(debared)
    # 只折叠连续空白，不动的类型名/括号内空格——保留"原始写法"，供审计
    raw_sig = re.sub(r"\s+", " ", debared).strip()

    if kind in ("method", "ctor"):
        lp = clean.find("(")
        rp = clean.rfind(")")
        if lp < 0 or rp < lp:
            return None
        head = clean[:lp].strip()
        params = split_params(clean[lp + 1: rp])
        name = head.split()[-1] if head.split() else ""
        name = name.split("<")[0].strip()
        return Member(package, cls, kind, name, params, clean, deprecated, flag,
                      raw_sig=raw_sig)

    no_default = clean.split("=")[0].strip()
    tokens = no_default.split()
    if not tokens:
        return None
    return Member(package, cls, kind, tokens[-1], (), clean, deprecated, flag,
                  raw_sig=raw_sig)


def parse_file(path: str) -> dict[str, Member]:
    """返回 key -> Member。"""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    out: dict[str, Member] = {}
    class_flag: dict[tuple, str] = {}
    package = ""
    cls = ""
    buf = ""
    buf_kind = ""  # "class" or "member"

    def flush_member(text: str) -> None:
        if not text or not cls or is_resource_class(cls):
            return
        m = parse_member_line(text, package, cls)
        if m is not None:
            # 类级 @FlaggedApi 会作用于整个类的所有成员，成员行本身不带注解，要继承
            if not m.flag and (package, cls) in class_flag:
                m.flag = class_flag[(package, cls)]
            out.setdefault(m.key, m)

    def flush_class(text: str) -> None:
        nonlocal cls
        cm = CLASS_DECL.match(strip_annotations(text).strip())
        if cm:
            cls = cm.group(2)
            fm = FLAGGED.search(text)
            if fm:
                class_flag[(package, cls)] = fm.group(1)
            if is_resource_class(cls):
                return
            member = Member(package, cls, "class", cls, (), normalize_decl(text),
                            # 类级 @Deprecated 必须从类行读出；写死 False 会让
                            # "整个类被废弃"这件事在差异表里彻底消失。
                            "@Deprecated" in text,
                            class_flag.get((package, cls), ""),
                            raw_sig=re.sub(r"\s+", " ", strip_annotations(text)).strip())
            out.setdefault(member.key, member)



    for raw in lines:
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip())
        s = strip_line_comment(raw.strip())
        if not s or s.startswith("//"):
            continue

        if buf:
            if buf_kind == "class":
                buf += " " + s
                if s.endswith("{"):
                    flush_class(buf[:-1])
                    buf = ""
                continue
            # member 续行：括号配平即结束
            buf += " " + s
            if buf.count("(") <= buf.count(")"):
                flush_member(buf.rstrip(";"))
                buf = ""
            continue

        if indent == 0 and s.startswith("package "):
            package = s[len("package "):].rstrip("{").strip()
            cls = ""
            continue

        if indent == 0 and s == "}":
            package = ""
            cls = ""
            continue

        if indent == 2 and s == "}":
            cls = ""
            continue

        if indent == 2:
            if s.endswith("{"):
                flush_class(s[:-1])
            else:
                buf, buf_kind = s, "class"
            continue

        if s.startswith(MEMBER_KINDS):
            if s.endswith(";"):
                flush_member(s[:-1])
            else:
                buf, buf_kind = s, "member"
            continue
    return out


def _class_attrs(surface, package: str, cls: str):
    """返回 (是不是注解类型, 是不是 final 类)。

    用来判断某个成员的 final/abstract 差异能不能解释成 metalava 的渲染差异：
    只有这两类位置的修饰符是"冗余可省"的，普通可继承类的方法真的加了 final 就是契约变化。
    """
    m = surface.get(f"{package}.{cls}#class")
    if m is None:
        return False, False
    decl = (m.raw_sig or m.signature or "").replace(" :: ", " ")
    return "@interface" in decl, re.search(r"\bfinal\s+(class|enum)\b", decl) is not None


def diff(old: dict[str, Member], new: dict[str, Member], old_api: int, new_api: int):
    rows = []
    # 必须用「包名+类名」作集合元素：不同包下可能有同名类，
    # 只按简单类名会比错，把新增类的成员误当成成员级新增。
    old_classes = {(m.package, m.cls) for m in old.values() if m.kind == "class"}
    new_classes = {(m.package, m.cls) for m in new.values() if m.kind == "class"}
    added_classes = new_classes - old_classes
    removed_classes = old_classes - new_classes

    for key, m in new.items():
        prev = old.get(key)
        if prev is None:
            fq = (m.package, m.cls)
            if m.kind == "class" and fq in added_classes:
                rows.append((m, "added", old_api, new_api, True))
            elif m.kind != "class" and fq not in added_classes:
                rows.append((m, "added", old_api, new_api, False))
            continue
        if m.kind == "class":
            # 类声明本身也参与比较：新增接口、加 final 都算演化
            if m.signature != prev.signature:
                rows.append((m, "changed", old_api, new_api, False))
            # 类级废弃单独记一条。少了这一支，"某个类整类被废弃"就查不出来
            # （成员行不带注解，不会替类说话）。
            elif m.deprecated and not prev.deprecated:
                rows.append((m, "deprecated", old_api, new_api, False))
            continue
        if m.signature != prev.signature:
            # 只差 final/abstract 的，先按文本判一次，但**不能一律当噪声**。
            # 只有下面两种位置，这两个修饰符才是可以打印/省略的渲染差异：
            #   ① 注解类型（@interface）的成员——隐式 public abstract，有的版本印有的不印；
            #   ② final 类里的成员——final 冗余，印不印不影响语义。
            # 普通可继承类的方法真的加了 final，或方法从抽象变实体，都是契约变化，要保留。
            a = prev.signature.replace(" final ", " ").replace(" abstract ", " ")
            b = m.signature.replace(" final ", " ").replace(" abstract ", " ")
            if a == b:
                is_annot, is_final_class = _class_attrs(new, m.package, m.cls)
                m.modifier_only = (m.kind != "class") and (is_annot or is_final_class)
            else:
                m.modifier_only = False
            rows.append((m, "changed", old_api, new_api, False))
        elif m.deprecated and not prev.deprecated:
            rows.append((m, "deprecated", old_api, new_api, False))

    for key, m in old.items():
        if key in new:
            continue
        fq = (m.package, m.cls)
        if m.kind == "class" and fq in removed_classes:
            rows.append((m, "removed", old_api, new_api, True))
        elif m.kind != "class" and fq not in removed_classes:
            rows.append((m, "removed", old_api, new_api, False))
    return rows


def load_removed(level: int):
    """读 Google 自己的 removed.txt（累计版：记录了历史上所有被删的公开 API）。

    返回 (类标题集合, 成员集合)。成员键带上参数，
    因为 `f(int)` 和 `f(String)` 是两条不同的 API，只按名字比会把重载混成一条。
    这是判断"删除"真伪的唯一外部依据。
    """
    path = os.path.join(RAW, f"removed{level}.txt")
    classes, members = set(), set()
    if not os.path.exists(path):
        return classes, members
    package = cls = ""
    for raw in open(path, encoding="utf-8"):
        s = raw.strip()
        if not s or s.startswith("//"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0 and s.startswith("package "):
            package = s[8:].rstrip("{").strip()
            continue
        if indent == 2 and s.endswith("{"):
            m = CLASS_DECL.search(s) or re.search(
                r"(class|interface|enum|@interface)\s+([\w.$]+)", s)
            cls = m.group(2) if m else cls
            classes.add((package, cls))
            continue
        if indent == 4 and s.startswith(MEMBER_KINDS):
            mem = parse_member_line(strip_line_comment(s), package, cls)
            if mem is not None:
                members.add((package, cls, mem.kind, mem.name, mem.params))
    return classes, members


def classify_removals(rows, old_surface, new_surface, removed_sets) -> None:
    """给每条"删除"定性，防止把"文件边界变了"当成"Google 删了 API"。

    四级证据（强→弱）：
      confirmed   官方 removed.txt 里明确记着 → 真删
                  类级要更严：光有类标题不够（一个类只要删过成员就会留下标题），
                  必须"旧版这个类的所有成员都出现在 removed.txt 里"才算整类被删
      moved_out   该包在新版本 API 文件里整个不存在，且 removed.txt 没记这笔删除
                  → 属"文件边界变化"，不是删 API（但不代表已确证它搬去了哪里）
      flagged     原本就是 @FlaggedApi 的开关 API → 未启用的预览能力被撤回
      unknown     以上都不是 → 必须人工复核
    """
    new_pkgs = {m.package for m in new_surface.values()}
    classes, members = removed_sets
    # 旧版每个类底下的成员（含参数），用于判断"整类被删"还是"只删了几个成员"
    old_members_of = {}
    for m in old_surface.values():
        if m.kind != "class":
            old_members_of.setdefault((m.package, m.cls), []).append(
                (m.package, m.cls, m.kind, m.name, m.params))
    for m, change, *_ in rows:
        if change != "removed":
            continue
        if m.kind == "class":
            owns = old_members_of.get((m.package, m.cls), ())
            hit = ((m.package, m.cls) in classes
                   and all(k in members for k in owns))
        else:
            hit = (m.package, m.cls, m.kind, m.name, m.params) in members
        if hit:
            m.removed_status = "confirmed"
        elif m.package not in new_pkgs:
            m.removed_status = "moved_out"
        elif m.flag:
            m.removed_status = "flagged"
        else:
            m.removed_status = "unknown"


def write_csv(rows, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["package", "class", "kind", "member", "params", "change",
                    "deprecated", "flag", "modifier_only", "removed_status",
                    "signature", "from_api", "to_api", "class_added_or_removed",
                    "raw_signature"])
        for m, change, fa, ta, cls_flag in rows:
            w.writerow([m.package, m.cls, m.kind, m.name, ",".join(m.params),
                        change, "yes" if m.deprecated else "no", m.flag,
                        "yes" if m.modifier_only else "no", m.removed_status,
                        m.signature, fa, ta,
                        "yes" if cls_flag else "no", m.raw_sig])


def summarize(rows, old, new, old_api, new_api) -> dict:
    by_change = Counter(r[1] for r in rows)
    by_kind = defaultdict(Counter)
    for m, change, *_ in rows:
        by_kind[change][m.kind] += 1

    added_pkgs = Counter(m.package for m, c, *_ in rows if c == "added")
    removed_pkgs = Counter(m.package for m, c, *_ in rows if c == "removed")

    flagged = Counter(c for m, c, *_ in rows if m.flag)
    flagged_by_change = defaultdict(Counter)
    for m, c, *_ in rows:
        if m.flag:
            flagged_by_change[c][m.kind] += 1

    return {
        "from_api": old_api,
        "to_api": new_api,
        "totals": {
            "old_api_surface": len(old),
            "new_api_surface": len(new),
            "changes": len(rows),
            "flagged_changes": sum(flagged.values()),
            "changed_modifier_only": sum(1 for m, c, *_ in rows
                                         if c == "changed" and m.modifier_only),
            "changed_real": sum(1 for m, c, *_ in rows
                                if c == "changed" and not m.modifier_only),
        },
        "by_change": dict(by_change),
        "removed_by_status": dict(Counter(m.removed_status for m, c, *_ in rows
                                          if c == "removed")),
        "by_change_kind": {k: dict(v) for k, v in by_kind.items()},
        "flagged_by_change": {k: sum(v.values()) for k, v in flagged_by_change.items()},
        "top_added_packages": added_pkgs.most_common(15),
        "top_removed_packages": removed_pkgs.most_common(15),
    }


def main() -> int:
    os.makedirs(DATA, exist_ok=True)
    surfaces = {}
    for level in (35, 36, 37):
        path = os.path.join(RAW, f"api{level}.txt")
        if not os.path.exists(path):
            print(f"缺少 {path}，先跑 scripts/download_api.py", file=sys.stderr)
            return 1
        surfaces[level] = parse_file(path)
        print(f"API {level}: 解析出 {len(surfaces[level]):,} 个 API 条目")

    stats = {}
    for a, b in ((35, 36), (36, 37)):
        rows = diff(surfaces[a], surfaces[b], a, b)
        classify_removals(rows, surfaces[a], surfaces[b], load_removed(b))
        out = os.path.join(DATA, f"diff_{a}_{b}.csv")
        write_csv(rows, out)
        st = summarize(rows, surfaces[a], surfaces[b], a, b)
        stats[f"{a}->{b}"] = st
        print(f"API {a}->{b}: {len(rows):,} 条变动 -> {out}")
        print(f"   {st['by_change']}")

    with open(os.path.join(DATA, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    print("统计已写入 data/stats.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
