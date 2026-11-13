# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 特性系统处理器 (6007 SPECIALTY_UPGRADE 单特性升级 / 6008 SPECIALTY_SUMUPGRADE 总等级升级)
# 客户端契约 (源码级, client Lua):
#   NetSpecialtyUpgrade (6007): sendMessage(ucode, slot, tallentid, matterlist=同卡怪物ucode列表)
#     recvMessage: msg=[roleInfo, monsterInfo(30), matterList(怪物ucode列表, 客户端 monster_deleteItem; [1]=="" 跳过), equipdata]
#     m_errorCode: 70 怪物为空 / 58 鬼道积分不足 / 30022 已达最高 / 30023 所需道具为空 /
#                  30027 不是同卡 / 30025 槽位错误 / 30026 不存在该特性
#   NetSpecialtySumUpgrade (6008): sendMessage(ucode, matterlist=[{matterucode,num}..] 特性经验道具)
#     recvMessage: msg=[roleInfo, monsterInfo(30), matterList(道具item[ucode,id,count] updateItem), tallentList(解锁特性)]
#     m_errorCode: 70 怪物为空 / 61 金币不足 / 30022 已达最高 / 30023 所需道具为空 / 30024 所需道具错误
#   规则来源:
#     SpecialtyUpgrade.lua confirm(): L97_2 按 FeatureLV=目标等级 (self.specialtyClass+1) 查行 -> FeatureCost/IfMonsterCost/MonsterNum
#     TeamDataManager.getSpecialtyData: MON 模式同卡 = L91 SourceID 相同; SPE 模式道具 = L02 Type==41 (人参果1012)
#   ★v46(2026-09-13): 人参果单果经验 **50 -> 250**（用户要求; 满级 27250 经验 = 109 个, 原 545 个）。
#     权威值在 `data/tables/L02_Loots.json` 的 ConsumablesValue，本模块只读表、不写死数值。
#     客户端本地表 `assets/res/data/L02_Loots.json` 必须同改 (见 build_lua_patches.patch_data_tables)
#     —— 否则 SpecialtyUpgrade:updatePreview 会按 50 算升级预览，与实际生效值脱节。
#     ⚠ 不需要动 FEAT_ALGO_VERSION: 特性数据源指纹只哈希 L91_Status (state._feat_source_paths)，
#       本改动只影响"每颗果子给多少经验"，不影响任何特性 id 的取值。
#     SpecialtyUpgrade.updatePreview: 金币消耗 = T23[239] x 道具个数 (每道具100金)
#     总等级阶梯 L97_1: FeatureLV=1..6 -> UpgradeExp=250/1000/3000/8000/15000 (lv6=0 封顶), ExpTotal 累计
#   SDK 文案: "满级10级，升级消耗鬼道积分，部分特性Lv4,Lv6,Lv8,Lv10级时，分别需要额外消耗1个本体妖怪"
import json
from state import (EQ_STATE, save_state, get_monster_base, get_monster_source,
                   ensure_features, _tallent_class_map)
from handlers_equip import _mon_with_state, mon_info_slot
from handlers_cult import _remove_monster
from handlers_game import _role_info

# ---------- 静态表 (L97 / L02 / T23) ----------
def _load_json(name):
    # ★v60 闭环 v58 §10 缺口 #3: 改走 state.load_table —— 按「尾字段 u32 长度」精确剥离,
    #   不再用 c[:c.rfind(']')+1]（长度字段含 0x5D 时会切错 -> 调用方 except -> 静默空表,
    #   实测 L98_AIPriority / T32_Achievement）。护栏: work/_v60_legacy_tail_guard_test.py
    #   ⚠ 必须**逐行复制**: load_table 的结果是**带缓存的同一个对象**, 而旧实现每次返回全新对象。
    #     调用方若原地改行(排序/填字段)会污染全局缓存 -> 这里保持"每次都是新对象"的原语义。
    from state import load_table as _lt
    # 本文件原先写死盘符 (r'<repo>\...\data/tables'); 改走 load_table 默认目录
    #   = state.table_dir() (由 state.py 位置推导, 不写死盘符), 与本表目录等价。
    return [list(_r) for _r in _lt(name)]

_L97_2 = {}       # (feature_id, feature_lv) -> [FeatureCost, IfMonsterCost, MonsterNum]
_L97_1 = {}       # feature_lv -> UpgradeExp (总等级阶梯)
_L02_T41 = {}     # prop_id -> ConsumablesValue (经验值, Type=41 特性经验道具)
_T23_239 = None   # 单道具金币消耗

