# -*- coding: utf-8 -*-
"""handlers_ugtask.py —— 任务界面 1311/1312/1313（新包 A0 第 10 批，**A 组最后一个可做的批次**）。

**修前的缺口**：三个号服务端一个都没有 ⇒ 落兜底 `[1,[[]]]`：
  · **1311** -> `msg[1..4]` 全是空表（Lua 真值，`_data[1] or 0` 得 0）⇒ 任务界面**每一页都是空的**、
    进度显示 `0/0`；`UgTaskHome` 拿不到任何任务 ⇒ 主线/支线两条线**整块不可玩**。
  · **1313** -> `msg[1]/msg[2]` 都 nil ⇒ `gamer_setRoleInfo(nil)`、**领奖没有掉落**、
    回调 `m_callFunc(true)` 却什么都没发生（玩家看到"领了但没东西"）。
  · **1312** -> 纯 ack（客户端只回调），但**副作用必须落**：不落 `aStory`/`fStory`
    每次登录都当"首次"，前置剧情按钮永远亮着。

**数据源（全部有出处）**：`UG_07_Task`(405) —— `TaskID`/`TaskType`(实测**只有 1 主线 81 行 /
2 支线 324 行**，没有 3/4)/`TaskAcceptLevel`/`ChapterID`/`ConditionType`/`Value`/`Quantity`/
`JumpTag`/`RewardID`(**405 个组实测全在 `T02_Reward`**)/`TaskAcceptPlotID`/`TaskFinishPlotID`/
`TaskNextId`/`TaskPrevId`；剧情 id 落在 `T20_StoryGroup`（17 个非 -1 的 `TaskAcceptPlotID`
**全部命中**）。`TaskAcceptPlotID` 就是客户端 `usGetTableData(tT20_StoryGroup.tName, ...)` 查的那张。

**三个实测出来的硬事实（都不是猜的）**
  1. ★**`TaskFinishPlotID` 405 行全是 `-1`** ⇒ 这个系统**没有后置剧情**，
     `fStory` 在客户端虽然被解出来但恒无意义。别按"两端都有剧情"去设计。
  2. ★**`TaskNextId` 不可信、`TaskPrevId` 才是链，而且这张表的链是"主线一条 + 支线散着"**：
     实测 405 行的 `TaskPrevId` 里有 **4 个 `-1` 的根**（主线 11001 / 支线 12002、12003、12004）
     和 **26 条指向表外的悬空引用**（如 12006→`10005`、32019→`31018`）；`TaskNextId` 同样有指向
     表外的（11001 的 `TaskNextId=10104` 就不在表里）。⇒ ① **链一律按 `TaskPrevId` 走**；
     ② **悬空前置当"无前置"**（否则那 26 条永远停在"不可接"，见 `_prev_of` 的注释）。
     按 type 分开看：**主线 81 行是干净的单链（1 根 / 0 悬空）**，支线 324 行是散的。
  3. ★**`Quantity` 不是 1**：分布从 1 到 100000（`1:218 / 3:25 / 2:30 / 5:32 / 2000:6 / 10000:6 …`），
     `Value` 还可能是**竖线分隔的 id 列表**（如 CT=23 的 `'1001|1002|1003|1004'`）⇒ 它是
     "**累计到这个数**"的门槛，不是"做过没有"的开关。

**状态机（客户端 `UgTaskMainView:538-640` 逐支反推）**：`TASK_STATUS` = 0 不可接 / 1 未接 /
2 进行中 / 3 未领奖 / 4 已领奖。客户端对四态各挂一个按钮分支：**NR(3)** 出「领取」、
**DO(2)** 出「去完成」(`JumpView:go(JumpTag)`)、**有前置剧情且 `aStory`** 出「播放剧情」。
⇒ 服务端按"前置链 + 前置剧情 + 计数门槛"三件事推：
  ① 前置任务没领完（或等级不够）-> **0**；② 有前置剧情且没看过 -> **1**；
  ③ 计数到门槛 -> **3**；④ 否则 **2**；⑤ 领过 -> **4**。

★★2026-10-18（UG 11005 取证后）：**CT=29「副本探索」也改成了按 `Value` 精确判** ——
  与 CT=31 同样是"同一 CT 多个 Value"，共享 `ug_ct` 会让 11005/21005/…/81002 互相串台。
  `ConditionType=29` 的 `Value` 是**大关卡 id**（简单=DefaultID，困难=EliteID；也可能像
  141019 一样是 `50001|150001` 的"任意难度"竖线列表），所以新增 **按 gate 记的账**
  `ug['gate_clear']`：只由 9003 胜利 + `info ∈ S12[gate].Type==1.Value1`（该关 BOSS 组）驱动，
  每个 explore run 最多 +1（与 CT=3 同一判据、同一去重），**不碰共享 `ug_ct`**。
  每关的计数上限 = `UG_07_Task` 里引用该 gate 的 CT=29 任务最大 `Quantity`
  （40001 上限 1 ⇒ 重打不涨；50001/150001 上限 5 ⇒ 141019「再探…任意难度」可累计）。
  ★★2026-10-22（独立审查 B2）：原先"其余 CT 按 ConditionType 一个计数器（`ug_ct`）走"
  **已被推翻** —— 同一 CT 多行、`Value` 指不同目标时，共享计数会把"打错目标"算成"打过"。现在分三类：
    · 单值 CT（4/9/12/32/44 的 `Value` 都是 `0`，全表只有一种语义）→ 仍用 `ug_ct`；
    · 按目标 CT（23/24/45/72/80/83/85，见 `_UG_VALUE_SCOPED`）→ `ug['ctv'][ct][value]`，
      只在**真实事件**那一行由 `bump_ug_ctv()` 写入；`bump_ug_ct` 对它们直接拒绝；
    · 有天然真值的 CT（29 gate_clear / 31 story_seen / 57 monster_lv / 28 峰值 / 45 mon_star）
      → `_sched` 直接读真值，不经过任何计数器。
"""
import json

from state import EQ_STATE, save_state, table_dir as _table_dir

_DATA = _table_dir()
_TYPE_MAIN, _TYPE_SIDE = 1, 2
_CT_STORY = 31          # 「完成剧情第 N 章」（17 行 Value=101..117，见 _sched 的注释）
_CT_GATE_CLEAR = 29     # 「副本探索·某关」（Value=大关卡 id；见 _sched / bump_ug_gate_clear）
_CT_MON_LV = 57         # 「指定妖怪升到 N 级」（主线 12 行，Value="mid&lv"；见 _sched）
_CT_PVP_STREAK = 28     # 「妖怪对战 N 连胜」（Value=1；读永久峰值，见 _sched）
_CT_BOSS_KILL = 23      # 「在《X》副本中击败某 BOSS」（19 行 / 15 个不同 Value=T01 BOSS 组 id）
_CT_POT = 24            # 「在《X》副本中打碎罐子 N 个」（Value=大关卡 id）
_CT_HIDDEN_BOX = 22     # 「在《X》副本中开启 N 个隐藏宝箱」（Value=大关卡 id，2026-10-24 接）
_CT_EVOLVE = 45         # 「把 N 个妖怪进化到 M 阶」（2 行，Value=M）
_CT_YMYW = 72           # 「妖魔异闻中击败某 BOSS」（Value=30001，**仍无驱动**，见 HANDOFF）
_CT_RANK = 80           # 「妖神排位达到某段位」（Value=P10 段位 id，**仍无驱动**）
_CT_GUILD = 83          # 「加入 1 个妖盟」（真值 = 是否**成员**，见 `_guild_joined`）
_CT_TRAIN = 85          # 「冲虚道场某训练场通关」（Value=B01 mainId）
# ---- 2026-10-24（独立审查 P1-0）：以下 4 类**本来就是状态量**，不需要"事件驱动" ----
#   修前它们落进共享 `ug_ct` 读取，而没有任何写口 ⇒ 36 行支线永远 0（审查的"138 条"里最先能修完的一批）。
#   判据一律照 CT=29/57/85 的 A 类做法：**读真值**，不建第二本账。
_CT_LAYOUT = 11         # 「妖力布阵妖力值达到 N」（Value=220000；真值 = 已保存防守队 score 之和）
_CT_FORMATION_POWER = 79  # 「提升布阵 妖力值达到 N」（Value=10000/150000/300000；同上）
_CT_MON_COUNT_LV = 46   # 「培养 N 只妖怪到 M 级」（Value=M 级、Quantity=只数；真值 = 背包里等级达标只数）
_CT_TOWER_FLOOR = 69    # 「古塔迷踪-N 层」（Value=1000+N；真值 = tower.plies）
_CT_GATE_DEGREE = 70    # 「副本制霸」（Value=`<gate>&100`；真值 = 该大关卡解锁度）

