# -*- coding: utf-8 -*-
r"""妖怪正传 私服 —— 在线奖励（日晷奖励）+ 限时新手任务剩余时间。

## 为什么单开一个模块

这两个系统是 **target client（`client Lua`，真机跑的那个）新增的**，other version 明文里
`onlineReward/` 目录与这些 msgid **都不存在**（实测：1.31 的 ConfigNet 里 `ONLINE_REWARD_*`
一个都没有）。它们跟 `handlers_activity` / `handlers_task` 的既有职责都不重叠，
放在那边只会把两个模块都撑大。

## 修的是什么（本轮 A0 第 2 批）

| 号 | 修前玩家看到什么 |
|---|---|
| 1302 ONLINE_REWARD_INFO   | 在线奖励面板 **6 个奖励格全空**、倒计时恒 0 |
| **1303 ONLINE_REWARD_REFRESH** | ★**点「刷新」直接抛 Lua 错** —— 见下 |
| 1304 ONLINE_REWARD_DRAW   | 点「试试手气」**奖励飘窗显示上一次的掉落**（或什么都不弹） |
| 4070 LIMITED_TIME_NEW_TASK_TIME | 主界面「新手限时」按钮倒计时 **恒显示 0**、永不倒数（同系统 4068/4069 早有 handler，只漏了这一条） |

★1303 是本批最脆的一条：客户端 `network/onlineReward/NetOnlineRewardRefresh.lua:36-38`
取完 `local info = msg[2]` **直接** `info[1]`，**没有 `or {}` 守卫**；而服务端从来没有 1303
⇒ 落 `resp_for` 兜底 `[1,[[]]]` ⇒ `msg[2] = nil` ⇒ `attempt to index a nil value`，
抛出点在 `network/SocketManager.lua:556`，**该行没有 pcall** ⇒ **打断整条 socket tick**
（就是本项目已登记过的"界面卡住"形态）。
对照：`1454 POST_TASK_REFRESH` 同样缺 body，但 `NetPostTaskRefresh.lua:35` 有 `or {}` ⇒ 不崩。
**这两条不是一类，别合并处理。**

## 契约（逐槽取自 1.30 装机客户端，出处逐行标注）

`NetOnlineRewardInfo.lua:34-45`（1302，**摊平**在 msg[1..4]）：
    msg[1] = 已刷新次数（number）        -> `MOnlineRewardInfo:getRefreshCount`
    msg[2] = 抽奖倒计时（**秒**）        -> `getCountdown`；`<=0` 才可抽（`getCanRecieve`）
    msg[3] = 当前档位（**0..5 整数**）    -> `getCurGear`；>5 会在 `VOnlineRewardHome.lua:441`
                                          `m_allGearPercentInfo[curGear].percent` **nil 索引崩**
    msg[4] = 奖励列表 `[[itemId,itemNum,itemType], ...]` -> `getRewardList`（`ipairs`，建议 6 条）

`NetOnlineRewardRefresh.lua:33-49`（1303，**嵌套**）：
    msg[1] = roleInfo（30 字段，`gamer_setRoleInfo` 直接吃）
    msg[2] = `[刷新次数, 倒计时, 档位, 奖励列表]`（**必须存在**，否则崩，见上）

`NetOnlineRewardDraw.lua:33-36`（1304）：
    msg[1] = roleInfo    msg[2] = dropInfo（**`dropInfo[2]` 才是奖励组列表**，
    见 `network/NetDropRewardData.lua:71`）

数值口径（全部来自表，不写死）：
  · 档位时间 `D302_OnlineRewardSet.Times` = `10|30|60|90|120`（**分钟**；客户端
    `SDOnlineRewardSet:getAllGears` 直接把它当"分钟数"显示 `getGearShowText`，抽奖条件按秒比）
  · 免费刷新次数 `FreeRefreshTimes` = `1`；付费刷新价 `RefreshPrice` = `500|1000|1500|2000`
    （按**已刷新次数**取档，越界取最后一段；货币 `RefreshPriceType` = `3` = 铜钱）
  · 抽奖池 `D301_OnlineReward`（32 行，**全部 DrawType=1**；按 `OpenLv` 分 5/9/14/19 四档）
    ⇒ 取"`OpenLv <= 玩家等级` 里最大的那一档"的 8 行，按 **`ExtractRate`** 加权抽 1 行。
    ⚠ `ShowRate` 这一列**全客户端零读取点**（`staticdata/SDOnlineReward.lua:23` 还把表名写错成
      `tT32_Achievement`，是个坏掉的 SD 类）⇒ 它到底是"展示概率"还是别的，**查不到出处**。
      本实现只用 `ExtractRate`；这条不确定已登记在 `docs/AUDIT-msgid-triage-2026-10-11.md`。
    ⚠ **表读不到 / 该档一行都没有 -> 不发奖励**（返回空掉落 + 留痕），绝不编造一个奖励。
  · 发奖走 `handlers_task.grant_one`（本轮刚抽出的**同一份**按类型发放实现）——
    在线奖励与 T02 要发的类型完全同一套，再写一份就是本仓库吃过两次亏的"两套实现一全一半"。

## 状态（`EQ_STATE['online_reward']`，懒创建——state 是合并式载入，未知键不会被清）

    {'date': 'YYYY-MM-DD',            # 每日 24 点重置（客户端文案："每日24点重置所有状态"）
     'refreshed': 0,                  # 已刷新次数（免费额度 = D302.FreeRefreshTimes）
     'draw_t': <unix 秒>,             # 上次抽奖时刻（档位时间是"距上次抽奖"的累计）
     'pool': []}                      # 当前这一档的奖励清单（抽奖时定、刷新时重抽）
"""
import json
import os
import random
import time

