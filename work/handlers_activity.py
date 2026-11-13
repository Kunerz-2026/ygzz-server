# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 活动/签到/开关/夜市/妖主入侵 (2026-09-08 按客户端源码契约逐一对齐)
#   7013 ACTIVITY_LIST   -> [1,[ [活动...] ]] ; 客户端 NetActivityList: msg[1] 遍历, 第1个=sequenceLoginData(8日登录)
#                           每个活动 [] = [ID, begin, over, countdown(秒), finish(0全领/1进行中), contentlist]
#                           contentlist 每行 = [A01_1_ID, group, finishvalve, state] (0未完成 1未领 2已领)
#   7014 ACTIVITY_RECEIVE req {taskid=A01_1_ID} -> [1,[roleInfo, dropInfo]]
#   7016 EIGHT_LOGIN_BUTTON -> [1,[isVisible]] (isVisible=true => 客户端隐藏按钮: MMainButton isVisible = not v)
#   7100 SIGNIN_INFO  -> [1,[good, noGood, loginedDays, round, dayList]] dayList=[[A21_ID, state]..] 30行
#       state: 0未解锁 1已领 2可领 3明日标记(客户端默认选中上一日; else 分支=锁定显示)
#   7101 SIGNIN_GET  req {id=当日索引1..30} -> [1,[roleInfo, dropInfo]]
#   106  PUSH_SWITCH req {area} -> [1,[[被关闭系统tag列表]]] (空=全开; 客户端 switchData[tag]~=nil 则隐藏)
#   1108 OPERATION_ACTIVITY_LIST -> [1,[[ID, Activityype, Title, Starttime, Enttime, Priority]..]]
#       Activityype: 2=妖主大入侵按钮 9=夜市按钮 16=限时任务按钮 (MMainButton:refreshInvadeStage)
#   1161 NIGHTMARKET_INFO -> [1,[markettoken, jointimes, starttime, endtime, daybuytimes, fubentime]]
#   1163 BUYJOINNUM -> [1,[soul, times, daybuytimes]]
#   1164 JOIN -> [1,[]] ack (扣1进入次数)
#   1162 SENDFOOD req {id} -> [1,[removeProp([uc,id,count]), token]]
#   1165 SHOP_INFO -> [1,[[shopItemId, buyCount]..]]
#   1166 SHOP_BUY req {shopItemId, count} -> [1,[roleInfo, dropInfo, num]]
#   1167 TOKEN_PUSH -> [1,[token]]
#   1102 INVADE -> [1,[time, daydamage, damage]]  9008 INVADE_RESULT -> [1,[roleInfo, formation, dropInfo]]
import json, os, time, random
from state import EQ_STATE, save_state, vitality_timerid, load_table

from state import table_dir as _table_dir   # ★2026-10-02 (审计 §1.6): 表根不再写死盘符
_DATA = _table_dir()

def _load_json(name):
    # ★v60 闭环 v58 §10 缺口 #3: 改走 state.load_table —— 按「尾字段 u32 长度」精确剥离,
    #   不再用 c[:c.rfind(']')+1]（长度字段含 0x5D 时会切错 -> 调用方 except -> 静默空表,
    #   实测 L98_AIPriority / T32_Achievement）。护栏: work/_v60_legacy_tail_guard_test.py
    #   ⚠ 必须**逐行复制**: load_table 的结果是**带缓存的同一个对象**, 而旧实现每次返回全新对象。
    #     调用方若原地改行(排序/填字段)会污染全局缓存 -> 这里保持"每次都是新对象"的原语义。
    return [list(_r) for _r in load_table(name, _DATA)]

