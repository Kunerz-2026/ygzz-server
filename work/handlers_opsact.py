# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 运营活动族 (登录活动 / 充值活动 / 单笔充值 / 消费活动 / 活动引导 / 基金)
#   ★2026-09-12 (v26 P1) 新增实装。这批 msgid 在此之前**全部未处理** ->
#   落 config_data.resp_for 兜底 [1,[[]]] -> 客户端 recvMessage 里 `pairs(msg[N])`(N>=5) 拿到 nil
#   -> Lua 抛错 -> SocketManager 的 socket recvMessage 无 pcall -> **整条 socket tick 中断 -> 界面卡死**。
#   （v25 §10.4.2 A 类表里的 1109/1113/1115/1150/1169/1811 就是它们）
#
#   客户端契约 (逐行取自 client Lua/app/network/notice/, 每个 handler 头部注明行号):
#     1113 ACTIVIRY_LOGIN           NetActivityLogin           req{id}   -> 活动Data列表
#     1114 ACTIVIRY_LOGIN_RECEIVE   NetActivityLoginReceive    req{id,taskid} -> [roleInfo, dropInfo]
#     1115 ACTIVIRY_RECHARGE        NetActivityRecharge        req{id}   -> 活动Data列表
#     1116 ACTIVIRY_RECHARGE_RECEIVE NetActivityRechargeReceive req{id,taskid} -> [roleInfo, dropInfo]
#     1150 ACTIVIRY_ONE_RCG_INFO    NetActivityOneRcgInfo      req{id}   -> list
#     1151 ACTIVIRY_ONE_RCG_RECV    NetActivityOneRcgRecv      req{id,taskid} -> [roleInfo, dropInfo]
#     1169 ACTIVITY_CONSUME_INFO    NetActivityConsumeInfo     req{id,type} -> list
#     1170 ACTIVITY_CONSUME_RECEIVE NetActivityConsumeReceive  req{id,taskid,type} -> [roleInfo, dropInfo]
#     1109 RECHARGE_ACTIVITY        NetRechargeActivity        req{id}   -> normaReward(17字段) + totalReward
#     1112 ACTIVIRY_GUIDE           NetActivityGuide           -> (skipid, img)  [只需给非 nil]
#     1811 FUND_ACT_INFO            NetFundActInfo             req{id}   -> (id, buyStatus, taskList)
#     1812 FUND_ACT_GET_REWARD      -> [roleInfo, dropInfo]
#     1813 FUND_ACT_BUY             -> [roleInfo]
#
#   ⚠ 本模块的**核心判定**: 这批活动都是"充值/消费/召唤"型 —— 私服没有真实计费,
#     所以进度口径统一取 `state.task_state` 里已有的对应计数 (召唤次数/消费额/金币流转),
#     拿不到就给 0 (状态 0 未完成), **绝不编造"已完成"**, 免得用户点了领奖却没有奖励依据。
import json, time
from state import EQ_STATE, save_state, _DIR

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


# ---------- A01_Activity / A01_1_Activitycontent (与 handlers_activity 同源, 独立加载避免循环 import) ----------
_A01 = {}       # id -> (Tittle, Desc, Begin, Over)
_A01_1 = {}     # group -> [(contentId, target, rewardGroupId)]

def _bootstrap():
    if _A01:
        return
    try:
        t = _load_json('A01_Activity.json')
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                _A01[_i(r[h['ID']])] = (str(r[h['Tittle']] or ''), str(r[h['Desc']] or ''),
                                        str(r[h['Begin']] or '20180810000001'),
                                        str(r[h['Over']] or '20291231235959'))
            except Exception:
                continue
    except Exception:
        pass
    try:
        t = _load_json('A01_1_Activitycontent.json')
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                g = str(r[h['Group']])
                _A01_1.setdefault(g, []).append((_i(r[h['ID']]), _i(r[h['Target']]), _i(r[h['Reward']])))
            except Exception:
                continue
        for g in _A01_1:
            _A01_1[g].sort()
    except Exception:
        pass


def _act_meta(aid, default_desc=''):
    _bootstrap()
    t, d, b, o = _A01.get(int(aid), ('', default_desc, '20180810000001', '20291231235959'))
    # 客户端 startDateTime/endDateTime 是**字符串** ("20180810000001" 这类 14 位); 原样下发
    return d or default_desc, b, o


# ---------- 进度口径 (只用真实存在的计数, 拿不到就是 0) ----------
def _ts():
    v = EQ_STATE.get('task_state')
    return v if isinstance(v, dict) else {}


def _draw_count():
    # 召唤次数: 抽卡计数 (draw 表由 handlers_draw 维护)
    d = EQ_STATE.get('draw') or {}
    for k in ('total', 'count', 'draw_count'):
        if k in d:
            return _i(d.get(k))
    return 0


