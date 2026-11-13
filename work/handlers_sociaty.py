# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 公会 (2026-09-06 按 NetGetSociatyInfo/GetSociatyList/CreateSociaty/DonateSociaty 契约)
#   1121 INFO    -> [1,[ishavegang, gangname, icon, leve, rank, wealth, notice, 0, 0,
#                     isautojoin, num, shopLevel, sociatyExp, sociatyId, todaydonate]]
#   1122 LIST    -> [1,[[rank, gangId, gangName, level, icon, leader, memberNum, activeWeek,
#                        autoJoinThreshold, isAutoJoin],...]] (无其它公会 -> [])
#   1123 CREATE  req {type, gangname} -> [1,[roleInfo]] (码: 30000名空/5031特殊/50003已加入/50002重名)
#   1133 DONATE  req {donateList:[{itemtype,itemuid,itemnum}]} -> [1,[[], [], []]] (扣道具+财富)
import json, re, time
from accounts import AccountsError
from guild_world import atomic as _guild_atomic
from state import EQ_STATE, save_state, vitality_timerid
from state import explore_role_id as _explore_role_id

CREATE_COST = 1000   # 建会金币 (客户端 UI 用常量, 服务器侧扣相同额度)

_SG_DEFAULT = {'name': '', 'icon': 0, 'level': 1, 'rank': 0,
               'wealth': 0, 'notice': '', 'id': '', 'created': '',
               'todaydonate': 0, 'donate_date': ''}


def _sg():
    """本账号档里的公会见（**多账号下它是世界键的缓存/投影**，见 `_world_guilds` 的说明）。

    ★2026-10-02（本轮）：多账号下每次取会数据都先 `sync_account()` 把**世界键里的会**
      回灌进来 —— 这样"盟主改了公告/会名/财富"对每个成员立刻可见，而不需要把
      "每次改会都要记得同步所有成员"写进每一个调用点。单档时这一整段是 no-op。
    """
    sg = EQ_STATE.setdefault('sociaty', dict(_SG_DEFAULT))
    gw = _gworld()
    if gw is not None and isinstance(sg, dict):
        try:
            if _sync_my_guild_cache():
                save_state()            # 缓存与世界键漂移了 -> 落盘（否则下次读还是旧的）
        except Exception:
            pass
    return sg

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

def _reset_donate_day():
    sg = _sg()
    today = time.strftime('%Y-%m-%d')
    if sg.get('donate_date') != today:
        sg['donate_date'] = today
        sg['todaydonate'] = 0

def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


# ==================== ★★2026-10-02 妖盟 = 真·共享实体（世界键） ====================
def _gworld():
    """妖盟世界键（`guild_world` 模块）。单档/未启用 -> `None`（**调用方一律按"None 走原路径"写**）。

    ★★ 判据**必须**落在 `guild_world.active()` 上 —— 本轮实测踩到的形态：
      早先这里只写 `import guild_world; return guild_world`，于是 `is not None` **永远成立**
      （模块对象不可能是 None）⇒ 单档下所有 handler 都走世界键分支，
      而世界键在单档里根本没有账号上下文（`current_account()` 是 None）：
      建会"看起来成功"（回 `[1,[roleInfo]]`、金币照扣），但会名一个字节都没写进档里。
      **失败形态与"功能没做"完全相同，却是因为判据写错了** —— 这类错最难看出来。
    """
    try:
        import guild_world
    except Exception:
        return None
    return guild_world if guild_world.active() else None


def _sync_my_guild_cache():
    """把"世界键里的会"回灌进**当前账号**的 `sociaty` 缓存。返回 `True` 表示改了档。

    ⚠ **不许写 `EQ_STATE.current()`**：单档模式下 `state.EQ_STATE` 是普通 dict
      （`state.py:608`），只有 `YGZZ_MULTI_ACCOUNT=1` 时它才是账号代理（`state.py:616-617`）
      ⇒ 直接调 `.current()` 会让**单档套件当场 AttributeError**（本轮实测踩到：
      `_sociaty_test.py` 在"建会"那一步崩）。账号上下文一律走 `guild_world.current_account()`。
    """
    gw = _gworld()
    if gw is None:
        return False
    try:
        return bool(gw.sync_account(gw.current_account()))
    except Exception:
        return False


def _world_guilds():
    """妖盟榜单行（1122/1143 的**唯一取数入口**）。未启用 -> `None`；启用但没会 -> `[]`。

    ★★2026-10-02（本轮）**这份派生视图的取数源换掉了**：修前它把"各账号档的
      `sociaty.name` 分组"当作会的真值，于是会没有自己的身份、也没有成员名册；
      现在读的是**世界键** `world['guilds']`（`accounts.WORLD_KEYS` 新增的第二个键，
      落盘在 `work/accounts/_world.json`），账号档里的 `sociaty` 只是**这个账号侧的缓存**。
      ⇒ **不留下两套真值**：会级字段（会名/等级/财富/公告/成员/申请）只有世界键那一份。
      ⚠ 模块名保留为 `_world_guilds` 是为了让 5 个既有套件与 1122/1143 的调用点一行不改。

    返回 `None` = 单档/未启用（调用方走原路径）；`[]` = 启用了但**一个妖盟都没有**（合法结果，
    此时 1122 回空列表是**对的**，不再需要"假行"来撑场面）。

    ⚠ **没有数据源的字段一律 0**（铁律）：`activeWeek`（1122）/ `liveness`（1143）服务端
      没有这个量 ⇒ 给 0，**不编造**。
    """
    gw = _gworld()
    if gw is None:
        return None
    try:
        return gw.list_rows()
    except Exception:
        return None


def _own_player_name():
    """我方显示名。

    ⚠ **单档模式必须逐字节保持原样** —— 返回 `'测试'`（与 `state.role_info_array()` 的
      field[1] 同口径，客户端 `SQGamer.lua` 就是拿它当自己的名字）。

    ★★2026-10-02（本轮）**多账号下不能再用 `'测试'`**：本项目的铁律是"他人行缺省不许回
      `'测试'`，要用 `未命名#<id>`（唯一、可判别）"。多账号下 1143 的"盟主名"、1155/1152 的
      "我方行"都会走出这个函数，而 `'测试'` 是个**所有账号都可能叫**的名字 —— 服务端日志里
      看到"测试"就分不出是谁。实测：生产三个账号档的 `role_name` 全是 `None`（92 键模板里
      没有这个字段），于是多账号下 1143 会显示"盟主 = 测试"，而它其实对应 `u0001`。
      ⇒ 多账号：先读**本账号自己的** `role_name`，没有再回落到 `未命名#<aid>`。
    """
    if _gworld() is None:
        try:
            return str(EQ_STATE.get('role_name') or '测试')
        except Exception:
            return '测试'
    try:
        from accounts import EQ_STATE as _AS
        acc = _AS.current()
        if acc is not None:
            d = acc.data if isinstance(acc.data, dict) else {}
            return str(d.get('role_name') or ('未命名#' + acc.id))
    except Exception:
        pass
    return '未命名'


def _world_guild_members():
    """**和我同会**的全部账号（含我自己），带各自的 `sociaty_boss` 伤害。未启用 -> `None`。

    给 1155（妖盟 Boss 排行）与 1152（Boss 血量推送）用 —— 它们的"他人行"修前是写死的假名。

    ★2026-10-02（本轮）**同会判定换成了世界键的名册**：修前是"拿各账号档的 `sociaty.name`
      比字符串"，于是两个人只要会名相同就算同会（哪怕他们各自的世界键里根本没有这个会）。

    ⚠ **`roleId` 已真值化**（本轮）：每行发**该账号自己的真实 roleId**（多账号 =
      `guild_world.role_id_of`；单档 = 1），它与 `state.role_info_array()` 的 `fields[22]`
      **同口径** ⇒ 客户端 `NetSociatyBossRankInfo.lua:59` 的
      `roleId == SQManager:gamer_getLeaderID()` 仍然只把真正的自己认成"我"。
      修前那套"我方发 1、别人发 `100+owner_ns`"已废除（`100 + ns % 1000` 超过 1000 个账号会撞号）。
    """
    gw = _gworld()
    if gw is None:
        return None
    try:
        from accounts import REGISTRY, EQ_STATE as _AS
        me = getattr(_AS.current(), 'id', None)
        _gid, g = gw.my_guild()
    except Exception:
        return None
    if not g:
        return []                    # 没入会 -> 没有"同会成员"
    out = []
    for aid in gw.member_aids(g):
        try:
            acc = REGISTRY.get_or_create(aid)
        except Exception:
            continue
        d = acc.data if isinstance(acc.data, dict) else {}
        b = d.get('sociaty_boss') if isinstance(d.get('sociaty_boss'), dict) else {}
        out.append({'aid': aid,
                    'is_me': aid == me,
                    'role_id': gw.role_id_of(aid),
                    'name': str(d.get('role_name') or ('未命名#' + aid)),
                    'dmg': _i(b.get('damage', 0), 0),
                    'head': _acc_head_of(d),
                    'frame': _acc_frame_of(d)})
    out.sort(key=lambda m: (-m['dmg'], m['aid']))
    return out


def _acc_head_of(d):
    try:
        v = _i((d.get('head') or {}).get('eq', 1001), 1001)
        return v if v else 1001          # ⚠ 非 0 且不在 T35 里的头像 id 会让客户端**崩**
    except Exception:
        return 1001


def _acc_frame_of(d):
    try:
        v = _i((d.get('head') or {}).get('frame_eq', 1001), 1001)
        return v if v else 1001
    except Exception:
        return 1001


def _world_member_rows():
    """1121 的 `msg[8]`（**成员名册**）。未启用/未入会 -> `None`（调用方走原路径）。

    行形状（客户端 `network/sociaty/NetGetSociatyInfo.lua` 逐列读，10 列）：
      `[roleId, roleName, img, imgframe, level, positionCode, points, offer, activeWeek, state]`

    ★★ 为什么这一项**必须**做（本轮读客户端才发现的）：
      `msg[8]` 修前恒为 `0`，于是客户端：
        · 成员列表永远是空的；
        · `sociaty/Information/ControllerMSociatyInfoHome.lua:102-121` 遍历 `msg[8]` 找
          `roleId == 自己` 来设 **`mePosi`（我方职位码）** ⇒ 找不到就一直是 `""`
        · 而**审批/踢人/调职位的按钮可见性全部依赖 `mePosi`**：
          `sociaty/VSociatyMember.lua:307/308`（调整与逐出要 `"L"` 或 `"FL"`）、
          `:480-486`（转让盟主要 `"L"` 且对方 `"FL"`）、
          `Information/VSociatyInformation.lua:36/299`（管理页签）
      ⇒ 就算服务端把 1126/1127/1130 全做对了，**玩家在界面上根本点不到那些按钮**。
        这就是"服务端做完了但真机上用不了"的形态 —— 与 1042 那次（形状对不上、界面空白）同源。

    ⚠ **没有数据源的字段一律 0**（铁律）：`offer`（妖盟贡献）/`activeWeek`（个人活跃）
      服务端都没这个量 ⇒ 0。`points` 用各账号**真实**的排位积分（`qualify.points`）。
    ⚠ `state`：客户端 `ControllerMSociatyInfoHome.lua:114` 用 `state == 0` 累加"在线人数"。
      服务端有 `REGISTRY.online`（fileno 集合）可以判"有没有连接"，但那**不区分"人不在"
      与"人在但没连"**，且本轮没有真机可验 ⇒ **统一给 1（非在线）**，绝不编造在线状态。
    """
    gw = _gworld()
    if gw is None:
        return None
    try:
        from accounts import REGISTRY
        _gid, g = gw.my_guild()
    except Exception:
        return None
    if not g:
        return []                       # 未入会 -> 空名册（客户端 pairs 遍历安全）
    rows = []
    me = gw.current_aid()
    for aid in gw.member_aids(g):
        try:
            acc = REGISTRY.get_or_create(aid)
        except Exception:
            continue
        d = acc.data if isinstance(acc.data, dict) else {}
        q = d.get('qualify') if isinstance(d.get('qualify'), dict) else {}
        mrow = ((g.get('members') or {}).get(aid) or {})
        # ★★ roleId：**真实 id**（多账号 = `owner_ns`；单档 = 1）。客户端
        #   `ControllerMSociatyInfoHome.lua:117` 用 `roleId == gamer_getLeaderID()` 在名册里
        #   认出"我"来设 `mePosi`（我方职位码）。而 `gamer_getLeaderID()` 来自
        #   `state.role_info_array()` 的 `fields[22]` —— 那个**同一个函数**会在多账号下
        #   发同一个真实 id（见该处注释）⇒ 两边天然对上，不再需要"我方特判发 1"。
        #   ⚠ 名册是按观察者生成的，但真实 id 与观察者无关 ⇒ 谁看都是同一套 id（更干净）。
        rows.append([gw.role_id_of(aid),
                     str(d.get('role_name') or ('未命名#' + aid)),
                     _acc_head_of(d), _acc_frame_of(d),
                     _i(d.get('player_lv', 1), 1),
                     str(mrow.get('position') or gw.POS_MEMBER),
                     _i(q.get('points', 0), 0),          # 真实排位积分
                     0,                                   # offer: 无数据源
                     0,                                   # activeWeek: 无数据源
                     1])                                  # state: 1=非在线（见上，不编造）
    return rows


# ============================================================================
# ★★2026-10-08 妖盟弈局（1144~1159）：**共享状态** + 6 条空桩的实装
# ----------------------------------------------------------------------------
# 修前：1146/1147/1148/1156/1157/1158 六条**客户端真的会发**的请求全部走
#   `handle_sociaty_simple` -> `[1,[]]`。客户端 `recvMessage` 只判 statecode，
#   于是「升级弈局 / 购买局数 / 开启 / 讨伐 / 上报血量 / 伤害排行」**一律回"成功"**，
#   服务端却一个字节都没记住（等级/次数/血量全在 per-account 的 `sociaty_boss` 假值里，
#   队友之间互不可见）。本段把它们接到**世界键的 `guilds[gid]['chess']`**（见 guild_world）。
#
# 客户端契约（逐行核对）：
#   1144 BOSS_LIST   req{}          -> [chessLevel, 今日可开局次数, [[bossId, 剩余时间]]]
#                                     `NetSociatyBossInfo.lua:31-46`；MSociatyTranscript 用它
#                                     判"哪个妖神在开启中"（`getIsOpening/getOpeningtime`）。
#   1145 SINGLE_BOSS req{bossid}    -> [剩余血量, 剩余时间, 今日进入次数]
#                                     `NetSociatySingleBossInfo.lua:31-33`；血量会除
#                                     `UN07.HP` 画进度条（`VSociatyTranscriptBoss:477-484`）。
#   1146 CHESS_UPGRADE req{}        -> 只回调（码 50000/50005/50045/50046/50049）
#   1147 CHESS_NUM_BUY req{}        -> 只回调（码 50000/50005/50046）
#   1148 BOSS_START req{bossid}     -> 只回调（码 50000/50005/50050/50051/50052/50060）
#   1149 ENTER_COUNT_BUY req{bossid}-> msg[1]=**剩余**可购买次数（客户端直接显示）
#   1152 BOSS_HP_PUSH   **推送**    -> [bossId, 当前血量, 我[4], 排行[[4]]]
#   1154 BOSS_OVER      **推送**    -> 只回调（客户端 `StageDelegateChess` 收到就判负结算）
#   1156 JOIN_CHESS req{bossid}     -> 只回调（码 50000/50050/50053/50054/50055）
#   1157 BOSS_HP req{bossid,hp,time,sign} -> 只回调（码 582 有专门提示）
#   1158 BOSS_HURT_RANK req{bossid} -> **推送语义**：客户端 `recvMessage` 不读回包，
#                                      真正生效靠服务端主动推 1152（见 `VSociatyTranscriptBoss`）
#   1159 BOSS_START_NOTICE **推送** -> 只回调；非盟主收到后重新拉 1145
#
# ⚠ **单档模式逐字节保持原样**：这些 handler 一律"`gw is None` -> 回原形状"，
#   绝不因为"新功能"改变单档玩家看到的东西（`_sociaty_test.py` 那类旧套件仍须全绿）。
# ⚠ 铁律：**没有数据源的字段一律 0**。`hitRate`（伤害百分比）服务端真的没有 ⇒ 发 `'0'`。
_CHESS_NUM_BUY_FALLBACK = 3000      # T23_Function Type=118（客户端 UI 用的同一个值）
_T23_CACHE = {}                     # type -> Value（`_load_json` 每次都要复制全表，别在请求里反复读）


