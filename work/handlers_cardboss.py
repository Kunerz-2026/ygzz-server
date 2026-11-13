# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 卡池BOSS / 异界之门 (2026-09-16 v59 多BOSS重写)
#
# ============================ 协议契约（逐行取自新包明文 client） ============================
#   T03_CardBoss  （新包 28 行，1.31 只有 1 行）：
#     ID / CardBossID / CardIMGID / Name(CHT/ENG) / Times(每日免费次数) / CostItem+CostNum(付费门票)
#     Type(可上阵种族 1人族2鬼魅3妖魔4精怪5仙灵, '|' 分隔) / Job / Quality / Level(可上阵等级下限)
#     BattleID(=T01_MstrGroup 行 id) / RewardID..5 + ShowReward(=T02_Reward.GroupID)
#     PrayValue("50_5|100_8|150_10|200_15") / ValueMax
#
#   8004 HOME   req {id: CardBossID} -> [1,[prayValue, up, lastMilliSeconds, ticketNum,
#                                            canChallengeNum, freeNum, cardBossID]]
#       client NetCardBossHome.lua:29-37 逐字段 tonumber；homeList.cardBossID -> refreshUI
#       ★refreshUI 第 202-207 行：`SDCardBoss:getCardBossIndex(cardBossID)` 会用**服务端回的
#         cardBossID** 去 T03 表里查下标 -> 返回 0 时 getObject(...,0) 得到空表 ->
#         getStatic_Name() 取 nil -> setString(nil) 崩。**所以服务端回的 id 必须真实存在于 T03。**
#   8003 RANK   req {id} -> [1,[rankList, myRankList]]
#       myRankList=[rank,name,hurt,img,frame,level]（client VCardBossActivityRank:setMyRankInfo 逐个读，
#       img 查 T35_HeadImage / frame 查 SDHeadFrame -> 给 0 会 setTexture(nil)）
#   8005 START  req {id} -> [1,[]]  （纯回调；消耗免费次数或门票）
#   8006 RESULT req {ourside:[{ucode,exp}], value(ZZRc4 密文=奖励组id), time, result,...}
#       -> [1,[roleInfo, ourFormation, dropInfo]]  （NetCardBossResult.recvMessage 逐字段）
#
# ============================ v59 改了什么（相对 1.31 单 BOSS 版） ============================
#   ① 从"写死 BOSS_ID=1001 / DAILY_TIMES=1"改为**按 T03_CardBoss 全表**（28 个 BOSS）
#   ② 每日免费次数 = **每只 BOSS 各自 1 次**（T03.Times），该 BOSS 免费次数用完才按
#      MAX_TICKET_COST(=1) 张门票/场（逐 BOSS 的 times 同时用于排行榜祈愿值与取证留痕）
#   ③ 8004 回**请求里的那个 cardBossID**（不再是常量 1001）
#   ④ 奖励接官方掉落组：8006 的密文 value -> T02_Reward.GroupID（走 handlers_game.official_drop_group）
#   ⑤ 8003 榜单补 img/frame/level（旧版 3 元组会让客户端索引 nil）
#   ⑥ 新增 [CARDBOSS] 留痕（纯 ASCII，便于真机取证）
#   ⑦ v59 续：8003 进榜即按名次结算 T04_CardBossReward 档位奖励（客户端没有领取协议）
#
# ★ 兼容性：单测 `_cardboss_test.py` 覆盖旧语义（免费 1 次 / 之后扣门票 / 胜 +300 金 +30 祈愿）
#   与新语义（28 BOSS / 逐 BOSS 隔离 / 8004 回真实 id / 8001 cardpool 契约）。
import json, os, time
from state import (EQ_STATE, save_state, get_monster_base, get_monster_lv, get_monster_exp,
                   all_monster_ucodes, vitality_timerid, load_table)
from state import explore_role_id as _explore_role_id
from handlers_equip import _mon_with_state
from handlers_cult import _MONSTER_EXP as _CB_EXP

_TABLE = 'T03_CardBoss.json'

def _load_boss_table():
    """T03_CardBoss: CardBossID(str) -> 行 dict。读不到 -> 空 dict（调用方兜底）。"""
    out = {}
    try:
        t = load_table(_TABLE)
    except Exception as e:
        print('[CARDBOSS] !! T03_CardBoss 读表失败: %r' % (e,))
        return out
    h = {c: i for i, c in enumerate(t[0])}
    for r in t[1:]:
        try:
            bid = str(r[h['CardBossID']])
            out[bid] = {c: r[h[c]] for c in h if h[c] < len(r)}
        except Exception:
            continue
    return out

# ★ 模块级缓存 + 显式刷新口（服务端换表后**必须重启**，见交接档 §0；这里只做首次懒加载）
_BOSS = None

def boss_table():
    global _BOSS
    if _BOSS is None:
        _BOSS = _load_boss_table()
        print('[CARDBOSS] T03_CardBoss loaded bosses=%d ids=%s'
              % (len(_BOSS), ','.join(sorted(_BOSS, key=lambda x: int(x) if x.isdigit() else 0)[:6]) + ('...' if len(_BOSS) > 6 else '')))
    return _BOSS

