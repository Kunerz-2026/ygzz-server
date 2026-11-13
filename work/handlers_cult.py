# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 妖怪养成处理器 (吞噬 CULTIVATE_DEVOUR 6002)
# 对标游戏数值 (对标 CultivateSwallow.lua / TeamDataManager:getSwallowData / SDMonster):
#   材料妖怪贡献: TypeGrow(1命/2攻/3防/4暴/5经验) 来自 L91 HpGrow/AtkGrow/DefGrow/CriGrow;
#                 ValueGrow = 对应 Grow 值;  GrowGet = 成长值贡献 (item字段6);
#   目标妖怪: GrowMax(成长值上限, L91); 属性加成累加到 item字段10-13 (命/攻/防/暴);
#             经验型材料(type5)才加经验 -> 升级.
#   6002 resp [1,[eggcoin, soulcoin, goldcoin, monsterInfo, matterlist, equipdata, skillList, talent, skill]]
import json, os, random
from state import (EQ_STATE, save_state, get_monster_base, get_monster_lv,
                   get_monster_exp, get_monster_grow, get_monster_add, set_monster_grow_add,
                   ensure_skills, load_table)
from config_data import _mon
from handlers_equip import _mon_with_state, mon_info_slot
from handlers_task import bump_task, CT_DEVOUR
from handlers_skill import _L93_BY_ID, _L93_LV_ROWS, _skill_item

from state import table_dir as _table_dir   # ★2026-10-02 (审计 §1.6): 表根不再写死盘符
_DATA = _table_dir()

def _load_json(name):
    # ★v60 闭环 v58 §10 缺口 #3: 改走 state.load_table —— 按「尾字段 u32 长度」精确剥离,
    #   不再用 c[:c.rfind(']')+1]（长度字段含 0x5D 时会切错 -> 调用方 except -> 静默空表,
    #   实测 L98_AIPriority / T32_Achievement）。护栏: work/_v60_legacy_tail_guard_test.py
    #   ⚠ 必须**逐行复制**: load_table 的结果是**带缓存的同一个对象**, 而旧实现每次返回全新对象。
    #     调用方若原地改行(排序/填字段)会污染全局缓存 -> 这里保持"每次都是新对象"的原语义。
    return [list(_r) for _r in load_table(name, _DATA)]

# L100_MonsterExp: Lv -> Exp(升到下级的经验)
_MONSTER_EXP = {}
# L100 TotalExp: Lv -> TotalExp (客户端怪物素材经验公式用: TotalExp(素材lv) * 0.7 * ExpRatio)
_MONSTER_TOTAL = {}
# L91_Status: mid -> (GrowMax, HpGrow, AtkGrow, DefGrow, CriGrow, GrowGet, Star, ExpRatio)
_MON_GROW = {}
# L104_RevLv: star -> MstLvLimit (怪物等级上限)
_LV_LIMIT = {}
# L02_Loots: 道具静态ID -> (Type, ConsumablesValue, GrowGet)  (吞噬道具语义)
_LOOT = {}
# T23_Function: 105=经验折损系数 107=吞噬基础金币 181=等级系数
_T23 = {}
try:
    _l91 = _load_json('L91_Status.json')
    _h9 = {h: k for k, h in enumerate(_l91[0])}
    for _r in _l91[1:]:
        try:
            _mid = int(_r[_h9['ID']])
            _gm = int(_r[_h9['GrowMax']] or 0)
            _hg = int(_r[_h9['HpGrow']] or 0)
            _ag = int(_r[_h9['AtkGrow']] or 0)
            _dg = int(_r[_h9['DefGrow']] or 0)
            _cg = int(_r[_h9['CriGrow']] or 0)
            _gg = int(_r[_h9['GrowGet']] or 0)
            _st = int(_r[_h9['Star']] or 1)
            _er = float(_r[_h9['ExpRatio']] or 1)
            _MON_GROW[_mid] = (_gm, _hg, _ag, _dg, _cg, _gg, _st, _er)
        except Exception:
            continue
    _l100 = _load_json('L100_MonsterExp.json')
    _h10 = {h: k for k, h in enumerate(_l100[0])}
    _has_total = 'TotalExp' in _h10
    for _r in _l100[1:]:
        try:
            _MONSTER_EXP[int(_r[_h10['Lv']])] = int(_r[_h10['Exp']] or 0)
            if _has_total:
                _MONSTER_TOTAL[int(_r[_h10['Lv']])] = int(_r[_h10['TotalExp']] or 0)
        except Exception:
            continue
    _l104 = _load_json('L104_RevLv.json')
    _h104 = {h: k for k, h in enumerate(_l104[0])}
    for _r in _l104[1:]:
        try:
            _LV_LIMIT[int(_r[_h104['RevLv']])] = int(_r[_h104['MstLvLimit']] or 30)
        except Exception:
            continue
    _l02 = _load_json('L02_Loots.json')
    _h02 = {h: k for k, h in enumerate(_l02[0])}
    for _r in _l02[1:]:
        try:
            _LOOT[int(_r[_h02['ID']])] = (int(_r[_h02['Type']] or 0),
                                          int(_r[_h02['ConsumablesValue']] or 0),
                                          int(_r[_h02['GrowGet']] or 0))
        except Exception:
            continue
    _t23 = _load_json('T23_Function.json')
    _h23 = {h: k for k, h in enumerate(_t23[0])}
    for _r in _t23[1:]:
        try:
            _T23[int(_r[_h23['ID']])] = float(_r[_h23['Value']] or 0)
        except Exception:
            continue
