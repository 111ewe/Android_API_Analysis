# 我负责的部分：抓数据 + 清洗数据

一句话：从 AOSP 官方仓库把 API 35 / 36 / 37 的 API 签名原文抓下来，清洗成 6 张能直接分析的 CSV。
包里所有数字都能用 `raw/` 那 6 个文件重跑出来，不需要联网。

---

## 一、抓数据

### 抓的是什么

Android 每个版本都会发布一份"这一版对外暴露了哪些 API"的签名清单，由 Google 的 metalava 工具生成：

| 文件 | 是什么 | 用途 |
|---|---|---|
| `core/api/current.txt` | 当前版本的公开 API 快照（全部类和成员，带 `@Deprecated`、`@FlaggedApi` 等注解） | 清洗的原料 |
| `core/api/removed.txt` | Google 自己记录的"历史上被删掉的公开 API"累计清单 | 交叉验证我们算出来的"删除"是真删还是文件边界变化 |

三个版本分别取 AOSP 的三个 tag：

- API 35 → `android-15.0.0_r1`
- API 36 → `android-16.0.0_r1`
- API 37 → `android-17.0.0_r1`

地址：`https://android.googlesource.com/platform/frameworks/base/+/<tag>/core/api/current.txt`（`?format=TEXT` 取 base64）。

### 抓下来是什么样

`raw/` 里 6 个文件，是官方仓库的**原字节**——二进制写入，没做任何换行转换，可以直接拿下面的 sha256 跟 AOSP 对：

| 文件 | 行数 | 字节数 | sha256 前 16 位 |
|---|---|---|---|
| api35.txt | 62,538 | 4,382,010 | `a07b0a1df1b7aeeb` |
| api36.txt | 64,084 | 4,540,786 | `1f84aa28d5e59cd4` |
| api37.txt | 64,980 | 4,657,449 | `116c793ecd67c793` |
| removed35.txt | 566 | 28,778 | `28c869a9855aca65` |
| removed36.txt | 566 | 29,138 | `13b89961ee12fb1c` |
| removed37.txt | 616 | 32,858 | `23d84564ea1586e9` |

> 这里踩过一个坑：用文本模式 `open(..., "w")` 写，Windows 会把每个 `\n` 换成 `\r\n`，六份文件各多出几千到六万多个字节，sha256 就跟官方对不上了（api35 会从 4,382,010 变成 4,444,548，多的 62,538 正好是行数）。现在 `download_api.py` 一律二进制写入，raw/ 与官方逐字节相同。

文件的缩进是有含义的：`package` 顶格，类缩进 2 格，成员缩进 4 格。清洗就是靠这个层级还原"哪个成员属于哪个类"。

### 三个必须说清楚的边界

**1. `current.txt` 不是完整的 `android.jar`。**

它是"公开 API 的签名子集"——只有类和成员的声明，没有方法体，也不含 `@hide` 的内部 API。

更具体地说：37 版这份文件覆盖 **155 个包**，但完整 SDK 里还有这里没有的类。实测 `android.telecom.Call`、`android.security.NetworkSecurityPolicy`、`android.bluetooth.BluetoothDevice`、`android.location.LocationManager`、`android.net.wifi.WifiManager` 在官方 API 37 的 `android.jar` 里存在，却不在本表。

所以准确的名字是：**"所选 AOSP 标签的 frameworks/base/core/api 签名快照（155 个包，排除 android.R）"**，不是"Android SDK 的全部公开 API"。凡是"从这套表里查不到"的判断，只能说明"不在本签名文件里"，不能说明"公开 SDK 没有"。

**2. `current.txt` 的覆盖面本身会变。**

某个包在新版里整包不出现在这个文件里，不代表这个包的 API 被删了。

- **35→36** 少了 `android.net.vcn`（11 个类、95 条声明）
- **36→37** 少了 `android.telecom`（45 个类、1,065 条声明）、`android.app.contextualsearch`（1 个类、2 条）