def boss_ids():
    """全部 CardBossID（int 升序）"""
    return sorted(int(k) for k in boss_table().keys() if str(k).isdigit())

def _boss(bid):
    return boss_table().get(str(bid))

DEFAULT_BOSS_ID = 1001       # 兜底（= 1.31 唯一那只）；T03 里一定存在
PRAY_PER_WIN  = 30           # 每次挑战胜利的祈愿值（保留 1.31 私服语义）
PRAY_PER_LOSE = 5
GOLD_WIN      = 300
GOLD_LOSE     = 30
TICKET_PROP   = '1'          # 门票道具背包 ucode（L02 1001 异界秘钥）
# ★门票上限（私服口径，2026-09-16 v59 用户裁定后定）：
#   新包 T03_CardBoss 的 CostNum 是 **999**（那是"活动期最多挑战 999 次"的上限语义，不是单价）。
#   若照抄 999，玩家每打一场要掏 999 张异界秘钥，玩法直接不可玩。
#   客户端只把 CostNum 当**文案**（"消耗 x999"），真正的扣费在服务端 -> 服务端按 1 张计。
MAX_TICKET_COST = 1

# ---------------------------------------------------------------- 状态
def _cb():
    """cardboss 状态容器。v59 新增字段：
         bosses    : {'1001': {'pray':N,'times':N,'date':''}}  —— 逐 BOSS 每日计数/祈愿
         active    : 当天展示的 CardBossID（8001 cardpool 下发它）
         active_day: 'YYYY-MM-DD'
       旧字段 pray/times/date 保留（= 汇总祈愿值 / 总次数），保证老档与老断言不变。
    """
    cb = EQ_STATE.setdefault('cardboss', {'pray': 0, 'times': 0, 'date': ''})
    cb.setdefault('pray', 0)
    cb.setdefault('times', 0)
    cb.setdefault('date', '')
    if not isinstance(cb.get('bosses'), dict):
        cb['bosses'] = {}
    cb.setdefault('active', 0)
    cb.setdefault('active_day', '')
    # ★v59 续2（用户裁定）: 名次奖励改走**邮件按玩家id结算发放**（T04.Rank = 名次奖励表，
    #   官方路径就是"活动结束后发奖邮件"，而不是点一次榜发一次）。
    #   rank_settled_day: 已经邮件结算过的日期（每天一次，跨天补发上一天的）。
    cb.setdefault('rank_settled_day', '')
    return cb

def _today():
    return time.strftime('%Y-%m-%d')

def _boss_state(bid):
    """逐 BOSS 状态；跨天自动清零 times"""
    cb = _cb()
    b = cb['bosses'].setdefault(str(bid), {'pray': 0, 'times': 0, 'date': ''})
    b.setdefault('pray', 0)
    b.setdefault('times', 0)
    b.setdefault('date', '')
    if b.get('date') != _today():
        b['date'] = _today()
        b['times'] = 0
    return b

def _reset_day():
    """全局每日重置（保留旧入口；逐 BOSS 的清零在 _boss_state 内做）"""
    cb = _cb()
    if cb.get('date') != _today():
        cb['date'] = _today()
        cb['times'] = 0
        cb['free'] = DAILY_FREE_TOTAL

# ---------------------------------------------------------------- 免费次数预算
# ★v59 口径（真机实测后修正）: T03.Times = **每只 BOSS 每天各自** 1 次免费。
#   判据（现场 UI 文案，_v59_s7.png / _v59_s10.png）:
#     8004 home 里 freeNum=1 时客户端显示「免费次数 1/1」，而这是**逐 BOSS 的界面**
#     （同一时刻点开 BOSS 1002 也显示 1/1）⇒ 客户端的 freeNum 语义就是"这只 BOSS 今天还剩几次免费"。
#   所以：免费次数记在**逐 BOSS 状态**里（_boss_state(bid)['times'] < T03.Times），
#         而卡池 D04 那一份 upNum 只影响曝光权重；门票只在"该 BOSS 免费次数用完"后才扣。
#   另: EQ_STATE['cardboss']['free_used'] 只作为"当天免费总消耗"的统计（不影响可用性判断）。
DAILY_FREE_TOTAL = 1        # 兜底（T03 读不到 Times 时用）

def _daily_free_left(bid, row=None):
    """该 BOSS 今日剩余免费次数 = T03.Times - 逐BOSStimes"""
    row = row if row is not None else (_boss(bid) or {})
    total = _free_total_for(row)
    used = _int(_boss_state(bid).get('times'), 0)
    return max(0, total - used)

def _free_total_for(row):
    """该 BOSS 的每日免费上限（= T03.Times，用于 UI 的 '免费次数 x/Times'）"""
    return max(1, _int((row or {}).get('Times'), 1)) if row else DAILY_FREE_TOTAL

def _bump_free_used():
    """统计用：当天免费消耗 +1（跨天自动清零）"""
    cb = _cb()
    if cb.get('date') != _today():
        cb['date'] = _today()
        cb['times'] = 0
        cb['free_used'] = 0
    cb['free_used'] = _int(cb.get('free_used'), 0) + 1