def _consume_count():
    # 消费额: gold_flow.spend (T37 每日消耗金币; 与"消费活动"口径一致)
    gf = EQ_STATE.get('gold_flow') or {}
    return _i(gf.get('spend'))


def _recharge_count():
    # 充值额: 私服无计费 -> 恒 0 (状态 0 未完成; 不编造)
    return _i((EQ_STATE.get('recharge') or {}).get('total'))


# 活动组 -> 进度口径。★必须与 A01_Activity 的**真实标题**对应, 不能按组号大小猜;
#   并且进度值必须与 A01_1.TargetType 的量纲一致 (下表 TargetType 是直读 A01_1 得到的):
#     g=101 TargetType=1  累计登陆天数      -> login7.days            (真实, 用)
#     g=103 TargetType=14 召唤次数          -> draw 计数              (无记录 -> 0)
#     g=104 TargetType=8  玩家等级          -> player_lv              (真实, 用)
#     g=105 TargetType=9  妖怪进化阶        -> 无阶数计数 -> 0
#     g=106 TargetType=2  **故事ID**(100/101..) 不是"次数"!  -> 无剧情进度 -> 0
#     g=107 TargetType=5  妖塔层数          -> tower.plies            (真实, 用)
#     g=108 TargetType=3  **关卡/章节ID**(1011/2011..)  -> 无进度 -> 0
#     g=109 TargetType=13 **妖神封印ID**(500131..)      -> 未实装 -> 0
#     g=110 TargetType=6  **妖魔异闻ID**(101/102..)     -> 无进度 -> 0
#     g=111 TargetType=7  对战连胜数        -> pvp 连胜计数           (无记录 -> 0)
#   ⚠ 教训: 106/108/110 的 Target **不是次数而是 ID**, 用"次数"去比会大面积误判"已完成"。
#   凡是没有真实计数口径的, 一律返回 0 —— 宁可让用户看到"未完成", 也不能给假的可领取状态
#   (点了领奖却没有发放依据, 是比空面板更难排查的一类 bug, 见 v25 铁律 #28)。
#
# ★v60 实测订正 (2026-09-16) —— v59 文档说"103/111 只差一行, 取数函数已写好"，**只对了一半**:
#   · g=111 对战连胜: `_pvp_streak()` 读 pvp.streak, 而 handlers_pvp.py:194 确实在维护它
#       (`p['streak'] += 1` / `streak_max`) ⇒ **真的只差一行**。已接。
#   · g=103 召唤次数: `_draw_count()` 读 draw['total'|'count'|'draw_count'],
#       但实测存档的 draw = {ticket,luck,five_ensure,free_day,free_used} ——
#       **三个键一个都没有**, handlers_draw 也从不写它 ⇒ 取数函数"写好了"但**计数源根本不存在**,
#       只改映射仍然恒 0。已在 handlers_draw 里补真实的累计计数(见该文件 _bump_draw_count)。
#   · g=106 主线剧情: Target **是故事ID**(100/101..)，而存档里有真实记录
#       `story_seen` = {'100'..'104'} ⇒ 应该用**集合成员判定**而不是 prog>=target。已接。
#   · g=105 怪物进化: A01_1 该组 Desc 实测是"**首次进化到2/3/4/5阶**"(不是次数!)
#       ⇒ 正确口径 = "有没有把某只怪进化到第 N 阶"，而**不是**"累计进化次数"。
#         v60 已接: 计数源 = EQ_STATE['evolve']['max_stage']，由 handlers_game 的 6003
#         进化成功路径在**真的发生进化时**写入（不从背包星级反推 —— 反推会把"召唤到手就是高星"
#         也算成"进化过", 那就是假的可领取状态）。
#   · g=108 探索关卡 / g=109 击杀妖神 / g=110 妖魔异闻: A01_1 的 Desc 是"普通-<怒目鬼>"这类
#       **具体关卡/BOSS 名**，Target 是关卡/妖神/异闻 ID ⇒ 需要"已完成 id 集合"，
#       而存档里**没有**任何这类记录（story_seen 只覆盖剧情故事ID，不含关卡） ⇒ 仍然返回 0。
#       要做就得先在对应战斗结算处**新增**一张"已击杀 id 集合"的记录表（真功能，不是一行）。
_GROUP_KIND = {
    '101': 'login',
    '103': 'draw',
    '104': 'level',
    '105': 'evolvestage',  # 见上: 口径="进化到第N阶", 计数源 evolve.max_stage
    # ★ 下面 4 组的 Target **是 ID 而不是次数** -> 不走 `prog >= target`, 而是走 **_DONE_SETS
    #   的集合成员判定(见 _content_state)。这里标成 'set' 而不是旧的 'zero',
    #   免得后来人把它当成"还没实现"又去"修"一遍(_progress_of 对 'set' 返回 0, 但那个值不会被用)。
    '106': 'set',          # 主线剧情: story_seen
    '107': 'tower',
    '108': 'set',          # 探索关卡: battle_cleared(T01 战斗组)
    '109': 'set',          # 妖神封印: battle_cleared(T01 战斗组, 5xxxxx 妖神难度段)
    '110': 'set',          # 妖魔异闻: battle_cleared -> IDGroup(T24_YMYW.BattleId)
    '111': 'pvpstreak',
}

