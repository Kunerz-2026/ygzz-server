# -*- coding: utf-8 -*-
"""handlers_skinlottery.py —— 皮肤抽奖 4500 / 4501 / 4502 / 4504 / 4505 / 4506 / 4507（新包 A0 第 5 批）。

**修前的缺口**：这 7 个号服务端**一个都没有**（4503 不存在，客户端也没定义）。落兜底
`[1,[[]]]` 的后果按号各不相同，但都不是"没有界面"——界面在本地表上照样画得出来，
所以症状全是**静默的假数据**：
  · 4500（兑换商城面板）`msg[1]`=`[]`（表，真值）→ `tonumber([])`=nil→0 → **兑换币恒 0**；
    `msg[2]`=nil → 商品列表**恒空** ⇒ 面板一片空白。
  · 4502（抽奖）`dropIdData`=`msg[3]`=nil → 客户端 `prizeData` 为空 ⇒ **转了但不出奖**，
    而它本地照样扣掉 10 张券（`deleteItem(ucode, self.lotteryNum)`）。
  · 4505（面板信息）`msg[2]`/`msg[3]` 都空 ⇒ 奖池页签与累计宝箱全空。
  · 4507 `msg[1]`=nil ⇒ "已拥有"标记不显示。

**数据源（4 张表，本轮全部由 `work\\_table_migrate.py add` 从目标客户端迁入表根）**
  · `D201_DoubleDrawItem`(336) —— 奖池格子。`ActivityID` = 奖池期，`DrawType` **1=树神盘(5 格) /
    2=九宫格(9 格)**；每格 `ItemType/ItemID/ItemNum` + `Rate1Th`(一抽权重) / `Rate10Th`(十抽权重)。
    ⚠ `D201.ID` **跨期全局唯一**（默认期 1~5 是树神盘、6~14 是九宫格），客户端就是拿这个 ID
      当"抽中了哪一格"（`setIconInfoList` 用它回查整行）。
  · `D202_DoubleDrawSet`(2) —— 消耗：`DrawType=2` → `DrawCostID=1101`(皮肤抽奖券)、
    `Cost1Th=1` / `Cost10Th=10`。**客户端也硬编码按 1101 扣**（`prop_getList(1101)`），
    两边同一口径 ⇒ 服务端只能按这张表扣，扣别的物品会出现"券没少、界面却少了"。
  · `D204_DoubleDrawReward`(6) —— 累计抽奖宝箱：`DrawNum` 5/10/30/60/150/200 → `RewardID`(T02 组 5101~5106)。
  · `D203_SkinShop`(19) —— 兑换商城：`GoodsType/GoodsId/GoodsCount`、`LimitCount`、
    `PriceType=10`(皮肤商城兑换道具) / `PriceID=1047`(彩衣之羽) / `PriceNum`。

**硬约束（照抄会崩客户端）**
  1. ★ `getSysOpenV(11025)` 在目标客户端里是 **1**（`A04_JumpTag` 的 `TagId=11025` 行、
     `FunctionCHS='1'`）⇒ 4502 必须回**四槽** `[isGetTreeGod, lastNum, 奖池ID列表, [roleInfo, dropInfo]]`。
     回三槽会让 `rewardData` 变成 `msg[3]`（本应是 id 列表）⇒ `roleInfo` 取到数字、
     `SQManager:gamer_setRoleInfo(number)` 崩（`AUDIT-msgid-triage` §2.2(b) 记的就是这条）。
  2. ★ `msg[3]` 的元素必须是 `D201.ID`，且**个数 == lotteryNum**：客户端
     `for i,v in ipairs(self.prizeData) do self:setIconInfoList(v) end` 会用每个 id
     `usGetTableData(D201, id)` 取整行；表外 id 会让 `SDDoubleDrawItem:loadStaticData`
     打出 "data is nil" 后**留下一个半初始化的对象**，再取 `getStatic_ItemType()` 得 nil
     ⇒ `IconData:create(nil, nil)` 报错。
  3. ★ 树神那一格的判据是**客户端自己定的**：`VSkinSweepstakeHome.lua:421`
     `if tonumber(activityItemData[i].ItemID) == 1102 then self.treeID = activityItemData[i].ID`。
     ⇒ 服务端"树神格" = 九宫格里 `ItemID == 1102`（`L02_Loots` 的"树神的祝福"）的那一格。
     抽中它时**报的是树神奖励那一行的 ID**（客户端 `lotteryRespons` 会把树神盘 ID 换成
     `treeID` 去点亮九宫格），同时 `isGetTreeGod=1`。
  4. 4507 的行**顺序必须与树神盘格子顺序（BoardID 升序）一致**：
     `setLotteryItemTreeIsHave` 是 `ipairs(_data)` 与 `activityItemTreeData[i]` **按位置**比，
     不是按 id 查表。
  5. `roleInfo` 必须是**数组**（`gamer_setRoleInfo` 逐字段读）—— 用 `handlers_game._role_info()`。
"""
import json
import random
import time