# ============ A01_Activity / A01_1 活动内容 (8日登录 = activity 101; 内容组 101, 目标1..8) ============
_A01 = []          # (id, begin, over)
_A01_1 = {}        # group -> [(contentId, target, rewardGroupId)]
try:
    _t = _load_json('A01_Activity.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _A01.append((int(_r[_h['ID']]), str(_r[_h['Begin']] or ''), str(_r[_h['Over']] or '')))
        except Exception:
            continue
except Exception:
    pass
try:
    _t = _load_json('A01_1_Activitycontent.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            g = str(_r[_h['Group']])
            _A01_1.setdefault(g, []).append((int(_r[_h['ID']]), int(_r[_h['Target']] or 0),
                                             str(_r[_h['Reward']] or '0')))
        except Exception:
            continue
    for _k in _A01_1:
        _A01_1[_k].sort()
except Exception:
    pass

# ============ A21 30日签到 / A22 宜忌 ============
_A21 = []          # (globalId, round, day, itemType, itemId, itemNum)
_A21_BY_ROUND = {}  # round -> [(globalId, day, itemType, itemId, itemNum)]
try:
    _t = _load_json('A21_30DayLoginReward.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _row = (int(_r[_h['ID']]), int(_r[_h['Round']] or 0), int(_r[_h['Day']] or 0),
                    int(_r[_h['ItemType']] or 0), str(_r[_h['ItemID']] or '0'),
                    int(_r[_h['ItemNum']] or 0))
            if _row[2] == 0:
                continue
            _A21.append(_row)
            _A21_BY_ROUND.setdefault(_row[1], []).append(_row)
        except Exception:
            continue
except Exception:
    pass
for _k in _A21_BY_ROUND:
    _A21_BY_ROUND[_k].sort(key=lambda x: x[2])
_A22_BY_TYPE = {}  # type -> [ids] (宜=1, 忌=2)
try:
    _t = _load_json('A22_30DayLoginLuck.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _A22_BY_TYPE.setdefault(str(_r[_h['Type']]), []).append(int(_r[_h['ID']]))
        except Exception:
            continue
except Exception:
    pass

# ============ A06_1 夜市商店 (17 行) ============
_A06 = []          # (id, goodsType, goodsId, goodsCount, limitType, limitCoun, priceType, price)
try:
    _t = _load_json('A06_1_ActivityMarketShop.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _A06.append((str(_r[_h['ID']]), int(_r[_h['GoodsType']] or 0), str(_r[_h['GoodsId']] or '0'),
                         int(_r[_h['GoodsCount']] or 1), int(_r[_h['LimitType']] or 0),
                         int(_r[_h['LimitCoun']] or 0), int(_r[_h['PriceType']] or 0),
                         int(_r[_h['Price']] or 0)))
        except Exception:
            continue
except Exception:
    pass

# ============ A05 妖主入侵 (MstrGroupID: 每日时段 000000-005959) ============
_A05_INTR = []
try:
    _t = _load_json('A05_ActivityMasterIntrusio.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _A05_INTR.append((str(_r[_h['ID']]), str(_r[_h['Begin']] or '000000'),
                              str(_r[_h['Over']] or '235959'), int(_r[_h['MonsterID']] or 0),
                              str(_r[_h['MstrGroupID']] or '')))
        except Exception:
            continue
except Exception:
    pass

NIGHTMARKET_START_H = 18   # 官方 18:00 开放 (VMainButton 注释"夜市显示由服务器时间控制")
NIGHTMARKET_ALWAYS_OPEN = True   # 私服: True=全天可用; False=18:00-23:59
NIGHTMARKET_JOIN_DAILY = 2       # 每日可进入次数
NIGHTMARKET_BUY_PRICE = 100      # 妖魂/加1次进入
NIGHTMARKET_SEED_JOIN = 1        # 新号进入次数 (测试方便)

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

def _drop(groups):
    return [[], list(groups or [])]

def _json_out(data):
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============ 内部状态 ============
def _login7():
    # 8日登录: days=累计登录日数 (每日+1), claimed=已领内容日数 (当前日每日可领 1 次)
    st = EQ_STATE.setdefault('login7', {'days': 0, 'claimed': 0, 'last': ''})
    today = time.strftime('%Y-%m-%d')
    if st.get('last') != today:
        # 新的一天: 登录日+1 (私服: 在线视为登录; 每天首次访问时递增)
        st['days'] = int(st.get('days', 0)) + 1
        st['last'] = today
        save_state()
    return st

def _signin30():
    st = EQ_STATE.setdefault('signin30', {'days': 0, 'claimed': [], 'last': ''})
    today = time.strftime('%Y-%m-%d')
    if st.get('last') != today:
        st['days'] = int(st.get('days', 0)) + 1
        st['last'] = today
        save_state()
    if not isinstance(st.get('claimed'), list):
        st['claimed'] = []
    return st

# ============ 7013 活动列表 (8日登录 = 第1个 activity) ============
def _act_content_state(day, cur, claimed):
    # day=1..8, cur=当前登录日(1..8), claimed=已领的当前日
    if day < cur:
        return 2
    if day == cur:
        return 1 if claimed < day else 2
    return 0

def handle_activity_list(body_json=None):
    # [1,[ [ID,begin,over,countdown,finish,[[cId,group,finishvalve,state]..]].. ]] (仅 8日登录 一个活动)
    try:
        import datetime as _dt
        _now = _dt.datetime.now()
        _mid = (_now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        _cd = int((_mid - _now).total_seconds())
    except Exception:
        _cd = 86400
    st = _login7()
    cur = max(1, min(8, int(st.get('days', 1))))
    claimed = int(st.get('claimed', 0))
    rows = _A01_1.get('101', [])
    if not rows:
        return _json_out([])
    content = []
    for (cid, target, reward) in rows:
        content.append([cid, 101, cur, _act_content_state(target, cur, claimed)])
    finish = 0 if (cur >= 8 and claimed >= 8) else 1
    activity = [101, '20180810000001', '20291231235959', _cd, finish, content]
    return _json_out([[activity]])   # msg[1]=活动列表 (NetActivityList: for i=1,#msg[1])

def handle_activity_receive(body_json):
    # 7014 req {taskid=A01_1_ID} -> [1,[roleInfo, dropInfo]] (码 543 为参考, 沿用 0/1 状态)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        cid = int(req.get('taskid') or 0)
    except Exception:
        return b'[543]'
    st = _login7()
    cur = max(1, min(8, int(st.get('days', 1))))
    claimed = int(st.get('claimed', 0))
    rows = _A01_1.get('101', [])
    # 仅允许领"当前登录日" (官方 7日/8日连续登录: 每日一领, 补签无)
    if claimed >= 8:
        return b'[543]'
    target = None
    reward_gid = None
    for (cid2, target2, gid2) in rows:
        if cid2 == cid:
            target, reward_gid = target2, gid2
            break
    if target is None:
        return b'[543]'
    if target > cur:
        return b'[543]'                      # 未到该登录日
    if target <= claimed:
        return b'[543]'                      # 已领 (按日序)
    from handlers_task import _grant_group
    groups, ok = _grant_group(reward_gid)
    if not ok:
        return b'[543]'
    st['claimed'] = max(claimed, target)
    save_state()
    return _json_out([_role_info(), _drop(groups)])

def handle_button_visible(body_json=None):
    # 7016: isVisible=true -> 客户端隐藏 8日登录按钮 (MMainButton setEightCallFunc)
    st = _login7()
    hide = 1 if int(st.get('days', 0)) >= 8 else 0
    return _json_out([hide])

# ============ 7100/7101 30日签到 ============
def _signin_day_state(d, day_idx, claimed_set, round_n):
    if d < day_idx:
        return 1 if (round_n, d) in claimed_set else 2
    if d == day_idx:
        return 1 if (round_n, d) in claimed_set else 2
    if d == day_idx + 1:
        return 3
    return 0

def handle_signin_info(body_json=None):
    st = _signin30()
    days = int(st.get('days', 0))
    claimed = set()
    for gid in st.get('claimed', []):
        for (g, r, d, it, iid, num) in _A21:
            if g == int(gid):
                claimed.add((r, d))
                break
    if days <= 0:
        good, noGood, round_n, day_idx, day_list = 1, 72, 0, 0, []
    else:
        round_n = (days - 1) // 30
        day_idx = (days - 1) % 30 + 1
        rows = _A21_BY_ROUND.get(round_n, _A21_BY_ROUND.get(0, []))
        day_list = []
        for (gid, r, d, it, iid, num) in rows:
            day_list.append([gid, _signin_day_state(d, day_idx, claimed, round_n)])
        # 宜/忌: A22 按日轮转 (type1 宜, type2 忌; 文本仅供展示)
        li = _A22_BY_TYPE.get('1') or [1]
        lj = _A22_BY_TYPE.get('2') or [72]
        good = li[(days - 1) % len(li)]
        noGood = lj[(days - 1) % len(lj)]
    return _json_out([good, noGood, days, round_n, day_list])

def _grant_a21_row(it, iid, num):
    # A21 ItemType -> 入账 (与客户端 IconData:create(ItemID, ItemType) 一致)
    it, num = int(it), int(num or 0)
    groups = []
    if it == 3:                                   # 铜钱
        EQ_STATE['gold'] = int(EQ_STATE.get('gold', 0)) + num
        groups.append([3, [[num]]])
    elif it == 7:                                 # 妖魂
        EQ_STATE['soulcoin'] = int(EQ_STATE.get('soulcoin', 0)) + num
        groups.append([7, [[num]]])
    elif it == 6:                                 # 鬼道积分
        EQ_STATE['kidou'] = int(EQ_STATE.get('kidou', 0)) + num
        groups.append([6, [[num]]])
    elif it == 8:                                 # 唤妖符
        EQ_STATE.setdefault('draw', {})['ticket'] = int(EQ_STATE.get('draw', {}).get('ticket', 0)) + num
        groups.append([8, [[num]]])
    elif it == 9:                                 # 体力
        EQ_STATE.setdefault('prop', {})['1006'] = min(20, int(EQ_STATE.get('prop', {}).get('1006', 0)) + num)
        groups.append([9, [[num]]])
    elif it == 2:                                 # 道具
        uc = str(iid)
        EQ_STATE.setdefault('prop', {})[uc] = int(EQ_STATE.get('prop', {}).get(uc, 0)) + num
        EQ_STATE.setdefault('prop_id', {})[uc] = int(iid)
        groups.append([2, [[uc, int(iid), num]]])
    elif it == 18:                                # 宝箱 (L103 箱, 进入 treasure_bag)
        # ★v32: 走 state.add_box (校验 id ∈ L103)。理由同上: 1060 裸下发 treasure_bag,
        #   表外 id 会让客户端渲染箱子时取 nil -> 崩。
        from state import add_box as _addbox
        _uc2, _tot = _addbox(iid, num)
        if _uc2 is not None:
            groups.append([18, [[int(iid), num]]])
    else:                                         # 其他: 仅展示
        groups.append([it, [[num]]])
    return groups

def handle_signin_get(body_json):
    # 7101 req {id = 当前轮内索引 1..30} -> [1,[roleInfo, dropInfo]]
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        idx = int(req.get('id') or 0)
    except Exception:
        return b'[0,{}]'
    st = _signin30()
    days = int(st.get('days', 0))
    if days <= 0:
        return b'[0,{}]'
    round_n = (days - 1) // 30
    day_idx = (days - 1) % 30 + 1
    if idx < 1 or idx > day_idx:
        return b'[0,{}]'                          # 未解锁
    rows = _A21_BY_ROUND.get(round_n, _A21_BY_ROUND.get(0, []))
    row = None
    for (gid, r, d, it, iid, num) in rows:
        if d == idx:
            row = (gid, it, iid, num)
            break
    if row is None:
        return b'[0,{}]'
    gid, it, iid, num = row
    claimed = st.setdefault('claimed', [])
    if gid in claimed:
        return b'[0,{}]'
    groups = _grant_a21_row(it, iid, num)
    claimed.append(gid)
    save_state()
    return _json_out([_role_info(), _drop(groups)])

# ============ 106 PUSH_SWITCH / 1108 运营活动 ============
def handle_push_switch(body_json=None):
    # 106: msg[1] 是被关闭系统的数字 tag 列表；目标客户端 LIMIT_GIFT_ACT=133。
    # TipsManager:showLimitGiftAct 据此阻止已决不做的充值礼包自动弹出。
    return _json_out([[133]])

def handle_operation_activity_list(body_json=None):
    # 1108: [[ID, Activityype, Title, Starttime, Enttime, Priority]..] — 类型 2 妖主入侵 / 9 夜市 / 16 限时任务 / 17 卡池BOSS / 19 烟花大会
    #
    #   客户端版本不同，活动过滤集不同；本实现按目标版本保留类型 19。
    #     · 少回 19 的直接后果：主界面**「烟花大会」按钮不出现**，而 `NetOperationActivityList.lua:41-44`
    #       **只在 `Activityype==19` 时才 `FwsData:setActID(data.ID)`** ⇒ 烟花那 5 个号
    #       （1192~1197）即使硬把界面打开，请求体也是空 `{}`。⇒ 8 个号（1190/1192~1197/1300）
    #       **客户端一次都不会发**，服务端实现它们等于白做。
    #     · 1.31 客户端的约束（`client Lua/app/main/VMainButton.lua:343-396`）是
    #       `ipairs` 的**下标 i** 去取 `activityPosData[i]`，而它只填了 4 槽
    #       ⇒ **给它 5 条会 nil 下标崩**。实测它对本列表是**类型白名单**（只认 2/17/9/16），
    #       多出来的 19 落到第 5 个下标、不匹配任何 `if` ⇒ **不会被读**。
    #       所以本函数**必须把 2/9/16/17 放前四位**（现有顺序已满足），把 19 追加在最后。
    #       ⚠ 若哪天真的要发 1.31 端，就先按包分流（照 1029 的先例），别直接照抄这里的 5 条。
    #     · 顺序/位次仍有硬要求：① 前四条必须是 2,9,16,17（否则 1.31 的槽位错位）；
    #       ② 必须给到第 4 个（17），否则卡池BOSS 按钮不亮。
    now = int(time.time())
    end = now + 365 * 86400
    rows = [
        [2002, 2, '妖主大入侵', now, end, 10],      # slot1 -> Button_YaoZhu（1.31 的 activityPosData[1]）
        [2009, 9, '夜市', now, end, 9],             # slot2 -> Button_YeShi1（1.31 的 [2]）
        [2016, 16, '限时任务', now, end, 8],        # slot3 -> Button_BaiGui（1.31 的 [3]）
        [2017, 17, '卡池BOSS', now, end, 7],        # slot4 -> Button_KaChi（1.31 的 [4]；已实装 8003-8006）
        # ★第 5 条：**只有 1.30 认它**（1.31 的类型白名单会跳过，不会去取 activityPosData[5]）。
        #   它同时是"烟花大会"页签与 `FwsData.actID` 的来源 —— 少了它，1192~1197 永远不会被发出。
        [2019, 19, '烟花大会', now, end, 6],        # 1.30: 大活动条 + 活动页 19 号页签
    ]
    # ★★2026-11-07（活动卡池轮换）：追加「卡池活动」页签（Activityype=6）。
    #   · 客户端 `NoticeActivity:updateItem` 按 `refreshActivity_<type>` 派发，6 = 卡池活动（1101）。
    #   · **必须放列表末位**：客户端 `initData:78-82` 在 pairs 里 `table.remove` 掉 type=17，
    #     删中间元素会让 `next` 跳过紧随其后的那个元素；被删的若是末位则谁都不会被跳过。
    #   · priority 取 5（最小）：客户端按 Priority 降序排序，前四位仍是 2/9/16/17。
    #   · 轮换关闭时**不加这一行**（空页签不如没有）。
    try:
        from cardpool_rotation import current as _rot_current
        _rot = _rot_current()
    except Exception as _e:
        _rot = None
        print('[1108] !! 卡池活动行读取失败: %r' % (_e,))
    if _rot:
        rows.append([2006, 6, '卡池活动', now, int(_rot.get('end') or end), 5])
    return _json_out([rows])   # msg[1]=活动列表 (NetOperationActivityList: for _,config in pairs(msg[1]))

# ============ 夜市 1161-1167 ============
def _nm():
    return EQ_STATE.setdefault('nightmarket', {'token': 0, 'buy_date': '', 'buy': 0,
                                               'join_date': '', 'join': 0})

def _nm_reset():
    nm = _nm()
    today = time.strftime('%Y-%m-%d')
    if nm.get('join_date') != today:
        nm['join_date'] = today
        nm['join'] = NIGHTMARKET_SEED_JOIN
    if nm.get('buy_date') != today:
        nm['buy_date'] = today
        nm['buy'] = 0
    return nm

def _nm_window():
    # 窗口语义: 客户端 VNightMarketHome 显示 getTimeStr(start, end-86400) 与 getTimeStr(start, end);
    #   官方 18:00 开放(次日结束); 私服全天开放, 但把 start 置昨日 20:00 / end 置明日 23:59:59,
    #   使客户端"探索时间 昨日20:00~今日23:59:59" 且"已结束"判定 (now > endtime) 不误报
    nm = _nm_reset()
    try:
        import datetime as _dt
        _now = _dt.datetime.now()
        if NIGHTMARKET_ALWAYS_OPEN:
            start = int((_now - _dt.timedelta(days=1)).replace(hour=20, minute=0, second=0, microsecond=0).timestamp())
            end = int((_now + _dt.timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0).timestamp())
        else:
            start = int(_now.replace(hour=NIGHTMARKET_START_H, minute=0, second=0, microsecond=0).timestamp())
            end = int(_now.replace(hour=23, minute=59, second=59, microsecond=0).timestamp())
    except Exception:
        start, end = int(time.time()) - 86400, int(time.time()) + 86400
    return start, end

def handle_nightmarket_info(body_json=None):
    # 1161 [markettoken, jointimes, starttime, endtime, daybuytimes, fubentime]
    #   ★2026-11-06（真机）：`fubentime` = “剩余可进入时间（秒）”。客户端
    #     `VNightMarketHome:384-390` 在 `fubentime <= 0` 时把「进入夜市」按钮
    #     `setEnabled(false)` + `setTouchEnabled(false)` —— 服务端原先恒发 0，
    #     于是按钮永远点不动、`1164` 永远不发（真机实测：点了 5 个坐标都无请求）。
    #     改成 `max(0, end - now)` 后按钮恢复，`1164` 正常发出。
    nm = _nm_reset()
    start, end = _nm_window()
    left = max(0, int(end) - int(time.time()))
    return _json_out([int(nm.get('token', 0)), int(nm.get('join', NIGHTMARKET_SEED_JOIN)),
                      start, end, int(nm.get('buy', 0)), left])

def handle_nightmarket_buy_join_num(body_json=None):
    # 1163 妖魂购买进入次数 -> [soul, times, daybuytimes]
    nm = _nm_reset()
    price = NIGHTMARKET_BUY_PRICE * (int(nm.get('buy', 0)) + 1)
    if int(EQ_STATE['soulcoin']) < price:
        return b'[60]'
    EQ_STATE['soulcoin'] = int(EQ_STATE['soulcoin']) - price
    nm['buy'] = int(nm.get('buy', 0)) + 1
    nm['join'] = int(nm.get('join', 0)) + 1
    save_state()
    return _json_out([int(EQ_STATE['soulcoin']), int(nm.get('join', 0)), int(nm.get('buy', 0))])

def handle_nightmarket_join(body_json=None):
    # 1164 进入夜市 (扣1次; 无次数返回 516)
    nm = _nm_reset()
    if int(nm.get('join', 0)) <= 0:
        return b'[516]'
    nm['join'] = int(nm.get('join', 0)) - 1
    save_state()
    return b'[1,[]]'

def handle_nightmarket_send_food(body_json):
    # 1162 送食物(道具) -> [removeProp, token] (食物道具 id 由客户端传入, 从背包扣 1)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        fid = int(req.get('id') or 0)
    except Exception:
        fid = 0
    uc = str(fid)
    if int(EQ_STATE.get('prop', {}).get(uc, 0)) < 1:
        return b'[97]'                            # 道具不足
    EQ_STATE['prop'][uc] = int(EQ_STATE['prop'][uc]) - 1
    if EQ_STATE['prop'][uc] <= 0:
        EQ_STATE['prop'].pop(uc, None)
        EQ_STATE.get('prop_id', {}).pop(uc, None)
    nm = _nm_reset()
    token = int(nm.get('token', 0)) + 1
    nm['token'] = token
    save_state()
    return _json_out([['%s' % uc, fid, 1], token])

def handle_nightmarket_token_push(body_json=None):
    # 1167 推送信物 (客户端注册回调)
    nm = _nm_reset()
    return _json_out([int(nm.get('token', 0))])


# ============================================================================
# 夜市排行榜 + 累计奖励 11611 / 11612（新包 A0 第 6 批）
# ----------------------------------------------------------------------------
# 数据源 = `A06_3_ActivityMarketReward`（14 行，**只在目标客户端**，本轮由
# `work\_table_migrate.py add` 迁入表根）。两条语义按 `Type` 分：
#   Type=1（8 行）排行榜名次奖励：`Rank` = "1|1" / "2|3" / …（名次区间），`GroupID`=91101~91108。
#     ⚠ 客户端只用它做**名次预览**（`VNightMarketReward:updateCell1` 读的是本地表
#       `getRankReward(1)`），**没有领取协议** ⇒ 服务端不需要发它、也不需要发奖。
#   Type=2（6 行）累计奖励宝箱：`Rank` = 1001~1006，`GroupID`=91001~91006，`ScoreLimit` = 积分门槛
#     （10000/7500/4000/1500/500/200 —— **按 Rank 升序递减**，也就是界面上"越靠上的箱子要求越高"）。
#
# 契约（逐行核对客户端）：
#   11611 `NetGetNightMarketRankInfo:recvMessage`
#     msg[1][1] = [[rank, rolename, hurt(积分), img, imgframe, level], ...]
#     msg[1][2] = [rank, rolename, hurt, img, imgframe, level]   ← 我这一行
#     msg[2]    = 宝箱状态**扁平表**（1-based、6 项）：
#                 1=已领取 / 2=未领取(达标) / 3=未完成
#     ⚠ `msg[1]` 长度必须 ≥2（它直接取 `msg[1][2]`）；`msg[2]` 必须存在
#       （`VNightMarketReward:getScoreRwd()` 直接返回它，随后被 `[_idx]` 取用）。
#     ⚠ **未上榜必须回 `rank = -1`**：`VNightMarketReward.lua:156` 就是判 `top == -1` 显示"未上榜"。
#   11612 `NetGetNightMarketRewards:sendMessage(_index)` → `msg["boxIndex"] = _index`，而调用点是
#     `Button_10:setTag(_idx - 1)` ⇒ **boxIndex 是 0-based**（同一个文件里 msg[2] 却是 1-based，
#     客户端自身不一致 —— 服务端只能照它写，别"顺手改成一致"）。
#     应答 = [roleInfo, dropInfo]（与 4444/1044 同族形状）；这张表没有 errorCode 表，
#     失败按 `[0]` 回（客户端 m_errorCode[0] = "消息体为空 错误"）与 `[97]`（道具不足，同 1162 的既有口径）。
# ★ 积分 = `nightmarket.token`（夜市信物，1162 送食材累积；服务端**已有**这个状态，不新开账本）。
# ============================================================================
_NM_RANK = []      # Type=1，文件顺序（给"名次区间->奖励组"用；本批不发，只登记口径）
_NM_BOX = []       # Type=2，**文件顺序** —— 客户端 `getRankReward(2)` 就是按文件顺序，
                   #   而 `boxComplete[_idx]` / `boxIndex=_idx-1` 都按这个下标走
try:
    _t = _load_json('A06_3_ActivityMarketReward.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _d = {'id': int(_r[_h['ID']]), 'type': int(_r[_h['Type']]),
                  'rank': str(_r[_h['Rank']]), 'group': int(_r[_h['GroupID']]),
                  'score': int(_r[_h['ScoreLimit']])}
        except Exception:
            continue
        (_NM_RANK if _d['type'] == 1 else _NM_BOX).append(_d)
except Exception:
    _NM_RANK, _NM_BOX = [], []


def _nm_box_state():
    """`msg[2]`：**扁平 6 项**（1=已领 / 2=未领 / 3=未完成），下标与 `_NM_BOX` 一一对应。"""
    score = int(_nm_reset().get('token', 0))
    claimed = EQ_STATE.setdefault('nightmarket_boxes', {})
    out = []
    for b in _NM_BOX:
        if str(b['id']) in claimed:
            out.append(1)
        elif score >= b['score']:
            out.append(2)
        else:
            out.append(3)
    return out


def _nm_me_row(rank, score):
    head = EQ_STATE.get('head') or {}
    return [rank, str(EQ_STATE.get('role_name') or ''), score,
            int(head.get('eq', 1001) or 1001), int(head.get('frame_eq', 1001) or 1001),
            int(EQ_STATE.get('player_lv', 1) or 1)]


def _nm_all_rows():
    """全部账号的夜市积分排行。**单档/未启用 -> `None`**（调用方走"只有我"那条路）。

    跨账号**只读 `acc.data` 的标量**（铁律 2：绝不 bind 到别人、绝不触发别人的落盘）。
    """
    try:
        import accounts as A
        reg = getattr(A, 'REGISTRY', None)
        if reg is None or not getattr(A, 'ENABLED', False):
            return None
        rows = []
        for acc in reg.all_accounts_full():
            d = acc.data if isinstance(acc.data, dict) else {}
            nmd = d.get('nightmarket') or {}
            try:
                sc = int(nmd.get('token') or 0)
            except Exception:
                sc = 0
            head = d.get('head') or {}
            rows.append((sc, str(acc.id),
                         str(d.get('role_name') or ''),
                         int(head.get('eq', 1001) or 1001),
                         int(head.get('frame_eq', 1001) or 1001),
                         int(d.get('player_lv', 1) or 1)))
        # 排序：积分降序 -> aid 升序（**必须定序**，否则同分时名次会在两次请求间跳）
        rows.sort(key=lambda x: (-x[0], x[1]))
        return rows
    except Exception:
        return None


def _nm_out(*body):
    """⚠ 本函数带 `ensure_ascii=False`：榜单里有**中文角色名**，而 `_json_out` 会把中文转成
    `\\uXXXX`。本文件的夜市其它号（1161~1167）全是数字所以看不出问题，这一条不能沿用。
    `_nm_out(a, b)` -> `[1,[a, b]]`（与 `_json_out` 的"单参数"口径**不同**，故单独一个函数）。"""
    return ('[1,' + json.dumps(list(body), separators=(',', ':'), ensure_ascii=False)
            + ']').encode('utf-8')


def handle_nightmarket_rank_info(body_json=None):
    """11611 夜市排行榜 + 累计奖励宝箱。"""
    score = int(_nm_reset().get('token', 0))
    allrows = _nm_all_rows()
    me = None
    try:
        import accounts as A
        cur = A.EQ_STATE.current() if callable(getattr(A.EQ_STATE, 'current', None)) else None
        me = getattr(cur, 'id', None)
    except Exception:
        me = None
    rows, my_rank = [], -1
    if allrows is None:
        # 单档：榜单只有我；积分为 0 视作**未上榜**（与多账号同口径）
        if score > 0:
            my_rank = 1
            rows = [[1] + _nm_me_row(1, score)[1:]]
    else:
        for i, (sc, aid, nm, img, frm, lv) in enumerate(allrows):
            if sc <= 0:
                continue
            if len(rows) >= 50:
                break
            rows.append([i + 1, nm, sc, img, frm, lv])
            if me is not None and aid == str(me):
                my_rank = i + 1
    if my_rank != -1:
        my = _nm_me_row(my_rank, score)
    else:
        my = _nm_me_row(-1, score)       # ★ 未上榜：客户端判 `top == -1` 显示"未上榜"
    return _nm_out([rows, my], _nm_box_state())


def handle_nightmarket_box_reward(body_json=None):
    """11612 领夜市累计奖励。req{boxIndex} —— **0-based**（见段头说明）。"""
    from handlers_task import _grant_group
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0]'
    if not isinstance(req, dict):
        req = {}
    idx = int(req.get('boxIndex') or 0)
    if idx < 0 or idx >= len(_NM_BOX):
        return b'[0]'
    box = _NM_BOX[idx]
    claimed = EQ_STATE.setdefault('nightmarket_boxes', {})
    if str(box['id']) in claimed:
        return b'[97]'                       # 已领过（这张表没有专门的重复领取码）
    if int(_nm_reset().get('token', 0)) < box['score']:
        return b'[97]'                       # 积分不够（与 1162"道具不足"同一个码，本工程既有口径）
    groups, ok = _grant_group(box['group'])
    if not ok:
        return b'[0]'                        # 奖励组查不到 -> 不记已领
    claimed[str(box['id'])] = 1
    save_state()
    # ⚠ 必须是 `_nm_out(roleInfo, dropInfo)`（两参）-> `[1,[roleInfo, dropInfo]]`：
    #   客户端 `msg[1]`/`msg[2]` **分别**取它们；包成一层 `[[r, d]]` 会让 `msg[2]` 变 nil
    #   ⇒ `refreshDropInfo(nil)` 拿不到掉落（"领了没弹奖励"）。
    return _nm_out(_role_info(), _drop(groups))


# ============================================================================
# ★2026-10-16（任务书 C①）：夜市商店的**数量上限 / 周期限购 / 已实现商品类型**
# ----------------------------------------------------------------------------
# · `_NM_BUY_MAX`：单次购买数量上限。**无表源**（`A06_3_ActivityMarketReward` 没有"单次上限"列），
#   取 99 是保守护栏（只为挡负数/天文数字，不改变正常玩法）；标"无源待拍板"。
# · `_NM_GOODS_TYPES`：表里实际出现的 GoodsType（1 装备 / 2 道具 / 4 妖怪 / 8 唤妖符 /
#   98 礼包），加上任务书点名的 3 铜钱 / 7 妖魂 / 9 体力 —— 都是 `REWARD_NEWTYPE` 里
#   `handlers_bag.grant_entries` 已实现的类型（发货只有一份实现）。
# · 周期限购与商城共用 `handlers_game._shop_period_key`（同一套周期键语义，避免两处漂移）。
# ============================================================================
_NM_BUY_MAX = 99              # 无源待拍板（表里没有单次上限列）
_NM_GOODS_TYPES = (1, 2, 3, 4, 7, 8, 9, 98)
_VITALITY_MAX = 20            # 与 state.VITALITY_MAX 同值（这里只是避免 import 环）


def _vitality_count():
    try:
        return int((EQ_STATE.get('prop') or {}).get('1006', 0))
    except (TypeError, ValueError):
        return 0


def _nm_sid_rows():
    return EQ_STATE.setdefault('nm_period', {})


def _nm_bought(sid, lt):
    """该商品**当前周期**已买次数（终身读老键 `nm_buy`，其余读 `nm_period`）。"""
    sid = str(sid)
    try:
        lt = int(lt)
    except (TypeError, ValueError):
        return 0
    if lt == 0:
        return 0
    if lt == 1:
        return int((EQ_STATE.get('nm_buy') or {}).get(sid, 0) or 0)
    try:
        from handlers_game import _shop_period_key
        key = _shop_period_key(lt)
    except Exception:
        key = None
    if not key:
        return 0
    book = (_nm_sid_rows().get(sid) or {})
    if not isinstance(book, dict):
        return 0
    try:
        return int(book.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _nm_bump(sid, n=1):
    """记一次购买：总次数（`nm_buy`，1165 老口径）+ 周期计数（`nm_period`）。"""
    sid = str(sid)
    try:
        n = max(1, int(n))
    except (TypeError, ValueError):
        n = 1
    nm_st = EQ_STATE.setdefault('nm_buy', {})
    nm_st[sid] = int(nm_st.get(sid, 0) or 0) + n
    lt = 0
    for (sid2, _gt, _gid, _gcnt, lt2, _lc, _pt, _pr) in _A06:
        if sid2 == sid:
            lt = lt2
            break
    if not lt:
        return
    try:
        from handlers_game import _shop_period_key
        key = _shop_period_key(lt)
    except Exception:
        key = None
    if not key:
        return
    book = _nm_sid_rows().setdefault(sid, {})
    if not isinstance(book, dict):
        book = _nm_sid_rows()[sid] = {}
    try:
        book[key] = int(book.get(key, 0) or 0) + n
    except (TypeError, ValueError):
        book[key] = n


def handle_nightmarket_shop_info(body_json=None):
    # 1165 [ [shopItemId, 周期内已购买数量].. ] (买0不限购行也给 0)
    #   ★2026-10-16（C①）：已购数量要按**当前周期**算（终身用老键，日/周/月用 nm_period），
    #     否则每日限购在界面上永远显示"还能买 N 次"。
    rows = []
    for (sid, gt, gid, gcnt, lt, lc, pt, pr) in _A06:
        rows.append([int(sid), int(_nm_bought(sid, lt))])
    return _json_out([rows])   # msg[1]=购买记录列表 (NetNightMarketShopInfo: for _,data in ipairs(msg[1]))

def handle_nightmarket_shop_buy(body_json):
    """1166 夜市商店购买 -> `[roleInfo, dropInfo, num]`（num>0 = 妖怪背包已满的提示位）。

    ★★2026-10-16（任务书 C①）：修前四处问题（都在同一段里）：
      ① `cnt = int(req.get('count') or 1)` **不设下界** ⇒ `count=-5` 会让
         `total = pr * cnt` 变负数，扣费语句变成"**把信物加回去**"，发货也是负数量；
      ② `GoodsType=4/8` 没实现 —— 但**扣费已经发生**，然后走空的 `groups` 直接回成功
         （"钱扣了、货没有"）；
      ③ 未支持的 `PriceType` 是扣费**之后**才落到 `else` 拒绝（同样是白扣）；
      ④ 限购只判 `LimitType==1`（终身），`LimitType=4`（每日）等周期限购不生效。
    现在：`count` 必须 1..99、`PriceType`/`GoodsType` 在**扣费之前**校验、限购按周期键、
    发货统一走 `handlers_bag.grant_entries`（类型分派只有一份实现，1/2/3/4/7/8/9/98 全支持）。
    """
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        sid = str(req.get('shopItemId') or 0)
        # ⚠ 不能用 `int(req.get('count') or 1)`：显式 `count=0` 会被 `or` 变成 1（本项目
        #   反复踩过的 falsy-默认坑）。缺省才取 1，显式 0/负数一律拒绝。
        _raw_cnt = req.get('count')
        cnt = 1 if _raw_cnt in (None, '') else int(_raw_cnt)
    except Exception:
        return b'[0,{}]'
    if cnt < 1 or cnt > _NM_BUY_MAX:
        return b'[0,{}]'                          # ★数量必须 1..上限（修前负数能加货币）
    row = None
    for (sid2, gt, gid, gcnt, lt, lc, pt, pr) in _A06:
        if sid2 == sid:
            row = (sid2, gt, gid, gcnt, lt, lc, pt, pr)
            break
    if row is None:
        return b'[0,{}]'
    _, gt, gid, gcnt, lt, lc, pt, pr = row
    # ---- 扣费前：货币类型与商品类型都必须已实现（修前 4/8 是"先扣后空"）----
    if pt not in (2, 3, 7):
        return b'[0,{}]'                          # 花沫信物(7)/妖魂(2)/金币(3)；其余不走这里
    if gt not in _NM_GOODS_TYPES:
        print('[NM] 1166 未实现的 GoodsType=%s (shopItemId=%s) -> 扣费前拒绝' % (gt, sid), flush=True)
        return b'[0,{}]'
    if gt == 9 and _vitality_count() >= _VITALITY_MAX:
        return b'[0,{}]'                          # 体力已满：不扣钱（否则钱没了体力没动）
    # ---- 周期限购（与商城同一套周期键）----
    if lt:
        if _nm_bought(sid, lt) + cnt > lc:
            return b'[52]' if lt == 1 else b'[0,{}]'
    total = pr * cnt
    if pt == 7:                                   # 花沫信物
        nm = _nm_reset()
        if int(nm.get('token', 0)) < total:
            return b'[60]'
        nm['token'] = int(nm.get('token', 0)) - total
    elif pt == 2:                                 # 妖魂
        if int(EQ_STATE['soulcoin']) < total:
            return b'[60]'
        EQ_STATE['soulcoin'] = int(EQ_STATE['soulcoin']) - total
    else:                                         # 金币
        if int(EQ_STATE['gold']) < total:
            return b'[60]'
        EQ_STATE['gold'] = int(EQ_STATE['gold']) - total
    # ---- 发货：类型分派只有一份实现 ----
    import handlers_bag as _hb
    _frag = _hb.grant_entries([(int(gt), int(gid) if str(gid).isdigit() else gid, int(gcnt) * cnt)])
    groups = []
    if _frag and _frag != '[]':
        try:
            groups = json.loads(_frag)[1]
        except Exception:
            groups = []
    # ★★2026-10-16（B 复核 4）：**"转成邮件"也算发货成功**。
    #   妖怪背包满时 `grant_entries` 的 dropInfo 是空的，但东西已经进邮件了；
    #   只看 `groups` 就会退款 —— 玩家既拿退款又拿邮件里的妖怪（**双份**）。
    _mailed = int(getattr(_hb, '_GRANT_MAILED', 0) or 0)
    if not groups and not _mailed:
        # 发放失败（例：体力刚好满了）→ **退款**，绝不吃钱不发货
        if pt == 7:
            _nm_reset()['token'] = int(_nm_reset().get('token', 0)) + total
        elif pt == 2:
            EQ_STATE['soulcoin'] = int(EQ_STATE['soulcoin']) + total
        else:
            EQ_STATE['gold'] = int(EQ_STATE['gold']) + total
        save_state()
        print('[NM] 1166 shopItemId=%s 发放失败已退还 total=%s' % (sid, total), flush=True)
        return b'[0,{}]'
    _nm_bump(sid)
    save_state()
    # ★2026-10-16（D 复核 · 逐槽对客户端）：第 3 槽 `num` 的语义是
    #   "大于 0 = 怪物背包已经达到上限"（`NetNightMarketShopBuy.lua` 注释原文），
    #   客户端拿到 >0 会弹"背包已满，去邮件领"。既然溢出的妖怪**真的转进了邮件**，
    #   这里就该把"转了几只"报给客户端 —— 修前恒 0，那句提示永远不会出现。
    return _json_out([_role_info(), _drop(groups), int(_mailed)])

# ============ 1102/9008 妖主入侵 (A05 时段窗; 基础版) ============
# ============ 探索形象 1184/1185/1186 (S20_ExploreBody; roleInfo[29]) ============
#   1184 INFO  -> [1,[[bodyid, state(1使用中/0未使用), time(-1永久/秒)..]]]
#   1185 ACTIVE req {id, ucode?, num?} -> [1,[roleInfo3(激活后), 消耗道具[ucode,id,count], dropInfo]]
#   1186 USE    req {id} -> [1,[useBodyID]] (客户端 emit -> SQManager 更新 ExploreRoleID)
_S20_ROWS = []      # (id, bodyid, name, type, time, needitem, neednum)
try:
    _t = _load_json('S20_ExploreBody.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _S20_ROWS.append((int(_r[_h['ID']]), int(_r[_h['BodyID']] or 0), str(_r[_h['Name']] or ''),
                              int(_r[_h['Type']] or 0), int(_r[_h['Time']] or -1),
                              str(_r[_h['NeedItem']] or '0'), int(_r[_h['ItemNum']] or 1)))
        except Exception:
            continue
except Exception:
    pass

def _explore_list():
    # 已激活形象列表 (S20 Type2 永久/Type3 限时; 私服: 激活即拥有, 时间 -1 永久)
    act = EQ_STATE.setdefault('explore_active', [])
    if not isinstance(act, list):
        EQ_STATE['explore_active'] = []
        act = EQ_STATE['explore_active']
    return act

def handle_switch_role_info(body_json=None):
    # 1184: 激活列表 [bodyid, state(使用中=1), time(-1 永久)]
    cur = int(EQ_STATE.get('explore_role_id', 1010))
    lst = _explore_list()
    rows = []
    if not lst:
        rows = [[cur, 1, -1]]
    else:
        rows = [[int(b), 1 if int(b) == cur else 0, -1] for b in lst]
    return _json_out([rows])

def handle_switch_role_active(body_json):
    # 1185: 激活形象 (消耗 S20 NeedItem x ItemNum; 私服: 无道具则自动发所需道具后激活, 保证可玩)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        bid = int(req.get('id') or 0)
    except Exception:
        return b'[0,{}]'
    row = None
    for (sid, bodyid, name, typ, t, ni, nn) in _S20_ROWS:
        if bodyid == bid:
            row = (bodyid, typ, t, ni, nn, name)
            break
    if row is None:
        return b'[0,{}]'
    bodyid, typ, t, ni, nn, name = row
    lst = _explore_list()
    ucode = str(req.get('ucode') or '0')
    prop_out = []
    if str(ni) not in ('0', '-1'):
        pid = int(ni)
        need = max(1, int(nn))
        have = int(EQ_STATE.get('prop', {}).get(str(pid), 0))
        if have < need:
            EQ_STATE.setdefault('prop', {})[str(pid)] = need     # 私服: 自动补齐所需道具
            EQ_STATE.setdefault('prop_id', {})[str(pid)] = pid
        else:
            EQ_STATE['prop'][str(pid)] = have - need
            if EQ_STATE['prop'][str(pid)] <= 0:
                EQ_STATE['prop'].pop(str(pid), None)
                EQ_STATE.get('prop_id', {}).pop(str(pid), None)
        prop_out = [str(pid), pid, need]
    if bodyid not in lst:
        lst.append(bodyid)
    EQ_STATE['explore_role_id'] = bodyid
    save_state()
    # 激活后角色信息: [id, state=1(使用中), time]
    role3 = [bodyid, 1, -1]
    return _json_out([role3, prop_out, [[], []]])

def handle_switch_role_use(body_json):
    # 1186: 使用已激活形象 -> [1,[useBodyID]]
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        bid = int(req.get('id') or 0)
    except Exception:
        return b'[0,{}]'
    if bid not in (int(x) for x in _explore_list()):
        return b'[0,{}]'
    EQ_STATE['explore_role_id'] = bid
    save_state()
    return _json_out([bid])

# ============ 1102/9008 妖主入侵 (A05 时段窗; 基础版) ============
AGGRESS_TICKET_PRICE = 100    # 妖魂/追加1次今日挑战
AGGRESS_TICKET_MAX = 3        # 每日购买上限 (558=已达到购买次数)

def _aggress_tickets():
    return EQ_STATE.setdefault('aggress_t', {'date': '', 'buy': 0})

def handle_aggress_tickets(body_json):
    # 1064 req {groupid} -> [1,[roleInfo]]; 码 60妖魂不足/557 id错/558已达上限
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        gid = str(req.get('groupid') or 0)
    except Exception:
        return b'[557]'
    from handlers_game import _T26_BY_GROUP, _aggr, _aggr_reset
    if gid not in _T26_BY_GROUP:
        return b'[557]'                          # 组 id 错误
    at = _aggress_tickets()
    today = time.strftime('%Y-%m-%d')
    if at.get('date') != today:
        at['date'] = today
        at['buy'] = 0
    if int(at.get('buy', 0)) >= AGGRESS_TICKET_MAX:
        return b'[558]'
    price = AGGRESS_TICKET_PRICE * (int(at.get('buy', 0)) + 1)
    if int(EQ_STATE['soulcoin']) < price:
        return b'[60]'
    EQ_STATE['soulcoin'] = int(EQ_STATE['soulcoin']) - price
    a = _aggr_reset()
    a['used'][gid] = max(0, int(a['used'].get(gid, 0)) - 1)   # 追回1次今日免费
    at['buy'] = int(at.get('buy', 0)) + 1
    save_state()
    return _json_out([_role_info()])


# ============ A05 妖主入侵 —— 伤害账 / 日榜 / 周榜 ============
#   ★★2026-11-07（内容收口）：修前这里只有 `day/damage/total` 三个键，而 `total` 从未增长
#     （9008 解不出加密伤害），1104 的奖励行表又读错列名（恒空）⇒ 整个活动没有可领奖励。
#   现在的模型（落 `EQ_STATE['invade']`，旧档缺字段按默认值补）：
#     day / damage                       当日
#     total / season                     本赛季累计（1106/1107 的"历史"口径）
#     last_day / last_day_damage         跨日快照
#     last_season / last_season_damage   跨周快照（周一 05:00，与排位同尺）
#     day_settle    {day, rank, group, claimed}
#     season_settle {season, rank, group, claimed}
#     box_claimed   {row_id: 1}          累计伤害宝箱（每赛季重置）
_INVADE_DEFAULTS = {
    'day': '', 'damage': 0, 'total': 0, 'season': '',
    'last_day': '', 'last_day_damage': 0,
    'last_season': '', 'last_season_damage': 0,
    'day_settle': {}, 'season_settle': {}, 'box_claimed': {},
}


def _invade():
    iv = EQ_STATE.setdefault('invade', {})
    if not isinstance(iv, dict):
        iv = EQ_STATE['invade'] = {}
    for k, v in _INVADE_DEFAULTS.items():
        if k not in iv or iv[k] is None:
            iv[k] = (dict(v) if isinstance(v, dict) else v)
    for k in ('day_settle', 'season_settle', 'box_claimed'):
        if not isinstance(iv[k], dict):
            iv[k] = {}
    return iv

def _invade_reset():
    """跨日/跨周：先快照旧值、再清零，返回当前档。"""
    iv = _invade()
    today = time.strftime('%Y-%m-%d')
    if str(iv.get('day') or '') != today:
        iv['last_day'] = str(iv.get('day') or '')
        iv['last_day_damage'] = int(iv.get('damage', 0) or 0)
        iv['day'] = today
        iv['damage'] = 0
    season = _invade_season_key()
    if str(iv.get('season') or '') != season:
        iv['last_season'] = str(iv.get('season') or '')
        iv['last_season_damage'] = int(iv.get('total', 0) or 0)
        iv['season'] = season
        iv['total'] = 0
        iv['box_claimed'] = {}
    return iv


def _invade_season_key():
    """周赛季起始日（与排位同一把尺子：周一 05:00）。"""
    try:
        from handlers_qualifying import _season_key
        return str(_season_key()[0])
    except Exception:
        return time.strftime('%Y-%m-%d')


def _acc_invade_score(a, kind, period):
    """只读别账号档里"某个周期"的妖主伤害（绝不 bind / setdefault）。"""
    try:
        d = a.data if isinstance(a.data, dict) else {}
        iv = d.get('invade') or {}
        if not isinstance(iv, dict):
            return 0
        if kind == 'day':
            if str(iv.get('last_day') or '') == period:
                return int(iv.get('last_day_damage', 0) or 0)
            if str(iv.get('day') or '') == period:
                return int(iv.get('damage', 0) or 0)
            return 0
        if str(iv.get('last_season') or '') == period:
            return int(iv.get('last_season_damage', 0) or 0)
        if str(iv.get('season') or '') == period:
            return int(iv.get('total', 0) or 0)
        return 0
    except Exception:
        return 0


def _invade_rank(kind, period, my_score):
    """名次 = 1 + 比我分高的人数；0 分不上榜（多账号读他人档快照，单档=1）。"""
    if int(my_score or 0) <= 0:
        return 0
    try:
        from accounts import ENABLED, REGISTRY, EQ_STATE as _AS
        if ENABLED and REGISTRY is not None:
            mine = getattr(_AS.current(), 'id', None)
            better = 0
            for a in REGISTRY.all_accounts_full():
                if mine is not None and a.id == mine:
                    continue
                if _acc_invade_score(a, kind, period) > int(my_score):
                    better += 1
            return better + 1
    except Exception:
        pass
    return 1


def _invade_rank_group(rows, rank):
    """名次 -> T02 组（区间来自 A05_ActivityMasterRanking.Rank，如 "4|10"）。"""
    try:
        r = int(rank)
    except Exception:
        return 0
    for (lo, hi, gid) in (rows or []):
        if int(lo) <= r <= int(hi):
            return int(gid)
    return 0


def _settle_invade(iv=None):
    """惰性结算：日榜（上一个有伤害的日子）+ 累计榜（上一个赛季）。幂等，异常不外抛。"""
    iv = iv if isinstance(iv, dict) else _invade()
    try:
        d = str(iv.get('last_day') or '')
        st = iv.get('day_settle') or {}
        if d and (not isinstance(st, dict) or st.get('day') != d):
            dmg = int(iv.get('last_day_damage', 0) or 0)
            rank = _invade_rank('day', d, dmg)
            iv['day_settle'] = {'day': d, 'rank': int(rank),
                                'group': _invade_rank_group(_A05_DAY_RANK, rank),
                                'claimed': 0}
    except Exception as e:
        print('[INVADE] !! 日榜结算异常: %r' % (e,))
    try:
        s = str(iv.get('last_season') or '')
        st = iv.get('season_settle') or {}
        if s and (not isinstance(st, dict) or st.get('season') != s):
            dmg = int(iv.get('last_season_damage', 0) or 0)
            rank = _invade_rank('season', s, dmg)
            iv['season_settle'] = {'season': s, 'rank': int(rank),
                                   'group': _invade_rank_group(_A05_HIS_RANK, rank),
                                   'claimed': 0}
    except Exception as e:
        print('[INVADE] !! 累计榜结算异常: %r' % (e,))
    return iv


def _invade_reward_state(iv, kind):
    """1104 的奖励状态：0 无 / 1 可领 / 2 已领。"""
    st = iv.get('day_settle' if kind == 'day' else 'season_settle') or {}
    if not isinstance(st, dict):
        return 0
    if int(st.get('claimed', 0) or 0) == 1:
        return 2
    if int(st.get('group', 0) or 0) > 0:
        return 1
    return 0

def _invade_window_now():
    # A05_ActivityMasterIntrusio 多行时段 (Begin/Over 串); 当前命中则返回 (MonsterID, MstrGroupID)
    try:
        _now = time.strftime('%H%M%S')
    except Exception:
        _now = '000000'
    for (sid, beg, over, mid, gid) in _A05_INTR:
        if beg <= _now < over:
            return (mid, gid)
    return None

# ============ A05 妖主大入侵 时段表 (ID/Begin/Over/MonsterID/MonsterInfo/MstrGroupID) ============
_A05 = []
try:
    _t = _load_json('A05_ActivityMasterIntrusio.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _A05.append((int(_r[_h['ID']]), str(_r[_h['Begin']] or '000000'), str(_r[_h['Over']] or '005959'),
                         int(_r[_h['MonsterID']] or 0), str(_r[_h['MonsterInfo']] or ''),
                         int(_r[_h['MstrGroupID']] or 0)))
        except Exception:
            continue
