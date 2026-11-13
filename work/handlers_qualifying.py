# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 竞技/排位赛 (1078-1091; 2026-09-08 按 NetPVPQualifying* 契约实装; 机器人对战)
#   1078 HOME  -> [1,[points, dangrading, daytime, rewardstate, count, wincount, [[id,state]..], ismatch]]
#                 points=积分 dangrading=段位id(P10) daytime=赛季剩余**天数**（客户端只渲染「N天」，见 handler 注释）
#                 count=今日剩余挑战次数 wincount=本周胜次 winrewardlist=[[P11_id,state]..] ismatch=0/1
#   1079 REWARD       -> [1,[roleInfo, dropInfo]] (赛季结算奖励; 码578已领)
#   1080 BOXREWARD req {id=P11_id} -> [1,[roleInfo, dropInfo]] (周胜次箱; 码538未达成)
#   1081 REPORT       -> [1,[totalwincount, [[brid, fivhgState(1攻/0守), score, victory(1胜/0负), dangrading, name]..]]]
#   1082 RANK  req {page} -> [1,[ownrank, img, rim, name, dangrading, point, totalwincount, serverid,
#                              [[rank,name,img,rim,dangrading,point,totalwincount,serverid,isSupport,supporNum,roleId]..],
#                              "cur/total"]]
#   1083 GETARRAY -> [1,[team1, team2]]; team=[teamscore, [阵容17字段..]]
#                 ⚠ 顶层**每队一个元素**, 不是平铺 [td, [0,[]], [0,[]]] —— 客户端
#                   `for i = 1, 2 do local teamlist = msg[i]` 把 msg[2] 当**第二队**读。
#                   队 1 = 防守(saveTeamOne) / 队 2 = 出战(saveAndExit); 修前只发 1 段且
#                   td 只有 2 槽 ⇒ 客户端 `teamlist[2]` 恒 nil ⇒ 队伍面板恒空。
#   1084 SAVEARRAY req {teamnum, defense} -> [1,[]] (真持久化到 qualify.defense[teamnum])
#   1085 MATCH  -> [1,[ownrank, [[num,rolename,rank,img,rim,dangrading,point,winpoint,team1,team2,roleid,serverid]..]]]
#   1086 REFRESHMATCH -> [1,[roleInfo, ownrank, [同样列表]]]
#   1087 CHECKARRAY -> [1,[roleInfo]]
#   1088 RESULT req {result(0胜/1负), num} -> [1,[points, addpoint, dangrading, tag(1升/0不变/-1降), state]]
#   1089 NETING -> [1,[stage(1战斗中断/0正常), battleData, ownrank]]
#   1090 ARRAGENETING req {choose} -> [1,[]]
#   1091 START req {num, roleid, rolename} -> [1,[roleInfo, battleData]] (扣1今日挑战次数)
# 赛季: 月度赛季键 YYYY-MM; 跨月结算 (按最终段位 P10 SectionReward 组) + 新赛季积分清零
import json, os, random, time
from state import EQ_STATE, save_state, vitality_timerid, get_monster_base, get_monster_lv, get_monster_exp, get_monster_add
from handlers_task import _grant_group

from state import table_dir as _table_dir   # ★2026-10-02 (审计 §1.6): 表根不再写死盘符
_DATA = _table_dir()

# ★2026-10-11（A8 排位赛重连）：1089 判"我正在战斗中"的时间窗（秒）。
#   为什么要上限：1091 START 存下这一局、1088 RESULT 才清；而"打完不结算就退出"
#   （或客户端根本没回 1088）是常态 ⇒ 不设上限会让玩家**每次登录都被判在战斗中**，
#   比重连不可达更糟。30 分钟远大于一局排位，且过期只是回 stage=0（不删档、不扣东西）。
NETING_TTL = 30 * 60

def _load_json(name):
    # ★v60 闭环 v58 §10 缺口 #3: 改走 state.load_table —— 按「尾字段 u32 长度」精确剥离,
    #   不再用 c[:c.rfind(']')+1]（长度字段含 0x5D 时会切错 -> 调用方 except -> 静默空表,
    #   实测 L98_AIPriority / T32_Achievement）。护栏: work/_v60_legacy_tail_guard_test.py
    #   ⚠ 必须**逐行复制**: load_table 的结果是**带缓存的同一个对象**, 而旧实现每次返回全新对象。
    #     调用方若原地改行(排序/填字段)会污染全局缓存 -> 这里保持"每次都是新对象"的原语义。
    from state import load_table as _lt
    return [list(_r) for _r in _lt(name, _DATA)]

# P10 段位: id -> (levelScore, winPoint, losePoint, sectionReward) 按 ID 升序
_P10 = []
try:
    _t = _load_json('P10_PVPLevel.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _P10.append((int(_r[_h['ID']]), int(_r[_h['LevelScore']] or 0),
                         int(_r[_h['WinPoint']] or 0), int(_r[_h['LosePoint']] or 0),
                         str(_r[_h['SectionReward']] or '0')))
        except Exception:
            continue
    _P10.sort(key=lambda x: x[1])
except Exception:
    pass
