# -*- coding: utf-8 -*-
"""handlers_posttask.py —— 悬赏任务 1450~1454（新包 A0 第 9 批）。

**修前的缺口**：5 个号服务端一个都没有 ⇒ 落兜底 `[1,[[]]]`。后果按号分：
  · 1450 -> `msg[1]` 是空表（Lua 真值，`ipairs` 得 0 条）⇒ 界面**一个悬赏都没有**、
    `msg[2]`(nil) ⇒ 刷新倒计时显示 0、`msg[4]`(nil) ⇒ "今日次数"恒 0。
  · 1453/1452 -> `msg[1]`/`msg[2]` 都 nil ⇒ `gamer_setRoleInfo(nil)`、领奖没有任何掉落。
  · 1454 -> `msg[2]` 是 nil ⇒ `serverTaskInfo = msg[2] or {}` ⇒ 刷新后**列表还是空的**。

**数据源（全部有出处）**
  · `T43_TravelTasks`(35)：`TaskQuality`/`TaskTime`(小时)/`TaskBg`/`TaskNumber`(需派几只)/
    `DemandQuality`/`DemandQualityNum`/`DemandType`(妖怪类别)/`Reward`(**T02 组，35 个全在**，
    实测过)/`Rate`(抽取权重，1000/300)/`OpenLevel`(12)/`FinishType`/`FinishPrice`(立即完成价)。
  · 槽位数与等级档：`T23_Function` **ID=261** `Value=13|25|30`（开启等级）、**ID=262**
    `Value=3|4|5`（对应开启个数）—— 客户端 `VPostTaskHome:refreshTaskRefreshCount` 就是这两行；
    ⚠ 它那段的 `idx = math.max(1, idx - 1)` 让实际档位是 **<25 开 3 个 / 25~29 开 4 个 / ≥30 开 5 个**，
    服务端必须**算出同一个数**（否则"今日次数 x/y"的 y 与列表长度对不上）。
  · 刷新货币类型：`T23_Function` **ID=264** `Value=3`（= `bag.baseType.coins` **铜钱**）；
    快速派遣等级：**ID=266** `Value=16`。
  · ⚠ **`flushCost`（刷新消耗的数量）没有表源** —— 264 只给"消耗哪种货币"，
    `T23[265]=500|1000|5000|10000` 是**4 档**、而 261/262 是**3 档**，对不上，不能当刷新价。
    ⇒ 现在取 **0（免费刷新）**，并把这条记进 HANDOFF 待拍板（**不编一个数字**）。
  · ⚠ `TaskTime` 的单位：客户端只做 `*0.001`（毫秒→秒）⇒ **服务端发毫秒**，`-1` = 未开始。

**硬约束（逐条核对客户端）**
  1. ★1450 与 1454 的**形状不同**：1450 = `[1,[taskList, refreshTime, flushCost, hadSendCount]]`
     （四个槽摊平）；1454 = `[1,[roleInfo, [taskList, refreshTime, flushCost, hadSendCount]]]`
     （**多一层嵌套** + 首槽是 roleInfo）。照抄任一个去写另一个，客户端会读到 nil。
  2. ★1454 的 `msg[1]` 是 **roleInfo 数组**（`gamer_setRoleInfo(msg[1])` 逐字段读）。
  3. ★任务行 4 槽 `[id, stdId, finishTime(ms, -1 未开始), monsters]`，`monsters` 是**妖怪 ucode 列表**。
  4. ★1451/1452/1453 的 `{id}` 指的是**槽位实例 id**（我们自己分配的），不是 `T43.ID` ——
     客户端把它从 1450 的 `taskInfo[1]` 原样回传。
  5. ★被派出去的妖怪**不能再派**（客户端 `PostTaskConfig:getMonsterList` 用
     `table.indexof(hadSendMonsters, ucode)` 把它们从可选列表里剔掉）⇒ 服务端也必须拒。
  6. ⚠ 1452"立即完成"的价位在表里（`FinishPrice`），货币是 `T23[264]`=铜钱；1453 才是发奖。
"""
import json
import time

from state import EQ_STATE, save_state, table_dir as _table_dir

_DATA = _table_dir()
_COIN = 'gold'          # T23[ID=264] = 3 = bag.baseType.coins(铜钱)
_DEFAULT_FLUSH = 0      # 无表源 ⇒ 免费刷新（见模块头；待拍板）


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


# ---------------------------------------------------------------- 静态表
_T23 = {}


def _t23(row_id, fallback=''):
    """`T23_Function` 按 **`ID`** 列取 `Value`（261/262/264/266 都是 ID 列，见模块头）。"""
    if not _T23:
        try:
            t = _load_json('T23_Function.json')
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                _T23[str(r[h['ID']])] = str(r[h['Value']])
        except Exception:
            pass
    return _T23.get(str(row_id), fallback)