def _t23_value(ftype, fallback):
    """T23_Function 的 `Value`（客户端 `usGetTableSelectDatae(T23,...,type)[1].Value` 同源）。"""
    if ftype in _T23_CACHE:
        return _T23_CACHE[ftype]
    val = fallback
    try:
        t = _load_json('T23_Function.json')
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            if _i(r[h['Type']]) == int(ftype):
                val = _i(r[h['Value']], fallback)
                break
    except Exception:
        val = fallback
    _T23_CACHE[ftype] = val
    return val


def _chess_num_buy_price():
    return _t23_value(118, _CHESS_NUM_BUY_FALLBACK)


def _chess_entry_buy_price():
    # T23:119 = 40 妖魂 —— 客户端已自行扣 `gamer_getSoulCoin()`，服务端同一口径。
    return _t23_value(119, 40)


def _current_acc():
    """当前上下文的 `Account`（单档/未绑定 -> `None`）。传账号给 world 层时用它。"""
    try:
        from accounts import EQ_STATE as _AS
        fn = getattr(_AS, 'current', None)
        return fn() if callable(fn) else None
    except Exception:
        return None


def _world_chess():
    """当前账号那个会的弈局**共享状态**；未启用/未入会 -> `None`（调用方走原路径）。

    ★ 与会名/财富同级：只有世界键这一份。修前那套 `_sgb()`（per-account 字典）
      **仍然保留**给单档与旧 1152/1155 的兼容分支用，但它不再是多账号的真值。
    """
    gw = _gworld()
    if gw is None:
        return None
    try:
        _gid, g = gw.my_guild()
    except Exception:
        return None
    if not g:
        return None
    return g


def _chess_view():
    """当前会弈局的只读快照（含 level/次数/开启中）；无会 -> `None`。

    ⚠ **跨天重置必须落一次盘**（本文件里唯一会写盘的"读"）：`chess_reset_day` 只在日期
      变了的那一次返回 `True`，如果只改内存，"今天的次数/上一局"会在每个请求里被反复
      重置（实测：1144 先读一次看到 6 次，读第二次才变 1 次 —— 同一秒内两个答案）。
      不变量仍然成立：**读取本身不改变任何业务语义**，它只是把"日期已翻页"这个事实写下来。
    """
    gw = _gworld()
    if gw is None:
        return None
    g = _world_chess()
    if g is None:
        return None
    try:
        reset = gw.chess_reset_day(g)
        from accounts import EQ_STATE as _AS
        aid = getattr(_AS.current(), 'id', None)
        v = gw.chess_snapshot(g, aid)
        if reset:
            reg = gw.use()
            if reg is not None:
                reg.save_world()
        return v
    except Exception:
        return None


def _chess_un07(stage_id):
    """`UN07` 行 -> dict（`HP`/`DisplayReward` 是修 1145/1152"编造血量"的关键字段）。"""
    for row in _UN07:
        if _i(row.get('stage')) == _i(stage_id):
            return row
    return None


def _chess_stage_row(stage_id):
    """按 `StageID` 或 `BossStageID` 找 UN07 行（客户端两处都用过，别只认一种）。"""
    sid = _i(stage_id)
    for row in _UN07:
        if _i(row.get('stage')) == sid or _i(row.get('boss_stage')) == sid:
            return row
    return None


def _chess_boss_id():
    """当前会开启中的妖神 StageID（`chess_active`）；无 -> 0。"""
    g = _world_chess()
    if g is None:
        return 0
    try:
        a = _gworld().chess_active(g)
    except Exception:
        return 0
    return _i((a or {}).get('stage', 0)) if isinstance(a, dict) else 0


def _chess_rank_rows():
    """当前弈局的伤害行（**共享真值**）：`[[roleId, name, rank, damage, at], ...]`，按伤害降序。

    未开启/未入会 -> `None`（调用方回落到 `_world_guild_members()` 的历史口径）。
    ⚠ 与 `_world_guild_members()` 的差别：那个读的是**各账号档里的累计 damage**（跨弈局的
      历史值），这里读的是**本局**的伤害 —— 修前 1155/1152 的"排行"其实永远是同一份历史值。
    """
    gw = _gworld()
    if gw is None:
        return None
    g = _world_chess()
    if g is None:
        return None
    try:
        rows = gw.chess_damage_rows(g)
    except Exception:
        return None
    if not rows:
        return None
    out = []
    for rank, (aid, dmg, at) in enumerate(rows, 1):
        try:
            acc = _gw_registry().get_or_create(aid)
        except Exception:
            acc = None
        d = (acc.data if acc is not None and isinstance(acc.data, dict) else {})
        out.append([gw.role_id_of(aid),
                    str(d.get('role_name') or ('未命名#' + str(aid))),
                    rank, dmg, _i(at, 0),
                    _acc_head_of(d), _acc_frame_of(d)])
    return out


def _gw_registry():
    from accounts import REGISTRY
    return REGISTRY


@_guild_atomic
def handle_sociaty_boss_chess_upgrade(body_json=None):
    """1146 升级妖盟弈局。消耗 = `UN06[当前等级].UpgradeConsume`，同时校验**妖盟等级**。

    ★ 两处判据都来自客户端源码，不是拍脑袋：
      · `VSociatyTranscript.lua:117-128` 用 `getNextLevel()` 比 `curSociatyLevel`
        （不满足提示"升级受限，请先提升妖盟等级" = 码 `50045`），用
        `getStatic_UpgradeConsume()` 比 `getSociatyTreasure()`（不足 = `50046`）；
      · `SD_OBJS.SDSociatyChessLevel` 的行就是 `UN06[Level]`：`UpgradeConsume` 是
        **从该等级升到下一级的价格**（lv1 -> lv2 花 1500），`CallTimes`/`FightTimes`
        是该等级下的开局/进入次数上限。
    """
    gw = _gworld()
    if gw is None:
        return b'[1,[]]'                     # 单档：原样（只回调）
    g = _world_chess()
    if g is None:
        return b'[50000]'
    if not gw.can_manage():
        return b'[50005]'
    v = _chess_view() or {}
    lv = max(1, _i(v.get('level', 1), 1))
    row = _UN06.get(lv)
    if row is None:
        return b'[50049]'
    cost, num_max, _ft = row
    new_lv = lv + 1
    if new_lv not in _UN06:
        return b'[50049]'                    # 已是满级
    # ⚠ 判据是**会等级**（世界键，与 1121 第 4 槽同源）。不要用 `_sg()['level']`：
    #   那是**账号侧的缓存**，在"刚改过会等级、还没被回灌"的窗口里会读到旧值
    #   （本轮实测：会等级 9 而缓存 1 -> 明明够级却回 50045）。
    g_ = _world_chess() or {}
    if _i(g_.get('level', 1), 1) < max(1, _i(_UN06[new_lv][2], 1)):
        return b'[50045]'                    # 妖盟等级不足
    code, _nl = gw.chess_upgrade(cost=cost, num_max=max(_UN06.keys()) if _UN06 else 1)
    if code is None:
        return b'[1,[]]'
    if code != 1:
        return ('[%d]' % code).encode('utf-8')
    _push_chess_data()
    return b'[1,[]]'


@_guild_atomic
def handle_sociaty_boss_chess_num_buy(body_json=None):
    """1147 购买开局次数。价格 = `T23_Function[Type=118].Value`（客户端 UI 同一个值）。

    回包 `msg[1]` = 购买后**剩余可开局次数** —— 客户端不读，但发对了才能在真机上
    用一次请求自证"钱扣了、次数涨了"（否则只能靠再发一次 1144）。
    """
    gw = _gworld()
    if gw is None:
        return b'[1,[]]'                     # 单档：原样（只回调 —— 修前就是 handle_sociaty_simple）
    g = _world_chess()
    if g is None:
        return b'[50000]'
    if not gw.can_manage():
        return b'[50005]'
    code, left = gw.chess_buy_num(cost=_chess_num_buy_price())
    if code is None:
        return b'[1,[]]'
    if code != 1:
        return ('[%d]' % code).encode('utf-8')
    _push_chess_data()
    return _out(_i(left, 0))


@_guild_atomic
def handle_sociaty_boss_start(body_json=None):
    """1148 开启弈局。req{bossid}；血量取 `UN07.HP`（**官方值**，不再编造）。"""
    gw = _gworld()
    if gw is None:
        return b'[1,[]]'                     # 单档：原样
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0]'
    if not isinstance(req, dict):
        req = {}
    row = _chess_stage_row((req or {}).get('bossid'))
    if row is None:
        return b'[50050]'                    # 不存在该弈局
    g = _world_chess()
    if g is None:
        return b'[50000]'
    if not gw.can_manage():
        return b'[50005]'
    v = _chess_view() or {}
    # 同上：用**世界键的会等级**判据，不用账号侧缓存（缓存可能还没回灌）
    if _i(g.get('level', 1), 1) < max(1, _i(row.get('openlv', 1), 1)):
        return b'[50045]'                    # 妖盟等级不足（同 1146 的 50045）
    code, _a = gw.chess_start(_i(row.get('stage')), _i(row.get('boss_stage')),
                              _i(row.get('hp'), 1), _i(row.get('findtime'), 1800))
    if code is None:
        return b'[1,[]]'
    if code != 1:
        return ('[%d]' % code).encode('utf-8')
    _push_chess_start_notice(v.get('level'))
    return b'[1,[]]'


@_guild_atomic
def handle_sociaty_boss_enter_count_buy(body_json=None):
    """1149 购买**进入次数**（`UN06[等级].FightTimes` 是免费额度，超出靠买）。

    ⚠ 修前这里只动自己档里的 `enter_bought`，与共享弈局无关 —— 别人开一局，
      我买的次数还是按"我自己那份"算。现在计入世界键的 `entry_bought`。
    """
    gw = _gworld()
    if gw is None:
        return _legacy_enter_count_buy()
    g = _world_chess()
    if g is None:
        return b'[50000]'
    if not gw.can_manage():
        return b'[50005]'
    code, left = gw.chess_buy_entry(cost=_chess_entry_buy_price())
    if code is None:
        return b'[1,[]]'
    if code != 1:
        return ('[%d]' % code).encode('utf-8')
    _push_chess_data()
    return _out(_i(left, 0))


def _legacy_enter_count_buy():
    """单档原实现（逐字节保持）：按 `UN06.CallTimes` 扣本地 `enter_bought`。"""
    _reset_boss_day()
    b = _sgb()
    left = max(0, _i((_UN06.get(_i(b.get('chess', 1), 1)) or (0, 1, 1))[1], 1)
               - _i(b.get('enter_bought', 0)))
    b['enter_bought'] = _i(b.get('enter_bought', 0)) + (1 if left > 0 else 0)
    save_state()
    return _out(max(0, left - 1))


@_guild_atomic
def handle_sociaty_join_chess(body_json=None):
    """1156 进入弈局（讨伐）。req{bossid}；进入次数 = `UN06[等级].FightTimes`（每日每人）。"""
    gw = _gworld()
    if gw is None:
        return b'[1,[]]'                     # 单档：原样
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0]'
    if not isinstance(req, dict):
        req = {}
    row = _chess_stage_row((req or {}).get('bossid'))
    g = _world_chess()
    if g is None:
        return b'[50000]'
    if row is None:
        return b'[50050]'
    code, _info = gw.chess_join(_i(row.get('stage')))
    if code is None:
        return b'[1,[]]'
    if code != 1:
        return ('[%d]' % code).encode('utf-8')
    return b'[1,[]]'


@_guild_atomic
def handle_sociaty_chess_hp(body_json=None):
    """1157 上报伤害。req{bossid,hp,time,sign} —— **`hp` 是这一击的伤害**。

    证据：`WarStageUnits.lua:987-996` 在弈局里每打一次就
    `sendMessage(MSG_ID.SOCIATY_BOSS_HP, bossId, _damage, ...)` —— 第三参就是 `_damage`；
    而 `NetSociatyChessHp.lua:20-31` 把它放进 `msg["hp"]` 并签名。
    ⇒ 服务端要**减**血量（修前 1157 是空桩，血量永远不会掉）。
    """
    gw = _gworld()
    if gw is None:
        return b'[1,[]]'                     # 单档：原样
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0]'
    if not isinstance(req, dict):
        return b'[0]'
    row = _chess_stage_row(req.get('bossid'))
    if row is None:
        return b'[50050]'
    # ⚠ 必须把**当前账号**显式传下去：击杀者的显示名取自 `acc.data['role_name']`，
    #   而 `acc=None` 时 world 层拿不到账号对象（`current_account()` 在那里不可靠）
    #   ⇒ 1155 的 msg[1][3] 会变成 `未命名#u0002`（本轮实测踩到）。
    _acc = _current_acc()
    code, info = gw.chess_report_hp(_i(row.get('stage')), _i(req.get('hp'), 0), acc=_acc)
    if code is None:
        return b'[1,[]]'
    if code != 1:
        return ('[%d]' % code).encode('utf-8')   # 582 = 未进入这次弈局（客户端有提示分支）
    # ★2026-10-11：击杀那一次 `active` 已被清空 ⇒ 不加 force 的话 `_push_chess_hp` 会
    #   在 `if not act and not force` 处直接返回 ⇒ **最终血量（0）与终版排行推不出去**
    #   （`chess_report_hp` 之所以在清空 active 前拍那份 `damage_rows` 快照，就是为了这一推）。
    _killed = isinstance(info, dict) and bool(info.get('killed'))
    _push_chess_hp(info, _i(row.get('boss_stage')), force=_killed)
    if _killed:
        _push_chess_over()
    return b'[1,[]]'


@_guild_atomic
def handle_sociaty_chess_hurt_info(body_json=None):
    """1158 查看该妖神的伤害排行。

    ⚠ 客户端 `NetSociatyChessHurtInfo:recvMessage` **不读回包**（原注释也写了这是推送语义），
      真正刷界面的是紧接着服务端主动推的 1152。所以这里：① 回 `[1,[]]` 保持形状；
      ② **主动推一次 1152** —— 否则玩家点「排行」永远看到上一次的数据。
    """
    gw = _gworld()
    if gw is None:
        return b'[1,[]]'
    info = None
    g = _world_chess()
    if g is not None:
        try:
            info = {'damage_rows': gw.chess_damage_rows(g), 'hp': None}
        except Exception:
            info = None
    _push_chess_hp(info if isinstance(info, dict) else None, 0, force=True)
    return b'[1,[]]'


def _push_chess_data():
    """1134（妖盟基础数据）推送：升级/购买后让盟内面板立刻刷新。best-effort。"""
    try:
        import game_server as _gs
        from accounts import REGISTRY as _R
        g = _world_chess()
        if g is None:
            return 0
        payload = handle_sociaty_data_payload()
        n = 0
        for aid in _gw_registry_member_aids(g):
            n += _gs.push_account(aid, 1134, payload)
        return n
    except Exception:
        return 0


def _gw_registry_member_aids(g):
    try:
        return _gworld().member_aids(g)
    except Exception:
        return []


