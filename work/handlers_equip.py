# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 装备穿戴(6004)/卸下(6005)/强化(6006) 处理器
import json, struct
from state import (EQ_STATE, BASE_EQUIP, BASE_PROP, ENHANCE_COST,
                   _EQUIP_TYPE, _TYPE_UEQ_IDX, get_monster_base, save_state,
                   get_monster_lv, get_monster_exp, get_monster_grow, get_monster_add,
                   get_feat_parts, equip_exp_ratio, equip_level_cap,
                   get_equip_append, set_equip_append, find_ghost, consume_ghost,
                   ensure_monster_ueq, equip_lv_limit, equip_class_static,
                   equip_type_static,
                   load_table)          # ★v32: 统一表加载器 (尾字段精确剥离 + 显式抛错)
from config_data import _mon
from handlers_task import bump_task, CT_INTENSIFY, CT_EQUIP

def extract_body_json(raw):
    # 从 framing-A 请求帧里可靠提取请求体 JSON 字符串。
    # 帧布局(已在 game_server.log 全部 7333 个观测帧验证, 无一例外):
    #   [0:5]   magic = 00 01 01 01 00
    #   [5:24]  固定头
    #   [24]    u8 内层消息长度 L (0x19/0x21/0x43/0x4a ...)
    #   [25:25+L]  CGMessage: [..][u32BE msgid @35:39][..][u16BE bodylen @46:47][utf8 json @48]
    # 即请求体 json 恒从 outer offset 48 开始, 前面 u16BE 为字节长度。
    if len(raw) < 48 or raw[:5] != b'\x00\x01\x01\x01\x00':
        return None
    blen = struct.unpack('>H', raw[46:48])[0]
    if blen <= 0 or len(raw) < 48 + blen:
        return None
    return raw[48:48+blen].decode('utf-8', 'replace')

# ---------- 装备强化经验表 (T22_EquipExp: Lv -> (Exp升级所需, ForgeExp材料经验); 客户端 EquipIntensify 权威公式) ----------
_T22 = None
def _cell_int(v, default=0):
    """表里同一个数值列会混着两种写法: '3830' 与 '3830.0'。

    ★v32 事故复盘 (t22 Lv20 整行丢失):
      `int('3830.0')` 会抛 ValueError。旧实现 `int(r[h['Exp']])` 外面套 `except: continue`,
      于是 **T22_EquipExp 的 Lv20 那行被整个丢掉**(全 30 行里唯一带小数的一格),
      而升级循环 `while ... total_exp >= t22.get(lv, (0,0))[0]` 把"缺失的等级"当成
      **"需要 0 经验"** -> 20→21 **白送一级**; 客户端 `tonumber("3830.0")` = 3830 正常收费
      -> **预览与实际结果不一致**。
      一律走 float 再取整, 就不会再因为一个写法差异丢掉整行。
    """
    try:
        return int(float(v))
    except Exception:
        return default


def _t22():
    global _T22
    if _T22 is None:
        tbl = {}
        try:
            t = load_table('T22_EquipExp')          # ★v32: 走统一加载器(尾字段精确剥离, 不再 rfind)
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                try:
                    lv = _cell_int(r[h['Lv']])
                    if lv <= 0:
                        continue
                    tbl[lv] = (_cell_int(r[h['Exp']]), _cell_int(r[h['ForgeExp']]))
                except Exception as _e:
                    # 不再静默 continue: 丢一行会让那一级"免费", 必须能看见
                    print('[T22] WARN 跳过一行 (%r): %r' % (r, _e), flush=True)
                    continue
            # ★完整性守卫: 等级必须连续 1..30。缺任何一级 -> 升级循环把它当 0 经验 -> 白送一级。
            _miss = [i for i in range(1, 31) if i not in tbl]
            if _miss:
                print('[T22] WARN 等级缺失 %s —— 这些等级会被升级循环当成"需要 0 经验"(白送一级)!' % _miss,
                      flush=True)
        except Exception as _e:
            print('[T22] ERROR 读取 T22_EquipExp 失败: %r' % _e, flush=True)
        _T22 = tbl
    return _T22

# L02 道具 ConsumablesValue (强化石类道具"消耗品效果值"; 客户端 ExpRatio 之外的乘数)
_L02_CV = None
def _l02_cv(pid):
    global _L02_CV
    if _L02_CV is None:
        tbl = {}
        try:
            t = load_table('L02_Loots.json')      # ★v60: 去掉 rfind(']') + 写死盘符
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                try:
                    tbl[int(r[h['ID']])] = float(r[h['ConsumablesValue']] or 0)
                except Exception:
                    continue
        except Exception:
            pass
        _L02_CV = tbl
    return _L02_CV.get(int(pid), 0.0)

# ---- T23_Function: 客户端 MacroConfig.Function_Type 的数值行 (id -> Value) ----
#   客户端 app\team\EquipIntensify.lua 明文:
#     Function_Type.EquipIntensifyBase = 103  -- 装备强化基础值      (T23[103].Value = 100)
#     Function_Type.IntensifyGold      = 108  -- 强化消耗资金（每张） (T23[108].Value = 80)
#   道具材料经验 = (0 + T23[103].Value) * L02[道具id].ConsumablesValue
#   (★v20 旧实现写成 0.1 * ConsumablesValue —— 与客户端预览相差 1000 倍:
#    蕴灵/秘法/鬼道/传说强化魂石 cv=1/1.2/1.5/2 -> 服务端 int(0.1*1)=0 -> 喂石头经验恒 0 -> 确定后 [0,{}] 报"消息体为空")
_EI_BASE = None      # T23 103 装备强化基础值 (官方表 = 100)
_EI_GOLD = None      # T23 108 强化消耗资金/每张 (官方表 = 80)
def _t23():
    global _EI_BASE, _EI_GOLD
    if _EI_BASE is not None:
        return _EI_BASE, _EI_GOLD
    _EI_BASE, _EI_GOLD = 100, ENHANCE_COST
    try:
        t = load_table('T23_Function.json')      # ★v60: 去掉 rfind(']') + 写死盘符
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                _id, _v = int(r[h['ID']]), float(r[h['Value']] or 0)
            except Exception:
                continue
            if _id == 103:
                _EI_BASE = _v
            elif _id == 108:
                _EI_GOLD = _v
    except Exception:
        pass
    return _EI_BASE, _EI_GOLD

