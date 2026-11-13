# -*- coding: utf-8 -*-
"""handlers_achievement.py —— 成就 1042 / 1044 (审计 §12.1 / §13 排名 8)。

**修前的两个缺陷**
  ① **1042 答非所问**: `game_server.py` 把 1042 路由到 `handle_task_info`(= 1029 TASK_INFO 的
     处理器), 回的是 `[1,[[[1,"主线·初入妖界",1],[2,"收集妖怪",2]]]]` —— 只有 3 个字段,
     而且 id 是 1/2, 而客户端成就界面的组 id 是 **T33 的 1001–1005**
     (`AchievementList.lua:119-123` 查不到就逐行 return) ⇒ **成就界面 5 行全空白**。
  ② **1044 无视 `achieveid`**: 与 1041 共用 `handle_task_get`, 恒定发 100 铜钱、
     整份覆盖 roleInfo、成就状态分毫不动 ⇒ 点"领取"按钮没有任何反应。

**客户端的硬契约 (逐字段反推, 带出处)**
  · 1042 响应 `[1, [ [ [id, state1, reward1, state2, reward2, state3, reward3], ... ] ]]`
    —— **三层, 不是两层**。`NetAchievementInfo.lua:48-65` 逐下标读 `data[1..7]`,
    而 `data` 是 `ipairs(error_verify(msg[1]))` 的元素。
    ⚠ **2026-10-02 修正 (上一版本这里是错的)**: `NetBase:parseMessage` 取的是
    `msg = _recvMsg[#_recvMsg]`(见 `NetBase.lua:25-41` 的循环 —— 不是 `[2]`,
    是**最后一个**元素), 而 1042 的负载正好是 2 个元素 `[statecode, body]`,
    所以 `msg = body`。客户端再取 `msg[1]` ⇒ **body 必须再套一层数组**,
    使 `msg[1]` 是"行数组"本身, 而不是"第一行"。
    旧写法 `[1, 行数组]` 会让 `msg[1]` = 第一行 `[1001,0,0,0,0,0,0]`,
    `ipairs` 遍历到的是 **数字** ⇒ 真机实测报
    `NetAchievementInfo.lua:54: attempt to index local 'data' (a number value)`
    (客户端 `files/bug/2026-10-01 19_13_07.json`, 与服务端
    `19:13:07 | sent msg(1042) response n=145` 同秒)。
    **参照物(正例)**: 7013 的线上字节是 `[1,[[[101,...]]]]` (三层),
    客户端 `NetActivityList.lua` 用 `for i=1,#msg[1] do msg[1][i][1]` 消费。
    `stateN` = **0–100 的百分比**(==100 即完成): `AchievementList.lua:184 Sprite_FinishN` /
    `:185 LoadingBar_N:setPercent`。
    `rewardN` = 0/1, **1 = 有可领取的奖励** -> `AchievementList.lua` 的 `Sprite_TanHaoN`。
    ⚠ `NetAchievementInfo.lua:58-62` 的注释写"0:完成 1:未完成", **与消费方矛盾** —— 以消费方为准。
    ⚠ 组 id 取 **T33_AchievementGroup.json 的 5 行 (1001–1005)**, 不是 T32 的行 id。
  · 1044 响应 `[1, [ roleInfo, dropInfo ]]` —— **两层, 缺一层客户端就崩**
    (`SQGamer.lua:187-189` 与 `NetDropRewardData.lua:71` 会对 -1 取下标)。
    `achieveid` = **T32_Achievement.ID**(如 1001201), 不是组 id。
    奖励 = `T32.Reward` -> **`T02_Reward.GroupID`**(`SDAchievement.lua:39` -> `IconData.lua:156`)。
    ⇒ 走既有的 `handlers_task._grant_group`(全工程唯一"奖励组"入口, 不另造一套发放)。

**进度从哪来 (诚实口径)**
  T32 的 `Type`/`Value1`/`Value2` 是达成条件。**全工程此前没有任何成就进度写入方**
  (`EQ_STATE['achievement']` 只被读过一次), 所以本模块同时提供**写入 API** `note_ach(...)`,
  由各玩法 handler 上报。**没有上报的条件一律算 0 进度** —— 这正是铁律
  「没有真实数据源必须返回 0」: 宁可显示 0%, **绝不编造已完成**。
"""
import json

from state import EQ_STATE, load_table, save_state

# 组 = T33_AchievementGroup.ID; 级别 = T32.LevelID (1..3) <-> 客户端 state1/2/3
_GROUPS = ['1001', '1002', '1003', '1004', '1005']
_LEVELS = ['1', '2', '3']

_T32 = None      # {achieveid(str): row dict}
_T32_BY_LVL = None   # {(SystemID, LevelID): [row, ...]}


