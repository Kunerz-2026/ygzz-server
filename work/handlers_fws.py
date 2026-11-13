# -*- coding: utf-8 -*-
"""handlers_fws.py —— 烟花大会 1192~1197 + 8086 播放推送（新包 A0 第 7 批）。

**修前的缺口**：这 6 个号服务端**一个都没有**（1108 开闸后客户端真的会发它们了）。落兜底
`[1,[[]]]` 的后果：1192 的 `msg[1..6]` 全空 ⇒ 活动页标题/描述/奖励位全空；1193 的
`msg[1]/msg[2]` 空 ⇒ `tonumber(nil)/1000` = nil，倒计时与"我的积分/排名"**全空**；
1195 的 `msg[1]` 空 ⇒ 积分奖励列表空 ⇒ **领不到任何档位奖励**。

**数据源（全部有出处，没有一处是编的）**
  · `A09_1_ActivityFireworkRank`(7 行，本轮由 `work\\_table_migrate.py add` 迁入表根)：
    `Rank`=1001~1007 = **档位 id**（`gearId`）、`ScoreLimit`=5/10/50/100/200/300/400 = **积分数量**
    （`gearPoints`）、`GroupID`=201~207 → `T02_Reward` 组（实测 7 组**全在**）。
    ⚠ 全表只有 `Type=1` ⇒ "排行档位奖励"（1194 的 `gearRanking`）**没有数据源**，
      见 `handle_fws_rank_preview` 的说明（宁可空列表，不编档位）。
  · 积分：`T23_Function` 里 **ID=255 → 2 分**（单色烟花）、**ID=256 → 8 分**（五彩烟花）
    —— 就是客户端 `FwsData:getOScore()/getFScore()` 读的那两行（`VSociatyRedBag` 那次
    踩过的"ID 列 vs Type 列"陷阱在这里同样成立：255/256 是 **ID**）。
  · 道具与价格：`FwsConfig.O_COLOR=2052`（单色）/`F_COLOR=2053`（五彩）→ `G02_Shop` 里
    `GoodsId` 命中那两行：`GoodsType=98`(礼包)、`PriceType=2`(**妖魂**)、`Price=20/80`。
  · 文案：标题取 `T41_CodeKey` 的 `LOCAL_UI_YANHUA_ACTIVITYTITLE`（"喜迎新春，烟花绽美"），
    描述取 `99_Language` 的 `LOCAL_YANHUA_MAIN_DESC`（那条"燃放烟花…积分可参与排行…"）。
    ⚠ **两张表都要查**：这两条分别在 T41 与 99_Language 里（`SDCodeKey:loadStaticData`
    的 CODE_KEY 分支就是"先 T41 再 99_Language"）。
  · 活动窗口：与 1108 第 5 行（`Activityype=19`）**同一份** `now / now+365d` —— 不复用的话
    "按钮出现的时间"与"活动时间"会是两个答案。

**硬约束（逐条核对客户端）**
  1. ★1193 的 `msg[1]/msg[2]` 必须是**数字**且单位是**毫秒**：客户端
     `msgData.startTime = tonumber(msg[1])/1000` —— 给字符串或给秒都会算错。
  2. ★1196/1197 的 `msg[1]` 必须是**完整 roleInfo 数组**（客户端 `gamer_setRoleInfo(roleInfo)`），
     用 `handlers_game._role_info()`；发字符串/少字段**不报错**，只是金币等级不刷新。
  3. ★1195 的行是 4 槽 `[gearId, gearPoints, gearStatus, [[rewardId,rewardType,rewardNum],...]]`，
     `gearStatus` **0 未完成 / 1 未领取 / 2 已领取**（`FwsReward.lua:308-310` 三态各判一次）。
     `msg[1]` **只包一层**：客户端源码里那句 `-- 服务器回的数据不对 多一层` + 被注释掉的
     `fireworksAccRewards = fireworksAccRewards[1]` 就是上一次多包一层留下的疤。
  4. 1193 的"未上榜"判据是 **`getTop() <= 0`**（`FwsRanks.lua:220`）⇒ 未上榜回 **-1**。
  5. 1197 的 `itemId` 只认 `G02_Shop` 里真实存在的两条（2052/2053），价格按表扣；
     `itemNum` 上限取 `FwsConfig.PRORS_NUM_MAX = 99`（客户端常量，UI 就是按它限的）。

**积分怎么来**（"燃放"= 在背包/烟花界面**使用**该礼包，走既有的 **1098 GIFT_BAG_USE**）：
  `FwsRanks:showFwsUse` 打开的是通用 `VBagUse`（消耗礼包），`closeFwsUse` 只在服务端确认后
  才把本地数量减掉 ⇒ **服务端才是计分与消耗的真值**。所以本模块提供 `note_firework_used()`，
  由 `handlers_bag.handle_gift_bag_use` 在**成功使用 2052/2053 之后**调用（懒 import + 吞异常，
  推送/计分失败绝不能影响开包主流程）。
  ⚠ 烟花礼包**"随机开出好礼"的奖励内容在表根里不存在**：`T02_Reward` 没有 `GroupID=2052/2053`、
    `L10_GiftInfo.ArrayID=2052` 指向的表也没有 —— ⇒ 目前只计分、不发物品（**不编造奖励**），
    已记进 HANDOFF 待拍板。
  ⚠ `score` **不落盘**：它是 `use_o/use_f` 的推导量（"由次数推导的量不留写入点"—— 见 DECISIONS
    里 `gear` 越界崩那次事故）。
"""
import json
import time

