# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 排行榜 / 妖主入侵排行 / 妖神传说结算
#   ★2026-09-12 (v26 P1) 新增实装。此前 1049/1050/1065/1066/1106/1107/9002 全部未处理 ->
#   落 config_data.resp_for 兜底 [1,[[]]] -> 客户端 recvMessage 越界取 msg[N] -> Lua 抛错 ->
#   SocketManager 的 socket tick 中断 -> 整个界面卡死(不是空面板)。
#
#   客户端契约 (逐行取自 client Lua, 见每个 handler 头部注释):
#     1049 RANK_DEFENST      NetRankingDefense      -> callback(ownRankData, rankList, curPage, totalPages)
#     1050 RANK_QUALIFYING   NetRankingQualifying   -> callback(ownRankData, rankList)
#     1065 RANK_GETMONSTER   NetRankingMonster      -> callback(ownRankData, rankList, curPage, totalPages)
#     1066 RANK_MONSTERINFO  NetRankingMonsterInfo  -> callback(monsterinfo)  [560=未上榜]
#     1106 INVADE_DAY_RANK   NetInvadeDayRank       -> callback(ownRankData, rankList, curPage, totalPages)
#     1107 INVADE_HIS_RANK   NetInvadeHisRank       -> callback(ownRankData, rankList, curPage, totalPages)
#     9002 LENGEND_REWARD    NetLengendReward       -> roleInfo / ourFormation / dropInfo
#
#   ⚠ 服务器是**唯一数据源**: 私服没有其他在线玩家, 排行榜必须由服务器造出"看起来真实"的名次,
#     否则界面全空。名次用真实 id/静态id (铁律 #22: 客户端 `getObject(T,id):getStatic_X()`
#     拿到 0/编造 id 会 nil 索引崩), 所以下面的假名次统一从 L01/L91 真表里取。
import json, time
from state import (EQ_STATE, save_state, all_monster_ucodes, get_monster_base,
                   get_monster_skin, get_monster_lv, get_equip_append,
                   # ★2026-10-02 (D4=B): 跨账号取数用的"只看传入 dict"版本 ——
                   #   与上面那几个同口径，但**不读当前账号上下文**（绝不 bind 到别人）。
                   all_monster_ucodes_in, monster_base_in)

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

# ---------- 真实静态表取样 (名次用的怪/装备/技能 id 必须真实) ----------
_L91 = []      # L91_Monster: 怪静态id 列表 (榜单里的"别人家的怪")
_L01 = []      # L01_Equipments: 装备静态id
_L02 = {}      # L02_Items: 道具静态id -> 名称 (榜单/头像用不到, 留给后续)

def _bootstrap():
    global _L91, _L01
    if _L91:
        return
    try:
        t = _load_json('L91_Monster.json')
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                _L91.append(int(r[h['ID']]))
            except Exception:
                continue
    except Exception:
        _L91 = []
    try:
        t = _load_json('L01_Equipments.json')
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                _L01.append(int(r[h['ID']]))
            except Exception:
                continue
    except Exception:
        _L01 = []

def _pick(seq, i, default=0):
    if not seq:
        return default
    return seq[i % len(seq)]

# ---------- 机器人名次生成 (确定性: 同一 page/index 结果稳定, 便于用户复验) ----------
_RANK_NAMES = ['剑指苍穹', '青丘小狐', '醉里挑灯', '半城烟沙', '一剑霜寒', '白衣渡江',
               '明月照沟渠', '大梦谁先觉', '踏雪寻梅', '孤舟蓑笠翁', '清风徐来', '沧海一粟']
_RANK_TEAMS = ['天罡北斗', '地煞七十二', '妖王亲卫', '镇妖司', '玄天正宗', '万妖盟']
_RANK_FRAMES = [1001, 1002, 1003]

def _page_of(body_json, key='page'):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    try:
        return max(1, int((req or {}).get(key) or 1))
    except Exception:
        return 1

def _pager(cur, total):
    # 客户端 tosplit(msg[N], "/") -> {currentPage, totalPages}; 必须是 "a/b" 形态
    return '%d/%d' % (int(cur), int(total))

PAGE_SIZE = 10

def _own_head():
    # 头像 id (T35_HeadImage.ID) —— ★必须是**数字**, 单独一个字段
    head = EQ_STATE.get('head') or {}
    return int(head.get('eq', 1001) or 1001)

def _own_frame():
    # 头像框 id (T36_HeadFrame.ID) —— 单独一个字段
    head = EQ_STATE.get('head') or {}
    return int(head.get('frame_eq', 1001) or 1001)

def _own_img():
    # ⚠⚠2026-09-12 (v26 goal-round-6) 真机 SIGSEGV 根因修正 —— **不要再返回 "a|b" 字符串**:
    #   客户端 NetRankingDefense.lua:45 的注释写着 `--2.21  |分隔头像头像框`, 但**实际消费者**是
    #   `VInvadeRanking:refreshItem` -> `updateHeaderIconById(config.img, config.imgframe, ...)`,
    #   而 UIHelper.lua:25 的实现是:
    #       if not headId or headId == "" or tonumber(headId) == 0 then headId = 1001 end
    #       local headData = usGetTableData(tT35_HeadImage.tName, headId)   -- 按 ID 查表
    #       local headImage = headData.Image                                 -- headData 为 nil -> 崩
    #   —— 它要的是**两个数字**, 不是 "1001|1003" 这种字符串!
    #   传字符串时 `tonumber("1001|1003")` = nil -> `usGetTableData(表, nil)` = nil ->
    #   `headData.Image` 取 nil 的字段 -> headImage = nil -> `imageSpriteChange(ctrl, nil)`
    #   -> C++ 侧空指针 -> tombstone: TableView::_setIndexForCell 前就死了。
    #   堆栈实证: signal 11 / fault addr 0x0 / Packets::GCMessageHandler::Execute <- 1106/1082 回调。
    #   → 保留本函数只为兼容旧调用点, 一律返回数字。
    return _own_head()