def _init_tables():
    global _T23_239
    if _L97_2:
        return
    t = _load_json('L97_2_TallentLv.json')
    h = {c: k for k, c in enumerate(t[0])}
    for r in t[1:]:
        try:
            _L97_2[(int(r[h['FeatureID']]), int(r[h['FeatureLV']]))] = \
                (int(r[h['FeatureCost']] or 0), int(r[h['IfMonsterCost']] or 0), int(r[h['MonsterNum']] or 0))
        except Exception:
            continue
    t = _load_json('L97_1_TallentLv.json')
    h = {c: k for k, c in enumerate(t[0])}
    for r in t[1:]:
        try:
            _L97_1[int(r[h['FeatureLV']])] = int(r[h['UpgradeExp']] or 0)
        except Exception:
            continue
    t = _load_json('L02_Loots.json')
    h = {c: k for k, c in enumerate(t[0])}
    for r in t[1:]:
        try:
            if str(r[h['Type']]) == '41':
                _L02_T41[int(r[h['ID']])] = int(r[h['ConsumablesValue']] or 0)
        except Exception:
            continue
    t = _load_json('T23_Function.json')
    h = {c: k for k, c in enumerate(t[0])}
    for r in t[1:]:
        try:
            if str(r[h['ID']]) == '239':
                _T23_239 = int(r[h['Value']] or 0)
        except Exception:
            continue

def _featlev(ucode):
    """特性总等级 (0..6) = **已解锁的特性系别槽数**。"""
    try:
        return int(EQ_STATE.get('monster_featlev', {}).get(str(ucode), 0) or 0)
    except Exception:
        return 0


def feat_slot_unlocked(ucode, slot):
    """槽位是否已解锁 —— ★v44 特性系统的核心门禁。

    ★为什么必须有这个门禁 (2026-09-13 用户报"我升级了不获得特性吗？"时定的案):
      客户端 `SpecialtyUpgrade.lua:652-658` 在**特性总等级升级界面**里按 featlev 点亮 6 个节点:
          for i=1,6 do
              if i > tonumber(self.m_intensifylevel) then node[i]:setOpacity(65)   -- 暗 = 未解锁
              else                                    node[i]:setOpacity(255)      -- 亮 = 已解锁
          且每个节点的文案是 `getLocal(SPECIALTY_UNLOCK)..data` = "解锁特性：<系别名>"
      6 个系别名来自 `T41_CodeKey[1099] SPECIALTY_TYPE_TEXT`
          = "基础系属性；|增量系属性；|暴击系属性；|控制系属性；|克制系属性；|高级系属性；"
      而每个系别槽的候选特性 = `L97_Tallents` 里 `UnLockLevel == 槽号` 的行
          (客户端 SkillHome:getClassSpecialityData(i) -> SDSpecialty:getUnLockLevelData(i))。
      ⇒ **`monster_featlev` 就是"解锁到第几个系别槽"**:
            featlev=1 -> 只能养成 基础系(仙脉/鬼爪/妖壳)
            featlev=3 -> 基础系+增量系+暴击系
            featlev=6 -> 六系全开
      旧实现**完全没有这道门禁** —— 于是"特性总等级"变成纯计数器:
      玩家喂了人参果把它升上去, 却看不到任何变化, 六个槽从一级就能随便激活。
      这正是用户那句"我升级了不获得特性吗？"的直接来源。
    """
    return 1 <= int(slot) <= _featlev(ucode)


def _feat_slot(ucode, slot):
    # 槽位(1-6) 当前 (feature_id, lv, is_use_entries); 空槽 -> (None, 0, [])
    #   槽串格式 "id#lv#isUse" (同槽多特征 & 连接; 客户端 analysisAllSpeciality 按 & 拆)
    slots = ensure_features(ucode)
    s = slots[slot - 1] if 0 < slot <= 6 else ''
    if not s or s == '0':
        return None, 0, []
    entries = []
    for part in str(s).split('&'):
        parts = part.split('#')
        if len(parts) >= 2:
            try:
                entries.append((parts[0], int(parts[1])))
            except Exception:
                continue
    if not entries:
        return None, 0, []
    # 首个为生效档 (isUse=1); 当前等级取列表内该特征自己的 lv
    for fid, lv in entries:
        return (fid, lv, entries)
    return None, 0, []