except Exception as _e:
    import sys as _s
    _s.stdout.write('CULT_LOAD_ERR: %r\n' % _e)
    _s.stdout.flush()

def _mid_of(ucode):
    b = get_monster_base(ucode)
    return int(b[0]) if b else None

def _material_contrib(ucode):
    # 材料妖怪 -> (type 1命/2攻/3防/4暴/5经验, 值, 成长值)
    mid = _mid_of(ucode)
    g = _MON_GROW.get(mid)
    if g is None:
        return 0, 0, 0
    _gm, hg, ag, dg, cg, gg, _st, _er = g
    if hg != 0:
        sw, val = 1, hg
    elif ag != 0:
        sw, val = 2, ag
    elif dg != 0:
        sw, val = 3, dg
    elif cg != 0:
        sw, val = 4, cg
    else:
        sw, val = 5, 0
    return sw, val, gg

def _star_of(target):
    base = get_monster_base(target)
    if base is None:
        return 1
    g = _MON_GROW.get(int(base[0]))
    return int(g[6]) if g else 1

def _lv_limit(target):
    # 星1=10 星2=15 星3=20 星4=25 星5=30 (客户端 tm.getMonsterLvLimit = L104_RevLv.MstLvLimit)
    return _LV_LIMIT.get(_star_of(target), 30)

def _exprap_of(mid):
    g = _MON_GROW.get(mid)
    return float(g[7]) if g else 1.0

def _mat_gold(mtype, ucode):
    # 客户端金币消耗 (CultivateSwallow:updatePreview): 怪物=200+50*(素材等级-1), 道具=200
    base = _T23.get(107, 200)
    coef = _T23.get(181, 50)
    if mtype == 1:
        mlv = get_monster_lv(ucode)
        return int(base + coef * (mlv - 1))
    return int(base)

def _mat_exp_for_monster(ucode):
    # 客户端 getSwallowData: 经验型怪物素材 exp = TotalExp(素材lv) * 0.7 * ExpRatio
    mid = _mid_of(ucode)
    mlv = get_monster_lv(ucode)
    total = _MONSTER_TOTAL.get(mlv, 0)
    lose = _T23.get(105, 0.7)
    return int(total * lose * _exprap_of(mid))

def _same_card_skill_upgrade(target):
    # 客户端文案: "吞噬同卡可以随机升级一个[默认技能]" (CultivateSwallow:updatePreview L755 消耗注释)
    #   -> 随机选目标"装配"技能之一, 按 L93 技能族行表 +1 级 (族内满级则返回 None)
    slots = EQ_STATE.get('monster_skills', {}).get(target) or []
    cands = [str(s) for s in slots if str(s) not in ('0', '', '0')]
    if not cands:
        print('[DEVOUR] same-card: no equipped skills for %s' % target, flush=True)
        return None
    k = random.choice(cands)
    bench = ensure_skills()
    cur = bench.get(k)
    if not cur:
        return None
    sid, lv, isopen = cur
    fam, _lv = _L93_BY_ID.get(sid, (0, 0))
    if fam == 0:
        print('[DEVOUR] same-card: skill %s not in L93 family' % sid, flush=True)
        return None
    nxt = _L93_LV_ROWS.get((fam, lv + 1))
    if nxt is None:
        print('[DEVOUR] same-card: skill %s family %s lv%d maxed' % (sid, fam, lv), flush=True)
        return None                          # 已满级
    _nid, _nlv = _L93_BY_ID[nxt]
    bench[k] = [nxt, _nlv, isopen]
    print('[DEVOUR] same-card: target=%s skill %s -> %s (lv%d)' % (target, sid, nxt, _nlv), flush=True)
    return _skill_item(k)