# 客户端 NetEquipIntensity:ctor 的错误码全集 (与服务器返回 body 的 statecode 一一对应)
EI_ERR = {
    70:  '要强化的装备为空',
    71:  '强化的素材为空',
    77:  '强化消耗的金币不足',
    711: '要强化的装备等级已经最高，不能再强化',
    715: '该素材被锁定，不能作为素材',
    718: '该素材为上装状态，不能被吞噬',
}
def _ei_err(code):
    # 只回 statecode (客户端 NetEquipIntensity:recvMessage 走 statecode ~= 1 分支 -> NetErrorCodeTips 提示)
    return ('[%d]' % int(code)).encode('utf-8')

def handle_equip_intensity(body_json):
    # 客户端 NetEquipIntensity(6006) (math = EquipIntensify:initData/updatePreview 权威, 1.31 明文):
    #   请求 {equipucode, matterlist:[{type,ucode,num}]}  (type: bag.baseType 1=装备 2=道具)
    #   材料经验:
    #     装备 = (T23[103].Value + T22[材料等级].ForgeExp) * L01[材料id].ExpRatio   <- 客户端 L46 (100+expValue)*expRatio
    #     道具 = (T23[103].Value + 0) * L02[道具id].ConsumablesValue * num         <- 客户端 L75 (0+exp)*cv
    #   升级: 主装备 exp 累加 -> 按 T22[Lv].Exp 逐级消耗 (上限 = L01.Level, 客户端 m_maxLevel)
    #   金币: T23[108].Value * 材料张数 (客户端 L609 preview: consumeItem 逐张累加, 注释"强化消耗资金（每张）")
    #   响应 [1,[eggcoin,soulcoin,goldcoin,[equipInfo(14字段)],matterlist消耗清单]]
    #   ★2026-09-11 修复 (v21 3.7 P0, 真机日志取证):
    #     - game_server.log 01:55:15/01:55:19/02:00:41/02:00:44/02:00:47 五次 [0,{}]:
    #       用户喂的是 L02 Type=44 强化魂石(2002-2005) -> 旧公式 int(0.1*cv) 恒为 0 -> 判"经验不足1级"拒绝,
    #       而客户端预览按 (0+100)*cv 每颗 100~200 经验 -> 用户看到"预览涨很多级、确定后没反应".
    #     - 金币口径: 旧实现 80x提升级数, 客户端预览 80x材料张数 -> 两端口径不一致(材料多而级少时多扣/误拒).
    #     - 经验不足 1 级不再拒绝: 官方语义是"经验入账但未满一级"; 旧实现回 [0,{}] -> 客户端提示"消息体为空".
    #     - 错误码补全 70/71/77/711/715/718; 材料按 matterlist 真消耗(与客户端删除背包行为一致).
    try:
        req = json.loads(body_json)
    except Exception:
        return _ei_err(70)
    if not isinstance(req, dict):
        return _ei_err(70)
    equipucode = str(req.get('equipucode') or '')
    matterlist = req.get('matterlist') or []
    # ---- 70 要强化的装备为空 ----
    if not equipucode or equipucode not in EQ_STATE['lv'] or equipucode not in EQ_STATE['equip_id']:
        return _ei_err(70)
    if not isinstance(matterlist, list) or not matterlist:
        return _ei_err(71)
    t22 = _t22()
    if not t22:
        return _ei_err(70)
    _base_val, _gold_each = _t23()
    eid = EQ_STATE['equip_id'].get(equipucode, 1001)
    lv = int(EQ_STATE['lv'].get(equipucode, 1))
    exp = float(EQ_STATE['exp'].get(equipucode, 0))
    # 上限 = min(客户端 L01.Level, T22 最大 Lv) —— 客户端 m_maxLevel = SDEquip:getStatic_Level() 全表 = 30
    from state import equip_level_cap
    cap = min(max(t22.keys()), equip_level_cap(eid))
    # ---- 711 要强化的装备等级已经最高 ----
    if lv >= cap:
        return _ei_err(711)
    lock_map = EQ_STATE.get('equip_islock', {}) or {}
    load_map = EQ_STATE.get('equip_isload', {}) or {}
    # ---- 材料经验 (客户端公式) ----
    #   反作弊口径: 装备材料同一 ucode 只计一次 (客户端每件材料是独立背包物品/独立 ucode;
    #   重复提交同一 ucode 属于伪造 -> 否则一件装备可刷 N 倍经验); 道具材料按 ucode 汇总后以"持有量"封顶
    #   (客户端道具材料按"每颗一条 num=1"下发, 故同一 ucode 重复出现是正常现象)。
    total_exp = float(exp)
    consumed = []                          # [(baseType, ucode, num)] -> matterlist 消耗清单
    sheets = 0                             # 金币按"张数"计 (客户端 preview 逐张 80)
    seen_eq = set()
    prop_agg = {}                          # ucode -> 已计入的数量
    for m in matterlist:
        if not isinstance(m, dict):
            continue
        mtype = str(m.get('type'))
        mu = str(m.get('ucode') or '')
        mnum = max(1, int(m.get('num') or 1))
        if mtype == '1':                   # 装备材料
            if mu not in EQ_STATE['equip_id'] or mu == equipucode or mu in seen_eq:
                continue
            if int(lock_map.get(mu, 0)) == 1:
                return _ei_err(715)        # 该素材被锁定
            if int(load_map.get(mu, 0)) == 1:
                return _ei_err(718)        # 该素材为上装状态
            mlv = int(EQ_STATE['lv'].get(mu, 1))
            mt = t22.get(mlv, (0, 0))
            total_exp += (_base_val + float(mt[1])) * float(equip_exp_ratio(EQ_STATE['equip_id'].get(mu, 1001)))
            sheets += 1
            seen_eq.add(mu)
            consumed.append((1, mu, 1))
        elif mtype == '2':                 # 强化石道具 (L02 Type=44 装备强化; ucode -> prop_id -> L02)
            pid = int(EQ_STATE.get('prop_id', {}).get(mu, mu) or 0)
            cv = _l02_cv(pid) if pid else 0.0
            own = int(EQ_STATE.get('prop', {}).get(mu, 0))
            used = prop_agg.get(mu, 0)
            mnum = min(mnum, max(0, own - used))
            if mnum <= 0:
                continue                   # 数量不足 -> 该条不计
            prop_agg[mu] = used + mnum
            total_exp += _base_val * cv * mnum
            sheets += mnum
            consumed.append((2, mu, mnum))
    if not consumed:
        return _ei_err(71)
    # ---- 金币: T23[108].Value x 材料张数 (客户端 preview 口径) ----
    gold_cost = int(_gold_each) * sheets
    if int(EQ_STATE['gold']) < gold_cost:
        return _ei_err(77)
    # ---- 逐级升级 (T22[Lv].Exp; 客户端 updatePreview 浮点累加, 最后 floor 落库) ----
    levels = 0
    while lv < cap and total_exp >= float(t22.get(lv, (0, 0))[0]):
        total_exp -= float(t22.get(lv, (0, 0))[0])
        lv += 1
        levels += 1
        if lv >= cap:
            total_exp = 0.0
            break
    final_exp = int(total_exp) if total_exp > 0 else 0
    # ---- 生效: 扣金 + 材料真消耗 (装备出背包 / 道具扣数量) + 主装备等级经验落库 ----
    EQ_STATE['gold'] = int(EQ_STATE['gold']) - gold_cost
    for (bt, mu, mn) in consumed:
        if bt == 1:
            EQ_STATE['lv'].pop(mu, None)
            EQ_STATE['exp'].pop(mu, None)
            EQ_STATE['equip_id'].pop(mu, None)
            EQ_STATE['equip_isload'].pop(mu, None)
            for _mu, _slots in list(EQ_STATE.get('monster_ueq', {}).items()):
                _sl = list(_slots)
                _ch = False
                for _i in range(len(_sl)):
                    if str(_sl[_i]) == mu:
                        _sl[_i] = '0'
                        _ch = True
                if _ch:
                    EQ_STATE['monster_ueq'][_mu] = tuple(_sl)
        else:
            left = int(EQ_STATE.get('prop', {}).get(mu, 0)) - mn
            if left > 0:
                EQ_STATE['prop'][mu] = left
            else:
                EQ_STATE['prop'].pop(mu, None)
                EQ_STATE.get('prop_id', {}).pop(mu, None)
    EQ_STATE['lv'][equipucode] = lv
    EQ_STATE['exp'][equipucode] = final_exp
    isload = EQ_STATE['equip_isload'].get(equipucode, 0)
    equip_info = [equipucode, eid, lv, final_exp, 0, 0, 0, isload, 0, 0, 0, 0, 0, 0]
    # 消耗清单 = 客户端 NetEquipIntensity:recvMessage 的删除依据:
    #   装备 [bag.baseType.equip(1), ucode] -> equip_deleteItem ; 道具 [2, ucode, num] -> prop deleteItem(ucode,num)
    matter_resp = []
    _prop_merged = {}
    for (bt, mu, mn) in consumed:
        if bt == 1:
            matter_resp.append([1, mu])
        else:
            _prop_merged[mu] = _prop_merged.get(mu, 0) + mn
    for _mu2, _mn2 in _prop_merged.items():
        matter_resp.append([2, _mu2, _mn2])
    bump_task(CT_INTENSIFY, 1)   # 每日任务: 强化装备
    save_state()
    data = [EQ_STATE['eggcoin'], EQ_STATE['soulcoin'], EQ_STATE['gold'], equip_info, matter_resp]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')