def _tiers():
    """`(等级档, 槽位数列表)` —— 两边长度不等就不做分档（客户端也这么判）。"""
    lv = [x for x in str(_t23(261, '')).split('|') if x]
    n = [x for x in str(_t23(262, '')).split('|') if x]
    return (lv, n) if len(lv) == len(n) and n else ([], [])


def _slots_max(level):
    """**逐字照抄**客户端那段档位选择（`VPostTaskHome:refreshTaskRefreshCount`）。

    ⚠ 那段有个 `idx = math.max(1, idx - 1)`，效果是"用**上一档**的个数"，所以实测档位是
      `lv < 25 -> 3` / `25 <= lv < 30 -> 4` / `lv >= 30 -> 5`（不是 13/25/30 三档各对应 3/4/5）。
      服务端算出同一个数才能让"今日次数 x/y"的 y 与列表条数一致。
    """
    lv, n = _tiers()
    if not lv:
        return 0
    idx = None
    for k, lim in enumerate(lv, start=1):
        if _i(level, 1) < _i(lim):
            idx = k
            break
    if idx is None:
        idx = len(n) + 1
    idx = max(1, idx - 1)
    return _i(n[idx - 1] if idx - 1 < len(n) else n[-1])


_TASKS = {}          # stdId -> row dict
_TASK_ORDER = []     # 按文件顺序


def _t43():
    if not _TASKS:
        try:
            t = _load_json('T43_TravelTasks.json')
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                d = {c: r[h[c]] for c in t[0]}
                sid = _i(d.get('ID'))
                if sid <= 0:
                    continue
                _TASKS[sid] = d
                _TASK_ORDER.append(sid)
        except Exception:
            pass
    return _TASKS


def _t43_of(sid):
    _t43()
    return _TASKS.get(_i(sid))


# ---------------------------------------------------------------- 账号档
def _today():
    return time.strftime('%Y%m%d')


def _next_midnight():
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))) + 86400


def _st():
    v = EQ_STATE.setdefault('post_task', {})
    if not isinstance(v, dict):
        v = EQ_STATE['post_task'] = {}
    if not isinstance(v.get('slots'), list):
        v['slots'] = []
    v['sent'] = _i(v.get('sent'), 0)
    return v


def _roll(n, level, exclude=()):
    """按 `Rate` 权重抽 n 个**互不相同**的 `T43` 行（照 `handlers_bag._apply_gift_rewards`
    那套加权抽取的写法：权重和 <=0 时等权）。`exclude` 里的 stdId 不参与。
    """
    import random
    _t43()          # ★必须先确保表已加载：`_roll` 直接读模块级 `_TASKS/_TASK_ORDER`，
                    #   少了这一行，**第一次**调用（`handle_post_task_info` -> `_fill` -> `_roll`）
                    #   会看到空池子 -> 一条悬赏都不发（本轮实测踩到：直接调 `_roll` 是好的，
                    #   因为调试脚本先调过 `_t43()`）。惰性加载的缓存**取用点**都要自己保证已加载。
    pool = []
    for sid in _TASK_ORDER:
        row = _TASKS[sid]
        if str(sid) in {str(x) for x in exclude}:
            continue
        if _i(row.get('OpenLevel'), 1) > _i(level, 1):
            continue
        w = max(0, _i(row.get('Rate'), 0))
        pool.append((sid, w))
    if not pool:
        return []
    picks = []
    for _ in range(max(0, n)):
        left = [(s, w) for (s, w) in pool if s not in picks]
        if not left:
            break
        wsum = sum(w for _s, w in left)
        if wsum <= 0:
            picks.append(random.choice(left)[0])
        else:
            x = random.randrange(wsum)
            pick = left[0][0]
            for s, w in left:
                if x < w:
                    pick = s
                    break
                x -= w
            picks.append(pick)
    return picks


def _next_sid():
    """槽位实例 id：单调递增、与 `T43.ID` 无关系（客户端只回传）。"""
    st = _st()
    n = max([_i(s.get('id')) for s in st['slots']] + [_i(st.get('seq'), 0)]) + 1
    st['seq'] = n
    return n


def _fill(level):
    """把空槽补满到当前档位；返回 slots。"""
    st = _st()
    want = _slots_max(level)
    if want <= 0:
        return st['slots']
    have = len(st['slots'])
    if have >= want:
        return st['slots']
    for sid in _roll(want - have, level):
        st['slots'].append({'id': _next_sid(), 'std': sid, 'finish': -1, 'monsters': []})
    return st['slots']


def _reset_if_new_day():
    st = _st()
    if st.get('day') == _today():
        return False
    st['day'] = _today()
    st['slots'] = []
    st['sent'] = 0
    st['seq'] = 0
    return True


