# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 背包操作处理器 (BAG_SELL 1178 / BAG_USE 1179 / GIFT_BAG_USE 1098 /
#   BAG_COMPOSE 1177 / PIECE_COMPOSE 1168) + 动态道具背包(1033)
#
# 客户端消息 (network/cultivate/NetBag*.lua + bag/VBag*.lua):
#   1178 BAG_SELL       req {sellList:[{type,staticId,ucode,num},..]} -> resp [1,[gold, sellList]]
#                       sellList=[[ucode,id,num,type],..]; 客户端按类型 equip_deleteItem/prop_deleteItem;
#                       gold=msg[1] -> gamer_setGoldCoin
#   1179 BAG_USE        req {ucode,staticId,num,type} (礼包) -> resp [1,[useGift, roleInfo, dropInfo, tipsid]]
#   1098 GIFT_BAG_USE   req {ucode} (周卡/月卡)           -> resp [1,[roleInfo, dropInfo]]
#   1177 BAG_COMPOSE    req {ucode,staticId,num,type} (元灵碎片31/技能碎片32) -> resp [1,[synthesislist, roleInfo, dropInfo]]
#                       synthesislist=[[ucode,id,num,type],..] 客户端 prop_deleteItem
#   1168 PIECE_COMPOSE  req {itemid,count} (怪物35/皮肤36/其他37/38碎片) -> resp [1,[prop, roleInfo, dropInfo, tipsid]]
#                       prop=[ucode,id,num] 客户端 prop_deleteItem(prop[1],prop[3])
import json, os, random
from state import EQ_STATE, save_state, BASE_EQUIP, BASE_PROP, get_monster_base
from state import alloc_monster_ucode, alloc_equip_ucode   # ★2026-10-02 (审计 §9 #3): 原子取号
from state import table_dir, load_table          # ★v32 表读取唯一入口
from config_data import _mon
from handlers_equip import _equip_item, _mon_with_state
from handlers_task import bump_task, CT_INTENSIFY, CT_SKILLCOMPOSE

_DATA = table_dir()      # ★v32: 由 state 从文件位置推导, 不再写死 <repo>\ygzz\...
# baseType 枚举 (bag\BagConfig.lua)
BT_EQUIP, BT_PROP, BT_MONSTER, BT_GHOST = 1, 2, 4, 5
# static_Type (bag.model) 用于区分合成类型
T_GHOST_PIECE = 31      # 元灵碎片 -> BAG_COMPOSE
T_SKILL_PIECE = 32      # 技能碎片 -> BAG_COMPOSE
T_MONSTER_PIECE = 35    # 怪物碎片 -> PIECE_COMPOSE
T_SKIN_PIECE = 36       # 皮肤碎片 -> PIECE_COMPOSE
T_OTHER_PIECE = (37, 38)  # 其他碎片 -> PIECE_COMPOSE

def _load_json(name):
    # ★v32: 改走 state.load_table —— 按尾字段 u32 长度精确剥离。
    #   旧写法 `c[:c.rfind(']')+1]` 在"正文长度低字节恰好是 0x5D(']')"的表上会切错
    #   (实测 L98_AIPriority.json), 而表读坏时这里会整块 except 掉 -> 静默拿到空字典。
    return load_table(name, _DATA)

# ---------- 静态数据: 装备(价格) / 道具(价格/类型) / 合成表 ----------
# L01_Equipments: ID -> SalePrice (IfSale)
_EQUIP_IDS = set()      # 全部合法装备静态id (礼包/宝箱装备发放白名单 = 全表)
_EQUIP_PRICE = {}
_EQUIP_QUALITY = {}     # ★2026-10-16（E/CT=20）：静态 id -> 品质（= L01.Class）
try:
    _l1 = _load_json('L01_Equipments.json')
    _h1 = {h: k for k, h in enumerate(_l1[0])}
    for _r in _l1[1:]:
        try:
            _eid = str(_r[_h1['ID']])
            _EQUIP_IDS.add(_eid)
            _sale = int(_r[_h1['SalePrice']] or 0)
            _ifs = int(_r[_h1['IfSale']] or 0)
            _EQUIP_PRICE[_eid] = (_sale if _ifs else 0)
            # ★2026-10-16（任务书 E / CT=20）：**品质就是 `L01_Equipments.Class`**。
            #   出处是客户端自己的静态类：`app/staticdata/SDEquip.lua:127`
            #   `function SDEquip:getStatic_Quality() return self.m_Class`，
            #   且同文件 `getColorByQuality` 把 1..5 映射成 White/Green/Blue/**Purple**/**Red**。
            #   `T37_DailyTasks` 里 CT=20「极品装备：获得1件紫色以上质量装备」的 `Value='4|5'`
            #   正是这两档 ⇒ 判据不需要猜。
            try:
                _EQUIP_QUALITY[_eid] = int(_r[_h1['Class']] or 0)
            except Exception:
                pass
        except Exception:
            continue
except Exception:
    pass


_CT20_MIN_QUALITY = 4      # T37 CT=20 的 Value='4|5'（SDEquip.getColorByQuality: 4=Purple 5=Red）


def note_equip_gained(static_id):
    """装备入包时调用：品质 ≥4（紫/红）就计一次 T37 CT=20「极品装备」。

    ⚠ 这是**唯一**的装备入包计数点：三处造装备的地方（`grant_entries` / 
      `handlers_game._grant_reward` / `handlers_game._grant_content_row`）都调它，
      免得再出现"两套实现一全一半"。
    """
    try:
        _q = _EQUIP_QUALITY.get(str(int(static_id)), 0)
    except Exception:
        return
    if _q < _CT20_MIN_QUALITY:
        return
    try:
        from handlers_task import bump_task as _bt, CT_EQUIP_QUALITY as _CT
        _bt(_CT, 1)
    except Exception as _e:
        print('[CT20] !! 极品装备计数失败(不影响入包): %r' % (_e,), flush=True)


# L02_Loots: ID -> (Type, SalePrice, IfSale, IfUse, IfCompose, IfView)
_LOOT = {}
try:
    _l2 = _load_json('L02_Loots.json')
    _h2 = {h: k for k, h in enumerate(_l2[0])}
    for _r in _l2[1:]:
        try:
            _lid = str(_r[_h2['ID']])
            _type = int(_r[_h2['Type']] or 0)
            _sale = int(_r[_h2['SalePrice']] or 0)
            _ifs = int(_r[_h2['IfSale']] or 0)
            _ifu = int(_r[_h2['IfUse']] or 0)
            _ifc = int(_r[_h2['IfCompose']] or 0)
            _ifv = int(_r[_h2['IfView']] or 0)
            _LOOT[_lid] = (_type, (_sale if _ifs else 0), _ifs, _ifu, _ifc, _ifv)
        except Exception:
            continue
except Exception:
    pass
# L05_ScrowPiece: 残章(books) 合成 —— 本版本不主动 seed, 保留接口
_SCROW = {}
try:
    _l5 = _load_json('L05_ScrowPiece.json')
    if type(_l5) is list and len(_l5) > 1:
        _h5 = {h: k for k, h in enumerate(_l5[0])}
        for _r in _l5[1:]:
            try:
                _sid = str(_r[_h5['ID']])
                _SCROW[_sid] = _r
            except Exception:
                continue
except Exception:
    pass
# L07_Item_Compose: ItemID -> (IteamNum, GroupID, Type, ComposeRate, ComposeID, ReturnID, ReturnNum)
_COMPOSE = {}
try:
    _l7 = _load_json('L07_Item_Compose.json')
    _h7 = {h: k for k, h in enumerate(_l7[0])}
    for _r in _l7[1:]:
        try:
            _id = str(_r[_h7['ItemID']])
            _n = int(_r[_h7['IteamNum']] or 1)
            _g = int(_r[_h7['GroupID']] or 0)
            _t = int(_r[_h7['Type']] or 0)
            _rate = str(_r[_h7['ComposeRate']] or '')
            _cids = [int(x) for x in str(_r[_h7['ComposeID']] or '').split('|') if x.strip().isdigit()]
            _rid = str(_r[_h7['ReturnID']] or '-1')
            _rn = str(_r[_h7['ReturnNum']] or '0')
            _COMPOSE[_id] = (_n, _g, _t, _rate, _cids, _rid, _rn)
        except Exception:
            continue
except Exception:
    pass

# L03_Equipghost: id -> SalePrice (元灵)
_GHOST_PRICE = {}
# L03_Equipghost: ScrapID(元灵碎片静态id) -> (元灵id, ScrapQuantity 所需碎片数)
#   客户端 VBagComposite:itemType==31 时 countUnit = L03.ScrapQuantity -> BAG_COMPOSE(type=31, num=count*unit)
_GHOST_BY_SCRAP = {}
try:
    _l3 = _load_json('L03_Equipghost.json')
    _h3 = {h: k for k, h in enumerate(_l3[0])}
    for _r in _l3[1:]:
        try:
            _gid = str(_r[_h3['ID']])
            _sale = int(_r[_h3['SalePrice']] or 0)
            _ifs = int(_r[_h3['IfSale']] or 0)
            _GHOST_PRICE[_gid] = (_sale if _ifs else 0)
            _scr = str(_r[_h3['ScrapID']] or '0')
            _q = int(_r[_h3['ScrapQuantity']] or 0)
            if _scr != '0' and _q > 0:
                _GHOST_BY_SCRAP[_scr] = (str(_gid), _q)
        except Exception:
            continue