def _push_chess_hp(info=None, boss_stage=0, force=False):
    """1152 推送：`[bossId, 当前血量, 我[4], 排行[[4]]]` → 同会**所有**在线成员。best-effort。"""
    try:
        import game_server as _gs
        gw = _gworld()
        g = _world_chess()
        if gw is None or g is None:
            return 0
        view = _chess_view() or {}
        act = view.get('active') or {}
        if not act and not force:
            return 0
        # ★2026-10-11：击杀那一次 `act` 是空的（`active` 刚被清），`_chess_boss_id()` 也读不到
        #   ⇒ boss_id 会算成 0 ⇒ 在下一行直接 return 0。`info['stage']` 是这次上报的局 id，
        #   必须作为兜底来源，否则"最终血量 + 终版排行"这一推永远发不出去。
        boss_id = (_i(act.get('stage', 0))
                   or _i(info.get('stage', 0) if isinstance(info, dict) else 0)
                   or _chess_boss_id())
        if not boss_id:
            return 0
        rows = _chess_rank_rows() or []
        if info is not None and isinstance(info.get('damage_rows'), list) and info['damage_rows']:
            # ★★2026-10-11（1152 真机前置修复）：这份快照是**世界层的 3 元组** `(aid, dmg, at)`
            #   （`guild_world.chess_damage_rows`），而 `_build_hp_push` 读的是 `r[3]`（damage）
            #   ⇒ `IndexError: tuple index out of range` ⇒ 被本函数末尾的 `except Exception: return 0`
            #   **静默吞掉** ⇒ 1152 一条都发不出去（1154/1159 正常，所以表面看只是"血量/排行不刷新"）。
            #   击杀那一次必须用这份快照（`active` 已被清空，`_chess_rank_rows()` 会变空），
            #   所以这里**转换形状**而不是丢弃。
            rows = _chess_hp_rows(info['damage_rows'])
        hp = _i(act.get('hp', info.get('hp') if isinstance(info, dict) else 0), 0)
        payload = _build_hp_push(boss_id, hp, rows)
        n = 0
        for aid in _gw_registry_member_aids(g):
            n += _gs.push_account(aid, 1152, payload)
        return n
    except Exception as _exc:
        # ★ 不再静默：这条 except 当年把 IndexError 变成了"什么都没发生"。
        print('[CHESS1152] 推送失败 %r' % (_exc,))
        return 0


def _chess_hp_rows(raw_rows):
    """把**任意来源**的伤害行归一成推送/榜单形状 `[roleId, name, rank, damage, at]`。

    两个来源形状不同（2026-10-11 实测）：
      · `_chess_rank_rows()`（读当前开启中的局）→ 已是该形状；
      · `guild_world.chess_damage_rows()` → 世界层 3 元组 `(aid, dmg, at)`。
    归一化放在这里，调用方就不必各自记住这件事。
    """
    gw = _gworld()
    if gw is None:
        return []
    out = []
    for rank, r in enumerate(raw_rows or [], 1):
        r = list(r or [])
        if len(r) >= 4:                      # 已经是推送形状，原样带过（保留 at/head/frame）
            out.append([_i(r[0], 0), str(r[1]), _i(r[2], rank), _i(r[3], 0),
                        _i(r[4], 0) if len(r) > 4 else 0])
            continue
        aid = str(r[0]) if r else ''
        dmg = _i(r[1], 0) if len(r) > 1 else 0
        at = _i(r[2], 0) if len(r) > 2 else 0
        try:
            acc = _gw_registry().get_or_create(aid)
        except Exception:
            acc = None
        d = (acc.data if acc is not None and isinstance(acc.data, dict) else {})
        out.append([gw.role_id_of(aid),
                    str(d.get('role_name') or ('未命名#' + str(aid))),
                    rank, dmg, at])
    return out


def _build_hp_push(boss_id, hp, rows):
    """1152 的报文体。行形状 `[roleId, name, rank, damage]`（`NetSociatyBossHpPush:42-54`）。"""
    gw = _gworld()
    me_rid = gw.role_id_of(gw.current_aid()) if gw is not None else 1
    out = []
    for rank, r in enumerate(rows or [], 1):
        r = list(r or [])
        # ★ 防御：行短于 4 列时按 0 填，绝不再 IndexError（2026-10-11 那次静默失效的根因）
        out.append([_i(r[0], 0) if len(r) > 0 else 0,
                    str(r[1]) if len(r) > 1 else '',
                    rank,
                    _i(r[3], 0) if len(r) > 3 else 0])
    me = next((x for x in out if x[0] == me_rid),
              [me_rid, _own_player_name(), 0, 0])
    return _out(boss_id, hp, me, out)


def _push_chess_start_notice(chess_level=None):
    """1159 通知**非盟主**玩家"弈局已开启"（客户端收到后自己重拉 1145）。best-effort。"""
    try:
        import game_server as _gs
        gw = _gworld()
        g = _world_chess()
        if gw is None or g is None:
            return 0
        payload = _out(_i(chess_level if chess_level is not None
                          else (_chess_view() or {}).get('level', 1), 1))
        me = gw.current_aid()
        n = 0
        for aid in _gw_registry_member_aids(g):
            if aid == me:
                continue
            n += _gs.push_account(aid, 1159, payload)
        return n
    except Exception:
        return 0


def _push_chess_over():
    """1154 推送"弈局结束"（妖神被击杀）。客户端 `StageDelegateChess` 收到就结算。"""
    try:
        import game_server as _gs
        g = _world_chess()
        if g is None:
            return 0
        payload = b'[1,[]]'
        n = 0
        for aid in _gw_registry_member_aids(g):
            n += _gs.push_account(aid, 1154, payload)
        return n
    except Exception:
        return 0


def handle_sociaty_info(body_json=None):
    # 1121
    #   15 列（客户端 `NetGetSociatyInfo.lua` 逐列读）:
    #     [1]ishavegang [2]gangname [3]icon [4]leve [5]rank [6]wealth [7]notice
    #     [8]成员名册 [9]动态消息表 [10]isautojoin [11]审批阈值 [12]shopLevel
    #     [13]sociatyExp [14]sociatyId [15]todaydonate
    #   ⚠ 修前第 10/11 槽恒 0 且第 8/9 槽恒 0 —— 第 8 槽就是"成员列表 + 我方职位码"的来源
    #     （见 `_world_member_rows`），第 10/11 槽是**盟主在入会管理页设的那两项**
    #     （1132 写进档里的 `autoJoin`/`autoJoinThreshold`，修前只写不读）。
    sg = _sg()
    having = 1 if sg.get('name') else 0
    mrows = _world_member_rows()
    if mrows is None:
        # ★2026-10-10 真机定因：客户端对 msg[8] **和 msg[9]** 都用 `pairs()` 遍历
        #   （NetGetSociatyInfo.lua:52 / :64）。给数字 0 会让 `pairs(0)` 抛错，
        #   整个 recvMessage 当场中断 ⇒ m_callFunc 永不触发 ⇒ **妖盟面板永远不显示**。
        #   真机打点轨迹：statecode=1 之后既无 hasCallFunc 也无回调 ⇒ 正是这里崩的。
        #   ⇒ 必须给空表（与第 324 行套件对 msg[8] 的口径一致）。
        mrows = []
    data = [having, sg.get('name', ''), int(sg.get('icon', 0)), int(sg.get('level', 1)),
            int(sg.get('rank', 0)), int(sg.get('wealth', 0)), sg.get('notice', ''),
            mrows, [],
            int(sg.get('autoJoin', 0)), int(sg.get('autoJoinThreshold', 0)),
            int(sg.get('shopLevel', 1)), 0, sg.get('id', ''), int(sg.get('todaydonate', 0))]
    return ('[1,' + json.dumps(data, separators=(',', ':'), ensure_ascii=False)
            + ']').encode('utf-8')

def handle_sociaty_list(body_json=None):
    # 1122: 其它公会列表
    #   行形状（本文件头部的契约，10 个字段）:
    #     [rank, gangId, gangName, level, icon, leader, memberNum, activeWeek,
    #      autoJoinThreshold, isAutoJoin]
    # ★2026-10-02 (D4=B): 多账号下给**真实**的妖盟（按账号档的 `sociaty.name` 分组派生）。
    #   ⚠ `gangId` 这里用**盟名**（服务端没有独立的数字公会 id；用它比编一个数字更诚实，
    #     而且客户端目前只把它当字符串展示/回传）。`activeWeek`/`autoJoinThreshold`/
    #     `isAutoJoin` **服务端没有数据源 ⇒ 一律 0**（铁律：不许编造）。
    wg = _world_guilds()
    if wg is None:
        return b'[1,[]]'        # 单档: 原样（"私服无其它玩家"; 客户端遍历空安全）
    rows = []
    for i, g in enumerate(wg[:20]):
        # ⚠2026-10-02（本轮）`gangId` 由**会名**改成**真的世界键 id**（`g0001`）。
        #   修前用会名当 gangId，而 1124（申请入会）要求 `gangid.isdigit()` ⇒ 玩家在界面上
        #   点「申请」必然拿到 `30001 公会Id错误` —— 也就是说那条路修前**根本走不通**。
        #   ⇒ 顺带纠正一处会骗人的设计：`_sociaty_world_test.py` 的 S5 原来断言 `gangId == 会名`，
        #     那是照着实现写的判据，不是客户端的要求（客户端只回传，不解释）。
        rows.append([i + 1, g['id'], g['name'], g['level'], 1001, g['leader'],
                     g['members'], 0, 0, 0])
    return ('[1,' + json.dumps(rows, separators=(',', ':'), ensure_ascii=False) + ']').encode('utf-8')

@_guild_atomic
def handle_sociaty_create(body_json):
    # 1123
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    name = str(req.get('gangname') or '').strip()
    sg = _sg()
    gw = _gworld()
    if gw is None:
        # ---- 单档：**判据顺序逐字节保持原样**（名空 -> 已入会 -> 特殊字符 -> 金币）----
        if not name:
            return b'[30000]'
        if sg.get('name'):
            return b'[50003]'                 # 已加入公会
        if not re.match(r'^[\w\u4e00-\u9fa5]{1,12}$', name):
            return b'[5031]'                  # 特殊字符
        if EQ_STATE['gold'] < CREATE_COST:
            return b'[77]'
        EQ_STATE['gold'] -= CREATE_COST
        sg['name'] = name
        sg['id'] = 'G%d' % int(time.time())
        sg['created'] = time.strftime('%Y-%m-%d')
        save_state()
        return json.dumps([1, [_role_info()]], separators=(',', ':')).encode('utf-8')
    # ---- 多账号：会名唯一性 / 已入会 都是**世界级判定**（都落在 `guild_world.create` 里）----
    #   ⚠ **不要在 handler 里再判一次 `sg.get('name')`**：建会成功后缓存立刻被回灌，
    #     于是第二次调用会先撞上本地的 50003，把"重名"这个真实故障伪装成"你已经有一个公会了"。
    #     `guild_world.create` 内部按 `50002 重名 -> 50003 已入会` 的顺序判（重名信息量更大）。
    if not name:
        return b'[30000]'
    if not re.match(r'^[\w\u4e00-\u9fa5]{1,12}$', name):
        return b'[5031]'                      # 特殊字符
    if EQ_STATE['gold'] < CREATE_COST:
        return b'[77]'
    gid, err = gw.create(name, _i(sg.get('icon', 0), 0))
    if err is not None:
        return ('[%d]' % err).encode('utf-8')
    EQ_STATE['gold'] -= CREATE_COST
    _sync_my_guild_cache()       # 把 id/name 回灌进缓存（`_sg()` 下一次也会修）
    save_state()
    return json.dumps([1, [_role_info()]], separators=(',', ':')).encode('utf-8')

@_guild_atomic
def handle_sociaty_donate(body_json):
    # 1133 妖盟捐献 —— 2026-11-07 全类型修复（同时修“发了妖怪在背包去除不了”）
    #
    # 客户端 `NetDonateSociaty.lua` 会发 itemtype 1装备 / 2道具 / 3铜钱 / 4妖怪 / 5元灵 /
    # 7妖魂 / 99技能；修前服务端只处理 2（道具），其余全跳过 -> 回 [30006]「类型错误」，
    # 客户端不删除本地物品，于是 6 阶/普通妖怪都卡在背包里（用户报的“去除不了”）。
    #
    # 贡献口径与客户端同一套表：
    #   · 1/2/4/5/99 -> L01_Equipments / L02_Loots / L91_Status / L02_Loots / L93_Skills
    #     的 `Contribution` 列 × 数量；
    #   · 3 铜钱 -> 数量 / T23[206] × T23[209]；
    #   · 7 妖魂 -> 数量 × T23[207] × T23[209]。
    # 会级财富 = 贡献 × T23[208]；每日贡献上限 = T23[220]（超出整单拒绝，避免扣了东西不给贡献）。
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    sg = _sg()
    if not sg.get('name'):
        return b'[50000]'                     # 请先加入一个工会
    _reset_donate_day()
    # `NetDonateSociaty` 把清单放在 `data` 里（元素 itemtype/itemuid/itemnum）；旧键兜底。
    lst = req.get('data') or req.get('donateList') or []
    if not lst:
        # 客户端 recvMessage 只按 `pairs(msg[1])` 读明细；给三个空表 = 成功但什么都没发生。
        return json.dumps([1, [[], [], []]], separators=(',', ':')).encode('utf-8')

    cap = _t23_value(120, 400)                 # T23 ID=220 -> Type=120
    try:
        cur = int(sg.get('todaydonate', 0) or 0)
    except (TypeError, ValueError):
        cur = 0
    remain = max(0, int(cap) - cur)

    plan, err = _donate_plan(lst, remain)
    if err == 'type':
        # 客户端 m_errorCode[30006] = 类型错误（也用于“一件都不在背包”）
        return b'[30006]'
    if err == 'cap':
        # 客户端 m_errorCode[30007] = 阀值错误（今日贡献已达上限）
        return b'[30007]'

    rows = []
    gained = 0
    try:
        from handlers_cult import _remove_monster
    except Exception:
        _remove_monster = None
    for itype, uid, take, contrib in plan:
        try:
            if itype == 3:                     # 铜钱
                EQ_STATE['gold'] = int(EQ_STATE.get('gold', 0)) - take
            elif itype == 7:                   # 妖魂
                EQ_STATE['soulcoin'] = int(EQ_STATE.get('soulcoin', 0)) - take
            elif itype == 2:                   # 道具
                EQ_STATE['prop'][uid] = int((EQ_STATE.get('prop') or {}).get(uid, 0)) - take
                if int(EQ_STATE['prop'][uid]) <= 0:
                    EQ_STATE['prop'].pop(uid, None)
                    (EQ_STATE.get('prop_id') or {}).pop(uid, None)
            elif itype == 1:                   # 装备（整件）
                from handlers_bag import _drop_equip
                _drop_equip(uid)
            elif itype == 4:                   # 妖怪（整只）
                if _remove_monster is not None:
                    _remove_monster(uid)
                # 清布阵里对该 ucode 的引用（否则 3200 会回一个已经不存在的 ucode）
                for _ft in list((EQ_STATE.get('formation') or {}).keys()):
                    try:
                        EQ_STATE['formation'][_ft] = [
                            r for r in (EQ_STATE['formation'].get(_ft) or [])
                            if str(r[0]) != str(uid)]
                    except Exception:
                        continue
            elif itype == 5:                   # 元灵
                for g in (EQ_STATE.get('ghost') or []):
                    if str(g[0]) == str(uid):
                        g[2] = int(g[2] or 0) - take
                        break
                EQ_STATE['ghost'] = [g for g in (EQ_STATE.get('ghost') or [])
                                     if int(g[2] or 0) > 0]
            elif itype == 99:                  # 技能袋
                EQ_STATE.get('skills', {}).pop(uid, None)
                # 清掉怪身上对该技能袋 ucode 的引用，避免“技能袋没了、槽还指着它”
                for _mu, _slots in list((EQ_STATE.get('monster_skills') or {}).items()):
                    try:
                        EQ_STATE['monster_skills'][_mu] = [
                            0 if str(s) == str(uid) else s for s in (_slots or [])]
                    except Exception:
                        continue
            rows.append([itype, uid, take])
            gained += int(contrib or 0)
        except Exception as _e:
            # 单条失败不影响其它条；失败条目**不进** rows（客户端不会误删）
            print('[SOCIATY_DONATE] 条目失败 type=%s uid=%s take=%s err=%r'
                  % (itype, uid, take, _e), flush=True)
            continue

    if not rows:
        return b'[30006]'

    wealth = int(gained * (_t23_value(108, 1) or 1))
    try:
        sg['todaydonate'] = int(sg.get('todaydonate', 0) or 0) + int(gained)
    except (TypeError, ValueError):
        sg['todaydonate'] = int(gained)
    _guild_add_wealth(wealth, sg)

    # T37 每日任务 CT=34「妖盟捐献 3 次」：一次请求算一次（扣到东西才算）
    try:
        from handlers_task import bump_task, CT_SOCIATY_DONATE
        bump_task(CT_SOCIATY_DONATE, 1)
    except Exception:
        pass
    # CT=36「获得妖盟财富 / 个人贡献」：按真实入账财富值 bump
    try:
        from handlers_task import bump_task as _bt36, CT_SOCIATY_CONTRIB as _CT36
        _bt36(_CT36, wealth)
    except Exception:
        pass
    save_state()
    return json.dumps([1, [rows, [], []]], separators=(',', ':')).encode('utf-8')