def _flush_cost():
    return _DEFAULT_FLUSH


def _rows():
    """1450 的任务行：`[id, stdId, finishMs, monsters]`（`finish` = **毫秒**，-1 未开始）。"""
    out = []
    for s in _st()['slots']:
        f = _i(s.get('finish'), -1)
        out.append([_i(s.get('id')), _i(s.get('std')), (f * 1000) if f > 0 else -1,
                    list(s.get('monsters') or [])])
    return out


def _info(sid):
    for s in _st()['slots']:
        if _i(s.get('id')) == _i(sid):
            return s
    return None


def _claimed_ucodes(except_id=None):
    """已经被别的槽派出去的妖怪 ucode 集合（客户端自己也是这么排除的）。"""
    out = set()
    for s in _st()['slots']:
        if except_id is not None and _i(s.get('id')) == _i(except_id):
            continue
        for u in (s.get('monsters') or []):
            out.add(str(u))
    return out


def _role_info():
    """**唯一权威实现** `state.role_info_array()`（照 `handlers_game._role_info` 的规范写法）。

    ⚠ 本文件原来写成 `from handlers_game import _role_info`（"转一手"）—— 运行时等价，但被
      `_roleinfo_convergence_guard_test` 的 **R1 判红**，而 **R1 是对的**：R1 要求每个
      `def _role_info*():` 的函数体里**逐字出现 `role_info_array()`**，正是为了防"转一手之后
      下一轮顺手在本地改两格"——`state.role_info_array` 的 docstring 记着 v32 分叉事故（两份副本
      各错一半、症状不同所以一直没被当成同一个 bug）。**别为了少一行 import 绕过这条**。
    """
    from state import role_info_array
    return json.loads(role_info_array())


def _drop(groups):
    from handlers_game import _drop as _gd
    return _gd(groups)


# ============================================================================
# 1450 领取列表页信息
# ============================================================================
def handle_post_task_info(body_json=None):
    """1450 -> `[1,[taskList, refreshTime, flushCost, hadSendCount]]`（**四槽摊平**）。

    ⚠ 与 1454 形状不同（那个是 `[roleInfo, [四槽]]`）—— 见模块头硬约束 1。
    """
    lv = _i(EQ_STATE.get('player_lv'), 1)
    _reset_if_new_day()
    _fill(lv)
    st = _st()
    save_state()
    return _out(_rows(), _next_midnight(), _flush_cost(), _i(st.get('sent'), 0))


# ============================================================================
# 1451 派遣
# ============================================================================
def handle_post_task_send(body_json):
    """1451 req `{monsterIds, id}` -> **纯 ack** `[1,[]]`（客户端只回调）。

    ⚠ 但**副作用必须真的落**：客户端 `MPostTaskInfo` 收到事件后会重拉 1450，
      派没派出去完全看服务端这一格。
    """
    req = _req(body_json)
    sid = _i(req.get('id'))
    mons = req.get('monsterIds') or []
    if not isinstance(mons, list):
        return b'[0]'
    lv = _i(EQ_STATE.get('player_lv'), 1)
    _reset_if_new_day()
    _fill(lv)
    slot = _info(sid)
    if slot is None:
        return b'[0]'
    if _i(slot.get('finish'), -1) != -1:
        return b'[0]'                       # 已经在跑/已完成，不能重派
    row = _t43_of(slot.get('std'))
    if row is None:
        return b'[0]'
    need = _i(row.get('TaskNumber'), 1)
    mons = [str(u) for u in mons if str(u)]
    # ⚠ 客户端是 `monsterCount >= needCount`（`VPostTaskSend:getCanSend`），**不是 ==** ——
    #   照 == 写会让 TaskNumber=4 的悬赏永远派不出去（本轮实测踩到：1001 的 TaskNumber 是 2，
    #   但 1027 那行是 4，"所有行都一样"这个假设是错的）。
    if not mons or len(mons) < need:
        return b'[0]'
    if len(set(mons)) != len(mons):
        return b'[0]'
    bag = EQ_STATE.get('monsters') or {}
    for u in mons:
        if u not in bag:
            return b'[0]'                   # 不是我的妖怪
    busy = _claimed_ucodes(except_id=sid)
    for u in mons:
        if u in busy:
            return b'[0]'                   # 已被别的悬赏派出去
    # ⚠ **种族（`DemandType`）与品级（`DemandQuality`/`DemandQualityNum`）服务端不校验**：
    #   客户端 `getCanSend` 会拦（不够时弹 LOCAL_UI_TRAVELTASK_FAMILYTIPS/QUALITYTIPS），
    #   而这两项要读妖怪静态表的 `Type`/`Quality`，**表根里没有那张表**（按 monster/card/role
    #   扫过表头，没有同时带 Quality 与 Type 的妖怪静态表）⇒ 宁可不拦，也不拿别的表凑一个质量数
    #   （凑错会把合法的派遣挡掉，比"少拦一条"更糟）。派遣名单**不影响奖励**（奖励只看 T43.Reward），
    #   所以这里不是经济口子。已记进 HANDOFF。
    hours = max(0, _i(row.get('TaskTime'), 0))
    slot['monsters'] = mons
    slot['finish'] = int(time.time()) + hours * 3600
    st = _st()
    st['sent'] = _i(st.get('sent'), 0) + 1
    save_state()
    return b'[1,[]]'