这两批共 57 条，在 37 版的 `current.txt` 里没出现、`removed.txt` 里也没有对应记录，因此标成 `removed_status=moved_out`，不计入实质删除。

**注意 `android.hardware.fingerprint` 是另一回事。** 它同样整包不见了（4 个类、36 条），但 Google 的 `removed.txt` 里明确记着这批删除，所以它的 4 个类连同 `Context.FINGERPRINT_SERVICE` 一共 5 条被标成 `confirmed`，是计入的。**"整包不见"只是一条线索，不能单凭它断定是换文件还是真删**——证据要看 `removed.txt`。

所以"删除"这件事我们一律回查 `removed.txt` 和 `raw/` 原文，不直接信差集。

**3. 版本口径：只取三个整数主 API level。**

35 / 36 / 37，不含 36.1 这类小版本和扩展 SDK。要谈"三版演化"，先说清这条边界。

---

## 二、清洗数据

原始文件是给人看的 Java 签名，不是给程序算的表。要变成能统计的数据，得做四件事，对应四个脚本。

### 第 1 步 拆：给每个 API 一个身份证号（`build_diff.py`）

一行签名 `method public void foo(int a, String b)` 拆成：

```
包名    android.app
类名    Activity
成员名   foo
参数     int,String
返回类型 void
```

再拼成一个唯一身份证（`api_key`）：

- 类：`android.app.Activity#class`
- 字段：`android.app.Activity#field:MODE_X`
- 方法：`android.app.Activity#method:foo(int,String)`
- 构造：`android.app.Activity#ctor:(Context)`

参数写进身份证，所以"参数列表变了"会自然表现为"删了一个旧的 + 加了一个新的"，不会被误算成"改了一个"。

### 第 2 步 归一：让三个版本能用同一把尺子量（`clean_surface.py`）

这一步最容易出错。API 37 的 metalava 是新版本，写法和 35/36 不一样：

- 类型名从 `java.util.Map` 变成 `Map`
- 参数逗号后不加空格 `f(int,String)` 变成 `f(int, String)`
- 注解类的成员不再打印 `abstract`
- `final` 类的方法不再重复打印 `final`

不归一的话，37 版会凭空冒出几千条"签名变了"，其实一个字都没改。归一之后，36→37 真正的签名变化是 1 条，其余 21 条只是排版差异（在表里标 `modifier_only=yes` / `noise=yes`）。

九条清洗规则，每条都在 `surface_log.json` 里记了触发次数，可以复算：

1. 剥注解（`@Nullable` / `@NonNull` / `@FlaggedApi` …），但 **`@interface` 必须保留**——它是"声明一个注解类型"的关键字，剥掉整类就没了
2. 类级 `@FlaggedApi` 下传给成员（成员行自己不写注解，不代表它没开关）
   —— 注意 `@FlaggedApi` 只表示"这个 API 受功能开关管理"，**不等于**"默认关闭"或"尚未开放"。具体某个构建里开关是开是关、是否导出到 SDK，要另查构建配置。
3. 类型名归一：`java.util.Map` → `Map`
4. 空白归一：`f( int)` → `f(int)`、`a,b` → `a, b`
5. 跨行签名按括号配平拼回去
6. 剥行尾 `//` 注释（不动字符串里的 `//`）
7. 排除 `android.R` 资源编号类（三版分别排除 2,916 / 2,965 / 3,010 条声明）
   —— 这是**研究范围的选择**，不是"资源符号不算 API"。公开资源符号的新增和弃用也可能影响兼容性，只是本研究不覆盖，所以把条数写在这里，谁要用可以自己加回来。
8. 同版本内同 `api_key` 去重，保留第一次出现并记 `line_no`
9. 字段字面量拆进 `value` 列（常量值不能被归一化改写）

另外两个踩过的坑，已经在脚本里堵上：

- **`throws` 子句和注解成员的 `default` 值**必须单独成列。它们只存在于签名串里，光靠"字段拼回去"会丢，导致签名对不上。
- **字符串字面量要先保护再归一**。否则 `"android.permission.ACCESS_HID"` 会被当成全限定类名啃成 `"ACCESS_HID"`，常量值被悄悄改写。