except Exception:
    pass

# L10_Gift: GiftID -> [(GoodsType, GoodsID, Num, Power, PowerSum), ..]
_GIFT_CONTENTS = {}
try:
    _lg = _load_json('L10_Gift.json')
    _hgl = {h: k for k, h in enumerate(_lg[0])}
    for _r in _lg[1:]:
        try:
            _gid = str(_r[_hgl['GiftID']])
            _gt = int(_r[_hgl['GoodsType']] or 0)
            _goods = str(_r[_hgl['GoodsID']] or '-1')
            _num = int(_r[_hgl['Num']] or 1)
            _pw = int(_r[_hgl['Power']] or 0)
            _GIFT_CONTENTS.setdefault(_gid, []).append((_gt, _goods, _num, _pw))
        except Exception:
            continue
except Exception:
    pass

# ---- 2026-09-07 商城礼包权威内容源: G03_SpellGroup (GroupId = G02 商品行.GoodsId) ----
#   互证链: G02_Shop(GoodsType 10/98 商品行) -> GoodsId -> G03.GroupId 行集合 (客户端界面
#   SDShop:getStatic_SpellGroupDesc 与服务器发放同源; 审计生成器 scripts/gen_pack_audit.py
#   -> build/gift_matrix.md 含"G03 原始行 <-> 静态表物品名 <-> 服务器发放格式"对照)。
#   组内分类: 全部行 Desc 相同且行数>1 -> 概率组 (物品行按 Prob 权重互斥抽 1 行, 货币类行固定);
#   其余 -> 全部固定发放 (G03 Prob 0/100 均表示"含于礼包", 私服全发)。
#   L10_Gift(2038-2051 背包礼包卡, Power/PowerSum 权重) 为第二源, 与 G03 无交集, 互补。
def _g03_gift_map():
    m_fixed, m_roll = {}, {}
    try:
        t = _load_json('G03_SpellGroup.json')
        h3 = {h: k for k, h in enumerate(t[0])}
        g = {}
        for _r in t[1:]:
            try:
                g.setdefault(str(_r[h3['GroupId']]), []).append(_r)
            except Exception:
                continue
        for _gid, rows in g.items():
            # G03 每行 Desc 均为该组完整礼包文案(复制), 不可作分类判据; 唯一概率信号 = Probe<100 的行
            #   概率组: 8023 皮肤/碎片 (Prob 1/10/50/1; 货币行如铜钱 Prob=100 固定) --- 其余全部固定发放
            items_roll = []
            for _r in rows:
                _gt = str(_r[h3['GoodsType']])
                _prob = int(_r[h3['Probability']] or 0)
                if _gt in ('3', '7', '8', '9', '16', '12', '21', '22'):
                    m_fixed.setdefault(_gid, []).append(
                        (int(_gt), str(_r[h3['ItemId']]), int(_r[h3['ItemCount']] or 1)))
                elif _prob > 0 and _prob < 100:
                    items_roll.append(_r)
                else:
                    m_fixed.setdefault(_gid, []).append(
                        (int(_gt), str(_r[h3['ItemId']]), int(_r[h3['ItemCount']] or 1)))
            if items_roll:
                for _r in items_roll:
                    m_roll.setdefault(_gid, []).append(
                        (int(str(_r[h3['GoodsType']]) or 0), str(_r[h3['ItemId']]),
                         int(_r[h3['ItemCount']] or 1), int(_r[h3['Probability']] or 0)))
    except Exception:
        pass
    return m_fixed, m_roll

_GIFTS_FIXED, _GIFTS_ROLL = _g03_gift_map()

# ---- ★★2026-09-12 (v26) 回退: 本表已置空。 ----
#   旧内容 (8004 大蟠桃x3/女儿红x1、8005 妖魂x200/唤妖符x1、8006 唤妖符x20、8007 古秘法天书x2/仙寿桃x3)
#   的注释声称出自 "G02 商品卡文案", 但逐表溯源 (_audit_gift_extra_src.py) 证明**注释不成立**:
#     8004 的追加 = G03 组 **8003** 的行 (女儿红/大蟠桃只出现在 8003);
#     8007 的追加 = G03 组 **8006** 的行 (古秘法天书/仙寿桃只出现在 8006)。
#   根因: G02_Shop 里**不同 GoodsId 可以共用同一个 Tittle** —— 商品 656/803 都叫"每周进阶礼包"
#         但分别是 GoodsId 8003 / 8004。照**标题**对齐就会整体错位一格, 于是"每周进阶礼包"
#         被贴上了 8003 的内容, 8005/8006/8007 依次串味。
#   → 对齐只能 `G02.GoodsId -> G03_SpellGroup.GroupId`, 绝不能用标题。G03 行是内容唯一权威。
#   影响面: 商品 804/805/806/807 (PriceType=2 妖魂价, 游戏内可直接买到) = 买 1 个礼包多拿 1~2 组物品。
_GIFT_EXTRA = {}
SKIN_DUP_FRAGMENT = 10      # 重复皮肤 -> 皮肤碎片 (L02 5000101, 50片合1) 数量

def _grant_skin_with_dup(sid):
    # 解锁皮肤; 若已拥有 -> 转皮肤碎片 (返回 (is_dup, groups))
    sid = int(sid)
    if str(sid) in (EQ_STATE.get('skin_owned') or {}):
        EQ_STATE.setdefault('prop', {})['5000101'] = int(EQ_STATE['prop'].get('5000101', 0)) + SKIN_DUP_FRAGMENT
        EQ_STATE.setdefault('prop_id', {})['5000101'] = 5000101
        return True, [[2, [['5000101', 5000101, SKIN_DUP_FRAGMENT]]]]
    EQ_STATE.setdefault('skin_owned', {})[str(sid)] = 1
    _grant_skin_to_monsters(sid)
    return False, [[14, [[sid]]]]

# ---- L98_Monsterskin: skinid -> 怪阶段族 (MonsterID "100501|100502|.." 阶段序列) ----
_L98_FAM = {}
try:
    _l98 = _load_json('L98_Monsterskin.json')
    _h98 = {h: k for k, h in enumerate(_l98[0])}
    for _r in _l98[1:]:
        try:
            _sid = str(_r[_h98['ID']])
            _L98_FAM[_sid] = set(x for x in str(_r[_h98['MonsterID']] or '').split('|') if x.strip())
        except Exception:
            continue
except Exception:
    pass

def _grant_skin_to_monsters(sid):
    # 皮肤发放 -> 按 L98 MonsterID 族匹配, 解锁给当前拥有的该族怪 (1500 皮肤列表可见/1501 可穿戴)
    fam = _L98_FAM.get(str(sid))
    if not fam:
        return
    for uc in list(EQ_STATE.get('monsters', {}).keys()) + ['1', '2', '3']:
        b = get_monster_base(uc)
        if not b:
            continue
        if str(b[0]) in fam:
            _lst = EQ_STATE.setdefault('monster_skin_open', {}).setdefault(str(uc), [])
            if int(sid) not in _lst:
                _lst.append(int(sid))
                print('[SKIN] unlock skinid=%s -> mon %s (mid=%s)' % (sid, uc, b[0]), flush=True)

# ---- 石敢当系列回退: G03 8003-8007 已含 (Type4 1200001/1200003/1200004), 与 G03 同源无需追加;
#      此段仅存供回退 (G03 读表失败时的最后防线) ----
for _gid, _mid in (('8003', 1200001), ('8004', 1200001), ('8005', 1200001),
                   ('8006', 1200003), ('8007', 1200004)):
    _lst = _GIFTS_FIXED.setdefault(str(_gid), [])
    if not any(c[0] == 4 and str(c[1]) == str(_mid) for c in _lst):
        _lst.append((4, str(_mid), 1))

# ---------- roleInfo ----------
# ★2026-09-13 (v32) 删除本文件里那份 **字段不一致** 的 role_info_array。
#   它把体力硬编码成 20、等级硬编码成 1、经验硬编码成 0, 而 handlers_sys 那份是把妖塔层数硬编码成 1;
#   由于 game_server 的 import 顺序, 本文件**内部**5 处调用走的是这一份 ->
#   "用礼包/合成后客户端等级显示回 1、体力显示回 20"。
#   现在唯一权威实现 = state.role_info_array() (通过 `from state import *` 取得)。
#   保留一个显式别名只为可读性, 不再重复实现。
from state import role_info_array  # noqa: E402  (唯一权威实现; 见 state.role_info_array 注释)

# ---------- 道具背包(1033)动态生成 ----------
def prop_bag_json():
    # 动态道具背包: 反映 EQ_STATE['prop'] 真实数量 (ucode -> count), id 由 prop_id 取
    items = []
    # ★2026-11-14: 同 1030 —— 缺 `prop` 键时旧写法会 KeyError，客户端卡加载（第二道闸）。
    _prop = EQ_STATE.get('prop') or {}
    for uc in sorted(_prop.keys(), key=int):
        cnt = _prop[uc]
        if cnt <= 0:
            continue
        pid = EQ_STATE.get('prop_id', {}).get(uc)
        if pid is None:
            # 兼容旧状态: 仅 ucode 无 id 时, id=1001 (既有默认道具)
            pid = 1001
        items.append('["%s",%d,%d]' % (uc, pid, cnt))
    return '[1,[[' + ','.join(items) + '],"1/1"]]'