def handle_monster_devour(body_json):
    # req {monsterucode, matterlist:[{type,ucode,num},..]}
    # 语义对照客户端 (CultivateSwallow:updatePreview / TeamDataManager:getSwallowData):
    #   目标怪: 任何已拥有怪物(含初始3只 _MONSTER_BASE, 曾被误拒 [70])
    #   材料怪 type1: 属性型加对应属性+成长值(GrowGet, 上限 GrowMax); 经验型 exp = TotalExp*0.7*ExpRatio
    #   材料道具 type3: L02_Loots Type 5-8=属性(ConsumablesValue), 10=经验(ConsumablesValue), GrowGet 加成长
    #   金币: 200 + 50*(素材等级-1) / 道具 200; 等级上限 L104_RevLv.MstLvLimit(星)
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    target = str(req.get('monsterucode') or '')
    matter = req.get('matterlist') or []
    if get_monster_base(target) is None:
        return b'[70]'
    if not isinstance(matter, list) or not matter:
        return b'[71]'
    tmid = _mid_of(target)
    tg = _MON_GROW.get(tmid)
    if tg is None:
        return b'[0,{}]'
    t_growmax, thg, tag, tdg, tcg, _, _st, _er = tg
    lv = get_monster_lv(target)
    exp = get_monster_exp(target)
    grow = get_monster_grow(target)
    ad = get_monster_add(target)
    max_lv = _lv_limit(target)
    # 1) 金币消耗预算 (T23[107]=200 + T23[181]=50*(素材等级-1); 道具等级=1)
    cost = 0
    for m in matter:
        if not isinstance(m, dict):
            continue
        mtype = int(m.get('type') or 0)
        ucode = str(m.get('ucode') or '')
        if not ucode:
            continue
        if mtype in (1, 3):
            cost += _mat_gold(mtype, ucode)
    if EQ_STATE['gold'] < cost:
        return b'[77]'
    print('[DEVOUR] target=%s matter=%d cost=%d lv_before=%d exp_before=%d' % (
        target, len(matter), cost, lv, exp), flush=True)
    skill_ups = []                      # msg[9] 技能升级列表 (同卡吞噬随机升一个默认技能)
    deducted = []                             # [[kind, ucode, num],..]
    for m in matter:
        if not isinstance(m, dict):
            continue
        mtype = int(m.get('type') or 0)
        ucode = str(m.get('ucode') or '')
        num = int(m.get('num') or 1)
        if not ucode:
            continue
        if mtype == 1:                        # 吞噬妖怪素材
            if ucode == target or get_monster_base(ucode) is None:
                continue
            sw, val, gg = _material_contrib(ucode)
            if sw == 5:                       # 经验型: TotalExp*0.7*ExpRatio
                exp += _mat_exp_for_monster(ucode)
            else:                             # 属性型: 加对应属性加成 + 成长值 (客户端: 成长未满才加属性)
                idx = {1: 0, 2: 1, 3: 2, 4: 3}.get(sw)
                if idx is not None and grow < t_growmax:
                    ad[idx] += val
                grow = min(grow + gg, t_growmax)
            if _mid_of(ucode) == tmid:        # 同卡吞噬 -> 随机升级一个默认技能 (客户端文案)
                try:
                    sup = _same_card_skill_upgrade(target)
                    if sup is not None:
                        skill_ups.append(sup)
                except Exception:
                    pass
            _remove_monster(ucode)
            deducted.append([1, ucode, 1])
        elif mtype == 3:                      # 吞噬道具素材
            if ucode not in EQ_STATE.get('prop_id', {}):
                continue
            stid = int(EQ_STATE['prop_id'].get(ucode, 0))
            loot = _LOOT.get(stid)
            if loot is None:
                continue
            have = EQ_STATE['prop'].get(ucode, 0)
            d = min(num, have)
            if d <= 0:
                continue
            otype, cvalue, gg = loot
            if otype == 10:                   # 经验型 (仙寿桃 Type=10 -> 20000/个)
                exp += d * cvalue
            elif otype in (5, 6, 7, 8):       # 属性型: Type 5-8 -> sw 1-4 (ConsumablesValue)
                idx = {5: 0, 6: 1, 7: 2, 8: 3}.get(otype)
                if idx is not None and grow < t_growmax:
                    ad[idx] += d * cvalue
            elif otype == 9:                  # 技能类: 本期按经验处理(客户端 swType=6)
                exp += d * cvalue
            if gg:
                grow = min(grow + d * gg, t_growmax)
            EQ_STATE['prop'][ucode] = have - d
            if EQ_STATE['prop'][ucode] <= 0:
                EQ_STATE['prop'].pop(ucode, None)
                EQ_STATE['prop_id'].pop(ucode, None)
            deducted.append([3, ucode, d])
    if not deducted:
        return b'[71]'
    EQ_STATE['gold'] -= cost            # 金币扣除 (与客户端 200+50*等级 公式一致; 无有效素材时不扣)
    # 2) 升级 (经验达到当前等级要求则升级; 上限=L104 星级上限, 达顶经验清零 同客户端)
    while lv < max_lv and exp >= _MONSTER_EXP.get(lv, 99999999):
        exp -= _MONSTER_EXP[lv]
        lv += 1
    if lv >= max_lv:
        exp = 0
    # 3) 落库 等级/经验/成长值/属性加成
    EQ_STATE['monster_lv'][target] = lv
    EQ_STATE['monster_exp'][target] = exp
    set_monster_grow_add(target, grow, ad[0], ad[1], ad[2], ad[3])
    bump_task(CT_DEVOUR, 1)          # 每日任务: 吞噬
    # ★2026-10-02 (成就进度写入方): T32 Type=1112「吞噬怪物次数达到N」-> 组 1004/级别1(妖魔相食)
    #   口径与上面 bump_task(CT_DEVOUR,1) 完全一致(一次成功吞噬 = 1), 不再另立一套计数。
    from handlers_achievement import note_ach
    note_ach(1112, 0, 1)
    save_state()
    monster_info = _mon_with_state(target)
    print('[DEVOUR] DONE target=%s lv=%d exp=%d grow=%d skill_ups=%d' % (
        target, lv, exp, grow, len(skill_ups)), flush=True)
    # matterlist 客户端 NetMonsterDevour:recvMessage 用 for i=1,#matterlist 遍历
    # [kind, ucode, num] (kind=1 删怪 / kind=3 扣道具), 单元素/空均无碍 -> 原样返回
    matter_out = deducted
    # ★★ v57 (2026-09-15) P0 修复: msg[9] 原来发 `skill_ups or None` -> 无技能升级时 JSON 里是 `null`。
    #   客户端 NetMonsterDevour.lua 对该字段的消费是:
    #       local skill = msg[9]
    #       if skill then for k,v in pairs(skill) do ... end end
    #   **cjson.null 是 userdata 且 truthy** -> `if skill then` 放行 -> `pairs(null)` 直接抛错
    #   -> 整个 recvMessage 中断 -> `self.m_callFunc(talent,upSkill)` 永不执行
    #   -> doSwallow 不跑 -> 界面不刷新(必须退出去重进才看到数值)。
    #   实测证据(logcat [ND9] 逐句探针): "B: if skill then (enter)" 与 "C: for pairs(skill)" 打印,
    #   而循环体内的 "D:" 不打印 => 断点确认在 pairs() 这一句。
    #   ⇒ 成功响应里**绝不能有 null**(铁律 LAW-A 的强化版); 无升级时发空数组, `pairs({})` 不迭代、安全。
    #   注意 msg[8] `talent` 也必须是"可 next() 的东西" —— doSwallow 里有 `if next(_tanlent)`,
    #   传 cjson.null 进去同样会炸。故这里一并发 []。
    data = [EQ_STATE['eggcoin'], EQ_STATE['soulcoin'], EQ_STATE['gold'],
            json.loads(monster_info), matter_out, [], [], [], list(skill_ups or [])]
    assert None not in data, 'LAW-A: 成功响应不得含 null (v57 吞噬刷新修复)'
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