def _my_saved_defense_units(n=5):
    """我方 7002 保存的防守队原始行 -> `[[staticid, level, score, ucode, skinid], ...]`。

    **没有"背包前 N 只"回退**（回退只在 `_my_formation_units` 里做）：1049/1065 需要
    区分"真的存过阵容"与"没存过"—— 两者口径不同（见 `_my_team_power` /
    `_my_monster_rank_row`）。读的是纯 JSON 行，不 bind、不写。
    """
    try:
        from handlers_pvp import _pvp_teams
        p = EQ_STATE.get('pvp') or {}
        tn = str(int(p.get('teamnum') or 1))
        teams = _pvp_teams()
        rows = (teams.get(tn) or {}).get('defense') or []
        if not rows:
            rows = (teams.get('1') or {}).get('defense') or []
        out = []
        for r in rows[:n]:
            if not isinstance(r, dict):
                continue
            try:
                out.append([int(float(r.get('staticid') or 0)),
                            int(float(r.get('level') or 0)),
                            int(float(r.get('score') or 0)),
                            str(int(float(r.get('ucode') or 0))),
                            int(float(r.get('skin') or 0))])
            except (TypeError, ValueError):
                continue
        return out
    except Exception as _e:
        print('[RANK] !! 读取已保存防守队失败: %r' % (_e,), flush=True)
        return []


def _my_formation_units(n=3):
    # 我方上阵怪摘要 -> [[staticid, level, demonvalue, ucode, skinid], ...]
    # ★★2026-10-18（真机："布阵之后排行榜上的东西没保存上去"）：
    #   修前这里取的是**背包里前 N 只怪**（`all_monster_ucodes()` 的顺序），
    #   与玩家在 妖力布阵 里保存的防守队**完全无关** —— 于是
    #     ① 1049 的"我的防守队"永远只显示 3 只（而布阵能放 5 只）；
    #     ② 换了阵容排行榜也不变；③ 与 7002 里存下来的 pvp.teams 各说各话。
    #   现在**优先读 7002 保存的防守队**，只有该队为空时才退回旧行为。
    def _from_bag():
        out = []
        for uc in all_monster_ucodes():
            if len(out) >= n:
                break
            b = get_monster_base(uc)
            if not b:
                continue
            out.append([int(b[0]), int(get_monster_lv(uc)), int(b[2] or 0),
                        str(uc), int(get_monster_skin(uc) or 0)])
        return out

    saved = _my_saved_defense_units(n)
    return saved if saved else _from_bag()


def _my_team_power():
    """我方 1049「妖力值」：**只有真的保存过防守队**才返回 5 只 `score` 之和。

    返回 `None` = 没存过阵容 -> 调用方回退 `qualify.points*10`（与 `_acc_team_power`
    对别人的回退**同一口径**）。⚠ 不能用 `_my_formation_units()` 的和来判断：那个函数
    在没存阵容时会退回"背包前 N 只"，恒有值 ⇒ 会把 1049 的分值从积分偷偷换成背包妖力
    （`_world_rank_test.py` W4 当场判红）。
    """
    try:
        s = sum(int(u[2]) for u in _my_saved_defense_units(5))
        return s if s > 0 else None
    except Exception:
        return None


def _my_monster_rank_row():
    """1065 "我"那一行的怪：`(score, lv, ucode, staticid, skin)`。

    ★★2026-10-18（审计 F3：我方与别人**两套口径**）：
      · 有 7002 保存的防守队 -> **5 只里 `score` 最高的一只**（与 `_world_monster_best()`
        对别人的算法逐字相同；修前取的是**队首**，队首不是最高分时我方分数会偏低）；
      · 没保存过 -> 按等级取背包最强一只，`score = lv*100`（**派生值**，与别人没存阵容
        时的回退同一口径；修前我方发的是 `monsters[uc][2]`，那是道具行的 `num`）。
    """
    saved = _my_saved_defense_units(5)
    if saved:
        best = max(saved, key=lambda u: (int(u[2]), int(u[1])))
        return (int(best[2]), int(best[1]), str(best[3]), int(best[0]),
                int(best[4]) or 11301)
    best = None
    for uc in all_monster_ucodes():
        b = get_monster_base(uc)
        if not b:
            continue
        lv = int(get_monster_lv(uc))
        if best is None or lv > best[0]:
            best = (lv, str(uc), b)
    if best is None:
        return 0, 0, '0', int(_pick(_L91, 0, 100031)), 11301
    lv, uc, b = best
    return lv * 100, lv, uc, int(b[0]), int(get_monster_skin(uc) or 0) or 11301


def _bot_units(seed, n=3):
    # 与 _invade_rows 同理: 机器人队伍的怪一律"借用"玩家自己背包里的 (mid, skin),
    # 保证客户端 `SDMonster:create` / `WarSoldier:create` 一定建得出来 (真机加固, 见 _safe_monster_pairs)
    pairs = _safe_monster_pairs(n)
    out = []
    for k in range(n):
        _mid, _skin = pairs[k % len(pairs)]
        out.append([_mid,
                    int(10 + (seed * 3 + k * 5) % 60),
                    int(50 + (seed * 11 + k * 7) % 300),
                    'bot%d_%d' % (seed, k),
                    _skin])
    return out

def _bot_row(rank, kind, seed, score):
    # kind: 'defense' | 'qualify' | 'monster'
    name = _RANK_NAMES[(seed + rank) % len(_RANK_NAMES)]
    img = 1001                    # ★数字; 不能给 '1001|1003' (见 _own_img 的 SIGSEGV 注释)
    if kind == 'defense':
        return [rank, img, name, 10 + (seed + rank) % 60,
                _RANK_TEAMS[(seed + rank) % len(_RANK_TEAMS)],
                _bot_units(seed + rank),
                int(score), 1 + (rank % 3),
                0, int(rank * 3 % 40)]                    # isSupport, supporNumt
    if kind == 'qualify':
        return [rank, 1001 + (seed + rank) % 9,               # dangrading
                _RANK_TEAMS[(seed + rank) % len(_RANK_TEAMS)],
                10 + (seed + rank) % 60, name,
                int(score), 5 + (seed + rank) % 40,
                1 + (seed + rank) % 20,
                '%.2f' % (0.3 + ((seed + rank) % 60) / 100.0),
                int(score * 2)]
    # monster (1065) —— 同样借用玩家自己背包里的怪 (真机加固: 见 _safe_monster_pairs)
    _mid, _skin = _safe_monster_pairs(1)[0]
    return [rank, img, name, 'bot%d_%d' % (seed, rank),
            _mid, int(score), 3 + (seed + rank) % 20, 1 + (seed + rank) % 9,
            0, int(rank * 2 % 30), _skin]