def ghost_bag_json():
    # 动态元灵背包(1031): item=[ucode,id,count,isload] (SQGhost:insertItem)
    items = []
    for g in EQ_STATE.get('ghost', []):
        try:
            ucode, gid, cnt, isload = str(g[0]), int(g[1]), int(g[2]), int(g[3])
        except Exception:
            continue
        if cnt > 0:
            items.append('["%s",%d,%d,%d]' % (ucode, gid, cnt, isload))
    return '[1,[[' + ','.join(items) + '],"1/1"]]'

def _prop_price(pid):
    pid = str(pid)
    if pid in _LOOT:
        return _LOOT[pid][1]
    return 0

def _equip_price(eid):
    eid = str(eid)
    if eid in _EQUIP_PRICE:
        return _EQUIP_PRICE[eid]
    return 0

# ---------- 礼包/周卡月卡 GIFT_BAG_GET (1097) ----------
def gift_bag_get_json():
    # 礼包/卡列表: msg[1]=[[ucode,type,id,count],..] (NetGiftBagGet:recvMessage)
    items = []
    for g in EQ_STATE.get('giftbag', []):
        try:
            ucode, btype, gid, cnt = str(g[0]), int(g[1]), int(g[2]), int(g[3])
        except Exception:
            continue
        if cnt > 0:
            items.append('["%s",%d,%d,%d]' % (ucode, btype, gid, cnt))
    return '[1,[[' + ','.join(items) + ']]]'

def _giftbag_row(ucode, btype=None):
    """在礼包袋里按 ucode 找那一行 `[ucode, type, 静态id, count]`；找不到 -> None。"""
    gb = EQ_STATE.get('giftbag')
    if not isinstance(gb, list) or not ucode:
        return None
    for g in gb:
        if len(g) < 4 or str(g[0]) != str(ucode):
            continue
        if btype is not None and int(g[1]) != int(btype):
            continue
        return g
    return None


def _use_giftbag(ucode, btype, gid, num):
    """从礼包袋里扣 `num` 个，返回**实际扣减数**（0 = 什么都没扣）。

    ★★2026-10-16（任务书 B①）：修前这一行有三个洞（`g[3] = max(0, int(g[3]) - num)`）：
      ① **`num=-1` 会给库存 +1**（`max(0, count+1)`）—— 负数把礼包越用越多；
      ② 不看 `btype`、也不核 `staticId` 与 ucode 的绑定 ⇒ 拿 A 礼包的 ucode 配 B 的
         `staticId`，服务端就按 B 发奖（**按请求发奖而不是按库存发奖**）；
      ③ 没有"最多扣到有"的上界校验：`num > count` 时被 `max(0,…)` 夹平，玩家白亏。
      ⇒ 现在：必须 `num>=1`、`num<=count`、行类型 == `btype`、行静态 id == `gid`，
        任一不符就**一个都不扣**（调用方按失败回 `[0]`）。
    """
    try:
        num = int(num)
    except (TypeError, ValueError):
        return 0
    if num <= 0:
        return 0
    g = _giftbag_row(ucode, btype)
    if g is None or int(g[2]) != int(gid):
        return 0
    have = int(g[3] or 0)
    if num > have:
        return 0
    g[3] = have - num
    return num

def _apply_gift_rewards(gid):
    # 按礼包内容发放 (2026-09-07 G03 权威源):
    #   源优先级: G03_SpellGroup(商城直购礼包; 固定全发 + 概率组互斥抽1) > L10_Gift(背包礼包卡2038-2051,
    #   行含 Power/PowerSum -> 加权抽取单行, 全0 -> 兼容旧行为全部发放)
    # 返回 dropInfo(str), 并把道具/装备/皮肤/宝箱/头像框等加入对应背包(持久)
    gid = str(gid)
    is_g03 = gid in _GIFTS_FIXED or gid in _GIFTS_ROLL
    if is_g03:
        contents = []
        roll = _GIFTS_ROLL.get(gid)
        if roll:
            # 概率组先互斥抽 1 行 (权重=Probability; 全0等权; 对应"有概率获得X/碎片"文案如 8023)
            weights = [int(r[3]) for r in roll]
            wsum = sum(weights)
            if wsum <= 0:
                pick = random.choice(roll)
            else:
                x = random.randrange(wsum)
                pick = roll[0]
                for r in roll:
                    if x < int(r[3]):
                        pick = r
                        break
                    x -= int(r[3])
            contents.append((pick[0], pick[1], pick[2], 0))
            print('[GIFT] %s roll -> gt=%s oid=%s num=%s' % (gid, pick[0], pick[1], pick[2]), flush=True)
        contents += [(gt, oid, num, 0) for (gt, oid, num) in _GIFTS_FIXED.get(gid, [])]
        # 2026-09-08: 文案补发 (G02 卡面"礼包内含/额外赠"内容; 与 _shop_item 确认框 goodslist 同步)
        contents += [(gt, oid, num, 0) for (gt, oid, num) in _GIFT_EXTRA.get(gid, [])]
        total = 0            # G03 源: 固定全发, 不再做加权二次抽取
    else:
        contents = _GIFT_CONTENTS.get(gid) or []
        total = sum(int(c[3]) for c in contents)
    if not contents:
        return '[]'
    if total > 0:
        roll = random.randrange(total)
        pick = contents[0]
        for c in contents:
            if roll < int(c[3]):
                pick = c
                break
            roll -= int(c[3])
        contents = [pick]
    # ★★v53 (2026-09-13) "抽到但这次一个都没入账" ≠ "礼包没有内容" —— 必须分开报。
    #   现场: G02#2050 妖刀皮肤礼包(80 妖魂) 的内容表里有一条 (9,'-1',5,权重1000) = 体力+5;
    #   玩家体力已满(20)时 grant_entries 会 skip 它 -> groups 空 -> 这里回哨兵 '[]'
    #   -> handlers_game._grant_reward 判成"礼包未实现" -> 商城回 [0,{}] "获取数据失败"
    #   —— 而**妖魂在扣费那一步已经扣掉了**。实测 80 次里 7 次命中, 玩家白掏钱还看报错。
    #   改法: 只要"内容表里有内容"(contents 非空), 抽到溢出就回**合法的空掉落** [[],[]],
    #   由上层按"购买成功、本次无入账"处理 (与 grant_entries 的 v32 口径一致: 不虚报飘字)。
    _di = grant_entries(contents)
    if _di == '[]':
        print('[GIFT] %s 抽到的内容本次全部未入账 (例: 体力已满), 按"成功但空掉落"返回' % gid, flush=True)
        return '[[],[]]'
    return _di

# ---------------------------------------------------------------------------
# ★2026-09-13 (v32) grant_entries —— **全工程唯一的"按类型发放"实现**
# ---------------------------------------------------------------------------
# 背景: 发放逻辑此前有两份 if-elif —— 礼包那份(_apply_gift_rewards, 下面这段)和
#       邮件那份(handlers_sys.handle_mail_receive)。
#       邮件那份只实现了 1/2/3/6/7/9/18, 于是:
#         · type=4/5/14/22 (妖怪/元灵/皮肤/头像框) 被**静默丢弃**, 邮件却已被标记"已领取"
#         · type=1 (装备) 用 `max(equip_id)+1` 当 ucode, 却用 `len(equip_id)` 当 lv/exp 的键
#           -> 键不连续时(生产存档 54 个键散布 12..125, 71 个缺号, 必然不连续)装备永远没有等级行
#       合并成一份之后, 邮件发放自动获得与礼包完全一致的语义与静态表校验。
#
# 与旧写法的两处**行为变化** (都是有意的):
#   ① 不再静默 continue: 被跳过的条目会记进 _GRANT_SKIPPED 并打一行 WARN, 便于运营侧定位
#      "为什么发了没到账"。旧写法的 except: continue 是"发了但没入账"最难受的形态。
#   ② 返回 dropInfo 的形状不变 ('[[],[ [style,[goods...]], ... ]]'), 调用方无需改。
_GRANT_SKIPPED = []          # [(type, goods, 原因)] 最近一次发放被跳过的条目
_GRANT_MAILED = 0            # 最近一次发放里**被转成邮件**的只数（>0 = 东西没丢，只是走了邮件）
#   ★★2026-10-16（B 复核 4）：调用方必须能区分"没发出去"和"发不进去但转邮件了"。
#   修前 `1166/1139` 只看 dropInfo 里有没有行 —— 妖怪背包满时 dropInfo 是空的，
#   于是它们**退款**，而妖怪其实已经进了邮件 ⇒ **双份**（退款 + 邮件里的怪）。

def _monster_room(need=0):
    """怪物背包**现在还能放几只**（`need` 只是给调用方读的自解释参数）。

    上限出处：`handlers_draw.MAX_BAG = 21`，注释写明对应客户端 `SQGamer:getMaxMonster()`。
    ★2026-10-16（B 复核）：修前 `grant_entries` 的 type=4 **完全不看上限** ——
      邮件/礼包的妖怪会一路加进 `monsters`，而客户端只按 21 只/一页渲染，
      超出的那几只**看不见也用不了**（"入账了但玩家拿不到"的典型）。商店那两条已经用
      回包的 `num` 槽提示"妖怪背包已满"，礼包/邮件这条却没有任何提示。
    """
    try:
        from handlers_draw import MAX_BAG as _MAX
    except Exception:
        _MAX = 21
    # ⚠ 口径必须与客户端**同一套**：背包"只数" = 内置种子怪（`_MONSTER_BASE`）
    #   + `monsters` 里造出来的 —— `state.all_monster_ucodes()` 正是两者之和，
    #   抽卡那条路（`handlers_draw` 的 `count`）用的也是它。
    #   ★2026-10-16 实测踩到：早先这里只数 `monsters`，于是"3 只种子 + 18 只造出"时
    #     room 仍算 3 —— 与抽卡口径打架，两只手各算各的。
    try:
        from state import all_monster_ucodes as _amu
        _have = len(_amu())
    except Exception:
        _have = len(EQ_STATE.get('monsters') or {})
    return int(_MAX) - int(_have)