# ---- 2026-11-07 妖盟捐献：贡献/可捐性计算（不落库；供 1133 先算后扣）----
_DONATE_CONTRIB = {}


def _donate_contrib(table, static_id):
    """静态 id -> `Contribution`（表读不到/没有该列 -> 0；按表缓存）。"""
    if table not in _DONATE_CONTRIB:
        m = {}
        try:
            t = _load_json(table)
            h = {c: k for k, c in enumerate(t[0])}
            if 'Contribution' in h and 'ID' in h:
                for r in t[1:]:
                    try:
                        m[str(_i(r[h['ID']]))] = _i(r[h['Contribution']])
                    except Exception:
                        continue
        except Exception:
            m = {}
        _DONATE_CONTRIB[table] = m
    try:
        return int(_DONATE_CONTRIB[table].get(str(static_id), 0) or 0)
    except (TypeError, ValueError):
        return 0


def _donate_plan(lst, remain):
    """把请求清单算成 `[(itemtype, uid, take, contrib)]`；不改状态。

    返回 `(plan, error)`：`error=None` 可执行；`'type'` -> 30006；`'cap'` -> 30007。
    """
    plan = []
    for d in lst:
        if not isinstance(d, dict):
            continue
        try:
            itype = int(d.get('itemtype') or 0)
            iuid = str(d.get('itemuid') or '')
            inum = int(d.get('itemnum') or 0)
        except (TypeError, ValueError):
            continue
        if inum <= 0:
            continue
        try:
            if itype == 3:                     # 铜钱
                ratio = _t23_value(106, 100) or 100
                pr = _t23_value(109, 1) or 1
                if int(EQ_STATE.get('gold', 0)) < inum:
                    continue
                plan.append((3, '', inum, int(inum / float(ratio) * pr)))
            elif itype == 7:                   # 妖魂
                ratio = _t23_value(107, 1) or 1
                pr = _t23_value(109, 1) or 1
                if int(EQ_STATE.get('soulcoin', 0)) < inum:
                    continue
                plan.append((7, '', inum, int(inum * ratio * pr)))
            elif itype == 2:                   # 道具
                have = int((EQ_STATE.get('prop') or {}).get(iuid, 0) or 0)
                take = min(have, inum)
                if take <= 0:
                    continue
                pid = (EQ_STATE.get('prop_id') or {}).get(iuid)
                plan.append((2, iuid, take,
                             _donate_contrib('L02_Loots.json', pid) * take))
            elif itype == 1:                   # 装备（整件）
                eid = (EQ_STATE.get('equip_id') or {}).get(iuid)
                if eid is None:
                    continue
                plan.append((1, iuid, 1, _donate_contrib('L01_Equipments.json', eid)))
            elif itype == 4:                   # 妖怪（整只）
                from state import get_monster_base
                b = get_monster_base(iuid)
                if not b:
                    continue
                plan.append((4, iuid, 1, _donate_contrib('L91_Status.json', int(b[0]))))
            elif itype == 5:                   # 元灵
                take, gid = 0, None
                for g in (EQ_STATE.get('ghost') or []):
                    if str(g[0]) == iuid:
                        gid = int(g[1] or 0)
                        take = min(int(g[2] or 0), inum)
                        break
                if take <= 0:
                    continue
                plan.append((5, iuid, take,
                             _donate_contrib('L02_Loots.json', gid) * take))
            elif itype == 99:                  # 技能袋
                sk = (EQ_STATE.get('skills') or {}).get(iuid)
                if sk is None:
                    continue
                try:
                    sid = int((sk or [0])[0] or 0)
                except (TypeError, ValueError):
                    sid = 0
                plan.append((99, iuid, 1, _donate_contrib('L93_Skills.json', sid)))
        except Exception as _e:
            print('[SOCIATY_DONATE] 计划条目异常 type=%s uid=%s err=%r'
                  % (itype, iuid, _e), flush=True)
            continue
    if not plan:
        return [], 'type'
    if sum(p[3] for p in plan) > int(remain):
        return plan, 'cap'
    return plan, None

def _guild_add_wealth(delta, sg=None):
    """把财富增减落到**会级真值**：多账号 -> 世界键；单档/未入会 -> 账号档里的 `sg['wealth']`。

    ⚠ 修前这里直接写 `sg['wealth']`。多账号下那只是**自己档里的一份副本**，
      于是"我捐的钱"在别人的面板上不增加（1121/1122/1143 各读各的缓存）——
      典型的"两套真值、双向不同步"。现在会级字段只有世界键那一份。
    """
    gw = _gworld()
    if gw is not None:
        v = gw.add_wealth(delta)
        if v is None:
            raise AccountsError('妖盟财富写入缺少有效世界实体')
        return v
    if sg is not None:
        sg['wealth'] = int(sg.get('wealth', 0)) + int(delta)
        return sg['wealth']
    return None


# ============================================================================
# ★2026-09-12 (v26 goal-round-2) 公会族补齐 1124~1160
# ----------------------------------------------------------------------------
# 发现过程: `_audit_netmap.py` 把 sociaty 一族全列进 B 类("兜底不崩"), 但**逐条读客户端**后发现
# 其中有 4 条真崩 (静态判据没覆盖到, 因为它们的取用方式不是 `pairs(msg[N])` 而是 `msg[N][k]` 直取):
#   1125 SOCIATY_JOIN_LIST -> `for _,x in pairs(msg[1])`  无守卫 -> nil -> 崩
#   1135 SOCIATY_WEEK_TASK -> `pairs(msg[1])` + `pairs(msg[3])` 双无守卫 -> 崩
#   1143 SOCIATY_RANK_INFO -> `local meMsg = msg[2]` 后直取 `meMsg[1]` **无 or{} 守卫** -> nil 索引崩
#   1155 SOCIATY_BOSS_RANK_INFO -> `msg[1][1]` / `msg[2]`(ipairs) / `msg[3][4]` 全无守卫 -> 崩
# 其余是"只回调"型 (兜底 [1,[[]]] 的 msg[1]={} 不崩), 但点了没反应。
#
# 客户端契约 (逐行核对, 见每条 handler 头部):
#   1125 JOIN_LIST      req{}                        -> msg[1]=申请列表 [[roleid,rolename,img,imgframe,points,level]]
#   1126 APPLY          req{...}                     -> 只回调
#   1127 KICK_OUT       req{...}                     -> 只回调
#   1128 NOTICE         req{notice}                  -> 只回调
#   1129 QUIT           req{}                        -> 只回调
#   1130 POST_CHANGE    req{roleId,positioncode}     -> 只回调
#   1131 JOIN           req{sociatyId}               -> 只回调
#   1132 JOIN_TYPE      req{type,threshold}          -> 只回调
#   1134 SOCIATY_DATA   **推送** [positionCode,activeWeek,wealth,wealthWeek,wealthGiveGang,todaydonate]
#   1135 WEEK_TASK      req{}                        -> msg[1]=任务[[id,num,state]] msg[2]=本周活跃 msg[3]=活跃奖励[[id,num,state]]
#   1136 TASKREWARD     req{taskid}                  -> [roleInfo, dropInfo]
#   1137 LIVENESSREWARD req{taskid}                  -> [roleInfo, dropInfo]
#   1138 SHOP_INFO      req{}                        -> msg[1]=[[商品配置id, 周期内已购买数]]
#   1139 SHOP_BUY       req{shopItemId,count}        -> [roleInfo, dropInfo, num(背包满>0)]
#   1140 DISMISS        req{}                        -> 只回调
#   1142 SHOP_UPGRADE   req{}                        -> 只回调 (码 50000未入会/50044满级/50045妖盟等级/50046财富不足)
#   1143 RANK_INFO      req{}                        -> msg[1]=榜单[[icon,level,rank,name,leaderName,memberNum,liveness]]
#                                                       msg[2]=我的[同 7 字段] ★必须给(客户端直取 meMsg[1])
#   1144 BOSS_LIST      req{}                        -> [chessLevel, chessNum(today), [[bossId, remainTime]]]
#   1145 SINGLE_BOSS_GET req{bossid}                 -> 单 boss 详情
#   1146 CHESS_UPGRADE  req{}                        -> 只回调
#   1147 CHESS_NUM_BUY  req{}                        -> 只回调
#   1148 BOSS_START     req{}                        -> 只回调
#   1149 ENTER_COUNT_BUY req{}                       -> msg[1]=剩余次数(数字)
#   1151 ONE_RCG_RECV   (已在 handlers_opsact)
#   1152 BOSS_HP_PUSH   **推送** [bossId, hp, my[roleId,roleName,rank,totalDamage], rankList[[...]]]
#   1153 CHESS_RESULT   req{bossId,ourside,mvp}     -> **8 槽**（装机客户端契约）
#        [result, rankNum(数字!), roleInfo, ourFormation, dropInfo, talentId, killerroleid, exDropItems]
#        ★单档仍回 2 槽 [roleInfo, dropInfo]（逐字节不变）
#   1154 BOSS_OVER      **推送**                     -> 只回调
#   1155 BOSS_RANK_INFO req{bossid}                  -> [msg[1]=最后一击 7 字段, msg[2]=排行(ipairs),
#                                                       msg[3]=我的 7 字段] ★三处都无守卫
#   1156 JOIN_CHESS     req{stageId}                 -> 只回调
#   1157 BOSS_HP        req{hp}                      -> 只回调
#   1158 BOSS_HURT_RANK **推送** -> 只回调
#   1159 BOSS_START_NOTICE **推送** -> 只回调
#   1160 JOIN_OUTTIME   **推送** msg[1]=冷却结束绝对时间 -> CommonTips 显示
# ============================================================================
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

def _head_pair():
    head = EQ_STATE.get('head') or {}
    return _i(head.get('eq', 1001), 1001), _i(head.get('frame_eq', 1001), 1001)

# ---- UN15_UnionTasks: 公会任务 (id, ConditionType, Quantity, RewardID) ----
_UN15 = []
try:
    _t = _load_json('UN15_UnionTasks.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _UN15.append((_i(_r[_h['ID']]), _i(_r[_h['ConditionType']]), _i(_r[_h['Quantity']]),
                          _i(_r[_h['RewardID']])))
        except Exception:
            continue
except Exception:
    pass
_UN15_BY_ID = {t[0]: t for t in _UN15}

# ---- UN16_UnionActivityReward: 活跃奖励 (ConditionType 2 -> 按 ActivityRequirement 阈值) ----
_UN16 = []
try:
    _t = _load_json('UN16_UnionActivityReward.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _UN16.append((_i(_r[_h['ID']]), _i(_r[_h['RewardType']]), _i(_r[_h['ActivityRequirement']]),
                          _i(_r[_h['RewardID']])))
        except Exception:
            continue
    _UN16.sort(key=lambda x: x[2])
except Exception:
    pass

# ---- UN05_UnionStoreLevel: 公会商店升级消耗 (Level -> ShopUpgradeConsume) ----
_UN05 = {}
try:
    _t = _load_json('UN05_UnionStoreLevel.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        _UN05[_i(_r[_h['Level']])] = _i(_r[_h['ShopUpgradeConsume']])
except Exception:
    pass
_SHOP_MAX_LEVEL = max(_UN05.keys()) if _UN05 else 1

# ---- UN02_UnionShop: 公会商店 (ID -> (GoodsType, GoodsId, GoodsCount, LimitCoun, PriceType, Price)) ----
_UN02 = {}
try:
    _t = _load_json('UN02_UnionShop.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _UN02[_i(_r[_h['ID']])] = (_i(_r[_h['GoodsType']]), str(_r[_h['GoodsId']]),
                                       _i(_r[_h['GoodsCount']], 1), _i(_r[_h['LimitCoun']], 999),
                                       _i(_r[_h['PriceType']], 6), _i(_r[_h['Price']]))
        except Exception:
            continue
except Exception:
    pass

# ★2026-10-16（任务书 C②）：UN02 商品类型 —— 发货走 `handlers_bag.grant_entries` 的类型空间。
#   表里实际出现的是 2(道具 13 行)/8(唤妖符 1)/18(宝箱 2)/22(头像框 1)；这里把
#   `REWARD_NEWTYPE` 里已实现的常用类型都列上，未列到的一律**扣费前拒绝**（不再"钱扣了货没有"）。
_UN_SHOP_GOODS_TYPES = (1, 2, 3, 4, 5, 6, 7, 8, 9, 14, 17, 18, 21, 22, 98)
_UN_SHOP_BUY_MAX = 99          # 单次数量上限：无表源（UN02 没有"单次上限"列），保守护栏

# ---- UN07_UnionBossLevel: 弈局阶段 ----
#   ★★2026-10-08：改成 **dict 行**并保留 `HP`/`DisplayReward`/`ExistTime` ——
#   1145/1152 的"妖神剩余血量"只能从 `UN07.HP` 来（修前服务端发的是编造的 10000，
#   而客户端 `VSociatyTranscriptBoss:480` 拿 `getStatic_HP()` 当分母算百分比 ⇒ 永远 2%）。
#   旧的 4 元组解包点（`_UN07[0]` 那几处）已一并改成字典取值。
_UN07 = []
try:
    _t = _load_json('UN07_UnionBossLevel.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _UN07.append({'stage': _i(_r[_h['StageID']]),
                          'name': str(_r[_h.get('StageName', _h['StageID'])] or ''),
                          'boss_stage': _i(_r[_h['BossStageID']]),
                          'openlv': _i(_r[_h['OpenLevel']], 1),
                          'findtime': _i(_r[_h['FindTime']], 1800),
                          'exist': _i(_r[_h['ExistTime']], 1800) if 'ExistTime' in _h else 1800,
                          'hp': _i(_r[_h['HP']], 1) if 'HP' in _h else 1,
                          'reward': _i(_r[_h['DisplayReward']], 0) if 'DisplayReward' in _h else 0})
        except Exception:
            continue
    _UN07.sort(key=lambda r: r['stage'])
except Exception:
    pass

# ---- UN06_UnionBossLevel: 弈局等级 (Level -> (UpgradeConsume, CallTimes, FightTimes)) ----
_UN06 = {}
try:
    _t = _load_json('UN06_UnionBossLevel.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        _UN06[_i(_r[_h['Level']])] = (_i(_r[_h['UpgradeConsume']]), _i(_r[_h['CallTimes']], 1),
                                      _i(_r[_h['FightTimes']], 1))