def _remove_monster(ucode):
    # ---- 2026-09-07 修复: 技能袋清理 ----
    #   旧实现 EQ_STATE['skills'].pop(str(ucode)) 用怪 ucode 当键 --- 袋键是技能 id, 永不命中,
    #   被吞怪的技能以 isload=0 永久残留技能页 (用户验收 bug 直接机制).
    #   正确: 取被吞怪基础技能, 若任何其他怪不再装配 -> 从袋移除 (引用计数语义)
    _b = get_monster_base(ucode)
    # ---- 2026-09-07(第二轮) 修复: 装备释放 ----
    #   旧实现仅 pop monster_ueq 槽引用, 但装备 equip_isload 未置 0 -> 吞噬后装备仍"已装备"状态,
    #   客户端无法出售/给其他怪穿戴 (用户实测: 图1 第一件 Lv.15 已装备卡死)
    _ueq_old = (EQ_STATE.get('monster_ueq') or {}).get(ucode)
    EQ_STATE['monsters'].pop(ucode, None)
    EQ_STATE['monster_ueq'].pop(ucode, None)
    if _ueq_old:
        for _u in _ueq_old:
            if _u and str(_u) != '0':
                EQ_STATE['equip_isload'][str(_u)] = 0
    EQ_STATE['monster_lv'].pop(ucode, None)
    EQ_STATE['monster_exp'].pop(ucode, None)
    EQ_STATE['monster_grow'].pop(ucode, None)
    EQ_STATE['monster_add'].pop(ucode, None)
    EQ_STATE['monster_skin_eq'].pop(ucode, None)
    EQ_STATE['monster_skin_open'].pop(ucode, None)
    EQ_STATE['monster_skills'].pop(ucode, None)
    if _b:
        _ms_rest = list(EQ_STATE.get('monster_skills', {}).values())
        for _sid in _b[3]:
            if not _sid:
                continue
            _k = str(_sid)
            if not any(str(_u) == _k for _slots in _ms_rest for _u in _slots):
                EQ_STATE['skills'].pop(_k, None)
    EQ_STATE['monster_feat'].pop(str(ucode), None)      # 特性槽 (1181/6002 同卡吞噬后清理)
    # ★v41: 出处表要跟着一起清 —— 否则 monster_feat_src 会留下"孤儿"键
    #   (功能上无害: 重算只遍历 monster_feat; 但会让"条数一致"这类不变量失真,
    #    也会让存档越用越脏。这条是 _feat_source_selftest.py 当场抓出来的。)
    EQ_STATE.get('monster_feat_src', {}).pop(str(ucode), None)
    EQ_STATE['monster_featexp'].pop(str(ucode), None)
    EQ_STATE['monster_featlev'].pop(str(ucode), None)
    # 初始三只 (不在 monsters 字典): 用 monster_deleted 标记
    if ucode not in EQ_STATE['monsters']:
        dl = EQ_STATE.setdefault('monster_deleted', [])
        if str(ucode) not in dl:
            dl.append(str(ucode))