def _set_feat_slot(ucode, slot, fid, lv):
    slots = ensure_features(ucode)
    while len(slots) < 6:
        slots.append('')
    cur = str(slots[slot - 1] or '')
    parts = [p for p in cur.split('&') if p]
    new_parts = []
    is_first_use = True
    done = False
    for p in parts:
        pp = p.split('#')
        if len(pp) >= 2 and pp[0] == str(fid):
            pp[1] = str(lv)
            pp[2] = '1' if is_first_use else '0'
            done = True
            is_first_use = False
        else:
            pp[2] = '0'
        new_parts.append('#'.join(pp))
    if not done:
        new_parts.append('%s#%d#%s' % (str(fid), lv, '1' if is_first_use else '0'))
    slots[slot - 1] = '&'.join(new_parts)
    return slots[slot - 1]

# ============================================================================
# 6007 SPECIALTY_UPGRADE: 单个特性升级 (鬼道积分 + 可选本体卡)
# ============================================================================
def handle_specialty_upgrade(body_json):
    _init_tables()
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[70]'
    if not isinstance(req, dict):
        return b'[70]'
    ucode = str(req.get('ucode') or '')
    try:
        slot = int(req.get('slot') or 0)
        tallentid = str(req.get('tallentid') or '')
    except Exception:
        return b'[70]'
    matter = req.get('matterlist') or []
    if get_monster_base(ucode) is None:
        return b'[70]'
    if not (1 <= slot <= 6):
        return b'[30025]'
    # ★v44 说明: **升级(1181) 故意不加 featlev 门禁** ——
    #   怪的**固有特性**是数据(或 YGTW 回退)直接种进槽里的, 它的 featlev 可能还是 0;
    #   若在这里也要求 slot<=featlev, 玩家就会看到"我明明有这个特性却升不了"。
    #   门禁只加在**往空槽里添加新特性**的 1183 上 —— 那才是"总等级解锁槽位"要管的事。
    # 槽位特性存在 + id 匹配 (同槽 & 多特征: 任一匹配即可)
    fid, cur_lv, entries = _feat_slot(ucode, slot)
    if not entries:
        return b'[30026]'
    if not any(str(f) == str(tallentid) for f, _ in entries):
        return b'[30026]'
    cur_lv = 0
    for f, lv in entries:
        if str(f) == str(tallentid):
            cur_lv = lv
            break
    next_lv = cur_lv + 1
    if next_lv > 10:
        return b'[30022]'
    cost_row = _L97_2.get((int(tallentid), next_lv))
    if cost_row is None:
        return b'[30022]'
    cost, if_mon, need_mon = cost_row
    # 鬼道积分
    if int(EQ_STATE.get('kidou', 0)) < cost:
        return b'[58]'
    # 本体卡 (同卡 = SourceID 相同; 数量按表行)
    if if_mon and need_mon > 0:
        mons = [str(m) for m in (matter if isinstance(matter, list) else [])]
        if not mons:
            return b'[30023]'
        if len(mons) != need_mon:
            return b'[30023]'
        src_self = get_monster_source(ucode)
        for mu in mons:
            if get_monster_base(mu) is None or mu == ucode:
                return b'[30027]'
            if get_monster_source(mu) != src_self:
                return b'[30027]'
    # ----- 生效 -----
    EQ_STATE['kidou'] = int(EQ_STATE.get('kidou', 0)) - cost
    consumed = []
    if if_mon and need_mon > 0:
        for mu in matter:
            _remove_monster(str(mu))
            consumed.append(str(mu))
    _set_feat_slot(ucode, slot, tallentid, next_lv)
    save_state()
    mi = _mon_with_state(ucode)
    matter_list = consumed if consumed else ['']
    data = [_role_info(), mon_info_slot(mi, ucode, '1181'), matter_list, []]
    # ★v57: 同 1180 —— 把"成功路径上 mi 必有值"变成断言（见文档 §8.3）
    assert data[1] is not None, '1181: 成功路径上怪物必存在 -> 响应不得含 null'
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============================================================================
# 1182 SPECIALTY_CHANGE: 切换同槽生效特性 (isUse=1 翻转)
#   客户端 NetSpecialtyChange:recvMessage: monsterInfo = msg (怪物item即响应主体!)
#   sendMessage(ucode, slot, tallentid); 错误码 70/30025/30026/30028(已使用)
# ============================================================================
def handle_specialty_change(body_json):
    _init_tables()
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[70]'
    if not isinstance(req, dict):
        return b'[70]'
    ucode = str(req.get('ucode') or '')
    try:
        slot = int(req.get('slot') or 0)
        tallentid = str(req.get('tallentid') or '')
    except Exception:
        return b'[70]'
    if get_monster_base(ucode) is None:
        return b'[70]'
    if not (1 <= slot <= 6):
        return b'[30025]'
    # 槽内 & 列表: 目标特性必须存在
    slots = ensure_features(ucode)
    while len(slots) < 6:
        slots.append('')
    cur = str(slots[slot - 1] or '')
    parts = [p for p in cur.split('&') if p]
    if not parts:
        return b'[30026]'
    target = None
    for p in parts:
        pp = p.split('#')
        if len(pp) >= 2 and pp[0] == str(tallentid):
            target = (pp[0], pp[1], pp[2] if len(pp) > 2 else '0')
            break
    if target is None:
        return b'[30026]'
    if target[2] == '1':
        return b'[30028]'                    # 已使用
    new_parts = []
    for p in parts:
        pp = p.split('#')
        if len(pp) >= 2:
            if pp[0] == str(tallentid):
                pp[2] = '1'
            else:
                pp[2] = '0'
            new_parts.append('#'.join(pp))
    slots[slot - 1] = '&'.join(new_parts)
    save_state()
    mi = _mon_with_state(ucode)
    return ('[1,' + (mi if mi else '["%s"]' % ucode) + ']').encode('utf-8')