except Exception:
    pass

# ---- UN09_UnionBossRanking: 妖神伤害名次奖励 (StageID, Rank) -> T02 奖励组 ----
#   Rank 0 = 最后一击；1..10 = 名次。66 行**全部**是合法 T02_Reward.GroupID（已逐行核对）。
#   客户端 `VSociatyTranscriptRank.lua:104` 用它显示"该 boss 对应的所有排行奖励"。
_UN09_RANK = {}
try:
    _t = _load_json('UN09_UnionBossRanking.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        _UN09_RANK[(_i(_r[_h['StageID']]), _i(_r[_h['Rank']]))] = _i(_r[_h['GroupID']])
except Exception:
    pass


def _sgb():
    # 公会额外状态 (不动 1121/1122/1123/1133 已有的字段)
    return EQ_STATE.setdefault('sociaty_boss', {'chess': 1, 'num_date': '', 'num': 0,
                                                'enter_bought': 0, 'stage': 0,
                                                'join_cd_until': 0, 'damage': 0})

def _reset_boss_day():
    b = _sgb()
    today = time.strftime('%Y-%m-%d')
    if b.get('num_date') != today:
        b['num_date'] = today
        b['num'] = 0
        b['enter_bought'] = 0

def _is_member():
    return bool((_sg() or {}).get('name'))


# ============================================================================
# UN15 里三个**本工程自己驱动**的 ConditionType（1035/1036/1037）
# ----------------------------------------------------------------------------
# `UN15_UnionTasks` 里这三行一直都在，但服务端**没有任何写入点** ⇒ 进度恒 0：
#   1035 【盟】妖盟签到  ConditionType=66 Quantity=5
#   1036 【盟】发放红包  ConditionType=67 Quantity=3
#   1037 【盟】抢红包    ConditionType=68 Quantity=5
#
# ⚠★ 它们**不能**读 `a07_ct`：那是**每日**账本（`handlers_task.py:198` 跨天清零），
#   而"一周签到 5 次"每天最多签到一次 ⇒ 放进每日账本**永远到不了 5**（HANDOFF 记的
#   正是这一条）。所以这三条走 per-account 的**周**计数 `EQ_STATE['sociaty']['wk_ct']`，
#   周键与 `handlers_task._wk()` 同源（ISO 周、周一起始）。
# ⚠ 其余 ConditionType 仍读 `a07_ct` —— 它们本来就是每日任务驱动的，换口径会把既有
#   进度清零（玩家可见的回退）。
# ============================================================================
CT_SOCIATY_SIGN = 66
CT_SOCIATY_SEND_REDBAG = 67
CT_SOCIATY_GET_REDBAG = 68
_UN15_SELF_CT = (CT_SOCIATY_SIGN, CT_SOCIATY_SEND_REDBAG, CT_SOCIATY_GET_REDBAG)


def _week_key():
    import datetime as _dt
    try:
        _d = _dt.date.today()
        return '%d-W%02d' % (_d.isocalendar()[0], _d.isocalendar()[1])
    except Exception:
        return time.strftime('%Y-%m-%d')


def _week_ct_read(ct):
    """**只读**：本账号本周该 ConditionType 的计数（没有/跨周 -> 0，**不建键、不落盘**）。"""
    sg = EQ_STATE.get('sociaty')
    st = sg.get('wk_ct') if isinstance(sg, dict) else None
    if not isinstance(st, dict) or st.get('wk') != _week_key():
        return 0
    cts = st.get('ct')
    return _i(cts.get(str(_i(ct))), 0) if isinstance(cts, dict) else 0


def _week_ct_bump(ct):
    """记一次（写路径）。返回记完之后的次数。跨周惰性清零。"""
    sg = _sg()
    st = sg.get('wk_ct')
    if not isinstance(st, dict):
        st = sg['wk_ct'] = {}
    if st.get('wk') != _week_key():
        st['wk'] = _week_key()
        st['ct'] = {}
    if not isinstance(st.get('ct'), dict):
        st['ct'] = {}
    k = str(_i(ct))
    st['ct'][k] = min(9999, _i(st['ct'].get(k), 0) + 1)
    return st['ct'][k]


def _un15_progress(ct, qty):
    # 复用每日任务驱动的 ConditionType 计数 (与 1068/3068 同一套 a07_ct 账本)
    #   ⚠ 三个"妖盟自己驱动"的 ct（66/67/68）走**周**账本，见上面那段说明。
    ct = _i(ct)
    if ct in _UN15_SELF_CT:
        n = _week_ct_read(ct)
    else:
        st = EQ_STATE.setdefault('a07_ct', {})
        n = _i(st.get(str(ct)), 0)
    return min(n, _i(qty)) if qty else n

def _task_claimed():
    return EQ_STATE.setdefault('sociaty_task_claimed', {})

def _liveness_claimed():
    return EQ_STATE.setdefault('sociaty_live_claimed', {})


def handle_sociaty_join_list(body_json=None):
    # 1125: 入会申请列表 -> msg[1] = [[roleid,rolename,img,imgframe,points,level]] (pairs 无守卫)
    # ❌ 修前（单档）："私服无其他玩家 -> **造** 3 条申请人"（半城烟沙/一剑霜寒/白衣渡江）。
    # ❌ 修前（多账号）：申请记录服务端**没有任何存储**（1126/1130/1131 都只回调）⇒ 回空。
    # ★2026-10-02（本轮）多账号：读**世界键里我这个会的申请名册**（`applications`）——
    #   1124/1131 真的写进去、1126 审批后真的删掉。**没有任何假行**。
    apps = _gworld_application_rows()
    if apps is None:
        img, frame = _head_pair()
        rows = []
        for i, nm in enumerate(('半城烟沙', '一剑霜寒', '白衣渡江')):
            rows.append(['bot_apply_%d' % (i + 1), nm, img, frame,
                         1200 - i * 150, 20 + i * 3])
        return _out(rows)
    return _out(apps)


def _gworld_application_rows():
    """`1125` 的申请行。未启用世界键 -> `None`（调用方走单档原路径）。"""
    gw = _gworld()
    if gw is None:
        return None
    try:
        return gw.application_rows()
    except Exception:
        return []


def handle_sociaty_week_task(body_json=None):
    # 1135: 公会周任务 -> msg[1]=[[taskid,num,state]] msg[2]=本周活跃 msg[3]=活跃奖励[[id,num,state]]
    #   ★ msg[1] 与 msg[3] 都是 `pairs` 且**无 or{} 守卫**
    claimed = _task_claimed()
    live_claimed = _liveness_claimed()
    # 本周活跃 = 已完成(可领+已领)任务的 Quantity 之和 (真实口径)
    vital = 0
    rows = []
    for (tid, ct, qty, rid) in _UN15:
        num = _un15_progress(ct, qty)
        if str(tid) in claimed:
            state = 2
        else:
            state = 1 if (qty and num >= qty) else 0
        if state >= 1:
            vital += _i(qty)
        rows.append([tid, num, state])
    lrows = []
    for (lid, rtype, need, rid) in _UN16:
        num = vital
        if str(lid) in live_claimed:
            state = 2
        else:
            state = 1 if vital >= need else 0
        lrows.append([lid, num, state])
    return _out(rows, vital, lrows)


def _sociaty_reward(body_json, table, claimed_map, need_of):
    from handlers_game import _role_info as _gi, _drop as _gd
    from handlers_task import _grant_group
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        req = {}
    tid = _i((req or {}).get('taskid'))
    if tid not in table:
        return b'[50000]'                          # 请先加入一个工会 (查无此任务)
    if not _is_member():
        return b'[50000]'
    if str(tid) in claimed_map:
        return b'[95]'                             # 奖励已经领取过
    # ★条件判定必须用**该任务自己的** ConditionType/Quantity 进度, 不能拿"周活跃"当门槛:
    #   周活跃 = 已领任务 Quantity 之和, 用它判定会造成"没领过就永远领不了"的死锁。
    ok_cond, rid = need_of(tid)
    if not ok_cond:
        return b'[63]'                             # 限制购买 (条件未达成)
    groups, _gok = _grant_group(rid)
    if not _gok:
        return b'[51]'                             # 暂无数据 (奖励组查不到)
    claimed_map[str(tid)] = 1
    save_state()
    return _out(_gi(), _gd(groups))


def _un15_need(tid):
    # 返回 (条件是否达成, RewardID)
    row = _UN15_BY_ID.get(tid)
    if not row:
        return (False, 0)
    _tid, ct, qty, rid = row
    return (_un15_progress(ct, qty) >= _i(qty) if qty else True, _i(rid))


def _un16_need(tid):
    # 活跃奖励的阈值 = UN16.ActivityRequirement; 进度 = 当前周活跃(含**所有任务已达成的** Quantity)
    for (lid, rtype, need, rid) in _UN16:
        if lid == tid:
            return (_full_week_vital() >= _i(need), _i(rid))
    return (False, 0)


def _full_week_vital():
    # 本周活跃 = 所有"已达成(未领或已领)"任务的 Quantity 之和
    v = 0
    for (tid, ct, qty, rid) in _UN15:
        if _un15_progress(ct, qty) >= _i(qty):
            v += _i(qty)
    return v


def _week_vital():
    # 对外展示用: 已领取任务的 Quantity 之和 (与 _task_claimed 一致)
    claimed = _task_claimed()
    return sum(_i(qty) for (tid, ct, qty, rid) in _UN15 if str(tid) in claimed)


def handle_sociaty_task_reward(body_json):
    # 1136: req{taskid} -> [roleInfo, dropInfo]
    return _sociaty_reward(body_json, _UN15_BY_ID, _task_claimed(), _un15_need)


def handle_sociaty_liveness_reward(body_json):
    # 1137: req{taskid} -> [roleInfo, dropInfo]; 阈值 = UN16.ActivityRequirement
    tbl = {lid: 1 for (lid, _t, _n, _r) in _UN16}
    return _sociaty_reward(body_json, tbl, _liveness_claimed(), _un16_need)


def handle_sociaty_shop_info(body_json=None):
    # 1138: msg[1] = [[商品配置id, 周期内已购买数量]] (ipairs)
    rec = EQ_STATE.setdefault('sociaty_shop_buy', {})
    rows = [[_i(k), _i(v)] for k, v in rec.items()]
    return _out(rows)


@_guild_atomic
def handle_sociaty_shop_buy(body_json):
    """1139 妖盟商店购买 -> `[roleInfo, dropInfo, num]`。

    ★★2026-10-16（任务书 C②）：修前把 `UN02.GoodsId` **当成奖励组**去查 `T02_Reward`
      （`_grant_group(gid)`），而且**完全忽略 `GoodsCount`** —— 后果是"买小仙桃(x1) 得到的是
      `T02_Reward.GroupID=1013` 那一组的内容（发玩家经验 20）"，钱付了、东西错了。
      `UN02` 的四个列本来就是 `REWARD_NEWTYPE` 意义上的 **(类型, 静态id, 数量)**：
        · 13 行 GoodsType=2（道具，如 1013 小仙桃 / 2003 药 / 1012）
        · 1 行 GoodsType=8（唤妖符）· 2 行 GoodsType=18（宝箱）· 1 行 GoodsType=22（头像框）
      ⇒ 现在按 `(GoodsType, GoodsId, GoodsCount × count)` 走 `handlers_bag.grant_entries`
        （与商城/邮件/礼包**同一套**类型分派），类型未实现就**扣费前**拒绝。
    """
    from handlers_game import _role_info as _gi, _drop as _gd
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        req = {}
    sid = _i((req or {}).get('shopItemId'))
    # ⚠ 同 1166：显式 `count=0` 不能被 `max(1, …)` 悄悄变成 1（缺省才取 1）。
    _raw_cnt = (req or {}).get('count')
    cnt = 1 if _raw_cnt in (None, '') else _i(_raw_cnt, 0)
    if cnt < 1 or cnt > _UN_SHOP_BUY_MAX:
        return b'[0,{}]'                           # 单次数量上限（无表源，保守护栏，见常量注释）
    if not _is_member():
        return b'[50000]'
    if sid not in _UN02:
        return b'[34]'                             # 物品不存在
    gt, gid, gcount, limit, ptype, price = _UN02[sid]
    # ---- 扣费前：货币与商品类型都必须已实现 ----
    if ptype != 6:
        return b'[0,{}]'                           # 表里 17 行全是 6=个人妖盟财富；其余无源
    if gt not in _UN_SHOP_GOODS_TYPES:
        print('[UN02] 1139 未实现的 GoodsType=%s (id=%s) -> 扣费前拒绝' % (gt, sid), flush=True)
        return b'[0,{}]'
    rec = EQ_STATE.setdefault('sociaty_shop_buy', {})
    bought = _i(rec.get(str(sid)), 0)
    if limit and bought + cnt > limit:
        return b'[52]'                             # 已达到购买次数
    total = price * cnt
    sg = _sg()
    if _i(sg.get('wealth', 0)) < total:
        return b'[50046]'                          # 工会财富值不足
    # ---- 发货（类型分派只有一份实现；GoodsCount 必须乘上去）----
    import handlers_bag as _hb
    _frag = _hb.grant_entries([(int(gt), int(gid) if str(gid).isdigit() else gid, int(gcount) * cnt)])
    groups = []
    if _frag and _frag != '[]':
        try:
            groups = json.loads(_frag)[1]
        except Exception:
            groups = []
    # ★★2026-10-16（B 复核 4）：转成邮件也算发货成功（否则会"不扣财富 + 邮件里已有货"双份）
    _mailed = int(getattr(_hb, '_GRANT_MAILED', 0) or 0)
    if not groups and not _mailed:
        print('[UN02] 1139 id=%s 发放失败 -> 不扣财富' % sid, flush=True)
        return b'[51]'                             # 暂无数据（发放失败；一分财富都不扣）
    # ★财富在**发放成功之后**才扣（修前也是这个顺序，这里保持不变并写明为什么）
    _guild_add_wealth(-total, sg)
    rec[str(sid)] = bought + cnt
    save_state()
    # ★2026-10-16（D 复核 · 逐槽对客户端）：第 3 槽 `num` = "大于 0 表示怪物背包已满"
    #   （`NetSociatyShopBuy.lua` 同款注释）。溢出的妖怪已转进邮件，就把只数报出去。
    return _out(_gi(), _gd(groups), int(_mailed))


@_guild_atomic
def handle_sociaty_shop_upgrade(body_json=None):
    # 1142: 只回调; 消耗 = UN05[当前商店等级]; 码 50000未入会/50044满级/50046财富不足
    if not _is_member():
        return b'[50000]'
    sg = _sg()
    lv = max(1, _i(sg.get('shopLevel', 1), 1))
    if lv >= _SHOP_MAX_LEVEL:
        return b'[50044]'                          # 工会商店已经满级
    cost = _i(_UN05.get(lv), 0)
    if _i(sg.get('wealth', 0)) < cost:
        return b'[50046]'                          # 工会财富值不足
    sg['shopLevel'] = lv + 1
    _guild_add_wealth(-cost, sg)               # ★会级财富（多账号下写世界键）
    save_state()
    return b'[1,[]]'


def handle_sociaty_rank_info(body_json=None):
    # 1143: msg[1]=榜单 [[icon,level,rank,name,leaderName,memberNum,liveness]]
    #       msg[2]=我的同一 7 字段 ★客户端 `local meMsg = msg[2]` 后**直取 meMsg[1]**(无守卫) -> 必须给
    # ★2026-10-02 (D4=B): 修前 msg[1] 底下的 3 个妖盟（天罡北斗/地煞七十二/万妖盟）是**写死的假行**
    #   ⇒ 多人世界里看不到彼此的妖盟。现在改成**按账号档派生**（见 `_world_guilds`）。
    #   ⚠ `liveness` **服务端没有这个量 ⇒ 给 0**（铁律：不许编造）；我方那行仍沿用 `_week_vital()`。
    sg = _sg()
    img, _f = _head_pair()
    wg = _world_guilds()
    if wg is None:
        # ---- 单档：逐字节保持原样 ----
        mine = [img, _i(sg.get('level', 1), 1), 1, sg.get('name') or '未入盟',
                '测试', 1, _week_vital()]
        rows = [mine]
        for i, nm in enumerate(('天罡北斗', '地煞七十二', '万妖盟')):
            rows.append([1001 + i, 2 + i, 2 + i, nm, '盟主%d' % (i + 1), 8 + i * 3,
                         900 - i * 220])
        return _out(rows, mine)
    # ---- 多账号：真实妖盟 ----
    myname = str(sg.get('name') or '')
    my_group = next((g for g in wg if g['mine']), None)
    mine = [img, _i(sg.get('level', 1), 1), 1, myname or '未入盟',
            _own_player_name(), (my_group['members'] if my_group else 1), _week_vital()]
    rows = []
    for i, g in enumerate(wg[:10]):
        # ⚠ **不跳过自己那个会**：修前的写法是 `rows = [mine] + 3 个假盟` —— 也就是我方那行
        #   既在 `msg[2]`（我方行）里、**也在** `msg[1]`（榜单）的第一位。客户端按"榜单里也有我"
        #   渲染，跳过就会少一行（本轮护栏 S1/S2 一开始就是照"跳过"写的，实测才发现对不上）。
        rows.append([1001, g['level'], i + 1, g['name'], g['leader'], g['members'], 0])
    return _out(rows, mine)


def handle_sociaty_boss_list(body_json=None):
    # 1144: [chessLevel, 今日可开局次数, bossList=[[bossId, remainTime]]]
    #   `bossId` 用 UN07 的真实 StageID（客户端 `MSociatyTranscript:getIsOpening` 拿它跟
    #   表里的 StageID 比对；而 `VSociatyTranscriptBoss` 展示用的是 `BossStageID`）。
    #   ★★2026-10-08：多账号下取**世界键**的弈局（谁开的全盟都看得到）；单档逐字节原样。
    gw = _gworld()
    if gw is not None:
        g = _world_chess()
        if g is not None:
            v = _chess_view() or {}
            rows = []
            act = v.get('active') or {}
            if act:
                rows.append([_i(act.get('stage', 0)), max(0, _i(act.get('left', 0), 0)) * 1000])
            return _out(max(1, _i(v.get('level', 1), 1)), _i(v.get('num_left', 0), 0), rows)
    _reset_boss_day()
    b = _sgb()
    rows = []
    if _UN07:
        row = _UN07[0]
        rows.append([_i(row.get('boss_stage')) or _i(row.get('stage')), _i(row.get('findtime')) * 1000])
    return _out(max(1, _i(b.get('chess', 1), 1)), _i(b.get('num', 0)), rows)


def handle_sociaty_boss_rank_info(body_json):
    # 1155: msg[1]=最后一击 [rank,roleId,roleName,hitRate,time,headID,headFrame]
    #       msg[2]=排行列表 (ipairs) 每行同 7 字段
    #       msg[3]=我的同 7 字段
    #   ★三处都**没有 or{} 守卫** -> 必须全给
    #   ★★2026-10-08：多账号下优先用**本局共享伤害**（`chess.damage`，见 `_chess_rank_rows`）；
    #     没有开启中的弈局 -> 回落到"同会成员的累计伤害"（原口径，保证面板不空）。
    img, frame = _head_pair()
    shared = _chess_rank_rows()
    members = None if shared else _world_guild_members()
    if shared is None and members is None:
        # ---- 单档：逐字节保持原样（含那 3 条假行）----
        me = [1, 'bot_me', '测试', '12.5', int(time.time()), img, frame]
        rank_rows = [me]
        for i, nm in enumerate(('半城烟沙', '一剑霜寒', '白衣渡江')):
            rank_rows.append([2 + i, 'bot_r%d' % i, nm, '%d.5' % (20 - i * 5),
                              int(time.time()) - 600 * (i + 1), img, frame])
        last = list(rank_rows[-1])
        return _out(last, rank_rows, me)
    # ---- 多账号 ----
    #   ⚠ `hitRate`（第 4 位）服务端**没有这个量 ⇒ 给 '0'**（铁律：不许编造）。
    #   NetSociatyBossRankInfo.lua:34/50 分别读第 1 槽 rank / 第 2 槽 roleId；
    #   roleId 不经 tonumber，必须与 roleInfo[23] 同为真实 numeric ID。
    gw = _gworld()
    me_rid = gw.role_id_of(gw.current_aid())
    shared = _chess_rank_rows() if shared is None and gw is not None else shared
    if shared:
        # 共享行已含 `at`（该成员的最后一击时间）；1155 的第 5 槽是"伤害-最后时间"。
        rank_rows = [[rank, r[0], r[1], '0', _i(r[4], 0) or int(time.time()), r[5], r[6]]
                     for rank, r in enumerate(shared, 1)]
    else:
        rank_rows = [[rank, m['role_id'], m['name'], '0', int(time.time()),
                      m['head'], m['frame']] for rank, m in enumerate(members or [], 1)]
    me = next((r for r in rank_rows if r[1] == me_rid),
              [1, me_rid, _own_player_name(), '0', int(time.time()), img, frame])
    if not rank_rows:
        rank_rows = [me]
    # msg[1] = **最后一击**（客户端 `NetSociatyBossRankInfo.lua:34-40/62-65` 拿它的 roleId
    # 在 msg[2] 里反查名次）。有击杀记录 -> 指向击杀者那一行；没有 -> 用榜首行。
    # ⚠ 必须是**榜单里的那一行**：否则客户端按 roleId 找不回来（`lastUserInfo.rank` 留在 0）。
    g = _world_chess()
    k_rid, _k_name = gw.chess_killer(g) if g is not None else (0, '')
    k_row = next((r for r in rank_rows if r[1] == k_rid), None) if k_rid else None
    last = list(k_row or rank_rows[0])
    last[4] = _i(last[4], 0) or int(time.time())
    return _out(last, rank_rows, me)


def handle_sociaty_boss_hp_push_payload():
    # 1152: 服务端主动推 [bossId, 当前血量, my[4], rankList[[4]]]
    #   ★★2026-10-08 修**编造血量**：修前第 2 槽恒 `10000`，而客户端
    #     `StageDelegateChess:21-41` 把它交给 `setBossCheeHp()` 当**当前血量**显示 ⇒
    #     任何妖神都显示成 2%（真值应从 `UN07.HP` 起扣，见 guild_world 的 chess.hp）。
    gw = _gworld()
    shared = _chess_rank_rows() if gw is not None else None
    if gw is not None:
        g = _world_chess()
        v = (_chess_view() or {}) if g is not None else {}
        act = v.get('active') or {}
        if g is not None and (act or shared):
            boss_id = _i(act.get('stage', 0)) or _chess_boss_id()
            hp = _i(act.get('hp', 0), 0)
            rows = [[_i(r[0], 0), str(r[1]), rank, _i(r[3], 0)]
                    for rank, r in enumerate(shared or [], 1)]
            me_rid = gw.role_id_of(gw.current_aid())
            me = next((r for r in rows if r[0] == me_rid), [me_rid, _own_player_name(), 0, 0])
            if not rows:
                rows = [me]
            return _out(boss_id or 1001, hp, me, rows)
    # ---- 回落：单档 / 无开启中的弈局（原口径，逐字节保持单档那段不变）----
    b = _sgb()
    img, frame = _head_pair()
    rows0 = _UN07[0] if _UN07 else {}
    bid = _i(rows0.get('boss_stage', 0)) or 1001
    members = None if shared else _world_guild_members()
    if members is None:
        # ---- 单档：逐字节保持原样（含那 2 条假行）----
        me = ['bot_me', '测试', 1, _i(b.get('damage', 0))]
        rows = [me]
        for i, nm in enumerate(('半城烟沙', '一剑霜寒')):
            rows.append(['bot_r%d' % i, nm, 2 + i,
                         max(0, _i(b.get('damage', 0)) - 1000 * (i + 1))])
        return _out(bid, 10000, me, rows)
    # ---- 多账号：同会**真实成员** ----
    #   行形状 `[roleId, roleName, rank, damage]`；roleId 是与观察者无关的真实 ID。
    #   ⚠ 那个第 3 位原本是 `2+i`（假名次），服务端没有"名次"这个量 ⇒ 给 **0**（铁律）。
    rows = [[m['role_id'], m['name'], 0, m['dmg']] for m in members]
    # ★"我方那行"的判据 = **真实 roleId**（不再硬编码 1 —— 真值化后 1 只属于 `u0001`）
    _gw2 = _gworld()
    _me_rid = _gw2.role_id_of(_gw2.current_aid()) if _gw2 is not None else 1
    me = next((r for r in rows if r[0] == _me_rid),
              [_me_rid, _own_player_name(), 0, _i(b.get('damage', 0))])
    if not rows:
        rows = [me]
    return _out(bid, 10000, me, rows)


def handle_sociaty_join_outtime_payload():
    # 1160: [冷却结束绝对时间] (客户端 getClockStrByTime 后弹提示)
    b = _sgb()
    return _out(_i(b.get('join_cd_until', 0)))


def handle_sociaty_data_payload():
    # 1134: 服务端主动推 [positionCode, activeWeek, wealth, wealthWeek, wealthGiveGang, todaydonate]
    sg = _sg()
    return _out('1', _week_vital(), _i(sg.get('wealth', 0)), _i(sg.get('wealth', 0)),
                _i(sg.get('todaydonate', 0)), _i(sg.get('todaydonate', 0)))


def handle_sociaty_sync_info(body_json=None):
    # 1141: 客户端拿它当"重新拉公会数据" -> 直接回 1121 同形 (客户端会自己刷新面板)
    return handle_sociaty_info()


# ---------- "只回调"型: 统一回 [1,[]] (客户端 recvMessage 不读 msg[N]) ----------
def handle_sociaty_simple(body_json=None):
    return b'[1,[]]'


def handle_sociaty_apply_join(body_json=None):
    # 1126: 入会审批 req{roleid,agree}（`NetApplySociaty.lua:21-23` 逐行读）
    #   ❌ 修前：**只回调，什么都没发生**（`handle_sociaty_apply_join` 这个名字有误导 ——
    #      它不是"申请入会"，1124 才是）。
    #   ★2026-10-02（本轮）多账号：读世界键里我这个会的 `applications`，按 roleid 找到申请人，
    #     同意 -> 写进名册（`members`）、拒绝 -> 删掉那条申请。**单档仍是只回调**。
    gw = _gworld()
    if gw is None:
        return b'[1,[]]'
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0]'
    if not isinstance(req, dict):
        return b'[0]'
    role_id = (req or {}).get('roleid')
    try:
        role_id = int(str(role_id).strip())
    except Exception:
        return b'[30002]'                     # 申请人id错误
    agree = 1 if _i((req or {}).get('agree'), 0) == 1 else 0
    try:
        code = gw.approve(role_id, agree)
    except Exception:
        return b'[0]'
    if code is None:
        return b'[1,[]]'
    if code != 1:
        return ('[%d]' % code).encode('utf-8')
    return b'[1,[]]'