# ★★2026-11-05（`_ug_full_chain_test.py` 支线取证）：CT=70 的 `72009「副本制霸-7」` 写的是
#   `Value = 100001&100`，可是**大关卡表里没有 100001**（`S12_SceneExplore`、
#   `S01_Level`、`S00_WorldMap` 全无这一行；最接近的是 `1000001` 河洛古墓）。
#   14 条「副本制霸」正好该覆盖 7 组大关卡的简单/困难两张：
#     40001/50001/60001/70001/80001/90001 + 140001/150001/160001/170001/180001/190001/2000001
#   实测这 13 个都在，**唯独缺 `1000001`、多出一个 `100001`** ⇒ 这是一处**笔误**
#   （不是隐藏关卡：`_explore_degree(100001)` 恒 0，那条支线永远做不完）。
#   ⇒ 服务端按"同一个大关卡"处理，**不改客户端表**（改表要重跑 kb/回归，且没有官方依据）。
_CT70_GATE_ALIAS = {100001: 1000001}
# ---- 2026-10-25（P1-4 的"战斗遥测"桶）：1234 每场都上报，真值在 `mon_dmg` ----
_CT_BATTLE_COUNT = 49   # 「使用 X 战斗 N 场」（Value=静态 mid；真值 = 该怪出战场次）
_CT_DMG_DEALT = 53      # 「使用 X 造成 N 点伤害」（Value=静态 mid；真值 = attackNum 累计）
_CT_DMG_TAKEN = 54      # 「使用 X 抵挡 N 点伤害」（Value=静态 mid；真值 = deffendNum 累计）
# ---- 2026-10-25（P1-4 的"养成"桶）：服务端本来就有这些真值，缺的只是 `_sched` 的读法 ----
_CT_SKILL_COUNT = 55    # 「X 获得 N 个技能」（Value=`mid&N`；真值 = monster_skills 非零槽数）
_CT_SKILL_LV = 56       # 「培养 X 的技能到 N 级」（真值 = 该怪装配技能的最高等级）
_CT_FEAT_LV = 58        # 「培养 X 的特性到 N 级」（真值 = monster_featlev）
_CT_POWER = 59          # 「X 妖力值达到 N」（真值 = monsters[uc][2]，第 30 字段即妖力值）
_CT_SUIT = 61           # 「给 X 装备套装」（Value=`mid&件id|件id|件id`；真值 = 该怪三槽里的装备）
_CT_BOND = 62           # 「获得 1 个羁绊」（真值 = L81_CP 的 CPType=2 组员齐全的组数）

# ★★2026-10-22（独立审查判红 B2）：**按 Value 记账的 CT**。
#   这些 CT 在 `UG_07_Task` 里有多行、每行 `Value` 指**不同目标**（不同 BOSS / 不同关卡 /
#   不同训练场 / 不同阶数）。共享 `ug_ct[ct]` 会把"打了 A"算成"打过 B"：
#     · CT=23 19 行 / 15 个不同 Value（2012 黄九郎、3012 河伯、4012 姬姬…）
#     · CT=24 1 行（但 Value=1000001 指定关卡，任意关卡的罐子都会串进来）
#     · CT=45 2 行（Value=2，"到 2 阶"不能被 2→3 的进化冒充）
#     · CT=85 1 行（Value=101，"冲虚道场 101"不能被 102/103 的子关卡点亮）
#     · CT=72/80/83 目前无驱动；先纳进来，避免将来补驱动时又走"裸 bump"污染共享账。
#   驱动方一律走 `bump_ug_ctv(ct, key, n)`，`bump_ug_ct` 对它们**直接拒绝写**（见该函数）。
_UG_VALUE_SCOPED = frozenset((_CT_LAYOUT, _CT_BOSS_KILL, _CT_HIDDEN_BOX, _CT_POT, _CT_EVOLVE,
                              _CT_YMYW, _CT_RANK, _CT_GUILD, _CT_TRAIN))
_UG_ST_VER = 1          # `ug` 账本迁移版本（2026-10-18 1312 映射反转的修复）

# ★★2026-10-24（独立审查 高 H2）：`_UG_CTV_VER` 从 1 提到 2 —— CT=22 刚纳入按目标记账，
#   已打过 v1 的老档里 `ug_ct['22']` 还在（审查实测 5）。版本不抬，那段共享计数就永远搬不走，
#   而"只要还有一条路径能读共享账，打错目标点亮任务就会从别处漏回来"（见 `bump_ug_ct` 的注释）。
_UG_CTV_VER = 2


def _load_json(name):
    from state import load_table as _lt
    return [list(_r) for _r in _lt(name, _DATA)]


def _out(*body):
    return ('[1,' + json.dumps(list(body), separators=(',', ':'), ensure_ascii=False)
            + ']').encode('utf-8')