def _bootstrap():
    global _T32, _T32_BY_LVL
    if _T32 is not None:
        return
    _T32, _T32_BY_LVL = {}, {}
    try:
        t = load_table('T32_Achievement.json')
        h = {c: i for i, c in enumerate(t[0])}
        for r in t[1:]:
            row = {'id': str(r[h['ID']]), 'sys': str(r[h['SystemID']]),
                   'lvl': str(r[h['LevelID']]), 'type': str(r[h['Type']]),
                   'v1': str(r[h['Value1']]), 'v2': str(r[h['Value2']]),
                   'reward': str(r[h['Reward']]), 'point': int(r[h['AchievementPoint']] or 0)}
            _T32[row['id']] = row
            _T32_BY_LVL.setdefault((row['sys'], row['lvl']), []).append(row)
    except Exception as e:
        print('[ACH] T32 载入失败: %r' % e, flush=True)


def _ach():
    """成就状态容器。**惰性创建**(老存档没有这两个键)。"""
    d = EQ_STATE.setdefault('achievement', {})
    if not isinstance(d.get('counters'), dict):
        d['counters'] = {}
    if not isinstance(d.get('claimed'), dict):
        d['claimed'] = {}
    return d


def note_ach(atype, value1=0, n=1):
    """**进度上报唯一入口** —— 各玩法 handler 达成一次就调一次。

    key = `Type:Value1`(T32 的两列): 同一个 Type 会因为 Value1 不同而分成多组条件
    (例: Type 105 的 Value1 是探索关卡 id)。
    """
    try:
        d = _ach()
        k = '%s:%s' % (atype, value1)
        d['counters'][k] = int(d['counters'].get(k, 0)) + int(n)
        return d['counters'][k]
    except Exception:
        return 0


def ach_value(row):
    """某条成就条件当前的**真实**计数值 (没有上报方 = 0)。

    派生型(见 `_DERIVED_TYPES`)**现算**, 不读计数器 —— 这样就不存在
    "某个玩法 handler 忘了上报 -> 进度静默恒 0" 的失败形态。
    """
    d = _derived_value(row)
    if d is not None:
        return int(d)
    try:
        return int(_ach()['counters'].get('%s:%s' % (row['type'], row['v1']), 0))
    except Exception:
        return 0


# ===========================================================================
# ★★2026-10-02 (D6=C1) 成就剩余 17 个 Type —— 两条新路
# ===========================================================================
# `note_ach` 是**纯累加**, 表达不了"当前值/存量"。补两条路, 按"能不能现算"分工:
#
#   ① **读取时派生**(`_derived_value`): 能从 EQ_STATE 现算的一律现算, **不存第二份真值**。
#      接 99 / 908 / 1103 / 1107 / 1108 / 1113 / 1117 共 7 个。
#      ⇒ 这类**不需要写入点**, 也就不存在"漏了某个事件 -> 静默恒 0"的失败形态
#        (那正是本轮之前"1042 五组恒 0%"的根因)。
#
#   ② **`set_ach` 峰值上报**: 只给"底层量会被**清零/重置**"的三种用 ——
#      1105(抽到 SSR 会把 `draw.luck` 清 0)、803(跨赛季 `qualify.points` 归零)、
#      810(每周一重置)。它们的"曾经达到过"**无法从当前状态反推**, 必须落盘。
#
# **不接的 4 个**(保持 0 —— 铁律: 没有真实数据源就回 0, 绝不编造):
#   808(防守胜利: 7004 那 3 条"被挑战"记录**本身就是伪造的 bot**, 拿它喂成就是在假数据上再叠一层)
#   909/910/911(别人给我的排行赞: `rank_praise` 记的是"我赞别人", 键里没有被赞者身份)
#
# ★ 105（关卡完成度）**已接**（2026-10-08 探索链轮）：Value1 = 大关卡 id，
#   当前值 = 该关卡的**历史最高解锁度**（`handlers_game` 的 `explore_prog[gates][g].best`，
#   由 1705/9003 两处推进）。修前 1705 把 degree 写死 0，所以整个 Type 105 家族
#   （"简单-荒村古刹 50%/100% 解锁"等）**永远拿不到**。
#
# ⚠ 105 接上之后，"因此不可达的成就"只剩 808 那一条（Type 99 的 `10053120`：
#   11 个兄弟 = 803×7 + 808×4）—— 别下一轮当 bug 修。
_DERIVED_TYPES = ('99', '908', '105', '1103', '1107', '1108', '1113', '1117')


def _int(x, d=0):
    try:
        return int(x)
    except Exception:
        return d