# ---------- "已完成 id 集合"口径 (Target 是 ID 而不是次数的那些组) ----------
def _story_seen_set():
    """主线剧情已看过的故事ID集合 (TargetType=2)。

    ★ 存档形状实测: state.json 里 `story_seen` 是 **dict** {'100':..., '101':...}。
      这里 dict/list/set 三种都容忍 —— 旧档/新档形状不一样时不至于整组变 0。
    """
    v = EQ_STATE.get('story_seen')
    if isinstance(v, dict):
        return set(str(k) for k in v)
    if isinstance(v, (list, tuple, set)):
        return set(str(x) for x in v)
    return set()


_DONE_SETS = {
    '106': _story_seen_set,      # 主线剧情: story_seen 的真实记录
    '108': None,                 # 探索关卡: 见下面 _cleared_groups（在定义后绑定）
    '109': None,                 # 妖神封印: 同上（Target 也是 T01_MstrGroup 行 id）
    '110': None,                 # 妖魔异闻: 见下面 _ymyw_done_groups
}


def _cleared_groups():
    """已通关的 **T01_MstrGroup 战斗组 id** 集合 —— A01 活动组 108 / 109 的计数源。

    ★v60 实测口径（三个活动共用"这场仗打的哪个战斗组"这一件事）:
      · 108 探索寻BOSS: Target **就是 T01_MstrGroup 行 id**(1011 <怒目鬼> / 2012 …),
        Desc "普通-<怪名>" -> "把这一关的 BOSS 打掉"。
      · 109 妖神封印  : Target **也是 T01_MstrGroup 行 id**，而且是 5xxxxx 段 =
        "妖神难度组"（逐行核对: 500131="<毕方>难度1" / 500241="<涂山九尾>难度1" …），
        Desc 正是"击杀毕方/刑天/腾蛇/帝江…"。
      钩子在 handlers_game.handle_war_result: **胜利**时把 `req['info']`(本场战斗组 id) 记进来。
      该函数只服务 9001/9003/9005/9006/9007 五个战场（9004 妖塔走另一个 handler），
      这五个模式的客户端都送 `msg["info"]=_warInfo`（NetWarResult/NetPVEResult/NetBossResult 已核对）。
    """
    v = EQ_STATE.get('battle_cleared')
    if isinstance(v, dict):
        return set(str(k) for k in v)
    if isinstance(v, (list, tuple, set)):
        return set(str(x) for x in v)
    return set()


# ---- 110 妖魔异闻: Target 是 T24_YMYW.**IDGroup**(101..107), 不是战斗组 id ----
#   实证: 每个 IDGroup 的 BattleId **14/14 都是** T01_MstrGroup 真实行(30001/30101/…)。
#   ⇒ "该 IDGroup 是否已通关" = "它名下任一 BattleId 已通关"。
#   所以这里把"已通关的战斗组集合"换算成"已通关的 IDGroup 集合"，复用同一套集合成员判定。
_YMYW_BATTLE = {}          # IDGroup(str) -> set(BattleId str)
try:
    _ty = _load_json('T24_YMYW.json')
    _hy = {c: k for k, c in enumerate(_ty[0])}
    for _r in _ty[1:]:
        try:
            _YMYW_BATTLE.setdefault(str(_r[_hy['IDGroup']]), set()).add(str(_r[_hy['BattleId']]))
        except Exception:
            continue
except Exception as _e:
    print('[ACT110] !! T24_YMYW 读取失败, 该组进度恒 0: %r' % (_e,), flush=True)


def _ymyw_done_groups():
    """已通关的妖魔异闻 IDGroup 集合（A01 活动组 110 的计数源）。"""
    if not _YMYW_BATTLE:
        return set()
    cleared = _cleared_groups()
    return {g for g, bids in _YMYW_BATTLE.items() if bids & cleared}


_DONE_SETS['108'] = _cleared_groups
_DONE_SETS['109'] = _cleared_groups
_DONE_SETS['110'] = _ymyw_done_groups


def _tower_plies():
    return _i((EQ_STATE.get('tower') or {}).get('plies'))


def _pvp_streak():
    p = EQ_STATE.get('pvp') or {}
    for k in ('streak', 'win_streak', 'currentStreak'):
        if k in p:
            return _i(p.get(k))
    return 0


