# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— B 类残留协议批量实装 (v30 施工轮 4)
#
#   本文件收口 _audit_netmap.py 修正口径后的 B 类 13 条"客户端真会发"的协议。
#   按契约复杂度分两类：
#
#   【甲类 · 只需 [1,[]]】客户端 recvMessage **只判 statecode、不读任何 msg[N]**
#     100  PUSH_SHOP         NetPushShop       : 商城推送轮询确认
#     1010 AGGRESS_WAVE      NetAggressWave    : 妖怪入侵每波开始, 只回调
#     1011 AGGRESS_FORMATION NetAggressFormation: 妖怪入侵布阵, 只回调
#     1054 POINT_UPDATE      NetPointUpdate    : 小红点推送确认, 只回调
#     1071 SHARE             NetShare          : 分享回调, 只判 statecode
#     1234 MONSTER_DAMAGE    NetMonsterDamage  : 战斗伤害上报, 只判 statecode
#     1400 SELECT_LANGUAGE   NetChangeLanguage: 切换语言, 只回调
#     1052 LIMITGIFT_RECEIVE NetLimitGiftReceive: roleInfo + dropInfo + 回调
#
#   【乙类 · 需要真数据】
#     1043 ACHIEVEMENT_DETAIL NetAchievementDetail: msg[1]=[[id,groupid,value,state]]
#     1051 LIMITGIFT_LIST     NetLimitGiftList    : msg[1]=[[11字段]..]  ★含 /1000 时间戳
#     1288 AD_NOTICE          NetAdNotice         : msg[1]=[[7字段(第7项是子表)]..]
#     1810 FUND_ACT_LIST      NetFundActList      : msg[1]=[真活动id..]
#     2000 ACTIVITY_NOTICE    NetActivityNotice   : msg[1]=[[10字段]..]
#
#   两条铁律（本轮实测踩到）:
#     ① **时间戳一律毫秒**：1051 客户端做 `config[5]/1000`、`config[6]/1000 - getTime()`；
#        2000/1288 的时间字段直接进 UI。给秒会让 UI 显示 1970 或倒计时错乱。
#     ② **只读接口绝不建键**：所有 `_store*` 走 create=False 或延后到参数校验之后（见坑点 #76）。
import json, time
from state import EQ_STATE, save_state, load_table

from state import table_dir as _table_dir   # ★2026-10-02 (审计 §1.6): 表根不再写死盘符
_DATA = _table_dir()


def _load_json(name):
    # ★v60 闭环 v58 §10 缺口 #3: 改走 state.load_table —— 按「尾字段 u32 长度」精确剥离,
    #   不再用 c[:c.rfind(']')+1]（长度字段含 0x5D 时会切错 -> 调用方 except -> 静默空表,
    #   实测 L98_AIPriority / T32_Achievement）。护栏: work/_v60_legacy_tail_guard_test.py
    #   ⚠ 必须**逐行复制**: load_table 的结果是**带缓存的同一个对象**, 而旧实现每次返回全新对象。
    #     调用方若原地改行(排序/填字段)会污染全局缓存 -> 这里保持"每次都是新对象"的原语义。
    return [list(_r) for _r in load_table(name, _DATA)]