def _gift_monster_need(gid, times=1):
    """`gid` 礼包**确定性内容**里 type=4 的只数 × `times`（随机组按"最小值"折算）。

    ⚠ 只有"每次开包都必然发"的内容才算得准：L10 那种 `total>0` 的礼包是从多行里**抽一行**，
      怪物的只数是随机的 ⇒ 这里取候选行里的**最小值**（可能为 0）。这样只在"无论抽到哪一行
      都放不下"时才拒绝，不会因为一个可能抽到别的行的礼包把玩家挡在门外。
    """
    gid = str(gid)
    try:
        times = max(1, int(times))
    except (TypeError, ValueError):
        times = 1
    rows = []
    if gid in _GIFTS_FIXED or gid in _GIFTS_ROLL:
        fixed = [(int(a), b, c) for (a, b, c) in _GIFTS_FIXED.get(gid, [])]
        extra = [(int(a), b, c) for (a, b, c) in _GIFT_EXTRA.get(gid, [])]
        det = sum(int(c) for (a, _b, c) in fixed + extra if a == 4)
        roll = _GIFTS_ROLL.get(gid) or []
        if roll:
            det += min(int(r[2]) if int(r[0]) == 4 else 0 for r in roll)
        rows.append(det)
    else:
        contents = _GIFT_CONTENTS.get(gid) or []
        total = sum(int(c[3]) for c in contents)
        if total > 0 and contents:
            rows.append(min(int(c[2]) if int(c[0]) == 4 else 0 for c in contents))
        else:
            rows.append(sum(int(c[2]) for c in contents if int(c[0]) == 4))
    return (rows[0] if rows else 0) * times


def mail_overflow_monsters(mids, why='妖怪背包已满'):
    """把**放不进背包**的妖怪投递成邮件，返回投递只数（0 = 投递失败/无需投递）。

    ★2026-10-16（B 复核 2）：客户端在背包满时的提示就是"赶快去邮件里领取"
      （`CallCard.lua:677-681`，触发条件是抽卡回包第 10 槽 `count > 0`），
      所以邮件这条通道**必须真的有东西**。实现走 `handlers_sys` 的既有写原语
      （与 `handlers_cardboss._mail_rank_reward` 同一套）：
        读 `gm_mail.json` -> 追加 -> `gm_mail_write` 原子落盘。
      附件类型 4（妖怪）在 `MAIL_ITEM_TABLE` 白名单里（`L91_Status.Name`），
      一封最多 `MAIL_AFFIX_MAX = 5` 个附件，超了分封。
    """
    mids = [int(m) for m in (mids or []) if str(m).lstrip('-').isdigit()]
    if not mids:
        return 0
    try:
        import handlers_sys as _hs
        import json as _json
        try:
            with open(_hs._GM_MAIL_PATH, 'rb') as _f:
                cfg = _json.loads(_f.read().decode('utf-8-sig'))
            if not isinstance(cfg, list):
                cfg = []
        except FileNotFoundError:
            cfg = []
        used = set()
        for c in cfg:
            try:
                used.add(int(c.get('id')))
            except Exception:
                pass
        cap = max(1, int(getattr(_hs, 'MAIL_AFFIX_MAX', 5)))
        sent = 0
        for k in range(0, len(mids), cap):
            chunk = mids[k:k + cap]
            mid0 = 9300
            while mid0 in used:
                mid0 += 1
            used.add(mid0)
            cfg.append({'id': mid0,
                        'title': '背包装不下（%s），自动转邮件' % why,
                        'type': 1, 'read': 0,
                        'items': [[4, int(m), 1] for m in chunk]})
            sent += len(chunk)
        _hs.gm_mail_write(cfg)
        print('[BAG] %s: %d 只妖怪已投递成邮件' % (why, sent), flush=True)
        return sent
    except Exception as _e:
        print('[BAG] !! 背包已满但邮件投递失败（这 %d 只妖怪**没发出去**）: %r'
              % (len(mids), _e), flush=True)
        return 0


def grant_monsters(mid, n=1):
    """把 `n` 只 `mid` 放进怪物背包：返回 `(可展示的 30 字段行, 已转邮件的只数)`。

    ★★2026-10-16（B 复核 2）：**"造怪"从此只有这一处实现**（背包上限 + 溢出转邮件）。
      修前工程里有 4 条造怪路径（`grant_entries` / `_grant_reward` / `_grant_content_row` /
      `handlers_draw` 抽卡），`state.py:684` 的注释记着这四条曾经各写各的、漏登记 `monster_ueq`；
      本轮又发现它们对"背包上限 21"的态度也各不相同 —— 抽卡会越界塞、宝箱/商城完全不看上限。
      ⇒ 上限判定与转邮件收口到本函数，调用方只负责展示返回的行。
    """
    try:
        n = max(1, int(n))
    except (TypeError, ValueError):
        n = 1
    rows, over = [], []
    for _ in range(n):
        if _monster_room() <= 0:
            over.append(mid)
            continue
        try:
            uc = _add_monster(mid)
            info = json.loads(_mon_with_state(uc)) if uc else None
            if info:
                rows.append(info)
            else:
                over.append(mid)
        except Exception as _e:
            import traceback as _tb
            _tb.print_exc()
            skip_note = '入包异常 %r' % _e
            print('[GRANT] SKIP type=4 goods=%s -> %s' % (mid, skip_note), flush=True)
            over.append(mid)
    mailed = mail_overflow_monsters(over) if over else 0
    return rows, mailed