def _evolve_stage():
    """(首次)进化到 N 阶 —— A01 活动组 105 的进度口径。

    ★v60 实测口径: A01_1 g=105 的 Desc 是"**首次进化到2/3/4/5阶**"(Target 2/3/4/5),
      所以它**不是"累计进化次数"**, 而是"有没有把某只怪进化到第 N 阶"。
      计数源 = `EQ_STATE['evolve']['max_stage']`, 由 handlers_game 的 6003 进化成功路径
      在**真的发生进化时**写入（不从背包星级反推 —— 反推会把"召唤到手就是高星"也算进去,
      那是假的可领取状态）。
    """
    e = EQ_STATE.get('evolve')
    if isinstance(e, dict):
        return _i(e.get('max_stage', 0))
    return 0


def _progress_of(kind):
    return {'login': lambda: max(1, _i((EQ_STATE.get('login7') or {}).get('days', 1))),
            'draw': _draw_count,
            'consume': _consume_count,
            'recharge': _recharge_count,
            'level': lambda: _i(EQ_STATE.get('player_lv', 1)),
            'tower': _tower_plies,
            'evolvestage': _evolve_stage,
            'pvpstreak': _pvp_streak,
            'zero': lambda: 0}.get(kind, lambda: 0)()


def _claimed_key(aid):
    # 已领内容 id 列表 (逐活动存, 与 1114/1116/1151/1170 共用)
    rc = EQ_STATE.setdefault('act_claimed', {})
    v = rc.get(str(aid))
    if not isinstance(v, list):
        v = []
        rc[str(aid)] = v
    return v


def _evolve_count():
    """（保留给下一轮 g=105 用）"最高进化阶"口径的占位；**当前未接线**。
    实测 A01_1 g=105 的 Desc 是"首次进化到2/3/4/5阶"，所以正确实现是
    "背包里所有妖怪的 L91.Star 最大值"，而不是一个累计计数器。
    没接线时 _GROUP_KIND['105'] 仍是 'zero' —— 宁可显示未完成，也不发假的可领取状态。"""
    return 0


def _content_state(aid, cid, target, prog):
    # 0 未完成 / 1 未领取 / 2 已领取 (客户端 ActivityState 枚举)
    if cid in _claimed_key(aid):
        return 2
    # ★ Target 是 **ID** 的组: 必须用"该 id 是否已完成"判定, 不能拿 prog>=target 数字比
    #   (106 的 Target 是 100/101.. 的故事ID; 拿"次数"比会把 target=100 判成"要 100 次")
    done = _DONE_SETS.get(str(aid))
    if done is not None:
        return 1 if str(target) in done() else 0
    return 1 if prog >= target else 0


def _activity_data(aid, limit=32):
    """NetActivityLogin.lua:41~58 的 config 行 (21 字段)
       [1]ID(=A01_1.ID=contentId) [2..16]五组(itemid,itemnum,itemtype)
       [17]rewardState(★与 [1] 对应) [18]title [19]startTime [20]endTime [21]subtitle
       ⚠ [17] 的状态位必须与 [1]=contentId 对应, 不能借用 A01_Activity.ID
       ⚠ A01_1 各组行数 3~32, 不能按固定 21 截断 (组 108 有 32 行)
    """
    _bootstrap()
    grp = str(aid)
    rows = _A01_1.get(grp) or _A01_1.get('101', [])
    desc, begin, over = _act_meta(aid)
    kind = _GROUP_KIND.get(grp, 'login')
    prog = _progress_of(kind)
    out = []
    for (cid, target, reward_gid) in rows[:limit]:
        row = [cid] + [0] * 15 + [_content_state(aid, cid, target, prog),
                                  desc, begin, over, '']
        out.append(row)
    return out


# ============================ 1113 / 1114 登录活动 ============================
def handle_activity_login(body_json):
    # NetActivityLogin.recvMessage: msg[1] description / msg[2] imgURL
    #   msg[3] startDateTime / msg[4] endDateTime / msg[5] Data(pairs!)
    req = _req(body_json)
    aid = _i(req.get('id'), 101) or 101
    desc, begin, over = _act_meta(aid, '累计登陆奖励')
    data = _activity_data(aid)
    return _out(desc, '', begin, over, data)


def handle_activity_login_receive(body_json):
    # NetActivityLoginReceive.recvMessage: msg[1] roleInfo(30字段) / msg[2] dropInfo
    return _generic_receive(body_json, default_aid=101)


# ============================ 1115 / 1116 充值活动 ============================
def handle_activity_recharge(body_json):
    # NetActivityRecharge.recvMessage: 同 1113 结构, 但 [19]currentRecharge / [21]totalRecharge
    req = _req(body_json)
    aid = _i(req.get('id'), 103) or 103
    desc, begin, over = _act_meta(aid, '充值活动')
    rows = _activity_data(aid)
    for r in rows:
        r[18] = _recharge_count()          # currentRecharge
        r[20] = 0                          # totalRecharge (私服无计费)
    return _out(desc, '', begin, over, rows)


