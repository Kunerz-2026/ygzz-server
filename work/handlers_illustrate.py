# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 图鉴 ILLUSTRATE_GET(1072) / ILLUSTRATE_SINGLEGET(1073)
#   ★2026-09-12 新增 (用户反馈 §3 "把图鉴的装备图鉴修复好"):
#     客户端 MIllustrateHome / MEquipHome 只从服务器取"已解锁的图鉴 ID 列表"与"星级/解锁数",
#     其余(名称/图标/品质/等级/属性)全部由客户端按 M01_Manual + L01_Equipments 本地计算。
#     此前服务器**完全没有 1072/1073 处理**, 请求落到默认响应 -> statecode!=1 -> 图鉴整页打不开。
#
#   客户端契约 (解码明文):
#     NetIllustrateGet(1072)       : msg[1] = [[id, start, unlockedNum, totalNum], ...]
#                                    VIllustrateHome:updateUI 取 i=1..4 -> getIDByIndex/getStartsByIndex/...
#     NetIllustrateSingleGet(1073) : req {id}; msg[1]=已解锁图鉴ID列表, msg[2]=已解锁数, msg[3]=总数
#                                    MEquipHome:refreshData 用 msg[1] 做 isunlocked(config.ID) 判定
#
#   M01_Manual 行: (ID, Type, UnlockType, Require1, Require2, Display)
#     Type/UnlockType 1 -> 101 人物图鉴: Require1 = 妖怪静态id 列表 "300601|300602|..." (拥有其一即解锁)
#     Type/UnlockType 2 -> 201 装备图鉴: Require1 = L01 装备静态id (拾取即解锁, state.note_manual_unlock 记录)
#     Type/UnlockType 3 -> 301 场景图鉴: Require1=场景id / Require2=阈值 (私服无探索度口径 -> 全开)
#     Type/UnlockType 4 -> 401 剧情图鉴: Require1=剧情id        (私服无剧情进度口径 -> 全开)
import json, os
from state import EQ_STATE, save_state, all_monster_ucodes, get_monster_base, table_dir

_DATA = table_dir()

def _load_json(name):
    # ★v60 闭环 v58 §10 缺口 #3: 改走 state.load_table —— 按「尾字段 u32 长度」精确剥离,
    #   不再用 c[:c.rfind(']')+1]（长度字段含 0x5D 时会切错 -> 调用方 except -> 静默空表,
    #   实测 L98_AIPriority / T32_Achievement）。护栏: work/_v60_legacy_tail_guard_test.py
    #   ⚠ 必须**逐行复制**: load_table 的结果是**带缓存的同一个对象**, 而旧实现每次返回全新对象。
    #     调用方若原地改行(排序/填字段)会污染全局缓存 -> 这里保持"每次都是新对象"的原语义。
    from state import load_table as _lt
    return [list(_r) for _r in _lt(name, _DATA)]