@_guild_atomic
def handle_sociaty_quit(body_json=None):
    # 1129: 退出公会 -> 清空本地公会名 (面板回到"未入盟")
    #   ★2026-10-02（本轮）多账号：**退会真的从世界键的名册里摘掉自己**；
    #     盟主退会 = 解散（否则名册里没有人能审批/踢人），其余成员一并解绑。
    sg = _sg()
    gw = _gworld()
    if gw is not None and sg.get('guild_id'):
        code = gw.leave()
        if code != 1:
            return ('[%d]' % (code or 0)).encode('utf-8')
        _sync_my_guild_cache()
        sg = _sg()
    if sg.get('name'):
        sg['name'] = ''
        sg['id'] = ''
        sg['guild_id'] = ''
    save_state()
    return b'[1,[]]'


@_guild_atomic
def handle_sociaty_dismiss(body_json=None):
    # 1140: 解散公会
    #   ★2026-10-02（本轮）多账号：真的解散（名册清空 + 全体成员的弱引用清掉）；单档同 1129。
    gw = _gworld()
    if gw is not None and _sg().get('guild_id'):
        code = gw.leave(dismiss=True)
        if code != 1:
            return ('[%d]' % (code or 0)).encode('utf-8')
        _sync_my_guild_cache()
    return handle_sociaty_quit()


@_guild_atomic
def handle_sociaty_notice(body_json=None):
    # 1128: 设置公告 req{notice}
    #   ★2026-10-02（本轮）多账号：公告是**会级字段** —— 修前只写自己那一份，别的成员永远看不到。
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    # ★★2026-10-16（请求字段对账）：`NetNoticeSociaty.lua` 发的是 **`annocontent`**，修前这里读 `notice` ⇒ 公告永远存空（改公告没反应）。旧键保留作兜底。
    txt = str((req or {}).get('annocontent') or (req or {}).get('notice') or '')
    gw = _gworld()
    if gw is not None and _sg().get('guild_id'):
        code = gw.set_notice(txt)
        if code != 1:
            return ('[%d]' % (code or 0)).encode('utf-8')
        _sync_my_guild_cache()
        save_state()
        return b'[1,[]]'
    if txt:
        _sg()['notice'] = txt[:200]
        save_state()
    return b'[1,[]]'


@_guild_atomic
def handle_sociaty_join_type(body_json=None):
    # 1132: 修改入会类型 req{type,threshold}
    #   ★2026-10-02（本轮）多账号：**会级字段**（全盟共用一套入会设置）；修前只写自己那份，
    #     于是"盟主设了自动入会"对其他成员和申请人都不生效。
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    gw = _gworld()
    if gw is not None and _sg().get('guild_id'):
        # ★★2026-10-16（请求字段对账）：`NetJoinTypeSociaty.lua` 发的是 **`isautojoin`**，修前读 `type` ⇒ 入会类型永远被设成 0。
        code = gw.set_join_type(_i((req or {}).get('isautojoin') or (req or {}).get('type')),
                                _i((req or {}).get('threshold')))
        if code != 1:
            return ('[%d]' % (code or 0)).encode('utf-8')
        _sync_my_guild_cache()
        save_state()
        return b'[1,[]]'
    sg = _sg()
    try:
        sg['autoJoin'] = _i((req or {}).get('isautojoin') or (req or {}).get('type'))
        sg['autoJoinThreshold'] = _i((req or {}).get('threshold'))
    except Exception:
        pass
    save_state()
    return b'[1,[]]'


def handle_sociaty_post_change(body_json=None):
    # 1130: 职位变更 req{roleid,positioncode}（`NetPostChangeSociaty.lua:22-25`）
    #   ❌ 修前：只回调。 ★2026-10-02（本轮）多账号：真的改名册里的职位（盟主/副盟主/成员）。
    gw = _gworld()
    if gw is None:
        return b'[1,[]]'
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0]'
    if not isinstance(req, dict):
        return b'[0]'
    role_id = (req or {}).get('roleid')
    try:
        role_id = int(str(role_id).strip())
    except Exception:
        return b'[50017]'                     # 只可以对同公会玩家操作
    # ★★ `positioncode` **是字符串**（客户端 `VSociatyMember.lua:498/506/514/522` 发
    #    "L"/"FL"/"A"/"N"）—— **绝不能**在这里做 `_i()`：那会把 "FL" 变成 0，
    #    于是 1130 一律回 `50019 非法职位代码`（本轮实测踩到）。解析交给
    #    `guild_world._pos_of_code`（它同时认字符串与旧数字码）。
    code = (req or {}).get('positioncode')
    try:
        r = gw.set_position(role_id, code)
    except Exception:
        return b'[0]'
    if r is None:
        return b'[1,[]]'
    if r != 1:
        return ('[%d]' % r).encode('utf-8')
    return b'[1,[]]'


