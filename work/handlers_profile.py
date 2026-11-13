# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 头像(1092-1096) + 宝箱(1060-1062) (2026-09-06 按 Net* 契约)
#   1092 HEAD_INFO    -> [1,[[id,state],...]] state: 0已解锁 2未解锁 (T35_HeadImage)
#   1093 HEAD_BUY     req {id} -> [1,[roleInfo]] (CostType=0 金币, Cost=1000)
#   1095 HEAD_SAVE    req {imgid} -> [1,[roleInfo]]
#   1094 HEAD_FRAME_INFO -> 同上 (T36_HeadFrame, 仅默认框 1001 解锁)
#   1096 HEAD_FRAME_SAVE req {imgframeid} -> [1,[roleInfo]]
#   1060 TREASURE_GET -> [1,[[id, ucode, count],...]] (宝箱=道具; L103 箱 id)
#   1061 TREASURE_BUY req {staticid, count} -> [1,[roleInfo, [fromid, [boxdata]]]] (鬼道积分 CurrencyQuantity×count)
#   1062 TREASURE_ACHIVE req {ucode} -> [1,[roleInfo, dropInfo]] (L103_TreasureBoxAward 按 ArrayID 权重开箱)
import json, random, time
from state import EQ_STATE, save_state

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

# ============================================================================
# 改名 CHANGE_NAME(2001)   ★2026-09-12 (v26 P1) 新增实装
# ----------------------------------------------------------------------------
# 客户端明文 (app/network/changename/NetChangeName.lua):
#   sendMessage(_name): msg = {newRoleName = _name}
#   recvMessage: statecode~=1 -> showTips(m_errorCode[statecode])
#                msg[1] = roleInfo(30字段) -> SQManager:gamer_setRoleInfo(roleInfo)
#                msg[2] = removeProp(道具 item [ucode,id,count]) -> prop_updateItem
#   错误码 (权威): 0/2 名字超20 / 3 非法字符 / 4 已被占用 / 7 空 / 9 已有角色
#                  15 特殊字符 / 17 不足4位 / 4101 每天只能改一次 / 4102 改名卡不足
#   ⚠ 旧实现根本没有 2001 -> 兜底 [1,[[]]] -> msg[2]=nil 跳过(不崩) 但 msg[1]={} 被当成
#     roleInfo 灌进 SQManager:gamer_setRoleInfo -> 玩家金币/等级/体力全变 nil -> 界面数值错乱。
#   ⚠ 铁律 #22: roleInfo 必须是**真实 30 字段** (客户端按 index 取 gold/level/exp...)
# ============================================================================
def handle_change_name(body_json):
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    name = str(req.get('newRoleName') or '')
    if not name:
        return b'[7]'                       # 角色名不能为空
    if len(name) > 20:
        return b'[2]'                       # 角色名太长
    if len(name) < 4:
        return b'[17]'                      # 角色名不足 4 位
    today = time.strftime('%Y-%m-%d')
    if str(EQ_STATE.get('name_changed_date') or '') == today:
        return b'[4101]'                    # 每天只能修改一次
    EQ_STATE['role_name'] = name
    EQ_STATE['name_changed_date'] = today
    save_state()
    from handlers_sys import role_info_array
    return ('[1,[' + role_info_array() + ']]').encode('utf-8')


# ============================================================================
# 好友列表 FRIEND_LIST(2100)   ★2026-09-12 (v26 P1) 新增实装
# ----------------------------------------------------------------------------
# 客户端明文 (app/network/friend/NetFriendList.lua:29~52):
#   sendMessage() 请求体为空
#   recvMessage: msg[1] = sendCoinCount (每日可送金币次数, getCoinGold() 缺省 50)
#                for _,friendInfo in pairs(msg[2]) do   ← ★ msg[2] 必须是表, nil -> pairs(nil) 崩
#                  一行 13 字段: serverId/roleName/friendName/friendLevel/friendHeadID/friendState
#                                /figthLogTime/figthLogResult/talkLastTime/isOnline
#                                /sendReward/acceptReward/state
#   friendState: 1好友 2黑名单 3最近联系人 ;  state: 0无关系 1是好友 2我申请 3对方申请
#   调用点: MFriendHome:26 / ChatPrivate:170 / PVEView:451 (进游戏主界面就可能触发)
#   私服无其他玩家 -> 造 2 条"好友 + 最近联系人"机器人行 (头像 id 用真实 T35 当前值, 铁律 #22)
# ============================================================================
def handle_friend_list(body_json=None):
    fw = _fw()
    if fw is not None:
        return _real_friend_list(fw)
    head = EQ_STATE.get('head') or {}
    h1 = int(head.get('eq', 1001) or 1001)
    now = int(time.time())
    # 13 字段 = [serverId, roleName, friendName, friendLevel, friendHeadID, friendState,
    #            figthLogTime, figthLogResult, talkLastTime, isOnline, sendReward, acceptReward, state]
    rows = [
        [1, '测试', '青丘小狐', 32, h1, 1, now - 3600, 1, now - 600, 1, 0, 1, 1],
        [1, '测试', '醉里挑灯', 28, h1, 3, now - 86400, 0, now - 7200, 0, 1, 0, 0],
    ]
    rows.extend(_friend_extra_rows())
    return ('[1,' + json.dumps([50, rows], separators=(',', ':')) + ']').encode('utf-8')