# M01_Manual: 保留 (ID, Type, UnlockType, Require1, Require2)
_MANUAL = []
try:
    _t = _load_json('M01_Manual.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            if int(_r[_h['Display']] or 0) != 1:
                continue                      # 客户端只显示 Display==1 的行
            _MANUAL.append((int(_r[_h['ID']]), int(_r[_h['Type']]), int(_r[_h['UnlockType']] or 0),
                            str(_r[_h['Require1']] or ''), str(_r[_h['Require2']] or '0')))
        except Exception:
            continue
except Exception:
    pass
# 类别顺序 = Type 升序 (客户端 FileNode_1..4 与 id 一一对应)
_MANUAL_CATS = sorted(set(m[1] for m in _MANUAL))

def _owned_monster_mids():
    s = set()
    for uc in all_monster_ucodes():
        b = get_monster_base(uc)
        if b:
            try:
                s.add(int(b[0]))
            except Exception:
                pass
    return s

def _unlocked_ids():
    # 全类别的已解锁图鉴 ID
    from state import manual_unlocked_equip_ids
    out = set()
    mids = _owned_monster_mids()
    eqs = manual_unlocked_equip_ids()
    for (mid_id, mtype, utype, req1, req2) in _MANUAL:
        if utype == 1:                        # 人物图鉴: Require1 = 妖怪静态id 列表
            for x in str(req1).split('|'):
                x = x.strip()
                if x.isdigit() and int(x) in mids:
                    out.add(mid_id)
                    break
        elif utype == 2:                      # 装备图鉴: Require1 = 装备静态id
            if req1.strip().isdigit() and int(req1) in eqs:
                out.add(mid_id)
        else:                                 # 场景/剧情图鉴: 私服无相应进度口径 -> 全开
            out.add(mid_id)
    return out

def _stars(unlocked, total):
    # 私人服语义: 图鉴星级 = 该类别收集度 (100% -> 3 星 / >=70% -> 2 星 / 其余 1 星)
    if total <= 0:
        return 1
    r = float(unlocked) / float(total)
    if r >= 0.999:
        return 3
    if r >= 0.7:
        return 2
    return 1

def _cat_totals(unlocked):
    # 每类: 总数 / 已解锁数
    tot, unl = {}, {}
    for (mid_id, mtype, utype, req1, req2) in _MANUAL:
        tot[mtype] = tot.get(mtype, 0) + 1
        if mid_id in unlocked:
            unl[mtype] = unl.get(mtype, 0) + 1
    return tot, unl

def handle_illustrate_get(body_json=None):
    # 1072: msg[1] = [[id(Type), 星级, 已解锁数, 总数], ...] (客户端按 index 1..4 渲染)
    #   ★★2026-09-12 (v26 P0 "图鉴点不进去") 形状修正 —— 这里必须**多包一层**:
    #     客户端 app/network/illustrate/NetIllustrateGet.lua:34
    #         for i, v in ipairs(msg[1]) do  data.id = tonumber(v[1]) ...
    #     其中 `msg = recvMsg[#recvMsg] = resp[1]` (NetBase:parseMessage 取最后一个元素)。
    #     旧实现 resp = [1, rows]  -> resp[1] = rows = [[101,1,3,89],...]
    #        -> msg[1] = [101,1,3,89] -> v = 101 (number) -> `v[1]` ->
    #           "attempt to index local 'v' (a number value)" 抛错 ->
    #           m_callFunc 永不执行 -> VIllustrateHome:updateUI 拿空列表 -> 不给 FileNode_1..4 绑点击
    #           -> 用户"图鉴点不进去", 且 1073 永远发不出去。
    #     正确: resp = [1, [rows]] -> resp[1] = [rows] -> msg[1] = rows -> v = [101,1,3,89] ✔
    #   ⚠ 铁律 #26: 断言必须模拟客户端取用路径 (msg[1] 的每个元素还得是 table), 不能只验"JSON 能解析"。
    unlocked = _unlocked_ids()
    tot, unl = _cat_totals(unlocked)
    rows = []
    for c in _MANUAL_CATS:
        _u, _t = unl.get(c, 0), tot.get(c, 0)
        rows.append([c, _stars(_u, _t), _u, _t])
    return ('[1,' + json.dumps([rows], separators=(',', ':')) + ']').encode('utf-8')

def handle_illustrate_single_get(body_json=None):
    # 1073: req {id}; resp [1, [已解锁图鉴ID列表], 已解锁数, 总数]
    #   注: MEquipHome 只拿 msg[1] 做 isunlocked 判定并自行按品质分页, 故 id 仅作记录不作过滤
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):     # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
        req = {}
    unlocked = _unlocked_ids()
    lst = sorted(int(x) for x in unlocked)
    return ('[1,' + json.dumps([lst, len(lst), len(_MANUAL)], separators=(',', ':')) + ']').encode('utf-8')
