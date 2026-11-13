# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 每日任务系统 (2026-09-06 按 NetGetDailyTask/NetAchiveTaskReward 契约)
#   1068 DAILY_TASK        req {} -> [1,[dailyTaskData, dayvitality, dayVitalityData, weekvitality, weekvitalitydata]]
#        dailyTaskData = [[taskid, num, state],...] (0未完成 1可领 2已领)
#        dayVitalityData = [[taskid, num, state],...] (T38_ActivityReward 活跃度宝箱)
#   1069 ACHIVE_TASKREWARD req {taskid} -> [1,[roleInfo, dropInfo]]; 码: 537任务ID错 538未完成 539已领
#   数据: T37_DailyTasks(ID,ConditionType,Quantity,RewardID=T02_Reward GroupID,OpenLevel); T38_ActivityReward(活跃度箱)
#   进度: 事件驱动 bump_task(ct) (召唤/吞噬/强化/技能升级/探索...) + 状态盘点 eval (金币类); 每日重置按日期
import json, os, random, time
from state import EQ_STATE, save_state, player_add_exp, vitality_timerid, gold_earn_today, gold_spend_today
from state import VITALITY_MAX          # 体力上限的唯一出处（见 state.VITALITY_MAX 的注释）
from state import explore_role_id as _explore_role_id

from state import table_dir as _table_dir   # ★2026-10-02 (审计 §1.6): 表根不再写死盘符
_DATA = _table_dir()

def _load_json(name):
    # ★v60 闭环 v58 §10 缺口 #3: 改走 state.load_table —— 按「尾字段 u32 长度」精确剥离,
    #   不再用 c[:c.rfind(']')+1]（长度字段含 0x5D 时会切错 -> 调用方 except -> 静默空表,
    #   实测 L98_AIPriority / T32_Achievement）。护栏: work/_v60_legacy_tail_guard_test.py
    #   ⚠ 必须**逐行复制**: load_table 的结果是**带缓存的同一个对象**, 而旧实现每次返回全新对象。
    #     调用方若原地改行(排序/填字段)会污染全局缓存 -> 这里保持"每次都是新对象"的原语义。
    from state import load_table as _lt
    return [list(_r) for _r in _lt(name, _DATA)]

# T37 每日任务: (id, ct, qty, groupid, openlv)
_T37 = []
# ★2026-10-16（第 32 轮）：`T37.Value` 单独存一张表 —— 它不参与 `_T37` 的 5 元组
#   （那样要改所有解包点），但"按 Value 匹配"的任务需要它（见 `bump_task_value`）。
#   CT=29 的七行（1020~1026）就是靠 Value 区分"哪一关"的：140001/150001/…/2000001。
_T37_VALUE = {}
try:
    _t = _load_json('T37_DailyTasks.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _T37.append((int(_r[_h['ID']]), int(_r[_h['ConditionType']]), int(_r[_h['Quantity']] or 1),
                         int(_r[_h['RewardID']] or 0), int(_r[_h['OpenLevel']] or 1)))
            try:
                _T37_VALUE[int(_r[_h['ID']])] = int(_r[_h['Value']] or 0)
            except Exception:
                _T37_VALUE[int(_r[_h['ID']])] = 0
        except Exception:
            continue
except Exception:
    pass
