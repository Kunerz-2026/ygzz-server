# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 技能系统处理器 (2026-09-06 按客户端契约首版; 2026-09-07 第二轮按注释/明文补齐学习-合成链)
#   1502 SKILL_CHIPCOMPOSE  req {skillpieceid(技能碎片静态id), scrowpieceid(残章ucode列表x3)}
#                          -> [1, goldcoin, pice(合成后碎片[ucode,id,count]), propList(消耗残章), addgoldcoin(0=成功)]
#   1503 SKILL_STUDY       req {ucode(怪), slot(1-6), skillid(技能袋ucode)} -> [1, goldcoin, monsterInfo(30f), skillInfo(6f)]
#   1504 SKILL_INTENSIFY   req {monsterucode, skillucode, matterlist}  -> [1, goldcoin, skillInfo, propList]
#   1505 SKILL_REPLACE     req {ucode, goalucode, replaceucode}        -> [1, goldcoin, monsterInfo, skillInfo1, skillInfo2]
#   1177 BAG_COMPOSE(t32)  -> 在 handlers_bag 处理, 本模块提供 produce_skill_from_piece() (碎片→技能书)
#
# 官方学习-合成链 (客户 SkillCompose/SkillStudy/VBagComposite 明文):
#   残章(books, L02 Type33 300002-300025) --1502(3残章=L05材料+金)→ 技能碎片(skillpiece, L02 Type32 200001-200047)
#        --1177(SkillScrapValue个碎片)→ L93 SkillLv=1 技能书(技能袋, drop style99) --1503(金1000)→ 怪槽位
#   数据: L05_ScrowPiece(碎片id→L93行id+3材料) / L93_Skills(SkillScrapID+SkillScrapValue) / T23_Function(134=1502金)
import json, os, random
from state import EQ_STATE, save_state, ensure_skills, skill_isload_map, get_monster_base, get_monster_star
from config_data import _mon
from handlers_equip import _mon_with_state, mon_info_slot
from handlers_task import bump_task, CT_SKILLUP, CT_SKILLCOMPOSE

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