from state import EQ_STATE, save_state, table_dir as _table_dir

_DATA = _table_dir()
_ACTIVITY_DEFAULT = 0          # 默认奖池期（客户端 `nowId == 0` 时不显示倒计时）
_TREE_ITEM_ID = 1102           # 树神的祝福 —— 九宫格里 ItemID 是它的那一格 = 树神格（客户端判据）
_LOTTERY_ITEM = 1101           # 皮肤抽奖券（D202.DrawCostID，客户端也硬编码这个）
_SHOP_COIN = 1047              # 彩衣之羽（D203.PriceID）
_SKIN_TYPE = 14                # 奖励类型 14 = 皮肤（`grant_one` / `_grant_skin_with_dup` 同一套）


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
def _cols(name):
    t = _load_json(name)
    return {c: k for k, c in enumerate(t[0])}, t[1:]


# D201: (activityID, drawType) -> [row dict]，按 BoardID 升序
_D201 = {}
_D201_BY_ID = {}
try:
    _h, _rows = _cols('D201_DoubleDrawItem.json')
    for _r in _rows:
        try:
            _d = {'id': _i(_r[_h['ID']]), 'act': _i(_r[_h['ActivityID']]),
                  'draw': _i(_r[_h['DrawType']]), 'board': _i(_r[_h['BoardID']]),
                  'itype': _i(_r[_h['ItemType']]), 'iid': _i(_r[_h['ItemID']]),
                  'inum': _i(_r[_h['ItemNum']], 1),
                  'rate1': _i(_r[_h['Rate1Th']], 0), 'rate10': _i(_r[_h['Rate10Th']], 0),
                  'maxrate': _i(_r[_h['MaxRate']], 0), 'minrate': _i(_r[_h['MinRate']], 0)}
            if not _d['id']:
                continue
            _D201.setdefault((_d['act'], _d['draw']), []).append(_d)
            _D201_BY_ID[_d['id']] = _d
        except Exception:
            continue
    for _k in _D201:
        _D201[_k].sort(key=lambda x: x['board'])
except Exception:
    _D201, _D201_BY_ID = {}, {}

# D202: drawType -> row dict
_D202 = {}
try:
    _h, _rows = _cols('D202_DoubleDrawSet.json')
    for _r in _rows:
        try:
            _D202[_i(_r[_h['DrawType']])] = {
                'cost_type': _i(_r[_h['DrawCostType']], -1), 'cost_id': _i(_r[_h['DrawCostID']]),
                'cost1': _i(_r[_h['Cost1Th']], -1), 'cost10': _i(_r[_h['Cost10Th']], -1)}
        except Exception:
            continue
except Exception:
    _D202 = {}

# D204: boxId -> row dict
_D204 = {}
try:
    _h, _rows = _cols('D204_DoubleDrawReward.json')
    for _r in _rows:
        try:
            _D204[_i(_r[_h['ID']])] = {'draw_num': _i(_r[_h['DrawNum']]),
                                       'reward': _i(_r[_h['RewardID']])}
        except Exception:
            continue
except Exception:
    _D204 = {}

