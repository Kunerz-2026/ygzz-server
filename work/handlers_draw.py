# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 抽卡/召唤系统处理器 (CARD_TIME 8001 / DRAW_CARD 8002)
# 协议 (NetCardTime/NetCard, lua_dump\35/34.lua):
#   8001 请求 {} -> 响应 [1,[costdemonsoul,costticket,remaintime,totalticket,goblineggs,
#        totaldemonsoul,luckvalue,remaindaycount,cardpool,summonFiveEnsure]]
#   8002 请求 {cardtype,id,bNextPage} -> 响应 [1,[cardtype,totalticket,goblineggs,totaldemonsoul,
#        luckvalue,remaintime,remaindaycount,cardlist,talentList,count,bNextPage]]
#   cardtype: 1=每日免费一抽 2=人脉(妖魂)一抽 3=唤妖符一抽 4=唤妖符五抽
#   id=卡池ID: 1001=每日池(组1) 1002=基本池(组2); 五连保底=组999
import json, os, random, time
from state import (EQ_STATE, save_state, get_monster_base, all_monster_ucodes,
                   get_monster_lv, get_monster_exp, get_monster_grow, get_monster_add,
                   static_tallents)
from state import alloc_monster_ucode   # ★2026-10-02 (审计 §9 #3): 原子取号
from config_data import _mon
from handlers_equip import _mon_with_state
from handlers_task import bump_task, CT_DRAW

from state import table_dir as _table_dir   # ★2026-10-02 (审计 §1.6): 表根不再写死盘符
DATA = _table_dir()

def _load_json(name):
    # ★v60 闭环 v58 §10 缺口 #3: 改走 state.load_table —— 按「尾字段 u32 长度」精确剥离,
    #   不再用 c[:c.rfind(']')+1]（长度字段含 0x5D 时会切错 -> 调用方 except -> 静默空表,
    #   实测 L98_AIPriority / T32_Achievement）。护栏: work/_v60_legacy_tail_guard_test.py
    #   ⚠ 必须**逐行复制**: load_table 的结果是**带缓存的同一个对象**, 而旧实现每次返回全新对象。
    #     调用方若原地改行(排序/填字段)会污染全局缓存 -> 这里保持"每次都是新对象"的原语义。
    from state import load_table as _lt
    return [list(_r) for _r in _lt(name, DATA)]

# ---------- 怪物静态数据 (L91_Status.json): mid -> (quality, skin, skills[6], tallents[2]) ----------
#   tallents 注意: CN 1.31 L91 的 Tallent1/2 是渠道阉割全 0; 经 state.static_tallents 回退
#   YGTW 1.25 逐怪分配 (2026-09-07 破案, 见 state.py) -> 8002 响应 msg[9] talentList 携带真实特性
_MON_STATIC = {}
_l91 = _load_json('L91_Status.json')
_ix = {h: k for k, h in enumerate(_l91[0])}
for _r in _l91[1:]:
    try:
        _mid = int(_r[0])
    except Exception:
        continue
    _skills = []
    for _n in range(1, 7):
        try:
            _skills.append(int(_r[_ix['Skill%d' % _n]] or 0))
        except Exception:
            _skills.append(0)
    try:
        _q = int(_r[_ix['Quality']] or 0)
        _sk = int(_r[_ix['Skin1']] or 0)
        _t1, _t2 = static_tallents(_mid)     # CN 全 0 -> YGTW 回退 (真实特性)
    except Exception:
        continue
    _MON_STATIC[_mid] = (_q, _sk, _skills, [_t1, _t2])

# ---------- 抽卡池 (D01-DrawCard.json): (Type,CardGroupID) -> [(chance,[hero ids])] ----------
#   Type: 1=每日 2=一抽 3=五抽; 组: 1=每日池 2=基本池 999=五连保底
_POOLS = {}
_ENSURE_HEROES = []
_d01 = _load_json('D01-DrawCard.json')
_ix2 = {h: k for k, h in enumerate(_d01[0])}
for _r in _d01[1:]:
    try:
        _t, _g = int(_r[_ix2['Type']]), int(_r[_ix2['CardGroupID']])
        _heroes = [int(x) for x in str(_r[_ix2['HeroID']]).split(',') if x.strip().isdigit()]
        _chance = int(_r[_ix2['Chance']] or 0)
    except Exception:
        continue
    if not _heroes or _chance <= 0:
        continue
    if _g == 999 and _t == 3:
        _ENSURE_HEROES = _heroes           # 五连保底: 强制一只该池SR
    else:
        _POOLS.setdefault((_t, _g), []).append((_chance, _heroes))

