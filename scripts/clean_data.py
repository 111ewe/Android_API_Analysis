"""Step 3: 清洗 + 去重，产出可直接用于分析的干净数据。

放在 data/clean/ 下，原始 data/diff_*.csv 与 raw/ 一律不改动（留作证据）。

做四件事，每件都有独立计数，最后写进 data/clean/cleaning_log.json：

一、去重
  1. 行内去重：同一个 api_key（包+类+成员+参数）在同一期只允许出现一次。
  2. 跨期去重：同一个 API 在 35→36、36→37 两期都出现时，不再算两次，
     合并成 api_unique.csv 里的一行（分别记两期的状态）。
  3. 类级去重：新增/删除的类只算一条类级记录，其成员不重复计入。
     （build_diff 已按此口径输出，这里只做核查并打上 unit 标记。）

二、修数据
  1. signature 列在类声明行被写成了比较用的中间形态
     （"public final class X :: Parcelable implements"），不可读。
     这里改用 raw_signature 重算一列 display_signature。
  2. 字段的归一化签名会把字符串字面量里的包名吃掉
     （"android.permission.ACCESS_HID" -> "ACCESS_HID"），
     display_signature 保留原值。

三、标噪声
  modifier_only=yes 的"签名改变"只是 metalava 渲染差异（注解类不再打印
  abstract、final 类方法去掉多余 final），不是真变化，标 noise=yes。

四、复核假删除
  被判定 removed 的成员，回查新版本原文：
  若同一个成员（同名、同参数、同类型）出现在**该类的父类型**里，说明它只是
  上移到父类，子类照样能调用，不算真删除。这类标 removal_verdict=hoisted_to_parent，
  substantive=no，不计入"移除"统计。

  注意：只认"父类型"这一条。一开始也试过"出现在别的类就算搬家"，结果
  getTag()、getExpectedPresentationTimeNanos() 这类通用名在无关类里撞车，
  把 2 条真删除（都是 @FlaggedApi 开关撤回）误判成搬家。同名巧合不算证据，
  因此 other_class_same_sig 只作线索记录，不改变判定。

用法：python scripts/clean_data.py
输出：
  data/clean/api_events.csv   一条变动一行（含去重键、噪声标记、实质标记）
  data/clean/api_unique.csv   一个 API 一行（跨期合并后的唯一视图）
  data/clean/cleaning_log.json 每步的处理条数
"""
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_diff  # 复用已通过校验的解析器，保证 API 身份口径一致

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.environ.get("DIFF_OUT") or os.path.join(ROOT, "data")
CLEAN = os.path.join(DATA, "clean")
RAW = os.path.join(ROOT, "raw")

PERIODS = [("35->36", 35, 36), ("36->37", 36, 37)]


def read_diff(a, b):
    path = os.path.join(DATA, f"diff_{a}_{b}.csv")
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def api_key_of(row):
    """与 build_diff.Member.key 完全一致的 API 身份串。"""
    head = f"{row['package']}.{row['class']}"
    kind = row["kind"]
    if kind == "class":
        return f"{head}#class"
    if kind in ("field", "enum_constant"):
        return f"{head}#{kind}:{row['member']}"
    return f"{head}#{kind}:{row['member']}({row['params']})"


def api_id_of(key):
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]


def display_sig(row):
    """人类可读的签名：优先用原文（只剥过注解），退回归一化签名。"""
    raw = (row.get("raw_signature") or "").strip()
    sig = (row.get("signature") or "").strip()
    text = raw or sig
    # 类声明行若回落到归一化签名，去掉比较用的 ":: 排序" 中间形态
    if " :: " in text:
        head, tail = text.split(" :: ", 1)
        kw = "implements" if "implements" in tail else "extends"
        body = tail.replace("implements", "").replace("extends", "").split()
        body = [w for w in body if w not in ("implements", "extends")]
        text = f"{head} {kw} {' '.join(body)}".strip()
    return text


