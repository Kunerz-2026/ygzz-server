# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 公会徽记(1814/1815/1816) + 新手引导任务(2068/2069) + 旧版限时任务(3070)
#   ★2026-09-12 (v26 goal-round-3) 新增实装。这三族都是 `_audit_netmap.py` 修正口径后
#     (按"有 sendMessage 调用点的可达 API"算) 才浮现出来的 —— 见交接档 §7.1。
#
#   客户端契约 (逐行核对):
#     1814 SIGN_SELECT_INFO  NetGetSociatySignSelect  req{gangId}
#          recv: `local iconList = type(msg[1])=="userdata" and {} or msg[1]`
#                然后 `for _,data in ipairs(iconList)` -> data[1]=signId / [2]=signIsLock / [3]=signTime
#          ★注意: 它**不是** 30 字段 roleInfo 那种结构, 而是"徽记行数组"。
#          ★静态表: `SDSociatySelectIcon.lua:18 -> tUN04_UnionIcon` (= UN04_UnionIcon.json, 9 行)
#            字段: ID/Name/Info/Image/Icon/Type(解锁类型)/level(解锁等级)/Price/ValidityPeriod/Display/Sort
#          signIsLock 语义 (由 VSociatySignSelect.lua:125~150 反推):
#            0 = 已拥有/当前生效   1 = 未解锁但可买(Price>0)   2 = 不可用(等级不够且不可买)
#     1815 SIGN_USE          req{gangId, iconId} -> 同 1814 的行数组 (客户端靠 EventManager 事件刷新)
#     1816 SIGN_BUY          req{gangId, iconId} -> 同 1814 的行数组
#          · 买徽记消耗的是 **公会财富** (VSociatySignSelect.lua:126 `getSociatyTreasure() < Price`)
#     2068 GUIDANCE_TASK_DATA NetGetNewbieTask  req{}
#          recv: msg[1] = [[taskid,tasknum,taskstate]] (pairs, 无守卫) ; msg[2] = finishState
#          静态表: `SDNewbieTask.lua -> tUG_05_GuideTask` (= UG_05_GuideTask.json, 23 行)
#            字段: ID/Tittle/JumpTag/ConditionType/Value/Quantity/RewardID/Sort
#     2069 GUIDANCE_TASK_AWARD NetNewbieTaskReward req{taskid} -> [roleInfo(30字段), dropInfo]
#     3070 LIMIT_TIME_TASK_INFO NetActivityLimitTask req{}
#          recv **8 个位置** 全部无守卫: [title, activityType, priority, startMs, endMs, img, taskID, jumpID]
#          ★ 客户端 `msg[4] / 1000` -> 必须给**毫秒**数字 (给字符串会算错, 给 nil 直接崩)
import json, time
import os
import accounts
from state import EQ_STATE, save_state, get_monster_base

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


def _out(*body):
    return ('[1,' + json.dumps(list(body), separators=(',', ':')) + ']').encode('utf-8')


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


# ============================================================================
# 公会徽记 1814/1815/1816
# ============================================================================
_UN04 = []          # [(id, name, level, price, type, validity, display, sort)]
_UN04_IDS = set()
try:
    _t = _load_json('UN04_UnionIcon.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _row = (_i(_r[_h['ID']]), str(_r[_h['Name']] or ''), _i(_r[_h['level']], 1),
                    _i(_r[_h['Price']]), _i(_r[_h['Type']]), _i(_r[_h['ValidityPeriod']]),
                    _i(_r[_h['Display']], 1), _i(_r[_h['Sort']], 1))
            _UN04.append(_row)
            _UN04_IDS.add(_row[0])
        except Exception:
            continue
    _UN04.sort(key=lambda x: x[7])
except Exception:
    pass


def _sg():
    return EQ_STATE.setdefault('sociaty', {'name': '', 'icon': 0, 'level': 1, 'rank': 0,
                                           'wealth': 0, 'notice': '', 'id': '', 'created': '',
                                           'todaydonate': 0, 'donate_date': ''})