_POOL_GROUP_FALLBACK = {1001: 1, 1002: 2}  # 只在 D04_CardPool 读不出来时兜底
_TYPE_MAP   = {1: 1, 2: 2, 3: 2, 4: 3}     # cardtype -> D01 Type


def _pool_group_map():
    """卡池ID -> CardGroupID，**从 D04_CardPool 表读**（v60 由写死改成表驱动）。

    ★为什么改（v59 §3.3/缺口 #7 的复查结论）:
      v59 文档说"19 个新卡池（卡组 33-51），服务端 _POOL_GROUP 写死 2 个池"。
      但 v60 实测: **D04_CardPool（卡池的权威表）在服务端和新包里都只有 2 行**
        （1001->组1, 1002->组2），新包并没有多出卡池行。
      D01-DrawCard 里确实新增了 CardGroupID 33~51，但**没有任何表把"某个卡池"映射到那些组**，
      客户端也不按 ID 查 D04 来建页面（CallCard:initData 完全用 8001 下发的 entry）。
      ⇒ 凭空造 19 个池会违反铁律 #22（不编造 id），所以**不造**；
        但把"只认 1001/1002"这个写死去掉是纯收益: 以后 D04_CardPool 真的加行, 卡池自动出现。
    """
    out = {}
    try:
        from state import load_table
        rows = load_table('D04_CardPool.json')
        h = {c: i for i, c in enumerate(rows[0])}
        for r in rows[1:]:
            try:
                pid = int(r[h['ID']])
                gid = int(r[h['CardGroupID']])
            except Exception:
                continue
            if pid and gid:
                out[pid] = gid
    except Exception as e:
        print('[CARD_POOL] !! D04_CardPool 读取失败: %r -> 退回内置两池' % (e,))
        out = {}
    if not out:
        out = dict(_POOL_GROUP_FALLBACK)
    # ★2026-11-07（活动卡池轮换）：私服合成池（默认 1003）不在 D04 里，由
    #   `cardpool_rotation` 按周给出当期 CardGroupID。关闭轮换时它返回 {}，行为不变。
    #   ⚠ 只在**内存里**合并，绝不改 `data/tables`。
    try:
        from cardpool_rotation import pool_group_map as _rot_map
        out.update(_rot_map() or {})
    except Exception as e:
        print('[CARD_POOL] !! 活动池轮换数据不可用(不影响基础池): %r' % (e,))
    return out