def _split_class_decl(raw: str):
    """从类声明原文里取 (父类, [接口...])。

    metalava 的类声明长这样：
        public final class Bundle extends android.os.BaseBundle implements Cloneable android.os.Parcelable
    两个必须注意的地方：
      - extends 之后只跟一个类型；
      - implements 之后的接口是用**空格**分隔的，不是逗号。
    """
    decl = (raw or "").replace(" :: ", " ")
    sup = ""
    ifaces = []
    m = re.search(r"\bextends\s+(.+?)(?=\s+implements\b|\s+permits\b|$)", decl)
    if m:
        sup = m.group(1).strip()
    m = re.search(r"\bimplements\s+(.+?)(?=\s+permits\b|$)", decl)
    if m:
        ifaces = [t for t in m.group(1).split() if t]
    return sup, ifaces


def _fq(name: str, package: str) -> str:
    """补成全限定名：原文带点的原样保留，只写简名的按当前包补。"""
    name = (name or "").strip()
    return name if "." in name else f"{package}.{name}"


def class_parents(surface):
    """包.类 -> 直接父类型（全限定名）集合，用于判断"上移到父类"。

    以前这里有两个毛病：implements 取错了段（拿到的是 extends 那段，接口全丢），
    父类型又只留简名，别的包里同名类会被误认成父类。现在一律用全限定名。
    """
    parents = {}
    for m in surface.values():
        if m.kind != "class":
            continue
        sup, ifaces = _split_class_decl(m.raw_sig or m.signature or "")
        ps = {_fq(t, m.package) for t in ([sup] if sup else []) + ifaces}
        parents[f"{m.package}.{m.cls}"] = ps
    return parents


def super_chain(cls_fq, parents, limit=40):
    """类自身 + 沿 extends/implements 一路向上的所有父类型（全限定名）。"""
    seen, stack = set(), [cls_fq]
    while stack and limit > 0:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for p in parents.get(cur, ()):
            if p not in seen:
                stack.append(p)
        limit -= 1
    return seen


def _sig_skeleton(sig: str, name: str) -> str:
    """签名去掉成员名和参数后剩下的部分（修饰符 + 返回类型）。

    用来验证"同名同参数"是不是真同一个 API：返回类型或可见性不同就不是。
    """
    head = (sig or "").split("(")[0].strip()
    idx = head.rfind(name) if name else -1
    if idx > 0:
        return head[:idx].strip()
    return head


def find_member_elsewhere(row, surface):
    """在新版本里按 (kind, 成员名, 参数) 找这个成员现在挂在哪个类下。

    只比名字和参数是不够的——返回类型不同的同名方法不是同一个 API。
    这里再加上"修饰符 + 返回类型"必须一致，才算候选。
    """
    hits = set()
    want = _sig_skeleton(row.get("signature") or row.get("raw_signature") or "", row["member"])
    for m in surface.values():
        if m.kind != row["kind"]:
            continue
        if m.name != row["member"]:
            continue
        if ",".join(m.params) != row["params"]:
            continue
        if want and _sig_skeleton(m.signature, m.name) != want:
            continue
        hits.add(f"{m.package}.{m.cls}")
    return hits


