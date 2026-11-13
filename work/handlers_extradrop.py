# -*- coding: utf-8 -*-
"""handlers_extradrop.py —— 额外掉落活动 1190 / 1300（新包 A0 第 11 批）。

**这是什么**：一个"**打怪掉兑换币 → 用兑换币换礼包**"的季节性活动。
客户端文案（`T41_CodeKey` 的 `LOCAL_UI_EXTRADROP_TIPS_DESC`，本节直接引自该表的 CHS 列）：
「【额外掉落，兑换好礼】在活动期间参与任意战斗可产生额外掉落，掉落的道具可用来兑换物品。
活动结束后，兑换道具会自动清空，请及时使用。」
两个号：**1190** = 活动信息（页面上那一列兑换栏位），**1300** = 点某个栏位兑换。

**修前的缺口**：两个号服务端都没有 ⇒ 落兜底 `[1,[[]]]`：
  · **1190** -> `msg[1..7]` 全空 ⇒ 兑换页**一个栏位都没有**、`ownNum`(我的兑换币)恒 0。
  · **1300** -> `msg[2]`(nil) 经 `error_verify` 得 **-1** ⇒ 客户端在 `prop[1]`/`prop[2]`/`prop[3]`
    上**对数字取索引**当场报错（这是 HANDOFF 里记的"缺一个就索引数字报错"）；`msg[4]`(nil) 同理
    ⇒ 即使兑换成功也会崩在回调里。

**数据源（本轮把之前记成"无源"的三格全部找到了出处）**
  · 兑换栏位 = `UG07_ExtraReward`(2 行)。列 `ID,GMId,Type,DropId,TradeProp,TradePropNum,TradeGoods,TradeTimes`：
    `ID`=**栏位 id**（客户端 1190 收下、1300 原样回传的就是它）、`TradeProp`=**兑换币的道具 id**、
    `TradePropNum`=**需要几个**、`TradeTimes`=**可兑换次数**（`-1` = 不限）。
  · ★**`DropId=10010` 指向 `G03_SpellGroup.GroupId`，不是 `T02_Reward`** —— 之前记的
    "`DropId=10010` 全表根解析不到"是因为**查错了表**（在 `T02_Reward` 里找 `GroupID=10010` 当然没有）。
    实测 `G03_SpellGroup` 里 `GroupId='10010'` 有 **4 行**，而且这 4 行的 `Desc` 列**自带明文**：
    「唤妖符x2、小仙桃x5、秘法强化魂石x2、铜钱x2000」—— 明文与四个结构化列
    `(GoodsType, ItemId, ItemCount)` **逐项吻合**（8/0/2、2/1013/5、2/2003/2、3/0/2000），
    所以这 4 行直接就是 1190 的 `rewardList` / 1300 要发的东西，**不需要任何推断**。
  · 描述文本 = `T41_CodeKey` 的 `LOCAL_UI_EXTRADROP_TIPS_DESC`。
  · 兑换币的"类型"(`type`，客户端 `IconData:create(itemId, type)` 要用) = 取 `T02_Reward` 里
    **同一个 `ObjectID` 已经用过的 `Type`**（1052 在 T02 里 10 次出现全部是 `Type=1`）——
    这是"按既有口径取"，不是猜。
  · ⚠ 另一张 `A08_ExtraReward`(21 行，`ID/ActivityId/Info/DropId`) 是**同一系统的另一半**：
    它是"**哪个季节活动掉什么**"（`Info` 是明文名：圣诞节/元旦/灯笼/鞭炮/…/七彩羽毛），
    `DropId` 指向 **`T02_Reward.GroupID`**（18001~18021，实测每组都有行）。
    ★两张表的 `DropId` 落在**两个不同的 id 空间**（UG07→`G03_SpellGroup`，A08→`T02_Reward`），
    这正是本项目反复踩的那类坑，别互相替代。本轮只实装 1190/1300 两个号（兑换侧）；
    **战斗掉落侧**（A08）要挂到战斗结算链上，属另一批（见 HANDOFF）。

**硬约束（逐条核对客户端）**
  1. ★1300 的 `msg[2]` 必须是**三元组** `[ucode, 静态id, 消耗数量]`，`msg[4]` 必须是**完整
     roleInfo 数组** —— 两个都走 `error_verify(nil)` -> **-1** -> 客户端对数字取索引**当场报错**。
     `msg[3]` 是 dropInfo（不能省）。
  2. ★1190 的 `msg[3]/msg[4]` 是**毫秒**（客户端 `* 0.001`）；`msg[7]` 的每行是 **6 槽**
     `[id, type, itemId, num, leftCount, rewardList]`，`rewardList` 每行是 **3 槽** `[type, itemId, num]`。
  3. 客户端会按 `num` **从大到小排序**（服务端顺序无所谓，但别指望它保持我们的顺序）。
  4. 错误码**客户端自己写好了**，直接照抄：`153` 活动结束或获取活动失败、`30501` 剩余次数不足、
     `30502` 道具不足。**不要另编码**。
  5. `TradeTimes = -1` = **不限次**（实测第 2 行就是 -1），不是"已用完"。
"""
import json
import time