except Exception:
    pass
# A05_ActivityMasterRanking 的真实列是 ID / Type / Rank / GroupID / HurtLimit：
#   Type=1 今日排行（Rank="1|1" / "4|10" 名次区间 -> GroupID 9101..9106）
#   Type=2 累计排行（-> 9201..9207）
#   Type=3 累计伤害宝箱（Rank="1001".. 是档位号；HurtLimit=伤害门槛；-> 9301..9304）
#   ★★2026-11-07 修复：修前按 `RequireDamage/RewardID` 读 —— 这两列**根本不存在**，
#     KeyError 被下面的 except 吞掉 -> `_A05_RANK` 恒空 -> 1104 回空行表 ->
#     客户端 `InvadeReward.lua:227` 对空 totalreward 取下标抛 Lua 错误（奖励页不可用）。
_A05_BOX = []        # [(row_id, hurt_limit, group_id)]  累计伤害宝箱（按 ID 排序）
_A05_DAY_RANK = []   # [(rank_lo, rank_hi, group_id)]    今日排行
_A05_HIS_RANK = []   # [(rank_lo, rank_hi, group_id)]    累计排行


def _rank_range(s):
    """`"1|1"` / `"4|10"` -> (lo, hi)；解析不出返回 None。"""
    parts = str(s or '').replace('|', '-').split('-')
    try:
        lo = int(parts[0].strip() or 0)
    except Exception:
        return None
    if lo <= 0:
        return None
    try:
        hi = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else lo
    except Exception:
        hi = lo
    return (lo, max(lo, hi))


