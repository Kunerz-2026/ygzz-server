# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— PVP 系统 (2026-09-06 按 NetPvp* 契约; 机器人对战, 全部本地化)
#   7001 GETBATARR  -> [1,[team1,team2?,team3?]]; team=[score,排名,队名,defenselist11f,rewardstate,rewardid,mapid]
#   7002 SAVEBATARR req {teamnum, defense} -> [1,{}] (客户端只回调)
#   7003 UNLOCKMAP  req {mapid} -> [1,[roleInfo]]
#   7008 ENTER      -> [1,[roleInfo, removeProp]] (扣1体力道具1006)
#   7010 MATCH      -> [1,[isrobot(0=机器人), atktime, rolename, Img, integral, formation(17f), currentStreak]]
#   7009 WARRESULT  req {ourside, defender, atktime, isrobot, result} -> [1,[coun, roleInfo]]
#   7011 GETBOXREWARDS -> [1,[box5, box10, box15, todaystreak, maxstreak]]
#   7012 RECEIVEBOX req {boxid}  -> [1,[box5, box10, box15, roleInfo, dropInfo]]
#   7006 RECEIVE    req {teamnum} -> [1,[roleInfo, dropInfo]];  7007 ONEKEYRECEIVE -> [1,[roleInfo, dropInfo]]
import json, random, time
from state import EQ_STATE, save_state, get_monster_base, get_monster_lv, get_monster_exp, get_monster_add, all_monster_ucodes, vitality_timerid
from state import explore_role_id as _explore_role_id
from handlers_draw import _MON_STATIC

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