from state import EQ_STATE, save_state, table_dir as _table_dir

_DATA = _table_dir()
_ERR_ACT = 153          # 活动结束或获取活动失败（客户端 m_errorCode[153]）
_ERR_TIMES = 30501      # 剩余次数不足
_ERR_PROP = 30502       # 道具不足


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


def _role_info():
    """唯一权威实现（全局护栏 R1 要求函数体里逐字出现 `role_info_array()`；别"转一手"）。"""
    from state import role_info_array
    return json.loads(role_info_array())


# ---------------------------------------------------------------- 静态表
_SLOTS = []       # [(slotId, gmid, dropId, propId, needNum, times)]
_SLOT_BY_ID = {}
_REWARDS = {}     # dropId -> [[type, itemId, num], ...]（来自 G03_SpellGroup 的 GroupId）


def _tables():
    if _SLOTS:
        return
    try:
        t = _load_json('UG07_ExtraReward.json')
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            sid = _i(r[h['ID']])
            if sid <= 0:
                continue
            rec = (sid, _i(r[h['GMId']]), _i(r[h['DropId']]),
                   _i(r[h['TradeProp']]), _i(r[h['TradePropNum']], 1), _i(r[h['TradeTimes']], -1))
            _SLOTS.append(rec)
            _SLOT_BY_ID[sid] = rec
    except Exception as e:
        print('[EXDROP] 读 UG07_ExtraReward 失败: %r' % (e,), flush=True)
    try:
        t = _load_json('G03_SpellGroup.json')
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            gid = _i(r[h['GroupId']])
            if gid <= 0:
                continue
            _REWARDS.setdefault(gid, []).append(
                [_i(r[h['GoodsType']]), _i(r[h['ItemId']]), _i(r[h['ItemCount']])])
    except Exception as e:
        print('[EXDROP] 读 G03_SpellGroup 失败: %r' % (e,), flush=True)


# 兑换币的"类型"：**必须走道具袋的那张表**（见下），不是"T02 里怎么用过它"
_LOOT_IDS = set()


def _prop_type(prop_id):
    """兑换币的 `type`（客户端 `IconData:create(itemId, type)` 用它选表渲染）。

    ★★这里踩过一个**同一 id 两张表**的坑，记下来免得下一轮又错：
      · `L01_Equipments[1052]` = 「踏雾冲虚剑」（**装备**）
      · `L02_Loots[1052]`      = 「人参果王」（**道具**）
      两者同号不同物。我第一版按"**取 `T02_Reward` 里同一 `ObjectID` 用过的 `Type`**"得到 **1**（装备），
      于是客户端会把 1052 渲染成一把剑，而服务端扣的/数的是 L02 的那个人参果王 ⇒ **图标与实物不符**。
      **判据只能是"兑换币进哪个袋"**：客户端 `SQProp:getObject(ucode)` 走的是
      `SDFactory:getObject(SD_OBJS.SDLoot, id)` —— `SD_OBJS.SDLoot` **就是 `L02_Loots`**
      ⇒ 这个袋里按 id 解析出来的是 **L02 的道具**，所以 `type` 取 **2（`bag.baseType.prop`）**。
      另外 `TradePropNum`（需要 5 / 2 个）只有在**可堆叠**时才讲得通，装备是一件一个 ucode —— 这条
      独立佐证了同一个结论。
    """
    if not _LOOT_IDS:
        try:
            t = _load_json('L02_Loots.json')
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                _LOOT_IDS.add(_i(r[h['ID']]))
        except Exception:
            pass
    return 2 if _i(prop_id) in _LOOT_IDS else 2      # 道具袋的口径；查不到也按 2（不在表里本来就不该发）


def _text(key, fallback=''):
    """按 codeKey 取文案（**先 T41_CodeKey 再 99_Language**，与 SDCodeKey 同序）。"""
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
    return val or fallback


def _window():
    """活动窗口 —— 与 1108 各活动行**同一份**口径（本活动的起止时间表里没有）。"""
    now = int(time.time())
    return now, now + 365 * 86400


# ---------------------------------------------------------------- 账号档
def _st():
    v = EQ_STATE.setdefault('extra_drop', {})
    if not isinstance(v, dict):
        v = EQ_STATE['extra_drop'] = {}
    if not isinstance(v.get('used'), dict):
        v['used'] = {}
    return v