# ============================================================================
# 1183 SPECIALTY_EXTENSION: 扩充/激活该槽一个未解锁特性 (消耗鬼道积分)
#   客户端 NetSpecialtyExtension:recvMessage: msg=[roleInfo, monsterInfo, tallentId]
#   (SpecialtyInfo.setSpecialtyListUI: needPrice=L97_2.ID=100100.FeatureCost=70 鬼道积分)
# ============================================================================
_EXT_COST = 70

def handle_specialty_extension(body_json):
    _init_tables()
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[70]'
    if not isinstance(req, dict):
        return b'[70]'
    ucode = str(req.get('ucode') or '')
    try:
        slot = int(req.get('slot') or 0)
    except Exception:
        return b'[70]'
    if get_monster_base(ucode) is None:
        return b'[70]'
    if not (1 <= slot <= 6):
        return b'[30025]'
    # ★v44 门禁: 该系别槽必须先由"特性总等级"解锁 (见 feat_slot_unlocked 的长注释)
    if not feat_slot_unlocked(ucode, slot):
        return b'[30025]'
    slots = ensure_features(ucode)
    have = set()
    for p in str(slots[slot - 1] or '').split('&'):
        if '#' in p:
            have.add(p.split('#')[0])
    # 该类别 (UnLockLevel==slot) 的候选特性 (L97_Tallents)
    cmap = _tallent_class_map()
    _tall_classes = cmap if isinstance(cmap, dict) else {}
    cands = [tid for tid, cls in sorted(_tall_classes.items(), key=lambda kv: int(kv[0]))
             if int(cls) == slot and str(tid) not in have]
    if not cands:
        return b'[30026]'
    new_id = str(cands[0])
    if int(EQ_STATE.get('kidou', 0)) < _EXT_COST:
        return b'[58]'
    EQ_STATE['kidou'] = int(EQ_STATE.get('kidou', 0)) - _EXT_COST
    cur = str(slots[slot - 1] or '')
    # ★v44 修 (用户"激活了却不获得特性"的真正机制):
    #   旧实现无条件写 `'%s#0#0'` —— 如果这个槽本来是**空的**(或里面没有任何 isUse=1 的档),
    #   新加的这条 isUse 也是 0 -> 整个槽**没有任何生效档** -> 特性不生效, UI 上也没有选中项。
    #   玩家花了 70 鬼道积分"激活", 结果什么也没发生。
    #   正确: 该槽原本没有生效档时, 新加的这条就是生效档 (isUse=1);
    #         槽里已经有生效档时保持 0 (之后用 1182 换档)。
    _has_active = any(('#' in _p and _p.split('#')[2:3] == ['1'])
                      for _p in cur.split('&') if _p)
    slots[slot - 1] = (cur + '&' if cur else '') + ('%s#0#1' if not _has_active else '%s#0#0') % new_id
    save_state()
    mi = _mon_with_state(ucode)
    data = [_role_info(), mon_info_slot(mi, ucode, '1183'), new_id]
    # ★v57: 把"成功路径上 mi 必有值"这一**推断**变成断言(见交接文档 §8.3)
    assert None not in data, '1183: 成功响应不得含 null'
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============================================================================
# 6008 SPECIALTY_SUMUPGRADE: 特性总等级升级 (消耗 Type=41 经验道具 + 金币; 阶梯 L97_1; 最高6)
# ============================================================================
def handle_specialty_sum_upgrade(body_json):
    _init_tables()
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[70]'
    if not isinstance(req, dict):
        return b'[70]'
    ucode = str(req.get('ucode') or '')
    matter = req.get('matterlist') or []
    if get_monster_base(ucode) is None:
        return b'[70]'
    if not isinstance(matter, list) or not matter:
        return b'[30023]'
    # 解析 + 校验道具 (matter=[{matterucode,num}..], 客户端 sendMessage: data.type=3 由 NetSpecialtySumUpgrade 组装)
    items = []                     # (ucode, prop_id, num)
    exp_sum = 0
    for m in matter:
        if not isinstance(m, dict):
            continue
        mu = str(m.get('matterucode') or '')
        try:
            num = int(m.get('num') or 1)
        except Exception:
            num = 1
        if not mu or num <= 0 or mu not in EQ_STATE['prop']:
            continue
        pid = EQ_STATE.get('prop_id', {}).get(mu)
        if pid is None or int(pid) not in _L02_T41:
            return b'[30024]'
        have = int(EQ_STATE['prop'].get(mu, 0))
        if have < num:
            return b'[30023]'
        items.append((mu, int(pid), num))
        exp_sum += _L02_T41[int(pid)] * num
    if not items:
        return b'[30023]'
    # 金币
    gold_cost = int(_T23_239 or 100) * sum(it[2] for it in items)
    if int(EQ_STATE.get('gold', 0)) < gold_cost:
        return b'[61]'
    # ----- 阶梯升级 (客户端 SpecialtyUpgrade:updatePreview 语义, 2026-09-07 复核修正) -----
    #   客户端: 阈值 = L97_1 行(FeatureLV=当前lv).UpgradeExp; L->L+1 需 UpgradeExp(L);
    #   初始总等级 = 1 (SQMonster.getfeatureLv nil 兜底=1; row(0) 不存在, lv0 会令 update() 索引 nil 崩溃);
    #   经验链验证: 250+1000+3000+8000+15000 = 27250 = L97_1 ExpTotal(6) ✓; 满级6后溢出清零(客户端 cap 置 0)
    lv = int(EQ_STATE['monster_featlev'].get(ucode, 1) or 1)
    if lv < 1:
        lv = 1
    elif lv > 6:
        lv = 6
    # 已达最高: 客户端 m_errorCode[30022] = "特性等级已达到最高"; 不扣资源 (客户端 m_type==1 无满级门, 依赖服务器)
    if lv >= 6:
        return b'[30022]'
    cur_exp = int(EQ_STATE['monster_featexp'].get(ucode, 0)) + exp_sum
    while lv < 6:
        need = _L97_1.get(lv, 0)
        if need <= 0 or cur_exp < need:
            break
        cur_exp -= need
        lv += 1
        if lv >= 6:
            cur_exp = 0
            break
    # ----- 消耗 -----
    EQ_STATE['gold'] = int(EQ_STATE['gold']) - gold_cost
    matter_ret = []
    for mu, pid, num in items:
        EQ_STATE['prop'][mu] = int(EQ_STATE['prop'].get(mu, 0)) - num
        if EQ_STATE['prop'][mu] <= 0:
            EQ_STATE['prop'].pop(mu, None)
            EQ_STATE.get('prop_id', {}).pop(mu, None)
        matter_ret.append([mu, pid, int(EQ_STATE['prop'].get(mu, 0))])
    EQ_STATE['monster_featlev'][ucode] = lv
    EQ_STATE['monster_featexp'][ucode] = cur_exp
    save_state()
    mi = _mon_with_state(ucode)
    data = [_role_info(), mon_info_slot(mi, ucode, '1180'), matter_ret, []]
    # ★v57: 把"成功路径上 mi 必有值"这个**推断**变成断言 —— 推断若不成立就显式报错,
    #   而不是静默发出 null 让客户端 pairs()/next() 抛错(6002/6003 就是这么炸的)。
    #   客户端 NetSpecialtySumUpgrade 对 msg[2] 是索引消费(null 安全), 但 LAW-A 是全局铁律。
    assert data[1] is not None, '1180: 成功路径上怪物必存在 -> 响应不得含 null'
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')