# P11 周胜次箱: id -> (count, rewardGid)
_P11 = []
try:
    _t = _load_json('P11_WeeklyWinCount.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _P11.append((int(_r[_h['ID']]), int(_r[_h['Count']] or 0), str(_r[_h['RewardID']] or '0')))
        except Exception:
            continue
except Exception:
    pass
_P11.sort(key=lambda x: x[1])

# 敌方阵容属性 (同 handlers_pvp._MON_STATS 源)
_MON_STATS = {}
try:
    _t = _load_json('L91_Status.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _MON_STATS[int(_r[_h['ID']])] = [int(_r[_h['Hp']] or 0), int(_r[_h['Atk']] or 0),
                                             int(_r[_h['Def']] or 0), int(_r[_h['Cri']] or 0)]
        except Exception:
            continue
except Exception:
    pass

SEASON_DAYS = 15          # 赛季长度 (天; 官方月度, 私服半月方便验证结算; 改 30=月度)
DAILY_CHALLENGE = 20      # 每日挑战次数
_NAMES = ['云游道人', '夜行书生', '江南小师叔', '山野猎妖人', '侠客阿良', '醉仙翁', '补天客']
_MON_NAMES = {1: '小狐仙', 2: '狸猫精', 3: '独眼鬼', 4: '树精', 5: '河童', 6: '山魈', 7: '石矶',
              8: '白猿', 9: '蜃精', 10: '蝎子精'}

def _q():
    q = EQ_STATE.setdefault('qualify', {'season': '', 'points': 0, 'reward': 0,
                                        'daily': '', 'daily_n': 0, 'week': '', 'week_win': 0,
                                        'boxes': {}, 'report': []})
    if not isinstance(q.get('boxes'), dict):
        q['boxes'] = {}
    if not isinstance(q.get('report'), list):
        q['report'] = []
    return q

def _season_key():
    """赛季键 = 周赛季（**对齐客户端文案**：每周一个赛季，周一 7:00 ~ 次周一 5:00）。

    2026-11-06 用户拍板「按客户端来」：原来私服是 15 天半月赛季（`SEASON_DAYS`），
    与客户端规则页写的「每周一个赛季」不一致 → 改成周赛季。
    边界取周一 05:00（客户端显示上一季到周一 5:00 结束）；新赛季从同一个 05:00 起算，
    05:00~07:00 是客户端的维护/结算间隙，服务端不再单独建模。
    """
    try:
        import datetime as _dt
        _now = _dt.datetime.now()
        _mon = (_now - _dt.timedelta(days=_now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        _cut = _mon + _dt.timedelta(hours=5)          # 本周一 05:00
        if _now < _cut:                                # 还没到本周一 5:00 -> 上一赛季
            _start = _mon - _dt.timedelta(days=7) + _dt.timedelta(hours=5)
            _end = _cut
        else:                                          # 本周一 5:00 之后 -> 本赛季
            _start = _cut
            _end = _mon + _dt.timedelta(days=7) + _dt.timedelta(hours=5)
        return _start.strftime('%Y-%m-%d'), int(_start.timestamp()), int(_end.timestamp())
    except Exception:
        _s = time.strftime('%Y-%m-%d')
        return _s, int(time.time()), int(time.time()) + 7 * 86400

def _week_key():
    import datetime as _dt
    try:
        _d = _dt.date.today()
        return '%d-W%02d' % (_d.isocalendar()[0], _d.isocalendar()[1])
    except Exception:
        return time.strftime('%Y-%m-%d')

def _dsg(points):
    # 积分 -> 段位 id (P10 LevelScore 查找到的最大门槛行)
    best = _P10[0][0] if _P10 else 1001
    for (sid, ls, wp, lp, sr) in _P10:
        if points >= ls:
            best = sid
    return best

def _dsg_bonus(sid):
    for (sid2, ls, wp, lp, sr) in _P10:
        if sid2 == sid:
            return wp, lp
    return 10, 0

def _season():
    key, s0, s1 = _season_key()
    q = _q()
    if q.get('season') != key:
        # 旧赛季 (若存在) -> 结算: 按最终段位 SectionReward 组计, reward=1 可领
        if q.get('season') and q.get('season') != '':
            _old_sid = _dsg(int(q.get('points', 0)))
            _gid = '0'
            for (sid2, ls, wp, lp, sr) in _P10:
                if sid2 == _old_sid:
                    _gid = sr
                    break
            q.setdefault('settle_gid', {})
            q['settle_gid'][str(q['season'])] = _gid
            q['reward'] = 1
        q['season'] = key
        # ★2026-11-06 用户拍板「按客户端来」：客户端规则页写明
        #   「每赛季开始时的初始积分为上赛季最终积分的 **70%**」；
        #   原来私服直接清零，与客户端文案不符 -> 改为 70%（向下取整）。
        _prev_pts = 0
        try:
            _prev_pts = int(q.get('points', 0) or 0)
        except (TypeError, ValueError):
            _prev_pts = 0
        q['points'] = max(0, _prev_pts * 70 // 100)
        q['daily'] = ''
        q['daily_n'] = DAILY_CHALLENGE
        q['boxes'] = {}
        q['report'] = []
        save_state()
    today = time.strftime('%Y-%m-%d')
    if q.get('daily') != today:
        q['daily'] = today
        q['daily_n'] = DAILY_CHALLENGE
        save_state()
    wk = _week_key()
    if q.get('week') != wk:
        q['week'] = wk
        q['week_win'] = 0
        # ★2026-10-02 (D6=C1): T32 Type=810「【周】挑战次数 N」的**计数源**。
        #   为什么必须新开一个键: `week_win` 是**胜次**(只在胜时 +1), `daily_n` 是**每日剩余**
        #   (每天重置), `report` 被截断到 50 条且只在跨赛季清 ⇒ 都不等于"周累计挑战次数"。
        #   上限证据: 810 的三档 60/100/140 = DAILY_CHALLENGE(20) × 3/5/7 ⇒ 正是"周累计次数"。
        q['week_n'] = 0
        q['boxes'] = {}
        save_state()
    return q, s0, s1

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

def _own_team():
    # 我方阵容 (17字段; 上阵前5)
    from handlers_pvp import _defense_item
    items = []
    score = 0
    from state import all_monster_ucodes as _amu
    for i, uc in enumerate(_amu()):
        if i >= 5:
            break
        d = _defense_item(uc, i)
        if d:
            items.append(d)
            score += d[10]
    return score, items

def _bot_team(pts):
    """敌方阵容（17 字段）：**镜像玩家当前上阵队**（与 `handlers_pvp._enemy_team` 同一口径）。

    ★★2026-11-06（真机：排位匹配到的怪全是"妖怪茧"）：
      修前是"从全 L91 随机抽 5 只 + 等级 1~5 + 第 8 槽 skin **写死 0**"。
      第 8 槽 = 客户端 `NetPVPQualifyingMatch` 的 `data.skin`，两条渲染路径都靠它取模型：
        · 战斗内 `StageDelegateQualifying:stage_initEnemyUnits`
          `WarSoldier:create(_staticID, ENEMY, Monster, skinid)`；
        · 匹配列表预览 `VQualifyingMatch.lua:245/284`
          `SDFactory:getMe():getObject(SD_OBJS.SDSkin, defenseList1[j].skin)`。
      skin=0 取不到皮肤/模型 -> 客户端画成"妖怪茧"。
      这与 2026-10-25 在 PVP 7008/7010 修过的"妖怪茧"是**同一个原因** ——
      那次只改了 `handlers_pvp._enemy_team`，排位(1085/1091) 的 `_bot_team` 漏了。
      同一段还有 10-25 的另一个教训没落进来：全 L91 基础属性 × 等级 1~5 = "1 级怪几万血"。
      ⇒ 一组功能一次修好：镜像玩家自己的上阵队（等级/属性/皮肤/技能全同源，同水平可打）。
    玩家一只怪都没有（新号/空档）时才走兜底：低血量怪 + 玩家等级 + 真实 Skin1。
    """
    from handlers_pvp import _defense_item   # 与 `_own_team` 同一份 11 槽构造
    from handlers_draw import _MON_STATIC    # mid -> (quality, skin, skills[6], tallents)
    try:
        _sc, own = _own_team()
    except Exception:
        own = []
    items = []
    for i, r in enumerate((own or [])[:5]):
        try:
            mid = int(r[1])
            skin = int(r[7]) or int((_MON_STATIC.get(mid) or (0, 0))[1] or 0)
            skills = [0] * 6
            try:
                b = get_monster_base(str(r[0]))       # (mid, skin, num, skills[6])
                if b and len(b) > 3 and b[3]:
                    skills = [int(x or 0) for x in (list(b[3]) + [0] * 6)[:6]]
            except Exception:
                pass
            item = [9000 + i, mid, int(r[2]), int(r[3]), int(r[4]), int(r[5]),
                    int(r[6]), skin, i + 1, 1, int(r[10])]
            item += skills
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
        item = [9000 + i, mid, lv, int(st[0] * f), int(st[1] * f), int(st[2] * f),
                max(1, int(st[3] / 100)), skin, i + 1, 1,
                int((st[0] + st[1] * 20) * f / 10)]
        item += [int(s or 0) for s in (list(skills) + [0] * 6)[:6]]
        items.append(item)
    return items

def _opponent(num, my_rank, my_points):
    # num: 1强敌 2上手 3下手; 名次/积分相对玩家
    if num == 1:
        pts = my_points + random.randint(400, 1200)
        rank = max(1, my_rank - random.randint(1, 6))
    elif num == 2:
        pts = my_points + random.randint(50, 200)
        rank = max(1, my_rank - 1)
    else:
        pts = max(0, my_points - random.randint(50, 200))
        rank = my_rank + random.randint(1, 10)
    name = random.choice(_NAMES) + str(random.randint(1, 999)).zfill(3)
    dsg = _dsg(pts)
    wp, lp = _dsg_bonus(dsg)
    return [num, name, rank, 0, 0, dsg, pts, wp, _bot_team(pts), _bot_team(pts), random.randint(100000, 999999), 9001]

def _qual_world_accounts():
    """多账号下返回**全部账号**的排位取数信息（按积分降序）；单档/未启用 -> None。

    ★2026-10-02 (D4=B): 1082 的他人行修前是 `_rank_list` **以自己为核心编的**
    （`pts = my_points + (my_rank-rk)*random(...)`、`name = random.choice(_NAMES)+str(1000+rk)`、
    `totalwincount = random(0,60)`）⇒ 多人世界里看不到彼此。现在改成**读各账号的真实
    `qualify.points` / `week_win` / 头像**。

    ⚠ `roleId`（第 11 位）现在是**真实 id**（`guild_world.role_id_of`，与
      `state.role_info_array()` 的 `fields[22]` 同口径）。修前是"我方发 1、别人发
      `100+owner_ns`"的权宜做法 —— 它在 1000 个账号以上会撞号（`u0002` 与 `u1002` 都是 102），
      撞号后客户端会把别人认成自己。见 `guild_world.role_id_of` 的说明。
    ⚠ `isSupport`/`supporNumt` 服务端没有这个量 ⇒ **0**（铁律：不许编造）。
    """
    try:
        from accounts import ENABLED, REGISTRY, EQ_STATE as _AS
    except Exception:
        return None
    if not (ENABLED and REGISTRY is not None):
        return None
    try:
        me = getattr(_AS.current(), 'id', None)
        accs = REGISTRY.all_accounts_full()
    except Exception:
        return None
    try:
        import guild_world as _gw
        _rid = _gw.role_id_of
    except Exception:
        _rid = lambda _a: 1
    out = []
    for acc in accs:
        d = acc.data if isinstance(acc.data, dict) else {}
        q = d.get('qualify') if isinstance(d.get('qualify'), dict) else {}
        head = d.get('head') if isinstance(d.get('head'), dict) else {}
        try:
            h = int(head.get('eq', 1001) or 1001)
        except Exception:
            h = 1001
        try:
            f = int(head.get('frame_eq', 1001) or 1001)
        except Exception:
            f = 1001
        try:
            pts = int(q.get('points', 0) or 0)
        except Exception:
            pts = 0
        try:
            win = int(q.get('week_win', 0) or 0)
        except Exception:
            win = 0
        out.append({'aid': acc.id, 'is_me': acc.id == me,
                    'role_id': _rid(acc.id),
                    'name': str(d.get('role_name') or ('未命名#' + acc.id)),
                    'pts': pts, 'win': win, 'head': h or 1001, 'frame': f or 1001})
    out.sort(key=lambda a: (-a['pts'], a['aid']))
    return out


def _rank_list(my_points, page, per_page=10):
    # 伪造榜: 以自己为核心生成 ±list
    # ★★2026-09-12 (v26 goal-round-6) 只报 1 页 (total = per_page):
    #   客户端 NetPVPQualifyingRank 是**顺序翻页**语义 —— 拿到 "cur/pages" 后会
    #   `if cur < pages then 再请求 cur+1`。原来 total=100 -> pages=10 -> 打开一次排行就发 10 次请求
    #   (真机实测 1082 连发 30 次)。私服就 10 行数据, 报 1 页即可。
    #
    # ★★2026-09-12 (v28) **字段顺序修正 —— 这就是 P0 闪退的真凶**:
    #   客户端 NetPVPQualifyingRank.lua:56-67 逐位读取的契约是:
    #       [1]rank [2]roleimg [3]rolerim [4]rolename [5]dangrading [6]point
    #       [7]totalwincount [8]serverid [9]isSupport [10]supporNumt [11]roleId
    #   而旧实现发的是 [rk, name, 0, 0, dsg, pts, ...] —— 把 **name 放在了 [2]**,
    #   也就是客户端眼里的"头像 id"位置。真机 [HDRID] 日志抓到的就是:
    #       headId = 夜行书生1001 (string)   <- 角色名被当头像 id 用
    #   -> UIHelper.lua:29 usGetTableData(T35_HeadImage, "夜行书生1001")
    #   -> 查表未命中 -> headData=nil -> L32 `headData.Image` 抛错
    #   -> initCell3 中断 -> METableView 拿不到 cell -> TableView::_setIndexForCell(NULL) -> SIGSEGV
    #   同族对照: handlers_rank.py 在 v26 已修过同类问题(_own_head/_own_frame 返回数字), 本文件当时漏了。
    head_id = 1001
    frame_id = 1001
    try:
        from handlers_rank import _own_head, _own_frame
        head_id = int(_own_head() or 1001)
        frame_id = int(_own_frame() or 1001)
    except Exception:
        pass
    total = per_page
    pages = 1
    cur = max(1, min(pages, int(page or 1)))
    start = (cur - 1) * per_page + 1
    # ★2026-10-02 (D4=B): 多账号下走**真·跨账号**（读各账号的真实积分/胜次/头像）。
    _wa = _qual_world_accounts()
    if _wa is not None:
        out = []
        my_rank = 1
        for idx, a in enumerate(_wa):
            rk = idx + 1
            if a['is_me']:
                my_rank = rk
            if not (start <= rk < start + per_page):
                continue
            # 11 字段，严格对齐 NetPVPQualifyingRank.lua:56-67
            out.append([rk, a['head'], a['frame'], a['name'], _dsg(a['pts']), a['pts'],
                        a['win'], 9001, 0, 0, a['role_id']])
        return out, my_rank, cur, pages, total
    out = []
    my_rank = 42 + random.randint(-5, 5)
    for i in range(start, start + per_page):
        rk = i
        if rk == my_rank:
            pts = my_points
            name = '测试'
        else:
            pts = max(0, my_points + (my_rank - rk) * (20 + random.randint(-10, 10)))
            name = random.choice(_NAMES) + str(1000 + rk)
        dsg = _dsg(pts)
        # 11 字段, 严格对齐 NetPVPQualifyingRank.lua:56-67
        out.append([rk, head_id, frame_id, name, dsg, pts,
                    random.randint(0, 60), 9001, 0, 0, 1000 + rk])
    return out, my_rank, cur, pages, total

# ============ handlers ============
def handle_qualifying_home(body_json=None):
    q, s0, s1 = _season()
    pts = int(q.get('points', 0))
    dsg = _dsg(pts)
    # ★★2026-11-06：`daytime` 发的是**剩余天数**，不是剩余秒。
    #   客户端 `VQualifyingHome:325` 把它直接 `LsetString` 到 `TextAtlas_Day`，
    #   后面是静态「天」字（PVP_New_Displacement_05.csb 只有这一个倒计时槽，
    #   没有时/分槽；同工程里 VNightMarketHome:406-410 的 day = floor(秒/86400)）。
    #   修前发 194220 秒 -> 面板显示「194220天」（真机 2026-11-06 23:03 截图可见
    #   「19」+静态「天」，后四位被「赛季奖励」文字盖住）。改成天数后显示「2天」。
    remain = max(0, s1 - int(time.time()))
    remain_day = remain // 86400
    boxes = []
    for (bid, cnt, gid) in _P11:
        st = int(q.get('boxes', {}).get(str(bid), 0))
        if st == 0 and int(q.get('week_win', 0)) >= cnt:
            st = 1
        boxes.append([bid, st])
    count = int(q.get('daily_n', DAILY_CHALLENGE))
    data = [pts, dsg, remain_day, int(q.get('reward', 0)), count,
            int(q.get('week_win', 0)), boxes, 0]
    return _json_out(data)

def handle_qualifying_reward(body_json=None):
    # 1079 赛季结算奖励
    q, s0, s1 = _season()
    if int(q.get('reward', 0)) != 1:
        return b'[578]'
    # 取本季结算组 (最近一季)
    sg = q.get('settle_gid', {})
    gid = '0'
    if sg:
        keys = sorted(sg.keys())
        gid = str(sg.get(keys[-1], '0'))
    if gid == '0':
        return b'[578]'
    groups, ok = _grant_group(gid)
    if not ok:
        return b'[578]'
    q['reward'] = 0
    save_state()
    return _json_out([_role_info(), _drop(groups)])

def handle_qualifying_box_reward(body_json):
    # 1080 周胜次箱 req {id}
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
    q, _, _ = _season()
    row = None
    for (bid2, cnt, gid) in _P11:
        if bid2 == bid:
            row = (bid2, cnt, gid)
            break
    if row is None:
        return b'[0,{}]'
    _, cnt, gid = row
    st = int(q.get('boxes', {}).get(str(bid), 0))
    if st >= 2:
        return b'[539]'
    if int(q.get('week_win', 0)) < cnt:
        return b'[538]'
    groups, ok = _grant_group(gid)
    if not ok:
        return b'[538]'
    q['boxes'][str(bid)] = 2
    save_state()
    return _json_out([_role_info(), _drop(groups)])

def handle_qualifying_report(body_json=None):
    q, _, _ = _season()
    rep = q.get('report', [])
    total_win = 0
    out = []
    for idx, r in enumerate(rep):
        # [result, score, addpoint] 存内部; 输出 [brid, fivhgState(1攻), score, victory(1胜), dangrading, name]
        _res, _pts, _add = int(r[0]), int(r[1]), int(r[2])
        if _res == 0:
            total_win += 1
        out.append([idx + 1, 1, _add, 1 if _res == 0 else 0, _dsg(_pts), '测试'])
    return _json_out([total_win, out])

def handle_qualifying_rank(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    page = int((req or {}).get('page') or 1)
    q, _, _ = _season()
    pts = int(q.get('points', 0))
    rows, my_rank, cur, pages, total = _rank_list(pts, page)
    # ★★v28 修正: "自己那一行"也必须按客户端的 8 字段顺序 [1]rank [2]roleimg [3]rolerim [4]rolename ...
    #   旧实现是 [my_rank, 0, 0, '测试', ...] —— 把 '测试' 放在了 [4](rolename) 但其实 [2]/[3] 给了 0,
    #   虽然这次没直接崩(0 会被兜底成 1001), 但与榜单行的字段语义不一致; 一并按契约补齐。
    from handlers_rank import _own_head, _own_frame
    # ★2026-10-02 (D4=B): 改过名的玩家显示真名；没改过的仍是 '测试'（与 `role_info_array` 同口径，
    #   单档模式下逐字节不变）。与 `handlers_rank._own_name` 同款。
    _myname = str(EQ_STATE.get('role_name') or '测试')
    me = [my_rank, int(_own_head() or 1001), int(_own_frame() or 1001), _myname,
          _dsg(pts), pts, int(q.get('week_win', 0)), 9001]
    return _json_out(me + [rows, '%d/%d' % (cur, pages)])

def handle_qualifying_get_array(body_json=None):
    """1083 PVPQUALIFYING_GETARRAY —— 读回两队布阵。

    ## 客户端契约 (逐槽反推, `NetPVPQualifyingGetArray:recvMessage`)
        msg[1] / msg[2] 各是**一个队** = `[teamscore, defenselist]`;
        `for i = 1, 2 do local teamlist = msg[i]` —— **msg[2] 是第二队, 不是"第二段"**。
        所以顶层必须是"每个队一个元素"的 list, 不是被 `_json_out` 包一层的 `[td, [..], [..]]`。

    ## 修前
    * `_json_out([td, [0, []], [0, []]])` 发的是**平铺三段**, 而客户端只把 msg[1]/msg[2]
      当两队读 ⇒ 第三段永远读不到, 且 msg[1] 里还多套了一层 `[td]`。
    * `td` 只装得下 2 个元素 (`[score, defense1]`), 而客户端读的是 `teamlist[1]`/`teamlist[2]`
      ⇒ `defenselist` **永远是 nil**, 客户端靠 `or {}` 兜住 ⇒ 队伍面板恒空。
    * 另一处槽位错: **队 1 = 防守**（`saveTeamOne`）、**队 2 = 出战**（`saveAndExit`）;
      修前 `_own_team()` 的注释把"上阵前5"当队 1 发。

    ## 取数顺序
    1084 存过的**按存的发**（这才是 1084 的意义）; 没存过才回落到"按当前怪物算"的派生阵容
    （等价于修前行为, 所以既有存档的玩家看到的不会突然变空）。
    """
    from handlers_achievement import set_ach
    store = _saved_arrays()
    teams = []
    scores = []
    for tn in (1, 2):        # 1=防守(saveTeamOne) / 2=出战(saveAndExit)
        saved = store.get(str(tn)) or {}
        rows = saved.get('list') or []
        if rows:
            teams.append([int(saved.get('score') or 0), rows])
            scores.append(int(saved.get('score') or 0))
        else:
            # 没存过: 回落到派生阵容 (修前行为 —— 既有存档的玩家看到的不会突然变空)
            _sc, items = _own_team()
            teams.append([_sc, items])
            scores.append(_sc)
    # ★2026-10-02 (D6=C1): T32 Type=809「排位赛布阵的妖力值」= 1083 下发的 `td[0]`。
    #   用**防守队**（队 1）的妖力值 —— 那才是"我布在榜上的阵"; 峰值语义, 换阵容下降不会回退。
    #   ⚠ 下面这一行**逐字**是 `_ach_typeset_test.py` G6 的写入点守卫（"恰好 1 处"）: 本轮把它
    #     改写成 `set_ach(809, 0, scores[0])`，那套护栏**当场判红** —— 判红是对的, 它的存在
    #     就是为了防止"写入点被删掉而用例仍绿"。所以这里保持原样的局部名 `score`。
    score = scores[0]
    set_ach(809, 0, score)
    return _json_out(teams)

def _saved_arrays():
    """已保存的出战/防守阵容容器。`{'1': {'score':int,'list':[[17字段]..]}, '2': {...}}`。"""
    q, _, _ = _season()
    return q.setdefault('defense', {})


def _save_array_row(raw):
    """把客户端发来的一个布阵条目规范化成**客户端解析器要的 17 槽**。

    客户端 `NetPVPQualifyingSaveArray:sendMessage` 发的每条是
    `{ucode,staticid,level,addhp,addatk,adddef,addcri,skin,pox,poy,score,skill1..skill6}`;
    回读侧 `NetPVPQualifyingGetArray:recvMessage -> refresh_defenselist` 按
    `info[1]..info[17]` 取 —— **同一套槽位**。`ucode == UCODE_DEONE` 会被客户端丢弃,
    所以这里也丢掉, 免得白占"最多 5 个"的名额。
    """
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    try:
        uc = int(raw[0])
    except Exception:
        return None
    if uc <= 0:                       # 空位/占位条目 (客户端 UCODE_DEONE) 直接丢
        return None
    row = []
    for i in range(17):
        v = raw[i] if i < len(raw) else 0
        try:
            row.append(int(v))
        except Exception:
            # ucode 之外的非数字一律归 0 —— 绝不把 None/字符串塞进整型槽
            row.append(0)
    row[0] = uc
    return row


def handle_qualifying_save_array(body_json):
    """1084 PVPQUALIFYING_SAVEARRAY —— 保存排位赛布阵 (teamnum=1 防守 / 2 出战)。

    ## 修前的两个缺陷（都有实测出处）
    1. **形状错**: 恒回 `[1,{}]`。而工程铁律 LAW-A 要求"成功响应里的 msg 必须能按
       `msg[N]` 索引"（`_contract_ironlaws_test.py` 第 134 行的判据就是"成功但 msg=object"= FAIL）。
       客户端这条路径只回调不读 msg, 所以**今天不会崩**, 但它是一条会静默违反铁律的例外 ——
       下一次谁照着它抄就会抄出真崩。改为 `[1,[]]`（与 `handle_point_request` 同款处置）。
    2. **数据丢**: 客户端 `WarPVPQualifyingDefenseUI:saveAndExit()` 把整支队伍的
       `defense` 数组发上来, 服务端一个字节都不记 —— 玩家摆好的阵容在服务端不存在。
       这本身还不算最坏（回读走 1083, 现状是从当前怪物算出来的等价阵容）;
       坏的是**它假装成功了**: 客户端 `m_callFunc()` 照跑, 界面切回主城, 玩家以为存上了。
        ⚠ 补充精确口径: `saveTeamOne` 的回调**确实会读** 1083（`_callback` 里的 `_loadlist`
       是从 1083 那份 `m_teamLists[2].defenselist` 来的）; 而 `saveAndExit`（队 2）只
       `enterScene` 不读。⇒ `[1,[]]` 对 `saveTeamOne` 这条回读链同样是必需的形状。

    现在的行为: 真保存, 并按 `NetPVPQualifyingGetArray` 的槽位形状回读 (见
    `handle_qualifying_get_array`)。**单档逐字节兼容不适用于本接口** —— 它在修前恒回
    `[1,{}]`, 没有任何既有测试把那个形状钉住; 而 `[1,[]]` 才是铁律要求的那一个。
    """
    req = {}
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        teamnum = int(req.get('teamnum') or 0)
    except Exception:
        teamnum = 0
    if teamnum not in (1, 2):
        return b'[0,{}]'
    rows = []
    for raw in (req.get('defense') or []):
        r = _save_array_row(raw)
        if r is not None:
            rows.append(r)
        if len(rows) >= 5:            # 客户端 `refresh_defenselist` 也只在 index<6 时收
            break
    if not rows:
        return b'[0,{}]'              # 空阵 = 非法 (客户端自己也会把空阵转成 nil/[])
    store = _saved_arrays()
    store[str(teamnum)] = {'score': sum(r[10] for r in rows), 'list': rows}
    save_state()
    return b'[1,[]]'

def handle_qualifying_match(body_json=None):
    q, _, _ = _season()
    pts = int(q.get('points', 0))
    rank = 42 + random.randint(-3, 3)
    rows = [_opponent(1, rank, pts), _opponent(2, rank, pts), _opponent(3, rank, pts)]
    return _json_out([rank, rows])

def handle_qualifying_refresh_match(body_json=None):
    q, _, _ = _season()
    pts = int(q.get('points', 0))
    rank = 42 + random.randint(-3, 3)
    rows = [_opponent(1, rank, pts), _opponent(2, rank, pts), _opponent(3, rank, pts)]
    return _json_out([_role_info(), rank, rows])

def handle_qualifying_check_array(body_json=None):
    return _json_out([_role_info()])

def _battle_data(num, pts):
    name = random.choice(_NAMES) + str(random.randint(1, 999)).zfill(3)
    rank = 43 + random.randint(-2, 2)
    dsg = _dsg(pts)
    wp, _ = _dsg_bonus(dsg)
    return [num, name, rank, 0, 0, dsg, pts, wp, _bot_team(pts), _bot_team(pts), random.randint(100000, 999999), 9001]

def handle_qualifying_start(body_json):
    # 1091 START: 开始挑战 (扣1今日次数) -> [1,[roleInfo, battleData]]
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        num = int(req.get('num') or 0)
    except Exception:
        num = 0
    if num not in (1, 2, 3):
        num = 1
    q, _, _ = _season()
    if int(q.get('daily_n', 0)) <= 0:
        return b'[1000]'
    q['daily_n'] = int(q.get('daily_n', 0)) - 1
    pts = int(q.get('points', 0))
    bd = _battle_data(num, pts)
    # ★★2026-10-17（任务链收口）：CT=44「成功参与妖神排位」的事件源 = 1091 开战。
    #   A07 10212/10310/10511 此前永远 0 进度。口径：**开战一次算一次参与**（扣了今日次数
    #   才算，次数不足在上面已 return [1000]）；胜负不计 —— 表文案是"参与"不是"获胜"。
    try:
        from handlers_task import bump_task as _bt, CT_QUALIFY as _CT_Q
        _bt(_CT_Q, 1)
    except Exception as _e:
        print('[QUALIFY] !! bump CT=44 失败(不影响开战): %r' % (_e,), flush=True)
    # ★2026-10-11（A8 排位赛重连）：这次"开打"要被记住 —— 1089 就是靠它判 stage=1。
    #   存的形状与 1085 的对手行**完全同形**（客户端两个 net 读的是同一套 12 槽）。
    q['neting'] = bd
    q['neting_at'] = int(time.time())
    q['neting_ownrank'] = 43 + random.randint(-2, 2)
    # ★★2026-10-16（任务书 D③）：`1088` 必须由 `1091` 开战 —— 这里就是"开战"。
    #   会话 target 用**对手位号 num**（1..3），客户端会在 1088 里原样回送 `msg["num"]`
    #   （`NetPVPQualifyingResult.lua:29`），所以两者天然配对；结算即消费，一局只认一次。
    try:
        from battle_session import open_session as _bs_open
        _bs_open('rank', num)
    except Exception as _e:
        print('[BS] !! 开排位赛会话失败(不影响开战): %r' % (_e,), flush=True)
    save_state()
    return _json_out([_role_info(), bd])

def handle_qualifying_result(body_json):
    # 1088 RESULT req {result(0胜/1负), num} -> [1,[points, addpoint, dangrading, tag, state]]
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        iswin = int(req.get('result') or 0) == 0
    except Exception:
        iswin = False
    try:
        num = int(req.get('num') or 0)
    except (TypeError, ValueError):
        num = 0
    q, _, _ = _season()
    # ★★2026-10-16（任务书 D③）：**一次性结算**。修前这条只信请求体：同一条 1088
    #   连发两次就再加一次积分、再记一次挑战次数、再推一次成就与日报。
    #   现在必须凭 1091 开出来的那一局（target = 对手位号 num）才能结算，结算即消费。
    #   ⚠ 被拒时的应答**不回错误码**，而是 `state=0`（客户端 `msg[5]` 的原义就是
    #     "是否是正常结算（1：是 0：否）"，见 `NetPVPQualifyingResult.lua:52`）——
    #     这样重放包既不会弹"结算失败"吓玩家，也不会改任何状态。
    _ok, _reason = False, 'error'
    try:
        from battle_session import settle as _bs_settle
        # ★★2026-11-05（`_ug_full_chain_test.py` 全链取证）：**票号必须给 None**。
        #   修前是 `'%s|%s' % (num, req.get('result'))` —— 而 1088 的请求体**只有**
        #   `{result, num}` 两个字段，所以票号对每一场排位赛都是同一个值
        #   （num 1..3 × 胜/负 = 最多 6 个）。`battle_session` 的票号表 TTL 是 1800s
        #   ⇒ **真实玩家每 30 分钟最多结算 6 局排位赛**，第 7 局开始一律 `state=0`
        #   （积分/成就/CT=44/CT=80 全都不动）。实测：连打 400 局只结算成功 1 局、积分停在 10。
        #   ⇒ 一次性判据**本来就够**：1091 开一局、1088 结算即消费（`consume=True`），
        #     同一场战斗重发同一条包 -> 会话已被消费 -> `no_open` -> 零状态变化。
        #     票号在这里不但没增加防护，反而把**合法的下一场**判成重放。
        _ok, _reason = _bs_settle('rank', num, None)
    except Exception as _e:
        print('[BS] !! 排位赛结算状态机异常(按未结算处理): %r' % (_e,), flush=True)
    if not _ok and _reason == 'no_open' and isinstance(q.get('neting'), dict):
        # ★2026-11-06 真机：服务端重启/掉线后客户端仍会把已打完的 1088 发上来，
        #   而 battle_session 是**内存态**，重启后必然 `no_open` -> 玩家白打一场。
        #   兜底：只要 1091 落过盘的开局记录（q['neting']）还在，就允许**这次**结算，
        #   并立即消费掉 neting（同一场重发会因为 neting 已清而继续被拒）。
        _ok, _reason = True, 'fallback_neting'
        print('[BS] 1088 会话不在(可能重启)，按 q.neting 兜底结算 num=%s' % (num,), flush=True)
    if _ok:
        try:
            q.pop('neting', None)
            q.pop('neting_at', None)
            q.pop('neting_ownrank', None)
        except Exception:
            pass
    if not _ok:
        print('[BS] 1088 结算被拒: reason=%s num=%s -> state=0 且零状态变化'
              % (_reason, num), flush=True)
        pts0 = int(q.get('points', 0))
        # ★2026-10-16（逐槽对客户端）：客户端的应答是**六槽**
        #   （`NetPVPQualifyingResult.lua:47-54`：points/addpoint/dangrading/tag/state/exDropItems）。
        #   排位赛**不发物品**，所以第 6 槽给空 dropInfo —— 槽数固定，客户端升级后
        #   若改成无条件下发 `refreshDropInfo(msg[6])` 也不会因为 nil 出问题。
        return _json_out([pts0, 0, _dsg(pts0), 0, 0, _drop([])])
    old_pts = int(q.get('points', 0))
    old_dsg = _dsg(old_pts)
    wp, lp = _dsg_bonus(old_dsg)
    add = wp if iswin else -max(1, lp)
    pts = max(0, old_pts + add)
    q['points'] = pts
    if iswin:
        q['week_win'] = int(q.get('week_win', 0)) + 1
    new_dsg = _dsg(pts)
    # ★2026-10-02 (D6=C1): 两条成就进度上报(都在**结算之后**, 胜负都算)。
    #   803「妖神排位达到段位」: 7 个 Value1 = P10_PVPLevel.ID(1003/1006/1010/1014/1019/1024/1029),
    #     与 `_dsg()` 的取值域**逐一相同** ⇒ 用 `set_ach_reached` 把 `<= new_dsg` 的档全部推平,
    #     否则一次连跳多档时中间档**永远拿不到**。必须峰值: 跨赛季 `q['points'] = 0`(上方 :146)。
    #   810「【周】挑战次数」: 每次结算 +1; 周一被 `_season()` 重置 ⇒ 也必须峰值。
    q['week_n'] = int(q.get('week_n', 0)) + 1
    from handlers_achievement import set_ach, set_ach_reached
    set_ach_reached(803, new_dsg)
    set_ach(810, 0, q['week_n'])
    # ★★2026-10-22（审查 B7 / UG 141013「妖神排位达到铜符道士1星」Value=1004）：
    #   UG 的 CT=80 要的是"**曾达到**"的段位 —— 输局会掉档、跨季 `points` 清零，
    #   所以只能在**真实结算之后**把峰值记下来（`ug['rank_peak']`），不能裸读当季积分。
    #   `new_dsg` 是服务端算出来的段位，与 803 成就同一个值；两本账各记各的（803 是成就，
    #   UG 是永久主线峰值），互不替代。
    try:
        from handlers_ugtask import bump_ug_rank_peak as _bump_rank
        _bump_rank(new_dsg)
    except Exception as _e80:
        print('[UG80] !! 段位峰值上报失败(不影响结算): %r' % (_e80,), flush=True)
    tag = 1 if new_dsg > old_dsg else (-1 if new_dsg < old_dsg else 0)
    q.setdefault('report', []).append([0 if iswin else 1, pts, add])
    if len(q['report']) > 50:
        q['report'] = q['report'][-50:]
    # ★2026-10-11（A8）：这一局打完 -> 清掉重连态（否则 1089 会一直判"我正在战斗中"）
    q.pop('neting', None)
    q.pop('neting_at', None)
    q.pop('neting_ownrank', None)
    save_state()
    # ★2026-10-16（逐槽对客户端）：`NetPVPQualifyingResult.lua:47-54` 读六槽，
    #   第 6 槽 `exDropItems` 本协议**恒为空**（排位赛不发物品），但槽位要保持 6 个。
    return _json_out([pts, add, new_dsg, tag, 1, _drop([])])

def handle_qualifying_neting(body_json=None):
    """1089 NETING（**断线重连**）-> `[stage, battleData, ownrank]`。

    ★★修前恒回 `stage=0` ⇒ 客户端 `MQualifyingHome:getRequestNeting()` 恒假 ⇒
      `VQualifyingHome.lua:394` 的**整条重连分支不可达**：玩家在排位战斗里掉线后重进，
      界面上"继续战斗"永远不会出现（只能白等/放弃），HANDOFF 的 A1 记的就是这一条。
      ⚠ "单做 1090 没意义"也是这个原因 —— 1090（`arrange_neting`）是重连**之后**的落地动作。

    契约（`NetPVPQualifyingNeting:recvMessage` 逐槽）：`stage` **1=在游戏中/0=否**；
    `msg[2]` 在 `stage==1` 时才被解（12 槽：num/rolename/rank/img/rim/dangrading/point/
    winpoint/team1list/team2list/roleid/serverid，两个 teamlist 每行 **17 槽**）；
    `msg[3]` = ownrank。**`stage=1` 却给 `msg[2]` 不是表 ⇒ 客户端在 `msg[2][1]` 当场崩。**

    ★ 真值落点：1091 START 时把那一份 `battle_data`（与 1085 的对手行**同形**）存进
      `qualify['neting']`，1088 RESULT 时清掉。⚠ **必须带时间窗**（`NETING_TTL`）：
      玩家打完不结算就退出（或客户端根本没回 1088）时，不设上限会让他**每次登录都被判
      "正在战斗中"**，比重连不可达更糟。
    """
    q, _, _ = _season()
    rank = 42 + random.randint(-3, 3)
    try:
        data = q.get('neting')
        at = int(q.get('neting_at') or 0)
    except Exception:
        data, at = None, 0
    fresh = at > 0 and (int(time.time()) - at) <= NETING_TTL
    if fresh and isinstance(data, list) and len(data) == 12:
        try:
            own = int(q.get('neting_ownrank') or rank)
        except Exception:
            own = rank
        return _json_out([1, data, own])
    if data is not None and not fresh:
        # 过期就顺手清掉（**不落盘**：这是一次读，清内存即可；下次写路径自然覆盖）
        q.pop('neting', None)
        q.pop('neting_at', None)
        q.pop('neting_ownrank', None)
    safe = [0, '', 0, 0, 0, _dsg(int(q.get('points', 0))), int(q.get('points', 0)), 0, [], [], 0, 9001]
    return _json_out([0, safe, rank])

def handle_qualifying_arrange_neting(body_json):
    # 1090 ARRANGE_NETING: 重连**落地**（客户端 `NetPVPQualifyingArrageNeting:recvMessage`
    #   只判 statecode + 回调）-> 必须是**纯 ack** `[1,[]]`。
    #
    # ★★这里**刻意不清** `neting`：我第一版"顺手收掉重连态"，被 `_coverage_gap_test.py` 的
    #   G2 当场判红（1090 是它冻结的 ack-only 三条之一），复核后发现**判红是对的**：
    #   1090 的语义是"我已重连进去"，**这一局还在打**。在这里清掉的话，玩家重连之后再掉一次
    #   就再也连不回来了（1089 会回 stage=0）。⇒ 收敛只有两条正确的路：
    #     ① **1088 RESULT**（这一局真的结束了）；② **`NETING_TTL` 过期**（没结算就退出的兜底）。
    return b'[1,[]]'