# ============================================================================
# 好友族 2101~2109   ★2026-09-12 (v26 P1) 新增实装
# ----------------------------------------------------------------------------
# 上一轮已做 2100 FRIEND_LIST (面板能看到 2 条机器人), 但**加/删/搜索/送收金币**全是兜底:
#   2104 RCMDS  : `pairs(msg[1])` 无守卫 -> nil -> 崩 (推荐好友页)
#   2108 SEARCH : msg[1..5] 直取无守卫 -> 崩 (搜索好友)
#   2106/2107/2109 SEND/RECV/ONEKEY: msg[1] 当 roleInfo 灌进 gamer_setRoleInfo -> 数值错乱
#   2101/2102/2103/2105: recvMessage 只回调 (兜底 msg[1]={} 不崩) -> 但"点了没反应"
#
# 客户端契约 (逐行核对 client Lua/app/network/friend/):
#   2101 NetFriendAdd    send{friendName}            recv: 只回调
#   2102 NetFriendOKAdd  send{confrimType,friendName} recv: 只回调 (confrimType 2=同意)
#   2103 NetFriendDelete send{friendName}            recv: 只回调
#   2104 NetFriendRcmds  send{account,userpwd}       recv: msg[1]=推荐列表(pairs!)
#          行 13 字段与 2100 同构: serverId/roleName/friendName/friendLevel/friendHeadID/
#          friendState/figthLogTime/figthLogResult/talkLastTime/isOnline/sendReward/acceptReward/state
#   2105 NetFriendUpdate send{account,userpwd}       recv: 只回调 (刷新)
#   2106 NetFriendSend   send{friendName}            recv: msg[1]=roleInfo(30字段)
#   2107 NetFriendRecv   send{friendName}            recv: msg[1]=roleInfo(30字段)
#   2108 NetFriendSearch send{}                      recv: msg[1..5]=serverId/roleName/roleLevel/
#                                                          roleHeadID/isOnline
#   2109 NetFriendOneKeyRecv send{}                  recv: msg[1]=roleInfo(30字段)
#
# 状态: EQ_STATE['friends'] = { 名字: 13 字段行 } —— 2100 的机器人 2 条是"内置", 这里只存新增的
#   (私服无其他玩家, 所以"加好友/同意"就是把对方记进本地名单, "删除"就是移除; 搜索/推荐造机器人)
# ============================================================================
_FRIEND_BUILTIN = ('青丘小狐', '醉里挑灯')
_FRIEND_POOL = ['半城烟沙', '一剑霜寒', '白衣渡江', '明月照沟渠', '大梦谁先觉',
                '踏雪寻梅', '孤舟蓑笠翁', '清风徐来', '沧海一粟', '剑指苍穹']


def _friends():
    v = EQ_STATE.setdefault('friends', {})
    return v if isinstance(v, dict) else {}


# ============================================================================
# 好友：**多账号真实关系**（2026-11-05 · 执行书 C2「最小真实好友」）
# ----------------------------------------------------------------------------
# 修前：`friends` 是**每账号私有**的一个名字表，加好友=把自己的档里写一行，
#   对方**完全不知道**（申请/同意/删除/赠收全是单机假动作，`_FRIEND_BUILTIN` 两条机器人撑场面）。
#
# 现在（仅 `ENABLED and REGISTRY`，单档仍走原路径，逐字节不变）：
#   `world['friends'] = {`
#       `'edges':    { aid: {aid: ts} },      # 双向各一条 —— 好友关系**唯一**真值`
#       `'requests': { aid: {aid: ts} },      # 收件人 -> 申请人（单向待办）`
#       `'gifts':    { aid: {day, sent{}, got{}} }   # 当日赠/收额度与流水`
#   `}`
# 两条硬纪律：
#   ① **写别人的档必须走 `REGISTRY.guild_transaction(对方)`**（journal 先落盘、可重放），
#      并且**只改 `acc.data` 的标量**，绝不 `EQ_STATE.bind(对方)` —— bind 之后任何
#      `save_state()` 都会写别人的档（`_b7` 的"B 档逐字节没变"判据正卡这里）。
#   ② 每日上限用 `FRIEND_DAILY_MAX`（客户端 `NetFriendList:getCoinGold` 缺省 50）；
#      赠送是**托管**：发送方立即 -50，接收方**领取时** +50 —— 金币总量守恒，且两边都有事件可断言。
# ============================================================================
FRIEND_DAILY_MAX = 50       # 每日赠/收上限（客户端缺省 50）
FRIEND_GIFT_GOLD = 50       # 每次赠/收的金币数（沿用原实现的值）


def _fw():
    """世界键 `friends`（多账号才不是 None）。形状不对 -> None（调用方落回单档路径）。"""
    try:
        from accounts import ENABLED, REGISTRY
        if not (ENABLED and REGISTRY is not None):
            return None
        w = REGISTRY.world
        f = w.get('friends')
        if not isinstance(f, dict):
            f = w['friends'] = {}
        for k in ('edges', 'requests', 'gifts'):
            if not isinstance(f.get(k), dict):
                f[k] = {}
        return f
    except Exception:
        return None


def _me_aid():
    try:
        from accounts import EQ_STATE as _AS
        return getattr(_AS.current(), 'id', None)
    except Exception:
        return None


def _acc_of_aid(aid):
    try:
        from accounts import REGISTRY
        for a in REGISTRY.all_accounts_full():
            if str(a.id) == str(aid):
                return a
    except Exception:
        pass
    return None