def grant_entries(contents):
    """按 [(类型, 静态id, 数量, 权重?)] 发放到各背包, 返回 dropInfo 字符串。

    contents 的元素可以是 3 或 4 元组 (第 4 位是礼包权重, 本函数不用)。
    类型枚举 = 客户端 app/public/IconData.lua 的 REWARD_NEWTYPE。
    """
    global _GRANT_SKIPPED, _GRANT_MAILED
    _GRANT_SKIPPED = []
    _GRANT_MAILED = 0
    groups = []           # [[style, goodslist], ..]

    def skip(gt, goods, why):
        _GRANT_SKIPPED.append((gt, goods, why))
        print('[GRANT] SKIP type=%s goods=%s num=? -> %s' % (gt, goods, why), flush=True)

    for entry in contents:
        gt, goods, num = int(entry[0]), entry[1], (entry[2] if len(entry) > 2 else 1)
        try:
            if gt == 1:                        # 装备 (L01 全表合法 id)
                eid = int(goods) if str(goods).isdigit() else 0
                if eid <= 0 or str(eid) not in _EQUIP_IDS:
                    skip(gt, goods, '不在 L01_Equipments 全表白名单内')
                    continue
                # ★★2026-10-16（任务书 B③）：**按 num 逐件发**。修前无论 num 是几都只发 1 件
                #   —— "邮件里写了 3 件装备、到手 1 件"，且客户端只显示 1 件所以没人发现。
                try:
                    _cnt = max(1, int(num))
                except (TypeError, ValueError):
                    _cnt = 1
                _rows = []
                for _ in range(_cnt):
                    ucode = _next_equip_ucode()
                    EQ_STATE['equip_id'][ucode] = eid
                    EQ_STATE['lv'][ucode] = 1
                    EQ_STATE['exp'][ucode] = 0
                    EQ_STATE['equip_isload'][ucode] = 0
                    # ★ 装备图鉴 (1072/1073): 拾取即解锁。旧邮件分支漏了这一步 -> 邮件发的装备不进图鉴。
                    try:
                        from state import note_manual_unlock as _nmu
                        _nmu(eid)
                    except Exception:
                        pass
                    # ★2026-10-16（E / CT=20）：极品装备（Class>=4 = 紫/红）计数
                    note_equip_gained(eid)
                    # ★v32 修 D7: goodslist 的元素 = goodsInfo 本身。
                    #   客户端 NetDropRewardData style==1: `for _,goodsInfo in pairs(goodslist)` 后
                    #     equip_insertItem(goodsInfo[1], goodsInfo) + drop.id = tonumber(goodsInfo[2])
                    #   所以 goodsInfo 必须是**14字段装备行本尊**。
                    _rows.append(_equip_item(ucode))
                groups.append([1, _rows])
            elif gt == 2:                      # 道具
                pid = int(goods) if str(goods).isdigit() else 0
                if pid <= 0:
                    skip(gt, goods, '静态 id 非法')
                    continue
                ucode = str(pid)
                EQ_STATE['prop'][ucode] = EQ_STATE['prop'].get(ucode, 0) + max(1, num)
                EQ_STATE['prop_id'][ucode] = pid
                groups.append([2, [[ucode, pid, max(1, num)]]])
            elif gt == 3:                      # 铜钱
                EQ_STATE['gold'] += int(num)
                groups.append([3, [[int(num)]]])
            elif gt == 4:                      # 怪物 (GoodsID=静态id; 客户端 style4 goodslist=30字段item)
                mid = int(goods) if str(goods).isdigit() else 0
                if mid:
                    # ★★2026-10-16（任务书 B③ + B 复核 2）：按 num 逐只发（修前恒发 1 只）；
                    #   上限与"溢出转邮件"收口到 `grant_monsters`（造怪只有那一处实现）。
                    _mrows, _mailed = grant_monsters(mid, num)
                    if _mrows:
                        groups.append([4, _mrows])
                    if _mailed:
                        _GRANT_MAILED += int(_mailed)
                        skip(gt, goods, '背包已满，%d 只已转邮件' % _mailed)
                else:
                    skip(gt, goods, '静态 id 非法')
            elif gt == 6:                      # 鬼道积分 (style6; 与宝箱 1061 共用 kidou)
                EQ_STATE['kidou'] = int(EQ_STATE.get('kidou', 0)) + int(num)
                groups.append([6, [[int(num)]]])
            elif gt == 7:                      # 妖魂
                EQ_STATE['soulcoin'] += int(num)
                groups.append([7, [[int(num)]]])
            elif gt == 8:                      # 唤妖符
                EQ_STATE.setdefault('draw', {}).__setitem__('ticket', EQ_STATE['draw'].get('ticket', 0) + int(num))
                groups.append([8, [[int(num)]]])
            elif gt == 9:                      # 体力 (★2026-09-12 v26 P1: 真入账, 不再只飘字)
                # 旧实现只往 dropInfo 塞 [9,[[num]]] -> 客户端飘字"体力+5"但服务端一点没加
                #   (铁律 #28 "展示了但没入账" 的典型)。这里与 handlers_game.py:252 商城直购
                #   保持**同一上限语义**: min(20, 现有 + num)。
                #   体力载体 = 道具 1006 (roleInfo 第5字段 / 1074 体力信息 / 探索消耗全部读它)。
                #   ★v32: dropInfo 报**实际入账量**; 已到上限(实际 0)时**整条不报** ——
                #     否则客户端会飘"体力+N"而服务端一个没加, 又变回反过来的那种谎。
                try:
                    _vn = max(1, int(num))
                except Exception:
                    _vn = 1
                _before = int((EQ_STATE.get('prop') or {}).get('1006', 0))
                _after = min(20, _before + _vn)
                EQ_STATE.setdefault('prop', {})['1006'] = _after
                EQ_STATE.setdefault('prop_id', {})['1006'] = 1006
                _gain = _after - _before
                if _gain > 0:
                    groups.append([9, [[_gain]]])
                else:
                    skip(gt, goods, '体力已达上限 20, 本次实际入账 0 -> 不报 (防飘字多于真实)')
            elif gt == 14:                     # 皮肤 (L98 皮肤id; 客户端 style14 -> REWARD_NEWTYPE.SKIN)
                sid = int(goods) if str(goods).isdigit() else 0
                if sid:
                    _isdup, _g = _grant_skin_with_dup(sid)
                    groups.extend(_g)
                else:
                    skip(gt, goods, '静态 id 非法')
            elif gt == 18:                     # 宝箱 (L103; 客户端 style18 -> TREASUREBOX)
                # 容器=state.treasure_bag (与 handlers_profile 1060/1062 一致; L103 id 与 L02 道具 id 同号, 不能放 prop)
                # ★v32: 改走 state.add_box —— 它会校验 id ∈ L103_TreasureBox。
                #   原因: 1060 是**裸下发** treasure_bag 的, 表外 id 会让客户端查不到箱表 -> 崩。
                from state import add_box as _addbox
                _uc2, _tot = _addbox(goods, num)
                if _uc2 is None:
                    skip(gt, goods, '箱 id 不在 L103_TreasureBox 内 (客户端渲染会崩)')
                else:
                    groups.append([18, [[int(goods), max(1, int(num))]]])
            elif gt == 17:                     # ★v37 玩家经验 (客户端 IconData: REWARD_NEWTYPE.EXP = 17)
                # 用户要求: "后台弄个发玩家经验的我需要升到高级解锁更多功能测试"
                # 语义: 数值类附件 (像铜钱/体力那样不需要选物品), num = 经验点数。
                # 走 state.player_add_exp() —— 它按 L101_PlayerExp 表**逐级**升级并保留余数,
                # 与战斗/任务拿经验是同一条实现 (别在别处再写一遍升级逻辑)。
                # dropInfo 用 [17,[[num]]] (数值类形状, 与体力 [9,[[n]]] 同构), 真机确认过客户端不崩。
                try:
                    _pn = int(num)
                except Exception:
                    _pn = 0
                if _pn <= 0:
                    skip(gt, goods, '经验数量必须 > 0')
                else:
                    _lv0 = int(EQ_STATE.get('player_lv', 1))
                    from state import player_add_exp as _pae
                    _pae(_pn)
                    _lv1 = int(EQ_STATE.get('player_lv', 1))
                    groups.append([17, [[_pn]]])
                    if _lv1 != _lv0:
                        print('[GRANT] 玩家经验 +%d -> 等级 %d -> %d' % (_pn, _lv0, _lv1), flush=True)
            elif gt == 22:                     # 头像框 (T35_HeadFrame; 客户端 style22 -> HEAD_FRAME)
                fid = int(goods) if str(goods).isdigit() else 0
                if fid:
                    EQ_STATE.setdefault('head', {}).setdefault('frame_own', {})[fid] = 1
                    groups.append([22, [[fid]]])
                else:
                    skip(gt, goods, '静态 id 非法')
            elif gt == 98:                     # ★v59 续: 礼包/背包卡 (客户端 style98 -> GIFT_BAG)
                #   ★语义判定（真机验证驱动）：type 98 是**背包里的可开礼包卡**，不是"已开的内容"。
                #     客户端礼包列表 1097 = giftbag 数组 [ucode, type, 静态id, count]，
                #     点它开包时客户端发 1179(ucode, staticId, num, type=98) -> _use_giftbag + _apply_gift_rewards。
                #     ⇒ 所以发放端要做的只是**把它放进 giftbag**（可叠加），开包仍走既有唯一入口。
                #   （踩过的坑：第一版写成直接 _apply_gift_rewards -> 邮件领到的是内容而不是礼包卡，
                #     客户端 1097 列表里根本看不到这个礼包。）
                try:
                    from state import add_giftbag as _addgb98
                    _uc98, _cnt98 = _addgb98(98, int(goods), max(1, int(num)))
                except Exception as _e2:
                    skip(gt, goods, '礼包入包异常 %r' % (_e2,))
                    continue
                if not _uc98:
                    skip(gt, goods, '礼包入包失败')
                    continue
                # 客户端 style98 的 goodslist = [ucode, 静态id, 数量]（与 1097 行同形）
                groups.append([98, [[_uc98, int(goods), _cnt98]]])
            elif gt == 5:                      # 元灵 (L03_Equipghost; 客户端 style5 -> ghost_getMe:insertItem [ucode,id,count,isload])
                gid5 = int(goods) if str(goods).isdigit() else 0
                if gid5:
                    # ★2026-09-12: 同 id 元灵叠加 (不再每次新建 ucode)
                    # ★v51: 发**全部受影响的行** —— 完整元灵不堆叠时一次会开多格,
                    #   只回一行会让客户端少建格子 (要等下次整包 1031 才补齐,
                    #   用户报的"要重复启动游戏才正确显示和使用"即此)。
                    from state import add_ghost_rows as _addgh
                    groups.append([5, _addgh(gid5, max(1, int(num)), 0)])
                else:
                    skip(gt, goods, '静态 id 非法')
            # 12/16 buff 仅作周/月卡 G03 卖点行, 由 card_state_fields 以 roleInfo 生效, 不在此发
            elif gt == 21:                     # 活跃度 (客户端 style21 / REWARD_NEWTYPE.LIVENESS)
                # ★2026-10-11: 本条是"邮件/礼包路径缺的那一半"。
                #   `handlers_task.py:293` 早就把 ty21 接到 `_add_extra_vital()`（奖励型活跃递补,
                #   计入今日活跃 + 本周累计），**但那只覆盖每日任务族**；邮件附件与礼包开包
                #   走的是本函数 ⇒ ty21 在这里落进 else 分支被 skip。
                #   实测影响面（对着 `T02_Reward` 全表算）：41 个官方奖励组带 ty21
                #   （组 800001~8000xx 段），任何引到它们的邮件/礼包都会**静默少给活跃度**。
                #   所以这里走与 handlers_task **同一个** `_add_extra_vital`（不新开计数键,
                #   否则会造出第二份真值 —— 本项目的铁律）。
                try:
                    from handlers_task import _add_extra_vital as _aev
                    _aev(max(0, int(num)))
                    groups.append([21, [[int(num)]]])
                except Exception as _e21:
                    skip(gt, goods, '活跃度入账失败 %r' % _e21)
            # ★ 以下三类**刻意不走这里**（不是漏了, 是核实过后的结论）:
            #   · ty24 工会活跃度 / ty25 妖盟个人财富 —— 官方表里确有（24 有 91 组、25 有 144 组），
            #     但**客户端没有任何读路径**消费它们（`NetDropRewardData` 只把它们做成飘字 item,
            #     没有 `insertItem` / setter）。ty25 在妖盟商店里读的是
            #     `ControllerMSociatyInfoHome:getMeOffer()` = **1121 的第 15 槽 `todaydonate`**,
            #     而那条链已经由捐献在维护。⇒ 只写不读 = 半截管道, 换不来任何玩家可见效果,
            #     却要动会级世界键（可重放事务）。**要做先补读路径**。
            #   · ty29 祈愿值 —— 客户端飘字用；其真值链是**卡池 UP 计数**，
            #     服务端 `handlers_draw.dr['luck']` 已经逐抽递增（`handlers_draw.py:268`），
            #     `cardboss` 另有独立的逐 BOSS `pray`。发它的官方组只有 11004~11006 三组，
            #     且**服务端没有任何 handler 引到这三组**（全仓 grep 零命中）⇒ 当前无从触发。
            else:
                skip(gt, goods, '该奖励类型未实现落库 (客户端 REWARD_NEWTYPE 有定义, 服务端没接)')
        except Exception as _e:
            skip(gt, goods, '异常 %r' % _e)
    if not groups:
        return '[]'
    return '[[],[' + ','.join('[%d,[%s]]' % (s, ','.join(json.dumps(x, separators=(',', ':')) for x in gl)) for s, gl in groups) + ']]'