# ============================================================================
# 1452 立即完成
# ============================================================================
def handle_post_task_finish(body_json):
    """1452 req `{id}` -> `[roleInfo, dropInfo]`。

    价位在表里（`FinishPrice`），货币取 `T23[ID=264]`（=3 铜钱）。
    ⚠ **这一条只把任务推到"可领取"，奖励在 1453**；客户端两个 net 都读 roleInfo+dropInfo，
      是为了让"扣钱"立刻反映在余额上。
    """
    req = _req(body_json)
    sid = _i(req.get('id'))
    lv = _i(EQ_STATE.get('player_lv'), 1)
    _reset_if_new_day()
    _fill(lv)
    slot = _info(sid)
    if slot is None:
        return b'[0]'
    if _i(slot.get('finish'), -1) == -1:
        return b'[0]'                       # 还没派遣
    if _i(slot.get('finish'), -1) <= int(time.time()):
        # 已经到点了 —— 直接可领，不收钱（客户端此时应走 1453）
        save_state()
        return _out(_role_info(), _drop([]))
    row = _t43_of(slot.get('std'))
    price = _i((row or {}).get('FinishPrice'), 0)
    if price > 0 and _i(EQ_STATE.get(_COIN), 0) < price:
        return b'[60]'
    if price > 0:
        EQ_STATE[_COIN] = _i(EQ_STATE.get(_COIN), 0) - price
    slot['finish'] = int(time.time())
    save_state()
    return _out(_role_info(), _drop([]))


# ============================================================================
# 1453 领取奖励
# ============================================================================
def handle_post_task_recieve(body_json):
    """1453 req `{id}` -> `[roleInfo, dropInfo]`；发 `T43.Reward` 组并**换一条新悬赏**。"""
    from handlers_task import _grant_group
    req = _req(body_json)
    sid = _i(req.get('id'))
    lv = _i(EQ_STATE.get('player_lv'), 1)
    _reset_if_new_day()
    _fill(lv)
    slot = _info(sid)
    if slot is None:
        return b'[0]'
    fin = _i(slot.get('finish'), -1)
    if fin <= 0 or fin > int(time.time()):
        return b'[0]'                       # 没派 / 还没到点
    row = _t43_of(slot.get('std'))
    if row is None:
        return b'[0]'
    groups, ok = _grant_group(_i(row.get('Reward')))
    if not ok:
        return b'[0]'
    st = _st()
    st['slots'] = [s for s in st['slots'] if _i(s.get('id')) != sid]
    _fill(lv)                               # 换一条新的顶上（保持档位条数）
    save_state()
    return _out(_role_info(), _drop(groups))


# ============================================================================
# 1454 刷新
# ============================================================================
def handle_post_task_refresh(body_json=None):
    """1454 -> `[roleInfo, [taskList, refreshTime, flushCost, hadSendCount]]`。

    ⚠ **形状与 1450 不同**：这里首槽是 roleInfo、任务信息整体**再嵌一层**（客户端
      `local serverTaskInfo = msg[2] or {}` 之后直接取 `serverTaskInfo[1]`）。
    """
    lv = _i(EQ_STATE.get('player_lv'), 1)
    _reset_if_new_day()
    st = _st()
    slot_n = _slots_max(lv)
    cost = _flush_cost()
    if cost > 0 and _i(EQ_STATE.get(_COIN), 0) < cost:
        return b'[60]'
    if cost > 0:
        EQ_STATE[_COIN] = _i(EQ_STATE.get(_COIN), 0) - cost
    # 重掷：**不保留**已派出去的那些槽（刷新就是换一批），但正在跑的也不该被吞掉
    running = [s for s in st['slots'] if _i(s.get('finish'), -1) != -1]
    keep = running if running else []
    st['slots'] = keep
    if len(st['slots']) > slot_n:
        st['slots'] = st['slots'][:slot_n]
    # 被保留下来的妖怪仍算"已派出"，新槽不能再派它们 -> `_fill` 之后由 `_claimed_ucodes` 兜住
    _fill(lv)
    save_state()
    return _out(_role_info(), [_rows(), _next_midnight(), cost, _i(st.get('sent'), 0)])