def handle_activity_recharge_receive(body_json):
    return _generic_receive(body_json, default_aid=103)


# ============================ 1150 / 1151 单笔充值 ============================
def handle_one_rcg_info(body_json):
    # NetActivityOneRcgInfo.recvMessage: msg[1]desc msg[2]img msg[3]start msg[4]end
    #   msg[5] list, 每行 21 字段: [id, item1id,num,type, item2..., item5..., targetnum,
    #                              Finishnum2, rewardnum, title, desc]
    req = _req(body_json)
    aid = _i(req.get('id'), 105) or 105
    desc, begin, over = _act_meta(aid, '单笔充值')
    _bootstrap()
    lst = []
    for (cid, target, reward_gid) in (_A01_1.get(str(aid)) or [])[:21]:
        st = _content_state(aid, cid, target, _recharge_count())
        lst.append([cid, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                    target, 0, st, '单笔充值', desc])
    return _out(desc, '', begin, over, lst)


def handle_one_rcg_receive(body_json):
    return _generic_receive(body_json, default_aid=105)


# ============================ 1169 / 1170 消费活动 ============================
def handle_consume_info(body_json):
    # NetActivityConsumeInfo.recvMessage: msg[1]desc msg[2]img msg[3]start msg[4]end
    #   msg[5] rounds  msg[6] gradeNum(玩家当前数值)  msg[7] list
    #   行 17 字段: [id, item1..item4 (id,num,type), rewardState, title, desc, grade]
    req = _req(body_json)
    aid = _i(req.get('id'), 106) or 106
    desc, begin, over = _act_meta(aid, '消费活动')
    _bootstrap()
    cur = _consume_count()
    lst = []
    for (cid, target, reward_gid) in (_A01_1.get(str(aid)) or [])[:21]:
        lst.append([cid, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                    _content_state(aid, cid, target, cur), '消费活动', desc, target])
    return _out(desc, '', begin, over, 1, cur, lst)


def handle_consume_receive(body_json):
    return _generic_receive(body_json, default_aid=106)


# ============================ 1109 充值活动(累天) ============================
def handle_recharge_activity(body_json):
    # NetRechargeActivity.recvMessage: msg[1]desc msg[2]img msg[3]start msg[4]end
    #   msg[5] limitNum(今日充值阀值) msg[6] totalNum(今日充值数)
    #   msg[7] = 17 字段: [1]normaRewardID [2..16]五组(itemid,itemnum,itemtype) [17]normaRewardState
    #   msg[8] = 累天奖励列表 (pairs!) 每行同 17 字段结构 + [1]=ID + [17]=rewardState
    req = _req(body_json)
    aid = _i(req.get('id'), 107) or 107
    desc, begin, over = _act_meta(aid, '累计充值')
    cur = _recharge_count()
    norma = [0] * 16 + [0]
    _bootstrap()
    rows = _A01_1.get(str(aid)) or []
    if rows:
        norma[0] = rows[0][0]
        norma[16] = 1 if cur >= rows[0][1] else 0
    total = []
    for (cid, target, reward_gid) in rows[:21]:
        row = [cid] + [0] * 15 + [1 if cid in _claimed_key(aid) else (1 if cur >= target else 0)]
        total.append(row)
    return _out(desc, '', begin, over, 0, cur, norma, total)


# ============================ 1112 活动引导 / 1811~1813 基金 ============================
def handle_activity_guide(body_json=None):
    # NetActivityGuide.recvMessage: msg[1]img  msg[2]skipid -> 回调 (skipid, img)
    #   ⚠ 这里**不能**给 nil: 客户端把 skipid 直接塞进跳转逻辑 (实测本轮真机发过一次 1112)
    return _out('', 0)