def _acc_name_of(acc):
    try:
        from handlers_rank import _acc_name
        return _acc_name(acc)
    except Exception:
        pass
    try:
        return str((acc.data or {}).get('role_name') or ('未命名#' + str(acc.id)))
    except Exception:
        return '未命名'


def _acc_by_role(name):
    """按 `role_name`（或 `未命名#<id>`）找账号；找不到 -> None。**不 bind、不落盘**。"""
    name = str(name or '').strip()
    if not name:
        return None
    try:
        from accounts import REGISTRY
        accs = REGISTRY.all_accounts_full()
    except Exception:
        return None
    for a in accs:
        if _acc_name_of(a) == name:
            return a
    for a in accs:
        if str(getattr(a, 'id', '')) == name:
            return a
    return None


def _is_online(aid):
    try:
        from accounts import REGISTRY
        return str(aid) in {str(x) for x in (REGISTRY.online or {})}
    except Exception:
        return False


def _friend_gift_day(fw, aid):
    """取/建该账号的当日赠收账；跨天自动清零（与 `friends_coin` 同口径）。"""
    today = time.strftime('%Y-%m-%d')
    g = fw['gifts'].get(str(aid))
    if not isinstance(g, dict) or g.get('day') != today:
        g = {'day': today, 'sent': {}, 'got': {}}
        fw['gifts'][str(aid)] = g
    for k in ('sent', 'got'):
        if not isinstance(g.get(k), dict):
            g[k] = {}
    return g


def _frow(acc, myname, state, fw):
    """一行 13 字段（与 2100 契约同构）。

    `[serverId, roleName, friendName, friendLevel, friendHeadID, friendState,
      figthLogTime, figthLogResult, talkLastTime, isOnline, sendReward, acceptReward, state]`
    · roleName = **我**的名字（客户端 `MFriendHome` 用它区分自己发的消息）
    · sendReward = 1 我今天还能给 TA 送（未送过）
    · acceptReward = 1 我还能从 TA 那里领（TA 今天送过我、我没领）
    """
    try:
        from handlers_rank import _acc_head
        head = _acc_head(acc)
    except Exception:
        head = 1001
    try:
        lv = int((acc.data or {}).get('player_lv') or 1)
    except Exception:
        lv = 1
    me = _me_aid()
    myg = _friend_gift_day(fw, me)
    theirg = _friend_gift_day(fw, acc.id)
    can_send = 0 if str(acc.id) in myg['sent'] else 1
    can_recv = 1 if (str(me) in theirg['sent'] and str(acc.id) not in myg['got']) else 0
    return [1, myname, _acc_name_of(acc), lv, head, 1,
            0, 0, 0, 1 if _is_online(acc.id) else 0,
            can_send, can_recv, state]


def _real_friend_list(fw):
    me = _me_aid()
    myname = _role_name()
    rows = []
    for aid in sorted((fw['edges'].get(str(me)) or {}).keys()):
        acc = _acc_of_aid(aid)
        if acc is not None:
            rows.append(_frow(acc, myname, 1, fw))
    # 我发出的申请（state=2）
    for to_aid, reqs in sorted(fw['requests'].items()):
        if str(me) in (reqs or {}):
            acc = _acc_of_aid(to_aid)
            if acc is not None:
                rows.append(_frow(acc, myname, 2, fw))
    # 收到的申请（state=3）
    for from_aid in sorted((fw['requests'].get(str(me)) or {}).keys()):
        acc = _acc_of_aid(from_aid)
        if acc is not None:
            rows.append(_frow(acc, myname, 3, fw))
    return (('[1,' + json.dumps([50, rows], separators=(',', ':')) + ']')
            .encode('utf-8'))


def _friend_ok_real(fw, me, other):
    """双向建立好友关系（幂等）。返回 True = 这次真的新加了边。"""
    e = fw['edges']
    a = e.setdefault(str(me), {})
    b = e.setdefault(str(other), {})
    new = (str(other) not in a) or (str(me) not in b)
    a[str(other)] = 1
    b[str(me)] = 1
    fw['requests'].get(str(me), {}).pop(str(other), None)
    fw['requests'].get(str(other), {}).pop(str(me), None)
    return new


def _all_others(me):
    """除我以外的所有账号（只读、不 bind）。"""
    try:
        from accounts import REGISTRY
        return [a for a in REGISTRY.all_accounts_full() if str(a.id) != str(me)]
    except Exception:
        return []


def _gift_log(fw, a, b, n):
    """赠/收流水（世界键里只留最近 200 条）：`[ts, from, to, gold]`。"""
    lg = fw.get('log')
    if not isinstance(lg, list):
        lg = fw['log'] = []
    lg.append([int(time.time()), str(a), str(b), int(n)])
    del lg[:-200]