from state import EQ_STATE, save_state, table_dir as _table_dir

_DATA = _table_dir()
_O_ID = 2052            # 单色烟花（FwsConfig.O_COLOR）
_F_ID = 2053            # 五彩烟花（FwsConfig.F_COLOR）
_GIFTBAG_TYPE = 98      # bag.baseType.giftbag
_COIN = 'soulcoin'      # PriceType=2 -> 妖魂（FwsRanks 的 Text_DiamondAccount 同一口径）
_NUM_MAX = 99           # FwsConfig.PRORS_NUM_MAX（客户端 UI 的上限）
_REWARD_SLOTS = 8       # 1192 的展示位：客户端 `for i = 1, 8 do FileNode_Rwd..i`


def _load_json(name):
    from state import load_table as _lt
    return [list(_r) for _r in _lt(name, _DATA)]


def _out(*body):
    return ('[1,' + json.dumps(list(body), separators=(',', ':'), ensure_ascii=False)
            + ']').encode('utf-8')


def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _req(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    return req if isinstance(req, dict) else {}


# ---------------------------------------------------------------- 静态表
def _t23_by_id(row_id, fallback):
    """`T23_Function` 按 **`ID`** 列取 `Value`（255/256 就是 ID 列，见模块头）。"""
    global _T23_ID
    if not _T23_ID:
        try:
            t = _load_json('T23_Function.json')
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                _T23_ID[str(r[h['ID']])] = r[h['Value']]
        except Exception:
            pass
    try:
        return int(_T23_ID.get(str(row_id), fallback))
    except Exception:
        return fallback


_T23_ID = {}

_GEARS = []          # [(gearId, gearPoints, groupId)] 按文件顺序
try:
    _t = _load_json('A09_1_ActivityFireworkRank.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            if _i(_r[_h['Type']]) != 1:
                continue
            _GEARS.append((_i(_r[_h['Rank']]), _i(_r[_h['ScoreLimit']]), _i(_r[_h['GroupID']])))
        except Exception:
            continue
except Exception:
    _GEARS = []
_GEAR_IDS = {g[0] for g in _GEARS}
_GEAR_BY_ID = {g[0]: g for g in _GEARS}

# 道具 -> (单价, 每份积分)；价格来自 G02_Shop，积分来自 T23[ID=255/256]
_SHOP = {}
try:
    _t = _load_json('G02_Shop.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            gid = _i(_r[_h['GoodsId']])
            if gid in (_O_ID, _F_ID) and _i(_r[_h['GoodsType']]) == _GIFTBAG_TYPE:
                _SHOP[gid] = _i(_r[_h['Price']])
        except Exception:
            continue
except Exception:
    _SHOP = {}


def _points(gid):
    return _t23_by_id(255 if gid == _O_ID else 256, 0)


def _shop_price(gid):
    return _i(_SHOP.get(gid), 0)


# 文案：**两张表都要查**（T41 与 99_Language）
_TEXT_CACHE = {}


def _code_text(key, fallback=''):
    """按 `codeKey` 取文案；**先 T41_CodeKey 再 99_Language**（`SDCodeKey:loadStaticData` 同序）。

    ⚠ 不能用 `val = fallback` 起手：那样第一张表没命中时 `val` 已经非空，`if val: break`
      会**直接跳出、永远不查第二张表**（本轮实测：`LOCAL_YANHUA_MAIN_DESC` 只在 99_Language 里，
      按错法写会静默回退成兜底值）。所以命中值单独判定、兜底放到最后。
    """
    if key in _TEXT_CACHE:
        return _TEXT_CACHE[key]
    val = ''
    for tname in ('T41_CodeKey.json', '99_Language.json'):
        try:
            t = _load_json(tname)
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                if str(r[h['codeKey']]) == key:
                    val = str(r[h['CHS']] or '')
                    break
        except Exception:
            continue
        if val:
            break
    if not val:
        val = fallback
    _TEXT_CACHE[key] = val
    return val


def _fws_window():
    """活动窗口 —— 与 1108 第 5 行（`Activityype=19`）**同一份**。"""
    now = int(time.time())
    return now, now + 365 * 86400


# ---------------------------------------------------------------- 账号档
def _st():
    v = EQ_STATE.setdefault('fws', {})
    if not isinstance(v, dict):
        v = EQ_STATE['fws'] = {}
    v.setdefault('use_o', 0)          # 已燃放的单色烟花数（**只记次数，积分是推导量**）
    v.setdefault('use_f', 0)
    if not isinstance(v.get('claimed'), dict):
        v['claimed'] = {}
    return v


def _score():
    """我的积分 = 单色数×2 + 五彩数×8（两项都来自 `T23_Function[ID=255/256]`）。"""
    st = _st()
    return _i(st.get('use_o'), 0) * _points(_O_ID) + _i(st.get('use_f'), 0) * _points(_F_ID)


def _counts():
    """我当前**持有**的烟花数量（1193 的 `oNum/fNum`，客户端直接显示）。"""
    prop = EQ_STATE.get('prop') or {}
    gb = EQ_STATE.get('giftbag') or []
    o = _i(prop.get(str(_O_ID)), 0)
    f = _i(prop.get(str(_F_ID)), 0)
    for g in gb:
        try:
            if int(g[2]) == _O_ID:
                o += max(0, int(g[3]))
            elif int(g[2]) == _F_ID:
                f += max(0, int(g[3]))
        except Exception:
            continue
    return o, f


def _my_aid():
    try:
        import accounts as A
        cur = A.EQ_STATE.current() if callable(getattr(A.EQ_STATE, 'current', None)) else None
        return getattr(cur, 'id', None)
    except Exception:
        return None


def _rank_rows():
    """按积分降序的榜（跨账号只读 `acc.data` 标量）。单档/未启用 -> `None`。"""
    try:
        import accounts as A
        reg = getattr(A, 'REGISTRY', None)
        if reg is None or not getattr(A, 'ENABLED', False):
            return None
        rows = []
        for acc in reg.all_accounts_full():
            d = acc.data if isinstance(acc.data, dict) else {}
            fw = d.get('fws') or {}
            sc = (_i(fw.get('use_o'), 0) * _points(_O_ID)
                  + _i(fw.get('use_f'), 0) * _points(_F_ID))
            head = d.get('head') or {}
            rows.append((sc, str(acc.id), str(d.get('role_name') or ''),
                         int(head.get('eq', 1001) or 1001),
                         int(head.get('frame_eq', 1001) or 1001)))
        rows.sort(key=lambda x: (-x[0], x[1]))
        return rows
    except Exception:
        return None


def _my_rank_and_rows():
    """`(我的名次(未上榜 -1), [[roleRanking, rolePoints, roleName, serverId, img, imgFrame], ...]])`。"""
    score = _score()
    me = _my_aid()
    allrows = _rank_rows()
    rows, mine = [], -1
    if allrows is None:
        if score > 0:
            head = EQ_STATE.get('head') or {}
            rows = [[1, score, str(EQ_STATE.get('role_name') or ''),
                     '1', int(head.get('eq', 1001) or 1001), int(head.get('frame_eq', 1001) or 1001)]]
            mine = 1
        return mine, rows
    for i, (sc, aid, nm, img, frm) in enumerate(allrows):
        if sc <= 0:
            continue
        if len(rows) >= 50:
            break
        rows.append([i + 1, sc, nm, '1', img, frm])
        if me is not None and aid == str(me):
            mine = i + 1
    return mine, rows


def _gear_rewards(gid):
    """`T02_Reward` 组 -> `[[rewardId, rewardType, rewardNum], ...]`。

    ⚠ 复用 `handlers_task._T02`（发放路径**同一份**数据）—— 预览与实际发的必须是同一组行，
      自己再读一遍表面上看更"独立"，实际是给两边留了分叉的余地。
    """
    try:
        from handlers_task import _T02
        return [[oid, ty, qty] for (ty, oid, qty, _rate) in _T02.get(_i(gid), [])]
    except Exception:
        return []


# ============================================================================
# 1192 活动展示信息
# ============================================================================
def handle_fws_activity(body_json=None):
    """1192 -> `[1,[标题, 描述, 开始, 结束, 底图, [[rewardId,rewardType,rewardNum],...]]]`。

    `rewards` 的取数：把 7 个档位组的奖励**按档位顺序并集去重**、截前 8 条
      （客户端只有 `FileNode_Rwd1..8` 八个位）。"并集"是**展示口径**，不是编造 ——
      每一条都能在 `T02_Reward` 里查到；截断是客户端槽位决定的。
    `floorImg`（`msg[5]`）**无表源**、客户端也没有读它（全仓 grep `floorImg` 只出现在
      这个 net 文件里）⇒ 发空串。
    时间取与 1108 第 5 行**同一份**窗口（`FwsRanks` 的倒计时就吃它）。
    """
    start, end = _fws_window()
    seen, rewards = set(), []
    for _gid, _pts, grp in _GEARS:
        for it in _gear_rewards(grp):
            k = (it[0], it[1])
            if k in seen:
                continue
            seen.add(k)
            rewards.append([_i(it[0]), _i(it[1]), _i(it[2])])
            if len(rewards) >= _REWARD_SLOTS:
                break
        if len(rewards) >= _REWARD_SLOTS:
            break
    return _out(_code_text('LOCAL_UI_YANHUA_ACTIVITYTITLE', '烟花大会'),
                _code_text('LOCAL_YANHUA_MAIN_DESC', ''),
                start, end, '', rewards)


# ============================================================================
# 1193 排行榜
# ============================================================================
def handle_fws_rank(body_json=None):
    """1193 -> `[1,[开始(ms), 结束(ms), 我的积分, 我的名次, 榜单, 单色数, 五彩数]]`。

    ★ 时间必须是**毫秒**（客户端 `tonumber(msg[1])/1000`）；★ 未上榜回 **-1**
      （`FwsRanks.lua:220` 判 `getTop() <= 0`）。
    """
    start, end = _fws_window()
    mine, rows = _my_rank_and_rows()
    o, f = _counts()
    return _out(start * 1000, end * 1000, _score(), mine, rows, o, f)


# ============================================================================
# 1194 排行奖励预览
# ============================================================================
def handle_fws_rank_preview(body_json=None):
    """1194 -> `[1,[我的积分, 我的名次, [[gearId, gearRanking, [[...奖励...]]], ...]]]`。

    ⚠ **排行档位奖励没有数据源**：`A09_1_ActivityFireworkRank` 7 行**全是 `Type=1`**
      （那是 1195 的积分档：`Rank`=档位id、`ScoreLimit`=积分门槛），没有任何"名次区间 ->
      奖励组"的行；全表根也没有第二张烟花表（`grep` 过表名含 fws/firework/yanhua 的）。
      ⇒ 这一格回**空列表**（页面显示"排行奖励预览"为空），**不拿积分档去冒充名次档**
      （名次区间凭 `ScoreLimit` 推不出来，编一组就是往存档/展示里灌假数据）。
      已记进 HANDOFF 待拍板。前两槽是真的（积分与名次与我一致）。
    """
    mine, _rows = _my_rank_and_rows()
    return _out(_score(), mine, [])


# ============================================================================
# 1195 积分奖励预览 + 领取
# ============================================================================
def _gear_status(gear_id, score):
    st = _st()
    if str(gear_id) in st['claimed']:
        return 2                       # 已领取
    gear = _GEAR_BY_ID.get(gear_id)
    if gear and score >= gear[1]:
        return 1                       # 未领取（达标）
    return 0                           # 未完成


def handle_fws_score_preview(body_json=None):
    """1195 -> `[1,[[gearId, gearPoints, gearStatus, [[rewardId,rewardType,rewardNum],...]], ...]]]`。

    `msg[1]` **只包一层**（客户端源码里"多一层"那句注释 + 被注释掉的解包就是证据）。
    """
    score = _score()
    rows = []
    for gid, pts, grp in _GEARS:
        rows.append([gid, pts, _gear_status(gid, score), _gear_rewards(grp)])
    return _out(rows)


def handle_fws_get_reward(body_json=None):
    """1196 req{activityId, gearId} -> `[roleInfo(**数组**), dropInfo]`。

    码：这张 net 的 `m_errorCode` 只有 `[0]`（"错误，消息体为空"）⇒ 一切拒绝都用 `[0]`，
    **不编造它不认识的码**（客户端 `showTips(m_errorCode[state], state)` 对未知码会
    `getLocal(state)` 查到别的文案，弹一个与操作无关的提示）。
    """
    from handlers_game import _role_info as _gi, _drop as _gd
    from handlers_task import _grant_group
    req = _req(body_json)
    gid = _i(req.get('gearId'))
    gear = _GEAR_BY_ID.get(gid)
    if gear is None:
        return b'[0]'
    score = _score()
    st = _st()
    if str(gid) in st['claimed']:
        return b'[0]'                                  # 已领过
    if score < gear[1]:
        return b'[0]'                                  # 积分不够（客户端注释也说"未完成"）
    groups, ok = _grant_group(gear[2])
    if not ok:
        return b'[0]'                                  # 组查不到 -> 不记已领
    st['claimed'][str(gid)] = 1
    save_state()
    return _out(_gi(), _gd(groups))


# ============================================================================
# 1197 购买烟花
# ============================================================================
def handle_fws_buy(body_json):
    """1197 req{itemId, itemType, itemNum} -> `[roleInfo(**数组**), dropInfo]`。

    只认 `G02_Shop` 里真实存在的两条（2052/2053，`GoodsType=98`），价格按表扣**妖魂**
    （`PriceType=2`；`FwsRanks` 的 `Text_DiamondAccount` 显示的就是妖魂余额，同一个口径）。
    `itemNum` 取 1..`_NUM_MAX`（客户端 UI 常量 99）。
    ⚠ **有"假成功"隐患**：客户端 `NetFwsBuy:recvMessage` 里 `self.m_callFunc(msgData)` 引用的
      `msgData` **没有定义**（是 nil 全局）⇒ 回调拿到 nil；它的回调 `FwsBuy:sendMsg` 里
      `smCB` 只看自身状态就弹"购买成功"并**本地加数量**。⇒ 服务端**必须真的扣钱发货**，
      否则就是"客户端显示买到了、服务端没有"。
    """
    from handlers_game import _role_info as _gi, _drop as _gd
    from handlers_task import grant_one
    req = _req(body_json)
    gid = _i(req.get('itemId'))
    itype = _i(req.get('itemType'), _GIFTBAG_TYPE)
    num = _i(req.get('itemNum'), 0)
    price = _shop_price(gid)
    if gid not in (_O_ID, _F_ID) or price <= 0:
        return b'[0]'                                  # 表里没有这个商品
    # ⚠ itemType 只做**宽松**校验：客户端发的是 `bag.baseType.giftbag`(=98)，但这是"客户端
    #   自己报的类型"，不是判据来源 ⇒ 不匹配也不拒绝（判据是 G02_Shop 那一行），只挡住明显
    #   非法值（0/负数）。
    if itype <= 0:
        return b'[0]'
    if num <= 0 or num > _NUM_MAX:
        return b'[0]'
    total = price * num
    if _i(EQ_STATE.get(_COIN), 0) < total:
        return b'[60]'                                 # 妖魂不足（本工程通用码 60）
    EQ_STATE[_COIN] = _i(EQ_STATE.get(_COIN), 0) - total
    groups = grant_one(_GIFTBAG_TYPE, gid, num)
    if not groups:
        EQ_STATE[_COIN] = _i(EQ_STATE.get(_COIN), 0) + total   # 发不出去就退钱
        return b'[0]'
    save_state()
    return _out(_gi(), _gd(groups))


# ============================================================================
# 1098 GIFT_BAG_USE 的烟花钩子（计分 + 8086 推送）
# ============================================================================
def note_firework_used(gid, num=1):
    """使用烟花礼包成功之后调用：记次数 + 推 8086（客户端播烟花动画）。**best-effort**。

    ⚠ 由 `handlers_bag.handle_gift_bag_use` 调用，**绝不能让异常冒出去影响开包主流程**
      （本函数整体包在 try 里，调用方也再包一层）。
    积分不落盘（推导量）；`use_o/use_f` 才是真值。
    """
    try:
        gid = _i(gid)
        if gid not in (_O_ID, _F_ID):
            return 0
        st = _st()
        st['use_o' if gid == _O_ID else 'use_f'] = (
            _i(st.get('use_o' if gid == _O_ID else 'use_f'), 0) + max(1, _i(num, 1)))
        save_state()
        aid = _my_aid()
        if aid:
            try:
                import game_server as _gs
                _gs.push_account(aid, 8086, _out(gid, max(1, _i(num, 1))))
            except Exception:
                pass
        return 1
    except Exception as e:
        print('[FWS] note_firework_used 失败 gid=%s num=%s: %r' % (gid, num, e), flush=True)
        return 0