def _sign_state():
    # 已购买徽记: {signId: 到期时间戳(0=永久)}; 当前使用的徽记 = sociaty.icon
    v = EQ_STATE.setdefault('sociaty_sign', {'owned': {}, 'used': 0})
    if not isinstance(v.get('owned'), dict):
        v['owned'] = {}
    return v


def _sign_rows():
    """生成徽记行 [[signId, signIsLock, signTime], ...]
       signIsLock: 0 已拥有/当前生效 | 1 未解锁可买 | 2 不可用"""
    sg = _sg()
    st = _sign_state()
    owned = st['owned']
    cur = _i(sg.get('icon', 0))
    glevel = max(1, _i(sg.get('level', 1), 1))
    now = int(time.time())
    rows = []
    for (sid, _n, lv, price, _ty, validity, display, _sort) in _UN04:
        if display != 1:
            continue
        exp = owned.get(str(sid))
        has = (str(sid) in owned)
        if sid == cur:
            lock = 0                                   # 当前生效
        elif has and (exp in (0, None) or _i(exp) > now):
            lock = 0                                   # 已拥有且未过期
        elif lv <= glevel or price <= 0:
            lock = 0                                   # 等级够 / 免费 -> 可用
        elif price > 0:
            lock = 1                                   # 可买
        else:
            lock = 2
        if exp is None:
            exp = 0
        rows.append([sid, lock, _i(exp)])
    return rows


def handle_sociaty_sign_select(body_json=None):
    # 1814
    return _out(_sign_rows())


def handle_sociaty_sign_use(body_json):
    # 1815: req{gangId, iconId}; 用它就把它记成"当前徽记"
    req = _req(body_json)
    sid = _i(req.get('iconId'))
    if sid not in _UN04_IDS:
        return b'[34]'                                # 物品不存在
    sg = _sg()
    if not sg.get('name'):
        return b'[50000]'                             # 请先加入一个工会
    st = _sign_state()
    # 使用前必须"已拥有 或 等级够 或 免费"
    lv = 1
    for (i2, _n, l2, _p, _t2, _v2, _d2, _s2) in _UN04:
        if i2 == sid:
            lv = l2
            break
    owned_ok = str(sid) in st['owned']
    if not owned_ok and lv > max(1, _i(sg.get('level', 1), 1)):
        return b'[50054]'                             # 徽记未解锁
    sg['icon'] = sid
    st['used'] = sid
    save_state()
    return _out(_sign_rows())


def handle_sociaty_sign_buy(body_json):
    # 1816: req{gangId, iconId}; 消耗 **公会财富**
    req = _req(body_json)
    sid = _i(req.get('iconId'))
    if sid not in _UN04_IDS:
        return b'[34]'
    sg = _sg()
    if not sg.get('name'):
        return b'[50000]'
    price = 0
    validity = 0
    for (i2, _n, _l2, p2, _t2, v2, _d2, _s2) in _UN04:
        if i2 == sid:
            price, validity = p2, v2
            break
    if price <= 0:
        return b'[50054]'                             # 免费徽记无需购买
    st = _sign_state()
    if str(sid) in st['owned']:
        return b'[95]'                                # 已拥有
    if _i(sg.get('wealth', 0)) < price:
        return b'[50046]'                             # 工会财富值不足
    sg['wealth'] = _i(sg.get('wealth', 0)) - price
    # ValidityPeriod(天) 0 = 永久
    st['owned'][str(sid)] = (int(time.time()) + validity * 86400) if validity > 0 else 0
    save_state()
    return _out(_sign_rows())


# ============================================================================
# 新手引导任务 2068 / 2069
# ============================================================================
_UG05 = []          # [(id, condtype, value, quantity, reward)]
_UG05_BY_ID = {}
try:
    _t = _load_json('UG_05_GuideTask.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _row = (_i(_r[_h['ID']]), _i(_r[_h['ConditionType']]), _i(_r[_h['Value']]),
                    _i(_r[_h['Quantity']], 1), _i(_r[_h['RewardID']]))
            _UG05.append(_row)
            _UG05_BY_ID[_row[0]] = _row
        except Exception:
            continue
    _UG05.sort()