# ---------- L93_Skills 行索引: row_id -> (skillid, lv); (skillid, lv) -> row_id ----------
_L93_BY_ID = {}
_L93_LV_ROWS = {}
try:
    _t = _load_json('L93_Skills.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _rid = int(_r[_h['ID']])
            _sid = int(_r[_h['SkillID']])
            _lv = int(_r[_h['SkillLv']] or 1)
            _L93_BY_ID[_rid] = (_sid, _lv)
            _L93_LV_ROWS[(_sid, _lv)] = _rid
        except Exception:
            continue
except Exception:
    pass

# ---------- L05_ScrowPiece: 技能碎片静态id -> {row: L93行id(同族), mats: [3残章id]} ----------
#   客户端 SkillCompose:updateItem 用 SDPiece(此表) 的 MatrielID1..3 自动选中 3 个残章;
#   getTotalPieceList = L02 Type32 全部碎片id (lua SkillBase:getTotalPieceList)。
_L05 = {}
# ---------- L93_Skills SkillScrapID: 碎片id -> {need: SkillScrapValue, lv1_row: SkillLv=1 行id} ----------
#   客户端 VBagComposite:itemType==32 时 countUnit=SkillScrapValue; 1177 消耗 num=count*unit 碎片,
#   无"选哪条 L93 行"的客户端信号 -> 取同一 SkillScrapID 下 SkillLv=1 的最小行id (卸甲术 200020 有 5 个
#   同 Lv1 不同 SkillID 变体 30031-30035 -> 取 30031, 其余变体可作同名重复技能使用)。
_PIECES = {}
try:
    _t = _load_json('L05_ScrowPiece.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _pid = str(_r[_h['ID']])
            _L05[_pid] = {'row': int(_r[_h['SkillID']]),
                          'mats': [str(_r[_h['MatrielID1']]), str(_r[_h['MatrielID2']]), str(_r[_h['MatrielID3']])]}
        except Exception:
            continue
    _t = _load_json('L93_Skills.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _sid = str(_r[_h['SkillScrapID']])
            if _sid == '0':
                continue
            if int(_r[_h['SkillLv']] or 1) == 1:
                _row = int(_r[_h['ID']])
                _cur = _PIECES.get(_sid)
                if _cur is None or _row < _cur['lv1_row']:
                    _PIECES[_sid] = {'need': int(_r[_h['SkillScrapValue']] or 0), 'lv1_row': _row}
        except Exception:
            continue
except Exception:
    pass

# ---------- 消耗表 T23_Function ----------
#   134=碎片合成铜钱(100) 135=学习/替换(1000) 136=强化(3000) —— 客户端 SkillCompose/SkillStudy/SkillChange
#   SkillCompose.updateUI/SkillStudy.updateSkillPageView 均直读该表(界面消耗显示=服务器判定值)
_COST = {'compose': 100, 'study': 1000, 'intensify': 3000}
# ★v42: 强化是**按素材数量计价**的 —— 客户端 `consumeCount = T23[136] × count`
#   (SkillInstensify.lua:331-340, count = Σ素材贡献)。单独留一份原值,
#   免得将来有人把 _COST['intensify'] 改成"一口价"时把这里也带跑偏。
_T23_136 = None
try:
    _t = _load_json('T23_Function.json')
    _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _cid = int(_r[_h['ID']])
            _val = int(_r[_h['Value']] or 0)
            if _cid == 134:
                _COST['compose'] = _val
            elif _cid == 135:
                _COST['study'] = _val
            elif _cid == 136:
                _COST['intensify'] = _val
                _T23_136 = _val
        except Exception:
            continue
except Exception:
    pass

def _skill_item(uc):
    # 技能袋 item 6字段 [ucode,id,level,isload,isopen,ischange]; uc = 技能袋ucode
    #   ischange: 技能书(1177 合成/掉落产出, 记于 skill_produced)=1(可替换/可学习装配); 怪自带(L91)=0
    s = ensure_skills().get(str(uc))
    if not s:
        return None
    sid, lv, isopen = s[0], s[1], s[2]
    ischange = 1 if str(uc) in (EQ_STATE.get('skill_produced') or []) else 0
    il = skill_isload_map()
    return [str(uc), sid, lv, il.get(str(uc), 0), isopen, ischange]

def _monster_slots(ucode):
    # 怪物当前技能槽 (覆盖值或基础技能), 长度6, 空槽"0"
    ms = EQ_STATE.setdefault('monster_skills', {})
    if ucode not in ms:
        # 从当前基础技能初始化 (L91 技能即已装备)
        b = get_monster_base(ucode)
        skills = [str(s) if s else '0' for s in (b[3] if b else [0] * 6)]
        ms[ucode] = (skills + ['0'] * 6)[:6]
    return ms[ucode]

# ============ 技能袋维护 (学习-合成链产出) ============
def _prop_add_piece(pid, n=1):
    # 通用道具+数量: 与已有同静态id ucode 合并 (v8 铁律: 同 id 道具一格; 900xxx 历史已迁移合并)
    pid = str(pid)
    for u, p in (EQ_STATE.get('prop_id') or {}).items():
        if str(p) == pid and EQ_STATE['prop'].get(u, 0) > 0:
            EQ_STATE['prop'][u] = EQ_STATE['prop'].get(u, 0) + n
            return str(u), EQ_STATE['prop'][u]
    u = pid
    EQ_STATE['prop'][u] = EQ_STATE['prop'].get(u, 0) + n
    EQ_STATE['prop_id'][u] = int(pid)
    return u, EQ_STATE['prop'][u]

def produce_skill_from_piece(piece_id, times=1):
    # 1177 技能碎片 -> 技能书: 每 times 次生成一件技能袋 item (L93 lv1 行id, isopen=1 ischange=1)
    #   返回 [(ucode, rowid), ...]; piece_id 无对应 L93 行 -> None
    m = _PIECES.get(str(piece_id))
    if not m:
        return None
    out = []
    s = ensure_skills()
    for _ in range(times):
        nxt = int(EQ_STATE.get('next_skill_ucode', 1))
        uc = 'sk%d' % nxt
        EQ_STATE['next_skill_ucode'] = nxt + 1
        s[uc] = [m['lv1_row'], 1, 1]       # [id, level(行SkillLv), isopen] (ischange 由 skill_produced 派生)
        EQ_STATE.setdefault('skill_produced', []).append(uc)
        out.append((uc, m['lv1_row']))
    return out

# ============ 1502 技能碎片合成 (残章+金 -> 技能碎片) ============
def handle_skill_chip_compose(body_json):
    # 客户端 SkillCompose:compouse 发送 skillpieceid=选中碎片静态id, scrowpieceid=[残章ucode x3]
    #   (注意: 是 ucode 字符串列表, 非 [[ucode,id,count]] —— 旧实现按三元组解析属误读, 本版修正)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    piece = str(req.get('skillpieceid') or '')
    scrolls = req.get('scrowpieceid') or []
    if not isinstance(scrolls, list):
        scrolls = []
    # 碎片必须存在于 L05 (碎片id错误 -> 113)
    m5 = _L05.get(piece)
    if m5 is None:
        return b'[113]'
    # 残章必须恰好 3 个且均存在于道具背包 (残章为空 -> 71)
    chips = []
    for sc in scrolls:
        u = str(sc)
        if u in EQ_STATE['prop'] and EQ_STATE['prop'][u] > 0 and u in EQ_STATE.get('prop_id', {}):
            chips.append(u)
    if len(chips) != 3:
        return b'[71]'
    # 残章静态id集合必须等于 L05 材料集 (材料错误 -> 114; 数量不符 -> 76)
    if sorted(str(EQ_STATE.get('prop_id', {}).get(u)) for u in chips) != sorted(m5['mats']):
        return b'[114]'
    # 铜钱 (T23 134)
    cost = int(_COST['compose'])
    if EQ_STATE['gold'] < cost:
        return b'[77]'
    # 消耗 3 残章 (各1)
    consumed = []
    for u in chips:
        EQ_STATE['prop'][u] -= 1
        consumed.append([u, int(EQ_STATE['prop_id'][u]), 1])
        if EQ_STATE['prop'][u] <= 0:
            EQ_STATE['prop'].pop(u, None)
            EQ_STATE.get('prop_id', {}).pop(u, None)
    EQ_STATE['gold'] -= cost
    # 产出: 技能碎片 +1 (合并同静态id格)
    pu, pcnt = _prop_add_piece(piece, 1)
    bump_task(CT_SKILLCOMPOSE, 1)     # 每日任务: 技能合成
    # ★2026-10-02 (成就进度写入方): T32 Type=1119「首次进行技能合成」-> 组 1004/级别3(妖御天下)
    from handlers_achievement import note_ach
    note_ach(1119, 0, 1)
    save_state()
    pice = [pu, int(piece), pcnt]
    data = [EQ_STATE['gold'], pice, consumed, 0]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============ 1503 技能学习 ============
def handle_skill_study(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    ucode = str(req.get('ucode') or '')
    try:
        slot = int(req.get('slot') or 0)
    except Exception:
        slot = 0
    skillid = str(req.get('skillid') or '')
    if ucode not in EQ_STATE['monsters'] and ucode not in ('1', '2', '3'):
        return b'[2004]'
    if slot < 1 or slot > 6:
        return b'[117]'
    bench = ensure_skills()
    if skillid not in bench:
        return b'[115]'                       # 技能id错误(不在技能袋)
    if not bench[skillid][2]:
        return b'[124]'                       # 学习的技能未解锁
    slots = _monster_slots(ucode)
    if str(slots[slot - 1]) == skillid:
        return b'[119]'                       # 不能重复学习
    if skillid in [str(x) for x in slots]:
        return b'[119]'                       # 同一技能不能重复装配
    cur = str(slots[slot - 1])
    if cur not in ('0', ''):
        return b'[119]'                       # 槽位已有技能 (客户端此槽走技能详情/替换, 不会来学习)
    # 槽位解锁: 怪物阶数(Star)内空槽可学, 超出阶数 -> [118] 技能槽位未解锁
    #   客户端 SQMonster:getSkillList 容错: k<=Star 时槽位 "0"->"1"(可学), 超出 = 锁定 "0"
    star = get_monster_star(ucode)
    if slot > star:
        return b'[118]'
    cost = int(_COST['study'])
    if EQ_STATE['gold'] < cost:
        return b'[77]'                        # 金币不足
    EQ_STATE['gold'] -= cost
    slots[slot - 1] = skillid
    save_state()
    mi = _mon_with_state(ucode)
    si = _skill_item(skillid)
    data = [EQ_STATE['gold'], mon_info_slot(mi, ucode, '1503'), si]
    # ★v57: 把"成功路径上 mi 必有值"这一**推断**变成断言(见交接文档 §8.3)
    assert None not in data, '1503: 成功响应不得含 null'
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============ 1504 技能强化 ============
# ★v42 修用户现场 bug「选了 N 本天书要一口气强化到5级, 结果只升 1 级、天书还没消耗」。
#
# 根因: 旧实现**完全忽略 matterlist**, 恒做 `_lv + 1` 并返回 `propList = []`
#       —— 客户端拿到空 propList 就什么都不扣, 所以天书凭空"不消耗"。
#
# 客户端真语义 (逐行取自 app/team/SkillInstensify.lua + app/team/SkillBase.lua):
#   · 素材候选 (SkillBase:getInstensifyList):
#       天书道具: `BagConfig.propIdList[bag.functionTag.strong] = {34}` -> **L02 Type 34**
#                 (全表只有一行: 300001 古秘法天书); 按**数量**展开, 每个贡献 1 级
#       技能书  : 与目标技能**同 SkillID 族**(L93.SkillID, 官方注释"标识统一类型的技能")
#                 且 `isload == 0`(未装配在怪身上); 每个贡献**它自己的 SkillLv**
#   · 预览 (SkillInstensify:updatePreview):
#       count = Σ(素材贡献) ; m_preViewLv = 当前等级 + count (显示时截到 const_maxLv)
#       金币  = T23_Function[136].Value(=3000) × count
#   · 上限: `local const_maxLv = 5` (SkillInstensify.lua:13)
#   · 回包 propList 驱动客户端扣料 (NetSkillIntensify:recvMessage):
#       [1, 技能ucode]            -> skill_deleteItem
#       [2, 道具ucode, 数量]      -> prop_deductItem
#
# 错误码 (客户端 m_errorCode): 71 素材错误 / 115 技能id错误 / 121 不可替换 / 123 已在角色身上
#                               124 未解锁 / 125 已达上限 / 126 所选素材过多 / 127 素材不是本体技能
_SKILL_MAX_LV = 5                 # 客户端 SkillInstensify.lua:13 `local const_maxLv = 5`
_STRONG_PROP_IDS = None           # L02 Type 34 (天书) 的 id 集合


def _strong_prop_ids():
    """L02 Type 34 = 客户端 `bag.functionTag.strong` 认可的"强化道具"(天书) id 集合。"""
    global _STRONG_PROP_IDS
    if _STRONG_PROP_IDS is None:
        ids = set()
        try:
            _t = _load_json('L02_Loots.json')
            _h = {c: k for k, c in enumerate(_t[0])}
            for _r in _t[1:]:
                try:
                    if str(_r[_h['Type']]) == '34':
                        ids.add(int(_r[_h['ID']]))
                except Exception:
                    continue
        except Exception:
            pass
        _STRONG_PROP_IDS = ids
    return _STRONG_PROP_IDS


def _skill_family(sid):
    """技能行 id -> 族(SkillID); 未知 -> 0"""
    return _L93_BY_ID.get(sid, (0, 0))[0]


def _skill_max_lv(fam):
    """该族在 L93 里的最高 SkillLv, 再与客户端 const_maxLv(=5) 取小。"""
    mx = 0
    for (_f, _l) in _L93_LV_ROWS.keys():
        if _f == fam and _l > mx:
            mx = _l
    return min(_SKILL_MAX_LV, mx) if mx else _SKILL_MAX_LV


def handle_skill_intensify(body_json):
    # 素材驱动强化 (与客户端 updatePreview 同口径)。
    #   · matterlist 为空 -> 保留旧行为(私服便利: 只花金币升 1 级); 客户端 UI 其实不会发空包
    #   · 有素材 -> 按素材算级数/金币, 并**回报 propList 让客户端真的扣掉素材**
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    skillucode = str(req.get('skillucode') or '')
    bench = ensure_skills()
    if skillucode not in bench:
        return b'[115]'
    _e = bench[skillucode]
    sid, lv, isopen = _e[0], _e[1], _e[2]
    if not isopen:
        return b'[124]'                       # 技能未解锁
    fam = _skill_family(sid)
    maxlv = _skill_max_lv(fam)
    if lv >= maxlv:
        return b'[125]'                       # 技能等级已达上限
    matter = req.get('matterlist')
    if not isinstance(matter, list):
        matter = []

    # ---------- 走素材: 按客户端口径算级数 ----------
    if matter:
        prop_have = EQ_STATE.get('prop') or {}
        prop_id = EQ_STATE.get('prop_id') or {}
        loads = skill_isload_map()            # 技能袋 ucode -> 是否已装配在怪身上
        strong = _strong_prop_ids()
        contrib = 0
        use_skills = []                       # [ucode]
        use_props = {}                        # ucode -> num
        for m in matter:
            if not isinstance(m, dict):
                return b'[71]'
            mu = str(m.get('ucode') or '')
            try:
                mn = int(m.get('num') or 1)
            except Exception:
                mn = 1
            mtype = str(m.get('type') or '')
            if not mu or mn <= 0:
                return b'[71]'
            if mtype == '1':                  # 技能书素材
                if mu == skillucode:
                    return b'[123]'           # 目标技能自己不能当素材
                if mu not in bench:
                    return b'[71]'
                if loads.get(mu):
                    return b'[123]'           # 技能已经在角色身上
                msid = bench[mu][0]
                if _skill_family(msid) != fam:
                    return b'[127]'           # 素材不是本体技能 (族不同)
                contrib += int(bench[mu][1] or 1)
                use_skills.append(mu)
            elif mtype == '2':                # 天书道具素材
                if mu not in prop_have:
                    return b'[71]'
                pid = int(prop_id.get(mu) or 0)
                if strong and pid not in strong:
                    return b'[71]'            # 不是天书(L02 Type 34)
                if int(prop_have.get(mu, 0)) < mn:
                    return b'[71]'
                contrib += mn
                use_props[mu] = use_props.get(mu, 0) + mn
            else:
                return b'[71]'
        if contrib <= 0:
            return b'[71]'
        if lv + contrib > maxlv:
            return b'[126]'                   # 技能强化所选的素材过多
        cost = int(_T23_136 or _COST['intensify']) * contrib
        new_lv = lv + contrib
    else:
        # ---------- 无素材: 保留旧行为 (只花金币升 1 级) ----------
        use_skills, use_props = [], {}
        cost = int(_COST['intensify'])
        new_lv = lv + 1
        if new_lv > maxlv:
            return b'[125]'

    nxt = _L93_LV_ROWS.get((fam, new_lv))
    if nxt is None:
        return b'[125]'
    if EQ_STATE['gold'] < cost:
        return b'[77]'
    # ----- 生效 -----
    EQ_STATE['gold'] -= cost
    for mu in use_skills:
        bench.pop(mu, None)
    for mu, mn in use_props.items():
        left = int(EQ_STATE['prop'].get(mu, 0)) - mn
        if left > 0:
            EQ_STATE['prop'][mu] = left
        else:
            EQ_STATE['prop'].pop(mu, None)
            EQ_STATE.get('prop_id', {}).pop(mu, None)
    _nid, _nlv = _L93_BY_ID[nxt]
    bench[skillucode] = [nxt, _nlv, 1]
    bump_task(CT_SKILLUP, 1)          # 每日任务: 技能升级 (一次强化算一次)
    save_state()
    # ★ 必须回报扣了哪些素材 —— 否则客户端背包里的天书/技能书不会消失 (就是用户报的"天书没消耗")
    matter_out = [[1, mu] for mu in use_skills] + [[2, mu, mn] for mu, mn in use_props.items()]
    data = [EQ_STATE['gold'], _skill_item(skillucode), matter_out]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============ 1505 技能替换 ============
def handle_skill_replace(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    ucode = str(req.get('ucode') or '')
    goal = str(req.get('goalucode') or '')
    replace = str(req.get('replaceucode') or '')
    if ucode not in EQ_STATE['monsters'] and ucode not in ('1', '2', '3'):
        return b'[2004]'
    bench = ensure_skills()
    if goal not in bench:
        return b'[120]'                       # 被替换的技能id错误
    if replace not in bench:
        return b'[122]'                       # 替换的技能id错误
    if not bench[replace][2]:
        return b'[124]'                       # 学习的技能未解锁
    slots = _monster_slots(ucode)
    idx = None
    for i, u in enumerate(slots):
        if str(u) == goal:
            idx = i
            break
    if idx is None:
        return b'[120]'                       # 该怪未装配 goal 技能
    if replace in [str(x) for x in slots]:
        return b'[119]'                       # 不能重复装配
    # 替换消耗金币 = 学习同价 (客户端 SkillChange 读 T23 135)
    cost = int(_COST['study'])
    if EQ_STATE['gold'] < cost:
        return b'[77]'
    EQ_STATE['gold'] -= cost
    slots[idx] = replace
    save_state()
    mi = _mon_with_state(ucode)
    data = [EQ_STATE['gold'], mon_info_slot(mi, ucode, '1505'),
            _skill_item(goal), _skill_item(replace)]
    # ★v57: 把"成功路径上 mi 必有值"这一推断变成断言(见交接文档 §8.3)
    assert data[1] is not None, '1505: 成功响应不得含 null'
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')