def _real_gift(fw, kind, name):
    """2106 送 / 2107 收：返回应答字节；`None` = 该走单档旧路径。

    经济口径（**金币守恒**）：赠送时发送方 -50（托管），接收方**领取时** +50。
    每日上限 `FRIEND_DAILY_MAX`（送/收各 50 次）。
    """
    from handlers_sys import role_info_array
    me = _me_aid()
    target = _acc_by_role(name)
    if target is None:
        return b'[3]'
    other = str(target.id)
    if other not in (fw['edges'].get(str(me)) or {}):
        return b'[3]'                             # 不是好友（删好友之后就不能再赠/收）
    myg = _friend_gift_day(fw, me)
    theirg = _friend_gift_day(fw, other)
    try:
        from accounts import REGISTRY
    except Exception:
        return b'[0,{}]'
    if kind == 'sent':
        if len(myg['sent']) >= FRIEND_DAILY_MAX:
            return b'[549]'                       # 今日赠送次数已满
        if other in myg['sent']:
            return b'[548]'                       # 同一好友当天只送一次
        if int(EQ_STATE.get('gold', 0)) < FRIEND_GIFT_GOLD:
            return b'[77]'                        # 金币不足
        with REGISTRY.guild_transaction(target):
            EQ_STATE['gold'] = int(EQ_STATE.get('gold', 0)) - FRIEND_GIFT_GOLD
            myg['sent'][other] = 1
            _gift_log(fw, me, other, FRIEND_GIFT_GOLD)
            REGISTRY.save_world()
        try:
            from handlers_task import bump_task as _bt, CT_FRIEND_GIFT as _CT_FG
            _bt(_CT_FG, 1)                        # CT=43「赠送好友」的事件源
        except Exception:
            pass
        save_state()
        return ('[1,[' + role_info_array() + ']]').encode('utf-8')
    # got
    if len(myg['got']) >= FRIEND_DAILY_MAX:
        return b'[549]'                           # 今日收取次数已满
    if other in myg['got']:
        return b'[548]'                           # 今天已经收过 TA 的
    if me not in (theirg['sent'] or {}):
        return b'[547]'                           # TA 今天没有送给你
    with REGISTRY.guild_transaction(target):
        EQ_STATE['gold'] = int(EQ_STATE.get('gold', 0)) + FRIEND_GIFT_GOLD
        myg['got'][other] = 1
        # ⚠ **不能**清对方 `sent` 里的这一条：那是"我今天送过谁"的当日账，
        #   清掉之后发送方当天就能对同一好友再送一次（重复给钱）。幂等靠接收方的 `got`。
        _gift_log(fw, other, me, FRIEND_GIFT_GOLD)
        REGISTRY.save_world()
    save_state()
    return ('[1,[' + role_info_array() + ']]').encode('utf-8')


def _real_onekey(fw):
    """2109 一键收取：把所有"送过我、我还没领"的好友一次性领掉（上限同样 50）。"""
    from handlers_sys import role_info_array
    me = _me_aid()
    myg = _friend_gift_day(fw, me)
    n = 0
    try:
        from accounts import REGISTRY
    except Exception:
        return ('[1,[' + role_info_array() + ']]').encode('utf-8')
    for from_aid in list(fw['gifts'].keys()):
        if n + len(myg['got']) >= FRIEND_DAILY_MAX:
            break
        g = fw['gifts'].get(from_aid) or {}
        if str(me) not in (g.get('sent') or {}):
            continue
        if from_aid in myg['got']:
            continue
        acc = _acc_of_aid(from_aid)
        with REGISTRY.guild_transaction(acc):
            EQ_STATE['gold'] = int(EQ_STATE.get('gold', 0)) + FRIEND_GIFT_GOLD
            myg['got'][from_aid] = 1
            _gift_log(fw, from_aid, me, FRIEND_GIFT_GOLD)
            REGISTRY.save_world()
        n += 1
    if n:
        save_state()
    return ('[1,[' + role_info_array() + ']]').encode('utf-8')


def _role_name():
    # 自己的角色名 (改名 2001 会改 state.role_name; 默认"测试" 与 1092/2001 一致)
    return str(EQ_STATE.get('role_name') or '测试')


def _friend_row(name, state=1, idx=0):
    head = EQ_STATE.get('head') or {}
    h1 = int(head.get('eq', 1001) or 1001)
    now = int(time.time())
    return [1, '测试', name, 20 + (idx % 30), h1, 1,
            now - 3600 * (idx + 1), 1, now - 600 * (idx + 1),
            1 if idx % 2 == 0 else 0, 0, 1, state]


def _friend_extra_rows():
    return [list(r) for r in _friends().values() if isinstance(r, list) and len(r) >= 13]


def handle_friend_add(body_json):
    # 2101: 发送好友申请 (私服无真实社交 -> 记录到"申请中"名单, state=2 我申请)
    #   错误码 (NetFriendAdd:ctor): 2 不能加自己 / 3 该用户不存在 / 4 已添加
    #   ⚠ 空请求体**不能**回 [0,{}] —— 客户端会弹"获取网络信息失败"(误导)。
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
        req = {}
    name = str((req or {}).get('friendName') or '')
    if not name or name == '0':
        return b'[3]'                        # 该用户不存在
    if name == _role_name():
        return b'[2]'                        # 不能把自己加好友
    fw = _fw()
    if fw is not None:
        me = _me_aid()
        target = _acc_by_role(name)
        if target is None:
            return b'[3]'                    # 该用户不存在（真名池里没有）
        if str(me) in (fw['edges'].get(str(target.id)) or {}):
            return b'[4]'                    # 已经是好友
        if str(me) in (fw['requests'].get(str(target.id)) or {}):
            return b'[1,[]]'                 # 已申请过 -> 幂等 ack（不重复写）
        try:
            from accounts import REGISTRY
            with REGISTRY.guild_transaction(target):
                fw['requests'].setdefault(str(target.id), {})[str(me)] = 1
                REGISTRY.save_world()
        except Exception as _e:
            print('[FRIEND] !! 写申请失败: %r' % (_e,), flush=True)
            return b'[0,{}]'
        return b'[1,[]]'
    if name in _FRIEND_BUILTIN or name in _friends():
        return b'[4]'                        # 这个好友已经被添加了
    _friends()[name] = _friend_row(name, state=2, idx=len(_friends()))
    save_state()
    return b'[1,[]]'