def _next_equip_ucode():
    # ★2026-10-02 (审计 §9 高severity #3): 旧实现是 max(在用号)+1 的**扫描式**发号 ——
    #   并发下两次调用返回同一个号; 即使不并发, "取号"与"insert"之间再取一次也会撞号。
    #   现委托给 state 的原子发号器(持锁 + 单调 + 跳过占用)。保留本函数名以免动调用点。
    return alloc_equip_ucode()

def _prop_ucode_for_sell(ucode, sid, btype):
    """★v47 出售道具的 ucode 解析: 精确命中优先, 否则按静态id兜底。

    事故 (2026-09-13 用户报): "合成和卖出残章后客户端还是显示那么多残章, 重启客户端后正常"。
      用户卖残章时, 服务端 `handle_bag_sell` 把所有"ucode 不在 prop 里"的条目
      **静默 continue**, 然后回一个 `[1,[gold,[],0]]` ——
        ① 客户端 NetBagSell 靠 `sellList` 逐条 deleteItem 来扣显示, 空表 = 一件都不扣;
        ② 服务端那一侧其实也没扣 (因为压根没匹配上), 于是"服务端说没卖 / 客户端说还在",
           而玩家明明看到卖出的提示 → 界面与真实状态分叉, 直到重启重新拉 1033 才对齐。
      兜底动机: 本工程残章的 ucode 历史上并不总是等于静态id
      (早期发材料用过 '900001' 这类 ucode; 静态id 又恰好落在 300002-300025 区间),
      且客户端 VBagSell 发的是自己那份 `_obj:getUcode()` —— 一旦两边分叉,
      精确匹配就会全表落空。按静态id 兜底可以在这种分叉下**仍然正确扣减**。

    返回 (ucode, pid, have); 完全找不到 -> (None, None, 0)。
    """
    prop = EQ_STATE.get('prop') or {}
    pidmap = EQ_STATE.get('prop_id') or {}
    if ucode and ucode in prop and (prop.get(ucode) or 0) > 0:
        return ucode, pidmap.get(ucode), int(prop[ucode])
    # ---- 走兜底前: 先把"同一静态id 被拆成多个 ucode"的存量**归并**到一个 ucode ----
    #   为什么必须先归并: 若 300003 与 900001 同时存在 (历史遗留), 只扣其中一格,
    #   客户端拿回包按 ucode 扣显示, 另一格会残留在界面上 —— 又变成"卖不干净"。
    #   归并方向: 取 ucode 数值最小的一格当主格 (确定性; 且本工程常态是 ucode==静态id)。
    want = str(sid or ucode or '')
    if want:
        same = [(str(u), int(prop[u])) for u, p in pidmap.items()
                if str(p) == want and (prop.get(u) or 0) > 0]
        if len(same) > 1:
            same.sort(key=lambda kv: (int(kv[0]) if kv[0].isdigit() else 1 << 62, kv[0]))
            keep_u = same[0][0]
            total = 0
            for u, c in same:
                total += c
                if u != keep_u:
                    prop.pop(u, None)
                    pidmap.pop(u, None)
            prop[keep_u] = total
            pidmap[keep_u] = int(want)
            print('[bag_sell] 静态id %s 多 ucode 归并: %r -> %s x%d'
                  % (want, [u for u, _ in same], keep_u, total), flush=True)
            return keep_u, int(want), total
        if len(same) == 1:
            return same[0][0], int(want), same[0][1]
    return None, None, 0


# ---------- 出售 BAG_SELL (1178) ----------
def handle_bag_sell(body_json):
    # req {sellList:[{type,staticId,ucode,num},..]} ; 客户端最多请求一次易确认
    # ★v47: 回包 [2] **必须**逐条回真实扣减结果 —— 客户端 NetBagSell 就是靠它 deleteItem 的;
    #   以前的静默 continue 会让"卖掉了但界面没变" (用户 2026-09-13 报的 bug)。
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    sellList = req.get('sellList') or []
    if not isinstance(sellList, list):
        return b'[0,{}]'
    add_gold = 0
    deducted = []          # [[ucode,id,num,type],..]
    skipped = []           # 未能出售的条目 (诊断用; 不发给客户端, 避免它误扣)
    for s in sellList:
        if not isinstance(s, dict):
            skipped.append(['not-dict'])
            continue
        try:
            btype = int(s.get('type') or 0)
            sid   = int(s.get('staticId') or 0)
            num   = int(s.get('num') or 0)
        except Exception:
            skipped.append(['bad-fields', str(s)[:60]])
            continue
        ucode = str(s.get('ucode') or '')
        if num <= 0 or (not ucode and not sid):
            skipped.append(['bad-num-or-id', ucode, sid, num])
            continue
        if btype == BT_EQUIP:
            # 装备出售: **整件**删除（客户端 `NetBagSell.lua:40` 就是 `deleteItem(data[1])`，
            # 不看 num）。★★2026-10-16（任务书 B②）：所以这里只接受 `num==1` ——
            # 修前删 1 件却按 `num*price` 给钱（`num=5` 就等于凭空多给 4 件的钱）。
            if num != 1:
                skipped.append(['equip-num-must-be-1', ucode, num])
                continue
            if ucode not in EQ_STATE['equip_id']:
                skipped.append(['equip-not-found', ucode])
                continue
            eid = EQ_STATE['equip_id'][ucode]
            price = _equip_price(eid)
            _drop_equip(ucode)
            add_gold += price
            deducted.append([ucode, eid, 1, btype])
        elif btype == BT_PROP:
            # 道具出售: 扣 count (★v47: 精确 ucode 命中失败时按静态id 兜底)
            u, pid, have = _prop_ucode_for_sell(ucode, sid, btype)
            if u is None:
                skipped.append(['prop-not-found', ucode, sid])
                continue
            d = min(num, have)
            if d <= 0:
                skipped.append(['prop-zero', u])
                continue
            price = _prop_price(pid)
            EQ_STATE['prop'][u] = have - d
            add_gold += d * price
            if EQ_STATE['prop'][u] <= 0:
                EQ_STATE['prop'].pop(u, None)
                EQ_STATE.get('prop_id', {}).pop(u, None)
            deducted.append([u, pid, d, btype])
        elif btype == BT_GHOST:
            # 元灵出售: 扣数量(id 1020-1025), 加金币
            # ★★2026-10-16（任务书 B②）：修前 `_deduct_ghost(ucode, num)` 只要行在就返回 True
            #   —— 行里只有 1 个、请求 `num=5` 时，扣到 0 仍按 **5 个**给钱（凭空印钱）。
            #   现在按**实际扣减量**计价，`deducted` 里报的也是实际值。
            gid = _ghost_id_of(ucode)
            if gid is None:
                skipped.append(['ghost-not-found', ucode])
                continue
            have = 0
            for g in EQ_STATE.get('ghost') or []:
                if str(g[0]) == str(ucode):
                    try:
                        have = int(g[2] or 0)
                    except (TypeError, ValueError):
                        have = 0
                    break
            d = min(num, have)
            if d <= 0:
                skipped.append(['ghost-zero', ucode])
                continue
            price = _GHOST_PRICE.get(str(gid), 0)
            if _deduct_ghost(ucode, d):
                add_gold += d * price
                deducted.append([ucode, gid, d, btype])
            else:
                skipped.append(['ghost-deduct-fail', ucode])
            continue
        else:
            skipped.append(['bad-btype', btype, ucode])
            continue
    EQ_STATE['gold'] += add_gold
    if skipped:
        print('[bag_sell] 未成交 %d 条: %r' % (len(skipped), skipped[:6]), flush=True)
    if deducted:
        save_state()
    # 响应 [1]=gold, [2]=sellList, [3]=count(剩余可出售数量)  —— 按 InterfaceDict BAG_SELL(1178)
    data = [EQ_STATE['gold'], deducted, 0]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

def _drop_equip(ucode):
    # 从状态中删除一件装备, 并清空怪物装备槽中对该 ucode 的引用
    EQ_STATE['equip_id'].pop(ucode, None)
    EQ_STATE['lv'].pop(ucode, None)
    EQ_STATE['exp'].pop(ucode, None)
    EQ_STATE['equip_isload'].pop(ucode, None)
    for muc, ueq in list(EQ_STATE['monster_ueq'].items()):
        l = list(ueq)
        for i in range(3):
            if str(l[i]) == str(ucode):
                l[i] = '0'
        EQ_STATE['monster_ueq'][muc] = tuple(l)