# ============================================================================
# ★★2026-10-16（任务书 C③）：基金 1811/1812/1813 —— 三处**取数源**都错了
# ----------------------------------------------------------------------------
# 修前：
#   · `1811` 用 `A01_1_Activitycontent`（活动内容表）按基金 id 查任务 —— 基金的活动内容
#     根本不在那张表里 ⇒ **恒空任务列表**（页面打开就是空的）；
#   · `1812` 只读 `taskid`/`id2`，而客户端基金页发的是 `grade`（A31_2_FundReward.ID）⇒ 永远 `[543]`；
#   · `1813` 买基金**不扣费**，直接把 `fund[aid]=1`（点一下就"已购买"）。
# 正确的表（两张都在表根，逐列核对过）：
#   `A31_1_FundActivities(ID, PriceType, Price, LimitLv, RewardID)`
#       6031 成长基金 PriceType=1 Price=98   RewardID=10001
#       6041 妖魂基金 PriceType=2 Price=500  RewardID=10002
#       6061 鬼道基金 PriceType=5 Price=50000 RewardID=10004
#   `A31_2_FundReward(ID, GroupID, RewardType, PageName, TargetType, TaskDesc, TaskValue, Reward)`
#       GroupID == A31_1.RewardID；ID = 界面上那一档（客户端叫 `grade`）；
#       TaskValue = 达标值；Reward = T02_Reward 组。
# 价格口径（DECISIONS 2026-10-16）：6031 是 `PriceType=1`（¥98，私服无支付通道）
#   ⇒ 沿用商城既有的 **RMB→妖魂 1:1**，即 98 妖魂；6041 表里就是妖魂 500（Desc 文案写 3000，
#   以**列**为准并在此注明）；6061 表里是鬼道积分 50000。
# ============================================================================
_FUND_ACT = {}       # aid -> (priceType, price, limitLv, rewardGroupId)
_FUND_TASK = {}      # (group, grade) -> (targetType, taskValue, rewardGroup)
_FUND_BY_GROUP = {}  # group -> [(grade, targetType, taskValue, rewardGroup)]
try:
    _t = _load_json('A31_1_FundActivities.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _FUND_ACT[_i(_r[_h['ID']])] = (_i(_r[_h['PriceType']], 1), _i(_r[_h['Price']], 0),
                                          _i(_r[_h['LimitLv']], 0), _i(_r[_h['RewardID']], 0))
        except Exception:
            continue
except Exception:
    pass