def main() -> int:
    os.makedirs(CLEAN, exist_ok=True)
    log = {"input": {}, "dedup": {}, "fix": {}, "noise": {}, "removal_check": {}}

    surfaces = {}
    for level in (35, 36, 37):
        surfaces[level] = build_diff.parse_file(os.path.join(RAW, f"api{level}.txt"))

    events = []
    for period, a, b in PERIODS:
        rows = read_diff(a, b)
        log["input"][period] = len(rows)

        # ---- 一.1 行内去重 ----
        seen, dropped = Counter(), 0
        for r in rows:
            k = api_key_of(r)
            seen[k] += 1
        same_period_dup = sum(v - 1 for v in seen.values() if v > 1)
        log["dedup"][period] = {
            "rows_in": len(rows),
            "unique_api_keys": len(seen),
            "intra_period_duplicates": same_period_dup,
        }

        parents_new = class_parents(surfaces[b])
        new_surface = surfaces[b]

        for r in rows:
            key = api_key_of(r)
            change = r["change"]
            modifier_only = r["modifier_only"] == "yes"

            # ---- 四. 假删除复核 ----
            verdict = ""
            elsewhere_note = ""
            substantive = True
            if change == "removed":
                # 先看这条"删除"有多硬。removed_status 是跟 Google 自己的
                # removed.txt 对账出来的：
                #   confirmed  官方也记了这次删除          -> 算实质
                #   flagged    是开关（@FlaggedApi）撤回   -> 算实质
                #   moved_out  整个包离开了本签名文件       -> 不算：可能被挪到别的
                #              签名文件（telecom/vcn 就是这样），不是 API 没了
                #   unknown    对不上号，无法证实           -> 不算：没有证据就不下结论
                # 这三类分开列着，别把它们合并成一句"删了 N 个"。
                if r["removed_status"] in ("moved_out", "unknown"):
                    substantive = False
                elsewhere = find_member_elsewhere(r, new_surface)
                me_fq = f"{r['package']}.{r['class']}"
                elsewhere.discard(me_fq)
                if elsewhere:
                    chain = super_chain(me_fq, parents_new)
                    if elsewhere & chain:
                        # 父类型里有同名同参数同返回类型的成员 -> 只是上移，调用方无感
                        verdict = "hoisted_to_parent"
                        substantive = False
                    else:
                        # 同名巧合不算搬家，仅留线索供人工复核（展示仍用简名）
                        simple = sorted(t.rsplit(".", 1)[-1] for t in elsewhere)
                        elsewhere_note = ";".join(simple[:3])

            # ---- 三. 噪声 ----
            noise = ""
            if modifier_only and change == "changed":
                noise = "modifier_render_only"
                substantive = False

            # ---- 二. 修签名 ----
            # 类声明在比较时被压成 "public final class X :: Y extends" 这种中间形态，
            # 展示时要还原。但只加注解的 deprecated 事件签名本来没变，
            # 不存在"重建"这回事，别给它打标签。
            disp = display_sig(r)
            fixed = ""
            if " :: " in (r["signature"] or ""):
                # 只加注解的 deprecated 事件，签名本来没变，不存在"重建"
                if change != "deprecated":
                    fixed = "class_decl_rebuilt"
            elif (r["raw_signature"] or "") != (r["signature"] or ""):
                fixed = "used_raw_literal"

            # 归一化会不会把引号里的值也改掉（只对字段常量有意义）
            q_raw = re.findall(r'"([^"]*)"', r["raw_signature"] or "")
            q_sig = re.findall(r'"([^"]*)"', r["signature"] or "")
            literal_rewritten = "yes" if (q_raw and q_sig and q_raw != q_sig) else "no"

            events.append({
                "period": period,
                "api_id": api_id_of(key),
                "api_key": key,
                "package": r["package"],
                "class": r["class"],
                "full_name": f"{r['package']}.{r['class']}",
                "unit": "class" if r["kind"] == "class" else "member",
                "kind": r["kind"],
                "member": r["member"],
                "params": r["params"],
                "change": change,
                "category": {
                    "added": "结构新增",
                    "removed": "结构删除",
                    "changed": "签名变更",
                    "deprecated": "废弃宣告",
                }.get(change, change),
                "display_signature": disp,
                "signature_fix": fixed,
                "literal_rewritten": literal_rewritten,
                "deprecated": r["deprecated"],
                "flagged": "yes" if r["flag"].strip() else "no",
                "flag_name": r["flag"],
                "noise": noise,
                "substantive": "yes" if substantive else "no",
                "count_structural": "1" if (substantive and change in ("added", "removed", "changed")) else "0",
                "removal_verdict": verdict,
                "other_class_same_sig": elsewhere_note,
                "removed_status": r["removed_status"],
            })

    log["dedup"]["intra_period_total"] = sum(v["intra_period_duplicates"] for v in log["dedup"].values())

    # ---- 补 since_api：这个 API 最早在哪个版本就存在了 ----
    # 只有 35/36/37 三个版本，所以"35 里就有"只知道一个上界——最迟在 35 版已存在，
    # 可能更早。写成 <=35 + upper_bound（之前误写成 lower_bound，方向是反的）。
    # 36/37 是在本次观察窗口里首次出现，记 window_first_seen：
    # 没查更老的历史，不能断言它是绝对首次引入（理论上可能删了又加回来）。
    for e in events:
        k = e["api_key"]
        in35, in36, in37 = k in surfaces[35], k in surfaces[36], k in surfaces[37]
        if in35:
            e["since_api"], e["since_precision"] = "<=35", "upper_bound"
        elif in36:
            e["since_api"], e["since_precision"] = "36", "window_first_seen"
        else:
            e["since_api"], e["since_precision"] = "37", "window_first_seen"
        e["last_seen_api"] = "37" if in37 else ("36" if in36 else "35")
        e["in_api37"] = "yes" if in37 else "no"

    # ---- 一.2 跨期去重 ----
    by_key = defaultdict(list)
    for e in events:
        by_key[e["api_key"]].append(e)
    cross = {k: v for k, v in by_key.items() if len({e["period"] for e in v}) > 1}
    log["dedup"]["cross_period_apis"] = len(cross)
    log["dedup"]["events_total"] = len(events)
    log["dedup"]["unique_apis"] = len(by_key)

    # ---- 统计各步处理量 ----
    log["fix"]["class_decl_rebuilt"] = sum(1 for e in events if e["signature_fix"] == "class_decl_rebuilt")
    log["fix"]["used_raw_literal"] = sum(1 for e in events if e["signature_fix"] == "used_raw_literal")
    log["fix"]["literal_rewritten"] = sum(1 for e in events if e["literal_rewritten"] == "yes")
    log["fix"]["abbrev_only"] = sum(1 for e in events if e["signature_fix"] == "used_raw_literal") - log["fix"]["literal_rewritten"]
    log["fix"]["empty_display"] = sum(1 for e in events if not e["display_signature"])
    log["noise"]["modifier_render_only"] = sum(1 for e in events if e["noise"] == "modifier_render_only")
    log["removal_check"]["hoisted_to_parent"] = sum(1 for e in events if e["removal_verdict"] == "hoisted_to_parent")
    log["removal_check"]["same_name_hint_only"] = sum(1 for e in events if e["other_class_same_sig"])
    # 删除事件只报两个数：计入的、不计入的。判据就是 count_structural，和 CSV 用同一列，
    # 不再用"removal_verdict 为空"——moved_out / unknown 那一列也是空的，会多算 59 条。
    log["removal_check"]["counted_removal"] = sum(
        1 for e in events if e["change"] == "removed" and e["count_structural"] == "1")
    log["removal_check"]["excluded_removal"] = sum(
        1 for e in events if e["change"] == "removed" and e["count_structural"] == "0")

    # ---- 写 api_events.csv ----
    # 固定按 (期次, api_key) 排序再落盘：同一份数据每次跑出来的行序都一样，
    # 才可以用 sha256 判断"重跑结果是否与交付文件一致"。
    events.sort(key=lambda e: (e["period"], e["api_key"]))
    cols = ["period", "api_id", "api_key", "package", "class", "full_name", "unit",
            "kind", "member", "params", "change", "category", "display_signature",
            "signature_fix", "literal_rewritten", "since_api", "since_precision",
            "last_seen_api", "in_api37", "deprecated", "flagged", "flag_name", "noise",
            "substantive", "count_structural", "removal_verdict",
            "other_class_same_sig", "removed_status"]
    with open(os.path.join(CLEAN, "api_events.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(events)

    # ---- 写 api_unique.csv（一个 API 一行）----
    pres = {lvl: surfaces[lvl] for lvl in (35, 36, 37)}
    uniq = []
    for key, evs in by_key.items():
        first = evs[0]
        per = {e["period"]: e for e in evs}
        obj = build_diff.Member(package=first["package"], cls=first["class"],
                                kind=first["kind"], name=first["member"],
                                params=tuple(p for p in first["params"].split(",") if p))
        present = {lvl: obj.key in pres[lvl] for lvl in (35, 36, 37)}
        structural = any(e["count_structural"] == "1" for e in evs)
        uniq.append({
            "api_id": first["api_id"],
            "api_key": key,
            "full_name": first["full_name"],
            "unit": first["unit"],
            "kind": first["kind"],
            "member": first["member"],
            "params": first["params"],
            "in_api35": "yes" if present[35] else "no",
            "in_api36": "yes" if present[36] else "no",
            "in_api37": "yes" if present[37] else "no",
            "p35_36": per["35->36"]["change"] if "35->36" in per else "",
            "p36_37": per["36->37"]["change"] if "36->37" in per else "",
            "p35_36_verdict": per["35->36"]["removal_verdict"] if "35->36" in per else "",
            "p36_37_verdict": per["36->37"]["removal_verdict"] if "36->37" in per else "",
            "n_periods": len({e["period"] for e in evs}),
            "has_structural_change": "yes" if structural else "no",
            "since_api": ("<=35" if present[35] else ("36" if present[36] else "37")),
            "since_precision": "upper_bound" if present[35] else "window_first_seen",
            "last_seen_api": "37" if present[37] else ("36" if present[36] else "35"),
            "state_in_37": "present" if present[37] else "gone",
        })
    uniq.sort(key=lambda r: (r["full_name"], r["kind"], r["member"], r["params"]))
    with open(os.path.join(CLEAN, "api_unique.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(uniq[0].keys()))
        w.writeheader()
        w.writerows(uniq)
    log["dedup"]["unique_rows_written"] = len(uniq)

    # ---- 自检 ----
    checks = []
    checks.append(("事件表与原两表行数一致",
                   len(events) == sum(log["input"].values()), f"{len(events)}"))
    checks.append(("同期内无重复 api_key", log["dedup"]["intra_period_total"] == 0,
                   str(log["dedup"]["intra_period_total"])))
    dup_uid = len(uniq) - len({r["api_id"] for r in uniq})
    checks.append(("唯一表中 api_id 不重复", dup_uid == 0, str(dup_uid)))
    checks.append(("展示签名无 '::' 残留",
                   not any(" :: " in e["display_signature"] for e in events),
                   str(sum(1 for e in events if " :: " in e["display_signature"]))))
    checks.append(("展示签名非空", log["fix"]["empty_display"] == 0, str(log["fix"]["empty_display"])))
    checks.append(("实质变动数 = 非噪声结构变动",
                   sum(int(e["count_structural"]) for e in events) ==
                   sum(1 for e in events if e["substantive"] == "yes" and e["change"] != "deprecated"),
                   str(sum(int(e["count_structural"]) for e in events))))
    log["checks"] = [{"name": n, "pass": p, "value": v} for n, p, v in checks]

    with open(os.path.join(CLEAN, "cleaning_log.json"), "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    # ---- 控制台摘要 ----
    print("=== 清洗结果 ===")
    for period in log["input"]:
        d = log["dedup"][period]
        n = log["noise"]["modifier_render_only"]
        print(f"{period}: 输入 {d['rows_in']} 条，唯一 API {d['unique_api_keys']} 个，"
              f"同期重复 {d['intra_period_duplicates']} 条")
    print(f"跨期重复 API: {log['dedup']['cross_period_apis']} 个 -> 合并后唯一 API {len(uniq)} 个")
    print(f"噪声行(渲染差异): {log['noise']['modifier_render_only']} 条")
    print(f"假删除: 上移到父类 {log['removal_check']['hoisted_to_parent']} 条，"
          f"同名巧合待人工看 {log['removal_check']['same_name_hint_only']} 条")
    # 移除必须按期报，合计会把两期的数混成一个，容易误读。
    # 口径与 CSV 完全一致：只看 count_structural，不再用 removal_verdict 是否为空判断。
    for period in log["input"]:
        rem = [e for e in events if e["period"] == period and e["change"] == "removed"]
        counted = [e for e in rem if e["count_structural"] == "1"]
        excl = {}
        for e in rem:
            if e["count_structural"] == "1":
                continue
            why = e["removal_verdict"] or e["removed_status"] or "其他"
            excl[why] = excl.get(why, 0) + 1
        detail = "、".join(f"{k} {v}" for k, v in sorted(excl.items())) or "无"
        print(f"  {period} 移除: 原文 {len(rem)} 条 -> 计入结构事件 {len(counted)} 条，"
              f"不计入 {len(rem) - len(counted)} 条（{detail}）")
    print(f"  两期合计：移除 {log['removal_check']['counted_removal']} 条计入、"
          f"{log['removal_check']['excluded_removal']} 条不计入")
    print(f"签名修复: 类声明重建 {log['fix']['class_decl_rebuilt']} 条，"
          f"改用原文 {log['fix']['used_raw_literal']} 条")
    print("--- 自检 ---")
    for c in log["checks"]:
        print(("  ✅ " if c["pass"] else "  ❌ ") + c["name"] + f" ({c['value']})")
    return 0 if all(c["pass"] for c in log["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