def set_ach(atype, value1=0, value=0):
    """**存量/当前值**上报入口(与累加的 `note_ach` 并列)。

    语义 = **幂等设值 + 只升不降(峰值)**。为什么**必须**只升不降:
      达成判定是**读取时现算**的(`ach_done` = 计数 >= Value2), 而 `claimed` 只记"领没领";
      若这里允许把计数改小, `_level_reward` 要求的 `all(ach_done(...))` 会随之抖动 ——
      已经亮过的**整级**奖励又变回未达成。真实来源确实会下降/清零(见上面 ② 的三种)。
    幂等性: 同一个值重复上报不会累积(与 `note_ach` 的关键区别)。
    """
    try:
        d = _ach()
        k = '%s:%s' % (atype, value1)
        old = int(d['counters'].get(k, 0))
        new = _int(value)
        if new > old:
            d['counters'][k] = new
            return new
        return old
    except Exception:
        return 0


def set_ach_reached(atype, current):
    """给"**达到过某个门槛**"型(如 803 段位)按 Type 一次推平。

    为什么要推平而不是只写当前档: 玩家可能**一次跨多档**(连胜连跳), 只写当前档会让
    中间那几档**永远拿不到**。判据 = 该 Type 所有 `Value1 <= current` 的行各置 1。
    """
    _bootstrap()
    n = 0
    try:
        for row in (_T32 or {}).values():
            if str(row.get('type')) != str(atype):
                continue
            if _int(row.get('v1'), -1) <= _int(current, -1):
                set_ach(atype, row['v1'], 1)
                n += 1
    except Exception:
        pass
    return n


def _derived_value(row):
    """能从 EQ_STATE **现算**的 Type -> 当前值; 不是派生型 -> None。

    全部写成"读不到就算 0"的容错形态: 老存档里这些键可能是 list/异形,
    派生读取**绝不能**因为一个坏键把整个 1042 打挂(参照 `handlers_opsact._cleared_groups`
    对 dict/list/set 三形态都容忍的写法)。
    """
    t = str(row.get('type'))
    if t not in _DERIVED_TYPES:
        return None
    try:
        if t == '99':
            # "完成本级别其它所有成就": Value2 == 同 (SystemID, LevelID) 里**除自己外**的行数
            #   (逐组复算过: 1001/L3 16-1=15、1003/L3 5-1=4、1004/L3 10-1=9、1005/L3 12-1=11)。
            # 排除 type 99 自身 -> 避免自引用无限递归。
            rows = (_T32_BY_LVL or {}).get((row['sys'], row['lvl'])) or []
            return sum(1 for r in rows
                       if r['id'] != row['id'] and str(r.get('type')) != '99' and ach_done(r))
        if t == '105':
            # 关卡完成度：Value1 = **大关卡 id**（S12/S01 的那一套，如 40001 简单 / 140001 困难），
            # 值 = 该关卡的历史最高解锁度（0~100）。写入口在 `handlers_game` 的
            # `_xp_note_degree()`（1705 每次下发时记一次 + 9003 战斗结算后记一次）。
            g = EQ_STATE.get('explore_prog')
            if not isinstance(g, dict):
                return 0
            gates = g.get('gates')
            if not isinstance(gates, dict):
                return 0
            rec = gates.get(str(_int(row['v1'])))
            return _int(rec.get('best')) if isinstance(rec, dict) else 0
        if t == '908':
            # 自己的评论**获得**的赞合计。只数 `comment[*].list[*].laud`(玩家自建评论);
            # `comment_laud_override` 是 T42 官方预设评论的赞, 不属于自己 -> 不计。
            tot = 0
            cm = EQ_STATE.get('comment')
            if isinstance(cm, dict):
                for b in cm.values():
                    lst = b.get('list') if isinstance(b, dict) else None
                    if isinstance(lst, dict):
                        for rec in lst.values():
                            if isinstance(rec, dict):
                                tot += _int(rec.get('laud'))
            return tot
        if t == '1103':
            from state import all_monster_ucodes, get_monster_lv
            need = _int(row['v1'])
            return sum(1 for uc in all_monster_ucodes() if get_monster_lv(uc) >= need)
        if t == '1107':
            need = _int(row['v1'])
            sk = EQ_STATE.get('skills')
            if not isinstance(sk, dict):
                return 0
            return sum(1 for e in sk.values()
                       if isinstance(e, (list, tuple)) and len(e) >= 2 and _int(e[1]) >= need)
        if t == '1108':
            need = _int(row['v1'])
            lv = EQ_STATE.get('lv')
            if not isinstance(lv, dict):
                return 0
            return sum(1 for v in lv.values() if _int(v) >= need)
        if t == '1113':
            # ⚠ "阶" = `L91.Star`(取证: handlers_game.py:1587 的注释
            #   `_stage = int(get_monster_star(ucode)) # 进化后该怪的当前星级(=阶)`)。
            #   **不要**复用 `evolve.max_stage` —— 那是活动 A01 的口径, 明确**不能**从背包星级反推;
            #   而 T32 1113 的语义是"**拥有**", 从背包星级反推恰恰正确。两个口径目标相反, 别合并。
            from state import all_monster_ucodes, get_monster_star
            need = _int(row['v1'])
            return sum(1 for uc in all_monster_ucodes() if _int(get_monster_star(uc)) >= need)
        if t == '1117':
            # Value1 = **装备静态 id**(1001=桃木剑 / 1030=青萍剑); 取该装备当前**最高**强化等级。
            eid = str(row['v1'])
            eq = EQ_STATE.get('equip_id')
            lv = EQ_STATE.get('lv')
            if not isinstance(eq, dict) or not isinstance(lv, dict):
                return 0
            return max([_int(lv.get(uc)) for uc, i in eq.items() if str(i) == eid] or [0])
    except Exception:
        return 0
    return None