def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _req(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    return req if isinstance(req, dict) else {}


def _role_info():
    """**唯一权威实现** `state.role_info_array()`。

    ⚠ 不要写成 `from handlers_game import _role_info`（"转一手"）：运行时等价，但会被
      `_roleinfo_convergence_guard_test` 的 **R1 判红**，而 R1 是对的 —— 它要求每个
      `def _role_info*():` 的函数体里**逐字出现 `role_info_array()`**，防的正是"转一手之后
      下一轮顺手在本地改两格"（v32 分叉事故）。这条本轮已经在 `handlers_posttask` 上踩过一次。
    """
    from state import role_info_array
    return json.loads(role_info_array())


def _drop(groups):
    from handlers_game import _drop as _gd
    return _gd(groups)


# ---------------------------------------------------------------- 静态表
_UG = {}             # TaskID(int) -> row dict
_ORDER = []          # 按文件顺序的全部 TaskID
_BY_TYPE = {}        # taskType -> [TaskID]（按文件顺序）
_STORY_IDS = set()


def _ug():
    if not _UG:
        try:
            t = _load_json('UG_07_Task.json')
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                d = {c: r[h[c]] for c in t[0]}
                tid = _i(d.get('TaskID'))
                if tid <= 0:
                    continue
                _UG[tid] = d
                _ORDER.append(tid)
                _BY_TYPE.setdefault(_i(d.get('TaskType'), 1), []).append(tid)
        except Exception as e:
            print('[UG] 读 UG_07_Task 失败: %r' % (e,), flush=True)
    if not _STORY_IDS:
        try:
            t = _load_json('T20_StoryGroup.json')
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                _STORY_IDS.add(_i(r[h['ID']]))
        except Exception:
            pass
    return _UG


# ---------------------------------------------------------------- 账号档
def _st():
    v = EQ_STATE.setdefault('ug', {})
    if not isinstance(v, dict):
        v = EQ_STATE['ug'] = {}
    for k in ('claimed', 'aplot', 'fplot', 'gate_clear'):
        if not isinstance(v.get(k), dict):
            v[k] = {}
    # ★★2026-10-18：老档迁移（单档/模板档路径；多账号路径在 accounts._load 另挂一次）。
    migrate_story_flags(EQ_STATE)
    # ★★2026-10-22：按-Value CT 的旧共享计数隔离（同上，两条装载路径都跑）。
    migrate_value_scoped_ct(EQ_STATE)
    return v


def migrate_value_scoped_ct(data):
    """把按-值 CT 的**旧共享计数**搬进 `ug_ctv_legacy` 并从 `ug_ct` 删掉；返回搬迁条数。

    ## 为什么必须搬走（不是留着当兜底）

    这些数字本来就是"**打错目标**"的产物：审查实测账号档里 `ug_ct['23']=3`，而
    CT=23 的 19 条主线要求的都是"在《X》击败某 BOSS"，那 3 次却是**妖卵**（T01 2013/3013…）
    击杀累计出来的 —— 妖卵组和 BOSS 组在 `T01_MstrGroup` 里是**不同行**（2012=黄九郎、
    2013=异界妖卵）。留在 `ug_ct` 里既没有归属，又可能被任何一处读取当成真进度。

    ## 代价

    受影响的都是**可重复内容**：再打一次 BOSS / 再砸 10 个罐子 / 再进化一只怪，
    不会造成不可恢复的卡死。已领奖的任务不受影响（`claimed` 是另一本账）。

    ## 幂等

    打 `ug['ctv_ver']`；重复调用直接返回 0。
    """
    if not isinstance(data, dict):
        return 0
    st = data.get('ug')
    if not isinstance(st, dict):
        return 0
    if _i(st.get('ctv_ver'), 0) >= _UG_CTV_VER:
        return 0
    ct_map = data.get('ug_ct')
    n = 0
    if isinstance(ct_map, dict):
        legacy = st.get('ug_ctv_legacy')
        if not isinstance(legacy, dict):
            legacy = st['ug_ctv_legacy'] = {}
        for ct in list(_UG_VALUE_SCOPED):
            k = str(ct)
            if k in ct_map:
                legacy[k] = ct_map.pop(k)
                n += 1
        if not legacy:
            st.pop('ug_ctv_legacy', None)
    st['ctv_ver'] = _UG_CTV_VER
    return n


def migrate_story_flags(data):
    """一次性修复 2026-10-18 前 1312 映射反转留下的脏标记（幂等；返回变更条数）。

    修前 `beforeAndAfter==1`（客户端 `STORY_TYPE.TYPE_1`=前置）被记成 `fplot`；而
    `UG_07_Task.TaskFinishPlotID` **405 行全 -1** ⇒ 任何 `fplot` 都不可能是真的后置剧情。
    迁移（只动传入的 `data`，账号档加载路径与 `_st()` 共用）：
      ① 删掉指向"后置剧情 id = -1"的任务的 `fplot`；
      ② 对 `TaskAcceptPlotID` 已在 `story_seen` 里的任务补 `aplot`（玩家确实看过前置剧情）；
      ③ 打 `ver`，之后直接返回 0（幂等）。
    ⚠ 不碰 `claimed` / `ug_ct`；也不改 `story_seen`。
    """
    if not isinstance(data, dict):
        return 0
    st = data.get('ug')
    if not isinstance(st, dict):
        return 0
    if _i(st.get('ver'), 0) >= _UG_ST_VER:
        return 0
    _ug()
    seen = data.get('story_seen') or {}
    aplot = st.get('aplot') if isinstance(st.get('aplot'), dict) else {}
    fplot = st.get('fplot') if isinstance(st.get('fplot'), dict) else {}
    n = 0
    for tid, row in _UG.items():
        key = str(tid)
        if key in fplot and _i(row.get('TaskFinishPlotID'), -1) == -1:
            fplot.pop(key, None)
            n += 1
        ap = _i(row.get('TaskAcceptPlotID'), -1)
        if ap != -1 and str(ap) in seen and key not in aplot:
            aplot[key] = 1
            n += 1
    st['aplot'] = aplot
    st['fplot'] = fplot
    st['ver'] = _UG_ST_VER
    return n


def _ct_map():
    v = EQ_STATE.setdefault('ug_ct', {})
    if not isinstance(v, dict):
        v = EQ_STATE['ug_ct'] = {}
    return v


def bump_ug_ct(ct, n=1):
    """累计 **按 ConditionType** 的进度（由 `handlers_task.bump_task` 顺带调用）。

    ⚠ 与 `a07_ct` 的**关键差别**：这一本**不跨天清零**（UG 主线是一条永久链，
      按日账本走的话"累计获得 2000 铜钱"永远到不了门槛 —— 与 UN15 周任务同一个坑）。
    ⚠ CT=29 **不写这里**：它的 Value 是关卡 id，写进共享计数会让 11005/21005/… 串台；
      走 `bump_ug_gate_clear()` 的 `ug['gate_clear']`（见模块头 ★★）。
    ★★2026-10-22（审查判红 B2）：`_UG_VALUE_SCOPED` 里的 CT **一律拒绝写**。
      它们由 `bump_ug_ctv(ct, key)` 按目标记账；只要还有一条路径能写共享计数，
      "打错目标也点亮任务"就会从别处漏回来（审查实测 `ug_ct[23]=3` 正是妖卵击杀刷出来的）。
      返回 True = 本次真的入账。
    """
    try:
        ct = _i(ct)
        if ct in _UG_VALUE_SCOPED:
            return False
        m = _ct_map()
        m[str(ct)] = _i(m.get(str(ct)), 0) + max(0, _i(n, 1))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 按 (CT, Value) 的永久账
# 结构：`EQ_STATE['ug_ctv'] = {ct: {valueKey: n}}`。与 `ug_ct` 并列、同样**不跨天清零**。
def _ctv_map():
    v = EQ_STATE.setdefault('ug_ctv', {})
    if not isinstance(v, dict):
        v = EQ_STATE['ug_ctv'] = {}
    return v


def _ctv_of(row):
    """本行在该 (CT, Value) 上的累计（没有 -> 0）。`Value` 支持 `a|b` 列表求和。"""
    ct = _i(row.get('ConditionType'))
    m = _ctv_map().get(str(ct))
    if not isinstance(m, dict):
        return 0
    total = 0
    for key in _value_parts(row):
        total += max(0, _i(m.get(str(key)), 0))
    return total


# (ct, value) -> 该目标在 UG 全表里被要求的最大 Quantity（懒加载；封顶用）
_CTV_MAXQ = {}


def _value_parts(row):
    """`Value` -> 正整数列表；非数字 / 0 一律丢掉（CT=4/9/12/32… 的 Value 是 `0`）。"""
    out = []
    for part in str(row.get('Value') or '').replace(',', '|').split('|'):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            out.append(int(part))
    return out


def _ctv_maxq():
    """(ct, value) -> 引用它**最多**的那条 UG 任务的 Quantity（只读表，懒加载）。"""
    if _CTV_MAXQ:
        return _CTV_MAXQ
    _ug()
    for row in _UG.values():
        ct = _i(row.get('ConditionType'))
        if ct not in _UG_VALUE_SCOPED:
            continue
        q = max(1, _i(row.get('Quantity'), 1))
        for key in _value_parts(row):
            k = (ct, key)
            _CTV_MAXQ[k] = max(_CTV_MAXQ.get(k, 0), q)
    return _CTV_MAXQ


def bump_ug_ctv(ct, key, n=1):
    """按 (ConditionType, 目标 Value) 累计 UG 永久进度；返回 True = 本次真的入账。

    调用点必须是"**这件事真的发生了**"的那一行（与 `bump_task` 同一条纪律），并且要能
    区分空操作（例如"一键上装但没得换"不算 CT=32、"2→3 进化"不该再算一次 CT=45 的 2 阶）。

    封顶 = 引用该 (ct, key) 的所有 UG 任务的**最大** Quantity：既防"重放刷数字"，
    也让账本不会无限长（40001 上限 1 ⇒ 重打不涨；CT=23 的 141022 上限 5 ⇒ 可累计 5 次）。
    """
    try:
        ct = _i(ct)
        k = _i(key)
        if ct <= 0 or k <= 0:
            return False
        cap = _ctv_maxq().get((ct, k), 0)
        if cap <= 0:
            # 该目标在 UG 表里**没有**任何任务引用它 —— 不记账，避免无主数据。
            return False
        m = _ctv_map()
        sub = m.get(str(ct))
        if not isinstance(sub, dict):
            sub = m[str(ct)] = {}
        cur = max(0, _i(sub.get(str(k)), 0))
        add = max(0, _i(n, 1))
        if add <= 0 or cur >= cap:
            return False
        sub[str(k)] = min(cap, cur + add)
        save_state()
        return True
    except Exception as e:
        print('[UG] !! bump_ug_ctv(%r,%r) 失败: %r' % (ct, key, e), flush=True)
        return False


# CT=29 的 gate -> 该 gate 在 UG_07_Task 里被要求的最大 Quantity（懒加载，只读表）。
#   40001 只出现在 11005（Quantity=1）⇒ 重打进不了账；
#   50001 同时被 21005(qty=1) 与 141019(qty=5) 引用 ⇒ 上限 5，可累计"再探 5 次"。
_CT29_MAXQ = {}


def _ct29_parts(row):
    """CT=29 的 `Value` -> 正整数 gate 列表（兼容 `50001|150001` 竖线列表）。"""
    out = []
    for part in str(row.get('Value') or '').replace(',', '|').split('|'):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            out.append(int(part))
    return out


def _ct29_maxq():
    if _CT29_MAXQ:
        return _CT29_MAXQ
    _ug()
    for row in _UG.values():
        if _i(row.get('ConditionType')) != _CT_GATE_CLEAR:
            continue
        q = max(1, _i(row.get('Quantity'), 1))
        for g in _ct29_parts(row):
            _CT29_MAXQ[g] = max(_CT29_MAXQ.get(g, 0), q)
    return _CT29_MAXQ


def bump_ug_gate_clear(gate, n=1):
    """UG CT=29 的**按 gate** 进度：`ug['gate_clear'][gate] += n`（封顶见 `_ct29_maxq`）。

    唯一驱动点是 `handlers_game._xp_ct3_bump()` —— 9003 胜利且 `info` 命中该关
    `S12_SceneExplore.Type==1.Value1`（BOSS 组），每个 explore run 最多 1 次。
    ⇒ **进关（1701）/退出（1706）/开箱（1704）/杀小怪都不算**；1706 请求体只有
    `{"gateid":...}`（真机 21:54:49 样本），没有任何"通关"标志，不能拿它当判据。

    为什么按 gate 而不是按 CT 裸 bump：`UG_07_Task` 的 CT=29 有 18 行、
    `Value` 覆盖 40001/50001/…/2000001 与 `50001|150001` 这类列表；
    共享计数一次点亮全部。也不写 `a07_ct`（每日账本）或 `ug_ct`（CT 聚合账）。

    返回 True = 本次真的入账（调用方用于日志/测试）。
    """
    g = _i(gate, 0)
    cap = _ct29_maxq().get(g, 0)
    if g <= 0 or cap <= 0:
        return False
    st = _st()
    m = st.get('gate_clear')
    if not isinstance(m, dict):
        m = st['gate_clear'] = {}
    cur = max(0, _i(m.get(str(g)), 0))
    add = max(0, _i(n, 1))
    if add <= 0 or cur >= cap:
        return False
    m[str(g)] = min(cap, cur + add)
    save_state()
    return True


def _formation_power():
    """已保存的"妖力布阵"防守队**妖力值**（= 上阵怪 `score` 之和；没存过阵容 -> 0）。

    ★★2026-10-24（独立审查 高 H3 / P1-0）：CT=11 的 122013 文案是"妖力布阵妖力值提升220000"、
      CT=79 的 3 行是 10000/150000/300000 —— 这**不是"保存了几次布阵"**，是阵容的妖力总量。
      修前 UG 把 CT=11 接到"7002 保存且阵容变了就 +1"上：任何一次换人都会给这些行记 1 次，
      与目标值 220000 完全不是一个量纲（审查据此判"忽略 Value"）。
    真值来源：真机快照 u0001 的 `pvp.teams['1'].defense` 里每行都带**客户端算好的** `score`
      （5 只合计 527000），所以直接取它，不用服务端另推近似公式
      （`handlers_pvp` 里 `hp/10+atk*2+…` 那条明确写着"不等于客户端权威公式"，不能拿来判任务）。
    `handlers_rank._my_saved_defense_units()` 是既有唯一取数入口（无阵容返回 []，不回退背包）。
    """
    try:
        from handlers_rank import _my_saved_defense_units
        return sum(max(0, _i(u[2], 0)) for u in _my_saved_defense_units())
    except Exception as _e:
        print('[UG] !! 读布阵妖力失败(CT=11/79): %r' % (_e,), flush=True)
        return 0


def _mon_count_at_lv(lv):
    """背包里等级 >= `lv` 的怪**只数**（CT=46「培养 N 只到 M 级」的真值）。"""
    try:
        from state import all_monster_ucodes, get_monster_lv
        return sum(1 for uc in all_monster_ucodes() if _i(get_monster_lv(uc), 0) >= lv)
    except Exception as _e:
        print('[UG] !! 数妖怪等级失败(CT=46): %r' % (_e,), flush=True)
        return 0


def _tower_plies():
    """妖塔当前层数（CT=69「古塔迷踪-N 层」的真值，`Value` = 1000+N）。"""
    try:
        from handlers_tower import _tw
        return _i((_tw() or {}).get('plies'), 0)
    except Exception as _e:
        print('[UG] !! 读妖塔层数失败(CT=69): %r' % (_e,), flush=True)
        return 0


def _explore_degree(gate):
    """某大关卡的探索解锁度 0..100（CT=70「副本制霸」的真值，与 1705 下发的同一算法）。"""
    try:
        from handlers_game import _xp_degree
        return _i(_xp_degree(_i(gate)), 0)
    except Exception as _e:
        print('[UG] !! 读探索度失败(CT=70): %r' % (_e,), flush=True)
        return 0


def _owned_in_chain(mid):
    """`mid` 所在进化链上**当前拥有**的 ucode（按 `L91_Status.SourceID` 同源）。"""
    from state import all_monster_ucodes, get_monster_base
    root = _mid_root(mid)
    out = []
    for uc in all_monster_ucodes():
        b = get_monster_base(uc)
        if b and _mid_root(_i(b[0])) == root:
            out.append(uc)
    return out


def _dmg_total(kind, mid):
    """CT=49/53/54 的真值：按同源链汇总 1234 上报的 出战 / 造成 / 抵挡。

    `kind` ∈ `battles` / `atk` / `def`（见 `handlers_batch.handle_monster_damage`）。
    直接遍历账本里的 static id 而不是"当前拥有的怪"：怪被吞噬/进化掉之后，
    历史战绩**不该消失**（与 CT=57 的"读真值"不同 —— 那个是"现在这只有多强"）。
    """
    per = EQ_STATE.get('mon_dmg') or {}
    m = per.get(kind) or {}
    if not isinstance(m, dict):
        return 0
    root = _mid_root(mid)
    total = 0
    for k, v in m.items():
        sid = _i(k, 0)
        if sid > 0 and _mid_root(sid) == root:
            total += max(0, _i(v, 0))
    return total


def _mon_skill_stats(mid):
    """`(已习得技能数, 最高技能等级)`；同源链上取最强的一只。

    技能真值 = `monster_skills[uc]` 的 6 个装配槽（`0` 是空槽），等级在技能袋
    `skills[袋 ucode] = [id, level, isopen]` 的第 2 槽（`state.ensure_skills` 维护）。
    """
    bag = EQ_STATE.get('skills') or {}
    ms = EQ_STATE.get('monster_skills') or {}
    best_n = best_lv = 0
    for uc in _owned_in_chain(mid):
        slots = ms.get(uc) or []
        n = lv = 0
        for s in slots:
            k = str(s)
            if k in ('', '0'):
                continue
            n += 1
            row = bag.get(k)
            if isinstance(row, (list, tuple)) and len(row) > 1:
                lv = max(lv, _i(row[1], 0))
        best_n = max(best_n, n)
        best_lv = max(best_lv, lv)
    return best_n, best_lv


def _mon_feat_lv(mid):
    """特性等级（`monster_featlev`，0~6，见 `state` 字段 27 的说明）。"""
    fl = EQ_STATE.get('monster_featlev') or {}
    vals = [_i(fl.get(uc), 0) for uc in _owned_in_chain(mid)]
    return max(vals) if vals else 0


def _mon_power(mid):
    """妖力值 = `monsters[uc][2]`（`_monster_semantics_selftest` 已定：下发 30 字段的第 30 位）。"""
    from state import get_monster_base
    ms = EQ_STATE.get('monsters') or {}
    best = 0
    for uc in _owned_in_chain(mid):
        row = ms.get(uc)
        if not isinstance(row, (list, tuple)):
            row = get_monster_base(uc)
        if isinstance(row, (list, tuple)) and len(row) > 2:
            best = max(best, _i(row[2], 0))
    return best


_CP_BONDS = []

# ★★2026-11-05（用户拍板）：`L81_CP` 里**没有**任何 `CPType=2` 组的怪 —— 持有即算 1 组。
#   为什么不是"改表"：补一组羁绊要策划名单（谁是组员、加成多少），而私服没有这份数据；
#   擅自编一个组会污染 `_monster_identity`/图鉴/战斗加成等**所有**读 L81_CP 的系统。
#   为什么只放全表 0 组的那一只：判据只覆盖"表里确实没有组"的 mid，对其它怪零影响。
_CT_BOND_OWNED_ONLY = frozenset((301001,))


def _cp_bonds():
    """L81_CP 的**怪物羁绊**（`CPType=2`）组员表：`[[member_mid, ...], ...]`（懒加载、只读表）。

    ★★2026-10-25（用户裁定）：CT=62「获得 1 个羁绊」按"该怪所在的那几组**组员全部拥有**"计数。
      · `CPType=1`（种族羁绊）的 `CPID` 是种族/类型 id（如 `1|1|1|1|1`），不是怪物 id，排除；
      · `CPType=2` 的 `CPID` 实测就是组员静态 id（`100301|200701` / `300601|201301|301101`）。
    """
    global _CP_BONDS
    if _CP_BONDS:
        return _CP_BONDS
    try:
        t = _load_json('L81_CP.json')
        h = {c: i for i, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                if 'CPType' not in h or _i(r[h['CPType']], 0) != 2:
                    continue
                ids = [_i(p) for p in str(r[h['CPID']] or '').split('|') if _i(p, 0) > 0]
                if ids:
                    _CP_BONDS.append(ids)
            except Exception:
                continue
    except Exception as _e:
        print('[UG] !! 读 L81_CP 失败(CT=62): %r' % (_e,), flush=True)
    return _CP_BONDS


def _suit_equipped(mid, ids):
    """该怪是否把 `ids` 这几件**套装件**都装备上了。

    装备真值：`monster_ueq[uc]` 三个槽存的是**装备 ucode**，静态 id 在 `equip_id[uc]`
    （`handlers_equip` 6004 就是这样维护的）。套装件表在 `L01_Equipments.SuitID`
    （实测 1013/1014/1015 = 洛阳铲/夜行衣/黑驴蹄子，三行 `SuitID` 都是 `1013|1014|1015`）。
    """
    eqid = EQ_STATE.get('equip_id') or {}
    ueq = EQ_STATE.get('monster_ueq') or {}
    need = set(ids)
    if not need:
        return False
    for uc in _owned_in_chain(mid):
        slots = ueq.get(uc) or ()
        have = set()
        for s in slots:
            k = str(s)
            if k in ('', '0'):
                continue
            eid = _i(eqid.get(k), 0)
            if eid > 0:
                have.add(eid)
        if need <= have:
            return True
    return False


def _bond_count(mid):
    """该怪所在羁绊里"组员全部拥有"的组数（`CPType=2`）。

    ★★2026-11-05（用户拍板，`_ug_full_chain_test.py` 支线取证）：**表内没有组的怪，持有即算 1 组**。
      犬灵 `301001` 在 `L81_CP` 里不属于任何 `CPType=2` 组（它和厌胜犬神 `301101` 是
      **两条**进化链，`SourceID` 不同）⇒ UG **82010**（`Value=301001`「[犬灵]获得1个羁绊」）
      按现表**永远返回 0**，这条支线永远做不完 —— 而它**没有后继**，所以只卡自己。
      2026-10-25 的裁定是"挂起并标注"；2026-11-05 用户改判：**服务端按"持有即算 1 组"处理**，
      **不改客户端表**（改 `L81_CP` 要策划名单，且会污染其它读这张表的系统）。
      ⇒ `_CT_BOND_OWNED_ONLY` 里的 mid：只要背包里真的有它（同源根相同），本组数**至少为 1**。
      ⚠ 边界写清：这是"补数据缺口"不是"放宽判据" —— 只对**表里确实没有组**的 mid 生效，
        且仍然要求"真的拥有这只怪"；没持有 -> 仍是 0（负例在支线套件里钉住）。
    """
    from state import all_monster_ucodes, get_monster_base
    own = set()
    for uc in all_monster_ucodes():
        b = get_monster_base(uc)
        if b:
            own.add(_mid_root(_i(b[0])))
    root = _mid_root(mid)
    n = 0
    for members in _cp_bonds():
        roots = set(_mid_root(m) for m in members)
        if root in roots and roots <= own:
            n += 1
    if n == 0 and root in _CT_BOND_OWNED_ONLY and root in own:
        n = 1
    return n


def _guild_joined():
    """CT=83「加入 1 个妖盟」的真值：**已成为成员**（"提交过申请"不算）。

    ★★2026-10-24（独立审查 阻断 B-1）：修前 61022 没有任何驱动 —— `ug_ct['83']` 永不增长；
      而且**也走不了** `bump_ug_ctv(83, 0)`：这一行的 `Value=0`，`_value_parts` 会把 0 丢掉、
      `bump_ug_ctv` 又对 `key<=0` 直接拒写 ⇒ "按目标记账"这条路对 CT=83 结构性不可用。
      后果不是少一条支线：61022 在任务链上有 **54 个后继主线**，它不完成，中后期主线整段停住。
    ⇒ 改用本模块已定的 A 类做法（与 CT=29/57/85 同一条纪律：**真值直读，不建第二本账**）：
      · 判据是"我这个账号在不在某个会的名册里" —— 读世界键的投影 `sociaty.guild_id`；
      · 投影可能落后于世界键（别人刚审批通过 / 刚建会）⇒ 先 `_sync_my_guild_cache()` 同步一次；
      · 确认过就写 `ug['guild_joined']` 永久标记：**退会不该把已完成的"加入"抹掉**，
        与 CT=28/80 的"永久峰值"同一条纪律（永久主线不许倒退）。
    该函数覆盖全部入会路径（1123 建会直接成为盟主 / 1124 自动入会 / 1126 人工审批 /
    1131 直接加入），因为审批是**跨账号**发生的，只能由申请人自己在读任务时同步到。
    """
    st = _st()
    if st.get('guild_joined'):
        return True
    sg = EQ_STATE.get('sociaty') or {}
    gid = str(sg.get('guild_id') or '').strip()
    if not gid or gid == '0':
        try:
            from handlers_sociaty import _sync_my_guild_cache
            if _sync_my_guild_cache():
                save_state()
        except Exception as _e:
            print('[UG] !! 同步妖盟成员状态失败(CT=83): %r' % (_e,), flush=True)
        sg = EQ_STATE.get('sociaty') or {}
        gid = str(sg.get('guild_id') or '').strip()
    if gid and gid != '0':
        st['guild_joined'] = 1
        save_state()
        return True
    return False


def _sched(row):
    """`schedule` = min(`Quantity`, 该 CT / 该 Value 的累计计数)。

    ★★2026-10-18：两个"同一 CT、多个 Value"的例外，都按 Value 精确判：
      · **CT=31（完成对应章节剧情）** 直接读 `story_seen[Value]`。原因：17 条主线任务
        共用 CT=31、Value 是章节 id（101..117），而 `bump_task(31)` 从未被调用
        （剧情完成只写 `story_seen`）⇒ 共享计数器永远是 0；即便补了 bump，17 个 Value
        共享一个计数器也会把"看完第 1 章"算成"17 章全看完"。
      · **CT=29（副本探索）** 读 `ug['gate_clear']` 里本行 `Value` 的 gate 之和：
        简单关 Value=DefaultID（40001…）、困难关 Value=EliteID（140001…），
        还有 `50001|150001` 这种"任意难度"列表（141019）。共享 `ug_ct` 会让
        11005/21005/…/81002 一次点亮全部；改由 9003 BOSS 击杀按 gate 入账。
      · **CT=57（指定妖怪升到 N 级，2026-10-20）** 直接读 `monster_lv`：`Value` 形如
        `300601&5`，主线 12 行各指一只怪；共享计数器会把"江酌月 5 级"算成 12 个全达成，
        而且升级本来就没有 bump 点（`monster_lv` 就是真值）。
      · 其余 CT 仍走 `ug_ct`（按 ConditionType 聚合，偏宽松，见模块头）。
    """
    ct = _i(row.get('ConditionType'))
    if ct == _CT_STORY:
        val = _i(row.get('Value'), -1)
        if val != -1 and str(val) in (EQ_STATE.get('story_seen') or {}):
            return _i(row.get('Quantity'), 1)
        return 0
    qty = _i(row.get('Quantity'), 1)
    if ct == _CT_GATE_CLEAR:
        counts = _st().get('gate_clear') or {}
        total = 0
        for g in _ct29_parts(row):
            total += max(0, _i(counts.get(str(g)), 0))
        return min(qty, total)
    if ct == _CT_MON_LV:
        # ★★2026-10-20（真机 21016）：CT=57 的 Value 是 `mid&lv`（如 `300601&5`），
        #   含义"**这只怪**升到 N 级"—— 与 CT=31/29 同一类"同 CT 多个 Value"，
        #   共享 `ug_ct` 会把"江酌月 5 级"算成 12 个指定怪全达成。
        #   真值直接读 `monster_lv`（同 CT=31 读 `story_seen` 的思路），不需要 bump：
        #   升级发生在 handlers_cult 的 6001/6002 里，玩家把怪喂到 5 级即达成。
        # ★★2026-10-22（审查判红 B5）：按**进化同源**（L91.SourceID）匹配，不再按精确静态 id。
        #   修前把江酌月（300601）进化成 300602 之后 `_mon_max_lv(300601)` 再也读不到它
        #   ⇒ 91004/101018「江酌月 15/20 级」永久卡死（唯一一只被进化掉就无解）。
        mid, need = _mon_lv_req(row)
        if mid and need:
            return qty if _mon_max_lv(mid) >= need else 0
        return min(qty, _i(_ct_map().get(str(ct)), 0))
    if ct == _CT_PVP_STREAK:
        # ★★2026-10-20（真机 41021「妖怪对战达成1连胜」）：PVP 的 `bump_task(28)` 只在
        #   **当天连胜 >= 10** 时记一次（那是给 A07/T37「10 连胜」用的，见 handlers_pvp），
        #   而这条主线只要 1 连胜 ⇒ 用共享计数永远到不了。
        #   改为读 PVP 真值：当天**最高连胜** `streak_max`（比当前 `streak` 稳 ——
        #   赢了之后再输一场不会把已到手的进度抹掉）。
        # ★★2026-10-22（审查高 H5）：再加一本**永久峰值** `ug['pvp_streak_peak']`。
        #   修前只读当天的 `pvp.streak_max`，而 `_reset_pvp_day()` 跨天会清零
        #   ⇒ 今天"可领奖"、隔天进 PVP 又退回"进行中"（永久主线不该倒退）。
        #   日字段继续给日宝箱（10 连胜箱）用，两本账各管各的。
        p = EQ_STATE.get('pvp') or {}
        need = max(1, _i(row.get('Value'), 1))
        got = max(_i(p.get('streak_max'), 0), _i(p.get('streak'), 0),
                  _i(_st().get('pvp_streak_peak'), 0))
        return qty if got >= need else 0
    if ct == _CT_EVOLVE:
        # ★★2026-10-22（审查高 H2）：Value = 目标**阶数**，"N 个妖怪进化到 M 阶"。
        #   修前任何一次进化都裸增共享计数 ⇒ 2→3 也冒充"进化到 2 阶"，
        #   同一只怪连升两阶会被算成"两只怪"。
        #   真值 = `ug['mon_star']`（ucode -> 该怪达到过的最高阶，只在真的进化成功时写）
        #   ⇒ 按"达到 >= M 阶的**不同 ucode** 个数"计数，天然去重。
        m = _st().get('mon_star')
        if not isinstance(m, dict):
            return 0
        need = _i(row.get('Value'), 0)
        if need <= 0:
            return 0
        return min(qty, sum(1 for v in m.values() if _i(v, 0) >= need))
    if ct == _CT_RANK:
        # ★★2026-10-22（审查 B7 / 141013「妖神排位达到铜符道士1星」Value=1004）：
        #   要的是"**曾达到**"的段位，不是当季积分换算出来的当前段位 —— 输局会掉、跨季会清零，
        #   永久主线不能跟着倒退。真值 = 1088 结算时写的永久峰值 `ug['rank_peak']`
        #   （按 `P10_PVPLevel.LevelScore` 顺序比较，见 `bump_ug_rank_peak`）。
        #   老档没有峰值时**保守**地只认当前段位（宁可慢一轮，也不凭空点亮）。
        need = _i(row.get('Value'), 0)
        if need <= 0:
            return 0
        peak = _i(_st().get('rank_peak'), 0)
        cur = 0
        try:
            from handlers_qualifying import _dsg as _dsg_now
            cur = _i(_dsg_now(_i((EQ_STATE.get('qualify') or {}).get('points'), 0)), 0)
        except Exception:
            cur = 0
        best = peak if _p10_rank_score(peak) >= _p10_rank_score(cur) else cur
        return qty if _p10_rank_score(best) >= _p10_rank_score(need) >= 0 else 0
    if ct == _CT_TRAIN:
        # ★★2026-10-22（审查高 H3）：Value = 训练场 mainId（21021 要求 `101`）。
        #   修前**任意子关卡首通**都裸 bump 共享计数 ⇒ 打通 102/103 下的关卡也会点亮
        #   "冲虚道场·101 通关"；而且 bump 抛异常时训练状态**已经**置 1、重试不会再 bump
        #   （审查点名的"部分成功裂缝"）。
        #   ⇒ 改成 A 类做法：真值直接读 `train[subid]`，经 `B01.SonStageID` 映射回 mainId。
        #     不存在第二本账，也就没有"账没记上但状态已变"的裂缝。
        main = _i(row.get('Value'), 0)
        if main <= 0:
            return 0
        return qty if _train_main_done(main) else 0
    if ct in (_CT_LAYOUT, _CT_FORMATION_POWER):
        # ★★2026-10-24（P0-3 / P1-0）：布阵妖力是**状态量**，读真值（见 `_formation_power`）。
        need = _i(row.get('Value'), 0)
        if ct == _CT_LAYOUT and need <= 0:
            # ★★2026-10-25（真机准备时发现）：52004「妖力布阵-1 / 完成妖力布阵」的 **Value=0** ——
            #   它不是"妖力达到 0"，而是"存过一次防守阵容"。同一 CT 里 122013 才是阈值 220000，
            #   ⇒ 这里按"是否真的有已保存的防守队"判（`_formation_power() > 0`，与 CT79 同源）。
            return qty if _formation_power() > 0 else 0
        return qty if need > 0 and _formation_power() >= need else 0
    if ct == _CT_MON_COUNT_LV:
        need = _i(row.get('Value'), 0)
        return min(qty, _mon_count_at_lv(need)) if need > 0 else 0
    if ct == _CT_TOWER_FLOOR:
        need = _i(row.get('Value'), 0) - 1000          # 1007 = 第 7 层
        return qty if need > 0 and _tower_plies() >= need else 0
    if ct == _CT_GATE_DEGREE:
        _p = str(row.get('Value') or '').split('&')
        _g = _i(_p[0])
        _g = _CT70_GATE_ALIAS.get(_g, _g)              # 100001 -> 1000001（笔误，见常量注释）
        _n = _i(_p[1], 100) if len(_p) > 1 else 100
        if _g <= 0 or _n <= 0:
            return 0
        return qty if _explore_degree(_g) >= _n else 0
    if ct in (_CT_BATTLE_COUNT, _CT_DMG_DEALT, _CT_DMG_TAKEN):
        # ★★2026-10-25（P1-4）：读 1234 上报的**真值**（见 `_dmg_total`），不再"无驱动"。
        need = _i(row.get('Value'), 0)
        if need <= 0:
            return 0
        kind = {_CT_BATTLE_COUNT: 'battles',
                _CT_DMG_DEALT: 'atk',
                _CT_DMG_TAKEN: 'def'}[ct]
        return min(qty, _dmg_total(kind, need))
    if ct in (_CT_SKILL_COUNT, _CT_SKILL_LV, _CT_FEAT_LV, _CT_POWER):
        # ★★2026-10-25（P1-4）：这四类的真值服务端本来就有（技能槽/技能袋/featlev/妖力值），
        #   修前只是因为 `_sched` 不认识它们而落进共享 `ug_ct`（永远 0）。
        mid, need = _mon_lv_req(row)
        if not mid or need <= 0:
            return 0
        if ct == _CT_SKILL_COUNT:
            got = _mon_skill_stats(mid)[0]
        elif ct == _CT_SKILL_LV:
            got = _mon_skill_stats(mid)[1]
        elif ct == _CT_FEAT_LV:
            got = _mon_feat_lv(mid)
        else:
            got = _mon_power(mid)
        return qty if got >= need else 0
    if ct == _CT_SUIT:
        # ★★2026-10-25（用户裁定）：`Value` = `mid&套装件|件|件`，判"该怪三槽里凑齐这套"。
        _p = str(row.get('Value') or '').split('&', 1)
        _mid = _i(_p[0], 0)
        _ids = [_i(x) for x in (str(_p[1]) if len(_p) > 1 else '')
                .replace(',', '|').split('|') if _i(x, 0) > 0]
        if _mid <= 0 or not _ids:
            return 0
        return qty if _suit_equipped(_mid, _ids) else 0
    if ct == _CT_BOND:
        # ★★2026-10-25（用户裁定）：`Value` = 怪物静态 id，数"组员齐全的羁绊组"。
        need = _i(row.get('Value'), 0)
        if need <= 0:
            return 0
        return min(qty, _bond_count(need))
    if ct == _CT_GUILD:
        # ★★2026-10-24（独立审查 阻断 B-1）：Value=0 走不了"按目标记账"，读真值（见 `_guild_joined`）。
        return qty if _guild_joined() else 0
    if ct in _UG_VALUE_SCOPED:
        # 其余"同一 CT 多个 Value"的行：只认本行 Value 那一格账（见 _UG_VALUE_SCOPED）。
        # 没有驱动 = 一直 0，**不会**被共享计数误点亮（CT=72/80/83 现在就是这个状态）。
        return min(qty, _ctv_of(row))
    return min(qty, _i(_ct_map().get(str(ct), 0)))


def _mon_lv_req(row):
    """CT=57 的 `Value` -> `(静态 mid, 目标等级)`；形状 `300601&5`（主线 12 行全是这个形状）。"""
    parts = [p.strip() for p in str(row.get('Value') or '').split('&')]
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return int(parts[0]), int(parts[1])
    return 0, 0


def _mid_root(mid):
    """静态 mid -> 进化链**同源根**（`L91_Status.SourceID`）。

    ★★2026-10-22（审查判红 B5）：同一条进化链上所有阶段的 `SourceID` 相同
      （实测 300601/300602/300603/300604 都是 300601；201301→201302 链是 201301）。
      CT=57 的 `Value` 写的是**未进化形态**的 mid，所以必须按同源根比较，
      否则怪一旦进化，服务端就再也认不出"这就是任务要的那只"。
    ⚠ 查不到 `L91` 行（表缺/新怪）时退回 mid 本身 —— 与"不编造"一致：
      此时只匹配精确 mid，行为与修前相同，不会误把别的怪算进来。
    """
    try:
        mid = _i(mid)
        from state import _l91_info
        info = _l91_info().get(mid)
        if info and _i(info[2], 0) > 0:
            return _i(info[2])
    except Exception:
        pass
    return _i(mid)


def _mon_max_lv(mid):
    """该静态 id（**含其进化链上的所有形态**）的怪里最高等级；没有 -> 0。

    多只同源取最高；初始三只（不在 `monsters` 字典里）也由 `all_monster_ucodes` 覆盖。
    """
    from state import all_monster_ucodes, get_monster_base, get_monster_lv
    root = _mid_root(mid)
    best = 0
    for uc in all_monster_ucodes():
        try:
            b = get_monster_base(uc)
            if b and _mid_root(_i(b[0])) == root:
                best = max(best, _i(get_monster_lv(uc), 0))
        except Exception:
            continue
    return best


_P10_SCORE = None


def _p10_rank_score(rank_id):
    """`P10_PVPLevel.ID` -> 该档的 `LevelScore` 门槛；查不到 -> -1（= 不可比）。

    段位高低一律按**门槛分数**比，不按 id 大小比 —— id 是策划编号，不保证单调。
    """
    global _P10_SCORE
    if _P10_SCORE is None:
        d = {}
        try:
            t = _load_json('P10_PVPLevel.json')
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                try:
                    d[int(r[h['ID']])] = int(r[h['LevelScore']] or 0)
                except Exception:
                    continue
        except Exception as _e:
            print('[UG80] !! 读 P10_PVPLevel 失败(段位不判): %r' % (_e,), flush=True)
            return -1
        _P10_SCORE = d
    return _P10_SCORE.get(_i(rank_id), -1)


def bump_ug_rank_peak(rank_id):
    """记"**曾达到**的最高段位"（`ug['rank_peak']`，按 P10 门槛比较）；返回 True = 刷新了峰值。

    驱动点：`handlers_qualifying.handle_qualifying_result`（1088 **真实结算之后**）——
    那里才有服务端认可的 `new_dsg`。修前 UG 的 CT=80 没有驱动，141013 永远 0 进度。
    ⚠ 用"峰值"而不是"当前段位"：输局会掉档、跨季 `points` 清零，而 141013 是永久主线
      （审查明确指出"不能裸读当季积分"）。
    """
    try:
        new = _i(rank_id, 0)
        if new <= 0:
            return False
        new_sc = _p10_rank_score(new)
        if new_sc < 0:
            return False
        st = _st()
        cur = _i(st.get('rank_peak'), 0)
        if cur > 0 and _p10_rank_score(cur) >= new_sc:
            return False
        st['rank_peak'] = new
        save_state()
        print('[UG80] 段位峰值 -> %s (LevelScore=%s)' % (new, new_sc), flush=True)
        return True
    except Exception as _e:
        print('[UG80] !! 记段位峰值失败: %r' % (_e,), flush=True)
        return False


def _train_main_done(main):
    """该训练场（`B01_TrainMain.ID` = mainId）是否已通关过**任意一个**子关卡。

    判据 = `train[subid] >= 1`（1027 首次胜利写的"完成未领取"）；1028 领奖后变 2，
    仍然算"通关过"（`>= 1`）。子关卡列表来自 `B01.SonStageID` —— 这就是 1025 回给客户端、
    客户端据此渲染的那张表，服务端不另编一份映射。
    """
    try:
        from handlers_train import B01
        st = EQ_STATE.get('train') or {}
        for (mid, sons, _lv) in B01:
            if _i(mid) != _i(main):
                continue
            return any(_i(st.get(str(s)), 0) >= 1 for s in sons)
    except Exception as _e:
        print('[UG] !! 读训练场状态失败(mainId=%r): %r' % (main, _e), flush=True)
    return False


def _prev_of(row):
    """★前置一律按 `TaskPrevId` 走（`TaskNextId` 实测不可信，见模块头第 2 条）。

    ★★**悬空前置当"无前置"**：实测 405 行里 **26 条**的 `TaskPrevId` 指向**表外的 id**
      （如 12006 指向 `10005`、32019 指向 `31018`），而 `TaskNextId` 也有指向表外的。
      按"前置没领就不能接"的朴素写法，这 26 条**永远停在状态 0（不可接）** ——
      玩家看到的就是"支线里有一批任务永远灰着"。一个**永远无法满足**的引用不可能是设计意图，
      所以这里把它当 `-1`（无前置）。⚠ 这不是"宽松"，是"不让一条坏数据把玩家锁死"。
    """
    p = _i(row.get('TaskPrevId'), -1)
    if p == -1:
        return -1
    _ug()
    if p not in _UG:
        return -1
    # ★★2026-10-22（审查判红 B1）：**自环 / 多跳环一律当"无前置"**。
    #   实测 `UG_07_Task` 主线唯一一个环就是 161008（`TaskPrevId=161008` 指向自己）。
    #   修前它永远满足不了"前置已领"⇒ 永久停在状态 0、且占住客户端唯一的主线首行，
    #   把表里后面的 171001（第 17 章）整章遮死。
    #   与模块头那条"悬空前置当 -1"同一个判据：**一个永远无法满足的引用不可能是设计意图**。
    #   ⚠ 光改这里不够：161008 变成"第二个根"后仍在文件序里排在 171001 前面，
    #     所以 `_block` 还要把**不在主链分量里的行**排到最后（见 `_main_component`）。
    tid = _i(row.get('TaskID'), -1)
    if p == tid:
        return -1
    seen = {tid}
    cur = p
    while cur in _UG and cur != -1:
        if cur in seen:
            return -1                      # 真·多跳环：不让一条坏数据把玩家锁死
        seen.add(cur)
        nxt = _i(_UG[cur].get('TaskPrevId'), -1)
        if nxt == cur:
            # ★★2026-10-24（独立审查 中 M1）：走到"**自环节点**"就停 —— 该节点自己会被上面的
            #   `p == tid` 归一化成根（可领），所以本行**保留**对它的前置要求才是对的。
            #   修前这里多走一步就撞回 `seen`，把**所有以 161008 为前置的 9 条正常支线**
            #   一起判成"无前置" ⇒ 没领 161008 也能进可领奖状态（审查反例：162003 扫荡 66 次即可领）。
            #   真·多跳环（A→B→A）仍按坏数据当 -1，那条意图不变。
            return p
        cur = nxt
    return p


# 主链外的孤点（坏数据造出来的"第二/第三个主线根"）；`_block` 的排序用。
_OFFCHAIN = set()


def _offchain_main():
    """主线里**不在链上**的行 —— 判据：归一化后 `prev == -1`，但又不是主链根。

    主链根 = 文件序里**第一个** `prev == -1` 的主线任务（实测 11001）。
    ★★2026-10-22（审查判红 B1）：这是"客户端唯一首行该显示谁"的判据。
      客户端主线页/主城横幅写死只渲染 `infoList[1]`（`UgTaskMainView.lua:373`、
      `UgTaskTitle.lua:77`），所以只有**链上**的行才配占首行；161008 这种"自己指向自己"
      的坏数据即使可做，也排到后面，不许遮住真正的下一章。

    ★ 为什么**不**按"从 11001 沿 prev 反向可达"算：实测主链会**经过支线根**继续走 ——
      21017 的 `TaskPrevId` 是支线 12004，而 12004 自己就是个根（`prev == -1`）。
      按可达算会把 21017 之后的**全部**章节误判成"不在链上"（本轮实测到达节点只有 7 个）。
      判据只需要回答"这条主线是不是另起了一个根"。
    """
    if _OFFCHAIN:
        return _OFFCHAIN
    _ug()
    root = None
    for tid in _ORDER:
        row = _UG.get(tid)
        if row is None or _i(row.get('TaskType'), 1) != _TYPE_MAIN:
            continue
        if _prev_of(row) != -1:
            continue
        if root is None:
            root = tid
            continue
        _OFFCHAIN.add(tid)
    return _OFFCHAIN


def _status(row):
    """0 不可接 / 1 未接 / 2 进行中 / 3 未领奖 / 4 已领奖（推导，不落盘）。

    ★★2026-11-05（`_ug_full_chain_test.py` 全链取证）：**主线不因"支线前置未领"而不可接**。
      实测 `UG_07_Task` 里有 2 条主线的 `TaskPrevId` 指向**支线**：
        · 21017「仗剑走天涯-2」← 支线 **12004**（同一套签名任务链 1→2）
        · 91005「仗剑走天涯-4」← 支线 **32037**（← 21017 ← 12004）
      `Type` 字段明确写着 12004/32037 是 `TaskType=2`（支线）。修前 `_status` 一律要求
      "前置已领" ⇒ 只玩主线的玩家到第二章就**永久看到 21017 不可接**，而客户端主线页
      只渲染主线（`UgTaskMainView` 只发 1311 的主线块），玩家没有任何入口知道
      "得先去支线里做《仗剑走天涯-1》" —— 主线在第 2 章静默断掉。
      ⇒ 判据改为：**主线的前置是支线时不作要求**；支线之间的前置照旧。
      ⚠ 不能改成"`_prev_of` 对主线返回 -1"：那会让 21017/91005 变成第二个/第三个
        "主线根"，被 `_offchain_main` 判成链外孤点**排到列表最后** → 主线首行彻底看不到它
        （161008 自环就是这个形态，见 `_offchain_main` 的注释）。链的形状必须保留。
    """
    st = _st()
    tid = _i(row.get('TaskID'))
    if str(tid) in st['claimed']:
        return 4
    prev = _prev_of(row)
    if prev != -1 and str(prev) not in st['claimed']:
        _prow = _UG.get(prev) or {}
        _cross = (_i(row.get('TaskType'), _TYPE_MAIN) == _TYPE_MAIN
                  and _i(_prow.get('TaskType'), _TYPE_MAIN) != _TYPE_MAIN)
        if not _cross:
            return 0                                # 前置任务还没领（跨类型除外）
    if _i(row.get('TaskAcceptLevel'), 1) > _i(EQ_STATE.get('player_lv'), 1):
        return 0                                    # 等级不够
    ap = _i(row.get('TaskAcceptPlotID'), -1)
    if ap != -1 and ap in _STORY_IDS and str(tid) not in st['aplot']:
        return 1                                    # 有前置剧情且没看过 -> 未接
    return 3 if _sched(row) >= _i(row.get('Quantity'), 1) else 2


def _block(task_type):
    """一个页签的 `[总任务数, 已完成数, infoList]`；infoList 行 **8 槽**。

    ★★2026-10-20（真机「任务页只显示第一条主线」）：**主线段把「已领奖(4)」稳定排到最后**。
      客户端主线页与主城横幅都写死取首行 —— `UgTaskMainView.lua:373` `local netData =
      self.List1[_idx]`（`_idx` 恒为 `selList1=1`）、`UgTaskTitle.lua:77-78`
      `local selIndex = 1` / `UgTaskData:getTaskInfo(TASK_TYPE_1, selIndex)`；
      而本函数按表文件序回全部 81 条 ⇒ **已领的 11001 永远占第一格**：
      真机 2026-10-20 账号 u0001（`claimed={11001:1}`）主城横幅仍在显示 11001 且带「领取」。
      `UgTaskData:setNtcData()` 收到 `STATUS_FR` 会 `table.remove`，说明"已领的不该留在列表里"
      本来就是客户端语义；这里只做**稳定排序**（4 挪末尾、其余保持链序），
      ⇒ 首行 = 当前该做的那条，11005 才有机会上屏。
      ⚠ 只动主线：支线列表由客户端自己 `table.sort`（`UgTaskData:getTaskList`），服务端顺序无意义。
    """
    _ug()
    st = _st()
    tids = _BY_TYPE.get(task_type, [])
    rows = []
    done = 0
    for tid in tids:
        row = _UG[tid]
        stat = _status(row)
        if stat == 4:
            done += 1
        ap = _i(row.get('TaskAcceptPlotID'), -1)
        fp = _i(row.get('TaskFinishPlotID'), -1)
        # ★★2026-10-22：**已领奖(4) 一律按"满进度"上报**。已领 = 必然达成过门槛，而它读的那本账
        #   可能已经被迁移/跨天清空 —— 例如本轮把 `ug_ct['23']=3` 搬走后，已领的 21012 若按账本
        #   算会显示 0/1，看上去像"领了却没完成"。`claimed` 本身才是"完成过"的凭据。
        sched = _sched(row)
        if stat == 4:
            sched = max(sched, _i(row.get('Quantity'), 1))
        rows.append([
            tid,
            _i(row.get('TaskType'), task_type),
            sched,
            stat,
            1 if (ap != -1 and str(tid) in st['aplot']) else 0,     # [5] TaskAcceptPlot：1 已回顾
            1 if (fp != -1 and str(tid) in st['fplot']) else 0,     # [6] TaskFinishPlot
            0 if (ap != -1 and str(tid) not in st['aplot']) else 1,  # [7] aStory：0 = 首次（客户端 ==0）
            0 if (fp != -1 and str(tid) not in st['fplot']) else 1,  # [8] fStory
        ])
    if task_type == _TYPE_MAIN:
        # 稳定排序：①已领奖的挪到末尾 ②**链外孤点**再挪到后面（见 `_offchain_main`）。
        #   主链是 11001→…→171001；161008 因为 `TaskPrevId` 指向自己而成了"第二个根"，
        #   修前它既永远停在状态 0、又按文件序排在 171001 之前 ⇒ 首行永远显示它。
        #   Python 的 sort 是稳定的，同 key 内仍是表内链序。
        off = _offchain_main()
        rows.sort(key=lambda r: (1 if r[3] == 4 else 0, 1 if r[0] in off else 0))
    return [len(tids), done, rows]


# ============================================================================
# 1311 任务界面信息
# ============================================================================
def handle_ug_task_info(body_json=None):
    """1311 -> `[1,[主线, 支线, 悬赏, 日常]]`，每块 `[总数, 完成数, infoList]`。

    ⚠ `UG_07_Task` **只有 `TaskType` 1/2**（实测 81 + 324）⇒ 悬赏/日常两块回**空段**
      `[0, 0, []]`（悬赏是另一套系统：`T43_TravelTasks` + 1450~1454）。
      客户端对空段是安全的（`_data[1] or 0` / `_data[3] or {}`），但**槽位不能少**：
      四个 `table.insert(msgData, ...)` 决定了下标，少一个后面全错位。
    """
    # 顺序固定 主线/支线/悬赏/日常（客户端 TASK_PAGE_INDEX 1..4 与 net 里四段的默认值同序）
    return _out(_block(_TYPE_MAIN), _block(_TYPE_SIDE), [0, 0, []], [0, 0, []])


# ============================================================================
# 1312 首次播放剧情 / 对话
# ============================================================================
def handle_ug_task_play(body_json):
    """1312 req `{taskId, beforeAndAfter}` -> **纯 ack** `[1,[]]`（客户端只回调 `m_callFunc()`）。

    ⚠ 但**副作用是这个号的唯一作用**：不落盘的话 `aStory`/`fStory` 永远是"首次"，
      前置剧情按钮每次进界面都亮着（且 `TaskAcceptPlot` 的"回顾"标记永远为 0）。
      `beforeAndAfter`：**1 = 前置**(`TaskAcceptPlotID`) / **2 = 后置**(`TaskFinishPlotID`)
      —— 出处客户端 `UgTaskConfig.lua:36-38` 的 `STORY_TYPE`（1/2），`NetUgTaskPlay`
      把 `_playType` 原样写进这个键。修前把 0/1 当成前置/后置，**反了**。
      ⚠ 实测 `TaskFinishPlotID` **405 行全是 -1**（没有后置剧情），所以 2 这一支现在打不到；
      仍然实现它，因为客户端会按 `STORY_TYPE.TYPE_2` 发（表什么时候补上就什么时候生效）。
    """
    req = _req(body_json)
    tid = _i(req.get('taskId'))
    ba = _i(req.get('beforeAndAfter'), 0)
    _ug()
    row = _UG.get(tid)
    if row is None:
        # ★2026-11-06 真机：客户端在“章节故事”路径可能送 0/未知 taskId，原来回 [0]
        #   -> 客户端弹「错误，消息体为空」。1312 只是“记录前置/后置剧情已看”的 ack，
        #   未知 id 时**直接 ack**（不改任何状态），不让错误弹窗卡住剧情。
        return b'[1,[]]'
    st = _st()
    # ★★2026-10-18：客户端 `STORY_TYPE.TYPE_1=1(前置)/TYPE_2=2(后置)`
    #   （`UgTaskConfig.lua:36-38`；`NetUgTaskPlay` 把 `_playType` 原样写进
    #   `beforeAndAfter`）。修前把 1 记成 `fplot`、else 记成 `aplot` —— **反了**，
    #   导致 accept 剧情播完 `aplot` 永远空、1311 回 status=1，客户端"不可接"
    #   分支**无条件**弹「角色等级不足」（`UgTaskTitle.lua:158-166`）。
    #   现在：2 -> `fplot`；1/缺省(0) -> `aplot`（客户端从不发 0，缺省按前置处理）。
    if ba == 2:
        st['fplot'][str(tid)] = 1
    else:
        st['aplot'][str(tid)] = 1
    save_state()
    return b'[1,[]]'


# ============================================================================
# 1313 完成任务领取奖励
# ============================================================================
def handle_ug_task_finish(body_json):
    """1313 req `{taskId}` -> `[roleInfo, dropInfo]`（`msg[3]` 可选：客户端那个消费点被注释掉了）。

    码：本 net 的 `m_errorCode` 只有 `[0]`（"错误，消息体为空"）⇒ 一切拒绝都用 `[0]`，
    **不编造它不认识的码**（客户端 `showTips(m_errorCode[state], state)` 对未知码会
    `getLocal(state)` 查到别的文案，弹一个与操作无关的提示）。
    """
    from handlers_task import _grant_group
    req = _req(body_json)
    tid = _i(req.get('taskId'))
    _ug()
    row = _UG.get(tid)
    if row is None:
        return b'[0]'
    if _status(row) != 3:                       # 只有"未领奖"能领（含防重复领）
        return b'[0]'
    groups, ok = _grant_group(_i(row.get('RewardID')))
    if not ok:
        return b'[0]'
    st = _st()
    st['claimed'][str(tid)] = 1
    save_state()
    return _out(_role_info(), _drop(groups))