# ==================== ★★2026-10-02 (D4=B) 真·跨账号取数 ====================
# 修前: 1049/1050/1065/1106/1107 的"他人行"**全是静态表伪造的 bot**
#   (`_bot_row` / `_invade_rows`, 名字取自 `_RANK_NAMES`、分数是 `20000 - i*137` 这种等差数列)
#   ⇒ 一行都不读 `EQ_STATE`, 多人世界里**看不到彼此** —— 这正是 D4 要消灭的现象。
#
# **两条设计约束**(都不许绕):
#   ① **必须 gate 在 `ENABLED` 上**: 78 套回归网全部建立在"单档"假设上, 若默认走跨账号路径,
#      它们会整批变红(而且变红的形态是"榜变空了", 不是崩溃)。判据必须是
#      `ENABLED and REGISTRY is not None` —— **不能**只判"结果是不是空列表":
#      多账号下"世界上只有我一个"是**合法**结果(空列表), 与"根本没启用"是两件事。
#   ② **只读 `acc.data` 的标量, 绝不 bind 到别人**: `state.save_state()` 是按
#      **当前上下文账号**路由的(`state.py:1453`), 一旦在别人的上下文里触发落盘就会写**别人的档**,
#      而 `_b7` 的 `"A 操作后 B 的档逐字节没变"` 判据正卡在这里。
#      ⇒ 阵容/怪物这类"要经 state 取数函数才能读到"的量，**不许 bind 去读**。
#        ⚠ 2026-10-18 更正：上一版据此对别人一律发空 `[]`，真机表现是**榜上每人 5 个"无上阵"**
#        （用户报"布阵之后排行榜没保存上去"）。正确做法是**直接读 `acc.data` 里的原始行**
#        （`pvp.teams[tn].defense` 是纯 JSON，不需要任何 state 取数函数、也不需要 bind）——
#        见 `_acc_defense_units`。空表只留给"该队真的没存过阵容"。
# 1065/1066(妖神榜·按怪)与妖盟/评论**本轮未做**, 见 HANDOFF 的"没做完的事"。
def _world_others():
    """多账号模式下返回**其它账号的档**; 单档/未启用 -> None(调用方落回现有 bot 路径)。"""
    try:
        from accounts import ENABLED, REGISTRY, EQ_STATE as _AS
    except Exception:
        return None
    if not (ENABLED and REGISTRY is not None):
        return None
    try:
        mine = getattr(_AS.current(), 'id', None)
    except Exception:
        mine = None
    try:
        accs = REGISTRY.all_accounts_full()
    except Exception:
        return None
    return [a for a in accs if mine is None or a.id != mine]


def _acc_val(acc, path, default=0, cast=int):
    """只读地从账号档里取一个**标量**(绝不 bind, 绝不 setdefault)。"""
    try:
        d = acc.data if isinstance(acc.data, dict) else {}
        for k in path[:-1]:
            d = d.get(k) if isinstance(d, dict) else None
            if not isinstance(d, dict):
                return default
        v = d.get(path[-1])
        return cast(v) if v is not None else default
    except Exception:
        return default


def _acc_name(acc):
    """显示名。⚠ `Account.name` 在没改过名时**等于账号 id**('u0002' 这种),
    不能直接当榜上名字; `role_name` 也不在 `_default_state()` 里(只有 1002 建角/2001 改名会写)
    ⇒ 缺省回 `未命名#<id>`: **唯一且可判别**, 绝不回 `'测试'`(否则全榜同名, 掩盖"跨账号没生效")。"""
    n = _acc_val(acc, ('role_name',), '', str)
    return n or ('未命名#' + acc.id)


def _acc_lv(acc):
    return _acc_val(acc, ('player_lv',), 1)


def _acc_guild(acc):
    return _acc_val(acc, ('sociaty', 'name'), '', str)


def _acc_head(acc):
    h = _acc_val(acc, ('head', 'eq'), 1001)
    return h if h else 1001        # ⚠ 非 0 且不在 T35 里的头像 id 会让客户端**崩**(UIHelper 无 nil 守卫)


def _acc_frame(acc):
    f = _acc_val(acc, ('head', 'frame_eq'), 1001)
    return f if f else 1001


def _acc_defense_units(acc, n=5):
    """**只读**别人档里的防守队（7002 存下来的原始行）-> 1049 榜单行的 5 槽。

    ★★2026-10-18（真机："布阵之后排行榜上的东西没有实际保存上去"）：修前这里对**别人**
      一律发空表 `[]` ⇒ 榜上每人都是 5 个"无上阵"（真机 NO.1 那行就是全空）。
      注释原写"阵容要经 state 取数函数才能读到，所以返回空表" —— 不成立：
      `acc.data['pvp']['teams'][tn]['defense']` 是**纯 JSON 原始行**，直接读即可，
      完全不需要 bind（`_acc_val` 的同一套只读纪律）。空表只保留给"该队真的没存过"。
    """
    try:
        d = acc.data if isinstance(acc.data, dict) else {}
        pvp = d.get('pvp') or {}
        tn = str(int(pvp.get('teamnum') or 1))
        teams = pvp.get('teams') or {}
        rows = (teams.get(tn) or {}).get('defense') or (teams.get('1') or {}).get('defense') or []
        out = []
        for r in rows[:n]:
            if not isinstance(r, dict):
                continue
            out.append([int(float(r.get('staticid') or 0)),
                        int(float(r.get('level') or 0)),
                        int(float(r.get('score') or 0)),
                        str(int(float(r.get('ucode') or 0))),
                        int(float(r.get('skin') or 0))])
        return out
    except Exception:
        return []


def _acc_team_power(acc):
    """该账号"妖力布阵"防守队的**妖力总和**（= 5 只 `score` 之和）。

    ★★2026-10-18（1049 复验）：`msg[7]` 在客户端就是"妖力值"这一栏
      （`NetRankingDefense.lua:60/84` -> `MRankingDefenseItem:setDemonValue`）。
      我方那行上一轮已改成"保存阵容 5 只的 `score` 之和"，但**别人那行**仍发
      `qualify.points*10`（排位积分，不是妖力）⇒ 同一个榜单两套口径，
      排序与显示都对不上。这里统一成"防守队妖力之和"；该账号没存阵容时
      退回 `points*10`（**不编**，与 `handle_ranking_defense` 的我方回退同一口径）。
    """
    s = sum(int(u[2]) for u in _acc_defense_units(acc, 5))
    return s if s > 0 else _acc_val(acc, ('qualify', 'points'), 0) * 10


def _acc_teamnum(acc):
    """该账号当前保存的妖力队伍号（1049 的 `msg[8]` / 榜单行第 9 槽）。"""
    try:
        d = acc.data if isinstance(acc.data, dict) else {}
        return int((d.get('pvp') or {}).get('teamnum') or 1)
    except Exception:
        return 1