except Exception:
    pass


def _guide_claimed():
    return EQ_STATE.setdefault('guide_claimed', {})


def _ug05_progress(ct, qty):
    # 与每日任务/限时任务同一本账: EQ_STATE['a07_ct'] (ConditionType -> 累计次数)
    st = EQ_STATE.setdefault('a07_ct', {})
    n = _i(st.get(str(_i(ct))), 0)
    return min(n, _i(qty)) if qty else n


def handle_guidance_task_data(body_json=None):
    # 2068: msg[1] = [[taskid,tasknum,taskstate]] (pairs 无守卫) ; msg[2] = finishState
    #   taskstate: 0 未完成 / 1 未领取 / 2 已领取
    claimed = _guide_claimed()
    rows = []
    all_done = True
    for (tid, ct, val, qty, rid) in _UG05:
        num = _ug05_progress(ct, qty)
        if str(tid) in claimed:
            state = 2
        else:
            state = 1 if (qty and num >= qty) else 0
            if state != 1:
                all_done = False
        rows.append([tid, num, state])
    # finishState: 客户端用于"全部完成"展示; 全部已领 = 2
    finish = 2 if (rows and all(r[2] == 2 for r in rows)) else (1 if all_done else 0)
    return _out(rows, finish)


def handle_guidance_task_award(body_json):
    # 2069: req{taskid} -> [roleInfo(30字段), dropInfo]
    from handlers_game import _role_info as _gi, _drop as _gd
    from handlers_task import _grant_group
    req = _req(body_json)
    tid = _i(req.get('taskid'))
    row = _UG05_BY_ID.get(tid)
    if not row:
        return b'[543]'                               # 参考码: 查无此任务
    _tid, ct, val, qty, rid = row
    claimed = _guide_claimed()
    if str(tid) in claimed:
        return b'[95]'                                # 已经领取过
    if qty and _ug05_progress(ct, qty) < qty:
        return b'[63]'                                # 条件未达成
    groups, ok = _grant_group(rid)
    if not ok:
        return b'[51]'                                # 奖励组查不到
    claimed[str(tid)] = 1
    save_state()
    return _out(_gi(), _gd(groups))


# ============================================================================
# 旧版限时任务 3070
# ============================================================================
_A01_META = {}
try:
    _t = _load_json('A01_Activity.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _A01_META[_i(_r[_h['ID']])] = (str(_r[_h['Tittle']] or ''),
                                           str(_r[_h['Begin']] or '20180810000001'),
                                           str(_r[_h['Over']] or '20291231235959'))
        except Exception:
            continue
except Exception:
    pass


def _ymd_to_ms(s):
    # "20180810000001" / "20291231235959" -> 毫秒时间戳
    try:
        s = str(s)
        t = time.strptime(s[:14], '%Y%m%d%H%M%S')
        return int(time.mktime(t) * 1000)
    except Exception:
        return int(time.time() * 1000)


def handle_activity_limit_task(body_json=None):
    # 3070 LIMIT_TIME_TASK_INFO (NetActivityLimitTask:recvMessage msg[1..8] **全部无守卫**)
    #   [1]title [2]activityType [3]priority [4]startTime(毫秒) [5]endTime(毫秒)
    #   [6]img [7]taskID [8]jumpID
    #   ★ 客户端对 [4]/[5] 做 `/1000` -> 必须是**数字毫秒**, 不能给 14 位字符串
    #   taskID 用真实旧版限时任务 id (UG_06 Stage=-1 大奖行) —— 铁律 #22
    tid = 10100
    try:
        from handlers_activity import _UG06_BIG
        if _UG06_BIG:
            tid = int(_UG06_BIG)
    except Exception:
        pass
    aid = 101
    title, begin, over = _A01_META.get(aid, ('限时任务', '20180810000001', '20291231235959'))
    start_ms = _ymd_to_ms(begin)
    end_ms = _ymd_to_ms(over)
    return _out(title, 0, 1, start_ms, end_ms, '', tid, 0)


