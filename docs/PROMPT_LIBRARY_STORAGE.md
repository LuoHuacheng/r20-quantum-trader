# 提示词方案库存储设计（切换为何改写文件 · 单文件 vs 一方案一文件）

> **状态：A / B / C 三案均已落地（v8.3.1 工作树）。**
> 本页第 1~4 节是 v8.3.0 时点的**实测现象与根因分析**，保留原样作为问题档案；
> 第 5~7 节的原建议在 **[§9 落地记录](#9-落地记录实测)** 里逐条对账（含 5 处与建议的偏差
> 及理由）。第 8 节锚点已按落地后的代码行号刷新。
>
> **适用对象**
> - `scripts/prompt_library.py` —— 方案库读写门面（`LIBRARY_FILE` / 目录存储 / CRUD / 校验 / 导入导出）
> - `r20_backend/routers/strategy/prompts.py` —— 管理后台端点（方案列表 / 切换 / 保存 / 历史 / 回滚）
> - `r20_backend/policy/{capture,restore,fingerprints}.py` —— 策略快照 / 恢复 / 指纹
>
> **锚点约定**：函数名是稳定锚点；行号是 v8.3.1 落地时的快照，会随后续改动漂移。

---

## 0. 结论速览

| 观察/主张 | 是否成立 | 说明 | 落地对策 |
|---|---|---|---|
| 切换提示词方案会改写 `data/prompt_library.json` | **成立** | 实测切换一次改动 3 个字段 | A① 只改指针（1 行；目录存储下 = 只改 `index.json`） |
| 改写的字段里包含**与切换无关**的方案内容 | **成立** | `stable` 的派生缓存被全库重算 | A① + A②（内容未变的方案沿用磁盘原文） |
| 这会改变交易行为 | **不成立** | 被重算的是运行时**从未被读取**的派生缓存 | 已用「切换前后每方案 `resolve_profile` 逐字节一致」钉住 |
| "改成多份文件（一方案一 JSON）"能消掉这条 diff | **不成立** | 只要全库重算逻辑不变，切换照样改写方案文件 | 故 B 在 A 之后落地（A 消掉因果，B 给粒度） |
| 该文件被 git 跟踪这件事本身合理 | **不成立** | 运行时可变状态被按发布物管理 | C：`git rm --cached` + `.gitignore` |
| 正确的最小对策 | 见 §6 | A + C 先行，B 随后 | 见 §9 |

---

## 1. 现象

管理后台在提示词工作室里**切换方案**后，`data/prompt_library.json` 出现一条很大的未提交 diff：

```
.active_profile_id:                      变化 6  -> 16  字符
.profiles.stable.trading_system:         变化 298 -> 6490 字符
.profiles.stable.trading_user:           变化 401 -> 993  字符
```

- `pipelines` 结构**一个字都没变**；
- 切换的动作只是把 `active_profile_id` 从 `stable` 换成 `wide_oscillation`。

也就是说：**切换 A 方案，改掉了 B 方案的内容。**

---

## 2. 复现（隔离沙箱，不触碰真实数据）

用 HEAD 版本的文件在 `/tmp` 副本上执行一次真实切换：

```python
import json, pathlib, sys
sys.path.insert(0, '.')
import scripts.prompt_library as pl
pl.LIBRARY_FILE = pathlib.Path('/tmp/pl_sandbox/data/prompt_library.json').resolve()
pl.activate_profile('wide_oscillation')
```

结果（v8.3.0 / 修复前）：

```
before active = stable    after active = wide_oscillation
  [内容被改写] stable.trading_system: 298 -> 6490 字符
  [内容被改写] stable.trading_user:   401 -> 993 字符
```

修复后同一条复现的输出（实测，`difflib` 逐行比对）：

```
before active = stable    after active = wide_oscillation
  [diff] -  "active_profile_id": "stable",
  [diff] +  "active_profile_id": "wide_oscillation",
```

即**只有指针那一行**变了；同一份复现已固化成用例（`tests/llm/test_prompt_library_storage.py`）。

---

## 3. 根因（三层，缺一不可）

### 3.1 切换走了「全库规范化」写路径

```
activate_profile()            scripts/prompt_library.py
  └─ save_library(library)
       └─ _migrate(payload)
            └─ _clean_profile(每个 profile)
                 └─ result[key] = compile_modules(...)   ← 扁平文本被无条件重算
```

`_clean_profile` 结尾对 `editor_mode == "modules"` 的方案一律重算扁平文本。因此**任何**写操作
都会重写**所有**方案。全部 8 个写入口无一例外：`create_profile` / `update_profile` /
`delete_profile` / `activate_profile` / `rollback_profile` / `import_profile` /
管理端点 `PUT /api/v1/admin/prompt-library` / 策略快照恢复。

### 3.2 扁平文本是**死数据**，且与 pipelines 长期漂移

运行时真源是 pipelines：`active_profile()` → `resolve_profile()` 只要发现 `pipelines` 非空，
就用 `compile_modules` 覆盖扁平文本。而 pipelines 里 `source="base"` 的模块是按 **id 引用当前
代码基座**（实测 6489 字符）。于是：

| | 值 | 谁在用 |
|---|---|---|
| 磁盘扁平文本 `stable.trading_system` | 298 字符 | **没人用** —— 运行时走 pipelines |
| pipelines 编译结果 | 6490 字符（≈ 基座 6489 + 1 换行） | 实际进提示词的文本 |

**代码基座变了，文件不会变；扁平文本只在写盘那一刻刷新。** 两者因此长期漂移，切换时被一次性
"补课"，产生了 20 倍的落差。

> 这一点很重要：**这次切换对交易行为零影响**，它只是把一份陈旧缓存刷新成了真值 —— 代价是污染
> git、并让任何看 diff 的人以为策略被改了。修复后这条因果被**反向**利用了：既然扁平文本是派生
> 缓存，那么「内容未变」的判定就不该把它算进去（A②），否则它永远被判成"已改动"。

### 3.3 该文件本不该入库

- `data/*.json` 已全部忽略，**唯独 `prompt_library.json` 被例外跟踪**（跟踪文件不受忽略规则约束）；
- 它是运行时可变状态（active 指针 + 用户编的方案 + 历史），却按"发布物"管理 → 每次切换必脏工作区；
- 代码里的 `PRESETS` 已是两个内置方案的完整权威定义。

---

## 4. 数据现状（v8.3.0 实测）

| 指标 | 值 |
|---|---|
| 文件体积 | 282,619 字节（落地时实测 282,609 字节） |
| `profiles` 占比 | 23,089 字符（**16%**），且只有 `stable` 一个 |
| `revisions` 占比 | 115,045 字符（**83%**），6 条 `stable` 历史快照，每条约 22k 字符 |
| 历史里的孤儿 | 有一条 `create custom-1768944f2c`，该 id 已不在 `profiles` 中 |
| 引用该模块的测试文件 | 64 个 |

---

## 5. 方案对比（含落地后的实测）

| | 做法 | 改动量 | 切换后的 diff | 解决的问题 | 状态 |
|---|---|---|---|---|---|
| **A** | ① `activate_profile` 走「只改指针」轻量写；② `save_library` 对内容未变的方案沿用磁盘版本 | 实测约 85 行（含注释/docstring）+ 用例 | **1 行** | 切换/保存不再改写无关方案；写放大消失 | **已落地** |
| **B** | `data/prompt_profiles/index.json` + `<pid>.json`；`load_library` 合成老结构，`save_library` 拆回 | 实测约 115 行（同样含注释）+ 用例 | `index.json` 里 1 个字段（实测 91 → 101 字节） | A 的全部 + 单方案写粒度（无关方案零字节触碰）/ 失败面被隔离到单文件 / 按方案版本化 / 孤儿历史不再落盘。⚠️「并发隔离」只到这一层，跨进程仍是全局锁（见 §9.4） | **已落地** |
| **C** | `git rm --cached data/prompt_library.json` + `.gitignore` | 2 行 git 操作 + 注释 | 不再产生 diff | 运行时状态不再入库 | **已落地**（已暂存，待提交） |

### A 与「不改」的边界

A 只改**写路径的形状**，不改存储布局、不改 API 语义。原建议里的两条落地如下：

```python
# ① 只改指针，绕开全库规范化
def activate_profile(profile_id):
    profile = get_profile(profile_id)            # 存在性 + enabled 校验保留
    if not profile.get("enabled", True): raise ValueError("该方案已停用")
    _set_active_profile_id(profile_id)           # 新：读原文 → 改 1 字段 → _atomic_write
    return profile

# ② 未改动的方案按磁盘原文沿用，只规范化被改的那个
#    （判定剔除派生缓存：pipelines 形态下四类扁平文本不参与等值比较）
for pid, stored in _stored_profiles().items():
    if _unchanged_profile(pid, incoming[pid], stored):
        normalized["profiles"][pid] = stored
```

### 为什么 B 单独做没用

只要 §3.1 的全库重算逻辑不变，切换仍会重写方案文件，只是从"大文件里两条巨行"变成"小文件里
两条巨行"。**必须先做 A，B 才是在 A 之上的布局收益** —— 落地顺序即按此执行。

---

## 6. 落地建议（已执行）

1. **A + C 先行** ✅：切换在 git 里只剩 `active_profile_id` 一行（C 之后连这行也不再有），
   且该文件彻底退出版本库。
2. **B 随后** ✅：`index.json` + 一方案一文件；切换只写 101 字节的索引，改一个方案只写那一个文件。

### A 的验证方式（已固化）

`tests/llm/test_prompt_library_storage.py` 用隔离副本（不触碰真实数据）钉住契约：

```
切换方案后：
  - active_profile_id 变了
  - 其他方案的任何字段逐字节未变          （ActivationPointerOnlyTests）
  - 文件其余字节未变（逐行 diff，恰好 2 行）  （test_only_the_pointer_line_changes）
```

并保留了「零行为影响」的回归：切换前后**每个方案**的 `resolve_profile` 逐字节一致
（`test_zero_behaviour_change_for_every_rendered_template`）—— 证明该结论不是靠推理。

### C 的副作用与补偿（已核对）

失去"入库的部署默认方案"这一版控 —— 但：

- 代码 `PRESETS` 已是更权威的默认定义；
- `data/policy_archives/` 另有策略快照备份链（`policy/capture.py`）；
- 迁移时把当前文件**保留在磁盘原处**（更彻底地"留在仓外"），仍被 `.gitignore` 忽略，
  因此无需额外 `.seed` 副本：它本身就是回滚副本。

---

## 7. 若要做 B：连带改造清单（逐项核对）

改 `load_library` / `save_library` 两个口即可撑住上层。原清单四项的落地结论：

| 位置 | 现状 | 落地结论 |
|---|---|---|
| `r20_backend/policy/capture.py:118` | `load_library()` 把整库当作**一个快照 payload** 采进策略归档 | **无需改动**：`load_library` 返回的内存整库与布局无关，采集仍是一个完整 payload |
| `r20_backend/policy/restore.py:107` | `save_library(payload["prompt_config"])` 整库回写 | **无需改动**：`save_library` 负责拆回目录；旧格式（v2 整库 / v1 单方案）快照仍可恢复，已加用例 |
| `r20_backend/policy/fingerprints.py:105` | 指纹基于 `active_profile()` | **无需改动**：指纹只看 profile 内容。已加用例证明布局切换后 `compute_layout_hash` 不漂移（否则历史策略快照会全部失配） |
| `rollback_profile` / `revisions` | 历史是**全局一条列表**，且含孤儿记录 | **已定归属**：revisions 随方案走（与方案同文件）；无主孤儿不再落盘（修掉 §4 的孤儿）。语义变化见 §9 偏差 ③ |

### 迁移策略（对建议做了两处调整，理由见 §9 偏差 ①②）

**不**在首次 `load_library` 时迁移（读路径保持无副作用），改为**首次写盘**时迁移：
`save_library` 发现目录索引不存在而旧单文件在 → 用旧文件内容建目录存储，**旧文件原样留在原处**
（不改写、不删除、不改名）。

### 目录形态（落地实测）

```
data/prompt_profiles/
  index.json      # {"version": 3, "active_profile_id": "...", "updated_at": "..."}  91~101 字节
  stable.json     # 单方案完整定义（profile + 该方案的 revisions）          276,163 字节
  <custom-xxxx>.json
```

> ⚠️ 单方案文件仍被**历史快照**主导（占比 83%，见 §4）—— 「单文件 282 KB → 约 3 KB」这条估算
> 只在把 revisions 挪去独立目录且做快照去重时才成立，实测未达成（见 §9 偏差 ④）。

---

## 8. 附：关键代码锚点索引（v8.3.1 落地后）

| 语义 | 位置 |
|---|---|
| 方案库文件路径（旧单文件 / 回落读） | `scripts/prompt_library.py:59`（`LIBRARY_FILE`） |
| 目录存储常量（`STORE_DIR_NAME` / `STORE_VERSION` / `STORE_INDEX_NAME`） | `scripts/prompt_library.py:64` |
| 目录存储根 | `scripts/prompt_library.py:477`（`library_dir()`） |
| 读目录存储（合成老结构） | `scripts/prompt_library.py:501`（`_read_store()`） |
| 写目录存储（一方案一文件 + 索引最后写） | `scripts/prompt_library.py:538`（`_save_store()`） |
| 轻量指针写（方案 A①） | `scripts/prompt_library.py:573`（`_set_active_profile_id()`） |
| 读（目录存储优先 → 旧单文件 → 默认） | `scripts/prompt_library.py:765`（`load_library`） |
| 写（唯一落盘口，含 A② 沿用判定） | `scripts/prompt_library.py:810`（`save_library`） |
| 全库重算的始作俑者 | `scripts/prompt_library.py:719`（`_clean_profile` 内 `compile_modules`） |
| 运行时真源 | `scripts/prompt_library.py:847`（`resolve_profile`）、`:1161`（`active_profile`） |
| 内置方案权威定义 | `scripts/prompt_library.py` 的 `PRESETS`（`stable` / `wide_oscillation`，:242） |
| 后台切换端点 | `r20_backend/routers/strategy/prompts.py`（`POST /api/v1/admin/prompt-profiles/{id}/activate`，:155） |
| 兼容别名（外部模块） | `load_active_profile = active_profile`、`load_prompt_config = load_library` |

---

## 9. 落地记录（实测）

### 9.1 改了什么

| 文件 | 改动 |
|---|---|
| `scripts/prompt_library.py` | A①（`_set_active_profile_id` + `activate_profile` 轻量写）、A②（`_unchanged_profile` + `_stored_profiles`）、B（`library_dir` / `_read_store` / `_save_store` / `_store_file_name` / `STORE_*` 常量，`load_library` 与 `save_library` 接入）；storage 相关 docstring 都写明"为什么" |
| `.gitignore` | `data/prompt_library.json`、`data/prompt_profiles/` 两条 + 理由注释（C 的一半） |
| git 索引 | `git rm --cached data/prompt_library.json`（C 的另一半；**尚未提交**，工作树的文件仍在原处且已被忽略） |
| `tests/llm/test_prompt_library_storage.py` | **新增 25 条**用例：A① 单行 diff / 零行为影响 / v1 回落、A② 不重算无关方案、B 拆文件 / 只动一个文件 / 读回等价 / 历史归属 / 孤儿清理 / 损坏回落 / 路径安全、§7 的采集-恢复闭环与指纹不漂移、C 的忽略规则与未被跟踪 |
| `tests/llm/test_prompt_rendering_isolated.py` | `test_storage_roundtrip_retains_slots` 改为经存储层读回（原来直接读 `LIBRARY_FILE`） |
| `tests/llm/test_prompt_library_tails.py` | `test_a_rollback_to_an_invalid_snapshot_is_refused` 改为经 `save_library` 注入坏快照（直接改写旧单文件已改不到活库） |
| `tests/llm/test_prompt_universality.py`、`tests/trading/test_risk_config_api.py` | 三处「线上方案快照」断言改经 `tests/config_sandbox.live_prompt_profile()` 读取（否则核对的是一份可能已冻结的旧单文件） |
| `tests/config_sandbox.py` | 新增 `live_prompt_library()` / `live_prompt_profile()`（只读、经存储层、无快照即 skip） |

### 9.2 验证证据

| 验证 | 命令 | 结果 |
|---|---|---|
| 全量套件（含 25 条新用例） | `.venv/bin/python -m tests.offline_suite` | **10417 tests，OK (skipped=68)**；改动前基线 **10392 / 67 skipped**，零失败（+1 skip 即新用例里需要 git 子进程的那条） |
| 越过生产数据守卫 | 套件自带的 `OfflineGuard` 报告 | `CONFIG_FINGERPRINT_CHANGES` / `CONFIG_WRITE_ATTEMPTS` 里**没有** `prompt_library` / `prompt_profiles`（与基线一致） |
| 切换只改一行（真实线上库副本） | `/tmp` 副本 + `difflib` 逐行 diff | 恰好 2 行（`-/+ active_profile_id`） |
| 切换不重算无关方案 | 同上 | `profiles` 逐字段相等；`stable.trading_system` 缓存保持 298 字符 |
| 写放大消失 | 目录存储副本 | 建方案后 `stable.json` 字节不变；改一个方案只有那一个文件变 |
| 目录存储体积 | 线上库副本迁移 | 282,609 → `index.json` 91 字节 + `stable.json` 276,163 字节 |
| 迁移的内容差异（真实线上库副本） | `/tmp` 副本 + 逐字段遍历 | `profiles` / active 指针 / 每方案历史 / `compute_layout_hash` **全部一致**；唯一差异 = 丢掉 §4 那条孤儿历史（revisions 6 → 5） |
| 切换的写入量 | 目录存储副本 | `index.json` 91 → 101 字节（单次切换只写这么多） |

### 9.3 与 §6/§7 建议的偏差与补充（6 处，都为了「缺省更安全」）

1. **迁移时机：首次写盘，而非首次 `load`。** 原建议在 `load_library` 里迁移 —— 那是**读**路径
   的副作用：实盘主脑每周期都 read，会把一次写放大成"读取即迁移"，且失败面波及交易进程。
   改为首次写盘迁移后，读路径永远无副作用，迁移发生在唯一的写口。
2. **旧单文件保留原地（不改名 `.migrated`）。** 原建议改名。改名会把「混版本进程读旧库」变成
   「读不到 → 静默回落 `_default()`」（active 指针与自定义方案全部消失）。原地保留时，新代码
   优先目录存储、旧代码仍读旧文件，**双方都读得到**，人工也能一眼看到回滚副本。
3. **`revisions` 随方案走，且尾部裁剪语义从「全局最后 N 条」变为「每方案最后 N 条」。**
   `MAX_REVISIONS` 不变；旧实现下单个方案刷满 100 条会把**其它**方案的历史挤掉，现在不会。
   代价：`delete_profile` 连带删除该方案的历史（"随方案走"的必然结果），即**不能用回滚复活已删
   方案**；换来的是 §4 那种孤儿不再堆积（已加用例）。
4. **「单文件 282 KB → 约 3 KB」未达成**：revisions 与方案同文件（§7 的目录形态草图就是这么画
   的），所以单方案文件仍被 83% 的历史快照主导。真实收益是**写粒度与写入量**（切换 101 字节、
   无关方案 0 字节），不是文件总大小。后续若要做，路径是「revisions 独立目录 + 快照去重/增量」。
5. **跨文件的整库原子性不如从前。** 旧单文件写是单次 `os.replace`（原子）；目录存储是多文件写，
   顺序为「先各方案文件、后索引」。崩溃窗口只会留下**旧指针 + 各自自洽的方案文件**，不会指到
   不存在的方案，也不会丢数据，但理论上可能混代。索引缺失/损坏时 `load_library` 回落到旧单文件
   （已加用例）。升级路径：需要更强保证时上 journal / 两阶段提交（当前场景是管理页低频写，
   不值得先付这个复杂度）。
6. **安全闸**：方案 ID 会拼进文件名，故 `_store_file_name` 用与路由白名单同源的 `^[A-Za-z0-9_-]+$`
   校验，防路径穿越（已加用例）。

### 9.4 仍未做 / 有意不做

- **生产迁移不主动触发**：`data/prompt_library.json`（真实实盘状态）没有被本次改动改写；目录存储
  在该进程下一次**写盘**（后台切换/保存/回滚/策略恢复）时自动生成。理由：生产数据迁移按本仓纪律
  需要显式授权。
- **真正的乐观并发不做**：跨进程仍是全局锁 + 整库 RMW，B 只把**写粒度**切到方案级（无关方案零字节
  触碰、单文件损坏不波及其它方案）。已知天花板：进程 1 拿着陈旧整库快照改 A 方案时，它对 B 方案的
  旧副本仍可能盖回线上 B（旧实现同型）。升级路径 = 每方案基线版本 + CAS（冲突则拒并提示重载）。
  当前场景是管理页低频人工写，不值得先付这个复杂度。
- **`.seed` 副本不建**：迁移后旧文件仍在原处，本身就是回滚副本（见 §6 C 的补偿）。
- **不做 revision 快照去重**：与本次三条主张无关，且会改动历史语义（YAGNI）。