# ============ 装备穿戴(6004)/卸下(6005) CULTIVATE_LOAD / CULTIVATE_UNLOAD ============
# 响应(NetEquipLoad/Unload recvMessage): [1,[eggcoin,soulcoin,goldcoin,monsterInfo,matterlist]]
#   msg[4]=monsterInfo(30字段怪物item, 客户端 monster_updateItem -> 更新 uequip 槽)
#   msg[5]=matterlist=[[ucode,14字段装备item],...] (客户端 equip_updateItem -> 更新装备 isload)
#
# ★★v34 (2026-09-13 bug#1 "妖怪装不上装备, 只有哪吒和月主能装"):
#   客户端错误码表 (NetEquipLoad.lua 原文, 服务端必须照发, 否则一律落到 [0,{}] ->
#   弹"错误，消息体为空" = LOCAL_UI_SYSTEM_108325, 用户完全看不出真实原因):
#     [0] 错误，消息体为空 / 70 要上装的怪物为空 / 71 怪物上装的素材为空 / 707 怪物上装的类型错误
#     / 708 装备与怪物的职业不匹配 / 709 装备与怪物的角色不匹配 / 716 该怪物已经被上装
#     / 717 上装的装备和之前的装备相等 / 561 怪物未达到穿戴此装备的限制等级 / 719 该装备不可穿戴
#   旧实现一共只有 3 个 `return b'[0,{}]'` 出口 (怪物不在 monster_ueq / matterlist[0] 不是表 /
#   primary 不在 equip_id), 全部对应"用户看不懂"的提示 —— 现按真实原因返回对应码。
EL_ERR_MON_EMPTY = 70       # 要上装的怪物为空
EL_ERR_MAT_EMPTY = 71       # 怪物上装的素材为空
EL_ERR_TYPE      = 707      # 怪物上装的类型错误
EL_ERR_NOT_WEAR  = 719      # 该装备不可穿戴
EL_ERR_LEVEL     = 561      # 怪物未达到穿戴此装备的限制等级

def _el_err(code):
    # 与客户端契约一致: 只回一个 statecode 标量数组 "[70]" / "[0]" (NetBase:parseMessage)
    return ('[%d]' % int(code)).encode('utf-8')

# 一键上装(TOTAL) 的槽位遍历顺序 + 每槽对应的 L01.Type
#   _TYPE_UEQ_IDX: {1攻:1, 2防:2, 3命:0}  (monster_ueq 元组下标 0=hp,1=atk,2=def)
_UEQ_AUTO_ORDER = ((1, _TYPE_UEQ_IDX[1]), (2, _TYPE_UEQ_IDX[2]), (3, _TYPE_UEQ_IDX[3]))