# ---------- 8001 cardpool（v59 实装） ----------
#   背景: 1.31 私服一直发 cardpool=[]（"本版无活动池"），后果是
#     ① 召唤界面「卡池BOSS」按钮永不可见（CallCard.lua:419 `tonumber(data.bossOpen) == 1` 才显示）
#     ② VMainButton:shendMassageCardBoss 拿不到 cardBossID -> 主城卡池入口也不可用
#   契约（客户端 CallCard:initData 逐字段）:
#     entry[1]=carID(卡池id, 必须是 D04_CardPool 真实行) [2]=剩余秒 [3]bg [4]name [5]remark
#     entry[6]=upNum(BOSS 活动额外权重, -1=无活动)     [7]bossOpen(1=开)   [8]pCardId(概率标识)
#   数据源: handlers_cardboss.cardpool_entry(池id) —— 只回真实池，绝不编造 id（铁律 #22）
#
# ★★2026-11-07（活动卡池轮换）：活动页列表**只发轮换池一条**，不再把 1001/1002 当活动页重复发。
#   · 客户端基础页固定用 `cd.basepoolid=1002`、每日按钮固定用 `cd.daypoolid=1001`
#     （`CardConfig.lua:19-20`），所以 8001 的 cardpool 列表本来就只承载"活动页"；
#     修前把 1001/1002 两条都塞进去 → 召唤页出现 3 页（基础页 + 两个重复活动页，且无图无名字）。
#   · 「卡池BOSS」按钮只存在于活动页（`CardHome.lua:359-396` 在 `_index ~= 1` 的分支），
#     所以活动页**必须保留一条**——轮换关闭时退回旧行为（1001/1002 两条），绝不发空列表。
#   · 轮换池是**私服合成 id**（`cardpool_rotation.POOL_ID`），底图/当期卡组都由它给出。
def _cardpool():
    try:
        from handlers_cardboss import cardpool_entry
    except Exception as e:
        print('[CARD_POOL] !! 导入 cardpool_entry 失败: %r' % (e,))
        cardpool_entry = None
    out = []
    try:
        from cardpool_rotation import current as _rot_current, POOL_ID as _ROT_PID
        r = _rot_current()
    except Exception as ex:
        r = None
        print('[CARD_POOL] !! 活动池轮换不可用(退回旧两页): %r' % (ex,))
    if r and cardpool_entry is not None:
        pid = int(r.get('pool_id') or _ROT_PID)
        gid = int(r.get('group') or 0)
        banner = r.get('banner') or 0
        remain = max(0, int(r.get('end') or 0) - int(time.time()))
        try:
            e = cardpool_entry(pid, group_override=gid, bg=banner)
        except Exception as ex:
            e = None
            print('[CARD_POOL] !! 轮换池=%s 组装失败: %r' % (pid, ex))
        if not e:
            e = [pid, remain, banner, 0, 0, -1, 0, gid]
        else:
            e[1] = remain
        out.append(e)
        return out
    # ---- 轮换关闭 / 不可用：保持 v60 行为（活动页 = D04 的真实池）----
    pg = _pool_group_map()
    for pid in sorted(pg):
        gid = pg[pid]
        try:
            e = cardpool_entry(pid) if cardpool_entry else None
        except Exception as ex:
            print('[CARD_POOL] !! pool=%s 组装失败: %r' % (pid, ex))
            e = None
        if e:
            out.append(e)
        else:
            # 没有 BOSS 活动的池也要出现在列表里（否则召唤页少一页）——upNum=-1 表示"无活动"
            out.append([pid, 0, 0, 0, 0, -1, 0, gid])
    return out

COST_DEMONSOUL = 100   # 人脉一抽消耗妖魂 (client const_Contacts 默认200, CARD_TIME.costdemonsoul 覆盖)
COST_TICKET    = 1     # 唤妖符一抽消耗券; 五抽=5
# ★★2026-11-14（用户要求放宽背包）：官方客户端 `SQGamer:getMaxMonster()` 写死 **21**，但这个
#   函数在装机包里**只有一个调用点**（`app/team/TableTest.lua:70`，一个调试面板）；
#   正式 UI（`team/RoleListVertical.lua`）是遍历 `monster_getTotalItem()` 的，不按 21 截断。
#   真正决定"能装多少"的是 1032 的 `"当前页/总页"`：客户端 `NetMonsterBag.lua:69-80` 拿到
#   `currentPage < totalPages` 会**自动请求下一页**，`== totalPages` 才 setFinishLoad —— 页大小
#   完全由服务端给，客户端没有写死每页几只。
#   ⇒ 扩容只改下面两个常量，**不用动客户端、不用重打 APK**：
#     · BAG_PAGE = 每页只数。★★2026-11-14 真机实测：**每多一页，客户端登录要多等 5 秒**
#       （21/页 + 200 只 = 10 页 → 卡在"游戏加载中"约 50 秒；日志是 page=1..10 每 5 秒一页）。
#       ⇒ 页大小直接取上限 200：满包也只有 **1 页**（1032 单帧 ~20KB，实测数量=200 正常）。
#       分页代码保留 —— 以后要 >200 时把 BAG_PAGE 调小即可，但要想清楚"5 秒/页"的代价。
#     · MAX_BAG  = 上限。2026-11-14 两次调整：21 → 42 → **200**。
BAG_PAGE       = 200   # 1032 每页只数：取满上限 = 正常只会有 1 页（见上，5 秒/页的实测）
MAX_BAG        = 200   # 怪物背包上限；抽卡/礼包/邮件/剧情四条造怪路径共用
_SSR_BOOST     = 50    # SSR档权重放大倍数 (私服娱乐: 真实权重10/8000≈0.1%太低, 放大后约6%)
_LUCK_SSR_STEP = 20    # 幸运值对SSR档的加权步长 (eff = chance*(1 + luck*step/1000))