def _world_rows(kind, page=1, my_score=None):
    """按 kind 生成**真·跨账号**榜单行(含分页/排序/名次)。

    返回 None = "没启用多账号" -> 调用方原样走旧的 bot 路径(单档行为逐字节不变)。
    返回 []   = "启用了, 但世界上只有我一个" -> 榜为空, 这是**合法**结果。

    ★2026-10-18（排行榜复验）：`my_score` 传进来时，榜行的名次会把**我**算进去
      （竞争排名：`1 + 比我高的人数`）。修前只把"别人"按 1..N 排名 ⇒ 真机上
      "我的排名 NO.1+" 与"榜首 NO.1" 同时出现、同一个榜两个 NO.1。
      不传 `my_score` 时保持旧行为（1..N），只给不展示"我的名次"的调用方用。
    """
    others = _world_others()
    if others is None:
        return None
    sfn = {
        # ★2026-10-18：1049 的分值必须是"妖力值"，不是排位积分 —— 与 `msg[7]` 同口径。
        'defense': lambda a: _acc_team_power(a),
        'qualify': lambda a: _acc_val(a, ('qualify', 'points'), 0),
        'invade_day': lambda a: _acc_val(a, ('invade', 'damage'), 0),
        'invade_his': lambda a: _acc_val(a, ('invade', 'total'), 0),
    }[kind]
    scored = sorted((((sfn(a)), a) for a in others), key=lambda t: -t[0])
    _all_scores = [int(sc) for (sc, _a) in scored]
    if my_score is not None:
        _all_scores.append(int(my_score))

    def _rk(sc):
        return 1 + sum(1 for x in _all_scores if x > int(sc))

    lo = (page - 1) * PAGE_SIZE
    rows = []
    for idx, (score, a) in enumerate(scored[lo:lo + PAGE_SIZE]):
        rank = _rk(score) if my_score is not None else (1 + lo + idx)
        if kind == 'defense':
            rows.append([rank, _acc_head(a), _acc_name(a), _acc_lv(a), _acc_guild(a),
                         _acc_defense_units(a), int(score), _acc_teamnum(a), 0, 0])
        elif kind == 'qualify':
            rows.append([rank, 1001, _acc_guild(a), _acc_lv(a), _acc_name(a),
                         int(score), 0, _acc_val(a, ('qualify', 'week_win'), 0),
                         '0.00', int(score) * 2])
        else:                                   # 1106 / 1107 (同形)
            rows.append([rank, _acc_lv(a), _acc_name(a), _acc_head(a), _acc_frame(a),
                         str(int(score)), [], 'role_%s' % a.id])
    return rows


def _world_my_rank(kind, my_score):
    """我在世界里的**真实**名次(修前是写死的 '812.' / '1268+' / 1000+ / -1)。

    返回**整数**名次; 未启用多账号 -> None。
    ⚠ 类型必须由调用方按各自协议的既有形状格式化: 1049/1050 的 `msg[N]` 是**字符串**
      (客户端会对它 `string.find(...,"+")`), 而 1106/1107 的 `msg[2]` 原本就是**整数**
      `-1` —— 这里若统一回字符串就把 1106/1107 的字段类型改掉了。
    """
    others = _world_others()
    if others is None:
        return None
    if kind == 'defense':
        # 1049：排序键 = 防守队妖力（与 `_world_rows('defense')` / `msg[7]` 同口径）
        better = sum(1 for a in others if _acc_team_power(a) > my_score)
        return better + 1
    key = {'qualify': ('qualify', 'points'),
           'invade_day': ('invade', 'damage'),
           'invade_his': ('invade', 'total')}[kind]
    better = sum(1 for a in others if _acc_val(a, key, 0) > my_score)
    return better + 1


def _rank_str(r):
    """名次的**字符串**形态(1049/1050 用): 1000 名以内给 '<n>.'，否则 '1000+'。"""
    return '%d.' % r if r <= 1000 else '1000+'


def _own_name():
    """我方行显示名。改过名的玩家用真名; 没改过的仍是 '测试'(与 `state.role_info_array()` 同口径,
    **单档模式下必须逐字节保持原样** —— 否则 78 套回归网的榜断言会红)。"""
    try:
        return str(EQ_STATE.get('role_name') or '测试')
    except Exception:
        return '测试'


def _own_guild():
    """我方行的队伍/妖盟名。没入会时必须仍是 '我的队伍'(同样是单档逐字节不变的要求)。"""
    try:
        return str((EQ_STATE.get('sociaty') or {}).get('name') or '我的队伍')
    except Exception:
        return '我的队伍'