def ach_done(row):
    """该条件是否达成: 计数 >= Value2 (Value2 为目标值; 0/空 视为未配置 -> 不达成)。"""
    try:
        need = int(row['v2'])
    except Exception:
        return False
    return need > 0 and ach_value(row) >= need


def _level_state(sysid, lvl):
    """该级别 (组, 级别) 的完成度百分比 0..100 = 已达成条件数 / 条件总数。"""
    rows = (_T32_BY_LVL or {}).get((sysid, lvl)) or []
    if not rows:
        return 0
    return int(round(100.0 * sum(1 for r in rows if ach_done(r)) / len(rows)))


def _level_reward(sysid, lvl):
    """该级别有没有**可领取**的奖励 (1/0): 条件全达成 + 至少一条还没领。"""
    rows = (_T32_BY_LVL or {}).get((sysid, lvl)) or []
    if not rows or not all(ach_done(r) for r in rows):
        return 0
    cl = _ach()['claimed']
    return 1 if any(not cl.get(r['id']) for r in rows) else 0


def any_claimable():
    """是否存在任何可领取的成就 (给红点用)。"""
    _bootstrap()
    for (sysid, lvl), rows in (_T32_BY_LVL or {}).items():
        if _level_reward(sysid, lvl):
            return True
    return False


def ach_point_total():
    """已领取成就点合计 -> 客户端 roleInfo[22] 的取数口径。"""
    try:
        cl = _ach()['claimed']
        return sum((_T32 or {}).get(aid, {}).get('point', 0) for aid, v in cl.items() if v)
    except Exception:
        return 0


def _resp(data):
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')


def handle_achievement_info(body_json=None):
    """1042 ACHIEVEMENT_INFO -> `[1,[[gid,s1,r1,s2,r2,s3,r3], ...]]`。

    ⚠ **外层必须再套一层数组**(见模块注释): 客户端拿 `msg[1]` 当"行数组"用。
        · 少这一层 -> `msg[1]` 退化成"第一行"(一串数字) -> `ipairs` 出数字 ->
          真机 `NetAchievementInfo.lua:54: attempt to index local 'data' (a number value)`;
        · 行内 <7 个字段 -> `error_verify` 取到 nil -> `setPercent(-1)`(进度条负数);
        · `msg[1]` 为 nil  -> `ipairs(-1)` **硬 Lua 报错**(所以绝不能用 `[1,{}]` 兜底 1042)。
    """
    _bootstrap()
    out = []
    for g in _GROUPS:
        row = [int(g)]
        for lv in _LEVELS:
            row.append(_level_state(g, lv))
            row.append(_level_reward(g, lv))
        out.append(row)
    return _resp([out])          # ← 三层: [1, [ 行数组 ]]


def handle_achievement_get(body_json):
    """1044 ACHIEVEMENT_GET req{achieveid} -> `[1,[roleInfo, dropInfo]]`。"""
    from handlers_task import _grant_group          # 全工程唯一"奖励组"发放入口
    from state import role_info_array
    _bootstrap()
    try:
        req = json.loads(body_json) if isinstance(body_json, (str, bytes)) else (body_json or {})
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    aid = str(req.get('achieveid') or req.get('id') or '').strip()
    row = (_T32 or {}).get(aid)
    # 542/543/544 是客户端 NetAchievementGet 的既有错误码语义(参数错/未达成/已领取)
    if row is None:
        return b'[542]'
    if not ach_done(row):
        return b'[543]'
    cl = _ach()['claimed']
    if cl.get(aid):
        return b'[544]'
    # ⚠ `_grant_group(gid) -> (dropgroups, ok)` **是二元组**, 必须解包 ——
    #   直接 `list(...)` 会得到 `[groups, ok]`, 客户端拿它当掉落清单会解析出垃圾。
    groups, _ok = ([], False)
    rw = str(row.get('reward') or '0')
    if rw.isdigit() and int(rw) > 0:
        groups, _ok = _grant_group(rw)
    cl[aid] = 1
    save_state()
    return _resp([json.loads(role_info_array()), [[], list(groups or [])]])