def _own(prop_id):
    """我持有的兑换币数量（prop 袋的 ucode == 静态 id 的字符串，本工程既有口径）。"""
    prop = EQ_STATE.get('prop') or {}
    return _i(prop.get(str(_i(prop_id))), 0)


def _left(rec):
    """`leftCount`：`TradeTimes = -1` = **不限次**（回 -1 让客户端不去判 0）。"""
    times = _i(rec[5], -1)
    if times < 0:
        return -1
    return max(0, times - _i(_st()['used'].get(str(rec[0])), 0))


def _rows():
    """1190 的 `list`：每行 **6 槽** `[id, type, itemId, num, leftCount, rewardList]`。"""
    _tables()
    out = []
    for rec in _SLOTS:
        sid, _gm, drop_id, prop_id, need, _t = rec
        out.append([sid, _prop_type(prop_id), prop_id, need, _left(rec),
                    [list(x) for x in _REWARDS.get(drop_id, [])]])
    return out


# ============================================================================
# 1190 活动信息
# ============================================================================
def handle_exdrop_info(body_json=None):
    """1190 -> `[1,[jumpId, description, startMs, endMs, imgName, ownNum, list]]`。

    ⚠ `msg[3]/msg[4]` 是**毫秒**（客户端 `* 0.001`）；`msg[5]`（底图名）**表里没有** ⇒ 发空串
      （客户端自带默认值 `Activity_Recharge_HeiFengPo.png`，空串会走它的 `or` 分支）。
    """
    _tables()
    start, end = _window()
    jump = _i(_SLOTS[0][1]) if _SLOTS else 0
    prop_id = _i(_SLOTS[0][3]) if _SLOTS else 0
    desc = _text('LOCAL_UI_EXTRADROP_TIPS_DESC', '')
    return _out(jump, desc, start * 1000, end * 1000, '', _own(prop_id), _rows())


# ============================================================================
# 1300 兑换
# ============================================================================
def handle_exdrop_exchange(body_json):
    """1300 req `{id}` -> `[ownNum, [ucode, 静态id, 消耗数量], dropInfo, roleInfo]`。

    ★三槽的形状都是硬约束，见模块头第 1 条：`msg[2]`/`msg[4]` 走 `error_verify(nil) -> -1`
    ⇒ 客户端会对**数字**取索引当场报错。所以成功路径必须**四槽齐全**。
    错误码直接照抄客户端 `m_errorCode`：`153` / `30501` / `30502`。
    """
    from handlers_task import grant_one
    _tables()
    req = _req(body_json)
    sid = _i(req.get('id'))
    rec = _SLOT_BY_ID.get(sid)
    if rec is None:
        return b'[153]'                       # 客户端：活动结束或获取活动失败
    _sid, _gm, drop_id, prop_id, need, _times = rec
    if _left(rec) == 0:
        return b'[30501]'                     # 剩余次数不足
    have = _own(prop_id)
    if have < need:
        return b'[30502]'                     # 道具不足
    # 发奖（先发后扣：发不出去就别动道具，避免"扣了没货"）
    groups = []
    for (ty, oid, num) in _REWARDS.get(drop_id, []):
        groups += grant_one(_i(ty), _i(oid), _i(num))
    if not groups and _REWARDS.get(drop_id):
        return b'[153]'
    prop = EQ_STATE.setdefault('prop', {})
    prop[str(_i(prop_id))] = have - need
    st = _st()
    st['used'][str(sid)] = _i(st['used'].get(str(sid)), 0) + 1
    save_state()
    from handlers_game import _drop as _gd
    # ⚠ 三元组的第 3 位是**扣后的余量**，不是"消耗了几个"：客户端 `SQProp:updateItem(ucode, prop)`
    #   里 `_count = tonumber(_info[3])` 之后是 **直接把这个数当成新的总数**存进本地背包的
    #   （`self.mBagList[_ucode].count = _count`）。发"消耗数"会让客户端本地**多扣一次**
    #   （12 用 5 -> 本地显示 5 而不是 7）。⚠ 同一行上面还有一句 `doOutflow(..., id, count)`
    #   是打点，它按"消耗数"读同一个值 ⇒ **两者语义在客户端里就是冲突的**；这里以**背包状态**为准
    #   （打点错一位无所谓，背包错了玩家立刻看得见），且下一次 1190 会用服务端真值把它冲正。
    # ⚠ 槽位顺序就是客户端读的顺序：ownNum / 消耗三元组 / dropInfo / roleInfo。别调换。
    return _out(_own(prop_id), [str(_i(prop_id)), _i(prop_id), have - need], _gd(groups), _role_info())
