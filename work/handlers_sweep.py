# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 扫荡 SWEEP_ENTER(1118) / SWEEP_START(1119) / SWEEP_FINISH(1120) + 兑换码 REDEEM_CODE(5000)
#   ★2026-09-12 (v26 goal-round-2 续) 新增实装。
#
#   ★怎么发现的 (方法论点): `_audit_netmap.py` 的口径在这一轮被修正 —— 旧实现只扫
#     `SocketManager.lua` 的注册表 (248 个 Net 类), 漏掉了**没注册但有真实调用点**的族
#     (公会 sociaty / 扫荡 sweepAway / 兑换码 …)。改成"两路都扫 + 只按**有 sendMessage 调用点**的
#     可达 API 算实装率"之后, 立刻暴露出两条**新的 A 类崩点**:
#       1119 SWEEP_START  `prop.field(msg[2])`  -> 属性关扫荡"一键扫荡"点了直接卡死
#       1120 SWEEP_FINISH 同上
#     教训: 覆盖率的分母取错, 会让整类问题**系统性地看不见**。
#
#   客户端契约 (逐行核对):
#    1118 NetSweepAwayEnter  : req{gateid(ZZRc4 加密)}; recvMessage 只 cclog + 回调 (不读 msg[N])
#    1119 NetSweepAwayStart  : req{ourside:[ucode...], gateid(加密), time}
#         recvMessage: msg[1]=roleInfo(30字段) msg[2]=prop(道具 item, 直取 prop[1])
#                      msg[3]=ourFormation(我方怪 item 数组, 客户端 `#monsterInfo > 1` 判有效)
#                      msg[4]=exloreRewardList msg[5]=battleRewardList msg[6]=unlockSign
#         · msg[4]/[5] 交给 `IconData:getLocalRenderList(ICON_ALL, list)` ->
#           `parseLocalRewardData` 要的是 **[{id=,type=,coinNum=}]** 形态 (不是 dropGroup!)
#           —— 与 dropInfo 的 `[[style, goodslist]]` **不是一个形状**, 写错就是空图标。
#         · msg[6] unlockSign -> ControlSweepAway:setUnlockSign(...)
#    1120 NetSweepAwayFinish : 同 1119 的 6 段结构, 但走 `sweepBattleSuccessed(exlore, battle, prop)`
#    5000 NetRedeemCode      : req{code}; recvMessage: msg[1]=roleInfo(30字段) msg[2]=dropInfo
#         · 错误码: 见客户端 m_errorCode (未配置的码原样弹)
import json, time
from state import EQ_STATE, save_state

_SWEEP_PROP = '1006'          # 扫荡消耗 = 体力道具 (S01Level.Energy; 私服与探索共用 1006)


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


def _sweep_state():
    return EQ_STATE.setdefault('sweep', {'day': '', 'count': 0, 'stage': 0})


def _reset_day():
    s = _sweep_state()
    today = time.strftime('%Y-%m-%d')
    if s.get('day') != today:
        s['day'] = today
        s['count'] = 0
    return s


def _reward_items(groups, limit=8):
    """把服务端内部的 dropGroup `[[style, goodslist]]` 转成 `IconData:parseLocalRewardData`
       要的 `[{id=, type=, coinNum=}]`。
       ⚠ 这是**两个不同形状**, 别混:
         · dropInfo(给 NetDropRewardData) = [[style, goodslist]]
             - 货币类 (style 3铜钱/6鬼道/7妖魂/8唤妖符/9体力/17经验/21活跃): goodslist = [[num]]
             - 道具类 (style 2/4/5/18/22/98): goodslist = [[ucode, staticId, num, ...]]
         · IconData:getLocalRenderList 要 = [{id=静态id, type=style, coinNum=数量}]
       旧实现把"货币行的 num"当静态 id 传进去 -> 图标必然错 (已修)。
    """
    out = []
    for g in (groups or []):
        try:
            style = _i(g[0])
            goods = g[1] or []
        except Exception:
            continue
        for it in goods:
            if len(out) >= limit:
                return out
            if not isinstance(it, list) or not it:
                continue
            if style in (3, 6, 7, 8, 9, 17, 21):
                # 货币/资源: [num] -> id 传 0 (getIconInfo 只认 type, 数量走 coinNum)
                out.append({'id': 0, 'type': style, 'coinNum': _i(it[0], 1) or 1})
            else:
                # 道具/装备/怪/皮肤/宝箱/头像框/礼包: [ucode, staticId, num, ...]
                sid = _i(it[1]) if len(it) > 1 else _i(it[0])
                num = _i(it[2], 1) if len(it) > 2 else 1
                out.append({'id': sid, 'type': style, 'coinNum': num or 1})
    return out


def _my_formation_items():
    from handlers_equip import _mon_with_state
    from state import all_monster_ucodes
    _rows = []
    try:
        from handlers_sys import _form_normalize
        _rows = _form_normalize(5)
    except Exception:
        _rows = []
    ucs = [r[0] for r in _rows] or [str(u) for u in all_monster_ucodes()[:5]]
    items = []
    for uc in ucs:
        mi = _mon_with_state(uc)
        if mi:
            items.append(json.loads(mi))
    return items


def handle_sweep_enter(body_json=None):
    # 1118: 进入扫荡界面。客户端 recvMessage **只 cclog + 回调**, 不读 msg[N]
    #   -> 回 [1,[]] 即可 (但**不能**落兜底: 兜底 [1,[[]]] 也不崩, 所以这条只是"以前没反应")
    return b'[1,[]]'