def _uc_sort_key(uc):
    try:
        return int(uc)
    except Exception:
        return 10 ** 9

def _equip_score_raw(ucode):
    # 装备"好坏"评分 (一键上装选优 / 是否值得换装):
    #   品级(L01.Class) > 装备强化等级 > 静态id > 寄妖个数 > 寄妖id之和
    #   ⚠ 必须带寄妖维度: 否则"同品级同等级同静态id"的两件会被无脑互换 ——
    #     把一件带寄妖(附魔了元灵)的换成空壳 (14字段 item 的 5/6/13/14 位即寄妖)。
    #   (客户端 EquipList 排序只有 品质->等级->staticId, EquipList:sortDataByTag tag=1)
    eid = int(EQ_STATE['equip_id'].get(ucode, 0) or 0)
    try:
        a = get_equip_append(ucode)
        ench = [int(x or 0) for x in a]
    except Exception:
        ench = [0, 0, 0, 0]
    return (equip_class_static(eid), int(EQ_STATE['lv'].get(ucode, 1) or 1), eid,
            sum(1 for x in ench if x), sum(ench))

def _equip_score(ucode):
    # 候选之间的完整排序键 = raw + ucode 降序 (确定性 tiebreak, 只用于候选互比)
    return _equip_score_raw(ucode) + (-_uc_sort_key(ucode),)

def _auto_pick_for_type(monsterucode, ty, cur_ucode):
    # 为某槽位(ty=1攻/2防/3命)挑"比现在更好"的装备 ucode; 挑不到 -> None
    #   可候选: 未上装(isload=0) / 未锁定 / 类型匹配 / LvLimit≠99 / 怪物等级达标
    lv = get_monster_lv(monsterucode)
    lock_map = EQ_STATE.get('equip_lock') or {}
    best, best_key = None, None
    for uc in EQ_STATE['equip_id']:
        uc = str(uc)
        if int(EQ_STATE['equip_isload'].get(uc, 0) or 0) == 1:
            continue                                  # 已被(本怪或别怪)穿着
        if int(lock_map.get(uc, 0) or 0) == 1:
            continue                                  # 锁定的装备不自动上装
        eid = int(EQ_STATE['equip_id'].get(uc, 0) or 0)
        if equip_type_static(eid) != ty:
            continue
        lim = equip_lv_limit(eid)
        if lim == 99:
            continue                                  # 客户端列表里不显示 -> 不自动穿
        if lv < lim:
            continue
        k = _equip_score(uc)
        if best_key is None or k > best_key:
            best, best_key = uc, k
    if best is None:
        return None
    if cur_ucode and cur_ucode in EQ_STATE['equip_id']:
        # 只在"确实更优"时换装 (不带 ucode tiebreak) -> 不会把等价/带寄妖的装备白换一遍
        if _equip_score_raw(cur_ucode) >= _equip_score_raw(best):
            return None
    return best

def _equip_item(ucode):
    # 14字段装备 item: [ucode,id,level,exp,uappend1,uappend2,islock,isload,0,0,0,0,uappend3,uappend4]
    #   (app/sqdata/SQEquip.lua:32~45 insertItem: [5]=uappend1 [6]=uappend2 [13]=uappend3 [14]=uappend4)
    #   ★2026-09-12 (v26): 补上 4 个寄妖位 —— 旧实现恒为 0, 导致"附魔成功但装备上看不到元灵"
    #     (客户端 VBagTips/VBagHome 用 getEnchantAttr -> getAppendList 渲染寄妖图标)
    a = get_equip_append(ucode)
    try:
        islock = 1 if int((EQ_STATE.get('equip_lock') or {}).get(str(ucode), 0) or 0) else 0
    except Exception:
        islock = 0
    return [ucode, EQ_STATE['equip_id'].get(ucode, 1001), EQ_STATE['lv'].get(ucode, 1),
            EQ_STATE['exp'].get(ucode, 0), a[0], a[1], islock, EQ_STATE['equip_isload'].get(ucode, 0),
            0, 0, 0, 0, a[2], a[3]]

def equip_bag_json():
    # 动态装备背包(1030): 反映真实 isload/等级 (避免静态硬编码导致"已装备但没穿上/等级都1"不符)
    #   ★2026-09-12 (v26): 统一走 _equip_item, 补上 islock + 4 个寄妖位 (旧实现这两处恒 0 ->
    #     "装备锁定不生效" 且 "附魔后重进背包寄妖图标消失")
    # ★2026-11-14: 用 `.get(...) or {}` 兜底 —— 老档/手改档缺 `equip_id` 时，旧写法直接
    #   KeyError ⇒ 1030 没有响应 ⇒ 客户端卡在 40%（`accounts._load` 已经会补键，这里是第二道闸）。
    items = [json.dumps(_equip_item(uc), separators=(',', ':'))
             for uc in sorted((EQ_STATE.get('equip_id') or {}).keys(), key=int)]
    return '[1,[[' + ','.join(items) + '],"1/1"]]'