from state import EQ_STATE, save_state, table_dir as _table_dir

_DATA = _table_dir()


def _load_json(name):
    """与 handlers_task._load_json 同款：逐行复制，避免污染 load_table 的全局缓存。"""
    from state import load_table as _lt
    return [list(_r) for _r in _lt(name, _DATA)]


def _i(v, d=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return d


# ---- D302 档位表（客户端 ctor 的硬前置：表空则整个面板连 root 都不建） ----
_OR_SET = {}
try:
    _t = _load_json('D302_OnlineRewardSet.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    _r = _t[1]
    _OR_SET = {
        'draw_id': _i(_r[_h['DrawID']], 1),
        'title': _r[_h['Tittle']],
        'times': [_i(x, 0) for x in str(_r[_h['Times']]).split('|') if str(x).strip() != ''],
        'free_refresh': _i(_r[_h['FreeRefreshTimes']], 0),
        'price_type': _i(_r[_h['RefreshPriceType']], 0),
        'price': [_i(x, 0) for x in str(_r[_h['RefreshPrice']]).split('|') if str(x).strip() != ''],
    }
except Exception as _e:
    print('[ONLINE_REWARD] !! D302_OnlineRewardSet 读取失败: %r（面板会打不开，客户端 ctor 直接 return）' % (_e,))

# ---- D301 抽奖池：按 OpenLv 分档，每档若干行（DrawType -> {openlv: [rows]}) ----
_OR_POOL = {}
try:
    _t = _load_json('D301_OnlineReward.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _dt = _i(_r[_h['DrawType']], 0)
            _lv = _i(_r[_h['OpenLv']], 0)
            _OR_POOL.setdefault(_dt, {}).setdefault(_lv, []).append({
                'type': _i(_r[_h['ItemType']], 0),
                'id': _i(_r[_h['ItemID']], 0),
                'num': _i(_r[_h['ItemNum']], 0),
                'rate': _i(_r[_h['ExtractRate']], 0),   # ⚠ 权重用 ExtractRate（ShowRate 全仓无读取点）
            })
        except Exception:
            continue
except Exception as _e:
    print('[ONLINE_REWARD] !! D301_OnlineReward 读取失败: %r（抽奖将不发奖励）' % (_e,))


def _or_player_lv():
    """玩家等级取 `EQ_STATE['player_lv']`（全工程口径，见 `handlers_activity.py:1012` 等 20+ 处）。"""
    return _i(EQ_STATE.get('player_lv'), 1) or 1


def _or_role_info():
    """roleInfo 复用 `handlers_game._role_info()` —— 它返回的是**数组**（内部走唯一权威实现
    `state.role_info_array()`，且已 `json.loads` 解开）。

    ⚠ 不要用 `handlers_sys.role_info_array()`：那个返回的是 **JSON 字符串**，
      直接塞进应答会让客户端 `roleInfo[1]` 索引字符串（`SQGamer:setRoleInfo` 拿到字符串，
      逐字段取值全得 nil，甚至抛错）。本轮就是这么错了第一版。
    """
    from handlers_game import _role_info
    return _role_info()


def _or_state():
    """按天重置的状态；`draw_t` 用**当前时间**初始化（这样新档玩家要等满第一档才会亮抽奖）。"""
    st = EQ_STATE.setdefault('online_reward', {})
    if not isinstance(st, dict):
        st = EQ_STATE['online_reward'] = {}
    today = time.strftime('%Y-%m-%d')
    if st.get('date') != today:
        st.clear()
        st['date'] = today
    now = int(time.time())
    if not _i(st.get('draw_t'), 0):
        st['draw_t'] = now
    for k, v in (('refreshed', 0), ('drawn_ticks', 0)):
        if not isinstance(st.get(k), int):
            st[k] = v
    if not isinstance(st.get('pool'), list):
        st['pool'] = []
    return st


def _or_gear_times():
    return list(_OR_SET.get('times') or [])


def _or_ticks():
    """累计时间跨过了几段档位（0..5）。`Times` 是**累计**边界：10/30/60/90/120 分钟。"""
    times = _or_gear_times()
    st = _or_state()
    elapsed = max(0, int(time.time()) - _i(st.get('draw_t'), 0))
    acc = 0
    n = 0
    for m in times:
        acc += _i(m, 0) * 60
        if elapsed >= acc:
            n += 1
        else:
            break
    return n


def _or_gear_now():
    """已达成档位（恒 0..5；客户端 `m_allGearPercentInfo` 只填了 0..5）。"""
    return min(_or_ticks(), 5)


def _or_elapsed():
    st = _or_state()
    return max(0, int(time.time()) - _i(st.get('draw_t'), 0))


def _or_boundary_secs():
    """跨过**下一段**档位边界所需的秒数（= `Times[已达段数]`）。全满时返回 None。"""
    times = _or_gear_times()
    n = _or_ticks()
    if n >= len(times) or n >= 5:
        return None
    return _i(times[n], 0) * 60


def _or_can_draw():
    """能不能抽 —— 与"跨过档位边界"同义（`_or_ticks() > 已抽过的段数`）。

    ★这里是本模块最容易写错的一处，把**客户端事实**逐条记下来（本轮连错三次）：

    · `VOnlineRewardHome:updateCountdown`（`VOnlineRewardHome.lua:70-81`）：
      `m_countdown` 逐秒递减，**一到 0 就立刻重发 1302** ⇒ 服务端下一次应答的 countdown
      已经是**下一段**的整段值。
    · `MOnlineRewardInfo:getCanRecieve` = `countdown <= 0`；按钮可见性 = `bCanRecieve and curGear < 5`。
      ⇒ 按钮**只在跨过边界的那一帧可见**（下一帧 countdown 已被刷新成下一段）。
    · **所以服务端不能照抄 `countdown <= 0` 当放行条件** —— 那等价于"只在那一帧能抽"，
      玩家永远点不到。正确口径是 **"累计时间跨过了新的档位边界就授权一次抽奖"**：
        · 进面板/刷新后 `draw_t = now`，要等满 `Times[0]`（10 分钟）才有第一次机会；
        · 抽一次之后重新计时，要再等满下一段（30 分钟）才有下一次；
        · 上限 5 次（客户端 `MAX_GEAR=5`）。
      这条与"每日 24 点重置"一起，构成这个玩法的完整节奏。
    · 发给客户端的 `countdown` 仍然按**下一段剩余**给（那是进度条与文案要的量），
      两者一个管"能不能抽"、一个管"显示还差多久"，**不是同一个判据**。
    """
    st = _or_state()
    return _or_ticks() > _i(st.get('drawn_ticks'), 0)


def _or_countdown():
    """喂给客户端的那一槽倒计时（进度条/文案/抽奖按钮都用它）。

    ★★2026-11-06 真机两次（03:0x、23:0x）同一症状：累计到点后客户端自动重发 1302，
    但**抽奖按钮始终不出现**，1303/1304 一次都没发。
    根因在客户端 `VOnlineRewardHome.lua:383-389`：
      按钮可见性 = `getCanRecieve()`（= `getCountdown() <= 0`）`and curGear < 5`
    —— 它**不看**服务端 `_or_can_draw()` 那套授权判据。旧实现即便已授权也回
    "下一段剩余"（>0）⇒ 按钮永远隐藏，玩家永远点不到。
    ⇒ 已授权（`_or_can_draw()`）时这一槽必须回 **0**（按钮出现）；
      抽完/未到点时仍回下一段剩余（进度条与文案要的量）。
      能不能抽仍只由 `_or_can_draw()` 决定（1304 的拒绝口径不变）。
    """
    if _or_can_draw():
        return 0
    b = _or_boundary_secs()
    if b is None:
        return 0
    return max(0, b - _or_elapsed())


def _or_draw_one(draw_type):
    """按 D301 加权抽 **一行** -> {'type','id','num'} 或 None（表外/无该档 -> None，不编造）。"""
    pool = (_OR_POOL.get(_i(draw_type, 0)) or {})
    if not pool:
        return None
    lv = _or_player_lv()
    cand = [k for k in pool.keys() if k <= lv]
    if not cand:
        cand = [min(pool.keys())]                  # 等级低于最低档 -> 用最低档（与"新手也能抽"一致）
    rows = pool[max(cand)]
    weights = [max(0, _i(r.get('rate'), 0)) for r in rows]
    if sum(weights) <= 0:
        return None
    return random.choices(rows, weights=weights, k=1)[0]


def _or_roll_pool(draw_type):
    """抽 6 条（客户端有 6 个奖励格）。同一条可重复——池里本来就有同 id 不同量的行。"""
    out = []
    for _ in range(6):
        r = _or_draw_one(draw_type)
        if r is None:
            break
        out.append([_i(r['id']), _i(r['num']), _i(r['type'])])
    return out


def _or_pool_response(st):
    """`[刷新次数, 倒计时, 档位, 奖励列表]` —— 1302 摊平在 msg[1..4]，1303 装在 msg[2]。"""
    if not st.get('pool'):
        st['pool'] = _or_roll_pool(_OR_SET.get('draw_id', 1))
    return [_i(st.get('refreshed'), 0), _or_countdown(), _or_gear_now(), [list(x) for x in st['pool']]]


def _or_refresh_cost(st):
    """本次刷新要花的铜钱：免费额度内 0，否则按**已刷新次数**取 D302 价格档（越界取最后一段）。"""
    n = _i(st.get('refreshed'), 0)
    if n < _i(_OR_SET.get('free_refresh'), 0):
        return 0
    prices = [p for p in (_OR_SET.get('price') or []) if p > 0]
    if not prices:
        return 0
    idx = n - _i(_OR_SET.get('free_refresh'), 0)
    return prices[min(idx, len(prices) - 1)]


def _or_dropinfo(groups):
    """dropInfo 契约：`[[], [[style, goodslist], ...]]`（见 handlers_game._drop 的注释）。"""
    return [[], list(groups or [])]


# ============================================================================
# 1302 ONLINE_REWARD_INFO —— req {type} -> [1, [刷新次数, 倒计时, 档位, 奖励列表]]
# ============================================================================
def handle_online_reward_info(body_json=None):
    if not _OR_SET:
        # 表读不到 -> 回"空但形状对"的 4 槽（客户端 4 个槽都有 `or 0` / `or {}` 守卫，不会崩）。
        #   ⚠ 不在这里编造奖励；客户端 ctor 本来就会因 D302 空而 return（面板打不开）。
        return json.dumps([1, [0, 0, 0, []]], separators=(',', ':')).encode('utf-8')
    st = _or_state()
    save_state()
    return json.dumps([1, _or_pool_response(st)], separators=(',', ':')).encode('utf-8')


# ============================================================================
# 1303 ONLINE_REWARD_REFRESH —— req {type} -> [1, [roleInfo, [刷新次数, 倒计时, 档位, 奖励列表]]]
#   ★msg[2] 必须存在：客户端 `NetOnlineRewardRefresh.lua:36-38` 直接 `info[1]`，没有 or {}
# ============================================================================
def handle_online_reward_refresh(body_json=None):
    if not _OR_SET:
        return b'[0,{}]'                            # 码表只要 statecode（客户端只有 m_errorCode[0]）
    st = _or_state()
    cost = _or_refresh_cost(st)
    if cost > 0:
        gold = _i(EQ_STATE.get('gold'), 0)
        if gold < cost:
            return b'[61]'                          # 铜钱不足（与 handlers_specialty 的 61 同码）
        EQ_STATE['gold'] = gold - cost
    st['refreshed'] = _i(st.get('refreshed'), 0) + 1
    st['draw_t'] = int(time.time())                 # 刷新 = 重新等这一档（也是"立刻可抽"的唯一途径）
    st['pool'] = _or_roll_pool(_OR_SET.get('draw_id', 1))
    save_state()
    return json.dumps([1, [_or_role_info(), _or_pool_response(st)]],
                      separators=(',', ':')).encode('utf-8')


# ============================================================================
# 1304 ONLINE_REWARD_DRAW —— req {type} -> [1, [roleInfo, dropInfo]]
#   只能抽**一条**（客户端回调走 `NetDropRewardData:getDropRewardList()[1]`）。
#   抽成功后 `draw_t` 重置、`gear` 归 0 —— 下一档重新累计（这是"每日 5 档"的唯一节奏）。
# ============================================================================
def handle_online_reward_draw(body_json=None):
    if not _OR_SET:
        return b'[0,{}]'
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    st = _or_state()
    # 放行判据 = 累计时间**跨过了新的档位边界**（`_or_can_draw`，理由见它的 docstring）。
    if not _or_can_draw():
        return b'[0,{}]'
    row = _or_draw_one(_i(req.get('type'), _OR_SET.get('draw_id', 1)))
    if row is None:
        print('[ONLINE_REWARD] !! 抽奖池无可用行（D301 DrawType=%s lv=%s）-> 不发奖励'
              % (req.get('type'), _or_player_lv()), flush=True)
        return json.dumps([1, [_or_role_info(), _or_dropinfo([])]],
                          separators=(',', ':')).encode('utf-8')
    from handlers_task import grant_one
    groups = grant_one(_i(row['type'], 0), _i(row['id'], 0), _i(row['num'], 0))
    st['drawn_ticks'] = _or_ticks()                 # 记下"抽到第几段"：下一次要跨过再下一段
    st['pool'] = _or_roll_pool(_OR_SET.get('draw_id', 1))
    save_state()
    return json.dumps([1, [_or_role_info(), _or_dropinfo(groups)]],
                      separators=(',', ':')).encode('utf-8')


# ============================================================================
# 4070 LIMITED_TIME_NEW_TASK_TIME —— req {} -> [1, [剩余秒数]]
#   主界面 `VMainButton:getLimitTaskTime()`（`VMainButton.lua:151-156`）**每次进营地无条件发**，
#   回调把值写进 `m_limitTimes`；不补 -> `Text_XinShouXianShi_Time` 恒显示 0、永不倒数。
#   同系统的 4068/4069 早有 handler（`handlers_activity.handle_new_limited_time_task`），
#   剩余时间就取它们的同一份状态，**不新开记账键**。
# ============================================================================
def handle_limited_time_new_task_time(body_json=None):
    left = 0
    try:
        # ★与 4068 的 `msg[4]` **同一份计算**（本轮抽成 `handlers_activity._nlt_remain`）——
        #   不新开记账键：两处算法一旦分叉，按钮倒计时会与面板里的对不上。
        from handlers_activity import _nlt_remain
        left = _nlt_remain()
    except Exception as _e:
        print('[ONLINE_REWARD] 4070 取剩余时间失败(回 0): %r' % (_e,), flush=True)
    return json.dumps([1, [left]], separators=(',', ':')).encode('utf-8')