def _do_sweep(body_json, finish=False):
    from handlers_game import _role_info, _drop, _t25_today, player_add_exp
    from handlers_task import _grant_group
    req = _req(body_json)
    # 消耗: 1 点体力 (与探索一致); 体力不足 -> 客户端 m_errorCode 里的"体力不足"码 (515 未配置则用 -1 避免误导)
    try:
        have = _i(EQ_STATE.get('prop', {}).get(_SWEEP_PROP, 0))
    except Exception:
        have = 0
    if have <= 0:
        # ⚠ 没有 1 点体力就明确失败 (客户端会弹自己的 m_errorCode 文案)
        return b'[-1]'
    s = _reset_day()
    EQ_STATE.setdefault('prop', {})[_SWEEP_PROP] = have - 1
    s['count'] = _i(s.get('count', 0)) + 1
    # ★★2026-10-17（任务链收口）：CT=47「扫荡关卡」的事件源 = 本函数（1119 开始扫荡 /
    #   1120 立即结束，两条都真的扣 1 点体力并发奖）。A07 10512（qty 12）此前永远 0 进度。
    #   口径：一次真实结算记 1（扣了体力才算，体力不足在上面已 return）。
    try:
        from handlers_task import bump_task as _bt, CT_SWEEP as _CT_SWEEP
        _bt(_CT_SWEEP, 1)
    except Exception as _e:
        print('[SWEEP] !! bump CT=47 失败(不影响扫荡本身): %r' % (_e,), flush=True)
    # 奖励: 用真实奖励组表 (与每日任务同一套 _grant_group)
    from handlers_task import _grant_group as _gg
    groups, _ok = _gg(4001) if _gg else ([], False)
    if not groups:
        groups = [[3, [[300]]], [7, [[20]]]]         # 兜底: 铜钱 300 / 妖魂 20
    try:
        EQ_STATE['gold'] = _i(EQ_STATE.get('gold', 0)) + 300
        player_add_exp(50)
    except Exception:
        pass
    save_state()
    rid, mid = (1001, 100031)
    try:
        rid, mid = _t25_today()
    except Exception:
        pass
    prop_item = [str(_SWEEP_PROP), 1006, 1]
    rewards = _reward_items(groups)
    # 客户端 setUnlockSign 用: 1 = 有"立即结束"入口 (私服给 0 保守)
    return _out(_role_info(), prop_item, _my_formation_items(), rewards, rewards, 0)


def handle_sweep_start(body_json):
    # 1119: 一键扫荡 (单次)。msg[2] 是道具 item -> 客户端 `prop_getMe():updateItem(prop[1], prop)` 直取, **必须是数组**
    return _do_sweep(body_json, finish=False)


def handle_sweep_finish(body_json):
    # 1120: 扫荡结算。同 6 段结构
    return _do_sweep(body_json, finish=True)


# ============================ 5000 兑换码 ============================
_REDEEM_CODES = {
    # 私服内置码 (大小写不敏感): 码 -> [(style, goodsid, num)]
    'YGZZ666': [(7, '9999', 666), (3, '0', 66666)],
    'YGZZ888': [(7, '9999', 888), (3, '0', 88888)],
    'FUHuo2026': [(7, '9999', 2026), (3, '0', 202600)],
}


def handle_redeem_code(body_json):
    # 5000: req{code} -> [roleInfo(30字段), dropInfo]
    #   ⚠ 老客户端里兑换码走的是 **HTTP** 而不是 socket: 若某版本 UI 不发 5000, 这个 handler 也不会被调到
    #     (无害)。但一旦被调, 兜底会给出非 30 字段的 roleInfo -> 界面数值错乱, 所以必须实装。
    from handlers_game import _role_info, _drop
    from handlers_task import _grant_group
    req = _req(body_json)
    # ★★2026-10-16（请求字段对账抓到的真缺陷）：`NetRedeemCode.lua` 发的是 **`codekey`**
    #   （`msg["codekey"] = _codekey or -1`），修前这里只读 `code`/`redemptionCode`
    #   ⇒ 兑换码永远拿不到 ⇒ 永远回"兑换码无效"，玩家也看不到原因。
    #   `-1` 是客户端的"没填"哨兵值，按空码处理。
    code = str(req.get('codekey') or req.get('code') or req.get('redemptionCode') or '').strip()
    if code in ('-1', '0'):
        code = ''
    if not code:
        return b'[0,{}]'
    used = EQ_STATE.setdefault('redeem_used', {})
    if code.upper() in used:
        return b'[95]'                                # 已经领取过 (客户端通用码)
    groups = _REDEEM_CODES.get(code) or _REDEEM_CODES.get(code.upper())
    if not groups:
        return b'[34]'                                # 兑换码不存在/无效
    got = []
    for (style, gid, num) in groups:
        if style == 3:
            EQ_STATE['gold'] = _i(EQ_STATE.get('gold', 0)) + _i(num)
        elif style == 7:
            EQ_STATE['soulcoin'] = _i(EQ_STATE.get('soulcoin', 0)) + _i(num)
        got.append([style, [[gid, num]]] if str(gid).isdigit() else [[num]])
    used[code.upper()] = 1
    save_state()
    return _out(_role_info(), _drop(got))