try:
    _t = _load_json('A31_2_FundReward.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _g = _i(_r[_h['GroupID']], 0)
            _grade = _i(_r[_h['ID']], 0)
            _row = (_i(_r[_h['TargetType']], 1), _i(_r[_h['TaskValue']], 0), _i(_r[_h['Reward']], 0))
            _FUND_TASK[(_g, _grade)] = _row
            _FUND_BY_GROUP.setdefault(_g, []).append((_grade,) + _row)
        except Exception:
            continue
    for _g in _FUND_BY_GROUP:
        _FUND_BY_GROUP[_g].sort()
except Exception:
    pass


def _fund_prog(target_type):
    """A31_2.TargetType -> 当前进度。

    1 = 玩家等级（`TaskDesc` 全是"等级达到 N 级可领取"）；其余 TargetType 本版**无读数路径**
    ⇒ 回 0（不编进度；宁可显示未完成）。
    """
    if int(target_type or 0) == 1:
        try:
            return int(EQ_STATE.get('player_lv', 1) or 1)
        except (TypeError, ValueError):
            return 1
    return 0


def _fund_claimed(aid):
    """基金领取记录 —— **独立键**（不与 A01 活动的 claimed 共用，避免串账）。"""
    f = EQ_STATE.setdefault('fund_claimed', {})
    if not isinstance(f, dict):
        f = EQ_STATE['fund_claimed'] = {}
    lst = f.get(str(aid))
    if not isinstance(lst, list):
        lst = f[str(aid)] = []
    return lst


def _fund_state(aid, grade, target, prog):
    """单档状态：0 未完成 / 1 可领 / 2 已领（三态与 A01 活动同一口径）。"""
    if str(grade) in [str(x) for x in _fund_claimed(aid)]:
        return 2
    return 1 if int(prog) >= int(target) else 0


def handle_fund_act_info(body_json):
    # NetFundActInfo.recvMessage: msg[1]id msg[2]buyStatus(1买/0未买) msg[3]taskList(pairs)
    #   行 = [id, curValue, rewardStat(0未完成/1可领/2已领)]
    req = _req(body_json)
    aid = _i(req.get('id'), 1) or 1
    _bootstrap()
    act = _FUND_ACT.get(aid)
    tasks = []
    if act:
        grp = act[3]
        for (grade, ttype, tval, _rew) in _FUND_BY_GROUP.get(grp, []):
            prog = _fund_prog(ttype)
            tasks.append([grade, min(prog, tval) if tval else prog, _fund_state(aid, grade, tval, prog)])
    return _out(aid, _i((EQ_STATE.get('fund') or {}).get(str(aid), 0)), tasks)


def handle_fund_act_get_reward(body_json):
    """1812：领基金某一档。

    客户端发的是 `{id, grade}`（**`grade`**，不是 `taskid`）。修前 `_generic_receive` 只认
    `taskid/id2` ⇒ 永远 `[543]`。
    """
    from handlers_game import _role_info, _drop
    from handlers_task import _grant_group
    req = _req(body_json)
    aid = _i(req.get('id'), 0)
    # ★★2026-10-16（请求字段对账）：客户端只发 **`grade`**（`NetFundActGetReward.lua:24`），
    #   旧的 `taskid`/`id2` 兜底从来拿不到值（已删）——“读一个永远为 None 的字段”就是这类缺陷的温床。
    grade = _i(req.get('grade') or 0, 0)
    _bootstrap()
    act = _FUND_ACT.get(aid)
    if not act or not grade:
        return b'[543]'
    row = _FUND_TASK.get((act[3], grade))
    if row is None:
        return b'[543]'                            # 档位错误
    if _i((EQ_STATE.get('fund') or {}).get(str(aid), 0)) != 1:
        return b'[543]'                            # 还没买基金（先 1813）
    ttype, tval, rew = row
    if _fund_prog(ttype) < tval:
        return b'[543]'                            # 未达标
    if _fund_state(aid, grade, tval, _fund_prog(ttype)) == 2:
        return b'[543]'                            # 已领过
    groups, ok = _grant_group(rew)
    if not ok:
        return b'[543]'
    _fund_claimed(aid).append(grade)
    save_state()
    # ★★2026-10-16（D 复核 · 逐槽对客户端）：1812 的应答是**四槽**，不是两槽！
    #   `app/network/fundAct/NetFundActGetReward.lua`：
    #     `msg[1]=actId / msg[2]=taskId / msg[3]=roleInfo / msg[4]=dropInfo`
    #   修前（含本轮 C 包的第一版）回 `[1,[roleInfo, dropInfo]]` ⇒ 客户端把 roleInfo 当 actId、
    #   把 dropInfo 当 taskId，而 `error_verify(msg[3])` 对 nil 返回 **-1**，接着
    #   `SQGamer:setRoleInfo(-1)` 里 `roleInfo[1]` 对**数字**取下标 -> Lua
    #   "attempt to index a number value" —— **领个基金奖励就把客户端解析打穿**。
    return ('[1,' + json.dumps([int(aid), int(grade), _role_info(), _drop(groups)],
                               separators=(',', ':')) + ']').encode('utf-8')


def handle_fund_act_buy(body_json):
    """1813：买基金 —— **真扣费**（修前免费置 `fund[aid]=1`）。

    价格 = `A31_1.PriceType/Price`；`PriceType=1`（人民币）按本工程既有口径
    **1:1 折妖魂**（DECISIONS 2026-10-16：6031 按 98 妖魂卖）。
    """
    from handlers_game import _role_info
    req = _req(body_json)
    aid = _i(req.get('id'), 1) or 1
    _bootstrap()
    act = _FUND_ACT.get(aid)
    # ★★2026-10-16（错误码对账）：`NetFundActBuy.lua` 的 `m_errorCode` **只有 `[0]`**。
    #   本函数原来回 `[543]/[554]/[60]/[61]` —— 客户端 `NetErrorCodeTips:showTips(
    #   self.m_errorCode[statecode])` 取到 nil ⇒ **玩家点"购买"什么都看不到**（静默失败）。
    #   客户端码表里没有的码等于没有：失败一律回 `[0]`（客户端唯一认识的码），
    #   具体原因仍打到服务端日志，运营侧照旧可查。
    _FAIL = b'[0]'
    if not act:
        return _FAIL
    if _i((EQ_STATE.get('fund') or {}).get(str(aid), 0)) == 1:
        return _FAIL                               # 已购买（一次性）
    ptype, price, limit_lv, _grp = act
    # ★2026-11-07（全付费回滚）：PriceType=1 是人民币商品（6031 成长基金 ¥98）。
    #   商城已回滚为“原价展示、不可购买”，基金也不能再按 98 妖魂卖；直接拒绝并打日志。
    #   1810 列表也已过滤掉它，正常客户端不会走到这里（防直连/旧客户端）。
    if ptype == 1:
        print('[FUND] 1813 基金 %s 是 PriceType=1（人民币），本服不提供购买 -> 拒绝' % aid, flush=True)
        return _FAIL
    # ★★2026-10-16（C 复核发现的表侧坑）：**卖之前先确认这个基金真有奖励行**。
    #   `A31_2_FundReward.GroupID` 只有 10001（40 行）与 10004（40 行）两个值；
    #   而 `A31_1_RewardID` 里 **6041 妖魂基金指向 10002** —— 表里**一行都没有**
    #   （官方那张表的悬空引用，和 900022 三行同类）。修前这里会**收 500 妖魂、
    #   然后给一个永远领不到东西的空页面**。现在扣费前拒绝，并在日志里点名。
    if not _FUND_BY_GROUP.get(_grp):
        print('[FUND] 1813 基金 %s 的奖励组 %s 在 A31_2 没有任何行 -> 拒绝出售（不扣费）'
              % (aid, _grp), flush=True)
        return _FAIL
    if limit_lv and _i(EQ_STATE.get('player_lv', 1)) < limit_lv:
        print('[FUND] 1813 等级不足: lv=%s 需要=%s' % (_i(EQ_STATE.get('player_lv', 1)), limit_lv), flush=True)
        return _FAIL
    if price < 0:
        return _FAIL
    if ptype in (1, 2):                            # 妖魂（1=¥ 按 1:1 折算）
        if int(EQ_STATE.get('soulcoin', 0)) < price:
            print('[FUND] 1813 妖魂不足: %s < %s' % (int(EQ_STATE.get('soulcoin', 0)), price), flush=True)
            return _FAIL
        EQ_STATE['soulcoin'] = int(EQ_STATE.get('soulcoin', 0)) - price
    elif ptype == 3:
        if int(EQ_STATE.get('gold', 0)) < price:
            print('[FUND] 1813 金币不足: %s < %s' % (int(EQ_STATE.get('gold', 0)), price), flush=True)
            return _FAIL
        EQ_STATE['gold'] = int(EQ_STATE.get('gold', 0)) - price
    elif ptype == 5:                               # 鬼道积分
        if int(EQ_STATE.get('kidou', 0)) < price:
            return b'[0]'
        EQ_STATE['kidou'] = int(EQ_STATE.get('kidou', 0)) - price
    else:
        return _FAIL                               # 未实现的货币：扣费前拒绝
    EQ_STATE.setdefault('fund', {})[str(aid)] = 1
    save_state()
    # ★★2026-10-16（D 复核 · 逐槽对客户端）：1813 的应答是**两槽**
    #   （`app/network/fundAct/NetFundActBuy.lua`：`msg[1]=id / msg[2]=roleInfo`）。
    #   修前回 `[1,[roleInfo]]`（一槽）⇒ `msg[1]` 变成 roleInfo 表、`msg[2]` 是 nil，
    #   客户端 `gamer_setRoleInfo(nil)` 只打日志不崩，但**"购买成功"事件里的 id 变成了 roleInfo 表**。
    return _out(int(aid), _role_info())


# ============================ 领奖公共实现 ============================
def _generic_receive(body_json, default_aid=None):
    """1114/1116/1151/1170/1812 共用的领奖实现。
       客户端契约统一为: msg[1]=roleInfo(30字段) / msg[2]=dropInfo
       ⚠ msg[1] 必须是**完整 30 字段** —— 旧兜底给 {} 会让 gamer_setRoleInfo 把金币/等级刷成 nil。
    """
    from handlers_game import _role_info, _drop
    from handlers_task import _grant_group
    req = _req(body_json)
    aid = _i(req.get('id'), default_aid or 0)
    # ★★2026-10-16（任务书 C④）：不同活动页发的**档位字段名不同**，修前只认 `taskid/id2`：
    #   · 1114（累计登录）→ `NetActivityLoginReceive.lua:21  msg["day"] = error_verify(_taskid)`
    #     —— **字段名叫 day，装的其实是任务 id**（不是"第几天"），所以按 id 查行是对的；
    #   · 1116（消耗活动）→ `NetActivityConsumeReceive.lua:24  msg["grade"] = _grade`；
    #   · 1170/基金 → `msg["grade"]`（基金那页是 `A31_2.ID`，见 1812 自己的实现）。
    #   ⚠ 本轮核过客户端：`NetActivityConsumeReceive` / `NetActivityOneRcgRecv` 只在
    #     `SocketManager` 里注册、**没有任何 UI 调用点**（这套号在当前目标客户端里是闲置的），
    #     所以上面两条 `grade` 分支目前打不到 —— 属防御性兼容，不要据此认为"这些页面已通"。
    #   只认 taskid/id2 的后果是这些页**永远领不到**（恒 543）。这里把两套字段都认下来，
    #   并且 `day`/`grade` 都必须能在该活动的行表里查到（查不到照样 543，不猜）。
    cid = _i(req.get('taskid') or req.get('id2') or req.get('grade') or req.get('day') or 0)
    _bootstrap()
    rows = _A01_1.get(str(aid)) or []
    target = None
    reward_gid = None
    for (c2, t2, g2) in rows:
        if c2 == cid:
            target, reward_gid = t2, g2
            break
    if target is None or reward_gid is None:
        return b'[543]'                       # 参考码 (与 7014 同族)
    kind = _GROUP_KIND.get(str(aid), 'login')
    prog = _progress_of(kind)
    if prog < target:
        return b'[543]'                       # 未达标
    if cid in _claimed_key(aid):
        return b'[543]'                       # 已领过
    groups, ok = _grant_group(reward_gid)
    if not ok:
        return b'[543]'
    _claimed_key(aid).append(cid)
    save_state()
    return ('[1,' + json.dumps([_role_info(), _drop(groups)], separators=(',', ':')) + ']').encode('utf-8')