# D203: 商城 19 行（按 ID）
_D203 = []
try:
    _h, _rows = _cols('D203_SkinShop.json')
    for _r in _rows:
        try:
            _d = {'id': _i(_r[_h['ID']]), 'gtype': _i(_r[_h['GoodsType']]),
                  'gid': _i(_r[_h['GoodsId']]), 'gnum': _i(_r[_h['GoodsCount']], 1),
                  'limit': _i(_r[_h['LimitCount']], 0), 'ptype': _i(_r[_h['PriceType']], -1),
                  'pid': _i(_r[_h['PriceID']]), 'pnum': _i(_r[_h['PriceNum']])}
            if _d['id']:
                _D203.append(_d)
        except Exception:
            continue
except Exception:
    _D203 = []
_D203_BY_ID = {d['id']: d for d in _D203}


def _tree_slot(act):
    """该奖池里"树神格"那一个九宫格行（`ItemID == 1102`）；没有 -> `None`。"""
    for s in _D201.get((act, 2), []):
        if s['iid'] == _TREE_ITEM_ID and s['itype'] == 2:
            return s
    return None


# ---------------------------------------------------------------- 账号档
def _st():
    v = EQ_STATE.setdefault('skin_lottery', {})
    if not isinstance(v, dict):
        v = EQ_STATE['skin_lottery'] = {}
    for k, d in (('pool', {}), ('boxes', {}), ('shop', {})):
        if not isinstance(v.get(k), dict):
            v[k] = d
    v.setdefault('total', 0)
    return v


def _pool_st(act):
    v = _st()['pool'].setdefault(str(act), {})
    v.setdefault('pity', 0)          # 自上次中树神起已抽次数（保底线用）
    v.setdefault('last', 0)
    return v


def _prop_count(pid):
    """道具袋里该静态 id 的数量（**ucode 就是静态 id 的字符串**，与 `grant_one` ty==2 同口径）。"""
    return _i((EQ_STATE.get('prop') or {}).get(str(pid)), 0)


def _prop_take(pid, n):
    """扣道具；够 -> 返回 `True` 并真扣，不够 -> `False` 且**一分不动**。"""
    have = _prop_count(pid)
    if n <= 0:
        return True
    if have < n:
        return False
    left = have - n
    if left > 0:
        EQ_STATE['prop'][str(pid)] = left
    else:
        EQ_STATE['prop'].pop(str(pid), None)
    return True


def _skin_owned(sid):
    return str(sid) in (EQ_STATE.get('skin_owned') or {})


def _box_rows():
    """`[[boxId, status]]`（**全部 6 档都发**：客户端按 `status` 决定"未完成/可领/已领"三种显示）。"""
    st = _st()
    total = _i(st.get('total'), 0)
    out = []
    for bid in sorted(_D204.keys()):
        if str(bid) in st['boxes']:
            status = 2                                  # 已领
        elif total >= _D204[bid]['draw_num']:
            status = 1                                  # 可领
        else:
            status = 0                                  # 未完成
        out.append([bid, status])
    return total, out


def _grant(gtype, gid, gnum):
    """按 (类型, 静态id, 数量) 发放 —— **走 `handlers_task.grant_one`**（全工程唯一一份实现）。"""
    from handlers_task import grant_one
    return grant_one(gtype, gid, gnum) or []


# ============================================================================
# 4500 / 4501 兑换商城
# ============================================================================
def handle_skin_shop_info(body_json=None):
    """4500 -> `[1,[兑换币数量, [[商品ID, 是否已拥有, 剩余次数], ...]]]`。

    `status`：0 未拥有 / 1 已拥有（客户端 `NetSkinLotteryShopInfo` 的注释如此；
    `GoodsType=14` 时就是"这个皮肤我有没有"）。
    `limitCount`：剩余可买次数 = `D203.LimitCount - 已购`（`LimitType` 全表都是 1=终身限购）。
    """
    st = _st()
    rows = []
    for d in _D203:
        owned = 1 if (d['gtype'] == _SKIN_TYPE and _skin_owned(d['gid'])) else 0
        bought = _i(st['shop'].get(str(d['id'])), 0)
        rows.append([d['id'], owned, max(0, d['limit'] - bought)])
    return _out(_prop_count(_SHOP_COIN), rows)