### 第 3 步 清洗变动表（`clean_data.py`）

变动表里有两类"假数据"要处理掉：

- **噪声**：21 条"签名改变"其实只是 metalava 排版差异，标 `noise=yes`，不算真变化
- **假删除**：73 条被判"删除"的成员里，有 3 条其实是被上移到了父类，子类照样能调，标 `removal_verdict=hoisted_to_parent`

同时做去重：同一个 API 在两期都变动时，合并成 `api_unique.csv` 里的一行，不重复计数。

> 只认"父类型"这一条判断。一开始也试过"出现在别的类就算搬家"，结果 `getTag()` 这种通用名在无关类里撞车，把 2 条真删除误判成搬家。同名巧合不算证据。

### 第 4 步 读回类级废弃（这是修过的一个真 bug）

类被整类标 `@Deprecated` 时，注解写在**类那一行**，成员行上什么都没有。原来的脚本在类行把 `deprecated` 写死成 `False`，结果：

- 三版里 353 / 355 / 377 个"废弃类"全被记成"没废弃"
- 36→37 有 26 个类新被废弃，事件表里一条都查不到

修法是把类行的 `@Deprecated` 读出来，并且在比对时给类级废弃单独记一条事件（原来类只比签名，命中就 `continue` 了）。修完之后：

- 三版废弃总数 5,677 / 5,775 / 5,870
- 事件数 804（35→36）、1,233（36→37）

---

## 三、产出

`data/clean/` 里 6 张表：

| 文件 | 行数 | 一行是什么 |
|---|---|---|
| api_surface_35.csv | 52,321 | 35 版的一个 API（类/方法/字段/构造/枚举常量） |
| api_surface_36.csv | 53,610 | 36 版的一个 API |
| api_surface_37.csv | 54,272 | 37 版的一个 API |
| api_surface_all.csv | 55,536 | 一个 API 一行，带三版存在性（跨版本看"什么时候来、什么时候走"） |
| api_events.csv | 2,037 | 一条变动（added / removed / changed / deprecated） |
| api_unique.csv | 2,018 | 一个变过的 API 一行，两期状态合并 |

列的含义见 `字段说明.md`。中间产物 `data/diff_*.csv`（没清洗的原始变动）也留着，方便追溯每一步改了什么。

---

## 四、怎么复现

```bash
python scripts/run_all.py            # 全跑：抓 → 拆 → 归一 → 清洗
python scripts/run_all.py --refetch  # 连 raw/ 也重新下载
```

`raw/` 已存在时默认跳过下载，所以断网也能复现清洗结果。四个脚本也能单独跑，顺序是
`download_api.py` → `build_diff.py` → `clean_surface.py` → `clean_data.py`。

依赖只有 Python 3.9+ 标准库，不需要装任何第三方包。脚本里用了 `Member | None` 这类类型标注，文件头加了 `from __future__ import annotations` 让注解不在运行时求值，所以 3.9 上也能跑；实际验证用的是 3.13。

### 重跑核对结果

用包里的 `raw/` 从零重跑一遍，六张表与交付文件**逐字节相同**（sha256 一致），不是"大致对得上"：

| 文件 | sha256 前 16 位 |
|---|---|
| api_surface_35.csv | `98873867db6306c7` |
| api_surface_36.csv | `c0c3e6cb61cbdca1` |
| api_surface_37.csv | `6c4f8d024742ebe7` |
| api_surface_all.csv | `97bafed3ce06c1d3` |
| api_events.csv | `d17d70f35aec5787` |
| api_unique.csv | `ce0bf45ec4c3fd8b` |

完整值在 `sha256.txt` 里。

---

## 五、读表前先看一眼的三个口径

### 1. 「实质结构变动 1,702」不是「平台删了多少 API」

`count_structural` 求和是 1,702，拆开看是：