def handle_friend_ok_add(body_json):
    # 2102: 审批好友申请 confrimType 2=同意 / 其他=拒绝
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
        req = {}
    name = str((req or {}).get('friendName') or '')
    try:
        ctype = int((req or {}).get('confrimType') or 2)
    except Exception:
        ctype = 2
    if not name or name == '0':
        return b'[3]'                        # 该用户不存在 (别回 [0,{}] -> 弹"获取网络信息失败")
    fw = _fw()
    if fw is not None:
        me = _me_aid()
        target = _acc_by_role(name)
        if target is None:
            return b'[3]'
        try:
            from accounts import REGISTRY
            with REGISTRY.guild_transaction(target):
                if ctype == 2:
                    _friend_ok_real(fw, me, target.id)
                else:
                    fw['requests'].get(str(me), {}).pop(str(target.id), None)
                REGISTRY.save_world()
        except Exception as _e:
            print('[FRIEND] !! 同意/拒绝失败: %r' % (_e,), flush=True)
            return b'[0,{}]'
        return b'[1,[]]'
    if ctype == 2:
        _friends()[name] = _friend_row(name, state=1, idx=len(_friends()))
    else:
        _friends().pop(name, None)
    save_state()
    return b'[1,[]]'


def handle_friend_delete(body_json):
    # 2103: 删除好友 (内置机器人不可删, 只能删自己加的)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
        req = {}
    name = str((req or {}).get('friendName') or '')
    fw = _fw()
    if fw is not None:
        me = _me_aid()
        target = _acc_by_role(name)
        if target is None:
            return b'[3]'
        try:
            from accounts import REGISTRY
            with REGISTRY.guild_transaction(target):
                fw['edges'].get(str(me), {}).pop(str(target.id), None)
                fw['edges'].get(str(target.id), {}).pop(str(me), None)
                fw['requests'].get(str(me), {}).pop(str(target.id), None)
                fw['requests'].get(str(target.id), {}).pop(str(me), None)
                REGISTRY.save_world()
        except Exception as _e:
            print('[FRIEND] !! 删除好友失败: %r' % (_e,), flush=True)
            return b'[0,{}]'
        return b'[1,[]]'
    if name in _FRIEND_BUILTIN:
        return b'[547]'
    _friends().pop(name, None)
    save_state()
    return b'[1,[]]'


def handle_friend_rcmds(body_json=None):
    # 2104: 推荐好友列表 -> msg[1] = 13 字段行列表 (pairs, 无守卫)
    fw = _fw()
    if fw is not None:
        me = _me_aid()
        myname = _role_name()
        rows = []
        for acc in _all_others(me):
            oid = str(acc.id)
            if oid in (fw['edges'].get(str(me)) or {}):
                continue
            if oid in (fw['requests'].get(str(me)) or {}):       # 我发给 TA 的
                continue
            if str(me) in (fw['requests'].get(oid) or {}):       # TA 发给我的
                continue
            rows.append(_frow(acc, myname, 0, fw))
        rows.sort(key=lambda r: (-r[9], r[2]))                   # 在线优先
        return ('[1,' + json.dumps([rows], separators=(',', ':')) + ']').encode('utf-8')
    rows = []
    for i, nm in enumerate(_FRIEND_POOL[:5]):
        if nm in _friends():
            continue
        rows.append(_friend_row(nm, state=0, idx=i + 3))
    return ('[1,' + json.dumps([rows], separators=(',', ':')) + ']').encode('utf-8')


def handle_friend_update(body_json=None):
    # 2105: 刷新 (recvMessage 只回调)
    return b'[1,[]]'


def _friend_coin(body_json, key):
    # 2106 送金币 / 2107 收金币 / 2109 一键收取: 统一回 [roleInfo(30字段)]
    from handlers_sys import role_info_array
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
        req = {}
    name = str((req or {}).get('friendName') or '')
    fw = _fw()
    if fw is not None:
        return _real_gift(fw, key, name)
    # 每日送收次数 (NetFriendList:getCoinGold 缺省 50) 与"该好友是否已处理"记在 friends_coin
    fc = EQ_STATE.setdefault('friends_coin', {})
    today = time.strftime('%Y-%m-%d')
    if fc.get('day') != today:
        fc.clear()
        fc['day'] = today
        fc['sent'] = {}
        fc['got'] = {}
    slot = fc.setdefault(key, {})
    if key == 'sent':
        # 送金币: 扣 50 铜钱/次 (与 getCoinGold 缺省 50 对齐), 同一好友当天只送一次
        if name:
            if name in slot:
                return b'[548]'
            cost = 50
            if int(EQ_STATE.get('gold', 0)) < cost:
                return b'[77]'
            EQ_STATE['gold'] = int(EQ_STATE['gold']) - cost
            slot[name] = 1
            # ★★2026-10-17（任务链收口）：CT=43「赠送好友体力」的事件源 = 2106 赠送成功。
            #   A07 10513（qty 20）此前永远 0 进度。口径：一次成功赠送记 1（同一好友当天
            #   只送一次 —— 上面的 `if name in slot: return [548]` 已经挡掉重复请求）。
            try:
                from handlers_task import bump_task as _bt, CT_FRIEND_GIFT as _CT_FG
                _bt(_CT_FG, 1)
            except Exception as _e:
                print('[FRIEND] !! bump CT=43 失败(不影响赠送本身): %r' % (_e,), flush=True)
    else:
        # 收金币: 每个好友当天只收一次, 得 50 铜钱
        if name and name not in slot:
            slot[name] = 1
            EQ_STATE['gold'] = int(EQ_STATE.get('gold', 0)) + 50
    save_state()
    return ('[1,[' + role_info_array() + ']]').encode('utf-8')