# T38 活跃度箱: (id, requirement, groupid)
_T38 = []
try:
    _t = _load_json('T38_ActivityReward.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _T38.append((int(_r[_h['ID']]), int(_r[_h['ActivityRequirement']] or 0), int(_r[_h['RewardID']] or 0)))
        except Exception:
            continue
except Exception:
    pass
# T02_Reward 组: groupid -> [(type, obj, qty, rate), ...]
#   ★2026-09-15 (v54): 第 4 元素 Rate 是**独立概率** (不是权重)。
#   证据: 客户端把同一张表同一列直接当百分比显示 ——
#     legend/VLegendSelect.lua:72  `local Rate = tonumber(data.Rate)*100` -> :84 `Rate.."%"`
#     pve/PVEView.lua:419 / map/MapTips.lua:357 同款。
#   (注: treasure 系统的 VTreasureBoxInfo.lua:74 用 `Rate/totalRate*100` 是**另一张表**的约定, 别混。)
#   旧实现只取 (type,obj,qty) 直接全发 -> 凡组里带 Rate<1 的行都被发成"必掉"。
# ★v54e: 私服倍率只作用于这些**可堆叠资源**类型 (客户端 REWARD_NEWTYPE 与 _grant_group 的分支对应):
#   2 道具 / 3 铜钱 / 6 鬼道 / 7 妖魂 / 8 唤妖符 / 9 体力 / 17 经验 / 18 宝箱 / 21 活跃度
#   装备(1)/妖怪(4)/元灵(5)/皮肤(14)/头像(22/23)/礼包(98) **不放大** —— 它们是"件", 放大既不合理也会刷爆格子。
_MULT_TYPES = (2, 3, 6, 7, 8, 9, 17, 18, 21)
_T02 = {}
try:
    _t = _load_json('T02_Reward.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _g = int(_r[_h['GroupID']] or 0)
            _T02.setdefault(_g, []).append((int(_r[_h['Type']] or 0), int(_r[_h['ObjectID']] or 0),
                                            int(_r[_h['Quantity']] or 0), float(_r[_h['Rate']] or 0)))
        except Exception:
            continue
except Exception:
    pass

# 事件条件类型 -> 任务 bump (客户端 T37 ConditionType 语义见 T37 表)
CT_LOGIN, CT_DRAW = 1, 2
CT_PVP_BATTLE = 8
CT_DEVOUR, CT_INTENSIFY, CT_MONTHCARD, CT_WEEKCARD = 9, 10, 14, 15
CT_SKILLUP, CT_SKILLCOMPOSE = 18, 19
# 只在真实事件完成后调用 bump_task。CT=35/36 等无事件源的类型不得伪造进度；
# 每类的证据、未接项和判定口径见 `_task_ct_coverage.py`。
CT_VITALITY, CT_TOWER = 25, 4
CT_PVP_STREAK = 28
CT_SOCIATY_DONATE, CT_SOCIATY_OFFER, CT_SOCIATY_CONTRIB = 34, 35, 36
# ★★2026-10-17（真机任务链收口）：把 A07 活动 1 用到的 4 个 CT 接到真实事件源。
#   判据 = 表文案 + 客户端发送点（the client network config）：
#     43 赠送好友体力     A07 10513 "赠送好友体力"        <- 2106 FRIEND_SEND（`_friend_coin` 的 sent 成功分支）
#     44 成功参与妖神排位 A07 10212/10310/10511           <- 1091 PVPQUALIFYING_START（开战即"参与"）
#     45 进化妖怪         A07 10104/10313 "将进化N个妖怪到M阶" <- 6003 CULTIVATE_EVOLVE 成功
#     47 扫荡关卡         A07 10512 "扫荡关卡"            <- 1119/1120 扫荡结算（`_do_sweep` 真正扣了体力）
#     30 排行榜点赞/围观大佬 A07 10211 + T37 同 qty=9     <- 1172 COMMENT_PRAISE（排名/评论页的点赞）
#   36 妖盟财富/个人贡献：**有源**（1133 捐献 -> `_guild_add_wealth`），按真实入账财富值 bump；
#   不是伪造 —— 旧注释里"CT=35/36 无事件源"只对 35（妖盟祭献）成立。
CT_FRIEND_GIFT = 43
CT_QUALIFY = 44
CT_EVOLVE = 45
CT_SWEEP = 47
CT_PRAISE = 30
CT_TREASURE = 12
# ★2026-10-16（任务书 D④）：CT=11「妖力布阵」的事件源 = **7002 保存妖力布阵**，
#   与"1090 排位赛重连确认"无关（2026-10-15 DECISIONS 已否掉那条替代）。
#   落点在 `handlers_pvp.handle_pvp_save_bat_arr`：只有**阵容真的变了**才 bump 一次。
CT_LAYOUT = 11
# ============================================================================
# ★★2026-10-16（任务书 E）：把 7 个"有真实事件源"的 GAP 逐条接上。
# 判据全部落在**表文案**（`T37_DailyTasks.Text`）与**客户端 msgid 常量**
# （the client network config）上，逐个写明：
#   5  妖神传说  "击败任意1只妖神"                      <- 9005 WAR_BOSSRESULT（BOSS 结算）
#         ⚠ 9005 也可能承载非妖神 BOSS（大蛇），所以只在 `info ∈ 今日 T25.MonsterGroup*` 时计
#   6  妖怪异闻  "在妖魔异闻中战胜任意1个妖怪"           <- 9006 WAR_PVERESULT
#         （客户端 `WarStageUnits.lua:823-827` PVE 分支的注释就写着"妖魔异闻改传参数"）
#   7  妖怪入侵  "在妖怪入侵胜利结算，获得奖励"           <- 9001 AGGRESS_RESULT（常量名即语义）
#   17 寄妖      "对任意装备进行2次寄妖"                 <- 6007 CULTIVATE_ENCHANT（"养成装备寄妖"）
#   23 妖魔肃清  "在任意关卡中累计击败3只妖卵"           <- 9003/9004 胜利且 info ∈ T37.Value 的 13 个组
#   24 调皮蛋    "在任意关卡中累计打碎50个罐子"           <- 1704 且 S03_Spawn.Type==1（罐子/莲蓬）
#   33 弈局战斗  "参与1次弈局战斗"                       <- 1153 结算成功（参与才会走到结算）
# ⚠ 仍然**不接**：20（极品装备：T37.Value=4|5 指向质量档，但 L01 没有质量列，判据无源）、
#   21（降妖达人：7 波通关的完成信号尚未闭合）、22（寻宝达人："隐藏宝箱"在 S03 里
#   有 IsHiddenSweep/UnlockType 两种可能读法，未定）、12/13/16/30/35/36（NO_SOURCE）。
#   ★2026-10-18：3 已拍板并接上（见下方 `CT_CLEAR`），不再是缺口。
CT_LEGEND = 5        # 妖神传说 9005
CT_YMYW = 6          # 妖怪异闻 9006
CT_AGGRESS = 7       # 妖怪入侵 9001
CT_ENCHANT = 17      # 寄妖 6007
CT_EGG = 23          # 妖魔肃清（击败妖卵）9003/9004
CT_POT = 24          # 调皮蛋（打碎罐子）1704
CT_EQUIP = 32        # 装备/更换装备 6004（UG 12004「仗剑走天涯-1」/ 21017「-2」）
CT_TRAIN = 85        # 冲虚道场法阵通关 1027（UG 21021「入门法阵通关」）
CT_CHESS = 33        # 弈局战斗 1153
# ★★2026-10-16（E 第二批）：两个"上一轮以为无源、实际客户端自己写着判据"的：
#   20 极品装备  "获得1件紫色以上质量装备"，Value='4|5'
#       → 品质 = `L01_Equipments.Class`（客户端 `SDEquip.lua:127 getStatic_Quality() = m_Class`，
#         同文件 getColorByQuality: 4=Purple / 5=Red）⇒ 入包时 Class>=4 计一次
#   22 寻宝达人  "在任意关卡中开启8个隐藏宝箱"
#       → "隐藏" = `S03_Spawn.Type==4`（客户端 `S03_Spawn.lua:149-156 getStatic_IsHidden()`
#         的**唯一判据就是 `m_Type == 4`**；`IsHiddenSweep` 是"扫荡时隐藏"，不是这个概念）
CT_EQUIP_QUALITY = 20
CT_HIDDEN_BOX = 22
# ★★2026-10-16（E 第三批）：21 降妖达人「通关妖怪入侵关卡(7波)1次」。
#   判据在表里：`T26_YGRQ.TotalRound`（**48 行全部 = 7**），关卡 id 就是 1010 的 `pointid`
#   （客户端 `NetAggressWave.lua:52  msg["pointid"] = _levelId  -- 关卡id`，
#     而 `_levelId = tonumber(AGGRESS_FIGHT_ID)` = `T26_YGRQ.ID`；
#     `StageDelegateAggress.lua:56-59` 的 `totalWave = data.TotalRound` + `setWaveTimes(curWave,totalWave)`
#     正是界面上的"第几波/共几波"）。
#   ⇒ 通关 = 9001 胜利结算时，本局**最后一次上报的波数 ≥ 该关卡 TotalRound**。
#   ⚠ 波数从 1 起（`MAggressData.lua:25 data.wavenum = 1`；`NetAggressReconnection.lua:61-65`
#     `if wavenum == 0 then wavenum = 1`、`deadnum = (wavenum-1)*5`），不是 0 起。
CT_AGGRESS_CLEAR = 21
# ★★2026-10-18（用户拍板，口径 A）：CT=3「通关任意关卡」= **9003 胜利且击杀该关 BOSS**。
#   判据：`gateid ∈ S01_Level` 且 `info ∈ S12_SceneExplore[gateid]` 里 **Type==1** 的
#   `Value1`（= "首杀BOSS-…" 那一行指向的 T01 战斗组；客户端
#   `exploreUnlockTool:killMonsterVictory():728` 用的正是同一条 `fightId == Value1 && T01.Type==2`）。
#   **每个 explore run 最多记 1 次**（`explore_prog.run` 去重）：一个关卡可能有 2 个 BOSS
#   （40001 = 1011/1012），同一局杀两个不该算 2 次"通关"；下一局再杀 BOSS 会再记。
#   ⚠ 反例（真机 deploy.log 已钉）：① 9003 中途杀小怪 info=1001/1002/1003 -> 不算；
#   ② 解锁度 100% -> 不算（打过一次之后每次 9003 都报 100%）；③ 1706 退出 -> 不算
#   （所有退出路径都会发 1706）。落点：`handlers_game._xp_ct3_bump`。
CT_CLEAR = 3

def _today():
    return time.strftime('%Y-%m-%d')

def _week_key():
    # 周一起始的 ISO 周键 (客户端"本周活跃度"按周一刷新; 图2 底部"本周活跃度")
    import datetime as _dt
    try:
        _d = _dt.date.today()
        return '%d-W%02d' % (_d.isocalendar()[0], _d.isocalendar()[1])
    except Exception:
        return time.strftime('%Y-%m-%d')

def _wk():
    # 周活跃状态: {'wk': 周键, 'vital': 本周累计活跃, 'box': {boxid: 2}} (T38 1006/1007 = 周箱 500/1000)
    st = EQ_STATE.setdefault('week_task', {})
    if st.get('wk') != _week_key():
        st.clear()
        st['wk'] = _week_key()
        st['vital'] = 0
        st.setdefault('box', {})
        save_state()
    st.setdefault('vital', 0)
    st.setdefault('box', {})
    return st

def _daily_vital_now():
    # 当前每日活跃度 = 各任务已达成值之和 (cap 150, 与 handle_daily_task 一致) + 奖励型活跃递补 (ty21)
    #   ★★2026-11-06（第 13 轮，真机「今日 150 / 本周 63」）：
    #     旧实现写死 `if int(lv) > 1: continue` —— 那是 v60 之前在 `handle_daily_task`
    #     里修掉的同一个 bug（当时官方 T37 的 OpenLevel 全是 1，写死 1 没暴露）。
    #     按官方把 OpenLevel 对齐成真值后（1019=10 / 1031=10 / 1028·1034=15 / 1020-1026=15-27），
    #     这个写死会把**所有等级门槛 >1 的任务**从"今日活跃"里剔掉；而 `handle_daily_task`
    #     的今日活跃用的是真实 `player_lv` ⇒ 同一份状态算出两个数：
    #       今日 150 / 本周 63（"消耗20000铜币" 1031 计入今日、不计入本周）。
    #     连带：1068 显示日箱可领、1070 领取却回 538。
    #   ⇒ 统一为**唯一实现**（本函数），过滤口径 = 真实 player_lv。
    _plv = int(EQ_STATE.get('player_lv', 1) or 1)
    v = 0
    for (tid, ct, qty, gid, lv) in _T37:
        if int(lv) > _plv:
            continue
        v += min(_task_num(tid, ct, qty), qty)
    v += _extra_vital()
    return min(v, 150)

def _week_vital_now():
    # 本周活跃度 = 已完成日的历史转结 (_wk['vital'], 跨天时并入) + 今日实时活跃
    #   ★2026-09-11 修复 (v21 §3.2): 旧实现只回 _wk['vital'] (跨天转结), 当日活跃不实时同步周栏 ->
    #     用户"今日活跃涨到150、下方本周活跃度不动"。_wk['vital'] 只在跨天时累加"昨日活跃",
    #     因此 _wk['vital'] + 今日 既不重复计也无遗漏, 与官方"本周活跃 = 周一至今每日活跃之和"一致。
    return int(_wk().get('vital', 0)) + _daily_vital_now()

def _extra_vital():
    # 奖励型活跃度 (T02 组 ty=21, 每日任务/活跃箱等领取时递补) —— 2026-09-08 实装
    #   ★2026-09-11: 改为**无副作用**读取。旧实现在 day 不匹配时先清零再返回 0,
    #   而 _reset_if_new_day 是"先 _daily_vital_now() 并入周累计, 后清零" —— 于是跨天结算时
    #   _daily_vital_now() 内部一读就把昨日递补活跃抹成 0 -> 昨日 ty21 活跃从本周累计中丢失。
    #   清零职责统一交给 _reset_if_new_day / _add_extra_vital。
    ev = EQ_STATE.get('vital_extra') or {}
    try:
        return max(0, int(ev.get('val', 0)))
    except Exception:
        return 0

def _add_extra_vital(n):
    ev = EQ_STATE.setdefault('vital_extra', {'day': _today(), 'val': 0})
    today = _today()
    if ev.get('day') != today:
        # 跨天首次写入且尚未执行每日重置: 先把"昨日奖励型活跃"并入本周累计再清零 (与 _reset_if_new_day 同口径, 防丢计)
        try:
            _wk()['vital'] = int(_wk().get('vital', 0)) + max(0, int(ev.get('val', 0) or 0))
        except Exception:
            pass
        ev['day'] = today
        ev['val'] = 0
    ev['val'] = int(ev.get('val', 0)) + max(0, int(n or 0))

def _ts():
    return EQ_STATE.setdefault('task_state', {})

def bump_task(ct, n=1):
    # 事件驱动: 所有 ConditionType==ct 且未领取的任务累加 (cap Quantity)
    ts = _ts()
    changed = False
    for (tid, c2, q, gid, lv) in _T37:
        if c2 != int(ct):
            continue
        k = str(tid)
        num, st = ts.get(k, [0, 0])
        if st >= 2:
            continue
        num = min(q, num + n)
        ts[k] = [num, 1 if num >= q else 0]
        changed = True
    # ★2026-09-12: A07 限时任务复用同一套 ConditionType 枚举 -> 顺带累计, 供 3068 报进度
    a7 = EQ_STATE.setdefault('a07_ct', {})
    a7[str(int(ct))] = int(a7.get(str(int(ct)), 0)) + max(0, int(n or 0))
    # ★★2026-10-11（A0 第 10 批 · UG 任务 1311）：同一套 CT 枚举**再累计一本永久的** ——
    #   `UG_07_Task` 的 405 行也用这套 ConditionType，而主线是一条**跨天不重置**的链。
    #   ⚠ 不能复用 `a07_ct`：那个每天清零（`_reset_if_new_day`），拿它算"累计 2000 铜钱"
    #     永远到不了门槛 —— 与 UN15 周任务踩过的是同一个坑（"一周 N 次"不能落每日账本）。
    #   懒 import + 整体吞异常：进度账本再重要也不能影响任务主流程。
    try:
        from handlers_ugtask import bump_ug_ct
        bump_ug_ct(int(ct), max(0, int(n or 0)))
    except Exception:
        pass
    changed = True
    if changed:
        save_state()


def bump_task_value(ct, value, n=1):
    """**带 Value 匹配**的任务累加：只累加 `T37.Value == value` 的那些任务。

    ## 为什么必须有它

    `bump_task(ct, n)` 只按 `ConditionType` 累加、**完全不看 Value**。而 T37 里有一类日常是
    "同一 CT、不同 Value 分属不同任务"：

    | CT | 任务 | Value |
    |---|---|---|
    | 29 | 1020 古刹拜访者 | 140001 |
    | 29 | 1021 道观拜访者 | 150001 |
    | 29 | … 至 1026 古墓拜访者 | 160001/170001/180001/190001/2000001 |

    直接 `bump_task(29)` ⇒ **打通一关就一次点亮 7 个任务**（每个都发一份奖励）。

    ## 口径（与本函数并列的两条纪律）

    * **不动** `a07_ct` 与 UG 那两本"按 CT 聚合"的账 —— 它们是"这个 CT 发生了几次"的全局计数，
      对"按关卡"的 CT 会重复计数（CT=29 的 7 行 Value 各不同，聚合账 +1 就污染 3068/1311）。
    * 与 `bump_task` 一致：已领（state>=2）不再累加；封顶到 `Quantity`。

    ⚠ 接线现状（2026-10-18 审计后更新）：CT=3 **已接**在 9003 上（按用户拍板口径 A，
      见 `handlers_game._xp_ct3_bump`；它只有一个 Value，所以用裸 `bump_task(CT_CLEAR)` 就够）。
      CT=29（困难难度某关）**仍未接** —— 缺的不是本接口，而是"9003 的 `gateid` 在困难难度下
      是否等于 `S00_WorldMap.EliteID`"的真机证据。见 `_task_ct_coverage.py` 的 CT=29 条。
    """
    ts = _ts()
    changed = False
    hit = 0
    for (tid, c2, q, gid, lv) in _T37:
        if c2 != int(ct):
            continue
        if _T37_VALUE.get(tid, 0) != int(value):
            continue
        k = str(tid)
        num, st = ts.get(k, [0, 0])
        if st >= 2:
            continue
        num = min(q, num + n)
        ts[k] = [num, 1 if num >= q else 0]
        changed = True
        hit += 1
    if changed:
        save_state()
    return hit

def _reset_if_new_day():
    # 跨天重置: 全部每日任务/T38日箱清零; 每天自动完成"每日登录"(CT=1); 前一日活跃计入本周活跃 (周一滚动)
    d = _today()
    ts = _ts()
    # v18 迁移: 旧版"已领任务跨天不清"污染 (task_day 已推进但 claimed 项残留) —— 一次性彻底清空
    #   (否则 task_day==today 提前成立, 新版全量重置分支永不触发)
    if EQ_STATE.get('task_reset_ver') != 'v18':
        EQ_STATE['task_reset_ver'] = 'v18'
        # 与常规跨天一致: 旧日活跃(任务进度+奖励型) 先并入周累计再清
        _wk()['vital'] = int(_wk().get('vital', 0)) + _daily_vital_now()
        _ev = EQ_STATE.setdefault('vital_extra', {'day': d, 'val': 0})
        _ev['day'] = d
        _ev['val'] = 0
        ts.clear()
        EQ_STATE['task_day'] = d
        bump_task(CT_LOGIN, 1)
        save_state()
        return
    if EQ_STATE.get('task_day') == d:
        return
    # 1) 昨日的活跃度(任务进度+奖励型活跃) 并入本周累计 (再清除, 保持"今日活跃度"只算当天)
    _wk()['vital'] = int(_wk().get('vital', 0)) + _daily_vital_now()
    EV_STATE_EXTRA_SAFE = EQ_STATE.setdefault('vital_extra', {'day': d, 'val': 0})
    EV_STATE_EXTRA_SAFE['day'] = d
    EV_STATE_EXTRA_SAFE['val'] = 0
    save_state()
    # 2) 任务重置: 全部每日任务/日箱清零 (含 state=2 已领 — 2026-09-08 修复: 旧条件 `state<2` 只清未领,
    #    已领任务永远保持 [x,2] -> 零点后每日任务"没刷新"), 再自动完成"每日登录"
    EQ_STATE['task_day'] = d
    ts.clear()                      # 每日任务 + T38 日箱(b*) + T37 全部一并清 (周一滚动逻辑见上)
    EQ_STATE['a07_ct'] = {}         # 限时任务同套 ConditionType 计数一并跨天清零
    # ★★2026-10-25：A07 CT=45 的去重账本与 `a07_ct` 同一周期（"一天开启一个阶段"）。
    EQ_STATE['a07_mon_star'] = {}
    bump_task(CT_LOGIN, 1)      # 每日登录
    save_state()

def _task_num(tid, ct, qty, gold=None):
    # 状态盘点任务 (金币类) 直接从 state 计算; 其余查 task_state
    if ct == 26:                # 养家糊口: 累计获得 10000 铜币 (当日金币获得净差累计)
        return min(qty, gold_earn_today())
    if ct == 27:                # 有钱任性: 累计消耗 20000 铜币 (当日金币消耗净差累计)
        return min(qty, gold_spend_today())
    ts = _ts()
    return ts.get(str(tid), [0, 0])[0]

def _task_state(tid, ct, qty):
    if ct in (26, 27):
        if _ts().get(str(tid), [0, 0])[1] >= 2:
            return 2
        return 1 if _task_num(tid, ct, qty) >= qty else 0
    return _ts().get(str(tid), [0, 0])[1]

def grant_one(ty, obj, qty, mult=1):
    """按 T02 的**一行**发放并返回 dropInfo 片段列表；发放不了就返回空表（不报该条）。

    ★2026-10-11：从 `_grant_group` 的循环体里原样抽出，**只此一份**实现。
      抽出的原因：在线奖励（1302~1304）的抽奖结果来自 `D301_OnlineReward` 而不是 T02 组，
      但它要发的类型（铜钱/妖魂/道具/体力/唤妖符/经验/宝箱…）**和 T02 完全同一套**
      —— 再写一份就是本仓库已经吃过两次亏的"两套实现一全一半"
      （`role_info_array` 分叉、`_grant_skin_with_dup` 分叉）。
      ⚠ 改本函数等于同时改 T02 全链路，改前先跑 `_grant_types_test.py`。
    """
    ty = int(ty)
    # ★v54e 私服倍率: 只放大"可堆叠资源"; 装备(1)/妖怪(4)/元灵(5)/皮肤(14)/礼包(98) 等"件"按官方 1 份
    if mult != 1 and ty in _MULT_TYPES:
        try:
            qty = int(qty) * int(mult)
        except Exception:
            pass
    groups = []
    if ty in (1, 4):
        # ★v54c (2026-09-15) ③ 落地 —— 装备/妖怪**真实发放**。
        #   旧实现是 `groups.append([1, []])`（空 goodslist）: 客户端 NetDropRewardData
        #   style1/style4 对空表 `pairs` 循环零次 -> **什么都不显示、什么都不给**。
        #   实现**复用** handlers_bag.grant_entries —— 那份已被 v32 修过形状
        #   (L01_Equipments 白名单 + 分配装备 ucode + 解锁装备图鉴 + 返回 14 字段装备行本尊),
        #   不再维护第二份(这正是本仓库出过两次"两套实现一全一半"的地方)。
        #   它返回的是 dropInfo 片段 JSON 字符串, 解析后并入 groups; 发不出去就**不报**该条
        #   (避免给客户端一个空图标, 铁律 #28 "展示了但没入账" 的反面)。
        try:
            from handlers_bag import grant_entries as _ge
            _frag = _ge([[ty, obj, qty]])
            if _frag and _frag != '[]':
                for _g in json.loads(_frag)[1]:
                    groups.append(_g)
            else:
                print('[GRANT] type=%s obj=%s 未能入账, 该条不计入掉落' % (ty, obj), flush=True)
        except Exception as _e:
            print('[GRANT] type=%s obj=%s 发放异常, 该条不计入掉落: %r' % (ty, obj, _e), flush=True)
        return groups
    if ty == 3:                                  # 铜钱
        EQ_STATE['gold'] += qty
        groups.append([3, [[qty]]])
    elif ty == 7:                                # 妖魂
        EQ_STATE['soulcoin'] += qty
        groups.append([7, [[qty]]])
    elif ty == 8:                                # 唤妖符
        EQ_STATE.setdefault('draw', {})['ticket'] = EQ_STATE['draw'].get('ticket', 0) + qty
        groups.append([8, [[qty]]])
    elif ty == 2:                                 # 道具类写道具袋 (obj=静态id; 5元灵/18宝箱见下, 不入道具袋)
        uc = str(obj)
        EQ_STATE['prop'][uc] = EQ_STATE['prop'].get(uc, 0) + qty
        EQ_STATE['prop_id'][uc] = obj
        groups.append([2, [[uc, obj, qty]]])
    elif ty == 6:                                # 鬼道积分
        EQ_STATE['kidou'] = EQ_STATE.get('kidou', 0) + qty
        groups.append([6, [[qty]]])
    elif ty == 98:                               # 礼包/卡 (月卡每日"妖怪礼包"2048 等): 入背包, 客户端使用开
        # ★2026-09-12 (用户反馈 §2 "背包可叠加的要能叠加"): 同静态id 叠加到同一格, 不再每天新建 ucode
        from state import add_giftbag as _addgb
        uc, cnt = _addgb(98, int(obj), int(qty))
        groups.append([98, [[uc, int(obj), int(qty)]]])
    elif ty == 17:                               # 玩家经验 (L101_PlayerExp 表驱动; 2026-09-08 入账实装)
        from state import player_add_exp as _pae
        _pae(int(qty))
        groups.append([17, [[qty]]])
    elif ty == 21:                               # 活跃度 (递补式, 计入今日活跃+本周累计; 2026-09-08 实装)
        _add_extra_vital(int(qty))
        groups.append([21, [[qty]]])
    elif ty == 9:                                # 体力 (T02 组; 2026-09-08 实际入账, 上限20)
        # ★★2026-10-16（任务书 B③）：修前 `min(20, cur+qty)` 后**恒报 `qty`** ——
        #   体力 18 时发 5 点，界面飘 "+5" 而服务端只加了 2（"飘字多于入账"）。
        #   现在只报**真实入账量**；一点都进不去就不报这一条（与 grant_entries 同口径）。
        _before9 = int((EQ_STATE.get('prop') or {}).get('1006', 0))
        _after9 = min(VITALITY_MAX, _before9 + int(qty))
        EQ_STATE.setdefault('prop', {})['1006'] = _after9
        if _after9 > _before9:
            groups.append([9, [[_after9 - _before9]]])
    elif ty == 14:                               # 皮肤 (L98; 重复->皮肤碎片) —— 统一走 handlers_bag._grant_skin_with_dup
        from handlers_bag import _grant_skin_with_dup
        _dup, _g = _grant_skin_with_dup(int(obj))
        groups.extend(_g)
    elif ty == 18:                               # 宝箱 (L103; 入 treasure_bag —— 传世吞噬宝箱 1030 等)
        # ★v32: 走 state.add_box (校验 id ∈ L103)。裸写会让表外 id 进背包,
        #   而 1060 是直接把 treasure_bag 下发的 -> 客户端查不到箱表就崩。
        from state import add_box as _addbox
        _uc2, _tot = _addbox(obj, qty)
        if _uc2 is not None:
            groups.append([18, [[int(obj), int(qty)]]])
    elif ty == 22:                               # 头像框 (T36)
        _fid = int(obj)
        EQ_STATE.setdefault('head', {}).setdefault('frame_own', {})[_fid] = 1
        groups.append([22, [[_fid]]])
    elif ty == 5:                                # 元灵 (L03; 入 ghost 袋)
        try:
            # ★v39 修"发号互不推进": 旧实现自己发号(`next_giftbag_ucode`, 与礼包共用)
            #   再 append, 计数器与 add_ghost 互不推进 -> 会发出重号。
            # ★v51: 完整元灵**不堆叠** -> 必须把**全部新建行**发给客户端。
            _gid5 = int(obj)
            from state import add_ghost_rows as _addgh
            groups.append([5, _addgh(_gid5, int(qty), 0)])
        except Exception:
            groups.append([5, [[qty]]])
    elif ty in (24, 25):                         # 公会活跃/财富 (私有服: 仅展示, 记入公会财富)
        groups.append([ty, [[qty]]])
    else:                                        # 29 等: 状态外, 仅展示
        groups.append([ty, [[qty]]])
    return groups


# ★★2026-10-17（真机复核）：A07 活动的奖励组 80100~80106 在 `data tables` 的
#   T02 里**一行都没有**（表版本差异；A07_2 却引用了它们），而alternate table set
#   `legacy tables\T02_Reward.json` 里是齐的。
#   只补"主表没有的组"、绝不覆盖已有组 —— 否则一次网关改动会悄悄改掉别的奖励。
_T02_FALLBACK = {}
_T02_FALLBACK_TRIED = False


def _t02_fallback(gid):
    global _T02_FALLBACK_TRIED
    gid = int(gid)
    if gid in _T02_FALLBACK:
        return _T02_FALLBACK[gid]
    if _T02_FALLBACK_TRIED:
        return None
    _T02_FALLBACK_TRIED = True
    try:
        from state import load_table as _lt
        d = (os.environ.get('YGZZ_TABLE_DIR_ALT') or '').strip()
        if not d:
            print('[T02FALLBACK] YGZZ_TABLE_DIR_ALT not set; skip', flush=True)
            return None
        f = os.path.join(d, 'T02_Reward.json')
        if not os.path.isfile(f):
            print('[T02FALLBACK] 缺alternate table set，跳过: %s' % f, flush=True)
            return None
        rows = _lt('T02_Reward.json', d)
        h = {c: i for i, c in enumerate(rows[0])}
        for r in rows[1:]:
            try:
                g = int(r[h['GroupID']] or 0)
            except Exception:
                continue
            if g in _T02:
                continue                    # 主表已有的组一律不碰
            try:
                _T02_FALLBACK.setdefault(g, []).append(
                    (int(r[h['Type']] or 0), int(r[h['ObjectID']] or 0),
                     int(r[h['Quantity']] or 0), float(r[h['Rate']] or 0)))
            except Exception:
                continue
        print('[T02FALLBACK] 已补主表缺失的奖励组 %d 个（源: alternate table set）'
              % len(_T02_FALLBACK), flush=True)
    except Exception as _e:
        print('[T02FALLBACK] !! 加载失败(按无该组处理): %r' % (_e,), flush=True)
    return _T02_FALLBACK.get(gid)


def _grant_group(gid, roll=True, mult=1):
    # 发放 T02_Reward 组奖励 -> (dropgroups, ok)
    #   ★2026-09-15 (v54b): roll 默认改为 **True** = 按官方 T02_Reward.Rate 逐行独立判定。
    #     为什么改默认: Rate 是官方写的概率 (客户端 legend/VLegendSelect.lua:72 `data.Rate*100` -> `.."%"`),
    #     而全库 1275 个组里 **507 个 (40%)** 含 Rate<1 的行 —— 旧默认整组照发 = 那些 5% 稀有全是**必掉**,
    #     与客户端面板显示的概率自相矛盾。用户 2026-09-15 拍板: 按官方还原(即"少发"而不是"多发")。
    #     roll=False 仍可用, 只用于复现旧行为 / 对照实验 (单测 §E4)。
    #   ★2026-09-15 (v54e): 新增 mult (私服倍率, 默认 1 = 官方原值)。
    #     只作用于**可堆叠资源** (见 _MULT_TYPES), 装备/妖怪/元灵/皮肤/宝箱/礼包这类"件"仍按官方 1 份 ——
    #     否则一次掉 5 件同 id 装备既不合理也会把装备格子刷爆。
    #     只有"接官方表后明显变穷"的那几条链路会传 mult (= REWARD_MULT), 其余调用点默认 1 不变。
    #   ★2026-10-11: 循环体抽成 `grant_one`（在线奖励 1304 要复用**同一套**按类型发放，
    #     不能再写第二份）。本函数行为逐字不变：抽取前后都用 `_grant_types_test.py` 判绿。
    rows = _T02.get(int(gid))
    if not rows:
        rows = _t02_fallback(gid)
    if not rows:
        return [], False
    groups = []
    for (ty, obj, qty, rate) in rows:
        if roll and rate < 1.0 and random.random() >= rate:
            continue                                 # 本次未抽中, 不进掉落清单
        groups.extend(grant_one(ty, obj, qty, mult))
    return groups, True


def handle_daily_task(body_json=None):
    # 1068: 每日任务列表 + 活跃度 + 活跃度宝箱(日/周)
    _reset_if_new_day()
    # 2026-09-07 盘点补记: 已激活周卡/月卡 -> "今日登入领取周卡/月卡"(T37 1016/1017, ct=15/14) 视为完成 (幂等)
    _cd = EQ_STATE.get('card_daily') or {}
    _now = time.time()
    for _cls, _ct, _days in (('week', CT_WEEKCARD, 7), ('month', CT_MONTHCARD, 30)):
        _st = _cd.get(_cls)
        if _st and float(_st.get('start', 0)) > 0 and _now < float(_st['start']) + _days * 86400:
            bump_task(_ct, 1)
    save_state()
    ts = _ts()
    daily = []
    vital = 0
    # ★v60 修复（按官方对齐 T37_DailyTasks 后暴露出来的真 bug）:
    #   原实现写死 `if int(lv) > 1: continue  # 玩家1级, 只回解锁任务`
    #   —— 它**完全不看玩家的真实等级**, 永远按"1 级"过滤。
    #   以前没炸是因为服务端 T37 的 OpenLevel 全是 1（没有任何任务被卡）;
    #   一旦按官方把 OpenLevel 对齐成真值(1031=10 / 1028·1034=15 / 1038=99),
    #   这个写死就会把**所有**等级解锁任务都吞掉 —— 28 级的玩家也看不到"有钱任性"。
    #   ⇒ 改成读真实 player_lv（语义 = OpenLevel 是解锁等级, 官方的口径）。
    _plv = int(EQ_STATE.get('player_lv', 1) or 1)
    for (tid, ct, qty, gid, lv) in _T37:
        if int(lv) > _plv:
            continue                                # 未达解锁等级
        num = _task_num(tid, ct, qty)
        st = _task_state(tid, ct, qty)
        daily.append([tid, num, st])
    # ★★2026-11-06：今日活跃 = `_daily_vital_now()` **唯一实现**（含 ty21 递补、cap 150）。
    #   旧版在这里又抄了一遍 sum，与 `_daily_vital_now`（1070 日箱门槛 / 周累计都走它）
    #   用了两套等级过滤口径 ⇒ 真机「今日 150 / 本周 63」。不要再写第二份。
    vital = _daily_vital_now()
    day_boxes = []
    for (bid, req, gid) in _T38:
        if bid in (1006, 1007):
            continue                                # 周箱单独构造 (T38 同表 1006/1007 = 周活跃 500/1000)
        bk = 'b' + str(bid)                     # 箱子独立命名空间 (T37 同 id 不冲突)
        claimed = ts.get(bk, [0, 0])[1] >= 2
        st = 2 if claimed else (1 if vital >= req else 0)
        day_boxes.append([bid, min(vital, req), st])
    # 本周活跃度 + 周箱 (T38 1006/1007; 周累计 = 已转结历史日 + 今日实时活跃, 周一滚动 _wk)
    #   (v21 §3.2: 旧实现 wv = _wk['vital'] 不含今日 -> 周栏不动)
    wv = min(1000, _week_vital_now())
    week_boxes = []
    for (bid, req, gid) in _T38:
        if bid not in (1006, 1007):
            continue
        claimed = _wk().get('box', {}).get(str(bid), 0) >= 2
        st = 2 if claimed else (1 if wv >= req else 0)
        week_boxes.append([bid, min(wv, req), st])
    resp = [daily, vital, day_boxes, wv, week_boxes]
    return ('[1,' + json.dumps(resp, separators=(',', ':')) + ']').encode('utf-8')

def handle_daily_task_reward(body_json):
    # 1069: 领取任务/活跃度箱奖励
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        tid = int(req.get('taskid') or 0)
    except Exception:
        return b'[537]'
    _reset_if_new_day()
    ts = _ts()
    k = str(tid)
    # 1) 每日任务 (1069; 活跃度箱走 1070, 无 id 冲突)
    for (t2, ct, qty, gid, lv) in _T37:
        if t2 == tid:
            if ts.get(k, [0, 0])[1] >= 2:
                return b'[539]'             # 已领取
            num = _task_num(tid, ct, qty)
            if num < qty:
                return b'[538]'
            groups, ok = _grant_group(gid)
            if not ok:
                return b'[537]'
            ts[k] = [qty, 2]
            save_state()
            return (json.dumps([1, [_role_info_here(), _drop_here(groups)]], separators=(',', ':'))).encode('utf-8')
    return b'[537]'

def handle_liveness_reward(body_json):
    # 1070: 领取活跃度宝箱 (T38_ActivityReward; 日箱 1001-1005 走当日活跃, 周箱 1006/1007 走本周活跃)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        tid = int(req.get('taskid') or 0)
    except Exception:
        return b'[537]'
    _reset_if_new_day()
    ts = _ts()
    for (bid, req2, gid) in _T38:
        if bid != tid:
            continue
        is_week = bid in (1006, 1007)
        if is_week:
            _w = _wk()
            if _w.get('box', {}).get(str(bid), 0) >= 2:
                return b'[539]'
            if _week_vital_now() < req2:
                return b'[538]'
            groups, ok = _grant_group(gid)
            if not ok:
                return b'[537]'
            _w['box'][str(bid)] = 2
            save_state()
            return (json.dumps([1, [_role_info_here(), _drop_here(groups)]], separators=(',', ':'))).encode('utf-8')
        bk = 'b' + str(bid)
        if ts.get(bk, [0, 0])[1] >= 2:
            return b'[539]'
        ls_vital = _daily_vital_now()              # 含奖励型活跃 (ty21) 递补
        if ls_vital < req2:
            return b'[538]'
        groups, ok = _grant_group(gid)
        if not ok:
            return b'[537]'
        ts[bk] = [req2, 2]
        save_state()
        return (json.dumps([1, [_role_info_here(), _drop_here(groups)]], separators=(',', ':'))).encode('utf-8')
    return b'[537]'

# --- roleInfo/dropInfo 构造 (与 handlers_game._role_info 布局一致, 避免循环 import) ---
#   2026-09-07 修复: 旧版为全静态骨架 (kidou=0/expbuff='0'/卡时间戳=0), 每次 1069/1070
#   领取都会让客户端 setRoleInfo 把周/月卡激活状态、经验/金币 buff、鬼道积分全部清零
#   (用户实测: 领完每日任务卡就"过期"), 现改为与 handlers_game._role_info 相同的动态字段
def _role_info_here():
    # ★★2026-10-02 (审计 §8 / §13 排名 4, P0): 本函数原是 roleInfo 的第 N 份**独立副本**。
    #   副本的字段[11] 停在 v58 已修掉的坏值 `'1'` —— 客户端把该字段按 `id&状态` 拆
    #   (SQGamer.lua:200 guide = tostring(roleInfo[12]) -> :224-232 按 ':' 和 '&' 拆),
    #   发 '1' 得到 guideArray = {{ "1" }} -> info[2] 为 nil ->
    #   NetUpdateInfo:sendMessage 第 24 行 `info[1].."&"..info[2]` 抛错 ->
    #   抽卡回调中断 -> 用户现象「符咒一抽」(cardtype=3) 抽完不出动画、不弹结果。
    #   除 [11] 外, 各副本还与权威实现在 [4] 体力 / [6] 妖塔层数 / [13] 鬼道积分 /
    #   [17][18] 周月卡 buff / [25][26] 卡到期时间戳 等处**各自漂移**(详见 git 历史),
    #   每漂移一处就是一类"用某个功能后 roleInfo 被刷坏"的事故。
    #   ⇒ 统一走**唯一权威实现** state.role_info_array() (它的 docstring 记着 v32/v58 两次事故)。
    from state import role_info_array
    return json.loads(role_info_array())

def _drop_here(groups):
    # 同 handlers_game._drop: 客户端 refreshDropInfo 无单元素数组解包, 原样返回
    return [[], list(groups or [])]