def _quality(mid):
    s = _MON_STATIC.get(mid)
    return s[0] if s else 0

def _roll_one(draw_type, group, luck):
    # 按 Chance 加权抽档位, 再从档位 HeroID 均匀取一只怪
    tiers = _POOLS.get((draw_type, group)) or _POOLS.get((2, 2))
    boosted = []
    for chance, heroes in tiers:
        c = chance
        if _quality(heroes[0]) >= 4:                       # SSR档: 基础放大 + 幸运加权
            c = c * _SSR_BOOST * (1000 + luck * _LUCK_SSR_STEP) // 1000
        boosted.append((c, heroes))
    total = sum(c for c, _ in boosted)
    roll = random.randrange(total) if total > 0 else 0
    for c, heroes in boosted:
        if roll < c:
            picked = random.choice(heroes)
            return picked if picked in _MON_STATIC else heroes[0]
        roll -= c
    return boosted[-1][1][0]

def handle_card_time():
    # 8001: 进入抽卡界面
    #   [3]remaintime [8]remaindaycount: 每日召唤的剩余CD秒/剩余免费次数 (客户端 CallCard:enterUI 门控倒计时)
    #   [9]cardpool: ★v59 起发**真实卡池**（含 BOSS 活动标记），不再是 []。见 _cardpool() 注释。
    dr = EQ_STATE['draw']
    _today = time.strftime('%Y-%m-%d')
    if dr.get('free_day') != _today:
        dr['free_day'] = _today
        dr['free_used'] = 0
    _rdc = 0 if int(dr.get('free_used', 0)) >= 1 else 1
    _rt = 0
    if _rdc == 0:
        try:
            import datetime as _dt
            _now = _dt.datetime.now()
            _mid = (_now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            _rt = max(1, int((_mid - _now).total_seconds()))
        except Exception:
            _rt = 86400
    data = [COST_DEMONSOUL, COST_TICKET, _rt, dr['ticket'], EQ_STATE['eggcoin'],
            EQ_STATE['soulcoin'], dr['luck'], _rdc, _cardpool(), dr['five_ensure']]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

def _mail_overflow_monsters(mids):
    """（兼容壳）背包放不下的妖怪 -> 邮件。实现已收口到 `handlers_bag.mail_overflow_monsters`。

    ★2026-10-16（B 复核 2）：造怪/邮件的唯一实现搬到 `handlers_bag`（那里还有 `grant_monsters`
      负责背包上限），本函数只做转发，避免"两处各写一份邮件格式"。
    """
    try:
        from handlers_bag import mail_overflow_monsters as _m
        return _m(mids, why='召唤时妖怪背包已满')
    except Exception as _e:
        print('[DRAW] !! 满包转邮件失败（这批妖怪没发出去）: %r' % (_e,), flush=True)
        return 0


def handle_draw_card(body_json):
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[80]'
    if not isinstance(req, dict):
        return b'[80]'
    try:
        cardtype = int(req.get('cardtype') or 0)
        poolid   = int(req.get('id') or 0)
    except Exception:
        return b'[80]'
    bnext = bool(req.get('bNextPage'))
    # ★v58 (2026-09-16): 8002 请求体留痕 —— 之前只记了响应, 出问题只能靠响应回显反推 cardtype。
    #   判据: cardtype=3(一抽) vs 4(五抽); id=卡池; bNextPage=是否第二页五连。
    #   排查"按了五连抽却只掉一只"这类问题时, 这一行直接给答案。
    #   纯 ASCII, 避免 GBK 控制台乱码; 用 try 包住, 绝不能因留痕影响业务。
    try:
        print('[DRAW8002] cardtype=%s pool=%s bNextPage=%s -> ticket_before=%s soul_before=%s'
              % (cardtype, poolid, bnext, EQ_STATE['draw'].get('ticket'), EQ_STATE.get('soulcoin')))
    except Exception:
        pass
    group = _pool_group_map().get(poolid)
    if group is None:
        return b'[96]'                                       # 卡池ID错误
    draw_type = _TYPE_MAP.get(cardtype)
    if draw_type is None:
        return b'[80]'
    n = 5 if cardtype == 4 else 1
    dr = EQ_STATE['draw']
    # ---- 每日召唤 (cardtype=1 TIME): 每日 1 次免费 (2026-09-08 修复: 旧实现无任何限制 -> 无限免费刷) ----
    if cardtype == 1:
        _today = time.strftime('%Y-%m-%d')
        if dr.get('free_day') == _today and int(dr.get('free_used', 0)) >= 1:
            return b'[83]'                       # 还在剩余CD时间中 (官方错误码; 客户端 NetCard m_errorCode[83])
        if dr.get('free_day') != _today:
            dr['free_day'] = _today
            dr['free_used'] = 0
    # ---- 扣费校验 (错误码: 81妖魂不足 82唤妖符不足) ----
    if cardtype == 2:
        if EQ_STATE['soulcoin'] < COST_DEMONSOUL:
            return b'[81]'
        EQ_STATE['soulcoin'] -= COST_DEMONSOUL
    elif cardtype in (3, 4):
        if dr['ticket'] < COST_TICKET * n:
            return b'[82]'
        dr['ticket'] -= COST_TICKET * n
    # ---- 抽怪 ----
    mids = [_roll_one(draw_type, group, dr['luck']) for _ in range(n)]
    got_ssr = any(_quality(m) >= 4 for m in mids)
    if n == 5 and not got_ssr:                               # 五连保底: 无SSR -> 末位换成保底SR
        cands = [h for h in _ENSURE_HEROES if h in _MON_STATIC]
        if cands:
            mids[-1] = random.choice(cands)
            got_ssr = False
            dr['five_ensure'] = 1
    # ---- 生成怪物进背包 (ucode 自增避撞) ----
    # ★★2026-10-16（B 复核）：**怪物背包上限 21**（`MAX_BAG` = 客户端 `SQGamer:getMaxMonster()`）。
    #   修前这里无条件把 n 只全塞进背包：超过 21 只以后客户端只按一页渲染 ⇒ **看不见也用不了**；
    #   而回包第 10 槽 `count` 又被算成"超出几只"，客户端 `CallCard.lua:677-681` 拿到
    #   `data.count > 0` 就弹"背包已经达到上限了，赶快去邮件里领取吧!" —— **提示去邮件，
    #   但一封邮件都没有**（那几只其实在背包里躺着看不见）。两边都不对。
    #   现在：放得下的进背包，放不下的**真的投递成邮件**（客户端提示与实际一致），
    #   `count` = 实际投递的只数。
    _room = max(0, MAX_BAG - len(all_monster_ucodes()))
    _fit, _over = mids[:_room], mids[_room:]
    count = 0
    card_parts, talents = [], []
    for mid in _fit:
        # ★2026-10-02 (审计 §9 高severity #3): 原子取号(旧: 读-改-写 -> 并发重号)
        uc = alloc_monster_ucode()
        q, skin, skills, tall = _MON_STATIC[mid]
        EQ_STATE['monsters'][uc] = [mid, skin, 100, list(skills)]
        EQ_STATE['monster_ueq'][uc] = ('0', '0', '0')
        EQ_STATE['monster_lv'][uc] = 1
        EQ_STATE['monster_exp'][uc] = 0
        # feat 槽(20-25): "id#lv#isUse" 按 L97 UnLockLevel 类别放置 (同槽 & 连接; 与 talentList 同源; 空槽='' -> _mon 输出数字0)
        from state import feat_slots_for
        feat_slots = feat_slots_for(tall)
        card_parts.append(_mon(int(uc), mid=mid, num=100, skin=skin, skills=list(skills),
                               uequip=('0', '0', '0'), feat=feat_slots))
        talents.append([uc, [t for t in tall if t]])
    if _over:
        count = _mail_overflow_monsters(_over)
    # ---- 幸运值: 抽到SSR清零, 否则累加 ----
    if got_ssr:
        dr['luck'] = 0
    else:
        dr['luck'] += n
    # ★2026-10-02 (D6=C1): T32 Type=1105「召唤UP值达到 N」(Title = 非酋之道入门…终结)。
    #   取证: `draw.luck` **就是**客户端那个被注释成 `--up值` 的控件显示的值
    #   (`CallCard.lua:272` LsetString(...,AtlasLabel_RateLevel, m_luckvalue) +
    #    `CardFiveResult.lua:30` 对该控件写 `--up值` + 本文件下发它作为响应第 5 个字段)。
    #   ⚠ **必须用 set_ach(峰值) 而不是当前值**: 上面那一支会把 luck **清零**,
    #     用当前值会让"已经达成的非酋之道"退回未达成, 而领奖时会**重新** ach_done 判定
    #     (`handlers_achievement.handle_achievement_get`) ⇒ 到手的条件变成领不到。
    from handlers_achievement import set_ach as _set_ach
    _set_ach(1105, 0, dr['luck'])
    if cardtype == 1:
        dr['free_used'] = int(dr.get('free_used', 0)) + 1     # 每日召唤免费 1 次
    # ★v60: 累计召唤次数 —— A01 活动组 103「召唤得奖励」的**真实计数源**。
    #   为什么必须在这里加: v59 文档说"103 只差接一行，_draw_count() 已写好"，但实测
    #   `_draw_count()` 读的是 draw['total'|'count'|'draw_count']，而本文件从不写这三个键
    #   (存档实测 draw = {ticket,luck,five_ensure,free_day,free_used}) ⇒ 只改映射仍然恒 0。
    #   口径: A01_1 g=103 的 Desc 是"召唤达到指定次数"(TargetType=14, 档位 10/20/50/100/200)，
    #   所以这里按**实际召唤出的妖怪数 n** 累加（五连记 5）。
    #   放在扣费/抽怪**成功之后**、save_state() **之前**，保证与落盘一致。
    #   ⚠ 这是 v60 定的口径（官方确切口径未证实），要改只改这一行。
    dr['total'] = int(dr.get('total', 0)) + n
    bump_task(CT_DRAW, 1)            # 每日任务: 召唤
    # ★2026-10-02 (成就进度写入方): T32 Type=1118「召唤次数达到N」-> 组 1004/级别2(妖怪召唤)
    #   累加量 n = **本次实际召唤出的妖怪数**(五连记 5), 与上一行 `dr['total']` 同一口径。
    from handlers_achievement import note_ach
    note_ach(1118, 0, n)
    save_state()
    # `count` 已在上面按"**真正投递成邮件**的只数"算好（>0 = 背包满，客户端据此弹提示）
    # [6]remaintime [7]remaindaycount: 每日召唤 -> 距午夜秒/剩余免费次数 (客户端 CallCard 倒计时门控 + NetDrawCard 解析)
    if cardtype == 1:
        try:
            import datetime as _dt
            _now = _dt.datetime.now()
            _mid = (_now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            _rt = max(1, int((_mid - _now).total_seconds()))
        except Exception:
            _rt = 86400
        _rdc = 0 if int(dr.get('free_used', 0)) >= 1 else 1
    else:
        _rt, _rdc = 0, 0
    # ★★2026-10-16（逐槽对客户端）：`NetCard.lua:53-64` 读到 **msg[15]**：
    #   msg[12]=newmonsterlist（新入怪物 ucode 列表） msg[13]=basefive
    #   msg[14]=fistten msg[15]=fistssr。修前只回 11 槽 ⇒ 四个槽恒 nil。
    #   已核过后果：`newmonsterlist` 有 `if not ... then return` 守卫、
    #   `basefive` 只被当布尔用 ⇒ **不会崩**；但"新入怪物高亮 / 首五连 / 首十连 / 首 SSR"
    #   四个特效永远不会出现。本服不追踪这四个标志（无源），
    #   按"无源一律 0"发空列表/零，但**槽位必须存在**（形状稳定）。
    resp = '[1,[%d,%d,%d,%d,%d,%d,%d,[%s],%s,%d,%s,[],0,0,0]]' % (
        cardtype, dr['ticket'], EQ_STATE['eggcoin'], EQ_STATE['soulcoin'], dr['luck'], _rt, _rdc,
        ','.join(card_parts), json.dumps(talents, separators=(',', ':')),
        count, 'true' if bnext else 'false')
    return resp.encode('utf-8')

# ---------- 动态背包: 怪物(1032) / 技能(1506) 含抽到的怪 ----------
def monster_bag_json(page=1):
    # ★2026-11-14: 分页下发（见上面 BAG_PAGE / MAX_BAG 的说明）。
    #   `msg[2]` 必须是字符串 "当前页/总页" —— 客户端 `NetMonsterBag.lua:39-46` 用 `string.find(msgy[2], "/")`
    #   解析；解析不出来就退成 {1,1}（等于假装只有一页，第 2 页永远不拉）。
    #   ⚠ 页大小必须 <= BAG_PAGE(21)；把 BAG_PAGE 改到 >= MAX_BAG 就退回单页 "1/1"（保留这条后路）。
    ucs = []
    for _uc in all_monster_ucodes():
        _b = get_monster_base(_uc)
        if _b is None:
            continue
        # ★★2026-11-14（P0 卡登录）：存档里若残留"L91_Status 里没有的怪"（如 500011 花末天奇，
        #   只有 L91_StatusRender 的展示行），客户端 `SDMonster:create(mid)` 返回 nil ⇒ 1032 解析
        #   抛错 ⇒ 登录卡 80%。这里**跳过**它们让玩家至少能进游戏；要真正救回那只怪，
        #   跑 `work/_fix_unknown_monsters.py`（默认把 500011 映射成 500001 大天师）。
        try:
            _mid_chk = int(_b[0])
        except (TypeError, ValueError):
            _mid_chk = None
        if _mid_chk is None or _mid_chk not in _MON_STATIC:
            print('[BAG] 1032 跳过 L91 里不存在的怪 ucode=%s mid=%r' % (_uc, _b[0]), flush=True)
            continue
        ucs.append(_uc)
    total = max(1, (len(ucs) + BAG_PAGE - 1) // BAG_PAGE)
    try:
        page = int(page or 1)
    except Exception:
        page = 1
    page = max(1, min(page, total))
    _start = (page - 1) * BAG_PAGE
    parts = []
    for uc in ucs[_start:_start + BAG_PAGE]:
        m = _mon_with_state(uc)
        if m is None:
            # ★2026-09-11 (v21 §3.5): 兜底分支也要用穿戴皮肤 (get_monster_skin), 否则没有 uequip 记录的怪
            #   (如抽到后未布阵) 在 1032 里皮肤回退成基础皮肤
            # ★v34: `uc in monster_ueq` 这个前置判断已删 —— 装备槽缺失会让 _mon_with_state 内部
            #   惰性补种 (ensure_monster_ueq), 而不是让 1032 走"没有 uequip"的兜底分支 (那样会掩盖 bug#1)。
            mid, skin, num, skills = get_monster_base(uc)
            try:
                from state import get_monster_skin as _gms
                skin = _gms(uc) or skin
            except Exception:
                pass
            ad = get_monster_add(uc)
            m = _mon(int(uc), mid=mid, num=num, skin=skin, skills=list(skills), uequip=('0', '0', '0'),
                     lv=get_monster_lv(uc), exp=get_monster_exp(uc), grow=get_monster_grow(uc),
                     addhp=ad[0], addatk=ad[1], adddef=ad[2], addcri=ad[3])
        parts.append(m)
    return '[1,[[' + ','.join(parts) + '],"%d/%d"]]' % (page, total)

def skill_bag_json():
    # 技能背包 = 独立技能袋(ensure_skills 种子)的并集; item=[ucode,id,level,isload,isopen,ischange]
    #   isload 由 monster_skills 推算 (任一怪装配即1); id=技能行id(决定等级); level=行SkillLv
    #   ★ 2026-09-07(第二轮) 修复: 必须【全量下发】(含 isload=1 的怪物自带技能)!
    #     之前改成"只输出 isload=0"导致客户端 SQSkill 袋为空 -> 怪物面板技能槽 getObject(ucode)=nil
    #     -> 客户端回退 L93 占位行(999999=Dummy) 显示"攻击 Lv.2 Dummy"; 技能替换/强化同样失效.
    #     客户端"背包技能页只显示 isload=0"是 SkillBase:getSkillList 的 UI 过滤, 服务器无需预过滤.
    from state import ensure_skills, skill_isload_map
    s = ensure_skills()
    il = skill_isload_map()
    items = []
    for k in sorted(s, key=lambda x: int(x) if str(x).isdigit() else 0):
        sid, lv, isopen = s[k]
        items.append('["%s",%d,%d,%d,%d,0]' % (k, sid, lv, il.get(k, 0), isopen))
    return '[1,[[' + ','.join(items) + '],"1/1"]]'