def _req(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    return req if isinstance(req, dict) else {}


def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _resp(*body):
    """[1,[body...]] —— 与 config_data._simple_resp 同构"""
    return ('[1,' + json.dumps(list(body), separators=(',', ':'), ensure_ascii=False) + ']').encode('utf-8')


def _ok():
    return b'[1,[]]'


def _rd_resp():
    """[1,[roleInfo, dropInfo]] —— 领取/结算类通用回包"""
    from handlers_game import _role_info, _drop
    return json.dumps([1, [_role_info(), _drop([])]], separators=(',', ':')).encode('utf-8')


# ============================================================
# 甲类：8 条只判 statecode / 只回调的协议
# ============================================================
def handle_push_shop(body_json=None):
    # 100: TipsManager 轮询商城是否有新推送 -> 只判 statecode, 不读字段
    return _ok()


def handle_aggress_wave(body_json=None):
    # 1010: req{wavenum,surparraynum,level,embattleformation,deadformation,pointid}
    #       客户端上报"本波上阵/阵亡", 服务端放行即回调; 错误码 524波数/525等级/526上阵/527已阵亡...
    req = _req(body_json)
    if req.get('wavenum') is None or req.get('pointid') is None:
        return b'[524]'                        # 当前波数错误
    st = EQ_STATE.setdefault('aggress_wave', {})
    if not isinstance(st, dict):
        st = {}
        EQ_STATE['aggress_wave'] = st
    st['last_wave'] = _i(req.get('wavenum'))
    st['pointid'] = _i(req.get('pointid'))
    st['n'] = _i(st.get('n', 0)) + 1
    save_state()
    return _ok()


def handle_aggress_formation(body_json=None):
    # 1011: 同 1010 结构（布阵）; 客户端 `string.gsub(test,"{}","[]")` 特殊处理空阵
    req = _req(body_json)
    if req.get('wavenum') is None or req.get('pointid') is None:
        return b'[524]'
    st = EQ_STATE.setdefault('aggress_wave', {})
    if not isinstance(st, dict):
        st = {}
        EQ_STATE['aggress_wave'] = st
    st['last_formation_wave'] = _i(req.get('wavenum'))
    st['form_n'] = _i(st.get('form_n', 0)) + 1
    save_state()
    return _ok()


def handle_point_update(body_json=None):
    # 1054: req{id} 小红点推送确认 -> 只回调。id 必须给(缺则客户端本就发了空)
    req = _req(body_json)
    if req.get('id') is None:
        return b'[0,{}]'
    st = EQ_STATE.setdefault('point_update', {})
    if not isinstance(st, dict):
        st = {}
        EQ_STATE['point_update'] = st
    st['last_id'] = _i(req.get('id'))
    save_state()
    return _ok()


def handle_share(body_json=None):
    # 1071: CaptureScreen 分享回调 -> 只判 statecode, 无字段
    return _ok()


def handle_monster_damage(body_json=None):
    # 1234: req{arr:[{staticId,attackNum,deffendNum}]} 战斗伤害上报 -> 只判 statecode
    req = _req(body_json)
    arr = req.get('arr')
    if not isinstance(arr, list):
        return b'[0,{}]'
    st = EQ_STATE.setdefault('monster_damage', {})
    if not isinstance(st, dict):
        st = {}
        EQ_STATE['monster_damage'] = st
    st['n'] = _i(st.get('n', 0)) + 1
    st['units'] = len(arr)
    st['ts'] = int(time.time())
    # ★★2026-10-25（独立审查 P1-4 的"战斗遥测"桶）：客户端**每场战斗结束都发这一包**
    #   （`WarStageUnits.lua:873/965` → `NetMonsterDamage.sendMessage(fightType, unitList, enemyList)`），
    #   字段是 `{fightType, arr:[{staticId, attackNum, deffendNum}], enemy:[...]}`：
    #     · `attackNum`  = 造成伤害（客户端 `damageCount`）
    #     · `deffendNum` = 承受伤害（`hurtDamageCount`）
    #   修前这里只记 n/units/ts 就把 arr 丢了 ⇒ UG 的 CT=49/53/54（"使用 X 战斗 N 场 / 造成 N 伤害 /
    #   抵挡 N 伤害"）34 行被判"无源"。现在按**静态 id**累计（跨进化阶段由 `_sched` 按 L91 同源汇总），
    #   每出现一次 = 该怪参与了这一场（`battles`）。
    #   ⚠ 这是客户端上报值（私服不校验战斗），只做非负与上限钳制，防一条脏包把账本刷成天文数字。
    per = EQ_STATE.setdefault('mon_dmg', {})
    if not isinstance(per, dict):
        per = EQ_STATE['mon_dmg'] = {}
    atk = per.setdefault('atk', {})
    dfn = per.setdefault('def', {})
    bat = per.setdefault('battles', {})
    for _r in arr[:64]:
        if not isinstance(_r, dict):
            continue
        _sid = _i(_r.get('staticId'), 0)
        if _sid <= 0:
            continue
        _k = str(_sid)
        _a = min(max(_i(_r.get('attackNum'), 0), 0), 1000000000)
        _d = min(max(_i(_r.get('deffendNum'), 0), 0), 1000000000)
        atk[_k] = _i(atk.get(_k), 0) + _a
        dfn[_k] = _i(dfn.get(_k), 0) + _d
        bat[_k] = _i(bat.get(_k), 0) + 1
    save_state()
    return _ok()


def handle_select_language(body_json=None):
    # 1400: req{language} 切换语言 -> 只回调。
    #   ⚠ 服务端**不切**语言（客户端资源是 CHS 单语言），只记录请求放行,
    #     否则客户端会去加载不存在的语言包。
    req = _req(body_json)
    lang = str(req.get('language') or '')
    if not lang:
        return b'[0,{}]'
    st = EQ_STATE.setdefault('language', {})
    if not isinstance(st, dict):
        st = {}
        EQ_STATE['language'] = st
    st['last'] = lang
    st['ts'] = int(time.time())
    save_state()
    return _ok()


def handle_limitgift_receive(body_json=None):
    # 1052: req{id, rewardgiftid(RC4密文), time}
    #   客户端 sendMessage 用 `ZZRc4.encodeEncrypt(rewardid, timeStamp)` 加密后上报。
    #   私服无计费 -> 限时充值礼包**永远不可达成**(currentpay=0) -> 一律拒绝, 不假装发放。
    #   错误码(客户端自带文案): 580 时间错误 / 581 密文错误 / 582 存在非法操作
    req = _req(body_json)
    if req.get('id') is None or req.get('rewardgiftid') is None:
        return b'[582]'                        # 存在非法操作
    _ts = _i(req.get('time'))
    if not _ts or abs(int(time.time()) - _ts) > 86400 * 7:
        return b'[580]'                        # 时间错误
    return b'[582]'                            # 未达成充值目标 -> 非法领取


# ============================================================
# 乙类 1: 1043 ACHIEVEMENT_DETAIL
# ============================================================
#   客户端 AchievementList.lua:284 `sendMessage(ACHIEVEMENT_DETAIL, data.id, lie, netCallFunc)`
#     -> req{"id":<SystemID>, "level":<LevelID>}
#     -> recvMessage: net_listAchieviment = msg[1];
#        每行 4 字段: [成就id, 成就组id, 完成值, 状态(0未完成/1未领取/2已完成)]
#        客户端按 state 重排(qulity 1/2/3)后回调
#   ⚠ 标题/描述/类型/奖励 全部由客户端查 T32 静态表拿, 服务端**只给进度**。
#   level 语义 = T32.LevelID (1初级/2中级/3高级); id = T32.SystemID (1001~1004)
_ACH_ROWS = {}          # (systemid, levelid) -> [ids...]


def _ach_bootstrap():
    if _ACH_ROWS:
        return
    try:
        t = _load_json('T32_Achievement.json')
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                sid = _i(r[h['SystemID']])
                lv = _i(r[h['LevelID']])
                aid = _i(r[h['ID']])
                if sid and lv and aid:
                    _ACH_ROWS.setdefault((sid, lv), []).append(aid)
            except Exception:
                continue
        for k in _ACH_ROWS:
            _ACH_ROWS[k].sort()
    except Exception:
        pass


def handle_achievement_detail(body_json=None):
    """1043 ACHIEVEMENT_DETAIL —— 单个成就**组**的明细。req `{id, level}`。

    ★★2026-10-02（本轮修复）**修前它对每一条成就恒返回 `value=0, state=0`**。
      根因：它读的是 `EQ_STATE['achievement'][成就id]['value'/'state']`，而**全仓没有任何
      地方写这两个键** —— 1042/1044 用的是另一套 schema（`handlers_achievement`：
      `counters['<Type>:<Value1>']` 记进度、`claimed['<成就id>']` 记已领）。
      两套 schema 并存 ⇒ 界面能渲染（形状对）、但**每一条都显示"未完成 0/需求"**。

    以哪套为准：**以 `handlers_achievement` 那套为准**（它是 1042/1044/1045 与
      `note_ach`/`ach_done`/`set_ach` 共用的、唯一有**写入点**的 schema）。
      本函数改为**派生**：`value` = 该成就的实时进度、`state` = 0/1/2。

    客户端契约（`network/task/NetAchievementDetail.lua` 逐列读，4 列）：
      `[成就id, 成就组id, 完成值, 状态]`，状态 0=未完成 / 1=未领 / 2=已领；
      客户端还**按状态重排**（可领 -> 未完成 -> 已领），所以 state 必须真。
    """
    req = _req(body_json)
    sid = _i(req.get('id'))
    lv = _i(req.get('level'))
    if not sid:
        return b'[540]'                        # 成就组id错误
    if lv not in (1, 2, 3):
        return b'[541]'                        # 成就组等级错误
    _ach_bootstrap()
    ids = _ACH_ROWS.get((sid, lv)) or []
    try:
        import handlers_achievement as HA
        HA._bootstrap()
        _tbl = HA._T32 or {}
    except Exception:
        HA, _tbl = None, {}
    rows = []
    for aid in ids:
        value, state = 0, 0
        if HA is not None:
            try:
                # ⚠ `_T32` 的键是**字符串**成就 id（`handlers_achievement` 建表时 str 化）
                row = _tbl.get(str(aid))
                if row is not None:
                    value = int(HA.ach_value(row) or 0)
                    if str(aid) in (HA._ach().get('claimed') or {}):
                        state = 2                        # 已完成（已领）
                    elif HA.ach_done(row):
                        state = 1                        # 未领取
            except Exception:
                value, state = 0, 0
        rows.append([aid, sid, value, state])
    return _resp(rows)


# ============================================================
# 乙类 2: 1051 LIMITGIFT_LIST 限时充值礼包
# ============================================================
#   客户端 recvMessage 逐字段（**注意 /1000 与倒计时**）:
#     [1]id [2]menutitle [3]title [4]described
#     [5]starttime = (config[5] or 0)/1000      ★ 毫秒
#     [6]endtime   = (config[6] or 0)/1000      ★ 毫秒
#     [7]currentpay [8]targetpay [9]rewardgiftid [10]csbimg
#     [11]isreceived  0未完成 1完成未领取 2已领取
#     然后 `day = getClockDetailStrByTime(config[6]/1000 - getTime())` -> endtime 必须 > now
#
#   ★ 私服策略 = 与充值族一致「可看不可买」: **没有计费通道 => currentpay 恒 0 => 永不达成**。
#     故返回**空列表** —— 客户端 `if #giftData > 0` 判空后 mState=false，入口自然隐藏,
#     不会出现"永远倒计时却永远领不到"的诱导界面。
def handle_limitgift_list(body_json=None):
    return _resp([])


# ============================================================
# 乙类 3: 1288 AD_NOTICE 广告位/公告轮播
# ============================================================
#   客户端 recvMessage 逐字段:
#     row = [ [1]id(客户端再 +100) [2]bannerType [3]note [4]starttime [5]endtime
#             [6]priority [7]<子表: 每项8字段> ]
#     子表字段: [1]url [2]title [3]des [4]skiptype [5]goid [6]noticetype
#               [7]monsterid [8]skinid(0->nil)
#     ⚠ 子表若为空, `for k,listdata in pairs(config[7])` 不执行 ->
#       data.url/title/des 全是 nil -> UI 取 nil 文本。故每条必须带**至少一个真子项**。
#   数据源: A01_Activity (官方活动公告, Begin/Over 为 YYYYMMDDHHMMSS) -> 毫秒。
def _a01_rows():
    out = []
    try:
        t = _load_json('A01_Activity.json')
        h = {c: k for k, c in enumerate(t[0])}

        def g(r, c, d=''):
            return r[h[c]] if c in h and h[c] < len(r) else d
        for r in t[1:]:
            try:
                out.append({
                    'id': _i(g(r, 'ID')),
                    'title': str(g(r, 'Tittle')),
                    'desc': str(g(r, 'Desc')),
                    'begin': str(g(r, 'Begin')),
                    'over': str(g(r, 'Over')),
                })
            except Exception:
                continue
    except Exception:
        pass
    return out


def _ymd_to_ms(s, default=None):
    """'20180810000001' -> epoch 毫秒; 解析失败返回 default"""
    s = str(s or '')
    if len(s) < 14:
        return default
    try:
        tm = time.mktime(time.strptime(s[:14], '%Y%m%d%H%M%S'))
        return int(tm * 1000)
    except Exception:
        return default


def handle_ad_notice(body_json=None):
    rows = []
    now_ms = int(time.time() * 1000)
    for i, a in enumerate(_a01_rows()[:5]):
        st_ms = _ymd_to_ms(a['begin'], now_ms)
        ov_ms = _ymd_to_ms(a['over'], now_ms + 365 * 86400 * 1000)
        rows.append([
            a['id'],                                   # [1] id (客户端 +100)
            1,                                         # [2] bannerType 1=横幅
            a['desc'] or a['title'],                   # [3] note
            st_ms,                                     # [4] starttime(毫秒)
            ov_ms,                                     # [5] endtime(毫秒)
            100 - i * 10,                              # [6] priority(客户端按此降序)
            [[                                         # [7] 子表(至少 1 项, 否则全 nil)
                '',                                    # [1] url
                a['title'],                            # [2] title
                a['desc'] or a['title'],               # [3] des
                3,                                     # [4] skiptype 3=不跳转
                '',                                    # [5] goid
                1,                                     # [6] noticetype 1=文本
                0,                                     # [7] monsterid
                0,                                     # [8] skinid (0 -> 客户端置 nil)
            ]],
        ])
    return _resp(rows)


# ============================================================
# 乙类 4: 1810 FUND_ACT_LIST 基金活动列表
# ============================================================
#   客户端 NetFundActList:recvMessage:
#     openActIDs = msg[1]; table.sort(tonumber 升序); emit(FUND_ACT_LIST, openActIDs)
#   然后 VFundActHome: `requestInfoMsg(self.m_openActIDs[1], ...)` 用 **该 id 查 tA31_1_FundActivities**
#   (SDFundActivities.lua:19) -> ⚠ 必须是 A31_1 的**真 ID**, 编造 id 会让 SD 对象建不出来。
#   按 A31_1.LimitLv 过滤（0 = 不限）。
_FUND_ROWS = []


def _fund_bootstrap():
    if _FUND_ROWS:
        return
    try:
        t = _load_json('A31_1_FundActivities.json')
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                fid = _i(r[h['ID']])
                lv = _i(r[h['LimitLv']]) if 'LimitLv' in h else 0
                # ★2026-11-07（全付费回滚）：A31_1 里 PriceType=1 是人民币商品（6031 成长基金 ¥98）。
                #   商城/充值已回滚为“不可购买”，基金页也不该再把它列出来（否则点进去只能静默失败）。
                pt = _i(r[h['PriceType']]) if 'PriceType' in h else 0
                if pt == 1:
                    continue
                if fid:
                    _FUND_ROWS.append((fid, lv))
            except Exception:
                continue
        _FUND_ROWS.sort()
    except Exception:
        pass


def handle_fund_act_list(body_json=None):
    _fund_bootstrap()
    lv = _i(EQ_STATE.get('player_lv', 1))
    ids = [fid for (fid, need) in _FUND_ROWS if (not need) or lv >= need]
    return _resp(ids)


# ============================================================
# 乙类 5: 2000 ACTIVITY_NOTICE 活动公告列表
# ============================================================
#   客户端 recvMessage: noticeServerDataList = msg[1]; 每行 10 字段:
#     [1]id [2]type(1文本/2图片/3文本+图片) [3]createtime [4]overtime
#     [5]title [6]subtitle [7]desc [8]url [9]urlimg [10]isActivate
#   数据源: A01_Activity 真表。type=3(有标题+描述), url/urlimg 留空(避免去下载不存在的图)。
def handle_activity_notice(body_json=None):
    now_ms = int(time.time() * 1000)
    rows = []
    # ★2026-11-07：免责声明固定放第一条（游戏内「公告」随时可读；客户端默认选中第一条）。
    #   文案唯一来源 = `work/disclaimer.py`；服务端下发，不依赖客户端 UI。
    try:
        import disclaimer as _disc
        rows.append(_disc.notice_row(now_ms, now_ms + 10 * 365 * 86400 * 1000))
    except Exception as _e:
        print('[NOTICE] 免责声明行构造失败: %r' % (_e,), flush=True)
    for a in _a01_rows():
        st_ms = _ymd_to_ms(a['begin'], now_ms)
        ov_ms = _ymd_to_ms(a['over'], now_ms + 365 * 86400 * 1000)
        rows.append([
            a['id'],                                   # [1] id
            3,                                         # [2] type 3=文本+图片
            st_ms,                                     # [3] createtime(毫秒)
            ov_ms,                                     # [4] overtime(毫秒)
            a['title'],                                # [5] title
            '',                                        # [6] subtitle
            a['desc'] or a['title'],                   # [7] desc
            '',                                        # [8] url
            '',                                        # [9] urlimg (留空, 不触发下载)
            1,                                         # [10] isActivate
        ])
    return _resp(rows)