# ============================================================================
# 收尾族 (★2026-09-12 v26 goal-round-3): 注册/建角 1001·1002 + 充值族 5004~5007 / 1110 / 1111
#   口径修正后仍在"可达未覆盖"里的最后一批。它们都不是"会崩"型, 但:
#     · 1001/1002 是**新账号/新角色**的必经路径 —— 现在靠 HTTP /hoolailogin 兜住了,
#       可一旦客户端走 socket 注册/建角, 兜底 [1,[[]]] 会让 `gamer_setAccount(nil)` 把账号写坏
#     · 5007 充能商店: 兜底 -> `ipairs(msg[1])` 拿到空表 -> 商店**空列表**
#     · 5004/5005/1110/1111 充值奖励类: 兜底 -> roleInfo 非 30 字段 -> 数值错乱
#     · 5006 SDK_BUY: 私服**没有计费**, 所以明确回"暂未开放 77"而不是假装下单
#   ★ 私服对充值族的统一策略: **可看不可买** —— 列表按真表下发, 购买明确失败。
# ============================================================================
def handle_register(body_json=None):
    # 1001: recvMessage 直取 msg[1..3] = [macid, account, userpwd] (全部无守卫)
    req = _req(body_json)
    macid = str(req.get('macid') or req.get('macId') or 'emu-0001')
    account = str(req.get('account') or req.get('userId') or '1')
    pwd = str(req.get('userpwd') or req.get('password') or '1')
    return _out(macid, account, pwd)


def handle_role_create(body_json=None):
    # 1002: recvMessage `tostring(msg[1])` -> 角色名; 之后 gamer_setGamerName
    req = _req(body_json)
    name = str(req.get('rolename') or req.get('roleName') or req.get('name') or '')
    if not name:
        name = str(EQ_STATE.get('role_name') or '测试')
    EQ_STATE['role_name'] = name
    save_state()
    return _out(name)


# ---------- 充值族 ----------
_G02_CACHE = None


def _g02_rows():
    """G02_Shop: 人民币商品 (PriceType=1) -> 充能商店列表用"""
    global _G02_CACHE
    if _G02_CACHE is not None:
        return _G02_CACHE
    rows = []
    try:
        t = _load_json('G02_Shop.json'); h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                if _i(r[h['PriceType']]) != 1:
                    continue
                rows.append((_i(r[h['ID']]), _i(r[h['Price']]), _i(r[h['GoodsCount']], 1),
                             str(r[h['Image']] or ''), str(r[h['Tittle']] or '')))
            except Exception:
                continue
        rows.sort()
    except Exception:
        rows = []
    _G02_CACHE = rows
    return rows


def handle_shop_recharge(body_json=None):
    # 5007: msg[1] = [[goodid, price, count, icon, des]] (ipairs)
    rows = [[gid, price, cnt, icon, des] for (gid, price, cnt, icon, des) in _g02_rows()[:20]]
    if not rows:
        rows = [[1, 6, 60, 'Loot_YuanBao_153x153', '充能 60'],
                [2, 30, 300, 'Loot_YuanBao_153x153', '充能 300'],
                [3, 98, 980, 'Loot_YuanBao_153x153', '充能 980']]
    return _out(rows)