try:
    _t = _load_json('A05_ActivityMasterRanking.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _rid = int(_r[_h['ID']] or 0)
            _typ = int(_r[_h['Type']] or 0)
            _gid = int(_r[_h['GroupID']] or 0)
            _hurt = int(_r[_h['HurtLimit']] or 0)
        except Exception:
            continue
        if _gid <= 0:
            continue
        if _typ == 3:
            _A05_BOX.append((_rid, max(0, _hurt), _gid))
        else:
            _rr = _rank_range(_r[_h['Rank']])
            if _rr:
                (_A05_DAY_RANK if _typ == 1 else _A05_HIS_RANK).append((_rr[0], _rr[1], _gid))
except Exception:
    pass
_A05_BOX.sort()
for _lst in (_A05_DAY_RANK, _A05_HIS_RANK):
    _lst.sort()

# ============ A07 限时任务(百鬼夜宴/金生千世) 3068 ============
#   A07_1_ActivityTaskCatalog: ID(活动id 1/2/3) / ActivityName / Type
#   A07_2_ActivityTaskList   : ID(任务id) / Activity_ID / **Stage** / ConditionType / Quantity(目标) / RewardID / Sort
#        ★ Stage == -1 的行 = "完成全部任务可领取"的**大奖行**; Stage 1..7 = 七个阶段的任务
#   ★2026-09-12 真机抓到的崩溃 (与 §1 同类):
#     MTimeLimitedTaskData:saveFinishList 会对每个 taskid 做
#       SDFactory:getMe():getObject(SD_OBJS.SDLimitedTimeTask, taskid) -> :getStatic_Tittle()
#     旧实现 msg[2] 填了 [0,0,0] (taskid=0) -> getObject 返回 nil -> "attempt to index a nil value"
#     -> 仍是在 SocketManager 的 recvMessage 回调栈里抛错 -> socket tick 中断。
#     **结论: 3068 里的每一个 taskid 都必须是 A07_2 真实行 id, 一个都不能编。**
_A07 = {}          # activity_id -> {'big': taskid, 'stages': {stage: [(taskid, ct, qty)]}}
_A07_REWARD = {}   # taskid -> RewardID（A07_2 的奖励组；3069 发奖用）
_A07_ROW = {}      # taskid -> (activity_id, stage, ct, qty)
_A07_VAL = {}      # taskid -> Value(int, 只取首段) —— CT=45 需要"到几阶"
_A07_STAGE_IDS = {}  # activity_id -> [全部阶段 taskid]（大奖的完成条件）
_A07_BIG = {}      # activity_id -> 大奖 taskid
try:
    _t = _load_json('A07_2_ActivityTaskList.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _aid = int(_r[_h['Activity_ID']] or 0)
            _tid = int(_r[_h['ID']] or 0)
            _stg = int(_r[_h['Stage']] or 0)
            _ct = int(_r[_h['ConditionType']] or 0)
            _qty = int(_r[_h['Quantity']] or 0)
            _rew = int(_r[_h['RewardID']] or 0)
            try:
                _val = int(str(_r[_h['Value']] or '0').replace('&', '|').split('|')[0].strip() or 0)
            except Exception:
                _val = 0
            _e = _A07.setdefault(_aid, {'big': 0, 'stages': {}})
            _A07_REWARD[_tid] = _rew
            _A07_VAL[_tid] = _val
            _A07_ROW[_tid] = (_aid, _stg, _ct, _qty)
            if _stg < 0:
                _e['big'] = _tid                 # 大奖行
                _A07_BIG[_aid] = _tid
            else:
                _e['stages'].setdefault(_stg, []).append((_tid, _ct, _qty))
                _A07_STAGE_IDS.setdefault(_aid, []).append(_tid)
        except Exception:
            continue
except Exception:
    pass
_A07_IDS = set()   # 全部合法任务 id (自检/防呆用)
for _a in _A07.values():
    if _a['big']:
        _A07_IDS.add(_a['big'])
    for _sl in _a['stages'].values():
        for (_tid, _ct, _qty) in _sl:
            _A07_IDS.add(_tid)
_A07_CAT = []      # A07_1: (id, name)
try:
    _t = _load_json('A07_1_ActivityTaskCatalog.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _A07_CAT.append((int(_r[_h['ID']]), str(_r[_h['ActivityName']] or '')))
        except Exception:
            continue
except Exception:
    pass

def _a07_progress(ct, qty, tid=0):
    # ★★2026-10-25（用户裁定）：CT=45「将 N 个妖怪进化到 M 阶」改读**去重账本** `a07_mon_star`
    #   （ucode -> 达到过的最高阶，只在真的进化成功时写，`handlers_game` 6003 处）。
    #   修前读 `a07_ct['45']`（"进化了几次"）：同一只怪升两阶会冒充两只，且完全不看 Value 的阶数。
    #   ⚠ 标题为准：10313/20409 标题写"3 阶"而表里 Value=2（数据矛盾，用户 2026-10-25 拍板）。
    if int(ct) == 45:
        need = int(_A07_VAL.get(int(tid), 0) or 0)
        if str(tid) in ('10313', '20409'):
            need = 3
        if need <= 0:
            return 0
        st = EQ_STATE.get('a07_mon_star') or {}
        if not isinstance(st, dict):
            return 0
        n = 0
        for v in st.values():
            try:
                if int(v) >= need:
                    n += 1
            except Exception:
                continue
        return min(n, int(qty)) if qty else n
    # 其余 CT 复用每日任务已经驱动的计数 (T37 同一套枚举: 3 通关关卡 / 10 装备强化 / 4 妖塔 ...)
    st = EQ_STATE.setdefault('a07_ct', {})
    try:
        n = int(st.get(str(int(ct)), 0))
    except Exception:
        n = 0
    return min(n, int(qty or 0)) if qty else n

def handle_limited_time_task(body_json=None):
    # 3068 LIMITED_TIME_TASK_DATA (NetGetLimitedTimeTask:recvMessage 明文):
    #   msg[1] = [[[taskid, tasknum, taskstate], ...] x 7]  下标 = 阶段 1..7, 空阶段给空表
    #   msg[2] = [大奖taskid, tasknum, taskstate]           <- 必须是 A07_2 Stage=-1 行的真实 id
    #   msg[3] = 活动ID (A07_1.ID)
    #   msg[4] = 剩余秒   msg[5] = 今日开放阶段 1..7
    st = EQ_STATE.setdefault('limited_task', {})
    today = time.strftime('%Y-%m-%d')
    if st.get('day') != today:
        st['day'] = today
        st['claimed_3068'] = {}
    claimed = st.setdefault('claimed_3068', {})
    _sid, _mid, _minfo, _gid = _a05_stage_now()
    stage = max(1, min(int(_sid), 7))
    aid = _A07_CAT[0][0] if _A07_CAT else 1
    ent = _A07.get(aid) or {'big': 0, 'stages': {}}
    groups = []
    for g in range(1, 8):
        rows = []
        for (tid, ct, qty) in ent['stages'].get(g, []):
            num = _a07_progress(ct, qty, tid)
            if str(tid) in claimed:
                state = 2
            else:
                state = 1 if (qty and num >= qty) else 0
            rows.append([tid, num, state])
        groups.append(rows)
    bid = int(ent.get('big') or 0)
    if bid:
        bnum = sum(1 for _sl in groups for _r in _sl if _r[2] == 2)
        bstate = 2 if str(bid) in claimed else (1 if all(_r[2] == 2 for _sl in groups for _r in _sl) and bnum > 0 else 0)
        big = [bid, bnum, bstate]
    else:
        big = [0, 0, 0]
    try:
        end = time.mktime(time.strptime(today + ' 23:59:59', '%Y-%m-%d %H:%M:%S'))
        remain = max(0, int(end - time.time()))
    except Exception:
        remain = 86400
    return _json_out([groups, big, aid, remain, stage])

def handle_limited_time_task_reward(body_json):
    """3069 领限时任务奖励。

    ★★2026-10-17（真机复核）：主界面「限时任务」按钮实测发 **3068**（不是 4068），
      客户端的 `NetLimitedTimeReward` 有 [537]/[538]/[539] 三张码表 ⇒ 本条按
      A07_2 的真实 `RewardID` 发奖（修前恒发空 dropInfo = 玩家"领了没东西"）：
        · task id 必须在 A07_2（否则 537）；
        · 已领过 -> 539；条件不足 -> 538；
        · 大奖（Stage=-1）要求该活动的全部阶段任务都已领取；
        · 奖励组缺失/发放失败 -> 537，且**不**标记已领。
      与 4069 的 `claimed` 分开：本条只写 `claimed_3068`，3068 也读它
      —— 否则领完面板永远不回显"已领取"。
    """
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
    if tid not in _A07_IDS:
        return b'[537]'
    st = EQ_STATE.setdefault('limited_task', {})
    _today = time.strftime('%Y-%m-%d')
    if st.get('day') != _today:
        # 与 3068 同口径：跨天先重置两本账（否则"先领后开面板"会被面板重置抹掉）
        st['day'] = _today
        st['claimed_3068'] = {}
    claimed = st.setdefault('claimed_3068', {})
    if not isinstance(claimed, dict):
        claimed = st['claimed_3068'] = {}
    if str(tid) in claimed:
        return b'[539]'
    reward_gid = int(_A07_REWARD.get(tid) or 0)
    if not reward_gid:
        print('[3069] 任务 %s 在 A07_2 没有 RewardID -> 537' % tid, flush=True)
        return b'[537]'
    _aid, _stg, _ct, _qty = _A07_ROW.get(tid, (0, 0, 0, 0))
    if _stg < 0:
        need = _A07_STAGE_IDS.get(_aid) or []
        if not need or not all(str(t) in claimed for t in need):
            return b'[538]'
    elif _qty and _a07_progress(_ct, _qty, tid) < _qty:
        return b'[538]'
    from handlers_task import _grant_group
    from handlers_game import _role_info as _gi, _drop as _gd
    groups, ok = _grant_group(reward_gid)
    if not ok:
        print('[3069] 奖励组 %s 发放失败（任务 %s）-> 537' % (reward_gid, tid), flush=True)
        return b'[537]'
    claimed[str(tid)] = 1
    save_state()
    return _json_out([_gi(), _gd(groups)])

def _a05_stage_now():
    # A05_ActivityMasterIntrusio: 22 个两小时时段 (Begin/Over = HHMMSS), ID 即"当前开启阶段"
    #   客户端 InvadeHome: mStartID = msg[4]; 战斗用 groupID = MstrGroupID
    try:
        hhmmss = time.strftime('%H%M%S')
        for (sid, b, o, mid, minfo, gid) in _A05:
            if b <= hhmmss <= o:
                return sid, mid, minfo, gid
    except Exception:
        pass
    return (1, 5901, '', 5901)

def _a05_remain_seconds():
    """当前时段剩余秒（客户端 `InvadeHome:getStrByTime` 把它当**时长**解析）。

    ★★2026-11-07 修复：修前 1102 发的是"当日 23:59:59 的绝对 epoch 秒"，
      客户端按剩余秒解析 ⇒ 面板倒计时显示「两万多天」。私人服不做时段门禁（全天可挑战），
      不在任何时段时回 0（客户端判据是 `time >= 0`，0 仍可点）。
    """
    try:
        import datetime as _dt
        now = _dt.datetime.now()
        hhmmss = now.strftime('%H%M%S')
        for (sid, b, o, mid, minfo, gid) in _A05:
            if b <= hhmmss <= o:
                end = now.replace(hour=int(o[0:2]), minute=int(o[2:4]),
                                  second=int(o[4:6]), microsecond=0)
                return max(0, int((end - now).total_seconds()))
    except Exception:
        pass
    return 0

def _invade_attack_data():
    # 1102 msg[5] = 特攻配置 10 字段 (1.31 NetInvade:recvMessage 明文):
    #   [type, switchIs, attacktype, attackid, damage, attackadditiontype,
    #    attackadditionvalue, attackresult, monsterdescribe, interfaceid]
    #   ★2026-09-12 修复 (用户反馈 §1 "界面卡死"): 旧响应只有 3 个字段 -> msg[5]=nil ->
    #     客户端 `local attackData = msg[5]; self.specialData.type = attackData[1]` 直接
    #     "attempt to index local 'attackData' (a nil value)" -> SocketManager 的 recvMessage
    #     抛错 -> 整条 socket tick 中断 -> 界面卡死 (实测 traceback: SocketManager.lua:497)。
    #   私服不做"特攻"玩法: switchIs=0 (客户端 `if self.mAttackData.switchIs == 1` 直接跳过),
    #   interfaceid=1241 (= 妖主大入侵自身 -> 客户端不跳转到其它系统)。
    _sid, _mid, _minfo, _gid = _a05_stage_now()
    return [1, 0, 0, '', '', 0, '', '', str(_minfo or ''), 1241]

def handle_invade(body_json=None):
    # 1102 [time(当前时段**剩余秒**), daydamage, damage, startID, attackData[10]]
    #   ★2026-11-07：time 由"绝对 epoch"改为剩余秒（见 `_a05_remain_seconds`）。
    iv = _invade_reset()
    _settle_invade(iv)
    _sid, _mid, _minfo, _gid = _a05_stage_now()
    return _json_out([_a05_remain_seconds(), int(iv.get('damage', 0)), int(iv.get('total', 0)),
                      int(_sid), _invade_attack_data()])

def handle_invade_rewards(body_json=None):
    # 1104 INVADE_REWARDS (NetInvadeRewards:recvMessage 明文):
    #   [daydamage, hisdamage, dayrank, hisrank, dayrewardstate, hisrewardstate, [[ID,state,rewardID,damage],...]]
    #   ★2026-09-12 新增: 此前 1104 无处理器 -> 默认响应 [1,{}] -> msg[1]=nil ->
    #     `pairs(msg[7])`/`tonumber(msg[1])` 抛错 -> socket tick 中断 -> 界面卡死。
    #   ★★2026-10-02（P3）`dayrank`/`hisrank` 改用与 1106/1107 同一取数口径 `_world_my_rank`。
    #   ★★2026-11-07（内容收口）：msg[7] 改为 `A05_ActivityMasterRanking` 的 Type=3 行
    #     （累计伤害宝箱，4 行）；行内容是 `[行ID, 状态, 奖励组, 伤害门槛]`。
    #     修前读错列名 -> 恒空 -> 客户端 `InvadeReward.lua:227` 取空表下标抛 Lua 错误。
    #   ⚠ **名次必须与本次响应的伤害用同一个值算**：`_invade_reset()` 会在跨天时把
    #     `damage` 清零，故先把要发的值取出来，再用它算名次。
    iv = _invade_reset()
    _settle_invade(iv)
    total = int(iv.get('total', 0) or 0)
    claimed = iv.get('box_claimed') or {}
    boxes = []
    for (rid, need, gid) in _A05_BOX:
        if str(rid) in claimed:
            state = 2
        elif total >= need:
            state = 1
        else:
            state = 0
        boxes.append([rid, state, gid, need])
    day_dmg = int(iv.get('damage', 0) or 0)
    his_dmg = total
    dayrank, hisrank = -1, -1
    try:
        from handlers_rank import _world_my_rank
        _d = _world_my_rank('invade_day', day_dmg)
        _h = _world_my_rank('invade_his', his_dmg)
        # ★0 伤害必须回 -1：客户端拿 -1 当"你还没入侵过"。若把 0 分也交给
        #   `_world_my_rank`，当全服都没伤害时它会算出第 1 名（1 + 0 个更高的）。
        if _d is not None and day_dmg > 0:
            dayrank = int(_d)
        if _h is not None and his_dmg > 0:
            hisrank = int(_h)
    except Exception:
        pass
    return _json_out([day_dmg, his_dmg, dayrank, hisrank,
                      _invade_reward_state(iv, 'day'), _invade_reward_state(iv, 'season'), boxes])

def handle_invade_get_rewards(body_json):
    # 1105 INVADE_GET_REWARDS req {type,id}: 1今日排行 / 2累计排行 / 3累计伤害宝箱
    #   响应 = [roleInfo, dropInfo] (NetInvadeGetRewards:recvMessage)
    #   ★2026-11-07：type=1/2 读**已结算**的 day_settle/season_settle（不拿当前名次冒充结算名次）；
    #     type=3 是累计伤害宝箱（行 id 来自 A05_ActivityMasterRanking Type=3）。
    #   码：538 条件不足 / 539 已领取 / 537 奖励组下发失败。
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        typ = int(req.get('type') or 0)
    except Exception:
        typ = 0
    rid = str(req.get('id') or '0')
    from handlers_game import _role_info as _gi, _drop as _gd
    from handlers_task import _grant_group
    iv = _invade_reset()
    _settle_invade(iv)
    groups = []
    if typ == 3:
        row = None
        for (bid, need, gid) in _A05_BOX:
            if str(bid) == rid:
                row = (bid, need, gid)
                break
        if row is None:
            return b'[538]'
        claimed = iv.setdefault('box_claimed', {})
        if rid in claimed:
            return b'[539]'                     # 已领取
        if int(iv.get('total', 0) or 0) < int(row[1]):
            return b'[538]'                     # 伤害不足
        groups, ok = _grant_group(row[2])
        if not ok:
            return b'[537]'
        claimed[rid] = 1
    elif typ in (1, 2):
        key = 'day_settle' if typ == 1 else 'season_settle'
        st = iv.get(key) or {}
        if not isinstance(st, dict):
            return b'[538]'
        if int(st.get('claimed', 0) or 0) == 1:
            return b'[539]'
        gid = int(st.get('group', 0) or 0)
        if gid <= 0:
            return b'[538]'                     # 没上榜/名次不落在任何奖励区间
        groups, ok = _grant_group(gid)
        if not ok:
            return b'[537]'
        st['claimed'] = 1
        iv[key] = st
    else:
        return b'[0,{}]'
    save_state()
    return _json_out([_gi(), _gd(groups)])

def handle_invade_result(body_json):
    # 9008: 妖主大入侵战斗结算。
    #   ★★2026-11-07 修复：客户端 `NetInvadeResult.lua:32` 把 damage 用 ZZRc4 加密后再发
    #     （密钥 = 同一包的 time），修前只认纯数字 ⇒ **伤害恒 0**，宝箱/日榜/累计榜全部不可达。
    #     现在按 time 解密；并按 `battle_session` 做一次性结算（1702 type=7 开、9008 消费），
    #     重放/没开战一律不发奖、不记伤害（回结构完整的空掉落，绝不崩客户端）。
    #   响应五槽（NetInvadeResult.lua:11-15）：roleInfo / ourFormation / dropInfo / talent / exDropItems。
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    from handlers_game import _role_info as _gi, _drop as _gd, _zzrc4_int
    from state import player_add_exp
    iv = _invade_reset()
    _settle_invade(iv)
    # ---- time 窗：客户端 time 同时是加密密钥，过期/超前都说明不是这一场 ----
    try:
        ts = int(req.get('time') or 0)
    except Exception:
        ts = 0
    now = int(time.time())
    if ts <= 0 or abs(now - ts) > 600:
        print('[INVADE] 9008 time=%r 与服务器差 %ss -> 580'
              % (req.get('time'), (now - ts) if ts else '?'), flush=True)
        return b'[580]'
    raw = req.get('damage')
    damage = 0
    if isinstance(raw, str) and raw.strip().isdigit():
        damage = int(raw.strip())               # 测试/明文兜底
    else:
        _v = _zzrc4_int(raw, req.get('time'))
        if _v is not None:
            damage = int(_v)
    damage = max(0, min(damage, 10 ** 9))
    # ---- 一次性：必须由 1702 type=7 开过这一局 ----
    ok, why = False, 'no_open'
    try:
        from battle_session import session_target as _bs_target, settle as _bs_settle
        _tgt = _bs_target('invade')
        if _tgt:
            ok, why = _bs_settle('invade', _tgt, '%s|%s' % (raw, req.get('time')))
    except Exception as e:
        print('[BS] !! 妖主结算状态机异常(按未结算处理): %r' % (e,), flush=True)
    if not ok:
        print('[INVADE] 9008 拒结算(%s) -> 空掉落（不记伤害/不发钱）' % (why,), flush=True)
        return _json_out([_gi(), [], _gd([]), 0, _gd([])])
    iv['damage'] = int(iv.get('damage', 0) or 0) + damage
    iv['total'] = int(iv.get('total', 0) or 0) + damage
    iswin = int(req.get('result') or 0) == 0
    gold = 300 if iswin else 30
    EQ_STATE['gold'] = int(EQ_STATE.get('gold', 0)) + gold
    player_add_exp(200 if iswin else 0)
    save_state()
    print('[INVADE] 9008 结算 damage=%d win=%s -> day=%d total=%d'
          % (damage, iswin, iv['damage'], iv['total']), flush=True)
    return _json_out([_gi(), [], _gd([[3, [[gold]]]]), 0, _gd([])])


# ============================================================================
# ★2026-09-12 (v26 P1) A 类剩余三件: 1012 断线重连 / 1017 妖神消耗 / 4068·4069 新版限时任务
# ----------------------------------------------------------------------------
# 这三条此前全部未处理 -> 落 config_data.resp_for 兜底 [1,[[]]]:
#   1012: `pairs(_embattleformation=msg[4])` -> nil -> 崩 -> socket tick 断 (妖主入侵断线重连直接卡死)
#   1017: `prop.field(msg[2])` -> SQGhost.lua:273 nil 算术 -> 崩 (妖神传说的**入口**, 走不到战斗)
#   4068: `pairs(msg[2])` -> nil -> 崩 (新版限时任务面板点开即卡)
# ============================================================================

def handle_aggress_reconnection(body_json=None):
    # 1012 AGGRESS_RECONNECTION (app/pve/MAggressData.lua:122 / PVEAggressData.lua:115)
    #   客户端明文 (NetAggressReconnection.lua:29~73):
    #     msg[1] wavenum(当前波数, ==0 时客户端自行置 1)
    #     msg[2] surparraynum(剩余换阵次数)
    #     msg[3] level(当前玩家等级)
    #     msg[4] _embattleformation -> for _,monDta in pairs(...) 取 [1]ucode [2]posx [3]posy [4]blood
    #                                  ★必须是表, nil -> pairs(nil) 崩
    #     msg[5] _deadlist          -> for k,deadUncode in pairs(...)  ★同样必须是表
    #     msg[6] levleId            -> ★v53 修正: 客户端当 **T26_YGRQZD.StageID**(=T26_YGRQ.ID, 10011/10021/..)
    #                                  用! MAggressData.lua:109 `AGGRESS_FIGHT_ID = tonumber(levelId)`,
    #                                  CAggressHome.lua:125-127 `getFightGroupList(levelId)` ->
    #                                  PVEAggressData.lua:125 `GetTableSelectData(T26_YGRQZD, StageID, id)`
    #                                  -> data[1]["ID"] -> SDAggressFight -> 每波战斗组。
    #                                  v26~v52 这里发的是 **T25 行 id**(1006) -> T26_YGRQZD 里没有 -> data=nil
    #                                  -> `data[1]` 索引 nil -> Lua 抛错, 断线重连直接死。
    #   语义: "断线后回到入侵战场第 N 波"。私服没有真实波次进度存储, 采用**保守重开**:
    #     波数 1 / 剩余换阵 0 / 玩家真实等级 / 布阵取当前落库阵容(blood=1 满血) / 空阵亡表 /
    #     levleId 用今天开放组的**最小真实 StageID**。
    from handlers_game import _role_info  # noqa: F401  (仅确保模块已加载, 保持 import 顺序稳定)
    lv = int(EQ_STATE.get('player_lv', 1))
    rows = []
    level_id = 10011
    try:
        from handlers_sys import _form_normalize, _FORM_MAP_X, _FORM_MAP_Y  # noqa: F401
        for (uc, px, py) in _form_normalize(5):
            rows.append([str(uc), int(px), int(py), 1])
    except Exception:
        rows = []
    try:
        from handlers_game import aggress_today_stage_id
        level_id = int(aggress_today_stage_id())
    except Exception:
        level_id = 10011
    return _json_out([1, 0, lv, rows, [], level_id])


def handle_lengend_consume(body_json):
    # 1017 LENGEND_CONSUME (app/legend/VLegendSelect.lua:158 "进入妖怪传说战斗")
    #   请求 {difficulty}; 响应 (NetLengendConsume.lua:30~54):
    #     msg[1] roleInfo(30字段) -> SQManager:gamer_getMe():setRoleInfo
    #     msg[2] prop([ucode,id,count]) -> SQManager:prop_getMe():updateItem  ★nil 合法, 非表崩
    #     msg[3] monsterid  -> 回调第 1 参
    #     msg[4] battleID   -> 回调第 2 参, 客户端拿去 initScene(..., _battleID)
    #   错误码: 0/513 体力不足/518 门票不足/524 本轮已挑战/555 困难度错误
    #   ⚠ 客户端 SQBIData.Type.PVE_LENGENT 注释写的是"妖魂 SPIRIT", 但 T23 里没有妖神门票的独立
    #     函数 id, 且 1014 已下发本轮 monsterid/skillstate —— 所以这里**不扣任何货币**, 只回真实
    #     roleInfo + 真实 monsterid/battleID (私服无计费语义; 将来要收费就在这里加扣减点)。
    #
    #   ★★★ v53 (2026-09-13) 修 P0: msg[4] 必须是 **T25 行 MonsterGroup{difficulty}**, 不是 T25 行 id!
    #     真机事故: 弹 "战斗组数据为空 id is: 1006" 后卡在 "游戏加载中… 0%"(永不出来)。
    #       客户端 VLegendSelect.lua:166 -> WarData.lua:104 `usGetTableData(tT01_MstrGroup, battleID)`
    #       -> 没有这行就弹提示 + return(且没置 m_isFinishData) -> StageScene 死等 -> 卡死。
    #     而 T25 行 id 与 T01_MstrGroup 是**两个 id 空间**:
    #       T25_YSCSSX   (妖神刷新表) : ID=1001..1033 (按天轮换, 21 行), 行内 MonsterGroup..5 = 各难度战斗组
    #       T01_MstrGroup(战斗组表)   : 5xxxxx 段就是妖神难度组, 例 "500241 <九尾>难度1"
    #     两者还会**撞号**: T25 的 1001/1002/1003/1011/1012 恰好也在 T01_MstrGroup 里(第一章关卡)
    #       -> 那几天不报错, 而是**静默把第一章小怪当成妖神加载**, 更阴。
    #     难度列映射 (SDYSCSSX.lua:36-54 / CLegendHome.lua:68-94 逐字段核对):
    #       1->MonsterGroup 2->MonsterGroup2 3->MonsterGroup3 4->MonsterGroup4 5->MonsterGroup5
    #     ★客户端 1.31 只有 5 档 (VLegendSelect 的 Button_Difficulty_1..7 中, 6/7 拿不到 itemObj
    #       -> setVisible(false); CLegendHome 只 setListItemObjs(1..5)) => 合法 difficulty 只有 1..5。
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        # ★v53: 显式区分"没传"与"传了非法值"
        #   没传 -> 按 1 档 (老口径, 向后兼容); 传了 0/负数/字符串 -> 走下面的 555
        _dif = req.get('difficulty')
        difficulty = int(_dif) if _dif not in (None, '') else 1
    except Exception:
        difficulty = 1
    if difficulty < 1 or difficulty > 5:
        return b'[555]'                       # 困难度错误 (客户端实际只会有 1..5 档)
    from handlers_game import (_role_info as _gi, _t25_today, _t25_today_row,
                               t25_war_group, t25_unlock_lv, war_group_guard)
    try:
        _rid, _mid = _t25_today()
    except Exception:
        _rid, _mid = (1001, 500231)
    _row = None
    try:
        _row = _t25_today_row()
    except Exception:
        _row = None
    # 该难度真正的战斗组 (T01_MstrGroup 行)
    _bid = t25_war_group(_row, difficulty)
    if _bid is None or not war_group_guard(_bid, '1017 LENGEND_CONSUME d=%d T25行=%s' % (difficulty, _rid)):
        # 解析不出来 -> 宁可回错误码(客户端弹提示, 人还留在妖神面板), 也**绝不**下发非法 id
        # (下发非法 id = 客户端永久卡 0%, 比报错糟得多)
        print('[LEGEND] 1017 无法解析战斗组: difficulty=%s T25行=%s row=%s'
              % (difficulty, _rid, 'None' if _row is None else 'ok'))
        return b'[555]'
    # 解锁等级门禁 (客户端 CLegendHome.lua:90-94 已按同一列拦过一次; 铁律 #157: 客户端拦了服务端也要拦)
    #   口径同 v49: **两边都取不到时放行**, 只在"都知道且不满足"时拒绝
    try:
        _need = t25_unlock_lv(_row, difficulty)
        if _need is not None and int(EQ_STATE.get('player_lv', 1)) < int(_need):
            print('[LEGEND] 1017 难度 %d 未解锁: lv=%s < UnlockLV=%s' %
                  (difficulty, EQ_STATE.get('player_lv', 1), _need))
            return b'[555]'
    except Exception:
        pass
    # msg[2] prop: 本处理器**不扣道具**, 所以给空表 [] —— 比给 null 更安全:
    #   Lua 里 `if prop then` 对空表为真 -> 进 SQProp:updateItem("", []) -> `tonumber(_info[3])` = nil
    #   -> 函数内部 cclog 后 return, **不改任何数据也不崩**。
    #   给 null 虽然客户端 `if prop then` 也会跳过, 但违反"成功响应不含 null"的契约铁律 (LAW-A)。
    return _json_out([_gi(), [], int(_mid), int(_bid)])


# ---------- 4068 / 4069 新版限时任务 (timeLimitedNewTask) ----------
#   客户端静态表绑定 (app/staticdata/SDLimitedTimeNewTask.lua:18):
#     usGetTableData(tUG_06_ActivityLimitTaskLis.tName, id)
#     → **UG_06_ActivityLimitTaskLis.json** (43 行, 没有 Activity_ID 列), 不是 3068 用的 A07_2!
#     UG_06 结构: (ID, Stage, Tittle, JumpTag, ConditionType, Value, Quantity, RewardID, Sort)
#     Stage == -1 的行 = "完成全部任务可领取" 大奖行 (UG_06 里是 10100, Quantity=42 = 7 阶段×6 条)
#   ⚠ 铁律 #22: saveList/saveFinishList 会 `getObject(SDLimitedTimeNewTask, taskid)`,
#     所以下发/接受的 taskid 必须是 UG_06 的真实行 id —— 给 0/编造 id 会 nil 索引崩 (3068 踩过)。
#   ⚠ 客户端 `math.floor(tasknum * 100 / value)` —— Quantity 为 0 会除零, 所以不能发 Quantity=0 的行。
_UG06 = {}         # stage -> [(tid, ct, qty)]
_UG06_BIG = 0
_UG06_IDS = set()
_UG06_REWARD = {}  # tid -> RewardID
try:
    _t = _load_json('UG_06_ActivityLimitTaskLis.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _tid = int(_r[_h['ID']] or 0)
            _stg = int(_r[_h['Stage']] or 0)
            _ct = int(_r[_h['ConditionType']] or 0)
            _qty = int(_r[_h['Quantity']] or 0)
            _UG06_REWARD[_tid] = int(_r[_h['RewardID']] or 0)
            if _stg < 0:
                _UG06_BIG = _tid
            else:
                _UG06.setdefault(_stg, []).append((_tid, _ct, _qty))
            _UG06_IDS.add(_tid)
        except Exception:
            continue
    for _k in _UG06:
        _UG06[_k].sort()
except Exception:
    pass


def _nlt_remain(today=None):
    """限时新手活动的剩余秒数（到当天 23:59:59）。

    ★2026-10-11：从 4068 的响应体里抽出 —— **4070 LIMITED_TIME_NEW_TASK_TIME**
      （主界面「新手限时」按钮的倒计时）要的是**同一个数**（客户端
      `VMainButton:getLimitTaskTime` → `m_limitTimes`）。抽出来共用，避免写第二份；
      两处口径不一致的话，按钮倒计时会和面板里的对不上。
    """
    today = today or time.strftime('%Y-%m-%d')
    try:
        end = time.mktime(time.strptime(today + ' 23:59:59', '%Y-%m-%d %H:%M:%S'))
        return max(0, int(end - time.time()))
    except Exception:
        return 86400


def handle_new_limited_time_task(body_json=None):
    # 4068 LIMITED_TIME_NEW_TASK_DATA (NetGetNewLimitedTimeTask.lua:23~75)
    #   msg[1] = [[[taskid,tasknum,taskstate], ...] x N]  每组 = 一个阶段 (客户端 pairs 外层)
    #   msg[2] = [大奖taskid, tasknum, taskstate]         ★必须是**单行** (客户端直接 task_config[1..3])
    #   msg[3] = 活动ID  msg[4] = 剩余时间  msg[5] = 今日开放阶段 (>7 客户端自行截到 7)
    #   ⚠ 客户端 refreshData 开头 `if not _timeLimitedTaskData or not _bigReward or not _StateID then return end`
    #     —— _StateID 不能为 0, 且三件都得给。
    #   进度口径复用 3068 的 a07_ct (同一套 ConditionType 枚举, 由每日任务驱动累计)
    st = EQ_STATE.setdefault('limited_task', {})
    today = time.strftime('%Y-%m-%d')
    if st.get('day') != today:
        st['day'] = today
        st['claimed'] = {}
    claimed = st.setdefault('claimed', {})
    groups = []
    stages = sorted(_UG06.keys())
    for g in stages:
        rows = []
        for (tid, ct, qty) in _UG06.get(g, []):
            num = _a07_progress(ct, qty, tid)
            if str(tid) in claimed:
                state = 2
            else:
                state = 1 if (qty and num >= qty) else 0
            rows.append([tid, num, state])
        if rows:
            groups.append(rows)
    # 今日开放阶段 = 第一个还有未完成任务的阶段; 全完成则取最后一个
    stage = stages[0] if stages else 1
    for _i, _g in enumerate(groups, 1):
        if any(r[2] != 2 for r in _g):
            stage = _i
            break
    else:
        stage = len(groups) if groups else 1
    if _UG06_BIG:
        done = sum(1 for _g in groups for _r in _g if _r[2] == 2)
        bstate = 2 if str(_UG06_BIG) in claimed else (
            1 if (done > 0 and all(_r[2] == 2 for _g in groups for _r in _g)) else 0)
        big = [_UG06_BIG, done, bstate]
    else:
        big = [0, 0, 0]
    remain = _nlt_remain(today)                     # ★与 4070 共用同一份计算（见 _nlt_remain）
    aid = _A07_CAT[0][0] if _A07_CAT else 1
    return _json_out([groups, big, aid, remain, stage])


def handle_new_limited_time_award(body_json):
    # 4069 LIMITED_TIME_NEW_TASK_AWARD (NetNewLimitedTimeReward.lua:27~50)
    #   req {taskid}; 响应 [1,[roleInfo(30字段), dropInfo]]
    #   taskid 必须在 UG_06 真表内 (防客户端拿旧版 3068 的 id 来领)
    from handlers_game import _role_info as _gi, _drop as _gd
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        tid = int(req.get('taskid') or 0)
    except Exception:
        return b'[0,{}]'
    if tid not in _UG06_IDS:
        return b'[543]'
    st = EQ_STATE.setdefault('limited_task', {})
    claimed = st.setdefault('claimed', {})
    if str(tid) in claimed:
        return b'[543]'                       # 已领取
    reward_gid = int(_UG06_REWARD.get(tid) or 0)
    if not reward_gid:
        return b'[543]'
    if tid != _UG06_BIG:
        ct = qty = 0
        for g in _UG06.values():
            for (t2, ct2, qty2) in g:
                if t2 == tid:
                    ct, qty = ct2, qty2
        if qty and _a07_progress(ct, qty, tid) < qty:
            return b'[543]'                   # 未达标
    from handlers_task import _grant_group
    groups, ok = _grant_group(reward_gid)
    if not ok:
        return b'[543]'
    claimed[str(tid)] = 1
    save_state()
    return _json_out([_gi(), _gd(groups)])