def handle_skin_shop_buy(body_json):
    """4501 req{id} -> `[roleInfo, dropInfo]`。码：`0` 参数/物品不存在；`586` 不可领取(次数用尽/钱不够)。

    价格按 `D203.PriceType=10`（皮肤商城兑换道具）→ `PriceID` 道具 × `PriceNum`。
    """
    from handlers_game import _role_info as _gi, _drop as _gd
    req = _req(body_json)
    sid = _i(req.get('id'))
    d = _D203_BY_ID.get(sid)
    if d is None:
        return b'[0]'
    st = _st()
    if _i(st['shop'].get(str(sid)), 0) >= d['limit'] > 0:
        return b'[586]'
    # ⚠ 只认 PriceType=10（本表 19 行**全是** 10）。别的币种本工程没有对应钱包 -> 拒绝，不编。
    if d['ptype'] != 10:
        return b'[586]'
    if not _prop_take(d['pid'], d['pnum']):
        return b'[586]'
    groups = _grant(d['gtype'], d['gid'], d['gnum'])
    if not groups:
        # ★ 发不出去 -> **退还**（"扣了钱没给货"是本工程反复吃亏的形态，不再制造一次）
        EQ_STATE.setdefault('prop', {})[str(d['pid'])] = _prop_count(d['pid']) + d['pnum']
        return b'[586]'
    st['shop'][str(sid)] = _i(st['shop'].get(str(sid)), 0) + 1
    save_state()
    return _out(_gi(), _gd(groups))


# ============================================================================
# 4502 抽奖
# ============================================================================
def _pick(board, rate_key):
    """按权重抽一格。权重全 <=0 时退化为"均匀随机"（**不许**返回 None 让上层崩）。"""
    ws = [max(0, s[rate_key]) for s in board]
    if sum(ws) <= 0:
        ws = [max(0, s['rate1']) for s in board]
    if sum(ws) <= 0:
        return random.choice(board)
    return random.choices(board, weights=ws, k=1)[0]


def handle_skin_lottery_start(body_json):
    """4502 req{skinPoolId, lotteryNum(1/10), lotteryMethod} -> **四槽**（见模块头硬约束 ①）。

    `[isGetTreeGod(0/1), 剩余几次抽到树神, [D201.ID × lotteryNum], [roleInfo, dropInfo]]`
    """
    from handlers_game import _role_info as _gi, _drop as _gd
    req = _req(body_json)
    act = _i(req.get('skinPoolId'), _ACTIVITY_DEFAULT)
    num = _i(req.get('lotteryNum'), 0)
    board = _D201.get((act, 2), [])
    if not board or num not in (1, 10):
        return b'[0]'                                  # 奖池不存在 / 抽数非法（客户端只发 1 或 10）
    cfg = _D202.get(2) or {}
    cost = cfg.get('cost1' if num == 1 else 'cost10')
    if cost is None or cost < 0:
        return b'[0]'                                  # 表里没给这档的消耗 -> 不允许白抽
    if not _prop_take(_LOTTERY_ITEM, cost):
        return b'[586]'                                # 抽奖券不足（客户端本地也会拦一次）
    tree = _tree_slot(act)
    pst = _pool_st(act)
    pity_cap = max(0, _i((tree or {}).get('maxrate'), 0))
    ids, groups, got_tree = [], [], False
    for _ in range(num):
        forced = bool(tree) and pity_cap > 0 and _i(pst['pity'], 0) >= pity_cap
        slot = tree if forced else _pick(board, 'rate1' if num == 1 else 'rate10')
        groups.extend(_grant(slot['itype'], slot['iid'], slot['inum']))
        if tree and slot['id'] == tree['id']:
            # ★ 抽中树神格：**再**从树神盘（DrawType=1）按权重给一份，并把它那一行的 ID 报上去
            #   （客户端 `lotteryRespons` 会把树神盘 ID 换成 `treeID` 去点亮九宫格）。
            tp = _D201.get((act, 1), [])
            trew = _pick(tp, 'rate1') if tp else None
            if trew is not None:
                groups.extend(_grant(trew['itype'], trew['iid'], trew['inum']))
                ids.append(trew['id'])
            else:
                ids.append(slot['id'])
            got_tree = True
            pst['pity'] = 0
        else:
            ids.append(slot['id'])
            pst['pity'] = _i(pst['pity'], 0) + 1
    pst['last'] = int(time.time())
    st = _st()
    st['total'] = _i(st.get('total'), 0) + num
    save_state()
    last_num = max(0, pity_cap - _i(pst['pity'], 0)) if pity_cap else 0
    return _out(1 if got_tree else 0, last_num, ids, [_gi(), _gd(groups)])