def _mon_with_state(ucode, skin_override=None):
    # 用 EQ_STATE['monster_ueq'] 的当前槽值构造 30字段怪物item(复用 _mon 输出 JSON 数组字符串)
    base = get_monster_base(ucode)
    if base is None:
        return None
    mid, skin, num, skills = base
    if skin_override is not None:
        skin = int(skin_override)
    else:
        # 2026-09-07(第二轮) 修复: 默认读穿戴皮肤(monster_skin_eq), 否则怪列表/重启后皮肤丢失
        # ★2026-09-11 (v21 §3.5): 收敛到 state.get_monster_skin 单一实现 (1032/3200/掉落/PVP 同一规则)
        from state import get_monster_skin as _gms
        skin = _gms(ucode) or skin
    # ★v34: 走 ensure_monster_ueq (缺记录则惰性补 ('0','0','0')) —— 旧写法 `.get(ucode,(0,0,0))`
    #   虽然不会崩, 但会把"这只怪根本没有装备槽记录"这件事一直藏住, 直到上装时才以 [0,{}] 爆出来。
    ueq = ensure_monster_ueq(ucode) or ('0', '0', '0')
    ad = get_monster_add(ucode)
    # 技能槽: 已学习/替换的 (monster_skills) 覆盖 L91 基础技能
    #   槽值 = 技能袋 ucode 字符串 (客户端 SQMonster:getSkillList -> skill_getMe:getObject(ucode));
    #   兼容原生数字技能id (11001) 与 1177 合成技能书 ucode (sk1) —— 必须原样下发, 数字过滤会吞掉 sk*
    ov = EQ_STATE.get('monster_skills', {}).get(ucode)
    if ov:
        skills = [u if str(u) not in ('0', '', None) else 0 for u in ov]
    fp = get_feat_parts(ucode)   # 特性槽(20-25) + 特性总经验/等级(26-27); None 则由 _mon 默认 0
    feat = fp[0] if fp else None
    featexp = fp[1] if fp else 0
    featlev = fp[2] if fp else 0
    return _mon(int(ucode), mid=mid, num=num, skin=skin, skills=list(skills), uequip=tuple(ueq),
                lv=get_monster_lv(ucode), exp=get_monster_exp(ucode),
                grow=get_monster_grow(ucode), addhp=ad[0], addatk=ad[1], adddef=ad[2], addcri=ad[3],
                feat=feat, featexp=featexp, featlev=featlev)


def mon_info_slot(mi, ucode, tag):
    """成功响应里的 **monsterInfo 槽** —— 唯一实现, **绝不发 null**。

    ★v60 闭环 v58 §10 缺口 #2「7 处条件式 None」。
      那 7 处的原写法是 `json.loads(mi) if mi else None`。客户端那边的消费方式**逐一核对**过,
      全都是同一个形状（见 work/_v60_moninfo_contract_test.py 顶部列出的 7 个客户端文件）:
          if monsterInfo then SQManager:getMe():monster_updateItem(monsterInfo[1], monsterInfo) end
      ⇒ 发 null **不会崩**（Lua 里 nil 让 if 为假），
        但会让"玩家刚花掉金币/道具"的那一次**怪物刷新被静默跳过** —— 界面看着没变
        （正是 v57 那批"界面不刷新"P0 的同族症状）。
      v57 已在每处写了断言"成功路径上 mi 必有值"; v60 把这条**推断收敛成唯一实现**:
        有值 -> 原样下发; 没值 -> **大声抛错**（宁可第一次调用就暴露, 也不静默发 null）。
      依据: 交接档 §9.3 铁律 8 —— 服务端给结构化字段发占位, 必须发该结构的"最小合法实例";
            而这里的"最小合法实例"就是**这只怪真实的 30 字段 item**（拿不到就不该假装成功）。
    """
    if not mi:
        raise AssertionError('%s: 成功路径上 _mon_with_state(%s) 返回空 -> '
                             '拒绝发送 null(客户端会静默不刷新怪物面板)' % (tag, ucode))
    return json.loads(mi)


def _build_equip_resp(monsterucode, changed_equips):
    # changed_equips = 本次被改动 isload 的装备 ucode 列表 -> matterlist=[[ucode,14字段装备item],...]
    monster_info = _mon_with_state(monsterucode)
    if monster_info is None:
        # 怪物数据取不到 (无 base / 已被吞噬) —— 旧实现回 [0,{}] = "错误，消息体为空";
        # 按客户端错误码表改回 [70] 要上装的怪物为空, 用户/日志都能直接看懂。
        return _el_err(EL_ERR_MON_EMPTY)
    matter_parts = []
    for uc in changed_equips:
        if uc not in EQ_STATE['equip_id']:
            continue
        # 扁平14字段装备item: [ucode,id,level,...,isload,...] (matterlist[i][1]=ucode, equip_updateItem)
        matter_parts.append(json.dumps(_equip_item(uc), separators=(',', ':')))
    matter_str = '[' + ','.join(matter_parts) + ']'
    resp = '[1,[%d,%d,%d,%s,%s]]' % (EQ_STATE['eggcoin'], EQ_STATE['soulcoin'], EQ_STATE['gold'],
                                     monster_info, matter_str)
    return resp.encode('utf-8')

def _equip_slot_index(etype):
    # 由装备类型决定其占用怪物 uequip 元组下标 (0=hp槽,1=atk槽,2=def槽)
    return _TYPE_UEQ_IDX.get(etype, 1)

def _equip_type_of(ucode):
    # 由装备 static id 决定类型 (L01.Type 全表; 缺省回退旧硬编码)
    from state import equip_type_static
    eid = EQ_STATE['equip_id'].get(ucode, 1001)
    return equip_type_static(eid)