def handle_friend_send(body_json):
    # 2106: recv msg[1]=roleInfo(30字段) -> gamer_setRoleInfo
    return _friend_coin(body_json, 'sent')


def handle_friend_recv(body_json):
    # 2107: 同上
    return _friend_coin(body_json, 'got')


def handle_friend_onekey(body_json=None):
    # 2109: 一键收取全部好友金币 -> msg[1]=roleInfo(30字段)
    from handlers_sys import role_info_array
    fw = _fw()
    if fw is not None:
        return _real_onekey(fw)
    fc = EQ_STATE.setdefault('friends_coin', {})
    today = time.strftime('%Y-%m-%d')
    if fc.get('day') != today:
        fc.clear()
        fc['day'] = today
        fc['sent'] = {}
        fc['got'] = {}
    got = fc.setdefault('got', {})
    names = list(_FRIEND_BUILTIN) + list(_friends().keys())
    n = 0
    for nm in names:
        if nm not in got:
            got[nm] = 1
            n += 1
    if n:
        EQ_STATE['gold'] = int(EQ_STATE.get('gold', 0)) + 50 * n
        save_state()
    return ('[1,[' + role_info_array() + ']]').encode('utf-8')


def handle_friend_search(body_json=None):
    # 2108: 搜索好友 -> msg[1..5] = serverId/roleName/roleLevel/roleHeadID/isOnline (全部无守卫)
    fw = _fw()
    if fw is not None:
        me = _me_aid()
        cand = _all_others(me)
        cand.sort(key=lambda a: (0 if _is_online(a.id) else 1, str(a.id)))
        if cand:
            a = cand[0]
            try:
                from handlers_rank import _acc_head
                h = _acc_head(a)
            except Exception:
                h = 1001
            try:
                lv = int((a.data or {}).get('player_lv') or 1)
            except Exception:
                lv = 1
            return ('[1,' + json.dumps(
                [1, _acc_name_of(a), lv, h, 1 if _is_online(a.id) else 0],
                separators=(',', ':')) + ']').encode('utf-8')
    head = EQ_STATE.get('head') or {}
    h1 = int(head.get('eq', 1001) or 1001)
    nm = _FRIEND_POOL[0]
    return ('[1,' + json.dumps([1, nm, 30, h1, 1], separators=(',', ':')) + ']').encode('utf-8')

# T35 头像: id -> [CostType, Cost, Unlocker(str'|'分段), UnlockStatus, UnlockType]
#   客户端契约 (ControllerHead.lua L27-31 consta_unlockState + NetHeadInfo.lua L41 注释):
#     0=已解锁(含怪族解锁)  1=已购买  2=未解锁
#   Unlocker 语义 (T35 数据实证 2026-09-07 第四轮):
#     UnlockType=0 (常规 97 行): Unlocker = 怪物族全形态 type id 枚举, 如 100031|100032|...|100036 (哪吒),
#       逐段精确匹配玩家已拥有怪物的 L91 type id (初始3只/抽到/进化后 6003 全覆盖).
#     UnlockType=1 (SPEC 21 行): Unlocker = 5 位家族基址, 如 30062 -> 家族成员形如 30062x (300621-300626);
#       按 基址*10+1..9 匹配. UnlockStatus=1 的行 (1039 UnlockType=9) 视为家族达成即"已拥有".
_HEAD = {}
try:
    _t = _load_json('T35_HeadImage.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _HEAD[int(_r[_h['ID']])] = (int(_r[_h['CostType']] or 0), int(_r[_h['Cost']] or 0),
                                        str(_r[_h['Unlocker']] or ''),
                                        int(_r[_h['UnlockStatus']] or 0),
                                        int(_r[_h['UnlockType']] or 0))
        except Exception:
            continue
except Exception:
    pass