# ============================================================================
# 4504 推送 / 4505 面板
# ============================================================================
def handle_skin_box_push(body_json=None):
    """4504 -> `[1,[累计抽奖次数, [[宝箱ID, 状态], ...]]]`（客户端也被动收同一形状）。"""
    total, rows = _box_rows()
    return _out(total, rows)


def handle_skin_home_info(body_json=None):
    """4505 -> `[1,[抽奖券数量, [[奖池ID, 结束时间, 剩余几次中树神], ...], [累计抽奖次数, [[宝箱ID,状态]]]]]`。

    ⚠ 奖池**只给默认期（ActivityID=0）**：`A01_Activity` 里**没有**皮肤抽奖的排期
      （全表只有 101/103/104… 那几项），活动期 1~23 的起止时间**服务端无数据源**。
      按"没有数据源一律 0/不发"的铁律，不编一个倒计时；默认期本来就不显示倒计时
      （客户端 `nowId == 0` 时 `Node_Time:setVisible(false)`）。
    """
    st = _st()
    tree = _tree_slot(_ACTIVITY_DEFAULT)
    pst = _pool_st(_ACTIVITY_DEFAULT)
    cap = max(0, _i((tree or {}).get('maxrate'), 0))
    residue = max(0, cap - _i(pst['pity'], 0)) if cap else 0
    total, boxes = _box_rows()
    return _out(_prop_count(_LOTTERY_ITEM),
                [[_ACTIVITY_DEFAULT, 0, residue]],
                [total, boxes])


# ============================================================================
# 4506 宝箱领取
# ============================================================================
def handle_skin_get_box(body_json=None):
    """4506 req{boxId} -> `[roleInfo, dropInfo]`。

    码（`NetSkinLotteryBoxReward.m_errorCode` 逐字）：`537` 任务ID错误 / `538` 该任务未完成 /
    `539` 该任务奖励已经领取。
    """
    from handlers_game import _role_info as _gi, _drop as _gd
    from handlers_task import _grant_group
    req = _req(body_json)
    bid = _i(req.get('boxId'))
    row = _D204.get(bid)
    if row is None:
        return b'[537]'
    st = _st()
    if str(bid) in st['boxes']:
        return b'[539]'
    if _i(st.get('total'), 0) < row['draw_num']:
        return b'[538]'
    groups, ok = _grant_group(row['reward'])
    if not ok:
        return b'[0]'                                  # 奖励组查不到 -> 不记已领（下次还能领）
    st['boxes'][str(bid)] = 1
    save_state()
    return _out(_gi(), _gd(groups))


# ============================================================================
# 4507 树神奖励是否已拥有
# ============================================================================
def handle_skin_get_have_skin(body_json=None):
    """4507 req{id} -> `[1,[[[D201.ID, 是否拥有], ...]]]`。

    ⚠ `id` 是**奖池期**（客户端传 `self.lotteryIdList[idx].id`）；返回的行是**该期树神盘
      （DrawType=1）的全部格子、按 BoardID 升序** —— 客户端 `setLotteryItemTreeIsHave`
      是 `ipairs(_data)` 与 `activityItemTreeData[i]` **按位置**比的，顺序错了标记就贴错格子。
    """
    req = _req(body_json)
    act = _i(req.get('id'), _ACTIVITY_DEFAULT)
    rows = []
    for s in _D201.get((act, 1), []):
        have = 1 if (s['itype'] == _SKIN_TYPE and _skin_owned(s['iid'])) else 0
        rows.append([s['id'], have])
    return _out(rows)