def handle_sociaty_kick_out(body_json=None):
    # 1127: 踢人 req{roleid}（`NetKickOutSociaty.lua:21-24`）
    #   ❌ 修前：只回调。 ★2026-10-02（本轮）多账号：真的从名册里摘掉（并解绑对方的弱引用）。
    gw = _gworld()
    if gw is None:
        return b'[1,[]]'
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0]'
    if not isinstance(req, dict):
        return b'[0]'
    role_id = (req or {}).get('roleid')
    try:
        role_id = int(str(role_id).strip())
    except Exception:
        return b'[30002]'                     # 申请人id错误
    try:
        code = gw.kick(role_id)
    except Exception:
        return b'[0]'
    if code is None:
        return b'[1,[]]'
    if code != 1:
        return ('[%d]' % code).encode('utf-8')
    return b'[1,[]]'


@_guild_atomic
def handle_sociaty_join(body_json=None):
    # 1131: 直接加入 req{gangid}（`NetJoinSociaty.lua:23-26`）
    #   ❌ 修前：只回调（注释写"私服无其他公会, 客户端会走 1123 创建" —— 现在真有别的公会了）。
    #   ★2026-10-02（本轮）多账号：直接写进名册；目标会开了自动入会且我够积分时才允许。
    gw = _gworld()
    if gw is None:
        return b'[1,[]]'
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0]'
    if not isinstance(req, dict):
        req = {}
    gangid = str((req or {}).get('gangid') or '').strip()
    if not gangid:
        return b'[30001]'                     # 公会Id错误
    try:
        code, _joined = gw.apply_join(gangid)
    except Exception:
        return b'[0]'
    if code is None:
        return b'[1,[]]'
    if code != 1:
        return ('[%d]' % code).encode('utf-8')
    _sync_my_guild_cache()
    save_state()
    return b'[1,[]]'


def handle_sociaty_single_boss_get(body_json=None):
    # 1145: 单个妖神详情 -> [**剩余血量**, 剩余击败时间, 今日进入次数]
    #   ★★2026-10-08：修前第 1 槽发的是 `BossStageID`（一个 6101 之类的**关卡 id**），
    #     而客户端 `VSociatyTranscriptBoss:477-484` 拿它当**当前血量**去除 `UN07.HP`
    #     ⇒ 血条算出 `6101/500000` = 1%，任何妖神、任何时候都是 1%。
    #     现在发世界键里的真实血量；未开启的妖神发**满血**（客户端据此画 100%）。
    # ⚠ `req` 必须在**分支之前**解析：2026-10-11 实测这里曾是"两处赋值、其中一处在 if 里"
    #   的形状 —— 自动补守卫的脚本把 `req = json.loads(...)` 留在 `if gw is not None:` 里面，
    #   却在分支外面读 `req` ⇒ 多账号路径下直接 `UnboundLocalError`（`_fuzz_test` 轰出来的）。
    #   解析点收敛到函数顶部一次，两条路径共用。
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    gw = _gworld()
    if gw is not None:
        row = _chess_stage_row((req or {}).get('bossid')) or (_UN07[0] if _UN07 else None)
        if row is None:
            return _out(0, 0, 0)
        g = _world_chess()
        v = (_chess_view() or {}) if g is not None else {}
        act = v.get('active') or {}
        hp_max = max(1, _i(row.get('hp'), 1))
        if act and _i(act.get('stage', 0)) == _i(row.get('stage', 0)):
            return _out(_i(act.get('hp', hp_max), hp_max), max(0, _i(act.get('left', 0), 0)) * 1000,
                        _i(v.get('entry_left', 0), 0))
        return _out(hp_max, 0, _i(v.get('entry_left', 0), 0))
    _reset_boss_day()
    b = _sgb()
    if not _UN07:
        return _out(0, 0, 0, 0)
    row = _UN07[0]
    return _out(_i(row.get('boss_stage')) or _i(row.get('stage')), _i(row.get('findtime')) * 1000,
                _i(b.get('num', 0)), max(1, _i(b.get('chess', 1), 1)))


@_guild_atomic
def handle_sociaty_chess_result(body_json):
    # 1153: 弈局结算 -> [roleInfo, dropInfo, ...]
    #   ★★2026-10-08：多账号下**伤害不在这里记**（修前这里读的是请求体里的 `damage`，
    #     而 `NetSociatyChessResult:29-35` 发的字段是 `bossId`/`ourside`/`mvp` ——
    #     根本没有 `damage` ⇒ 那行永远是 0）。真实伤害走 1157（每击上报），
    #     这里只负责"发奖"。
    #   ★★2026-10-26（独立复盘 P0）：领奖判据从**进程内票号**改成**世界档持久账本**。
    #     修前三个可利用面（均已在 `_guild_chess_test.py` 写成红例）：
    #       · 票号按 (stage, killer_rid, killer_at) 生成 ⇒ 击杀那一次票号变了 ⇒
    #         同一账号击杀前领过、击杀后还能再领（一局两次）；
    #       · 票号表在内存 ⇒ 重启后可再领；
    #       · 从不校验"领奖者是否参战"，击杀后 `active` 被清空 ⇒ 伤害行空 ⇒
    #         任何会内成员发 1153 都落到 Rank 0（UN09 的**最后一击**奖励组）。
    #     现在是 `chess['claims'][局号][aid][display|rank]`：
    #       · 局号 = `active.started`，击杀时抄进 `killer` ⇒ 击杀前后同一个局号；
    #       · 参战判据 = `killer.rows` / `active.damage` 的账号集合；
    #       · 名次取击杀快照，击杀者才是 Rank 0；
    #       · 判定、发奖、记账在同一个 `guild_transaction` 里（journal 可重放）。
    #   ★★2026-10-08（战斗奖励轮）：**接官方掉落表**。
    #     修前恒发死值 500 金 + 200 经验（私服编的），而官方两张表一直就写着发什么：
    #       · `UN07.DisplayReward` = 每个妖神的**解锁奖励组**（4401~4406，全部是合法 T02 组）
    #         —— 客户端 `VSociatyTranscriptBoss:269` 的"獎勵"一栏就是照它显示的；
    #       · `UN09_UnionBossRanking(StageID, Rank, GroupID)` = **伤害名次**奖励组
    #         （Rank 0 = 最后一击 / 1..10 名，共 66 行，全部是合法 T02 组）
    #         —— 客户端 `VSociatyTranscriptRank:104` 用它显示名次奖励。
    #     ⇒ 参战给 DisplayReward，击杀（= 有击杀记录的那一局）再按**本局伤害名次**补 UN09。
    #     ⚠ 单档没有共享弈局状态 ⇒ 保留原死值路径（逐字节不变）。
    from handlers_game import _role_info as _gi, _drop as _gd
    from handlers_task import _grant_group as _gg
    from state import player_add_exp
    b = _sgb()
    _reset_boss_day()
    _gw = _gworld()
    if _gw is None:
        # ---- 单档：原样（那边 `sociaty_boss.num` 就是"今日弈局次数"的唯一来源）----
        b['num'] = _i(b.get('num', 0)) + 1
    gold = 500
    groups, used_official = [], False
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    _req_stage = _i((req or {}).get('bossId'), 0) if isinstance(req, dict) else 0
    rows, k_rid = [], 0        # ★共享世界：本局伤害名次 / 最后一击（下面按需填充）
    # ★★2026-10-16（任务书 C⑥）：**一局只发一次**。修前这条只看请求体：同一局重复发 1153
    #   就重复发奖（客户端只看 statecode，重放完全能刷）。
    #   判据（shared-world）：
    #     ① 必须有一局**刚结束或进行中**的弈局（`chess_record` 的 `killer` / `active`），
    #        没有就是"凭空结算"；
    #     ② `bossId` 必须与那一局的 StageID 一致；
    #     ③ 票号 = (stage, killer_rid, killer_at)（或进行中局的 started）——同一局第二次
    #        必然 `replay`。`chess_start` 开新局时会清 `killer`，所以下一局拿到的是新票号。
    #   ⚠ 单档没有共享弈局状态：保持原路径（X9 要求单档 500 金 + 200 经验逐字节不变）。
    if _gw is not None and _req_stage and _chess_stage_row(_req_stage) is None:
        # ★bossId 表外必须在**一次性判定之前**回 503（客户端码表里的"掉落组ID错误"），
        #   否则会被"没有进行中的弈局"抢先吞掉，错误码就变了。
        return b'[503]'
    _round = None
    _got_display = False
    _rank_num = 0
    _k_rid = 0
    if _gw is not None:
        try:
            _round = _gw.chess_round(_world_chess())
        except Exception as _e:
            print('[CHESS1153] !! 读取弈局状态失败(按不发放处理): %r' % (_e,), flush=True)
            _round = None
        if _round is None:
            print('[CHESS1153] 没有进行中/刚结束的弈局 -> 不发奖', flush=True)
            return _out(0, 0, _gi(), [], _gd([]), 0, 0, [])
        _stage = _i(_round.get('stage'), 0)
        _boss_stage = _i(_round.get('boss_stage'), 0)
        # ⚠ 客户端的 `bossId` 是 **UN07 的 BossStageID**（`chess_start(stage, boss_stage,…)`
        #   的第二个参数），不是 `stage`。两者都要接受 —— 判据用 `_chess_stage_row(bossId)`
        #   反查出它属于哪一局（X8 样本：stage=1002 / bossId=6102 / UN07 里同一条）。
        if _req_stage and _stage:
            _row_req = _chess_stage_row(_req_stage) or {}
            if _req_stage not in (_stage, _boss_stage) and _i(_row_req.get('stage'), 0) != _stage:
                print('[CHESS1153] bossId=%s 不属于弈局 stage=%s -> 不发奖'
                      % (_req_stage, _stage), flush=True)
                return _out(0, 0, _gi(), [], _gd([]), 0, 0, [])
        _aid = _gw.current_aid()
        if not _gw.chess_participated(_world_chess(), _aid, _round):
            print('[CHESS1153] aid=%s 未参加本局 -> 不发奖' % (_aid,), flush=True)
            return _out(0, 0, _gi(), [], _gd([]), 0, 0, [])
        _key = _round.get('key')
        _row = _chess_stage_row(_req_stage) if _req_stage else None
        if _row is None:
            _row = _chess_stage_row(_stage) or {}
    if _gw is not None:
        # 参战奖（UN07.DisplayReward）：**每局每账号一次**（击杀前领过就不再发）
        if _gw.chess_claim(_world_chess(), _key, _aid, 'display'):
            _dr = _i(_row.get('reward'), 0)
            if _dr:
                _g, _ok = _gg(_dr, roll=True, mult=1)   # 官方组按 Rate 独立概率抽，**不放大**
                if _ok:
                    groups.extend(_g or [])
                    used_official = True
                else:
                    print('[CHESS1153] !! UN07.DisplayReward=%s 发放失败 -> 走兜底' % _dr)
            _got_display = True
        # 名次奖（UN09）：**只有局已结束**才发；名次取击杀快照，击杀者才是 Rank 0
        _k_rid = _i(_round.get('killer_rid'), 0)
        _rank = _gw.chess_rank_of(_world_chess(), _aid, _round)
        _rank_num = _i(_rank, 0)
        if _round.get('over') and _k_rid and _rank is not None:
            _rg = _UN09_RANK.get((_stage, _rank_num))
            if _rg and _gw.chess_claim(_world_chess(), _key, _aid, 'rank'):
                _g, _ok = _gg(_rg, roll=True, mult=1)
                if _ok:
                    groups.extend(_g or [])
                    used_official = True
                print('[CHESS1153] UN09 名次奖励 rank=%s 组=%s -> %d 条'
                      % (_rank_num, _rg, len(_g or [])))
        if not _got_display and not groups:
            # 已领过（display+rank 都领完）或本局没有可发奖的项 -> 静默零奖励
            print('[CHESS1153] 本局奖励已领过（局号=%s aid=%s） -> 零奖励'
                  % (_key, _aid), flush=True)
            return _out(0, 0, _gi(), [], _gd([]), 0, 0, [])
    if not used_official and (_gw is None or _got_display):
        # 兜底：单档 / 单档旧行为 / 官方表取不到 —— 保持私服原值（绝不空手）
        EQ_STATE['gold'] = _i(EQ_STATE.get('gold', 0)) + gold
        groups.append([3, [[gold]]])
    if _gw is None or _got_display:
        player_add_exp(200)
        # ★2026-10-16（任务书 E）：T37 CT=33「弈局战斗：参与 1 次弈局战斗」。
        #   判据 = 1153 第一次真的走到发奖这一步（补领名次奖的那次不再重复计数）。
        try:
            from handlers_task import bump_task as _bt, CT_CHESS as _CT_CHESS
            _bt(_CT_CHESS, 1)
        except Exception as _e:
            print('[CT33] !! 弈局战斗计数失败(不影响结算): %r' % (_e,), flush=True)
    save_state()
    if _gw is None:
        # 单档：形状**逐字节不变**（HANDOFF「必须继续守住的选择」：单档 1153 = 500 金 + 200 经验）
        return _out(_gi(), _gd(groups))
    # ★★2026-10-11：共享世界模式必须按**装机版客户端**的 8 槽契约回（真机验收前置修复）。
    #   装机源码 `app/network/sociaty/NetSociatyChessResult.lua:49-56`：
    #       msg[1]=result  msg[2]=rankNum  msg[3]=roleInfo msg[4]=ourFormation
    #       msg[5]=dropInfo msg[6]=talentId msg[7]=killerroleid msg[8]=exDropItems
    #   修前这里只回 `[roleInfo, dropInfo]` 两槽 ⇒ 客户端把 **dropInfo 表**当成 rankNum，
    #   而 `VWarChessVictory:refreshInvadeUI()`（装机 `:108`）要
    #       `refStr .. ":" .. self._victoryInfo:getRankNum()`
    #   —— **表不能参与字符串拼接** ⇒ `parseUI` 当场抛错 ⇒ 弈局结算面板永远出不来。
    #   （与 1121 第 9 槽 `pairs(0)` 同族：服务端"应答正确"、客户端解析期抛错。）
    #   ⚠ 核心判据：`msg[2]` 必须是 **JSON number**，不是表、不是字符串。
    #   ⚠ 无数据源的字段一律 0（铁律）：ourFormation/talentId/exDropItems 服务端没有。
    return _out(1, _rank_num, _gi(), [], _gd(groups), 0, _k_rid, [])