def _ghost_id_of(ucode):
    # 元灵 ucode -> 静态 id (state.ghost 里查)
    for g in EQ_STATE.get('ghost', []):
        if str(g[0]) == str(ucode):
            return int(g[1])
    return None

def _deduct_ghost(ucode, num):
    # 扣元灵数量; 扣完移除
    gh = EQ_STATE.get('ghost')
    if not isinstance(gh, list):
        return False
    for g in gh:
        if str(g[0]) == str(ucode):
            g[2] = max(0, int(g[2]) - num)
            if g[2] <= 0:
                gh.remove(g)
            return True
    return False

# ---------- 使用礼包 BAG_USE (1179) ----------
def _merge_gift_drops(frags):
    """把若干次 `_apply_gift_rewards` 的 dropInfo 片段并成一份 `[[], groups]`。

    ★逐份抽出来的条目应当**逐条列出**（客户端按 style 分组渲染），所以这里做的是
      "把每份的 groups 顺序接起来"，不是按 (id,kind) 求和 —— 求和会把"开了 3 份各得 1 个"
      显示成"得了 3 个"的一份，与真实抽取过程不符。
    """
    groups = []
    for f in frags:
        try:
            body = json.loads(f)
        except Exception:
            continue
        if isinstance(body, list) and len(body) >= 2 and isinstance(body[1], list):
            groups.extend(body[1])
    return [[], groups]


def handle_bag_use(body_json):
    """1179 BAG_USE：开**背包礼包卡**（baseType 98）。

    req `{ucode, staticId, num, type, giftId}`（`NetBagUse.lua:20-25`）；
    resp `[useGift, roleInfo, dropInfo, tipsid]`（`:39-42`）。
    ★★2026-10-16（任务书 B①⑤）：
      · `num` 个礼包要**扣 num 个、发 num 份**（修前 `_use_giftbag` 扣 N 却只发 1 份）；
      · `staticId` 必须与该 ucode 那一行**绑定**（修前按请求体发奖，换 staticId 就能开别的礼包）；
      · 烟花（2052/2053）的**计分 + 8086 推送**挂在这里 —— 这里才是 1179 的真实路径
        （修前那句钩子写在 `handlers_bag.handle_gift_bag_use` 里，而 1098 实际路由到
        `handlers_card.handle_gift_bag_use`，所以那份是**死代码**：钩子从来没被执行过）。
    """
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0]'
    if not isinstance(req, dict):
        return b'[0]'
    ucode = str(req.get('ucode') or '')
    try:
        sid = int(req.get('staticId') or 0)
        num = int(req.get('num') or 0)
        btype = int(req.get('type') or 0)
    except (TypeError, ValueError):
        return b'[0]'
    if not ucode or sid <= 0 or btype != 98:
        return b'[0]'
    # ★2026-10-16（B 复核）：**开包前先看怪物背包放不放得下**。
    #   礼包卡的内容里有妖怪（如 6000/8003~8007 各带 1~2 只），而客户端怪物背包上限
    #   是 21（`handlers_draw.MAX_BAG`，= `SQGamer:getMaxMonster()`）。修前这里照样扣礼包、
    #   发出超上限的妖怪 —— 玩家看不见也用不了，等于**礼包被吃掉**。
    #   现在：确定会发的妖怪放不下就**一个都不扣**（回 `[0]`，并打日志点名），
    #   玩家清理背包后重开即可。
    _need = _gift_monster_need(sid, num)
    if _need and _need > _monster_room():
        print('[BAG] 1179 礼包 %s x%s 需要 %s 个妖怪格，剩 %s -> 拒绝（不扣礼包）'
              % (sid, num, _need, _monster_room()), flush=True)
        return b'[0]'
    n = _use_giftbag(ucode, btype, sid, num)
    if n <= 0:
        return b'[0]'
    # 逐份抽取（L10 权重 / G03 固定内容），每份都是一次独立开包
    frags = [_apply_gift_rewards(sid) for _ in range(n)]
    drop_info = _merge_gift_drops(frags)
    save_state()
    # 烟花计分 + 8086 推送（只有 2052/2053 会被 `note_firework_used` 认；整段吞异常）
    try:
        from handlers_fws import note_firework_used
        note_firework_used(sid, n)
    except Exception as _e:
        print('[BAG] note_firework_used 调用失败 gid=%s n=%s: %r' % (sid, n, _e), flush=True)
    use_gift = [ucode, sid, n, 98]
    data = [use_gift, json.loads(role_info_array()), drop_info, 0]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ---------- 使用周卡/月卡 GIFT_BAG_USE (1098) ----------
# ★★2026-10-16（任务书 B④）：**这里原来还有一份 `handle_gift_bag_use`，已删除。**
#   事实：`game_server.py:266` 显式 `from handlers_card import handle_gift_bag_use`（1098 的
#   真正入口），而 `from handlers_bag import *`（:117）会先绑一份同名的；谁后 import 谁生效
#   ⇒ 1098 实际一直跑的是 `handlers_card` 那份，本文件这份是**死代码**。
#   而死代码里恰好挂着烟花的计分/推送钩子 ⇒ "烟花用掉了不涨分"（`_fireworks_test.py`
#   F9 当时测的也是这个死函数，所以从没红过）。钩子已移到 1179 的真实路径（`handle_bag_use`）。
#   ⚠ 不要再在这里加一份同名实现：那会重新把 1098 的路由绑回本文件（护栏
#     `_bag_conservation_test.py` 的 R1/R2 会当场红）。

# ---------- 背包合成 BAG_COMPOSE (1177) (元灵碎片31/技能碎片32) ----------
def handle_bag_compose(body_json):
    # req {ucode,staticId,num,type}; type=31 元灵碎片 / 32 技能碎片
    #   官方链 (VBagComposite:sendMessage): 碎片(31/32) -> 元灵(ghost, drop style5) / 技能书(skill袋, drop style99)
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    ucode = str(req.get('ucode') or '')
    sid = int(req.get('staticId') or 0)
    num = int(req.get('num') or 0)     # 本次消耗的碎片总数 (客户端 countCur*countUnit)
    itype = int(req.get('type') or 0)
    if ucode not in EQ_STATE['prop']:
        return b'[0,{}]'
    have = EQ_STATE['prop'][ucode]
    if itype == T_GHOST_PIECE:
        # ---- 元灵碎片 -> 元灵 (L03: ScrapID=碎片静态id, ScrapQuantity=所需数量) ----
        g = _GHOST_BY_SCRAP.get(str(sid))
        if g is None:
            return b'[0,{}]'
        gid, need = g[0], int(g[1])
        if num <= 0 or have < num or num % need != 0:
            return b'[0,{}]'
        times = num // need
        EQ_STATE['prop'][ucode] -= num
        if EQ_STATE['prop'][ucode] <= 0:
            EQ_STATE['prop'].pop(ucode, None)
            EQ_STATE.get('prop_id', {}).pop(ucode, None)
        # 元灵入背包 (ghost 列表; item [ucode,id,count,isload])
        #   ★v51 语义变更: **完整元灵不堆叠, 一格一个** (只有碎片类可堆叠)。
        #     ⇒ 一次合成 times 个就必须把**全部新建的行**发给客户端,
        #       否则客户端只建出一格, 要等下次整包 1031 才补齐。
        from state import add_ghost_rows as _addgh
        _rows = _addgh(int(gid), times, 0)
        save_state()
        synthesis = [[ucode, sid, num, 2]]
        drop_info = [[], [[5, _rows]]]
        data = [synthesis, json.loads(role_info_array()), drop_info]
        return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')
    if itype == T_SKILL_PIECE:
        # ---- 技能碎片 -> 技能书 (L93: SkillScrapID=碎片静态id, SkillScrapValue=所需数量) ----
        #   产出技能 = SkillLv=1 行 (L93), isopen=1, ischange=1, 落入技能袋 (drop style99 客户端 insertItem)
        from handlers_skill import produce_skill_from_piece, _PIECES
        pm = _PIECES.get(str(sid))
        if pm is None:
            return b'[0,{}]'
        need = int(pm['need'])
        if need <= 0 or num <= 0 or have < num or num % need != 0:
            return b'[0,{}]'
        times = num // need
        EQ_STATE['prop'][ucode] -= num
        if EQ_STATE['prop'][ucode] <= 0:
            EQ_STATE['prop'].pop(ucode, None)
            EQ_STATE.get('prop_id', {}).pop(ucode, None)
        made = produce_skill_from_piece(sid, times)
        if not made:
            return b'[0,{}]'
        bump_task(CT_SKILLCOMPOSE, 1)      # 每日任务: 技能合成
        save_state()
        synthesis = [[ucode, sid, num, 2]]
        # drop style99: goodsInfo=[ucode,id,level,isload,isopen,ischange] (NetDropRewardData: 客户端 skill_getMe insertItem)
        goods = [[u, row, 1, 0, 1, 1] for u, row in made]
        drop_info = [[], [[99, goods]]]
        data = [synthesis, json.loads(role_info_array()), drop_info]
        return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')
    return b'[0,{}]'