- 新增 **1,669**
- 真删除 **32**
- 真签名变化 **1**（另有 21 条只是 metalava 排版差异，标 `noise`，不计）

表里一共 94 条「删除」，只有 32 条算数。剩下 62 条这样处理：

| 期次 | `moved_out` 整包离开本文件 | `flagged` 开关撤回 | `confirmed` 官方确认删除 | `unknown` 对不上号 |
|---|---|---|---|---|
| 35→36 | 11（不计） | 10（计） | 0 | 0 |
| 36→37 | 46（不计） | 17（计） | 5（计） | 5（不计） |

> **为什么 `moved_out` 不计**：35→36 的 `android.net.vcn`（11 个类）、36→37 的 `android.telecom` 等（45 个类），都是整包从 `current.txt` 里消失，属于文件覆盖面的变化，不是 API 消失。
>
> **为什么 `unknown` 不计**：5 条里有 3 条是成员被上移到父类（`hoisted_to_parent`，API 还在），另 2 条连 Google 的 `removed.txt` 都对不上号。没有证据就不下结论。
>
> **`confirmed` 是怎么判的**：成员按「名字 + 参数」对齐，`q(String)` 的删除记录不会算到 `q(int)` 头上；类级更严——`removed.txt` 里出现类标题只说明"这个类被删过东西"，必须旧版这个类的**所有**成员都在里面，才算整类被删（fingerprint 那 4 个类正好全部成员都对上了）。
>
> 所以这 1,702 是「本签名快照里的结构事件」，不等于「开发者必须改代码的 API 数」。
>
> 那 32 条（计入的移除）又是什么？拆开是 **27 条原本带 `@FlaggedApi` 的移除 + 5 条与 `removed.txt` 对应的记录**。`@FlaggedApi` 只说明"这个 API 受功能开关管理"，不能反推出"它曾经默认关闭"或"它曾经是稳定 SDK 的一部分"。所以准确的说法是：
>
> **按本研究计数规则保留 32 条移除事件，对稳定公开 SDK 和应用兼容性的实际影响，需要逐项再补证据。**
>
> 另外计数里既有类事件也有成员事件——删掉一个类只记 1 条，不是把受影响的调用点都数出来。另外这 1,702 里有 1,660 条带开关标记，都不宜直接当成「稳定 SDK 的演化数量」。

### 2. `since_precision`：`<=35` 是上界，不是下界

- `upper_bound`：只知道「最迟在 35 版就已经存在」，可能更早，所以 `since_api` 写 `<=35`
- `window_first_seen`：在本次观察窗口（35/36/37）里第一次见到。没查更老的历史，不能断言它是绝对首次引入
- 三版合并表的 `first_api` 同理，读作「首次观察到的版本」，不是「问世的版本」

### 3. 这套表没有覆盖的东西

- **只有签名，没有文档。** 权限、线程要求、空值约束、替代方案，这些都在 Javadoc 里，得另找人补
- **注解变化没有独立维度。** 表里只留了 `deprecated` / `flagged`，`@Nullable`、`@RequiresPermission` 这类注解被剥掉了。所以「某个方法签名没变、但空值约束变了」这种变化，这张表看不出来
- **只到「结构事件」这一层。** 功能语义上的演化（同一个 API 行为变了、推荐用法变了）需要人工读文档，数据管道给不了

---

## 六、目录

```
交付_抓取与清洗/
├─ README.md              ← 本文件
├─ 字段说明.md             ← 6 张表逐列解释
├─ raw/                   ← 抓下来的原文，6 个文件，不改
├─ data/
│  ├─ diff_35_36.csv      ← 中间产物：未清洗的变动
│  ├─ diff_36_37.csv
│  ├─ stats.json          ← 两期变动汇总
│  └─ clean/              ← 交付的 6 张表 + surface_log.json（清洗动作计数）
├─ scripts/               ← 4 个脚本 + 一键入口
└─ sha256.txt             ← 全部文件的指纹，用来确认传输没坏
```