def handle_total_recharge(body_json=None):
    # 5004: msg[1] = [[累计充值id, 状态(0不能领/1可领/2已领)]]
    #   ★ 客户端 NetTotalRecharge:sendMessage 里是 `if true return end` (**所有版本都不发这条**),
    #     所以它只可能被服务端**主动推**。
    #   ★v60: 有了假充值闭环之后 `recharge.total` 会真的增长 -> 状态要按**真实累计额**算,
    #         不能再"一律 0 不可领"（那会让玩家充了钱也领不到）。
    from handlers_opsact import _A01_1, _bootstrap
    _bootstrap()
    claimed = EQ_STATE.setdefault('recharge_claimed', {})
    cur = _i((EQ_STATE.get('recharge') or {}).get('total'))
    rows = []
    for (cid, target, rid) in (_A01_1.get('107') or [])[:21]:
        rows.append([cid, 2 if str(cid) in claimed else (1 if cur >= target else 0)])
    if not rows:
        for (cid, target, rid) in (_A01_1.get('101') or [])[:3]:
            rows.append([cid, 2 if str(cid) in claimed else (1 if cur >= target else 0)])
    return _out(rows)


def _recharge_reward(body_json, kind):
    from handlers_game import _role_info as _gi, _drop as _gd
    from handlers_task import _grant_group
    from handlers_opsact import _A01_1, _bootstrap
    _bootstrap()
    req = _req(body_json)
    cid = _i(req.get('taskid') or req.get('id'))
    rows = {c: (t, r) for (c, t, r) in ((_A01_1.get('107') or []) + (_A01_1.get('101') or []))}
    if cid not in rows:
        return b'[543]'
    target, gid = rows[cid]
    claimed = EQ_STATE.setdefault('recharge_claimed', {})
    if str(cid) in claimed:
        return b'[95]'                                # 已经领取过
    # ★v60: 按**真实累计充值额**判定（假充值闭环会把它加上去），不再一律 [87]。
    cur = _i((EQ_STATE.get('recharge') or {}).get('total'))
    if cur < target:
        return b'[87]'                                # 累计充值金额未达到购买限制
    groups, ok = _grant_group(gid)
    if not ok:
        print('[RECHARGE] !! 发放失败 cid=%s gid=%s' % (cid, gid), flush=True)
        return b'[0,{}]'
    claimed[str(cid)] = 1
    save_state()
    return ('[1,' + json.dumps([_gi(), _gd(groups)], separators=(',', ':')) + ']').encode('utf-8')


def handle_total_get_recharge(body_json):
    # 5005: [roleInfo(30字段), dropInfo]
    return _recharge_reward(body_json, 'total')


def handle_recharge_day_reward(body_json):
    # 1110: [roleInfo(30字段), dropInfo]
    return _recharge_reward(body_json, 'day')


def handle_recharge_box_reward(body_json):
    # 1111: [roleInfo(30字段), dropInfo]
    return _recharge_reward(body_json, 'box')


def handle_sdk_buy(body_json=None):
    # 5006 SDK_BUY —— 2026-11-07 全付费回滚：**不再假装下单/发货**。
    #
    # 历史：2026-09-16 曾按用户裁定做过“假充值闭环”（服务端自己完成购买 + 累计充值 + 推 8084）。
    # 2026-11-07 用户裁定回滚：商城恢复原版标价、所有 RMB 商品不可购买、降低法律风险。
    # 客户端链路不变：5006 拿不到 orderID -> `NetSdkBuy.m_errorCode[77]` 弹「暂未开放」，
    # 不会像“调官方支付 SDK”那样卡死。
    #
    # 返回：
    #   · shopid 为空/非数字/不在 G02_Shop -> [107]（参数错误，客户端码表同义）
    #   · 合法 shopid -> [77]（暂未开放；不发货、不记 recharge.total、不推 8084）
    req = _req(body_json)
    sid = 0
    for _part in str(req.get('shopid') or '').split('&'):
        _part = _part.strip()
        if _part.isdigit():
            sid = int(_part)
            break
    if not sid:
        return b'[107]'
    try:
        from handlers_game import _SHOP_ROWS
    except Exception as _e:
        print('[SDK_BUY] !! 导入 handlers_game 失败: %r' % (_e,), flush=True)
        return b'[77]'
    if sid not in _SHOP_ROWS:
        return b'[107]'
    return b'[77]'