# ============ ★★2026-10-02 (D4=B 收尾) 1065/1066 妖神榜·按怪 真·跨账号 ============
# 修前 1065 的他人行是 `_bot_row('monster')` 造的，1066 只能看**自己**的怪。
#
# **为什么 1065 与 1066 必须一起做**：只做 1065 会变成"榜单显示别人的怪、点进去却是自己的"
#   —— 比留 bot 行更糟（用户会以为数据错了）。所以本轮把两个一起改。
#
# **两条形状问题**（与评论那套同源，序号算法共用 `accounts.owner_ns`）：
#   ① 各账号的 `ucode` 都从 1 起各自计数 ⇒ 显示 id 必须命名空间化
#      （`owner_ns*100000 + uc`），否则点别人的行会打开自己的怪。
#   ② 跨账号**绝不能 `bind()`**：`save_state()` 按当前上下文路由，会写**别人的档**。
#      ⇒ 1066 的取数改成走一个**只读视图**（`_MonView`）：当前账号读 EQ_STATE，
#        别的账号读它的 `acc.data`，**两种来源同一套读法**（抄一份必然漂移）。
def _acc_int(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _ns_ucode(aid, uc):
    """怪物 ucode -> **跨账号展示 id**（序号为 0 时原样）。序号算法只有一份：`accounts.owner_ns`。"""
    try:
        from accounts import ns_id
        return ns_id(aid, int(uc))
    except Exception:
        return _acc_int(uc)


def _split_ns_ucode(v):
    """展示 id -> `(owner_ns, 原 ucode)`；没有命名空间的 -> `(0, v)`。"""
    try:
        from accounts import split_ns_id
        return split_ns_id(v)
    except Exception:
        return 0, _acc_int(v)


def _my_ns():
    try:
        from accounts import ENABLED, REGISTRY, EQ_STATE as _AS, owner_ns as _on
        if not (ENABLED and REGISTRY is not None):
            return 0
        return _on(getattr(_AS.current(), 'id', None))
    except Exception:
        return 0


def _account_by_ns(ns):
    """按命名空间序号找**别的**账号（自己那个返回 None —— 调用方用当前上下文）。"""
    try:
        from accounts import ENABLED, REGISTRY, EQ_STATE as _AS, owner_ns as _on
    except Exception:
        return None
    if not (ENABLED and REGISTRY is not None) or not ns:
        return None
    try:
        me = getattr(_AS.current(), 'id', None)
        for acc in REGISTRY.all_accounts_full():
            if acc.id != me and _on(acc.id) == ns:
                return acc
    except Exception:
        return None
    return None


class _MonView(object):
    """1066 的取数视图：当前账号（`data=None` -> EQ_STATE）或别的账号（只读它的档 dict）。"""

    __slots__ = ('_d',)

    def __init__(self, data=None):
        self._d = data if isinstance(data, dict) else None

    def get(self, key, default=None):
        if self._d is None:
            return EQ_STATE.get(key, default)
        v = self._d.get(key)
        return default if v is None else v

    def base(self, uc):
        return get_monster_base(str(uc)) if self._d is None else monster_base_in(self._d, uc)

    def ucodes(self):
        return all_monster_ucodes() if self._d is None else all_monster_ucodes_in(self._d)

    def lv(self, uc):
        return _acc_int((self.get('monster_lv') or {}).get(str(uc), 1), 1)

    def skin(self, uc):
        b = self.base(uc)
        if b is None:
            return 0
        eq = (self.get('monster_skin_eq') or {}).get(str(uc))
        try:
            return int(eq) if eq else int(b[1])
        except Exception:
            return 0

    def add(self, uc):
        a = (self.get('monster_add') or {}).get(str(uc))
        try:
            if isinstance(a, (list, tuple)) and len(a) >= 4:
                return [int(x) for x in a[:4]]
        except Exception:
            pass
        return [0, 0, 0, 0]


def _world_monster_best():
    """多账号下**每个其它账号的最强一只怪** -> `[(score, lv, ucode, mid, skin, acc)]` 降序。
    未启用多账号 -> None（调用方走 bot 路径）。

    ★2026-10-18（1065 复验）：榜行的 `totalScore`（msg[6]）在客户端叫"妖力值"
      （`MRankingMonsterItem:setDemonValue`）。**唯一有表/客户端算出来的真值**是
      玩家 7002 保存过的防守行里的 `score`（客户端 `SQFormula:getFightScore` 的结果，
      1049 的 `demonvalue` 同源）—— 服务端**没有**独立复算公式。
      ⇒ 有保存阵容的账号取"5 只里 score 最高的那只"；没保存过的才退回旧的
      `lv*100` 派生值（**在注释里标明是派生**，不假装是妖力）。
      `monsters[uc][2]` 是道具行里的 `num`（种子值恒 100），**不是**这个榜要显示的妖力。
      `totalBattle` / `mvp` 服务端**没有数据源 ⇒ 一律 0**（铁律：不许编造）。
    """
    others = _world_others()
    if others is None:
        return None
    scored = []
    for acc in others:
        units = _acc_defense_units(acc, 5)
        if units:
            best = max(units, key=lambda u: (int(u[2]), int(u[1])))
            scored.append((int(best[2]), int(best[1]), str(best[3]),
                           int(best[0]), int(best[4]), acc))
            continue
        # 没保存过防守队 -> 退回旧行为（按等级取最强；score = lv*100 是派生值）
        d = acc.data if isinstance(acc.data, dict) else {}
        best = None
        for uc in all_monster_ucodes_in(d):
            b = monster_base_in(d, uc)
            if not b:
                continue
            lv = _acc_int((d.get('monster_lv') or {}).get(str(uc), 1), 1)
            if best is None or lv > best[0]:
                best = (lv, str(uc), b)
        if best is None:
            continue
        scored.append((best[0] * 100, best[0], best[1],
                       int(best[2][0]), _acc_int(best[2][1]), acc))
    scored.sort(key=lambda t: (-t[0], -t[1], t[5].id))
    return scored


def _world_monster_rows(page=1, my_score=None):
    scored = _world_monster_best()
    if scored is None:
        return None
    _all_scores = [int(t[0]) for t in scored]
    if my_score is not None:
        _all_scores.append(int(my_score))
    lo = (page - 1) * PAGE_SIZE
    rows = []
    for i, (score, lv, uc, mid, skin, acc) in enumerate(scored[lo:lo + PAGE_SIZE]):
        rank = (1 + sum(1 for x in _all_scores if x > int(score))) if my_score is not None else (1 + lo + i)
        rows.append([rank, _acc_head(acc), _acc_name(acc), _ns_ucode(acc.id, uc),
                     int(mid), int(score), 0, 0, 0, 0, int(skin)])
    return rows


def _world_monster_my_rank(my_score):
    """1065 的"我的名次"：把我和所有其它账号的最强怪放在一起做竞争排名。
    未启用多账号 -> None（调用方保留旧文案 `1000+`）。"""
    scored = _world_monster_best()
    if scored is None:
        return None
    return 1 + sum(1 for t in scored if int(t[0]) > int(my_score))



# ============================ 1049 妖域镇守排行 ============================
def handle_ranking_defense(body_json):
    # NetRankingDefense.recvMessage 逐字段:
    #   msg[1]=我的名次(字符串; ""->"1000.")  msg[2]=img  msg[3]=rolename  msg[4]=level
    #   msg[5]=teamName(""->"----")          msg[6]=我的防守队 [[staticid,level,demonvalue,ucode,skinid]]
    #   msg[7]=score  msg[8]=teamnum  msg[9]=榜单行列表  msg[10]="cur/total"
    #   ⚠ 客户端 L40 `ownRankData["rank"]=(msg[1]=="" and "1000." or msg[1])` 后立刻
    #     `string.find(...,"+")` -> msg[1] 必须是**字符串**(给数字会报 "bad argument #1 to 'find'")
    page = _page_of(body_json)
    seed = int(time.strftime('%j')) % 97
    total = 1          # ★只报 1 页: 客户端是顺序翻页语义, 报 5 页会连发 5 个请求
    _p = EQ_STATE.get('pvp') or {}
    _myteam = _my_formation_units(5)
    # ★2026-10-18：`msg[7]`(score) 在客户端就是"**妖力值**"这一栏（真机截图显示 0，因为
    #   修前这里是排位积分 points*10）。**真的保存过防守队**时改用 5 只的妖力之和
    #   （与布阵页"队伍妖力值"同一口径），否则退回原值 —— 与 `_acc_team_power` 同口径。
    _team_score = _my_team_power()
    my_score = int(_team_score) if _team_score else int(
        EQ_STATE.get('qualify', {}).get('points', 0) or 0) * 10
    rows = _world_rows('defense', page, my_score)
    _r = _world_my_rank('defense', my_score)
    my_rank = _rank_str(_r) if _r is not None else None
    if rows is None:            # 单档/未启用 -> 原样走 bot 路径(行为不变)
        rows = [_bot_row(1 + (page - 1) * PAGE_SIZE + i, 'defense', seed,
                         20000 - (page - 1) * PAGE_SIZE - i * 137) for i in range(PAGE_SIZE)]
    if my_rank is None:
        my_rank = '1000+' if int(EQ_STATE.get('player_lv', 1)) < 3 else '812.'
    data = [my_rank, _own_img(), _own_name(), int(EQ_STATE.get('player_lv', 1)),
            _own_guild(), _myteam, int(my_score), int(_p.get('teamnum') or 1),
            rows, _pager(page, total)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============================ 1050 妖神排位排行 ============================
def handle_ranking_qualifying(body_json=None):
    # NetRankingQualifying.recvMessage: msg[1]=rank(字符串) msg[2]=dangrading msg[3]=img msg[4]=level
    #   msg[5]=rolename msg[6]=总积分 msg[7]=总参与 msg[8]=总冠军 msg[9]=胜率 msg[10]=totalScore
    #   msg[11]=榜单行列表 (+ 之后无分页字段, 回调只有 2 参)
    seed = int(time.strftime('%j')) % 97
    q = EQ_STATE.get('qualify') or {}
    my_score = int(q.get('points', 0) or 0)
    rows = _world_rows('qualify', my_score=my_score)
    _r = _world_my_rank('qualify', my_score)
    my_rank = _rank_str(_r) if _r is not None else None
    if rows is None:
        rows = [_bot_row(1 + i, 'qualify', seed, 15000 - i * 211) for i in range(PAGE_SIZE)]
    if my_rank is None:
        my_rank = '1268+'
    data = [my_rank, 1001, _own_img(), int(EQ_STATE.get('player_lv', 1)), _own_name(),
            my_score, int(q.get('daily_n', 0) or 0) + 1,
            int(q.get('week_win', 0) or 0), '0.50',
            my_score * 2, rows]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============================ 1065 妖神榜 (按怪) ============================
def handle_ranking_monster(body_json):
    # NetRankingMonster.recvMessage: msg[1]rank msg[2]img msg[3]rolename msg[4]ucode msg[5]staticid
    #   msg[6]totalScore msg[7]totalBattle msg[8]mvp msg[9]榜单行 msg[10]"cur/total" msg[11]skinid
    page = _page_of(body_json)
    seed = int(time.strftime('%j')) % 97
    total = 1          # ★只报 1 页: 客户端是顺序翻页语义, 报 5 页会连发 5 个请求
    # ★★2026-10-18（审计 F3）：妖神榜的"我"这一行，`msg[6]` 是**妖力值**
    #   （客户端 `MRankingMonsterItem:setDemonValue(_config.totalScore)`）。
    #   口径与"别人"那行**逐字相同**：有 7002 保存阵容 -> 5 只里 `score` 最高的一只；
    #   没保存过 -> 背包最强一只，`score = lv*100`（派生值）。
    #   ⚠ 不要改用 `monsters[uc][2]`：那是道具行的 `num`（种子值恒 100），不是妖力。
    _my_score, _m_lv, my_uc, my_mid, _my_skin = _my_monster_rank_row()
    rows = _world_monster_rows(page, my_score=_my_score)
    _mr = _world_monster_my_rank(_my_score)
    if rows is None:                        # 单档/未启用 -> 原样走 bot 路径
        rows = [_bot_row(1 + (page - 1) * PAGE_SIZE + i, 'monster', seed,
                         9000 - (page - 1) * PAGE_SIZE - i * 97) for i in range(PAGE_SIZE)]
        _mr = None
    my_rank = _rank_str(_mr) if _mr is not None else '1000+'
    # ★2026-10-02 (D4=B): 我方 ucode 在多账号下**也加命名空间** —— 否则"我那一行"在别人屏幕上
    #   会和别人的行撞号，而且点自己那行时 1066 解析不出来（同一行两个 id 就永远对不上）。
    my_ns = _my_ns()
    if my_ns:
        my_uc = _ns_ucode('u%04d' % my_ns, my_uc)
    data = [my_rank, _own_img(), _own_name(), my_uc, my_mid, _my_score, 0, 0, rows, _pager(page, total),
            _my_skin]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============================ 1066 妖神详情 ============================
def handle_ranking_monster_info(body_json):
    # NetRankingMonsterInfo.recvMessage (req {ucode, teamid}):
    #   msg[1]rank(字符串, ""->"1000+") msg[2]score msg[3]staticId msg[4]level msg[5]skin
    #   msg[6]皮肤列表[[id,state]]  msg[7]equipscore msg[8]装备列表[[id,level]]
    #   msg[9]skillscore msg[10]技能列表[[id,level,state]]
    #   msg[11]featurescore msg[12]特性列表  msg[13]attrscore
    #   msg[14..21] = hp/addhp/atk/addatk/def/adddef/cri/addcri
    #   msg[22] = 附加列表 [[id,value]]
    #   ⚠ 客户端第 43/53/65/95 行是 `for i=1,#xxx`(无 or {}) -> 6/8/10/12/22 位必须给数组
    #   ⚠ 560 = "未上排行榜暂无数据"
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    ucode = str((req or {}).get('ucode') or '')
    # ★2026-10-02 (D4=B): 展示 id 可能带**别人的**命名空间 ⇒ 先解析出 owner，再决定从哪读。
    #   ⚠ 跨账号**只读那个账号的 `acc.data`**（`_MonView(data)`），**绝不 bind** ——
    #     `save_state()` 按当前上下文路由，在别人上下文里落盘就会写**别人的档**。
    _ns, _raw = _split_ns_ucode(ucode)
    _view = None
    if _ns:
        if _ns != _my_ns():
            _acc = _account_by_ns(_ns)
            if _acc is None:
                return b'[560]'
            _view = _MonView(_acc.data)
        ucode = str(_raw)
    V = _view if _view is not None else _MonView()
    base = V.base(ucode) if ucode else None
    rows = V.ucodes()
    if base is None and rows:
        ucode = str(rows[0])
        base = V.base(ucode)
    if base is None:
        return b'[560]'
    _bootstrap()
    mid = int(base[0])
    lv = int(V.lv(ucode))
    skin = int(V.skin(ucode) or 0)
    # 皮肤列表: 已解锁 + 一条未解锁 (客户端 skin.state 0/1)
    opens = (V.get('monster_skin_open') or {}).get(ucode) or []
    skinlist = [[skin, 1]]
    for sid in opens[:5]:
        try:
            if int(sid) != skin:
                skinlist.append([int(sid), 1])
        except Exception:
            continue
    if len(skinlist) < 2:
        skinlist.append([skin + 1, 0])
    # 装备列表: 用该怪真实穿戴的装备 (monster_ueq)
    equips = []
    ueq = (V.get('monster_ueq') or {}).get(ucode) or ()
    for eu in ueq:
        try:
            eu = str(eu)
            _eid = V.get('equip_id') or {}
            _elv = V.get('lv') or {}
            if eu and eu != '0' and eu in _eid:
                equips.append([int(_eid[eu]), int(_elv.get(eu, 1))])
        except Exception:
            continue
    if not equips:
        equips = [[int(_pick(_L01, 0, 1001)), 1]]
    # 技能列表 [[id,level,state]]
    skills = []
    try:
        ms = (V.get('monster_skills') or {}).get(ucode) or base[3]
        for su in ms:
            su = str(su)
            if su in ('0', '', 'None'):
                continue
            sk = (V.get('skills') or {}).get(su)
            if sk:
                skills.append([int(sk[0]), int(sk[1]), int(sk[2] or 1)])
            elif su.isdigit():
                skills.append([int(su), 1, 1])
    except Exception:
        skills = []
    if not skills:
        skills = [[int(_pick(_L91, 0, 10001)), 1, 1]]
    feats = []
    try:
        for f in (V.get('monster_feat') or {}).get(ucode) or []:
            if f:
                feats.append(f)
    except Exception:
        feats = []
    add = []
    try:
        a = V.add(ucode)
        add = [[1001, int(a[0])], [1002, int(a[1])], [1003, int(a[2])], [1004, int(a[3])]]
    except Exception:
        add = []
    data = ['826.', 0, mid, lv, skin,
            skinlist, 0, equips,
            0, skills,
            0, feats,
            0,
            0, 0, 0, 0, 0, 0, 0, 0,
            add]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============================ 1106 / 1107 妖主入侵排行 ============================
def _safe_monster_pairs(n=3):
    """返回 n 组 (staticid, skinid) —— **只用客户端已经证实在渲染的怪**。

    ★★2026-09-12 (v26 goal-round-6) 真机事故根因 (tombstone 实证):
       `VInvadeRanking:refreshItem` 会对 monsterlist 的每一项做
         `SDMonster:create(config.monsterlist[i].staticid)` -> `getStatic_Head82x81()` -> `imageSpriteChange`
       一旦 staticid 在客户端静态表里**建不出对象**, 后面的 icon 就是 nil,
       而 cell 构造失败会让 `TableView::updateCellAtIndex` 拿到 **NULL TableViewCell***,
       最终在 `TableView::_setIndexForCell` 空指针崩溃 (SIGSEGV / fault addr 0x0)。
       这是 **C++ 层崩溃**, 不会有任何 Lua 错误 —— 所以 logcat 里查不到, 必须看 tombstone。

       之前从 L91_Monster 随机抽 id, 抽到"建不出来"的行就必崩。
       这里改成只用**玩家自己怪物袋里的 mid** (`config_data.MONSTER_BAG_JSON` /
       `monsters`) —— 这些 mid 客户端正在怪袋/主界面渲染, 一定建得出来。
       再兜底用三个"全世界都有"的初始怪 mid。
    """
    out = []
    try:
        from state import all_monster_ucodes, get_monster_skin
        for uc in all_monster_ucodes():
            if len(out) >= n:
                break
            b = get_monster_base(uc)
            if not b:
                continue
            out.append((int(b[0]), int(get_monster_skin(uc) or b[1] or 0)))
    except Exception:
        out = []
    # 兜底: 初始三只 (config_data.MONSTER_BAG_JSON 里的 100031 / 200101 / 201001 —— 客户端必然有)
    for _fb in ((100031, 11301), (200101, 20011), (201001, 20101)):
        if len(out) >= n:
            break
        out.append(_fb)
    # 去重 (同一 mid 重复无意义)
    seen, uniq = set(), []
    for (m, s) in out:
        if m in seen:
            continue
        seen.add(m)
        uniq.append((m, s))
    while len(uniq) < n:
        uniq.append((100031, 11301))
    return uniq[:n]


def _invade_rows(page, seed, monster_cnt=3):
    pairs = _safe_monster_pairs(monster_cnt)
    rows = []
    for i in range(PAGE_SIZE):
        rank = 1 + (page - 1) * PAGE_SIZE + i
        name = _RANK_NAMES[(seed + rank) % len(_RANK_NAMES)]
        img = 1001                    # ★数字; 不能给 '1001|1003' (见 _own_img 的 SIGSEGV 注释)
        mlist = []
        for k in range(monster_cnt):
            _mid, _skin = pairs[k % len(pairs)]
            mlist.append([_mid,
                          100000 + (seed + rank) * 137 + k * 31,
                          'bot%d_%d_%d' % (seed, rank, k)])
        rows.append([rank, 10 + (seed + rank) % 60, name, img,
                     1001 + (seed + rank) % 8,
                     str(9000000 - (page - 1) * PAGE_SIZE * 10000 - i * 10000),
                     mlist, 'botrole%d' % rank])
    return rows

def handle_invade_day_rank(body_json):
    # NetInvadeDayRank.recvMessage: msg[1]今日伤害 msg[2]今日排行 msg[3]榜单
    #   msg[8]="cur/total"  (msg[4..7] 客户端不读)
    #   榜单行 [rank, level, rolename, img, imgframe, hurt, monsterlist[[staticid,score,ucode]], roleid]
    page = _page_of(body_json)
    seed = int(time.strftime('%j')) % 97
    inv = EQ_STATE.get('invade') or {}
    # ★2026-10-02 (D4=B) P3 键名修正: 修前读的是 `invade['daydamage']` 与
    #   `EQ_STATE['last_invade_damage']` —— **全仓零写入点**(实测 grep 只命中这两行),
    #   ⇒ "今日伤害"恒 0。真键是 `invade['damage']`(写入 handlers_activity.py:951,
    #   清零 :694) 与 `invade['total']`(累计 :952)。
    my_day = int(inv.get('damage', 0) or 0)
    total = 1          # ★只报 1 页: 客户端是顺序翻页语义, 报 5 页会连发 5 个请求
    rows = _world_rows('invade_day', page, my_score=my_day)
    my_rank = _world_my_rank('invade_day', my_day)
    if rows is None:
        rows = _invade_rows(page, seed)
        my_rank = -1
    if my_rank is None:
        my_rank = -1
    data = [my_day, my_rank, rows, 1, 1, 1, 1, _pager(page, total)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

def handle_invade_his_rank(body_json):
    # NetInvadeHisRank.recvMessage: msg[1]累计伤害 msg[2]排行 msg[3]榜单 msg[8]="cur/total"
    page = _page_of(body_json)
    seed = (int(time.strftime('%j')) + 7) % 97
    inv = EQ_STATE.get('invade') or {}
    my_total = int(inv.get('total', 0) or 0)          # ★同上: 真键是 invade['total']
    total = 1          # ★只报 1 页: 客户端是顺序翻页语义, 报 5 页会连发 5 个请求
    rows = _world_rows('invade_his', page, my_score=my_total)
    my_rank = _world_my_rank('invade_his', my_total)
    if rows is None:
        rows = _invade_rows(page, seed)
        my_rank = -1
    if my_rank is None:
        my_rank = -1
    data = [my_total, my_rank, rows, 1, 1, 1, 1, _pager(page, total)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============================ 9002 妖神传说结算 ============================
# ★2026-09-15 (v54) 重写。事故(用户现场报「每打一场发两份奖励」+ 逐行核对 1.31 明文):
#
#   客户端在**三个**地方发 9002 (旧档只记了两个, 漏了战败那一处):
#     app/war/WarStageUnits.lua:783   result=0  真·胜利结算
#     app/war/WarStageUnits.lua:867   result=1  战败结算 (warFinishFaileResultReuest)
#     app/war/WarStagePVE.lua:1283    result=1  Stage:exitPVELegend() 退场补发
#                                     (attack/round/dead/kill/addexp 全 0, ourSide 只有 ucode)
#   请求体 = NetLengendReward:sendMessage 的 msg 字典 (NetBase:toServerMsg -> CTable2Json),
#     键: attack/round/dead/skillnum/addexp/result/ourside/difficulty/time/sign
#     ⇒ **result 和 difficulty 就在请求体里**, 旧实现却整个 body_json 都没接 (函数签名收了, 从不解析)。
#
#   旧实现 = "来一次给一次 500+lv*10 金币" ⇒ 两个后果:
#     ① 战败(result=1) 和 退场补发(result=1) **照样发钱**;
#     ② 胜利后退出战斗场景再发一次 ⇒ **每场两份** (实测 04:44:18 / 04:44:24 与 04:55:31 / 04:55:41 各一对)。
#   对照铁证: 其余结算 9001/9003-9008/8006/1027/1088/7009 **全部**按 result 分支 —— 9002 是唯一的例外。
#
#   现在按官方语义:
#     - **只有 result==0 且 difficulty ∈ 1..5 才结算奖励**; 其余一律空掉落, 且**不写存档**。
#     - 奖励改为**官方掉落表**: 当天 T25 行的 Difficulty{difficulty}RewardID (= T02_Reward.GroupID,
#       handlers_game.t25_reward_group 取数), 由 _grant_group(gid, roll=True) 按 Rate 逐行判定。
#       ⇒ 面板"可能获得"的 5% 稀有终于和实发一致 (旧实发是私服造的金币, 与预览无关)。
#     - 无论胜负都回 roleInfo + 我方阵容 (客户端 :80 gamer_setRoleInfo / :82 monster_updateItem 要用),
#       失败/退场路径也必须回 [1,...] —— 客户端 :64 `statecode ~= 1 就 return`, 胜利路径的 _callFunc
#       是结算 UI 回调, 回错误码会把胜利界面卡住 (铁律 #165: 宁可空掉落, 不可断状态机)。
def handle_lengend_reward(body_json):
    from handlers_game import (_role_info, _drop, _gain_monster_exp, _t25_today_row,
                               t25_reward_group)
    from handlers_equip import _mon_with_state
    from handlers_task import _grant_group

    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    # 解析不出 result 时按"失败"处理: 不确定就绝不发奖 (旧实现在这里就漏了)
    try:
        result = int(req.get('result'))
    except Exception:
        result = 1
    try:
        difficulty = int(req.get('difficulty') or 0)
    except Exception:
        difficulty = 0
    iswin = (result == 0) and (1 <= difficulty <= 5)

    # 1) 我方怪经验同步 (与 9001/8006 同口径: 只有胜利才吃 addexp)
    units = []
    for m in (req.get('ourside') or []):
        if not isinstance(m, dict):
            continue
        try:
            uc = str(m.get('ucode') or '')
            addexp = int(m.get('exp') or 0)
        except Exception:
            continue
        if not uc:
            continue
        if iswin and addexp > 0:
            _gain_monster_exp(uc, addexp)
        try:
            mi = _mon_with_state(uc)
            if mi:
                units.append(json.loads(mi))
        except Exception:
            continue
    if not units:
        # 兜底: 请求里没有可解析阵容时仍回当前上阵怪 (msg[2] 不能是 null, 见 LAW-A)
        for uc in all_monster_ucodes()[:5]:
            try:
                mi = _mon_with_state(uc)
                if mi:
                    units.append(json.loads(mi))
            except Exception:
                continue

    # 2) 奖励: 只有真·胜利 + 合法难度
    drop = []
    if iswin:
        gid = t25_reward_group(_t25_today_row(), difficulty)
        if gid:
            groups, ok = _grant_group(gid, roll=True)
            if ok and groups:
                drop = groups
        if not drop:
            # 保底: 官方组取不到时给固定金币 (与旧行为同量级)。正常路径不会走到这 ——
            # 59 个官方组每组都含 Rate=1 的铜钱/鬼道行, roll 之后必非空。
            lv = int(EQ_STATE.get('player_lv', 1))
            gold = 500 + lv * 10
            EQ_STATE['gold'] = int(EQ_STATE.get('gold', 0)) + gold
            drop = [[3, [[gold]]]]
        save_state()          # 失败/退场路径无变更 -> 不写盘
    # ★2026-10-16（逐槽对客户端）：`NetLengendReward.lua` 与战场结算同族读**五槽**
    #   （roleInfo / ourFormation / dropInfo / talent / exDropItems），而这里原来只回 3 槽
    #   ⇒ msg[4]/msg[5] 恒 nil。已核过本装机：`talent` 读完无使用点、`exDropItems` 有 next() 守卫，
    #   所以当前不崩也不丢奖励（奖励在 msg[3]）；但"同一少槽形态"刚在 7009/8006 上
    #   造成"同一协议两条路径两种形状"，故统一补成 5 槽。
    data = [_role_info(), units, _drop(drop), 0, _drop([])]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')