# T36 头像框
_FRAME = set()
try:
    _t = _load_json('T36_HeadFrame.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _FRAME.add(int(_r[_h['ID']]))
        except Exception:
            continue
except Exception:
    pass
# L103 宝箱: id -> (ArrayID, CurrencyType, CurrencyQuantity)
_BOX = {}
try:
    _t = _load_json('L103_TreasureBox.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _BOX[int(_r[_h['ID']])] = (int(_r[_h['ArrayID']] or 0), int(_r[_h['CurrencyType']] or 1),
                                       int(_r[_h['CurrencyQuantity']] or 0))
        except Exception:
            continue
except Exception:
    pass
# L103_TreasureBoxAward: ArrayID -> [(type, obj, qty, rate)]
_AWARD = {}
try:
    _t = _load_json('L103_TreasureBoxAward.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _AWARD.setdefault(int(_r[_h['ArrayID']] or 0), []).append(
                (int(_r[_h['Type']] or 0), int(_r[_h['ObjectID']] or 0),
                 int(_r[_h['Quantity']] or 0), int(_r[_h['Rate']] or 1)))
        except Exception:
            continue
except Exception:
    pass

DEFAULT_HEAD = 1001
DEFAULT_FRAME = 1001

def _hd():
    return EQ_STATE.setdefault('head', {'own': {DEFAULT_HEAD: 1}, 'eq': DEFAULT_HEAD,
                                        'frame_eq': DEFAULT_FRAME, 'frame_own': {DEFAULT_FRAME: 1}})

def _owned_mids():
    # 玩家当前拥有的怪物 type id 集合 (初始3只/抽到/进化覆盖; 已吞噬移除的不含)
    #   依据 state: all_monster_ucodes + get_monster_base (6003 进化后的 mid 已含)
    mids = set()
    try:
        from state import all_monster_ucodes, get_monster_base
        for uc in all_monster_ucodes():
            b = get_monster_base(uc)
            if b:
                mids.add(str(int(b[0])))
    except Exception:
        pass
    return mids

def _head_family_unlocked(hid):
    # T35 Unlocker 匹配: 任一已拥有怪物 type id 命中族段 -> 解锁
    #   段规则: 6+位 = 精确 type id; 5位 = 家族基址 -> 基址*10+1..9 (SPEC 行, 如 30062 -> 300621-300626)
    info = _HEAD.get(hid)
    if info is None:
        return False
    unlocker = info[2]
    if not unlocker:
        return False
    mid_set = _owned_mids()
    if not mid_set:
        return False
    for tok in str(unlocker).split('|'):
        tok = tok.strip()
        if not tok:
            continue
        if len(tok) >= 6:
            if tok in mid_set:
                return True
        else:
            if not tok.isdigit():
                continue
            base = int(tok)
            for k in range(1, 10):
                if str(base * 10 + k) in mid_set:
                    return True
    return False

def _head_state(hid):
    # 0=已解锁(含怪族) 1=已购买 2=未解锁; 1039(UnlockType=9, UnlockStatus=1)家族达成即视为已拥有
    if int(hid) in _hd()['own']:
        return 1
    if _head_family_unlocked(hid):
        info = _HEAD.get(int(hid))
        return 1 if (info and int(info[3]) == 1) else 0
    return 2

def _role_info():
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

def handle_head_info(body_json=None):
    # resp msg[1]=_listHeadInfo (NetHeadInfo:recvMessage) -> 响应必须 [1,[rows]]
    #   三段式: 0=已解锁(含怪族) 1=已购买 2=未解锁 (NetHeadInfo.lua L41 / ControllerHead.lua L27)
    rows = []
    for hid in sorted(_HEAD):
        rows.append([hid, _head_state(hid)])
    return ('[1,[' + json.dumps(rows, separators=(',', ':')) + ']]').encode('utf-8')

def handle_head_buy(body_json):
    # 1093: 客户端 NetHeadBuy m_errorCode: 569 头像id错误 / 61 金币不足 / 60 妖魂不足 /
    #   570 您尚未解锁此头像无法购买 / 571 您已购买此头像 (2026-09-07 第三轮: 旧实现统一 [0] ->
    #   客户端提示 "获取数据失败" 且已购头像重复购买也应提示 571 而非失败)
    try:
        req = json.loads(body_json) if body_json else {}
        if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
            req = {}
        hid = int(req.get('id') or 0)
    except Exception:
        return b'[569]'
    info = _HEAD.get(hid)
    if info is None:
        return b'[569]'
    cost_type, cost = info[0], info[1]
    if hid in _hd()['own']:
        return b'[571]'                       # 您已购买此头像
    if not _head_family_unlocked(hid):
        return b'[570]'                       # 您尚未解锁此头像，无法购买
    if cost_type == 0 and int(EQ_STATE.get('gold', 0)) < cost:
        return b'[61]'                        # 金币不足
    if cost_type == 1 and int(EQ_STATE.get('soulcoin', 0)) < cost:
        return b'[60]'                        # 妖魂不足
    if cost_type == 0:
        EQ_STATE['gold'] = int(EQ_STATE.get('gold', 0)) - cost
    elif cost_type == 1:
        EQ_STATE['soulcoin'] = int(EQ_STATE.get('soulcoin', 0)) - cost
    _hd()['own'][hid] = 1
    save_state()
    return json.dumps([1, [_role_info()]], separators=(',', ':')).encode('utf-8')

def handle_head_save(body_json):
    # 1095: 客户端 NetHeadSave m_errorCode: 569 id错 / 572 未解锁或未购买该头像，无法保存
    try:
        req = json.loads(body_json) if body_json else {}
        if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
            req = {}
        hid = int(req.get('imgid') or 0)
    except Exception:
        return b'[0,{}]'
    if hid not in _HEAD:
        return b'[569]'
    if hid not in _hd()['own'] and not _head_family_unlocked(hid):
        return b'[572]'
    _hd()['eq'] = hid
    save_state()
    return json.dumps([1, [_role_info()]], separators=(',', ':')).encode('utf-8')

def handle_head_frame_info(body_json=None):
    rows = []
    for fid in sorted(_FRAME):
        st = 0 if fid in _hd()['frame_own'] else 2
        rows.append([fid, st])
    return ('[1,[' + json.dumps(rows, separators=(',', ':')) + ']]').encode('utf-8')

def handle_head_frame_save(body_json):
    # 1096: 客户端 NetHeadFrameSave m_errorCode: 573 头像边框id错误 / 574 您尚未拥有该边框，无法保存
    try:
        req = json.loads(body_json) if body_json else {}
        if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
            req = {}
        fid = int(req.get('imgframeid') or 0)
    except Exception:
        return b'[573]'
    if fid not in _FRAME:
        return b'[573]'
    if fid not in _hd()['frame_own']:
        return b'[574]'
    _hd()['frame_eq'] = fid
    save_state()
    return json.dumps([1, [_role_info()]], separators=(',', ':')).encode('utf-8')

# ============ 宝箱 ============
# 宝箱在独立容器 state.treasure_bag (L103 箱 id 与 L02 道具 id 同号, 不能放 prop)
def _owned_boxes():
    items = []
    for uc, v in (EQ_STATE.get('treasure_bag') or {}).items():
        bid, cnt = v[0], int(v[1])
        if cnt > 0:
            items.append([bid, uc, cnt])
    return items

def handle_treasure_get(body_json=None):
    # 1060: 宝箱列表; NetTreasureGet:recvMessage for _,config in pairs(msg[1]) -> [1,[rows]]
    return ('[1,[' + json.dumps(_owned_boxes(), separators=(',', ':')) + ']]').encode('utf-8')

def handle_treasure_buy(body_json):
    # 1061: 客户端 NetTreasureBuy m_errorCode: 549 id错/550 数量错/551 不可购买/552 不存在/60 妖魂不足
    try:
        req = json.loads(body_json) if body_json else {}
        if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
            req = {}
        sid = int(req.get('staticid') or 0)
        count = int(req.get('count') or 1)
    except Exception:
        return b'[0,{}]'
    if count <= 0:
        return b'[550]'
    info = _BOX.get(sid)
    if info is None:
        return b'[552]'
    _, _, cost = info
    total = cost * count
    # 客户端错误码 [60]="妖魂不足" -> 按妖魂扣费 (L103 CurrencyQuantity; 旧实现误用 kidou)
    if total > int(EQ_STATE.get('soulcoin', 0)):
        return b'[60]'
    EQ_STATE['soulcoin'] = int(EQ_STATE.get('soulcoin', 0)) - total
    # ★v32: 统一走 state.add_box (已由上面的 _BOX 校验过, 这里再兜一层 L103 校验, 保持唯一入包入口)
    from state import add_box as _addbox
    _addbox(sid, count)
    save_state()
    # resp [1,[roleInfo, [66(fromid 购买), [[18,[[sid,count]]]]]]]  客户端 boxdata=[18,[[sid,count]]]
    data = [_role_info(), [66, [[18, [[sid, count]]]]]]
    return json.dumps([1, data], separators=(',', ':')).encode('utf-8')

def handle_treasure_achive(body_json):
    # 1062: 开启宝箱 -> resp [1,[roleInfo, dropInfo, isLimit(0)]]
    from handlers_game import _grant_content_row
    try:
        req = json.loads(body_json) if body_json else {}
        if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
            req = {}
        uc = str(req.get('ucode') or '')
    except Exception:
        return b'[0,{}]'
    tb = EQ_STATE.setdefault('treasure_bag', {})
    v = tb.get(uc)
    if v is None or int(v[1]) <= 0:
        return b'[0,{}]'
    pid = int(v[0])
    info = _BOX.get(pid)
    if info is None:
        return b'[0,{}]'
    array_id = info[0]
    # 权重开箱: 客户端 VTreasureHome:openBoxEffect 按 5 个奖励槽渲染
    #   (rewardArray[1..5] 都存在才拼 "获得A,B,C,D,E"), 官方每箱开 5 次独立加权抽取
    awards = _AWARD.get(array_id)
    drop_groups = []
    if awards:
        total = sum(a[3] for a in awards)
        for _ in range(5):
            roll = random.randrange(total) if total > 0 else 0
            picked = awards[-1]
            for a in awards:
                if roll < a[3]:
                    picked = a
                    break
                roll -= a[3]
            ty, obj, qty, _ = picked
            # 完整类型发放 (装备全L01/道具/元灵/皮肤等; 旧实现仅道具/铜钱/妖魂)
            drop_groups.extend(_grant_content_row(ty, str(obj), int(qty)) or [[ty, [[qty]]]])
    v[1] = int(v[1]) - 1
    if int(v[1]) <= 0:
        tb.pop(uc, None)
    # ★★2026-10-17（任务链收口）：CT=12「鉴定宝箱」的事件源 = 1062 开启宝箱（鉴定系统
    #   `VTreasureHome.lua:503` 开箱 → 1062；1061 只是"买/鉴定入包"）。
    #   A07 10315/10515/10712 与 T37「鉴定」此前永远 0 进度。口径：开一个箱子记 1。
    #   ⚠ 口径存疑（"鉴定"也可解释成 1061 买箱），但两者都在同一个鉴定系统内，
    #     真机可一次开箱验证；若将来对不上，改挂 1061 即可（本段注释是唯一判据来源）。
    try:
        from handlers_task import bump_task as _bt12, CT_TREASURE as _CT_TREASURE
        _bt12(_CT_TREASURE, 1)
    except Exception as _e:
        print('[TREASURE] !! bump CT=12 失败(不影响开箱本身): %r' % (_e,), flush=True)
    save_state()
    return json.dumps([1, [_role_info(), [[], drop_groups], 0]], separators=(',', ':')).encode('utf-8')