# ============ 1124 SOCIATY_APPLY_JOIN (v28 补注册) ============
# 背景: 本表 1121~1160 曾经【唯独 1124 没注册】(交接档 v27 坑点 #54) —— 注释写了"已补齐",
#       但 AST 取键集合一跑就能看出缺一个。属于"承诺了却没注册"。
# 客户端契约 (app/network/sociaty/NetApplyJoinSociaty.lua:21-41 逐行读):
#       sendMessage(sociatyId)  ->  req {"gangid": tostring(sociatyId)}
#       recvMessage: statecode ~= 1 -> showTips(m_errorCode[statecode]);
#                    statecode == 1 -> 只调 m_callFunc(), 不读 msg 内容
#       m_errorCode 里真实存在的码: 0(通用失败) / 30001(Id错误) / 13 / 50007(已入会)
#                                   / 50001(公会不存在) / 50011(重复申请)
# ⚠ 不要复用 handle_sociaty_apply_join —— 那个名字有误导, 它实际是 **1126 SOCIATY_APPLY(入会审批)**
#   的处理器。两者语义不同。
# 多账号下 1122 已返回世界中的妖盟，申请入口可达；单档仍沿用原兼容路径。
@_guild_atomic
def handle_sociaty_apply_join_req(body_json):
    """1124 申请入会（`NetApplyJoinSociaty.lua:21-24` 的 `req{"gangid"}`）。

    ★2026-10-02（本轮）多账号：**真的把申请写进世界键里那个会的 `applications`**；
      单档保持原样（只记 `apply_gangid`，因为单档根本不存在"别人批我"这件事）。

    ⚠ 码分两套，别混（两处客户端 `m_errorCode` 是**不同**的表）：
      1124 用 `30001` id 错 / `50007` 已入会 / `50001` 不存在 / `50011` 重复申请；
      1131 用 `30001` / `50007` / `50001` / `50012` 已满 / `50021` 已发送申请 / `50022` 未达标准。
      ⇒ `guild_world.apply_join` 只返回**语义**，码由各自的 handler 翻。
    """
    sg = _sg()
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0]'
    if not isinstance(req, dict):
        req = {}
    gangid = str((req or {}).get('gangid') or '').strip()
    if not gangid:
        return b'[30001]'                      # Id 错误
    gw = _gworld()
    if gw is None:
        # ---- 单档：原判据（`sociaty['id']` 就是"有没有会"的唯一标志）----
        if sg.get('id') and str(sg.get('id')) != '':
            return b'[50007]'
    else:
        # ★2026-10-02（本轮）多账号："有没有会"**必须问世界键的名册**，不能再看
        #   `sociaty['id']` —— 那个字段是**缓存**，踢人/退会/解散之后它可能还是旧值
        #   （本轮实测：被踢的人再申请拿到 `50007 你已经加入了一个工会`，而名册里根本没有他）。
        gid_mine, g_mine = gw.my_guild()
        if g_mine is not None:
            return b'[50007]'
    if gw is not None:
        try:
            code, joined = gw.apply_join(gangid)
        except Exception:
            return b'[0]'
        if code == 1:
            if joined:
                _sync_my_guild_cache()
                save_state()
                return b'[1,[]]'               # 自动入会：直接进去了
            # 待审批：把"申请中"的冷却时间记进档里供 1160 推送用（客户端显示"加入公会cd中"）
            b = _sgb()
            b['apply_gangid'] = gangid
            b['apply_until'] = int(time.time()) + 86400
            b['join_cd_until'] = int(time.time()) + 3600
            save_state()
            return b'[1,[]]'
        return ('[%d]' % code).encode('utf-8')      # 50001 不存在 / 50011 重复申请 / 50007
    # ---- 单档：原行为（逐字节不变）----
    b = _sgb()
    b['apply_gangid'] = gangid
    b['apply_until'] = int(time.time()) + 86400
    save_state()
    return b'[1,[]]'


# ============================================================================
# 妖盟红包 1187 / 1188 / 1189（新包 A0 第三批，2026-10-11）
# ----------------------------------------------------------------------------
# **数据层的说明在这里只写差异**；"为什么修前是那三个玩家现象"、错误码出处
# （`T41_CodeKey.codeValue`）、以及会级实体形状全部在 `guild_world.py` 的
# 「妖盟红包」段头。协议层只说三件事：
#
#   1187 SOCIATY_GETREDBAGLIST req{gangId}
#        -> [1,[剩余发送次数, [[redId,redType,红包个数,剩余个数,内容,状态,
#                              发包人名,头像,头像框,总金额], ...]]]
#   1188 SOCIATY_SENDREDBAG    req{moneyType,moneyNum,redType,redNum,content}
#        -> **与 1187 逐槽同形**（客户端两处共用同一个 netCallFunc；
#           `NetSociatySendRedBag:recvMessage:58-59` 发完立刻按 1187 的形状刷新界面）
#   1189 SOCIATY_GETREDBAGINFO req{redId,gangId}
#        -> [1,[ucode,goodsId,goodsType,goodsNum,[[领取人,领到多少,手气], ...]]]
#
# ★★ 1188 的**实参顺序**是 `sendMessage(moneyType, moneyNum, redType, redNum, content)`
#    （`VSociatySendRedBag.lua:139`：`6, selectGoldValue, 1, 红包个数, 想说的话`）
#    —— `moneyType` 恒 6（鬼道积分），`redType` **客户端硬编码 1**。
#    ⇒ **1188 只接受 redType=1**：客户端根本不发 2，而 2（充值礼包）在官方是"成员充值后
#      系统代发、不消耗发包人任何物品"（T41:1219）—— 放行 2 等于开了"白送全场礼包"的口子。
# ★ `remain` 必须是**数字**：客户端 `tonumber(msg[1]) or 0`，发给它表就恒 0。
# ★ 内容含中文 ⇒ 必须 `ensure_ascii=False`（本文件的 `_out()` 不带这个开关，
#   而 `handle_sociaty_info`/`handle_sociaty_list` 都显式带了 —— 跟随后者）。
# ============================================================================
_UN18 = {}                      # ID -> row dict（UN18_UnionRedPocket，表根**有**，无需迁移）
try:
    _t = _load_json('UN18_UnionRedPocket.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        _UN18[_i(_r[_h['ID']])] = {k: _r[v] for k, v in _h.items()}
except Exception:
    _UN18 = {}

_T23_BY_ID_CACHE = {}


def _t23_by_id(row_id, fallback):
    """`T23_Function` 按 **`ID`** 列取 `Value`（与 `_t23_value()` 按 `Type` 列取**不是一回事**）。

    ⚠ 两列的数值差 100（`ID=247 <-> Type=147`），客户端两种查法都在用：
      `VSociatyRedBag.lua:131` 用 `tT23_Function.ID`（红包领取上限 = 5），
      `VSociatyTranscript.lua:181` 用 `tT23_Function.Type`（弈局购买价 = 3000）。
      ⇒ **不要**拿 `_t23_value()` 去查 ID 语义的行，那会读到另一行的数（静默、且看起来"有个值"）。
    """
    if row_id in _T23_BY_ID_CACHE:
        return _T23_BY_ID_CACHE[row_id]
    val = fallback
    try:
        t = _load_json('T23_Function.json')
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            if _i(r[h['ID']]) == int(row_id):
                val = _i(r[h['Value']], fallback)
                break
    except Exception:
        val = fallback
    _T23_BY_ID_CACHE[row_id] = val
    return val


_REDBAG_CLAIM_MAX_FALLBACK = 5      # 表读不到时的兜底（= T23 ID=247 的实测值，不是编的）


def _redbag_claim_max():
    return max(1, _t23_by_id(247, _REDBAG_CLAIM_MAX_FALLBACK))


def _redbag_out(statecode, *body):
    """`[statecode]`（纯错误码）或 `[1, ...body]`。

    ⚠ 不带 body 时**绝不能**拼出 `[30405[]]`（多一个 `[]` 就是非法 JSON，客户端当场
      解析失败 ⇒ 面板卡死）。错误码一律走 `[%d]`，与本文件其它 handler 同形。
    """
    if not body:
        return ('[%d]' % statecode).encode('utf-8')
    return ('[%d,%s]' % (statecode,
                         json.dumps(list(body), separators=(',', ':'), ensure_ascii=False))
            ).encode('utf-8')


def _my_guild_obj():
    """我这个会的世界对象（单档/未入会 -> `None`）。"""
    try:
        gw = _gworld()
        if gw is None:
            return None
        _gid, g = gw.my_guild()
        return g or None
    except Exception:
        return None


def _push_attention_members(extra_aid=None):
    """把 8082 推给**全会在线的每个成员**（按**各自的档**算载荷）。best-effort。

    ⚠ 为什么"发红包/领红包/签到"都必须推：红点是"服务端算、客户端缓存"，而客户端
      **不清理缺席条目** ⇒ 不主动推就得等下次进游戏才刷新
      （`handlers_point.U_SUB_MESSAGE` 段有详解）。
    ⚠⚠ **逐个成员用自己的档算**（`acc_of`）：绝不能拿发起人的上下文算一份群发 ——
      那会把"我已经签到了"写进所有人的红点缓存（串档，且玩家可见）。
    """
    try:
        from handlers_point import push_attention
        from accounts import REGISTRY as _R
        g = _my_guild_obj()
        ids = []
        if g is not None:
            try:
                ids = list(_gworld().member_aids(g))
            except Exception:
                ids = []
        if extra_aid and extra_aid not in ids:
            ids.append(extra_aid)

        def _acc_of(aid):
            try:
                return _R.get_or_create(aid) if _R.exists(aid) else None
            except Exception:
                return None

        n = 0
        for aid in ids:
            n += push_attention(aid, _acc_of(aid))
        return n
    except Exception:
        return 0


def _redbag_gid_ok(req):
    """请求里的 `gangId` 与**我所在的会**是否一致（客户端把 1121 的 sociatyId 原样回传）。

    单档/空会：服务端没有会 id 可比 —— 这时**不拦**（`gw.my_guild()` 已经判过"我有没有会"）。
    """
    gw = _gworld()
    if gw is None:
        return True
    try:
        gid, _g = gw.my_guild()
    except Exception:
        return True
    want = str((req or {}).get('gangId') or '')
    return (not gid) or (not want) or (want == str(gid))


@_guild_atomic
def handle_sociaty_redbag_list(body_json=None):
    """1187 红包列表。"""
    gw = _gworld()
    if gw is None:
        # 单档：本工程单档下没有"会"这回事（1121 恒 ishavegang=0）。⚠ **不能**回 `[1,[[]]]`：
        #   1187 的兜底体正是"列表恒空"这个现象的来源，继续发它等于把 bug 留在原地。
        #   回 30405（暂无妖盟红包）：客户端对它**静默 return**，不弹任何提示。
        return _redbag_out(30405)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    if not _redbag_gid_ok(req):
        return _redbag_out(30405)
    code, remain, rows = gw.redbag_list()
    if code == 1:
        return _redbag_out(1, remain, rows)
    return _redbag_out(code if code is not None else 30405)


@_guild_atomic
def handle_sociaty_redbag_send(body_json=None):
    """1188 发送红包（扣**发包人**鬼道积分；应答与 1187 同形）。"""
    gw = _gworld()
    if gw is None:
        return _redbag_out(30405)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return _redbag_out(30404)
    if not isinstance(req, dict):
        req = {}
    red_type = _i(req.get('redType'), 0)
    # ① 只放行普通红包（理由见段头；2 类是系统代发的充值礼包）
    if red_type != 1:
        return _redbag_out(30404)
    money_num = _i(req.get('moneyNum'), 0)
    red_num = _i(req.get('redNum'), 0)
    content = str(req.get('content') or '')
    # ② 金额必须是 UN18 里**真实存在的档位**（客户端只能从 getGoldList() 里选，
    #    服务端不能信"客户端会选对的"—— 不信客户端正是 1705/1153 那两处的既有判据）
    row = None
    for r in _UN18.values():
        if _i(r.get('Type'), 0) == 1 and _i(r.get('Rank'), -1) == money_num:
            row = r
            break
    if row is None:
        return _redbag_out(30404)
    lo, hi = _i(row.get('LowerLimit'), 0), _i(row.get('UpperLimit'), 0)
    if red_num <= 0 or (lo and red_num < lo) or (hi and red_num > hi):
        return _redbag_out(30404)
    # ③ moneyType 必须是客户端硬编码的 6（鬼道积分）；别的单位本服务端没有钱包可扣
    if _i(req.get('moneyType'), 0) != 6:
        return _redbag_out(30404)
    code, remain, rows = gw.redbag_send(
        money_type=6, money_num=money_num, red_type=1, red_num=red_num, content=content,
        goods_type=_i(row.get('Content'), 0), goods_id=_i(row.get('ContentNum'), 0),
        lower_num=_i(row.get('LowerNum'), 1), upper_num=_i(row.get('UpperNUm'), 1),
        goods_num=_i(row.get('LowerNum'), 1),
        expire_hours=_i(row.get('Time'), 24))
    if code == 1:
        # UN15 1036【盟】发放红包（ConditionType=67，一周 3 次）—— 这是它**唯一**的写入点
        _week_ct_bump(CT_SOCIATY_SEND_REDBAG)
        save_state()
        # 会里多了个未领红包 -> 全会的妖盟红包红点都要亮
        _push_attention_members()
        return _redbag_out(1, remain, rows)
    return _redbag_out(code if code is not None else 30405)


@_guild_atomic
def handle_sociaty_redbag_info(body_json=None):
    """1189 领取红包。"""
    gw = _gworld()
    if gw is None:
        return _redbag_out(30405)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return _redbag_out(30405)
    if not isinstance(req, dict):
        req = {}
    if not _redbag_gid_ok(req):
        return _redbag_out(30405)
    red_id = _i(req.get('redId'), 0)
    if red_id <= 0:
        return _redbag_out(30405)
    code, info = gw.redbag_claim(red_id, claim_max=_redbag_claim_max())
    if code != 1 or not info:
        return _redbag_out(code if code is not None else 30405)
    # UN15 1037【盟】抢红包（ConditionType=68，一周 5 次）—— 它**唯一**的写入点
    _week_ct_bump(CT_SOCIATY_GET_REDBAG)
    save_state()
    # 我领过了 -> 我的红包红点该灭（其余成员的红点状态也一并刷新，幂等）
    _push_attention_members()
    return _redbag_out(1, info.get('ucode', 0), info.get('goodsId', 0),
                       info.get('goodsType', 0), info.get('num', 0), info.get('rows') or [])


# ============================================================================
# 1191 SOCIATY_SIGN 妖盟签到（新包 A0 第 3 批）
# ----------------------------------------------------------------------------
# 契约：`NetSociatySign:recvMessage` **一个槽都不读**（只判 statecode 然后 emit 事件）
#   ⇒ 应答体给 `[1,[]]` 就够；**真正决定按钮能不能按的是 8082**。
#
# ★★ 修前 1191 不在表里 ⇒ 兜底 `[1,[[]]]`（statecode=成功的空体）。看起来"没问题"，
#   实际是**按钮永远按不动**：
#     `VSociatyInformation:onTick`（每秒一次）调 `setSignRedDotState()`，它读
#     `getState(UNOIN=11, MESSAGE=3, SIGN=1)`，为假就 `signBtn:setEnabled(false)` +
#     文案改"已签到"，而**整个文件里没有第二处 `setEnabled(true)`**
#     ⇒ 服务端不推 `[11,3,1,true]`，进面板约 1 秒后按钮就变灰，玩家**点不到**。
#   ⇒ 所以这一条的实装 = ①记下"今天签过了" ②把红点状态推下去。
#
# 落点：per-account `EQ_STATE['sociaty']` 的 `sign_date`。**不能**塞 `a07_ct`：
#   那是每日账本，而 UN15 1035【盟】妖盟签到要**一周 5 次**（每天只能签一次）⇒
#   放每日账本永远到不了 5。周进度落在 `wk_ct`（见 CT_SOCIATY_SIGN 段）。
# ⚠ **重复请求（重放包）回成功但不再计进度**：签到天然幂等（一天一次），而 T41 里
#   "已签到"（`LOCAL_UI_GANGS_SIGNIN_SUCCEED`）是**客户端文案键**（codeKey），
#   没有任何证据表明官方把它当 statecode 用 ⇒ 不编一个错误码去拒绝它。
# ============================================================================
def handle_sociaty_sign(body_json=None):
    """1191 妖盟签到。"""
    sg = _sg()
    if not sg.get('name'):
        return b'[50000]'                     # 请先加入一个工会（与 1133/1815 同码）
    today = time.strftime('%Y-%m-%d')
    if str(sg.get('sign_date') or '') != today:
        sg['sign_date'] = today
        sg['sign_days'] = _i(sg.get('sign_days', 0), 0) + 1
        _week_ct_bump(CT_SOCIATY_SIGN)
        save_state()
    _push_attention_members()                  # 把"今天已签"推下去（红点灭 + 按钮保持灰）
    return b'[1,[]]'