# ============================================================================
# 6009 CULTIVATE_RESET: 吞噬重置 (无启树根道具 L02 id=1030)
#   文案: "使用一个无启树根将成长值重置为0，同时会清除所有吞噬获得的属性加成"
#   客户端 NetMonsterReset: sendMessage({monsterucode}); recvMessage msg=[prop, monsterInfo]
#     prop = 道具item [ucode,id,count] (prop_updateItem); m_errorCode 为空 -> 错误码走全局 1013 表
# ============================================================================
def handle_monster_reset(body_json):
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[70]'
    if not isinstance(req, dict):
        return b'[70]'
    ucode = str(req.get('monsterucode') or '')
    if get_monster_base(ucode) is None:
        return b'[70]'
    # 无启树根 (L02 1030, Type=21): 服务器自查字典消耗 (客户端只发 monsterucode)
    root_uc = None
    for uc, pid in EQ_STATE.get('prop_id', {}).items():
        if str(pid) == '1030':
            root_uc = uc
            break
    if root_uc is None or int(EQ_STATE['prop'].get(root_uc, 0)) < 1:
        return b'[71]'                      # 素材为空 (全局 1013: 71 => 素材为空)
    EQ_STATE['prop'][root_uc] = int(EQ_STATE['prop'].get(root_uc, 0)) - 1
    if EQ_STATE['prop'][root_uc] <= 0:
        EQ_STATE['prop'].pop(root_uc, None)
        EQ_STATE.get('prop_id', {}).pop(root_uc, None)
    # 成长值清零 + 吞噬属性加成清除; 等级/经验/技能/特性保留 (文案只说成长+属性)
    EQ_STATE['monster_grow'][ucode] = 0
    EQ_STATE['monster_add'][ucode] = [0, 0, 0, 0]
    save_state()
    mi = _mon_with_state(ucode)
    prop_item = [root_uc, 1030, int(EQ_STATE['prop'].get(root_uc, 0))]
    data = [prop_item, mon_info_slot(mi, ucode, '6009')]
    # ★v57: 把"成功路径上 mi 必有值"这一**推断**变成断言(见交接文档 §8.3)
    assert None not in data, '6009: 成功响应不得含 null'
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')