# ---------------------------------------------------------------- 工具
def _midnight_ms():
    """距下个本地午夜的毫秒数（client `lastMilliSeconds/1000` -> getClockStrType）"""
    try:
        import datetime as _dt
        now = _dt.datetime.now()
        mid = (now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1000, int((mid - now).total_seconds() * 1000))
    except Exception:
        return 86400000

def _int(v, d=0):
    try:
        return int(v)
    except Exception:
        return d

def _boss_cost_item(row):
    """门票在**服务端道具背包 (EQ_STATE['prop'])** 里的 ucode。
    ★实测 (2026-09-16): T03.CostItem='1001' 是 L02_Loots 的静态 id（异界秘钥），
      而本私服道具背包是 [ucode, 静态id, 数量] 形态，异界秘钥的 ucode = **'1'**
      （见 state.py `_PROP_ID_DEFAULTS = {'1': 1001, ...}`，镜像 1033 PROP_BAG）。
      所以扣费/显示一律走 ucode '1'；若将来背包改成"ucode==静态id"形态，
      `_qty()` 会自动回退到用 T03.CostItem 去查（两种形态都安全）。
    """
    return TICKET_PROP

def _qty(ucode, static_id=None):
    """道具数量: 优先按 ucode 查；查不到且给了静态 id 时按静态 id 再查一次（形态兜底）"""
    prop = EQ_STATE.get('prop') or {}
    if ucode in prop:
        return _int(prop.get(ucode), 0)
    if static_id is not None and str(static_id) in prop:
        return _int(prop.get(str(static_id)), 0)
    return 0

def _pay(ucode, static_id, cost):
    """扣道具；返回 True/False。两种背包形态都能扣对。"""
    prop = EQ_STATE.setdefault('prop', {})
    k = ucode if ucode in prop else (str(static_id) if static_id is not None and str(static_id) in prop else None)
    if k is None or _int(prop.get(k), 0) < cost:
        return False
    prop[k] = _int(prop[k], 0) - cost
    return True

def _ticket_static(row):
    v = (row or {}).get('CostItem')
    return str(v) if v not in (None, '', '0') else '1001'

def _boss_cost_num(row):
    """门票单价（张）。T03.CostNum=999 是"活动期上限"语义，不能当单价见 MAX_TICKET_COST 注释。"""
    return max(1, min(MAX_TICKET_COST, _int((row or {}).get('CostNum'), 1)))

# ---------------------------------------------------------------- 排行榜名次奖励 (T04_CardBossReward)
#   表：T04_CardBossReward = [ID, Rank("4|10"), GroupID(T02_Reward.GroupID)]
#       新包 6 档： 1|1 -> 19001 / 2|3 -> 19002 / 4|10 -> 19003 /
#                   11|20 -> 19004 / 21|50 -> 19005 / 51|9999 -> 19006
#   ★客户端**没有"领取"协议**（新包 ConfigNet 里 CARDBOSS 只有 8003/8004/8005/8006），
#     排行榜界面只**展示**名次奖励档位（VCardBossActivityRank:setItemInfo -> SDCardBossReward:getGroupID）。
#   ⇒ 私服口径：**服务端按名次直接把该档奖励打进账号**（同名次档只发一次）。
#     与 v58 §10 缺口登记一致：缺的是"发放时机"而不是"奖励组"（组已由 patch1 补进 T02）。
_T04_CACHE = None