def handle_equip_load(body_json):
    # 客户端 NetEquipLoad(6004): 请求 {type:0或1, monsterucode, matterlist}
    #   type=0 SINGLE: matterlist=[{ucode:"装备ucode"}, ...]  (EquipAttr:wearEquip -> 单个/换装)
    #   type=1 TOTAL : matterlist=[""]                        (EquipInfo:onTouch "一键装备"按钮!)
    #     ⚠ 客户端 TOTAL 分支发的是**一个空字符串**(NetEquipLoad.lua `temp = {""}`), 不是表数组。
    #       旧实现只认 matterlist[0] 是 dict -> 一键装备**100%**回 [0,{}] -> 弹"错误，消息体为空"。
    #       官方语义 = 服务端为 攻/防/命 三个槽位各挑一件最合适的装备 (客户端只负责刷新)。
    try:
        req = json.loads(body_json)
    except Exception:
        return _el_err(EL_ERR_MON_EMPTY)
    if not isinstance(req, dict):
        return _el_err(EL_ERR_MON_EMPTY)
    monsterucode = str(req.get('monsterucode') or '')
    try:
        req_type = int(req.get('type') or 0)
    except Exception:
        req_type = 0
    if req_type not in (0, 1):
        return _el_err(EL_ERR_TYPE)                 # 707 怪物上装的类型错误
    # ---- 70 要上装的怪物为空 (含"怪物不存在/已被吞噬") ----
    if not monsterucode or get_monster_base(monsterucode) is None:
        return _el_err(EL_ERR_MON_EMPTY)
    # ★v34: 惰性补种装备槽 (旧存档里 商城直发/宝箱抽怪 来的怪没有 monster_ueq 记录)
    ueq0 = ensure_monster_ueq(monsterucode)
    if ueq0 is None:
        return _el_err(EL_ERR_MON_EMPTY)
    matterlist = req.get('matterlist') or []
    changed = []                       # 本次被改动 isload 的装备 ucode
    ueq = list(ueq0)
    if req_type == 1:
        # ================= 一键上装 (TOTAL) =================
        for ty, ueq_idx in _UEQ_AUTO_ORDER:
            cur = ueq[ueq_idx]
            cur_uc = str(cur) if cur not in (0, '0', None) else ''
            pick = _auto_pick_for_type(monsterucode, ty, cur_uc)
            if not pick:
                continue
            if cur_uc and cur_uc in EQ_STATE['equip_id']:
                EQ_STATE['equip_isload'][cur_uc] = 0
                if cur_uc not in changed:
                    changed.append(cur_uc)
            ueq[ueq_idx] = pick
            EQ_STATE['equip_isload'][pick] = 1
            if pick not in changed:
                changed.append(pick)
        if not changed:
            # 没有可换的装备 (等级/类型/已穿满): 回成功空变更, 让客户端正常关掉 loading 并刷新
            #   ⚠ 绝不能回 [0,{}] —— 那会弹"错误，消息体为空", 用户以为功能坏了
            print('[6004] 一键上装 ucode=%s: 无可上装装备 (等级/类型/槽位均无更优) -> 空变更' % monsterucode,
                  flush=True)
        EQ_STATE['monster_ueq'][monsterucode] = tuple(ueq)
        if changed:
            # 任务 CT=32「装备/更换装备」：UG 12004「仗剑走天涯-1」(第一件武器) /
            #   21017「仗剑走天涯-2」(更换 1 次)。只有**真的改了槽位**才计，
            #   "一键上装但没得换"（changed 为空）不算 —— 与客户端按钮语义一致。
            bump_task(CT_EQUIP, 1)
        save_state()
        return _build_equip_resp(monsterucode, changed)
    # ================= 单件/换装 (SINGLE) =================
    if not isinstance(matterlist, list) or not matterlist:
        return _el_err(EL_ERR_MAT_EMPTY)            # 71
    # 主装备(第一个 matterlist 项)必须存在, 否则失败
    if not isinstance(matterlist[0], dict):
        return _el_err(EL_ERR_MAT_EMPTY)
    primary = str(matterlist[0].get('ucode') or '')
    if not primary or primary not in EQ_STATE['equip_id']:
        return _el_err(EL_ERR_NOT_WEAR)             # 719 该装备不可穿戴
    for m in matterlist:
        if not isinstance(m, dict):
            continue
        equipucode = str(m.get('ucode') or '')
        if not equipucode or equipucode not in EQ_STATE['equip_id']:
            continue
        eid = int(EQ_STATE['equip_id'].get(equipucode, 0) or 0)
        # 561 怪物未达到穿戴此装备的限制等级 (L01.LvLimit; 99=限定强化石, 客户端列表里不出现)
        lim = equip_lv_limit(eid)
        if lim != 99 and get_monster_lv(monsterucode) < lim:
            return _el_err(EL_ERR_LEVEL)
        etype   = _equip_type_of(equipucode)
        ueq_idx = _equip_slot_index(etype)
        # 该槽原有装备 -> 卸下(旧装备 isload 置0)
        old = ueq[ueq_idx]
        old_uc = '' if old in (0, '0') else str(old)
        # ★★2026-10-22（审查高 H4）：**原样再穿同一件**不是"更换"。
        #   修前这里无条件 `changed.append(equipucode)` ⇒ 反复发 6004 把同一件装备穿进
        #   它已经在的槽位，`changed` 非空、`bump_task(CT=32)` 照记 ⇒ 21017「更换 1 次装备」
        #   不用真换就能完成（T37 的 CT=32 / A07 同样被刷）。
        #   现在按"操作前后槽位是否真的变了"判：同件同槽 = 空操作，不计也不回变更列表
        #   （一键上装分支早就是按 `changed` 非空才计，两边口径统一）。
        _same = (old_uc == equipucode)
        if old_uc and not _same:
            if old_uc in EQ_STATE['equip_isload']:
                EQ_STATE['equip_isload'][old_uc] = 0
            if old_uc not in changed:
                changed.append(old_uc)
        # 穿戴新装备: 填槽 + isload 置1
        ueq[ueq_idx] = str(equipucode)
        EQ_STATE['equip_isload'][equipucode] = 1
        if not _same and equipucode not in changed:
            changed.append(equipucode)
    EQ_STATE['monster_ueq'][monsterucode] = tuple(ueq)
    if changed:
        bump_task(CT_EQUIP, 1)   # CT=32「装备/更换装备」（同上面 TOTAL 分支）
    save_state()   # 穿戴改动 monster_ueq/equip_isload -> 落盘
    return _build_equip_resp(monsterucode, changed)

def handle_equip_unload(body_json):
    # 客户端 NetEquipUnload(6005): 请求 {monsterucode, matterlist:[{ucode}]}
    try:
        req = json.loads(body_json)
    except Exception:
        return _el_err(EL_ERR_MON_EMPTY)
    if not isinstance(req, dict):
        return _el_err(EL_ERR_MON_EMPTY)
    monsterucode = str(req.get('monsterucode') or '')
    matterlist = req.get('matterlist') or []
    if not monsterucode or get_monster_base(monsterucode) is None:
        return _el_err(EL_ERR_MON_EMPTY)
    ueq0 = ensure_monster_ueq(monsterucode)
    if ueq0 is None:
        return _el_err(EL_ERR_MON_EMPTY)
    if not isinstance(matterlist, list) or not matterlist:
        return _el_err(EL_ERR_MAT_EMPTY)
    # 主装备(第一个 matterlist 项)必须存在, 否则失败
    if not isinstance(matterlist[0], dict):
        return _el_err(EL_ERR_MAT_EMPTY)
    primary = str(matterlist[0].get('ucode') or '')
    if not primary or primary not in EQ_STATE['equip_id']:
        return _el_err(EL_ERR_NOT_WEAR)
    changed = []
    ueq = list(ueq0)
    for m in matterlist:
        if not isinstance(m, dict):
            continue
        equipucode = str(m.get('ucode') or '')
        if not equipucode or equipucode not in EQ_STATE['equip_id']:
            continue
        etype   = _equip_type_of(equipucode)
        ueq_idx = _equip_slot_index(etype)
        # 清空对应槽(置字符串"0"=UCODE_ZERO), 该装备 isload 置0
        if ueq[ueq_idx] == equipucode:
            ueq[ueq_idx] = "0"
        EQ_STATE['equip_isload'][equipucode] = 0
        if equipucode not in changed:
            changed.append(equipucode)
    EQ_STATE['monster_ueq'][monsterucode] = tuple(ueq)
    save_state()   # 卸下改动 monster_ueq/equip_isload -> 落盘
    return _build_equip_resp(monsterucode, changed)