# ---------- 碎片合成 PIECE_COMPOSE (1168) (怪物35/皮肤36/其他37/38) ----------
def handle_piece_compose(body_json):
    # req {itemid,count}; itemid=碎片道具静态ID, count=本次消耗碎片总数
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    itemid = str(req.get('itemid') or '')
    count = int(req.get('count') or 0)
    if itemid not in _COMPOSE:
        return b'[114]'                       # 残章id错误/无此合成表
    iteam, group, ctype, rate, cids, rid, rn = _COMPOSE[itemid]
    # 找到对应道具背包条目 (ucode), 校验数量
    ucode = None
    for uc, pid in EQ_STATE.get('prop_id', {}).items():
        if str(pid) == itemid:
            ucode = uc
            break
    if ucode is None:
        # 兼容: 碎片 ucode == itemid
        if itemid in EQ_STATE['prop']:
            ucode = itemid
    if ucode is None or ucode not in EQ_STATE['prop']:
        return b'[0,{}]'
    have = EQ_STATE['prop'][ucode]
    if have < count or count <= 0:
        return b'[76]'                        # 素材数量不足
    if iteam <= 0:
        return b'[0,{}]'
    times = count // iteam                    # 合成次数
    # ★★2026-11-14（P0 卡登录）：产怪类合成**先验 ComposeID 再扣碎片**。
    #   L07 里 `4000501 UR卡妖怪·魂`（100 片）与 `4500011 花末天奇·魂`（100 片）的 ComposeID 带
    #   `500011`，而 `L91_Status` 里没有这只怪（只有 L91_StatusRender 的展示行）⇒ 合出来客户端
    #   渲染不了、下次登录卡在 80%。修法：只从"L91 里真有的怪"里抽；一个都没有就**不扣碎片**回 114。
    _cids4 = cids
    if group == 4:
        try:
            from handlers_draw import _MON_STATIC as _MS4
            _cids4 = [c for c in cids if int(c) in _MS4]
        except Exception:
            _cids4 = list(cids)
        if not _cids4:
            print('[BAG] 1168 拒绝合成: item=%s 的 ComposeID=%r 全部不在 L91_Status（不扣碎片）'
                  % (itemid, cids), flush=True)
            return b'[114]'
    EQ_STATE['prop'][ucode] -= times * iteam
    if EQ_STATE['prop'][ucode] <= 0:
        EQ_STATE['prop'].pop(ucode, None)
        EQ_STATE.get('prop_id', {}).pop(ucode, None)
    # ---- 按 GroupID 产出对应类型的合成结果 ----
    if group == 4:                       # 妖怪碎片 -> 产怪 (30字段item进怪包)
        made = []
        for _ in range(times):
            mid = _pick_compose(_cids4, rate, ctype)
            if mid is None:
                mid = int(_cids4[0]) if _cids4 else 0
            if mid:
                uc = _add_monster(mid)
                if uc:                       # ★2026-11-14: _add_monster 现在会拒绝未知 mid 并返回 None
                    made.append(uc)
        drop_info = _drop_monster(made)
    elif group == 14:                    # 皮肤碎片 -> 产皮肤 (dropInfo style14; 已验证不崩)
        #   ★2026-09-12 修复 (用户反馈 §4 "背包打开重复的皮肤没有变换成碎片"):
        #     旧实现只调 _drop_skin(纯展示), 皮肤既不写入 skin_owned、也不判重、更不用 L07 的
        #     ReturnID/ReturnNum —— 合成出来的皮肤"包里没反应", 重复皮肤也无碎片。
        #     L07_Item_Compose 官方字段 (实测 5010172 魅蛾妖童·魄 / 5030112 冥府镇兽·魄):
        #       IteamNum=50 片 -> ComposeID=10172/30112 (皮肤), 已拥有时 -> ReturnID=5000101(密藏皮肤·魄) x ReturnNum=25
        groups = []
        for _ in range(times):
            sid = _pick_compose(cids, rate, ctype)
            if sid is None:
                sid = int(cids[0]) if cids else 0
            if not sid:
                continue
            if str(int(sid)) in (EQ_STATE.get('skin_owned') or {}):
                # 已有该皮肤 -> 按官方 ReturnID/ReturnNum 返通用皮肤碎片
                rid, rn = int(rid or 0), int(rn or 0)
                if rid > 0 and rn > 0:
                    uc2 = str(rid)
                    EQ_STATE['prop'][uc2] = int(EQ_STATE['prop'].get(uc2, 0)) + rn
                    EQ_STATE['prop_id'][uc2] = rid
                    for _grp in groups:
                        if _grp[0] == 2 and _grp[1] and _grp[1][0][0] == uc2:
                            _grp[1][0][2] += rn
                            break
                    else:
                        groups.append([2, [[uc2, rid, rn]]])
            else:
                _dup, _g = _grant_skin_with_dup(int(sid))
                groups.extend(_g)
        if not groups:
            drop_info = '[]'
        else:
            drop_info = '[[],[' + ','.join(
                '[%d,[%s]]' % (s, ','.join(json.dumps(x, separators=(',', ':')) for x in gl))
                for s, gl in groups) + ']]'
    else:                                # 其它产物类型(头像/其它): 只消耗碎片, 不回奖励
        #   原因: 头像等奖励客户端渲染未在真机验证, 硬塞会卡死(用户实测)
        drop_info = '[]'
    save_state()
    prop = [ucode, int(itemid), times * iteam]
    data = [prop, json.loads(role_info_array()), json.loads(drop_info), 0]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

def _pick_compose(cids, rate, ctype):
    # 按 ComposeRate (例 '1|1|1' 对应 cids) 加权随机取一个合成结果 ID; 失败返回 None
    if not cids:
        return None
    rates = [int(x) for x in str(rate).split('|') if x.strip().isdigit()]
    if len(rates) != len(cids):
        return random.choice(cids)
    total = sum(rates) or 1
    roll = random.randrange(total)
    for r, cid in zip(rates, cids):
        if roll < r:
            return cid if cid else None
        roll -= r
    return cids[-1]

def _add_monster(mid):
    # 沿用 handlers_draw 的入包逻辑: ucode 自增, 写入 EQ_STATE['monsters'] + monster_ueq
    # ★2026-10-02 (审计 §9 高severity #3): 取号走**原子**入口(旧实现是读-改-写, 并发重号)
    # ★★2026-11-14（P0 卡登录）：mid 必须先能在 `L91_Status` 里查到，否则**拒绝建怪**。
    #   修前查不到时是"静默兜底"（skin=0 / skills=0）照样写进存档 —— 而客户端
    #   `SDFactory:getObject(SDMonster, mid)` 拿不到静态表返回 nil，`1032` 一加载就抛错 ⇒
    #   **登录永远卡在 80%**。真机已复现：L07 的 `4000501 UR卡妖怪·魂` / `4500011 花末天奇·魂`
    #   ComposeID 里带 **500011（花末天奇）**，这只在 `L91_Status` 里根本不存在（只有
    #   `L91_StatusRender` 的展示行）⇒ 100 片 UR 碎片合出来即砖档。
    from handlers_draw import _MON_STATIC as _MS
    try:
        _mid = int(mid)
    except (TypeError, ValueError):
        _mid = None
    if _mid is None or _mid not in _MS:
        print('[BAG] 拒绝创建 L91_Status 里不存在的妖怪 mid=%r（否则客户端 1032 解析必崩）' % (mid,),
              flush=True)
        return None
    uc = alloc_monster_ucode()
    # 怪物的 skin/skills 从 L91 取 (同 handlers_draw 的 _MON_STATIC)
    _q, skin, skills, _tall = _MS[_mid]
    EQ_STATE['monsters'][uc] = [_mid, skin, 100, list(skills)]
    EQ_STATE['monster_ueq'][uc] = ('0', '0', '0')
    EQ_STATE['monster_lv'][uc] = 1
    EQ_STATE['monster_exp'][uc] = 0
    return uc

# ---------- 掉落(dropInfo)工具 ----------
def _drop_gold(amount):
    # dropInfo=[_, [style,goodslist]]; style3=铜钱 -> [[ [gold],... ]]
    return '[[],[[3,[[%d]]]]]' % amount

def _drop_soul(amount):
    return '[[],[[7,[[%d]]]]]' % amount

def _drop_monster(made_ucodes):
    # style=4 怪物: goodslist=[30字段怪物item]; 无产出则空
    #   ★2026-09-11 (v21 §3.5): 皮肤改用 get_monster_skin(穿戴优先) —— 旧实现取基础皮肤,
    #   掉落/奖励里同一只怪会显示成未穿皮肤的样子
    from state import get_monster_skin as _gms
    goods = []
    for uc in made_ucodes:
        base = get_monster_base(uc)
        if base is None:
            continue
        mid, skin, num, skills = base
        mi = _mon(int(uc), mid=mid, num=num, skin=_gms(uc) or skin, skills=list(skills), uequip=('0', '0', '0'))
        goods.append(mi)
    if not goods:
        return '[]'
    return '[[],[[4,[%s]]]]' % ','.join(goods)

def _drop_skin(cids, times):
    # style=14 皮肤: goodslist=[[skinid],..]; 客户端只展示奖励, 不入包
    picks = [random.choice(cids) for _ in range(max(1, times))] if cids else []
    if not picks:
        return '[]'
    return '[[],[[14,[%s]]]]' % ','.join('[%d]' % x for x in picks)

def _drop_head(cids, times):
    # style=23 头像: goodslist=[[headid],..]
    picks = [random.choice(cids) for _ in range(max(1, times))] if cids else []
    if not picks:
        return '[]'
    return '[[],[[23,[%s]]]]' % ','.join('[%d]' % x for x in picks)