# L91 基础属性: mid -> [hp, atk, def, cri]
_MON_STATS = {}
try:
    _t = _load_json('L91_Status.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _mid = int(_r[_h['ID']])
            _MON_STATS[_mid] = [int(_r[_h['Hp']] or 0), int(_r[_h['Atk']] or 0),
                                int(_r[_h['Def']] or 0), int(_r[_h['Cri']] or 0)]
        except Exception:
            continue
except Exception:
    pass

_NAMES = ['测试玩家', '云游道人', '夜行书生', '江南小师叔', '山野猎妖人', '侠客阿良', '醉仙翁', '补天客']

def _pvp():
    return EQ_STATE.setdefault('pvp', {'teamnum': 1, 'mapid': 1, 'streak': 0, 'streak_max': 0,
                                       'streak_date': '', 'boxes': {'5': 0, '10': 0, '15': 0},
                                       'rank_day': ''})

def _reset_pvp_day():
    p = _pvp()
    today = time.strftime('%Y-%m-%d')
    if p.get('streak_date') != today:
        p['streak_date'] = today
        p['streak'] = 0
        p['streak_max'] = 0
        p['boxes'] = {'5': 0, '10': 0, '15': 0}
        p['rank_day'] = ''

def _defense_item(uc, idx):
    # 11字段: [ucode, staticid, level, hp, atk, def, cri, skin, posx, posy, score]
    b = get_monster_base(uc)
    if b is None:
        return None
    mid, skin, num, skills = b
    # ★2026-09-11 (v21 §3.5): PVP 阵容皮肤也要用穿戴皮肤 (旧实现取基础皮肤 -> 竞技场里皮肤被"脱掉")
    try:
        from state import get_monster_skin as _gms
        skin = _gms(uc) or skin
    except Exception:
        pass
    st = _MON_STATS.get(mid, [0, 0, 0, 0])
    ad = get_monster_add(uc)
    hp = st[0] + ad[0]; atk = st[1] + ad[1]; df = st[2] + ad[2]; cri = st[3] + ad[3]
    lv = get_monster_lv(uc)
    score = int(hp / 10) + atk * 2 + df * 2 + cri * 5 + lv * 10
    # ★2026-10-18：槽 0 = **字符串 ucode**（客户端背包键是字符串，理由见 `_pvp_row_11f` 注释）
    return [str(int(uc)), mid, lv, hp, atk, df, cri, skin, idx + 1, 1, score]

def _player_team():
    # 我方队伍 (上阵: 初始3只+已抽, 最多5)
    items = []
    score = 0
    for i, uc in enumerate(all_monster_ucodes()):
        if i >= 5:
            break
        d = _defense_item(uc, i)
        if d:
            items.append(d)
            score += d[10]
    return score, items

def _enemy_team():
    """机器人敌阵容（17 字段：11f + skill1..6）。

    ★★2026-10-25（真机：对手 1~2 级却几万血、根本打不过）：
      修前从**全 L91** 随机抽 5 只（含 BOSS 级高血怪），hp/atk/def 直接取表里的基础值，
      等级却随机 `1/2/3/5` —— 于是出现"1 级怪几万血"。客户端 `NetPvpMatch` 是把这些槽
      逐个 `tonumber` 当**真实战斗数值**用的，所以那不是显示问题，是真打不过。
    现在优先**镜像玩家自己的上阵队**：`_player_team()` 的 11 槽本来就是客户端按等级算好的
    真实数值（等级/属性/皮肤/技能全同源）⇒ 同水平、必然可打。
    只有玩家一只怪都没有（新号）时才走保守兜底：只取低血量怪 + 玩家等级 + 每级 ~10% 放大。
    """
    try:
        _score, own = _player_team()
    except Exception:
        own = []
    items = []
    for i, r in enumerate((own or [])[:5]):
        try:
            mid = int(r[1])
            skin = int(r[7]) or int((_MON_STATIC.get(mid) or (0, 0))[1] or 0)
            item = [2000 + i, mid, int(r[2]), int(r[3]), int(r[4]), int(r[5]),
                    int(r[6]), skin, i + 1, 1, int(r[10])]
            item += [int(x or 0) for x in (list(r[11:17]) + [0] * 6)[:6]]
            items.append(item)
        except Exception:
            continue
    if items:
        return items
    lv = max(1, min(60, int(EQ_STATE.get('player_lv', 1) or 1)))
    mids = [m for m, s in _MON_STATS.items() if 0 < int(s[0]) <= 2000]
    if not mids:
        return []
    f = 1.0 + lv * 0.1
    picks = random.sample(mids, min(5, len(mids)))
    items = []
    for i, mid in enumerate(picks):
        st = _MON_STATS.get(mid, [0, 0, 0, 0])
        sk = _MON_STATIC.get(mid, (0, 0, [0] * 6, None))
        skills = sk[2] if len(sk) > 2 else [0] * 6
        skin = int(sk[1] or 0) if len(sk) > 1 else 0
        item = [2000 + i, mid, lv, int(st[0] * f), int(st[1] * f), int(st[2] * f),
                max(1, int(st[3] / 100)), skin, i + 1, 1,
                int((st[0] + st[1] * 20) * f / 10)]
        item += [int(s or 0) for s in (list(skills) + [0] * 6)[:6]]
        items.append(item)
    return items

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
    # 与原 handlers_game._drop 相同: 客户端 refreshDropInfo 无"单元素数组解包",
    # 组列表/每组 goodslist 均原样返回 (单组也直接输出, 否则服务器解包崩/客户端 goodsInfo 变表)
    return [[], list(groups or [])]

def _box_states(p):
    st = []
    for need in ('5', '10', '15'):
        s = p['boxes'].get(need, 0)
        st.append(2 if s >= 2 else (1 if p['streak_max'] >= int(need) and s == 0 else 0))
    return st

def _grant(gold=0, soul=0, others=None):
    if gold:
        EQ_STATE['gold'] += gold
    if soul:
        EQ_STATE['soulcoin'] += soul
    groups = [[[3, [[gold]]], [7, [[soul]]]] if (gold or soul) else []][0]
    if others:
        groups += others
    return _drop(groups)

# ============ handlers ============
# ============================================================================
# ★★2026-10-16（任务书 D④）：妖力布阵的**三队**与"真持久化"
# ----------------------------------------------------------------------------
# 修前两个事实（都是"看着能用、其实是空壳"）：
#   · `7001` 只回**一个** team 元素，而客户端 `NetPvpGetBatArr.lua:74` 是
#     `for i = 1, 3 do local teamlist = msg[i] or nil`，紧接着
#     `PVPDefenseHome.lua:254` 的 `for teamnum = 1, 3 do … self.m_teamData[teamnum].defenselist`
#     —— 只有一个元素时 `m_teamData[2]` 是 nil，`nil.defenselist` **当场抛 Lua 错误**
#     ⇒ 妖力布阵开页崩（任务书 D 验收第一条）。
#   · `7002` 只把 `teamnum` 存进 `pvp`，`defense` 整个丢掉 ⇒ 保存后重进阵容永远是空的。
# 现在：`EQ_STATE['pvp']['teams'][<1|2|3>]` 存每队的防守行（客户端 7002 送的那份原始行），
#   7001 按**三队**回，每队 7 槽 `[teamscore, ranking, teamname, defenselist11f, rewardstate,
#   rewardid, mapid]`；行的 11 槽由存下来的行现场折算（hp=basehp+addhp …）。
#   同一只妖怪不许同时在两个队里（客户端码表 `5027 存在在其他队伍列表中`）。
# ============================================================================
_PVP_TEAM_MAX = 5          # 客户端 `refresh_defenselist` 的 `index < 6` 就是 5 只

def _pvp_teams():
    p = _pvp()
    t = p.get('teams')
    if not isinstance(t, dict):
        t = p['teams'] = {}
    for k in ('1', '2', '3'):
        if not isinstance(t.get(k), dict):
            t[k] = {'name': '', 'defense': []}
        if not isinstance(t[k].get('defense'), list):
            t[k]['defense'] = []
    return t

def _pvp_row_11f(row):
    """7002 存下来的原始行 -> 7001 的 11 槽 `[ucode, staticid, level, hp, atk, def, cri,
    skin, posx, posy, score]`（客户端 `NetPvpGetBatArr.lua:41-51` 逐个 tonumber 读）。"""
    def _n(k, d=0):
        try:
            return int(float(row.get(k) or d))
        except (TypeError, ValueError):
            return d
    hp = _n('basehp') + _n('addhp')
    atk = _n('baseatk') + _n('addatk')
    df = _n('basedef') + _n('adddef')
    cri = _n('basecri') + _n('addcri')
    # ★★2026-10-18（真机："开始布阵后 5 只全变妖怪茧、Lv.1"）：
    #   **槽 0 的 ucode 必须是字符串**。客户端 `SQMonster:insertItem` 的键就是 1032 行里
    #   `_mon()` 发出的字符串（`'"%d"' % ucode`），而
    #     · `getID/_getCurSkin` 会 `tostring(_ucode)` 再查 —— 发数字还能命中；
    #     · `getLevel` **不 tostring**，直接 `data[_ucode]` —— 发数字**必然查不到，回落 1**；
    #   于是 StageScene 建士兵时 staticid/skin 勉强对、等级恒 1；而
    #   `WarPVPDefenseUI:getLoadAndUnloadList` 里 `if ucode == objucode`（背包键是字符串）
    #   也对不上 ⇒ 上阵列表全被当成"未上阵"。发字符串与其它所有怪物协议一致。
    return [str(_n('ucode')), _n('staticid'), _n('level'), hp, atk, df, cri,
            _n('skin'), _n('pox'), _n('poy'), _n('score')]

def _pvp_team_score(rows):
    return sum(_pvp_row_11f(r)[10] for r in rows)

def _pvp_reward_group(need):
    """5/10/15 连胜宝箱 → `T02_Reward` 组号。

    ★★2026-10-25（用户指出"宝箱有明文奖励"）：客户端 `PVPBattle.lua:refreshRewardPreview`
      用 `rewardID = 902 + index`（index 1/2/3 对应 5/10/15 连胜）去 `IconData:create()`
      渲染预览 ⇒ **明文组号就是 903 / 904 / 905**：
        903: 鬼道积分20 + 铜钱100 + 体力2 + 道具4000601×1
        904: 鬼道积分30 + 铜钱300 + 体力5 + 道具4000601×1
        905: 鬼道积分50 + 铜钱500 + 道具1017×1 + 唤妖符×1
      修前发的是自定 `need×100 金 / need×10 妖魂`，与表完全不符。
    """
    return str(902 + int(need) // 5)


# 每日防守奖励：客户端 7001 的 `rewardid` 决定预览，`rewardstate` 决定按钮可不可点。
# ⚠ 明文只到这一步；组号取 **901**（鬼道积分 200）—— 它与宝箱段 903~905 同段、
#   902 是空奖励（积分 0），按编号段推定为每日防守奖励。
PVP_DAILY_REWARD_GID = '901'


def _daily_def_reward(p):
    """→ `(rewardstate, rewardid)`：1=可领 / 2=今日已领；组号见 `PVP_DAILY_REWARD_GID`。"""
    today = time.strftime('%Y-%m-%d')
    return (2 if p.get('rank_day') == today else 1), int(PVP_DAILY_REWARD_GID)


def _pvp_team_element(rows, p, idx, empty_defense):
    """7001 的单个 team 元素；**没有阵容的队也要给结构完整的一项**（否则客户端索引 nil）。"""
    if rows:
        defense = [_pvp_row_11f(r) for r in rows]
        score = _pvp_team_score(rows)
        name = (p['teams'].get(str(idx)) or {}).get('name') or ('妖力队伍%d' % idx)
    else:
        defense, score, name = [], 0, ('妖力队伍%d' % idx)
    _rs, _rid = _daily_def_reward(p)
    return [score, 1, name, defense, _rs, _rid, p.get('mapid', 1)]

def handle_pvp_get_bat_arr(body_json=None):
    _reset_pvp_day()
    p = _pvp()
    score, defense = _player_team()
    # ★2026-10-02 (D6=C1): T32 Type=601「妖力布阵总妖力值」= 7001 下发的 `team[0]`。
    #   取证: 客户端 `PVPDefenseHome.lua:386/587` 把 `.teamscore` 填进界面, 同文件
    #   `:352/:385/:576` 注释逐条写「妖力值」, `:1` 是「妖力布阵」, `:57` 用的正是本 msgid。
    #   ⚠ 口径说明: 服务端这个 score(`handlers_pvp._defense_item` 的
    #   `hp/10 + atk*2 + df*2 + cri*5 + lv*10`) **不等于**客户端权威公式
    #   `SQFormula.getFightScore`; 这里接的是"布阵界面显示的那个 teamscore", 有据可依。
    #   用峰值: 换阵容会让它下降, 而达成判定是读取时现算的。
    from handlers_achievement import set_ach
    set_ach(601, 0, score)
    teams = _pvp_teams()
    out = []
    for i in (1, 2, 3):
        rows = teams[str(i)].get('defense') or []
        if not rows and i == 1:
            # 第 1 队还没保存过阵容时，沿用"当前最强 5 只"这份既有默认（改造前就一直这么发）
            _rs, _rid = _daily_def_reward(p)
            out.append([score, 1, '我的妖怪军团', defense, _rs, _rid, p.get('mapid', 1)])
        else:
            out.append(_pvp_team_element(rows, p, i, False))
    return ('[1,' + json.dumps(out, separators=(',', ':')) + ']').encode('utf-8')

def handle_pvp_save_bat_arr(body_json):
    """7002 SAVEBATARR：**真的**把该队的防守阵容存下来（任务书 D④）。

    契约与错误码逐个取自装机客户端 `app/network/pvp/NetPvpSaveBatArr.lua:6-17`：
      5026 队伍id错误 / 2009 保存的防守阵容为空 / 2002 怪物背包为空 /
      2005 怪物背包中不存在该怪物 / 5027 存在在其他队伍列表中 / 2010 场上怪物过多
    ⚠ `2009 阵容为空`**不拦**：客户端在"清空队伍再保存"时发的也是 `{}`，
      把它当错误会让玩家永远清不掉一个队。空 = 清空该队（见 `_pvp_teams`）。
    """
    try:
        req = json.loads(body_json) if body_json else {}
        if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
            req = {}
    except Exception:
        return b'[0,{}]'
    try:
        teamnum = int(req.get('teamnum') or 0)
    except (TypeError, ValueError):
        teamnum = 0
    if teamnum not in (1, 2, 3):
        return b'[5026]'
    raw = req.get('defense')
    rows = []
    if isinstance(raw, list):
        for r in raw:
            if isinstance(r, dict):
                rows.append(r)
    if len(rows) > _PVP_TEAM_MAX:
        # ⚠ 顺序：`2010 场上怪物过多` 是**请求体形状**错误，先判它。
        #   否则 6 行里只要有一行重复/不是自己的妖怪，就会先撞 5027/2005，
        #   玩家看到的提示变成"存在在其他队伍列表中"——与真实原因不符。
        return b'[2010]'
    own = set(str(u) for u in all_monster_ucodes())
    seen = []
    for r in rows:
        try:
            uc = str(int(r.get('ucode')))
        except (TypeError, ValueError):
            return b'[2005]'
        if uc not in own:
            return b'[2005]'                # 怪物背包中不存在该怪物
        if uc in seen:
            return b'[5027]'
        seen.append(uc)
    teams = _pvp_teams()
    for other in (1, 2, 3):                 # 同一只妖怪不许同时在两个队里
        if other == teamnum:
            continue
        for r in (teams[str(other)].get('defense') or []):
            try:
                if str(int(r.get('ucode'))) in seen:
                    return b'[5027]'
            except (TypeError, ValueError):
                continue
    _pvp()['teamnum'] = teamnum
    changed = ([str(int(r.get('ucode'))) for r in rows]
               != [str(int(r.get('ucode'))) for r in (teams[str(teamnum)].get('defense') or [])
                   if isinstance(r, dict) and str(r.get('ucode', '')).lstrip('-').isdigit()])
    teams[str(teamnum)] = {'name': teams[str(teamnum)].get('name') or '',
                           'defense': rows}
    save_state()
    # ★2026-10-16（任务书 D④ + E 的 CT=11）：只有**阵容真的变了**才记一次「妖力布阵」。
    #   证据：`T37_DailyTasks` 的 CT=11 文案是"妖力布阵"，而 7002 正是"保存妖力布阵"这条协议
    #   （`NetPvpSaveBatArr.lua:24 sendMessage(_teamnum, _defenseList)`）；
    #   1090 是排位赛重连确认，与布阵无关（2026-10-15 DECISIONS 已明确否掉那条替代）。
    if changed:
        try:
            from handlers_task import bump_task, CT_LAYOUT
            bump_task(CT_LAYOUT, 1)
        except Exception:
            pass
    return b'[1,{}]'

def handle_pvp_unlock_map(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
        if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
            req = {}
        p = _pvp()
        p['mapid'] = int(req.get('mapid') or 1)
        save_state()
    except Exception:
        pass
    return json.dumps([1, [_role_info()]], separators=(',', ':')).encode('utf-8')

def handle_pvp_enter(body_json=None):
    """7008 PVP_ENTER：进入妖怪对战，**消耗 1 张挑战符（道具 1016）**。

    ★★2026-10-25（真机："打完符没消耗"）：客户端的依据是明文的 ——
      · `PVPBattle.lua:74` 大厅里显示 `prop_getMe():getCount(1016)`（票数就是道具 1016）；
      · `NetPvpEnter.lua` 的码表只有 `0 消息体为空` 与 **`5014 进入妖师对战的门票不足`**；
      · 发完 7008 后 BI 打点 `doOutflow(..., 1016, 1)`（`PVPBattle.lua:175`）也是扣 1 张符。
    修前这里扣的是**体力 1006**、回包也回 `['1006',1006,…]` ⇒ 客户端那栏符数永远不变，
    而且体力被白扣（PVP 本来就不吃体力）。现在按 1016 扣、按 1016 回，码用 5014。
    """
    _p = EQ_STATE.setdefault('prop', {})
    if not isinstance(_p, dict):
        _p = EQ_STATE['prop'] = {}
    try:
        have = int(_p.get('1016', 0) or 0)
    except (TypeError, ValueError):
        have = 0
    if have < 1:
        return b'[5014]'                       # 客户端码表：进入妖师对战的门票不足
    rem = have - 1
    if rem > 0:
        _p['1016'] = rem
    else:
        _p.pop('1016', None)
    save_state()
    return json.dumps([1, [_role_info(), ['1016', 1016, rem]]],
                      separators=(',', ':')).encode('utf-8')

def handle_pvp_match(body_json=None):
    _reset_pvp_day()
    p = _pvp()
    name = random.choice(_NAMES) + str(random.randint(1, 999)).zfill(3)
    # ★★2026-10-16（任务书 D③）：`atktime` 是这一场的**唯一句柄**：
    #   客户端会把它原样回送在 7009 里（`NetPvpWarResult.lua:36 msg["atktime"]`），
    #   所以"开战"与"结算"天然配对。这里开一局 `pvp` 会话，7009 必须凭它结算。
    atktime = int(time.time() * 1000)
    try:
        from battle_session import open_session as _bs_open
        _bs_open('pvp', atktime)
    except Exception as _e:
        print('[BS] !! 开妖怪对战会话失败(不影响匹配): %r' % (_e,), flush=True)
    data = [0, atktime, name, 0, random.randint(1000, 9999), _enemy_team(), p.get('streak', 0)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

def handle_pvp_war_result(body_json):
    # 7009: 结算连胜/宝箱; resp [1,[coun, roleInfo]]
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    # 装机客户端的战斗结束路径只发 0（胜）或 1（负）。缺失/非法结果不是一场已结算的对战；
    # 尤其不能让空请求经 `or 0` 被当作胜利，进而虚增任务、连胜和奖励。
    result = req.get('result')
    if not ((type(result) is int and result in (0, 1))
            or (type(result) is str and result in ('0', '1'))):
        return b'[514]'
    # ★2026-10-16（任务书 D③）：`isrobot`（0=机器人/1=玩家）与 `defender` 都是客户端
    #   真有码表的字段（`NetPvpWarResult.lua:8-9`：5015 机器人类型错误 / 5016 防守者角色名为空）。
    isrobot = req.get('isrobot')
    if isrobot is not None and str(isrobot) not in ('0', '1'):
        return b'[5015]'
    if not str(req.get('defender') or '').strip():
        return b'[5016]'
    # ★★2026-10-16（任务书 D③）：**一次性结算**。修前这里只信请求体：同一条 7009 连发两次
    #   就再加一次金币/妖魂、再计一次连胜、再 bump 一次任务与成就。
    #   现在必须凭 7010 开出来的那一局（target = atktime）才能结算，结算即消费。
    try:
        atktime = int(req.get('atktime') or 0)
    except (TypeError, ValueError):
        atktime = 0
    try:
        from battle_session import settle as _bs_settle
        _ok, _reason = _bs_settle('pvp', atktime, '%s|%s|%s' % (atktime, req.get('defender'),
                                                               req.get('result')))
    except Exception as _e:
        print('[BS] !! 妖怪对战结算状态机异常(按未结算处理): %r' % (_e,), flush=True)
        _ok, _reason = False, 'error'
    if not _ok:
        # 重放/没开战：**给成功应答但零状态变化**（客户端不该弹错，但一分奖励都不发）
        print('[BS] 7009 结算被拒: reason=%s atktime=%s -> 零奖励零状态变化'
              % (_reason, atktime), flush=True)
        # ⚠ 形状必须与正常路径**同槽数**（3 槽）：客户端按固定下标取 msg[2]/msg[3]，
        #   "有的分支 2 槽、有的 3 槽"会让同一协议在不同情况下走不同分支（本轮 D19 护栏钉住）。
        return ('[1,' + json.dumps([1, _role_info(), _drop([])],
                                   separators=(',', ':')) + ']').encode('utf-8')
    _reset_pvp_day()
    p = _pvp()
    # ★★2026-10-25（真机：连胜不涨，7009 日志 `result=1 -> LOSE`）：**PVP 的 result 是翻转过的**。
    #   客户端 `WarStageUnits.lua:791`（成功路径）`_warResult = 0`、`:897`（失败路径）`= 1`，
    #   而 PVPBATTLE 分支在发 7009 前又做了一次翻转（`:830` / `:925`）：
    #       `_warResult = _warResult == 1 and 0 or 1`
    #   ⇒ 服务端收到的 **1 = 胜、0 = 负**，与修前假设的"0=胜"正好相反。
    #   ⚠ 只有 7009 这条翻；9003/9009/9006 等同族协议不翻，别把这个映射套过去。
    iswin = int(result) == 1
    # ★2026-10-25（真机：连胜不涨）：这行是排查用的**唯一事实来源** —— 客户端的 `result`
    #   语义是 0=胜 / 1=负（`NetPvpWarResult.lua`），但"玩家觉得赢了却记成负"要靠它才能证实/证伪。
    print('[PVP] 7009 结算: result=%r -> %s, atktime=%s, isrobot=%r, streak(前)=%s'
          % (result, 'WIN' if iswin else 'LOSE', atktime, isrobot, p.get('streak')), flush=True)
    # ★2026-10-02 (成就进度写入方): 斗法/妖怪对战的胜负累计。
    #   T32 Type=804「【攻】发起挑战获得胜利N」-> 组 1005/级别1(斗法小道士)
    #   T32 Type=805「【攻】挑战失败次数N」   -> 组 1005/级别1(同组另一条条件)
    #   T32 Type=701「首次进行妖怪对战」      -> 组 1004/级别3(妖御天下)
    #   口径: "妖怪对战" = 客户端对 7001-7012 这套 PVP 的正式叫法
    #        (`AppConfig.lua:49 PVPMETCH=1112,--妖怪对战`); 一次结算 = 一次对战。
    from handlers_achievement import note_ach
    if iswin:
        p['streak'] += 1
        p['streak_max'] = max(p['streak_max'], p['streak'])
        # ★★2026-10-22（审查高 H5）：UG 41021「妖怪对战达成 1 连胜」是**永久**主线任务，
        #   而 `pvp.streak_max` 跨天会被 `_reset_pvp_day()` 清零 ⇒ 修前今天"可领奖"、
        #   隔天进 PVP 又退回"进行中"（永久主线不该倒退）。
        #   这里另记一本**永久峰值** `ug.pvp_streak_peak`（`handlers_ugtask._sched` 读它）；
        #   日字段继续给「10 连胜宝箱」用，两本账各管各的、互不替代。
        try:
            _ug = EQ_STATE.setdefault('ug', {})
            if not isinstance(_ug, dict):
                _ug = EQ_STATE['ug'] = {}
            _pk = int(_ug.get('pvp_streak_peak', 0) or 0)
            if int(p['streak_max']) > _pk:
                _ug['pvp_streak_peak'] = int(p['streak_max'])
        except Exception:
            pass
        # ★★2026-10-25（用户质疑"PVP 有没有每场掉落"）：**撤掉每场 +100 金 / +10 妖魂**。
        #   查证：PVP 只有 `P01_PVPMstrGroup / P10_PVPLevel / P14_PVPScore` 三张表，**没有奖励表**；
        #   客户端 `NetPvpWarResult` 只是"有就给掉落面板"（`exDropItems` 为空则不弹），
        #   并没有任何"每场必给"的明文/配置。原来的 +100/+10 来自初始提交，属**私服自定值**，
        #   按本项目铁律（不编造）撤掉。连胜本身照记；连胜宝箱仍走 7011/7012（那是客户端明写的 UI）。
        note_ach(804, 0, 1)
        note_ach(701, 0, 1)
        # ★2026-10-12: T37 每日任务 CT=28「妖怪对战达成 10 连胜」。
        #   口径 = **连胜一旦达成门槛就记一次**（`bump_task` 内部按 Quantity cap，
        #   重复调用不会超过门槛，也不会把"每天都要重新打 10 连胜"这种语义搞错）。
        if p['streak'] >= 10:
            try:
                from handlers_task import bump_task, CT_PVP_STREAK
                bump_task(CT_PVP_STREAK, 1)
            except Exception:
                pass
    else:
        p['streak'] = 0
        note_ach(805, 0, 1)
    # T37 CT=8「完成 1 次妖怪对战」按 7009 真实结算计；胜负都算，匹配/进场不算。
    from handlers_task import bump_task, CT_PVP_BATTLE
    bump_task(CT_PVP_BATTLE, 1)
    save_state()
    print('[PVP] 7009 结算完成: streak=%s max=%s gold=%s'
          % (p.get('streak'), p.get('streak_max'), EQ_STATE.get('gold')), flush=True)
    # ★★2026-10-16（任务书 D 复核）：**回包必须补齐第 3 槽**。
    #   客户端 `NetPvpWarResult.lua:53-55` 读三槽：`msg[1]=coun / msg[2]=roleInfo / msg[3]=exDropItems`，
    #   并在 `:69-71` 用 `if exDropItems and next(exDropItems) and next(exDropItems[2])`
    #   决定要不要弹掉落面板。**槽位必须恒定存在**（2 槽/3 槽随分支变化是协议事故），
    #   但**内容为空**（2026-10-25：每场掉落已按"无表可证"撤掉，这里就回空 dropInfo）。
    return ('[1,' + json.dumps([1, _role_info(), _drop([])],
                               separators=(',', ':')) + ']').encode('utf-8')

def handle_pvp_get_box_rewards(body_json=None):
    _reset_pvp_day()
    p = _pvp()
    data = _box_states(p) + [p.get('streak', 0), p.get('streak_max', 0)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

def handle_pvp_receive_box(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
        if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
            req = {}
        boxid = str(int(req.get('boxid') or 0))
    except Exception:
        return b'[0,{}]'
    _reset_pvp_day()
    p = _pvp()
    need = int(boxid)
    if need not in (5, 10, 15):
        return b'[0,{}]'
    if p['boxes'].get(boxid, 0) >= 2:
        return b'[0,{}]'
    if p['streak_max'] < need:
        return b'[0,{}]'
    # ★★2026-10-25：按**明文表**发奖，不再自定数值（组号见 `_pvp_reward_group`）。
    #   发不出去就**不标已领**（否则玩家点了一次、箱子没了、东西没到）。
    gid = _pvp_reward_group(need)
    try:
        from handlers_task import _grant_group
        groups, ok = _grant_group(gid)
    except Exception as _e:
        print('[PVP] !! 宝箱发放异常 boxid=%s gid=%s: %r' % (boxid, gid, _e), flush=True)
        groups, ok = [], False
    if not ok:
        return b'[0,{}]'
    p['boxes'][boxid] = 2
    save_state()
    data = _box_states(p) + [_role_info(), _drop(groups)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

def handle_pvp_receive(body_json):
    # 7006 每日防守奖励（客户端 `PVPDefenseHome.lua:645` 用 7001 下发的 rewardid 渲染预览）
    try:
        req = json.loads(body_json) if body_json else {}
        if not isinstance(req, dict):   # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
            req = {}
    except Exception:
        return b'[0,{}]'
    _reset_pvp_day()
    p = _pvp()
    if p.get('rank_day') == time.strftime('%Y-%m-%d'):
        return b'[0,{}]'
    # ★★2026-10-25：按 `PVP_DAILY_REWARD_GID`（T02 901 = 鬼道积分 200）发，不再自定 200 金。
    try:
        from handlers_task import _grant_group
        groups, ok = _grant_group(PVP_DAILY_REWARD_GID)
    except Exception as _e:
        print('[PVP] !! 每日防守奖励异常: %r' % (_e,), flush=True)
        groups, ok = [], False
    if not ok:
        return b'[0,{}]'
    p['rank_day'] = time.strftime('%Y-%m-%d')
    save_state()
    data = [_role_info(), _drop(groups)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

def handle_pvp_one_key_receive(body_json=None):
    # 7007 一键领取（日奖励 + 可领宝箱）
    # ★★2026-10-25：改为**逐项按明文组发奖**（`_pvp_reward_group` / `PVP_DAILY_REWARD_GID`），
    #   不再把"金币×N / 妖魂×N"折算 —— 修前 7007 的数字和 7006/7012 各自一套，对不上表。
    #   任何一项发放失败都**不标记已领**，避免"点了没东西"。
    _reset_pvp_day()
    p = _pvp()
    try:
        from handlers_task import _grant_group
    except Exception as _e:
        print('[PVP] !! 一键领取导入失败: %r' % (_e,), flush=True)
        return b'[0,{}]'
    groups_all = []
    claimed = False
    for need in ('5', '10', '15'):
        if p['boxes'].get(need, 0) == 0 and p['streak_max'] >= int(need):
            groups, ok = _grant_group(_pvp_reward_group(need))
            if ok:
                p['boxes'][need] = 2
                groups_all += groups
                claimed = True
    if p.get('rank_day') != time.strftime('%Y-%m-%d'):
        groups, ok = _grant_group(PVP_DAILY_REWARD_GID)
        if ok:
            p['rank_day'] = time.strftime('%Y-%m-%d')
            groups_all += groups
            claimed = True
    if not claimed:
        # 客户端码表 `NetPvpOneKeyReceive.lua:8` = 5033「没有奖励可领取」
        return b'[5033]'
    save_state()
    data = [_role_info(), _drop(groups_all)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')