# ============================================================================
# 装备寄妖/附魔 CULTIVATE_ENCHANT(6007)   ★2026-09-12 (v26 P1) 新增实装
# ----------------------------------------------------------------------------
# 客户端明文 (app/network/cultivate/NetEquipEnchant.lua):
#   sendMessage(_equipucode, _ghostucode, _slot, _callFunc, _callFailedFunc)
#     msg = {equipucode=..., ghostucode=..., slot=1..4}
#   recvMessage:
#     statecode ~= 1 -> m_callFailedFunc() + NetErrorCodeTips:showTips(m_errorCode[statecode])
#     msg[1]=eggcoin msg[2]=soulcoin msg[3]=goldcoin msg[4]=ghostucode msg[5]=equipInfo(14字段装备item)
#     · msg[4] ~= "" -> ghost_deleteItem(ghostucode)      (元灵被寄走, 从背包移除)
#     · msg[5] -> equip_updateItem(equipInfo[1], equipInfo) (装备带上寄妖位)
#   错误码 (m_errorCode, 权威): 0 消息体为空 / 70 要寄妖的装备为空 / 71 被寄妖的素材为空
#                               / 77 寄妖所需金币不足 / 712 寄妖槽位错误
#   槽位上限 = L01_Equipments.Slot (客户端 SDEquip:getStatic_EnchantCount(), 全表 2/3/4)
#   单次消耗 = T23_Function[110].Value = 1000 金币
#             (MacroConfig.Function_Type.EnchantGold = 110; EquipEnchant.lua:676 GetTableSelectData)
#   ⚠ 旧实现根本没有 6007 -> 落 resp_for 兜底 [1,[[]]] -> msg[5]=nil ->
#     客户端 SQGhost.lua:52 `table index is nil` 抛错 -> socket tick 断 -> 附魔界面卡死。
# ============================================================================
_ENCHANT_GOLD = 1000          # T23_Function[110].Value

_L01_SLOT = None
def _l01_slot_map():
    # L01_Equipments: 静态id -> Slot (寄妖槽位上限)
    global _L01_SLOT
    if _L01_SLOT is None:
        m = {}
        try:
            t = load_table('L01_Equipments.json')      # ★v60: 去掉 rfind(']') + 写死盘符
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                try:
                    m[int(r[h['ID']])] = int(r[h['Slot']] or 2)
                except Exception:
                    continue
        except Exception:
            m = {}
        _L01_SLOT = m
    return _L01_SLOT

def _equip_slot_cap(equipucode):
    eid = int(EQ_STATE['equip_id'].get(equipucode, 0) or 0)
    return int(_l01_slot_map().get(eid, 2))

_L01_TYPE = None
def _l01_type_map():
    # L01_Equipments: 静态id -> Type (1=武器 2=衣服 3=饰品)
    #   ★v49: 客户端 ct.equipString / EquipEnchant:getEquipAttrByType 的 Type 语义:
    #     1 -> 攻击(武器) / 2 -> 防御(衣服) / 3 -> 生命(饰品) / 4 -> 暴击
    #   实测装备名对照: Type1=桃木剑/洛阳铲(武器), Type2=粗布服/七星袍(衣服), Type3=羽扇/玉如意(饰品)
    global _L01_TYPE
    if _L01_TYPE is None:
        m = {}
        try:
            t = load_table('L01_Equipments.json')      # ★v60: 去掉 rfind(']') + 写死盘符
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                try:
                    m[int(r[h['ID']])] = int(r[h['Type']] or 0)
                except Exception:
                    continue
        except Exception:
            m = {}
        _L01_TYPE = m
    return _L01_TYPE


_L03_TYPE = None
def _l03_type_map():
    # L03_Equipghost: 元灵静态id -> Type (部位限制, 与 L01.Type 同一套编码)
    #   ★v49 权威依据 = 客户端本地化里每条元灵的说明文案:
    #     "【只能为饰品类装备寄妖】"(xianti/暴击系, Type=3) /
    #     "【只能为武器类装备寄妖】"(guili/攻击系, Type=1) /
    #     "【只能为衣服类装备寄妖】"(yushen/防御系, Type=2)
    #   实测对照 L03: 1020仙体灵石(Type3,HP+350) / 1021鬼力灵石(Type1,Atk+47) /
    #                 1022御身灵石(Type2,Def+24) / 1049怒心魂玉(Type3,Crt+400)
    #   ⇒ **元灵 Type == 可寄妖的装备 Type**。
    global _L03_TYPE
    if _L03_TYPE is None:
        m = {}
        try:
            t = load_table('L03_Equipghost.json')      # ★v60: 去掉 rfind(']') + 写死盘符
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                try:
                    m[int(r[h['ID']])] = int(r[h['Type']] or 0)
                except Exception:
                    continue
        except Exception:
            m = {}
        _L03_TYPE = m
    return _L03_TYPE


_ENCHANT_TYPE_ERR = 713   # ★v49 新增: 该元灵不能寄妖到这类装备 (按 L03.Type vs L01.Type)
_TYPE_NAME = {1: '武器', 2: '衣服', 3: '饰品', 4: '暴击'}