def reward_tiers():
    """T04_CardBossReward -> [(rank_lo, rank_hi, group_id), ...]（按 lo 升序；读不到 -> []）"""
    global _T04_CACHE
    if _T04_CACHE is not None:
        return _T04_CACHE
    out = []
    try:
        t = load_table('T04_CardBossReward.json')
        h = {c: i for i, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                seg = str(r[h['Rank']]).split('|')
                lo = _int(seg[0], 1)
                hi = _int(seg[1], lo) if len(seg) > 1 else lo
                gid = _int(r[h['GroupID']], 0)
            except Exception:
                continue
            if gid:
                out.append((lo, hi, gid))
    except Exception as e:
        print('[CARDBOSS] !! T04_CardBossReward 读表失败: %r' % (e,))
    out.sort(key=lambda x: x[0])
    _T04_CACHE = out
    return out

def _rank_group_for(rank):
    """名次 -> 该档奖励组 id（不在任何档内 -> None）"""
    for lo, hi, gid in reward_tiers():
        if lo <= rank <= hi:
            return gid
    return None

def _my_rank(bid, rows=None):
    """我在该 BOSS 榜上的名次。
    ★私服只有 1 个玩家: 挑战过 = 第 1 名; 没挑战过 = **-1**。
      客户端语义（VCardBossActivityRank:setMyRankInfo）: -1=暂未上榜 / -2=50名开外 / >0=真实名次。
    ★★2026-11-05（审查 #11）：有了**真实他人行**之后，名次不能再恒为 1 —— 由 `_cb_board`
      把"我"和他人放在**同一张榜**里排序算（`rows` 参数保留只为兼容旧调用点）。
    """
    return _cb_board(bid)[1]


# ---------------------------------------------------------------- 8003 他人榜
CB_BOT_MARK = '（系统）'


def _cb_my_name():
    """自己在榜上的显示名：`role_name` 优先，其次账号名，最后 '测试'。

    ⚠ 客户端 `setMyRankInfo` 直接 `setString(name)`；给 nil 会崩，给空串会显示空行。
    """
    try:
        n = str(EQ_STATE.get('role_name') or '').strip()
        if n:
            return n
        from accounts import ENABLED, REGISTRY, EQ_STATE as _AS
        if ENABLED and REGISTRY is not None:
            cur = _AS.current()
            aid = getattr(cur, 'id', None)
            if aid:
                return '未命名#' + str(aid)
    except Exception:
        pass
    return '测试'


def _cb_board(bid):
    """8003 的完整榜：`(rankList, myRank)` —— **我的那一行参与排位**。返回 `(rows, myrank)`。

    ★★2026-11-05（审查 #11 + `_rank_matrix_test` R5 当场判红）：第一版把他人行**单独**排成
      1..n，于是"我祈愿值 100 最高"时列表里乙仍显示第 1 名 —— 客户端会把两行都画成名次 1。
      现在把"我"作为一个条目一起排序、再拆出来，名次天然唯一且连续（1..n+1）。
    返回 `myrank = -1` 表示"没挑战过、未上榜"（客户端语义）。
    """
    st = _boss_state(bid)
    mypray = _int(st.get('pray'), 0)
    played = (_int(st.get('times'), 0) > 0) or (mypray > 0)
    entries = []          # [pray, name, img, frame, level, is_me]
    for (name, pray, img, frame, lv) in _cb_other_raw(bid):
        entries.append([pray, name, img, frame, lv, False])
    if played:
        entries.append([mypray, _cb_my_name(), 1001, 1001,
                        _int(EQ_STATE.get('player_lv'), 1), True])
    # 排序：祈愿值降序；**同值时真人在前**（占位行只是撑场面，不能靠名字顺序把玩家挤到后面）；
    #   再同则按名字（保证确定性，测试可复现）。
    entries.sort(key=lambda e: (-_int(e[0], 0), 0 if e[5] else 1, str(e[1])))
    rows, myrank = [], (-1 if not played else None)
    for i, e in enumerate(entries):
        if e[5]:
            myrank = i + 1
            continue
        rows.append([i + 1, e[1], _int(e[0], 0), e[2], e[3], e[4]])
    return rows, myrank


def _cb_other_raw(bid):
    """8003 的**他人行**原始数据 -> `[(name, pray, img, frame, level), ...]`（不排名次）。

    ★★2026-11-05（C0 审查 #11「卡池 BOSS 他人榜为空」）：修前恒回**空表** ——
      多人服里别人打出的祈愿值一行都看不到，玩家看到的是一个永远只有自己的榜。
      两条路径，判据不许合并：
        · **多账号**（`ENABLED and REGISTRY`）：只读地取每个**其它账号**在该 BOSS 上的
          `cardboss.bosses[bid].pray`。纪律与 `handlers_rank._acc_*` 完全一致：
          **只读 `acc.data`、绝不 bind、绝不 setdefault**（bind 到别人会在
          `save_state()` 里写别人的档，`_b7` 的"B 档逐字节没变"判据正卡这里）。
          名字走 `handlers_rank._acc_name`（缺省 `未命名#<id>`，绝不回 '测试' —— 否则全榜同名，
          掩盖"跨账号没生效"）。
        · **单档**（旧存档 / 单测）：世界上没有"别人"这个数据源 ⇒ 回 **2 行系统占位**，
          名字带 `（系统）` 后缀**明示**，祈愿值恒 0（**不编进度**）。空表已经被审查判过一次，
          所以这里宁可给明示的占位，也不给"看起来没实装"的空列表。
    """
    rows = []
    others = None
    try:
        from accounts import ENABLED, REGISTRY, EQ_STATE as _AS
        if ENABLED and REGISTRY is not None:
            mine = getattr(_AS.current(), 'id', None)
            others = [a for a in REGISTRY.all_accounts_full()
                      if mine is None or a.id != mine]
    except Exception:
        others = None
    if others is not None:
        try:
            from handlers_rank import _acc_name, _acc_head, _acc_frame, _acc_lv
        except Exception:
            return rows
        for a in others:
            try:
                d = a.data if isinstance(a.data, dict) else {}
                cb = d.get('cardboss') or {}
                b = (cb.get('bosses') or {}).get(str(bid)) or {}
                pray = _int(b.get('pray'), 0)
            except Exception:
                pray = 0
            rows.append((_acc_name(a), pray, _acc_head(a), _acc_frame(a), _acc_lv(a)))
    else:
        rows = [('巡山小妖' + CB_BOT_MARK, 0, 1001, 1001, 1),
                ('看门道人' + CB_BOT_MARK, 0, 1001, 1001, 1)]
    return rows

def _rank_reward_state(bid):
    return _boss_state(bid).setdefault('rank_reward', {})

def settle_rank_reward(bid, force=False):
    """结算并发**邮件**名次奖励（幂等：同一档只发一次）。

    ★口径（2026-09-16 用户裁定）："没有领奖口的是根据玩家id通过邮件发放"。
      T04_CardBossReward.Rank 列 = 名次奖励表，官方路径 = 活动名次 -> **邮件**（按玩家 id 投递），
      而不是"点一次排行榜就发一次货"。
    本函数因此改为：
      1) 取该玩家在该 BOSS 上的名次（私服单人：打过=1，没打过=-1）
      2) 名次 -> 该档 GroupID（T04）
      3) 该档没发过 -> 把 T02 组内容**逐条**作为附件发一封 GM 邮件（id 从 9100 起单调递增）
      4) 记录 granted_group / rank / date（防重复投递）
    返回 (group_id|None, items|[])
    """
    rank = _my_rank(bid)
    if rank <= 0:
        return None, []
    gid = _rank_group_for(rank)
    if not gid:
        return None, []
    rs = _rank_reward_state(bid)
    if _int(rs.get('granted_group'), 0) == gid:
        return gid, []                                    # 本档已发过
    items = _group_items(gid)
    if not items:
        print('[CARDBOSS] 名次奖励组 %s 无可发内容（T02 里没有？）' % (gid,))
        return gid, []
    ok = _mail_rank_reward(bid, rank, gid, items)
    if not ok:
        return gid, []
    rs['granted_group'] = gid
    rs['rank'] = rank
    rs['date'] = _today()
    try:
        save_state()
    except Exception:
        pass
    print('[CARDBOSS] 名次奖励已发**邮件** bid=%s rank=%s group=%s -> %d 项附件'
          % (bid, rank, gid, len(items)))
    return gid, items


def _group_items(gid):
    """T02 组 -> 邮件附件列表 [(REWARD_NEWTYPE, 静态id, 数量)]（最多 5 项，客户端只有 affix1~5）"""
    out = []
    try:
        t = load_table('T02_Reward.json')
        h = {c: i for i, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                if _int(r[h['GroupID']]) != _int(gid):
                    continue
                rate = float(r[h['Rate']] or 0)
            except Exception:
                continue
            if rate < 1.0:                       # 概率行：邮件是"必给"，不投递没抽中的行（保底只取 100% 的）
                continue
            ty = _int(r[h['Type']])
            oid = _int(r[h['ObjectID']])
            qty = max(1, _int(r[h['Quantity']], 1))
            if ty <= 0:
                continue
            out.append([ty, oid, qty])
            if len(out) >= 5:
                break
    except Exception as e:
        print('[CARDBOSS] 读 T02 组 %s 失败: %r' % (gid, e))
    return out


def _mail_rank_reward(bid, rank, gid, items):
    """把名次奖励投递成一封 GM 邮件（走 handlers_sys 的既有写原语，异常兜底返回 False）"""
    try:
        import handlers_sys as _hs
        # 读原始配置：handlers_sys 没有 _cfg_list（那是 admin_backend 的），这里自己读同一路径，
        #   保持"读 -> 追加 -> 用 gm_mail_write 原子写"三步，避免绕开校验/时间边车逻辑。
        import json as _json
        try:
            with open(_hs._GM_MAIL_PATH, 'rb') as _f:
                # utf-8-sig: 容忍 BOM（运营用记事本/PowerShell 编辑过就会带 —— 本轮真机踩到）
                cfg = _json.loads(_f.read().decode('utf-8-sig'))
            if not isinstance(cfg, list):
                cfg = []
        except FileNotFoundError:
            cfg = []
        mid = 9100
        used = set()
        for c in cfg:
            try:
                used.add(int(c.get('id')))
            except Exception:
                pass
        while mid in used:
            mid += 1
        cfg.append({'id': mid,
                    'title': '异界之门 · 殿主%s 第%d名奖励' % (bid, rank),
                    'type': 1, 'read': 0,
                    'items': [[int(i[0]), int(i[1]), int(i[2])] for i in items]})
        _hs.gm_mail_write(cfg)
        print('[CARDBOSS] 名次奖励邮件 id=%s bid=%s rank=%s group=%s items=%s'
              % (mid, bid, rank, gid, items))
        return True
    except Exception as e:
        print('[CARDBOSS] !! 名次奖励邮件投递失败: %r' % (e,))
        return False


def _pray_steps(row):
    """PrayValue '50_5|100_8|...' -> [(阈值, UP值), ...]"""
    out = []
    for seg in str((row or {}).get('PrayValue') or '').split('|'):
        p = seg.split('_')
        if len(p) >= 2:
            try:
                out.append((int(p[0]), int(p[1])))
            except Exception:
                continue
    return out

def _up_for(row, pray):
    """当前祈愿值达到的 UP 档位值（达不到第一档 -> 0）"""
    up = 0
    for th, val in _pray_steps(row):
        if pray >= th:
            up = val
    return up

def _card_pool_id(bid):
    """该 BOSS 挂在哪个卡池（D04_CardPool / 8001 cardpool 的行 id）。
    ★1.31 只有 2 个池（1001 每日池 / 1002 基本池），全部 28 只 BOSS 统一挂 1001 每日池
      （成本最低、玩家一进召唤页就能看到「卡池BOSS」按钮；m_cardID 走 BOSS 自身 id，与池无关）。
    """
    return 1001

def daily_boss_id():
    """当天展示的 BOSS（跨天轮换；当天内固定）。全 28 只都会轮到，且存档里 active 可追溯。"""
    ids = boss_ids()
    if not ids:
        return DEFAULT_BOSS_ID
    cb = _cb()
    if cb.get('active_day') != _today() or _int(cb.get('active'), 0) not in ids:
        try:
            import datetime as _dt
            doy = _dt.datetime.now().timetuple().tm_yday
        except Exception:
            doy = 1
        bid = ids[(doy - 1) % len(ids)]
        cb['active_day'] = _today()
        cb['active'] = bid
    return _int(cb.get('active'), DEFAULT_BOSS_ID)

# ---------------------------------------------------------------- 8001 cardpool 数据
def cardpool_entry(pool_id, group_override=None, bg=None):
    """按 D04_CardPool 行 id 现算一条卡池记录（8 字段，契约见 handlers_draw 注释）。

    返回 None = 该池无 BOSS 活动（客户端 Button_CardPoolBoss 不显示）。
    ★只用 D04_CardPool 里**真实存在**的池 id，绝不编造 —— 客户端 getPoolIndex 按 carID 匹配。

    ★★2026-11-07（活动卡池轮换）：新增 `group_override` / `bg` 两个可选参数，供
      `cardpool_rotation` 的**私服合成池**（默认 1003，D04 里没有这一行）组装同形记录：
        · `group_override` 给出时不再查 D04 的 CardGroupID（当期卡组由轮换器决定）；
        · `bg` 是活动页底图（1.30 内置 Raffle 裸文件名），0 表示不换图。
      D04 读取失败但给了 `group_override` 时仍能组装（卡池BOSS 入口不能因为表读坏而消失）。
    """
    try:
        t = load_table('D04_CardPool.json')
    except Exception:
        t = []
    row = None
    h = {}
    if t:
        h = {c: i for i, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                if _int(r[h['ID']]) == _int(pool_id):
                    row = r
                    break
            except Exception:
                continue
    if row is None and group_override is None:
        return None
    bid = daily_boss_id()
    row_boss = _boss(bid) or _boss(DEFAULT_BOSS_ID) or {}
    # 卡池的 CardGroupID 决定"抽卡概率表"；BOSS 自身的 UP 值与它无关（UP 只影响 BOSS 权重）
    if group_override is not None:
        _cg = _int(group_override, 0)
    else:
        _cg = _int(row[h['CardGroupID']], 0)
    up = _up_for(row_boss, _int(_cb().get('pray'), 0))
    return [int(pool_id), 0, (bg if bg else 0), 0, 0, up, 1, _cg]
    #         ↑time(s) ↑bg  ↑name ↑remark ↑upNum ↑bossOpen ↑pCardId

# ---------------------------------------------------------------- 8004 HOME
def handle_cardboss_home(body_json):
    # 8004
    _reset_day()
    req = _q(body_json)
    bid = _int(req.get('id'), 0) or daily_boss_id()
    row = _boss(bid)
    if row is None:
        # ★绝不回一个 T03 里没有的 id（客户端 refreshUI 会拿它查下标 -> 0 -> 崩）
        print('[CARDBOSS] 8004 请求了不存在于 T03 的 id=%s -> 兜底 %d' % (bid, DEFAULT_BOSS_ID))
        bid = DEFAULT_BOSS_ID
        row = _boss(bid)
    if row is None:                       # 连兜底都没有（表读坏了）-> 回错误码，绝不发脏 id
        print('[CARDBOSS] !! T03_CardBoss 为空, 8004 返回错误码')
        return b'[0,{}]'
    st = _boss_state(bid)
    free_total = _free_total_for(row)
    free_left = _daily_free_left(bid, row)
    _uc = _boss_cost_item(row)
    tickets = _qty(_uc, _ticket_static(row))
    remain = free_left + tickets
    data = [_int(st.get('pray'), 0), _up_for(row, _int(st.get('pray'), 0)), _midnight_ms(),
            tickets, remain, free_left, bid]
    try:
        print('[CARDBOSS] 8004 home id=%s free=%d/%d tickets=%d(ucode=%s) pray=%d up=%d'
              % (bid, free_left, free_total, tickets, _uc, data[0], data[1]))
    except Exception:
        pass
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ---------------------------------------------------------------- 名次奖励的"自动结算时机"
def settle_daily_rank_rewards():
    """跨天时把**上一天**的异界之门名次奖励按邮件补发一次（幂等）。

    为什么要有这个（用户口径：名次奖走邮件按玩家 id 发放）：
      · 官方是"活动结算 -> 发奖邮件"，玩家不需要点任何按钮；
      · 私服里最接近的时机是**每天第一次进游戏/拉邮件**时补结算一次（跨天判定 rank_settled_day）。
    调用点：handlers_sys.handle_mail_get（1055）—— 玩家每次进游戏都会拉邮件列表。
    """
    cb = _cb()
    if cb.get('rank_settled_day') == _today():
        return []
    out = []
    try:
        for bid in boss_ids():
            st = _boss_state(bid)
            if _int(st.get('times'), 0) <= 0 and _int(st.get('pray'), 0) <= 0:
                continue                       # 没打过这只 -> 没名次 -> 不发
            rs = st.get('rank_reward') or {}
            gid = _rank_group_for(_my_rank(bid))
            if gid and _int(rs.get('granted_group'), 0) != gid:
                _g, _items = settle_rank_reward(bid)
                if _items:
                    out.append((bid, _g, len(_items)))
    except Exception as e:
        print('[CARDBOSS] 每日名次结算异常: %r' % (e,))
    cb['rank_settled_day'] = _today()
    try:
        save_state()
    except Exception:
        pass
    if out:
        print('[CARDBOSS] 每日名次奖励结算: %s' % (out,))
    return out


# ---------------------------------------------------------------- 8003 RANK
def handle_cardboss_rank(body_json):
    # 8003: rankList = **他人行**（多账号读真实档；单档回明示的系统占位，见 `_cb_other_rows`）;
    #   myRankList=[rank, name, hurt, img, frame, level]
    #   ★hurt 语义 = 服务端累的**祈愿值**（我在该 BOSS 上的进度）；客户端 TextAtlas_Hurt 直接 setString。
    #   ★名次奖励**不在 8003 发放**（口径：按玩家 id 发邮件，见 settle_daily_rank_rewards）。
    #     这里只回榜单数据。
    #   ★★2026-11-05（审查 #11）：修前 `data = [[], my]` —— 他人榜恒空。
    req = _q(body_json)
    bid = _int(req.get('id'), 0) or daily_boss_id()
    st = _boss_state(bid)
    rows, rank = _cb_board(bid)
    my = [rank, _cb_my_name(), _int(st.get('pray'), 0), 1001, 1001,
          _int(EQ_STATE.get('player_lv'), 1)]
    data = [rows, my]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ---------------------------------------------------------------- 8005 START
def handle_cardboss_start(body_json):
    # 8005: 优先消耗每日免费次数；免费次数用完 -> 扣门票（CostItem × CostNum）
    _reset_day()
    req = _q(body_json)
    bid = _int(req.get('id'), 0) or daily_boss_id()
    row = _boss(bid)
    if row is None:
        bid = DEFAULT_BOSS_ID
        row = _boss(bid)
    st = _boss_state(bid)
    free_total = _free_total_for(row)
    item = _boss_cost_item(row)
    static_id = _ticket_static(row)
    cost = _boss_cost_num(row)
    used = _int(st.get('times'), 0)
    cb = _cb()
    if used < free_total:                                        # 该 BOSS 今日免费次数未用完
        _bump_free_used()
    else:
        if not _pay(item, static_id, cost):
            return b'[0,{}]'                                     # 无免费次数且门票不足
    st['times'] = used + 1
    cb['times'] = _int(cb.get('times'), 0) + 1                     # 总次数（旧字段，向后兼容）
    # ★★2026-10-16（任务书 D③）：`8006` 必须由 `8005` 开战 —— 这里就是"开战"。
    #   会话 target = **该次开战的 BOSS id**；8006 的 `pooId` 要与之相等（且属今日 BOSS）。
    try:
        from battle_session import open_session as _bs_open
        _bs_open('cardboss', bid)
    except Exception as _e:
        print('[BS] !! 开卡池 BOSS 会话失败(不影响开战): %r' % (_e,), flush=True)
    save_state()
    try:
        print('[CARDBOSS] 8005 start id=%s used=%d/%d free_left=%d item=%s(+%s) cost=%d'
              % (bid, st['times'], free_total, _daily_free_left(bid, row), item, static_id, cost))
    except Exception:
        pass
    return b'[1,[]]'

# ---------------------------------------------------------------- 8006 RESULT
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

def handle_cardboss_result(body_json):
    # 8006: 同战斗结算形状, 祈愿值累计 + 官方掉落组
    req = _q(body_json)
    if not req:
        return b'[0,{}]'
    try:
        iswin = _int(req.get('result'), 1) == 0
    except Exception:
        iswin = False
    # ★★2026-10-16（任务书 D③）：`pooId` 必须是**真实存在的 BOSS**，且本场必须由 8005 开出来。
    #   修前 `bid = pooId or daily_boss_id()`，表外/伪造 pooId 会**静默回落到默认 BOSS**
    #   —— 于是任何 pooId 都能拿去结算，奖励照发、祈愿值照涨。
    #   ⚠ 口径说明：任务书写的是"pooId 属于今日 BOSS"。本服 v59 起 28 只 BOSS **逐 BOSS**
    #     独立存档（`cardboss.bosses[bid]`，`_cardboss_test` 的"逐BOSS pray 隔离"就是这条），
    #     而 `daily_boss_id()` 只是**展示轮换**的那一只。把判据写成 `pooId == daily_boss_id()`
    #     会：① 让非当日 BOSS 的战斗无法结算（与 28 只都保留状态的既有实现矛盾）；
    #            ② 在**跨午夜**开打/结算时误杀正常战斗。
    #     所以这里取更严且不会误杀的口径：**pooId 必须是 T03 真实行，且等于 8005 开战那一只**
    #     （会话 target 就是对它开的）。跨 BOSS 挪用、缺会话、伪造 id 三条都堵住了。
    _poo = _int(req.get('pooId'), 0)
    if _poo and _boss(_poo) is None:
        print('[CARDBOSS] 8006 pooId=%s 不在 T03_CardBoss -> 503' % _poo, flush=True)
        return b'[503]'                       # NetCardBossResult.m_errorCode[503] = 掉落组ID错误
    bid = _poo or daily_boss_id()
    row = _boss(bid)
    if row is None:
        return b'[503]'
    # ★2026-10-16（任务书 D③）：一次性结算（必须凭 8005 开出的那一局，结算即消费）。
    #   被拒时不改任何状态，给**结构完整但无奖励**的应答（客户端不弹错）。
    _ok, _reason = False, 'error'
    try:
        from battle_session import settle as _bs_settle
        _ok, _reason = _bs_settle('cardboss', bid, '%s|%s' % (bid, req.get('result')))
    except Exception as _e:
        print('[BS] !! 卡池 BOSS 结算状态机异常(按未结算处理): %r' % (_e,), flush=True)
    if not _ok:
        print('[BS] 8006 结算被拒: reason=%s bid=%s -> 零奖励零状态变化'
              % (_reason, bid), flush=True)
        return ('[1,' + json.dumps([_role_info(), [], _drop([]), 0, [[], []]],
                                   separators=(',', ':')) + ']').encode('utf-8')
    # ---- 我方怪物经验（与战斗结算一致的升级逻辑）+ 回包阵容 ----
    formation = []
    for m in (req.get('ourside') or []):
        try:
            uc = str(m.get('ucode') or '')
            addexp = _int(m.get('exp'), 0)
        except Exception:
            continue
        if addexp > 0 and iswin and get_monster_base(uc):
            lv = get_monster_lv(uc)
            exp = get_monster_exp(uc) + addexp
            max_lv = max(_CB_EXP.keys()) if _CB_EXP else 200
            while lv < max_lv and exp >= _CB_EXP.get(lv, 99999999):
                exp -= _CB_EXP[lv]
                lv += 1
            EQ_STATE['monster_lv'][uc] = lv
            EQ_STATE['monster_exp'][uc] = exp
        mi = _mon_with_state(uc)
        if mi:
            formation.append(json.loads(mi))
    # ---- 祈愿值：逐 BOSS + 汇总 ----
    st = _boss_state(bid)
    gain = PRAY_PER_WIN if iswin else PRAY_PER_LOSE
    st['pray'] = _int(st.get('pray'), 0) + gain
    cb = _cb()
    cb['pray'] = _int(cb.get('pray'), 0) + gain
    # ---- 奖励：优先官方掉落组（8006 的密文 value = T02_Reward.GroupID） ----
    dropgroups = []
    if iswin:
        _gid = None
        try:
            from handlers_game import official_drop_group
            _gid = official_drop_group(8006, req)
        except Exception as e:
            print('[CARDBOSS] official_drop_group 失败: %r' % (e,))
        if _gid:
            try:
                from handlers_task import _grant_group
                _g, _ok = _grant_group(_gid, roll=True, mult=1)
                if _ok and _g:
                    dropgroups = _g
                    print('[CARDBOSS] 8006 id=%s 官方掉落组=%s -> %d 条' % (bid, _gid, len(_g)))
            except Exception as e:
                print('[CARDBOSS] 发奖失败 gid=%s: %r' % (_gid, e))
    if dropgroups:
        gold = GOLD_WIN if iswin else GOLD_LOSE
        EQ_STATE['gold'] += gold
    else:
        # 与 1.31 私服行为完全一致（官方组取不到/没抽中时不空手）
        gold = GOLD_WIN if iswin else GOLD_LOSE
        EQ_STATE['gold'] += gold
        dropgroups = [[3, [[gold]]]]
    save_state()
    try:
        print('[CARDBOSS] 8006 id=%s win=%s pray=%d gold=+%d groups=%d'
              % (bid, iswin, st['pray'], gold, len(dropgroups)))
    except Exception:
        pass
    # ★2026-10-16（逐槽对客户端）：`NetCardBossResult.lua:9-13` 读**五槽**
    #   （roleInfo / ourFormation / dropInfo / talent / exDropItems），
    #   而这里原来只回 3 槽 —— 与**同一函数的被拒分支**（上方那条已回 5 槽）形状不一致，
    #   正是 7009 那个"同一协议两条路径两种形状"的同一个坑。补齐 talent(0) 与空 exDropItems。
    data = [_role_info(), formation, _drop(dropgroups), 0, _drop([])]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ---------------------------------------------------------------- 请求体
def _q(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return {}
    return req if isinstance(req, dict) else {}