def handle_equip_enchant(body_json):
    # 6007: req {equipucode, ghostucode, slot}
    #   resp [1,[eggcoin, soulcoin, goldcoin, ghostucode, equipInfo]]
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    equipucode = str(req.get('equipucode') or '')
    ghostucode = str(req.get('ghostucode') or '')
    try:
        slot = int(req.get('slot') or 0)
    except Exception:
        slot = 0
    if not equipucode or equipucode not in EQ_STATE['equip_id']:
        return b'[70]'                       # 要寄妖的装备为空
    if not ghostucode:
        return b'[71]'                       # 被寄妖的素材为空
    g = find_ghost(ghostucode)
    if g is None or int(g[2] or 0) <= 0:
        return b'[71]'
    if not (1 <= slot <= 4) or slot > _equip_slot_cap(equipucode):
        return b'[712]'                      # 寄妖槽位错误 (超出 L01.Slot)
    # ★v49: 部位限制 —— 元灵的 L03.Type 必须等于装备的 L01.Type
    #   客户端 UI 会按文案拦住玩家, 但**服务端不能依赖客户端校验** (v41 铁律 #122:
    #   "不要假设客户端会拦住非法请求"), 否则武器上能寄饰品元灵, 属性白拿。
    #   两条路都取不到类型时**放行** (表缺失不该阻断玩法), 只在"两边都知道且不等"时拒绝。
    _gid = int(g[1] or 0)
    _eid = int(EQ_STATE['equip_id'].get(equipucode, 0) or 0)
    _gt = _l03_type_map().get(_gid)
    _et = _l01_type_map().get(_eid)
    if _gt and _et and _gt != _et:
        return ('[%d]' % _ENCHANT_TYPE_ERR).encode('utf-8')
    if int(EQ_STATE.get('gold', 0)) < _ENCHANT_GOLD:
        return b'[77]'                       # 寄妖所需的金币不足
    # ---- 生效 ----
    ghost_static_id = int(g[1] or 0)
    EQ_STATE['gold'] = int(EQ_STATE.get('gold', 0)) - _ENCHANT_GOLD
    consume_ghost(ghostucode, 1)
    set_equip_append(equipucode, slot, ghost_static_id)
    # ★2026-10-02 (成就进度写入方): T32 Type=1110「寄妖数达到N」-> 组 1003/级别? (该 Type 在 T32 里)
    #   位置在消耗元灵 + 写入 append **都成功之后** ⇒ 失败路径不会计数。
    from handlers_achievement import note_ach
    note_ach(1110, 0, 1)
    # ★2026-10-16（任务书 E）：T37 CT=17「寄妖：对任意装备进行 2 次寄妖」。
    #   事件源 = 6007 CULTIVATE_ENCHANT（客户端 `ConfigNet.lua`：`CULTIVATE_ENCHANT = 6007
    #   --养成装备寄妖`），计数点在**金币扣掉 + 元灵消耗 + append 写入都成功之后** ⇒
    #   任何一条失败路径（[70]/[71]/[712]/部位不符/[77]）都不会计数。
    try:
        from handlers_task import bump_task as _bt, CT_ENCHANT as _CT_ENCHANT
        _bt(_CT_ENCHANT, 1)
    except Exception as _e:
        print('[CT17] !! 寄妖计数失败(不影响寄妖): %r' % (_e,), flush=True)
    save_state()
    data = [int(EQ_STATE['eggcoin']), int(EQ_STATE['soulcoin']), int(EQ_STATE['gold']),
            ghostucode, _equip_item(equipucode)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')


# ============================================================================
# 怪物锁定 MONSTER_LOCK(6001) / 装备锁定 EQUIP_LOCK(6008)   ★2026-09-12 (v26 P1) 新增实装
# ----------------------------------------------------------------------------
# 客户端明文:
#   NetMonsterLock.lua: send {ucode, type} (MONSTER_LOCK.UNLOCK=0 / LOCK=1)
#                       recv: msg[1]=monsterInfo(30字段) -> monster_updateItem; 然后 m_callFunc()
#   NetEquipLock.lua  : send {ucode, type} (EQUIP_LOCK.UNLOCK=0 / LOCK=1)
#                       recv: msg[1]=equipInfo(14字段)  -> equip_updateItem;   然后 m_callFunc()
#   错误码: NetEquipLock 有 {0,70 要锁定的装备为空,713 装备锁定的类型错误,714 已是该状态};
#          NetMonsterLock 无自定义 (走 NetBase 默认 {0})。
#   怪物 item 第 5 位 = islock (config_data._mon 的 5 号字段), 装备 item 第 7 位 = islock。
#   ⚠ 旧实现两条都没有 -> 兜底 [1,[[]]] -> msg[1] = {} 是空表(不崩) 但 equip_updateItem({}[1]=nil)
#     -> 客户端 SQEquip:insertItem 用 _info[1]... 全是 nil -> 锁定按钮点了没反应/装备行被写坏。
# ============================================================================
def handle_monster_lock(body_json):
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    ucode = str(req.get('ucode') or '')
    try:
        lock = int(req.get('type') or 0)
    except Exception:
        lock = 0
    if not ucode or get_monster_base(ucode) is None:
        return b'[0,{}]'
    EQ_STATE.setdefault('monster_lock', {})[ucode] = 1 if lock else 0
    save_state()
    mi = _mon_with_state(ucode)
    if mi is None:
        return b'[0,{}]'
    return ('[1,[' + mi + ']]').encode('utf-8')

def handle_equip_lock(body_json):
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    ucode = str(req.get('ucode') or '')
    try:
        lock = int(req.get('type') or 0)
    except Exception:
        lock = 0
    if not ucode or ucode not in EQ_STATE['equip_id']:
        return b'[70]'                       # 要锁定的装备为空
    if lock not in (0, 1):
        return b'[713]'                      # 装备锁定的类型错误
    cur = int(EQ_STATE.setdefault('equip_lock', {}).get(ucode, 0) or 0)
    if cur == lock:
        return b'[714]'                      # 该装备已是该状态
    EQ_STATE['equip_lock'][ucode] = lock
    save_state()
    return ('[1,[' + json.dumps(_equip_item(ucode), separators=(',', ':')) + ']]').encode('utf-8')
