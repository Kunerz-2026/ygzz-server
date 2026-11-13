# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 系统处理器 (按 服务器接口字段字典.md 字段照抄)
#   1500 SKIN_GET (皮肤列表) / 1501 SKIN_REPLACE (替换皮肤)
#   3200 FORMATION_LIST (布阵) / 3201 FORMATION_UPDATE
#   1055 MAIL_GET / 1057 MAIL_RECEIVE / 1059 MAIL_TOTAL
import json, time
from state import EQ_STATE, save_state, get_monster_base, get_monster_lv, get_monster_exp, all_monster_ucodes, get_monster_add, get_monster_grow
from state import role_info_array, load_table, load_table_index, table_dir   # ★v32: 唯一权威实现 (本文件旧版那份已删)
from config_data import _mon
from handlers_equip import _mon_with_state

# ★2026-09-13 (v32) 本文件原有第二份 role_info_array (体力正确但妖塔层数硬编码 1) 已删除;
#   唯一权威实现在 state.role_info_array()。详见那里的分叉复盘。

# ============ 皮肤 SKIN_GET(1500) / SKIN_REPLACE(1501) ============
# 数据: 每只妖的皮肤 id 集合 (L98_Monsterskin: MonsterID -> skin id(s)), 基础皮肤 = 妖怪 item 的 skin 字段
_DATA = table_dir()          # ★v32: 不再写死 <repo>\ygzz\... 由 state 从文件位置推导

def _load_json(name):
    # ★2026-09-13 (v32): 改走 state.load_table —— 内部按"尾字段 u32 长度"精确剥离,
    #   不再用 rfind(']') (对 L98_AIPriority.json 这类表会切错并静默拿到空表; 也顺带去掉 errors='replace')。
    return load_table(name, _DATA)

# L98_Monsterskin: skinid -> 怪阶段族 (与 handlers_bag._L98_FAM 同源; 礼包发的皮肤按族解锁)
_L98_FAM = {}
try:
    _t98 = _load_json('L98_Monsterskin.json')
    _h98 = {h: k for k, h in enumerate(_t98[0])}
    for _r in _t98[1:]:
        try:
            _L98_FAM[str(_r[_h98['ID']])] = set(x for x in str(_r[_h98['MonsterID']] or '').split('|') if x.strip())
        except Exception:
            continue
except Exception:
    pass

def _base_skin_of(ucode):
    b = get_monster_base(ucode)
    return int(b[1]) if b else 0    # base[1]=skin

def _skin_open_of(ucode):
    # 该妖已解锁的皮肤集合 (基础皮肤 + monster_skin_open + skin_owned 中本族皮肤)
    b = get_monster_base(ucode)
    mid = str(b[0]) if b else ''
    open_s = set()
    bs = _base_skin_of(ucode)
    if bs:
        open_s.add(bs)
    for sid in EQ_STATE.get('monster_skin_open', {}).get(ucode, []):
        open_s.add(int(sid))
    for sid in (EQ_STATE.get('skin_owned') or {}):
        fam = _L98_FAM.get(str(sid))
        if fam and mid in fam:
            open_s.add(int(sid))
    return sorted(open_s)

def _skin_eq_of(ucode):
    return int(EQ_STATE.get('monster_skin_eq', {}).get(ucode, _base_skin_of(ucode)))

def handle_skin_get(body_json):
    # req {ucode}; resp [1]=[[skinid, isopen, isequip],..]
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    ucode = str(req.get('ucode') or '')
    if ucode not in EQ_STATE['monsters'] and ucode not in ('1', '2', '3'):
        return b'[2004]'                     # 怪物id错误 (初始3只不在 state.monsters)
    eq = _skin_eq_of(ucode)
    rows = []
    for sid in _skin_open_of(ucode):
        rows.append([sid, 1, 1 if sid == eq else 0])
    if not rows:
        rows = [[_base_skin_of(ucode), 1, 1]]
    # 2026-09-07(第二轮) 修复: parseMessage 取最后一个元素为 msg, 客户端 NetSkinGet:data=msg[1]
    #   -> 服务器必须发 [1,[rows]] (旧版 [1,rows] 会让 msg[1]=第一行 -> 皮肤列表渲染失败/3个?占位)
    data = [rows]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

def handle_skin_replace(body_json):
    # req {ucode, skinid}; resp [1]=monsterInfo(更新后的30字段妖怪item, skin字段=新皮肤)
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    ucode = str(req.get('ucode') or '')
    skinid = int(req.get('skinid') or 0)
    if ucode not in EQ_STATE['monsters'] and ucode not in ('1', '2', '3'):
        return b'[2004]'
    # 客户端 NetSkinReplace:ctor 错误码全集: 2004 怪物id错误 / 110 皮肤id错误 / 111 皮肤未解锁 / 112 目前穿戴的即为该皮肤
    if skinid <= 0:
        return b'[110]'
    if skinid != _base_skin_of(ucode) and skinid not in _skin_open_of(ucode):
        return b'[111]'                      # 皮肤未解锁 (基础皮肤视为天然解锁)
    if _skin_eq_of(ucode) == skinid:
        return b'[112]'                      # 目前穿戴的即为该皮肤
    EQ_STATE['monster_skin_eq'][ucode] = skinid
    # 1500 列表同步: 穿戴的皮肤必然进入已解锁集合 (否则下次 1500 不显示 isequip=1)
    _open = EQ_STATE.setdefault('monster_skin_open', {}).setdefault(ucode, [])
    if skinid not in _open and skinid != _base_skin_of(ucode):
        _open.append(skinid)
    save_state()
    mi = _mon_with_state(ucode, skin_override=skinid)
    data = [json.loads(mi)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============================================================================
# 系统解锁 UMLOCK_SYS(1200)
# ----------------------------------------------------------------------------
# ★2026-09-12 (v30) 新增实装。原为 `_audit_netmap.py` B 类"可达未覆盖"，
#   且 v29 §2 把它列为"服务端真缺口"(唯一一处判断正确的缺口)。
#
#   客户端契约 (app/network/gamer/NetUnlockSys.lua 逐行核对):
#     · sendMessage(_systype, _callFunc)  req = { systype = <1..8> }
#     · recvMessage: statecode,msg = parseMessage(); **不读 msg[N]，只回调**
#       -> 成功只需 `[1,[]]`
#     · m_errorCode = { [0]="消息体为空", [100]="系统类型错误", [102]="等级不足！还无法进入此关卡" }
#
#   真实入口 (app/pve/PVEHome.lua，三处):
#     · :232 妖塔     sendMessage(UMLOCK_SYS, sys_config.SysType, cb -> 再发 1038 STORY_UNLOCKSCENE)
#     · :305 妖怪入侵  sendMessage(UMLOCK_SYS, sys_config.SysType, cb -> 进 CAggressHome)
#     · :340 妖神传说  sendMessage(UMLOCK_SYS, sys_config.SysType, cb -> 进 CLegendHome)
#   ⚠ 目标客户端在这三处被补丁改成"先进界面、再补发 1200 且回调为空" —— 但**照发不误**
#     (server_debug.log 可见 1200 实发)。所以 1200 未实装时虽不卡死，却**丢掉了等级门槛**。
#
#   私服口径: 按 L102_SysUnlock(ID=115 等) 与玩家等级做**真判** ——
#     等级够 -> [1,[]]；不够 -> [102] (客户端会弹"等级不足！还无法进入此关卡")。
#     见 v29 §4.1 ②: 鉴定系统门槛 = 角色 11 级 + L102_SysUnlock ID=115。
_SYS_UNLOCK_TYPES = {1: '妖塔', 2: '妖魔异闻', 3: '妖师对战', 4: '妖神传说',
                     5: '妖怪入侵', 6: '妖神排位', 7: '妖域镇守', 8: '冲虚道场'}
#   SysType -> 进入该玩法所需的 L02/S00 等级门槛(0=不拦)。
#   妖塔/入侵/传说 客户端 PVEHome 已各自用 sys_config.PlayLv 前置判过一遍本地等级，
#   服务端这里做**同口径二次校验**(防客户端被改后越级进入)，阈值与表一致:
#     · 妖塔(1)/妖怪入侵(5)/妖神传说(4): 走 S00_WorldMap + T2x 表, 此处按玩家等级不设硬门槛,
#       改由各玩法自身的 handler 校验 (1034/1063/1014)，避免误拦。
#   故本表当前全部为 0 = 不拦，但**结构已就位**，日后要在服务端加门槛只改这里。
_SYS_UNLOCK_MINLV = {}


def handle_unlock_sys(body_json=None):
    # 1200 UMLOCK_SYS: req{systype}; 只回调 -> 成功 = [1,[]]
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    st = req.get('systype')
    try:
        st = int(st)
    except Exception:
        return b'[100]'                        # 系统类型错误
    if st not in _SYS_UNLOCK_TYPES:
        return b'[100]'                        # 系统类型错误
    need = int(_SYS_UNLOCK_MINLV.get(st, 0) or 0)
    if need and int(EQ_STATE.get('player_lv', 1)) < need:
        return b'[102]'                        # 等级不足
    return b'[1,[]]'


# ============================================================================
# 布阵 FORMATION_LIST(3200) / FORMATION_UPDATE(3201) / FORMATION_SAVA(3202)
# ----------------------------------------------------------------------------
# ★2026-09-12 (v26 P0 "开战不能进入战斗") 说明 — 为什么 3202 是开战链路的必需项:
#
#   客户端明文 app/war/WarStagePVE.lua:1003~1080 的 startBattle 分支:
#     · PVEAGGRESS(妖主入侵无尽波) -> 1011 AGGRESS_FORMATION
#     · PVETRAIN / GUIDE          -> 本地直接 startFight()+startBattle()
#     · 其余【全部】(常规关卡/副本/剧情/排位/棋战/妖主入侵/扫荡) ->
#         sendMessage(MSG_ID.FORMATION_SAVA, UnitVolume, dataList, netCallFunc)
#       而 netCallFunc 里才调用 startFight()+m_WarUI:startBattle()。
#   即: 3202 没有正确响应 -> 回调永不触发 -> "点了开战没反应"。
#
#   客户端 NetFormationSave 契约 (app/network/war/NetFormationSave.lua 逐行核对):
#     sendMessage(_formationType,_formationList,_callFunc)
#       msg["model"]     = _formationType                  (上阵上限; 也是回调第 3 参)
#       msg["formation"] = [{ucode,pox,poy}, ...]          (由 dataList 的 ucode/posx/posy 摊平)
#                          全空时发假值 {""} (Lua 空表不为 nil 的写法)
#       ★ 注意请求键名是 **pox/poy**, 不是 posx/posy —— 别照 dataList 猜。
#     recvMessage(_recvMsg):
#       statecode ~= 1 -> showTips(self.m_errorCode[statecode]) 并 return (无回调)
#       msg[1] = formationType    (原样回传请求的 model)
#       msg[2] = formation        -> `for _,info in pairs(formation)` -> **必须是数组/表**,
#                                    元素 [ucode,posx,posy]; nil -> pairs(nil) 崩
#       msg[3] = loadFormation    -> `if loadFormation then for ... monster_updateItem(m[1],m)`
#                                    (nil 合法; 但要同步怪物最新 item 就得给)
#       最后 self.m_callFunc(loadlist, unloadlist, formationType)
#     ⚠ 客户端 unloadlist 计算有 bug: `for _,ucode in pairs(monsterlist) do if loadlist[ucode]==nil`
#       —— loadlist 是**数组**(下标 1..n)不是 ucode 字典, 故恒为 nil -> 每只怪都进 unloadlist。
#       回调里 netCallFunc 只做 startFight(), 不消费这两个参数, 所以不修客户端也能开战。
#
#   本处理器直接采用**服务端权威**语义: 校验客户端提交的布阵 -> 落库 -> 回显落库结果。
#   错误码取客户端 NetFormationSave:ctor 的 m_errorCode 全集(2001/2002/2009/2008/2005/2006/2010)。
# ============================================================================

# 布阵网格 —— ★★v53 (2026-09-13) 修"备战的妖怪出现坐标错误": 客户端里其实有**两套网格**,
#   旧实现把 ① 当成 ② 来校验, 于是把客户端合法坐标判成非法。
#
#   ① 协议网格 (客户端 app/network/war/NetFormationList.lua:19-20 `m_mapLenthY=2 / m_mapLenthX=5`):
#      只用于"给缺位上阵的怪找空格子" —— 客户端自己也只这么用
#      (recvMessage:getUnloadMapList `for posx=1,m_mapLenthX do for posy=1,m_mapLenthY`)。
#      ⇒ 生成默认阵型 / 找空格子 / 自动改位 都用它。
#
#   ② 战场网格 (MapSize×MapSize; T01_MstrGroup.MapSize: 434 行=5 / 295 行=6):
#      **客户端提交回来的坐标来自这里** —— app/war/WarStageUnits.lua:1066 `Stage:getMatrixUnitList()`
#      取的是每个单位在战场上的 `obj:getGridPos()`, 妖神传说那一族战斗组全是 6×6。
#      ⇒ 校验客户端提交的坐标必须用它。
#
#   事故实证 (work/server_debug.log, 用户真机 04:43:01 的失败请求):
#     3202 req={"formation":[{"poy":2,"ucode":"4","pox":1},{"poy":4,"ucode":"8","pox":1},
#                            {"poy":1,"ucode":"1","pox":1},{"poy":3,"ucode":"5","pox":1},
#                            {"poy":5,"ucode":"10","pox":1}],"model":5}
#     -> 旧 5×2 盒子把 py>2 全判非法 -> 回 [2006] "y坐标错误"
#     -> 客户端 NetFormationSave 走错误分支 -> m_callFunc 不执行 -> WarStagePVE 的
#        netCallFunc(startFight) 永不触发 -> **玩家卡在布阵界面开不了战**。
#     五个怪全在 pox=1、poy=1..5 是客户端布阵自动排布的**合法一列**(同 6×6 战场网格),
#     官方元数据 T01_MstrGroup.PositionCordinates("1,6|1,5|…|2,1") + PositionAvalable=12 也印证 12 个可选格。
_FORM_MAP_X = 5          # ① 协议网格宽 (生成默认阵型 / 找空格子)
_FORM_MAP_Y = 2          # ① 协议网格深
_FORM_GRID_MAX = 6       # ② 战场网格上界 = max(T01_MstrGroup.MapSize); 表读不到时保守取 6
try:
    _mg_t = _load_json('T01_MstrGroup.json')
    _mgh = {c: i for i, c in enumerate(_mg_t[0])}
    _mx = 0
    for _r in _mg_t[1:]:
        try:
            _mx = max(_mx, int(_r[_mgh['MapSize']] or 0))
        except Exception:
            continue
    if _mx > 0:
        _FORM_GRID_MAX = _mx
except Exception:
    pass

def _form_default_type():
    # 无 model 参数时的缺省上阵上限 (与原写死的 5 一致)
    return 5

def _form_load_type(body_json):
    # 从请求体取 model (= 客户端 self.m_formationType)。3200/3202 都带 model。
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return None
    if not isinstance(req, dict):
        return None
    try:
        v = req.get('model')
        return int(v) if v is not None else None
    except Exception:
        return None

def _form_stored(ftype):
    # EQ_STATE['formation'] = { "<model>": [[ucode,posx,posy], ...] }  (不存 skin: 皮肤走 get_monster_skin 单一实现)
    f = EQ_STATE.get('formation')
    if not isinstance(f, dict):
        return []
    v = f.get(str(ftype))
    if not isinstance(v, list):
        return []
    out = []
    for row in v:
        if isinstance(row, (list, tuple)) and len(row) >= 3:
            try:
                out.append([str(row[0]), int(row[1]), int(row[2])])
            except Exception:
                continue
    return out

def _form_put(ftype, rows):
    EQ_STATE.setdefault('formation', {})[str(ftype)] = [[str(u), int(x), int(y)] for (u, x, y) in rows]

def _form_pack(rows):
    # rows = [[ucode,posx,posy]] -> 下发形状 [[ucode,posx,posy,skin]]
    #   skin 走 state.get_monster_skin(穿戴皮肤优先) —— ★2026-09-11 v21 §3.5 收敛点, 别再改回 _base_skin_of
    out = []
    for (uc, px, py) in rows:
        b = get_monster_base(uc)
        if b is None:
            continue                      # 怪已被吞噬/移除 -> 静默剔除
        out.append([uc, int(px), int(py), _skin_eq_of(uc)])
    return out

def _form_load_items(rows):
    # loadFormation = 上阵怪的 30 字段 item 数组 (客户端 monster_updateItem(monsterInfo[1], monsterInfo))
    items = []
    for (uc, _px, _py) in rows:
        mi = _mon_with_state(uc)
        if mi:
            items.append(json.loads(mi))
    return items

def _form_proto_cells():
    # 协议网格 (5×2) 的空格枚举顺序 —— 与客户端 NetFormationList:getUnloadMapList 同序 (x 外, y 内)
    return [(x, y) for x in range(1, _FORM_MAP_X + 1) for y in range(1, _FORM_MAP_Y + 1)]

def _form_auto_rows(ftype):
    # 无落库记录时的默认布阵: 取前 ftype 只怪, 按**协议网格**空格顺序落位 (进游戏即有阵)。
    #   ★v53 (2026-09-13) 修: 旧实现写的是 `[uc, 1, len(rows)+1]` —— 即 (1,1)(1,2)(1,3)(1,4)(1,5),
    #     纵向排到 y=5,**越出了它自己声明的协议网格 (5×2)**, 而 3202 的校验只认 5×2 ⇒
    #     客户端把这份默认阵提交回来时必然吃到 [2006] y坐标错误 (与用户报的"备战妖怪坐标错误"同源)。
    #     现在与 _form_repair 的补位枚举完全一致 (x 外 y 内)。
    n = max(1, int(ftype or _form_default_type()))
    rows = []
    cells = _form_proto_cells()
    for uc in all_monster_ucodes():
        if len(rows) >= n:
            break
        if get_monster_base(uc) is None:
            continue
        if not cells:
            break
        x, y = cells.pop(0)
        rows.append([str(uc), x, y])
    return rows

def _form_sanitize(rows):
    # 只做合法性清洗 (去重/去死怪/去越界/去同格), **不补怪** ——
    #   补怪只属于"读取/开战前读取"路径 (官方 NetFormationList:repairFunc 语义);
    #   3201 单怪下阵是用户的显式意图, 补怪会把人刚摘下去的怪又装回来。
    alive = []
    seen_pos = set()
    seen_uc = set()
    for (uc, px, py) in rows:
        if uc in seen_uc:
            continue                      # 去重 (客户端 recvMessage 也会去重, 此处先保证干净)
        if get_monster_base(uc) is None:
            continue                      # 已吞噬/移除
        # ★v53: 越界判据用**战场网格**上界 (见文件上方 §布阵网格)。旧的 5×2 会把客户端
        #   来自 6×6 战场的合法坐标 (例: pox=1,poy=5) 直接丢掉 —— 那样"存进去的阵下次读就变形"。
        if not (1 <= px <= _FORM_GRID_MAX and 1 <= py <= _FORM_GRID_MAX):
            continue                      # 格子越界
        if (px, py) in seen_pos:
            continue                      # 同格冲突
        seen_pos.add((px, py))
        seen_uc.add(uc)
        alive.append([uc, px, py])
    return alive

def _form_repair(rows, ftype):
    # 上阵不足 -> 用未上阵的怪补满 (官方 NetFormationList:repairFunc 同语义)
    limit = max(1, int(ftype or _form_default_type()))
    out = list(rows[:limit])
    if not out:
        return _form_auto_rows(ftype)
    if len(out) >= limit:
        return out
    seen_pos = set((r[1], r[2]) for r in out)
    seen_uc = set(r[0] for r in out)
    for uc in all_monster_ucodes():
        if len(out) >= limit:
            break
        uc = str(uc)
        if uc in seen_uc or get_monster_base(uc) is None:
            continue
        for (px, py) in _form_proto_cells():          # ★v53: 与 _form_auto_rows 同一套枚举
            if (px, py) not in seen_pos:
                seen_pos.add((px, py))
                seen_uc.add(uc)
                out.append([uc, px, py])
                break
    return out

def _form_normalize(ftype):
    # 服务端当前权威布阵 (读取路径): 落库 -> 清洗 -> 缺位补怪 -> 保底默认
    return _form_repair(_form_sanitize(_form_stored(ftype)), ftype)

def _form_list_payload(ftype, repair=True):
    # repair=True  : 读取路径 (3200/3202) —— 缺位补怪, 保证"进游戏即有满编阵" (官方 repairFunc 语义)
    # repair=False : 显式编辑路径 (3201 单怪下阵) —— 尊重用户意图, 不把刚摘下的怪装回去
    rows = _form_normalize(ftype) if repair else _form_sanitize(_form_stored(ftype))
    return [ _form_pack(rows), _form_load_items(rows) ]

def handle_formation_list(body_json=None):
    # 3200 FORMATION_LIST: resp [1]=formation([[ucode,posx,posy,skin]]), [2]=loadFormation(怪物item数组)
    ftype = _form_load_type(body_json)
    if ftype is None:
        try:
            ftype = int(EQ_STATE.get('formation_type') or 0) or _form_default_type()
        except Exception:
            ftype = _form_default_type()
    formation, load_f = _form_list_payload(ftype)
    return ('[1,' + json.dumps([formation, load_f], separators=(',', ':')) + ']').encode('utf-8')

def handle_formation_update(body_json):
    # 3201 FORMATION_UPDATE: req {model,type,ucode,pox,poy}  (单个怪上/下阵, 拖拽布阵界面用)
    #   type: NFORM_OPERATIONTYPE.LOAD=1 上阵 / UNLOAD=0 下阵
    # resp: 与 3200 同形 -> 客户端 NetFormationUpdate 照 3200 取 [1]/[2]
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    ftype = _form_load_type(body_json) or _form_default_type()
    ucode = str(req.get('ucode') or '')
    op = 0
    try:
        op = int(req.get('type') or 0)
    except Exception:
        op = 0
    # ⚠ 这里用 _form_sanitize 而非 _form_normalize: 单怪下阵是显式意图, 补怪会把它又装回来
    rows = [r for r in _form_sanitize(_form_stored(ftype)) if r[0] != ucode]
    if op == 1 and get_monster_base(ucode) is not None:
        try:
            px = int(req.get('pox') if req.get('pox') is not None else req.get('posx') or 0)
            py = int(req.get('poy') if req.get('poy') is not None else req.get('posy') or 0)
        except Exception:
            px, py = 0, 0
        # ★v53: 合法性判据 = 战场网格 (旧 5×2 会拒掉客户端来自 6×6 战场的合法坐标 -> 卡在布阵)
        if not (1 <= px <= _FORM_GRID_MAX and 1 <= py <= _FORM_GRID_MAX):
            used = set((r[1], r[2]) for r in rows)
            for (x, y) in _form_proto_cells():         # ★v53: 自动改位搜协议网格的空格
                if (x, y) not in used:
                    px, py = x, y
                    break
        rows.append([ucode, px, py])
    _form_put(ftype, rows[:max(1, ftype)])
    EQ_STATE['formation_type'] = int(ftype)
    save_state()
    formation, load_f = _form_list_payload(ftype, repair=False)
    return ('[1,' + json.dumps([formation, load_f], separators=(',', ':')) + ']').encode('utf-8')

def handle_formation_save(body_json):
    # 3202 FORMATION_SAVA (客户端注释: "修正版 上阵下阵指令") —— **开战链路必需**
    #   req  = {model, formation:[{ucode,pox,poy}...]}  (见 NetFormationSave:sendMessage)
    #   resp = [1,[formationType, formation, loadFormation]]
    #          formationType = 原样回传 model (回调第 3 参; 客户端 self.m_formationType 校验用)
    #          formation     = 服务端确认后落库的布阵 [[ucode,posx,posy,skin], ...]
    #          loadFormation = 上阵怪最新 30 字段 item (客户端 monster_updateItem 同步)
    #   错误码 (客户端 m_errorCode): 2001 布阵类型错误 / 2002 怪物背包为空 / 2009 布阵信息为空
    #                                2008 X坐标错误 / 2006 Y坐标错误 / 2005 背包中不存在该怪物 / 2010 布阵怪物数量过多
    try:
        req = json.loads(body_json) if body_json else None
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'                                   # 0 = 消息体为空
    try:
        ftype = int(req.get('model') or 0)
    except Exception:
        ftype = 0
    if ftype <= 0:
        return b'[2001]'                                   # 布阵类型错误
    have = all_monster_ucodes()
    if not have:
        return b'[2002]'                                   # 怪物背包为空
    rows = req.get('formation')
    if rows is None:
        return b'[2009]'                                   # 布阵信息为空
    if not isinstance(rows, list):
        return b'[2009]'
    if len(rows) >= 1 and rows[0] == '':                   # NetFormationSave 的 {"",} 空表伪值
        rows = []
    if len(rows) > ftype:
        return b'[2010]'                                   # 布阵怪物数量过多
    accepted = []
    seen_pos = set()
    seen_uc = set()
    for it in rows:
        if not isinstance(it, dict):
            continue
        uc = str(it.get('ucode') if it.get('ucode') is not None else '')
        if uc in ('', '-1', 'nil', 'None'):
            continue                                       # UCODE_DEONE = 空位
        if uc not in [str(x) for x in have]:
            return b'[2005]'                               # 背包中不存在该怪物
        try:
            px = int(it.get('pox') if it.get('pox') is not None else (it.get('posx') or 0))
        except Exception:
            return b'[2008]'
        try:
            py = int(it.get('poy') if it.get('poy') is not None else (it.get('posy') or 0))
        except Exception:
            return b'[2006]'
        if not (1 <= px <= _FORM_GRID_MAX):
            return b'[2008]'                               # X坐标错误
        if not (1 <= py <= _FORM_GRID_MAX):
            return b'[2006]'                               # Y坐标错误
        if uc in seen_uc or (px, py) in seen_pos:
            continue                                       # 重复怪 / 同格冲突 -> 静默丢弃 (客户端也做去重)
        seen_uc.add(uc)
        seen_pos.add((px, py))
        accepted.append([uc, px, py])
    # 客户端可能发空 formation (全空位) -> 视为"保存当前上阵"而非清空, 用现有布阵兜底
    if accepted:
        _form_put(ftype, accepted)
        EQ_STATE['formation_type'] = int(ftype)
        save_state()
    formation, load_f = _form_list_payload(ftype)
    return ('[1,' + json.dumps([ftype, formation, load_f], separators=(',', ':')) + ']').encode('utf-8')

# ============ 邮件 MAIL_GET(1055) / MAIL_READ(1058) / MAIL_RECEIVE(1057) / MAIL_TOTAL(1059) ============
# 时间字段 = **毫秒时间戳** (客户端 MailList:getTimeStamp(timeStamp) -> os.date("%Y-%m-%d", timeStamp/1000);
#   旧版 "20260824000000" 字符串 /1000 => 20260824000s (2612年) => os.date 非法 -> 详情"有效时间"显示异常 (-1 现象))
def _mail_now_ms():
    return int(time.time() * 1000)

_MAIL = [
    [1, "欢迎来到妖怪世界", _mail_now_ms(), 1, 0, [['2', 100020, 10]]],   # 未读奖励邮件: 道具100020 x10
    [2, "成长礼包", _mail_now_ms(), 1, 1, [['1', 1001, 3]]],
    # 2026-09-08 用户指令邮件: 妖魂 100000 / 鬼道积分 50000 (1057 领取一次性)
    [3, "妖魂补给", _mail_now_ms(), 1, 0, [['7', 9999, 100000]]],
    [4, "鬼道点数补给", _mail_now_ms(), 1, 0, [['6', 9999, 50000]]],
]
# 有效期 (秒; 官方=发送后30天; 1055 列表字段未含, 客户端在 1058 详情按时间字段显示; 此处供扩展)
MAIL_EXPIRE_SEC = 30 * 86400

# ============================================================================
# ★2026-09-12 (v27) GM 邮件 —— 从 work/gm_mail.json **热读**的投递通道
# ★2026-09-13 (v32) 加三件事: 附件类型白名单+静态表校验 / 附件 ≤5 / time 稳定
# ----------------------------------------------------------------------------
#   为什么要做成"读文件"而不是"写 state.json":
#     ① 服务器进程把 EQ_STATE 放在内存里, 外部改 state.json 会被它下一次 save_state 覆盖;
#        要写存档就得先停服 -> 而停服会打断正在游戏的客户端连接。
#     ② 邮件是"运营配置"而不是"玩家数据", 放文件里语义更对, 也方便反复改。
#     ③ 领取/删除仍然记在 state.json 的 mail_claimed / mail_deleted (按 id 字符串), 逻辑不变。
#
#   文件格式 (UTF-8 JSON 数组; 每次请求按 mtime 判缓存, 改完存盘**立刻生效, 不用重启**)：
#     [
#       {"id": 9001, "title": "妖主大入侵宝箱 ×100", "type": 1, "read": 0,
#        "time": 1789000000000,
#        "items": [[18, 1028, 100]]}
#     ]
#     · id     : **必须 ≥ 9000** (GM_MAIL_ID_MIN), 避开内置邮件 1~4
#     · title  : 邮件标题 (1055 列表里显示的那行)
#     · type   : emailType, 1=个人 (与内置邮件一致)
#     · read   : 0=未读 1=已读
#     · time   : 毫秒时间戳。**不填也不会每次随文件 mtime 变** (走 gm_mail_time.json 边车表),
#                否则每改一次文件, 所有邮件的 time 都会变 -> 客户端 NetMailTotal:updateState
#                按 (id,title,time,emailtype) 判重 -> 列表里出现**重复邮件行**。
#     · items  : 附件列表, 每项 = [REWARD_NEWTYPE, 静态id, 数量]; **最多 5 项**
#                (客户端 NetMailRead 只读 affix1~affix5; 第 6 项起"发得到但看不见")
# ============================================================================
import os as _os
GM_MAIL_ID_MIN = 9000            # GM 邮件 id 下限 (避开内置 1~4)
MAIL_AFFIX_MAX = 5               # = 客户端 affix 槽位数, 不是拍脑袋定的

# 附件类型 -> (校验用静态表, 名称列)。表里没有 = 只吃数量/不支持。
#   ★ 与 handlers_bag.grant_entries() 的分支**一一对应**; 两边不一致会出现"校验放行但发不出去"。
MAIL_ITEM_TABLE = {
    1:  ('L01_Equipments', 'Name'),
    2:  ('L02_Loots', 'SceneName'),
    4:  ('L91_Status', 'Name'),
    5:  ('L03_Equipghost', 'Name'),
    14: ('L98_Monsterskin', 'SkinName'),
    18: ('L103_TreasureBox', 'SceneName'),
    22: ('T36_HeadFrame', 'Name'),
    # ★v59 续: 礼包/背包卡(type98)。此前只差这条 —— grant_entries 已经能发 98（本次补），
    #   但校验器拒绝 -> 后台发不出礼包邮件。校验器与发放分支必须一一对应。
    98: ('L10_GiftInfo', 'ItemName'),
}
MAIL_ITEM_MONEY = (3, 6, 7, 8, 9, 17, 21)  # 铜钱/鬼道/妖魂/唤妖符/体力/玩家经验/活跃度: 静态id 无意义
#   ★2026-10-11: 补 21(活跃度)。`handlers_bag.grant_entries` 本轮接上了 ty21
#     （走 `handlers_task._add_extra_vital`），而**校验器与发放分支必须一一对应** ——
#     类型 98 那次就吃过"发放能发、校验器拒绝 -> 后台发不出礼包邮件"的亏（见上表 98 的注释）。
#     ⚠ 24/25/29 仍**刻意不在**这里: 它们在客户端没有任何读路径 / 服务端无从触发（见
#     handlers_bag.grant_entries 末尾的三条说明）。放进来只会让后台"能发但没用"。
MAIL_ITEM_TYPES = tuple(sorted(list(MAIL_ITEM_TABLE) + list(MAIL_ITEM_MONEY)))

# ★v36 修正记录: 曾在这里登记过 `MAIL_ITEM_DISABLED = {14: '皮肤'}` —— 那是我**理解错了**用户的意思。
#   用户说的"皮肤"是**妖怪的皮肤形态**(妖神夜庭柯/大天师/天师-暮成雪 这类"穿着皮肤的单独怪"),
#   不是系统里的"皮肤"附件类型(14)。该禁用已**完全回滚**, type=14 恢复可用。
#   "皮肤形态怪"的处置在 admin_backend.search_items() 里按 **L91.SourceID 链首**过滤 (见那里的注释)。
MAIL_ITEM_DISABLED = {}                    # 预留: 真正要停用的附件类型写这里 (目前为空)

_ID_SET = {}

def _table_ids(name):
    if name not in _ID_SET:
        try:
            _ID_SET[name] = set(int(k) for k in load_table_index(name, 'ID'))
        except Exception as e:
            print('[MAIL] WARN 静态表 %s 读取失败: %r (该类型附件一律拒绝)' % (name, e), flush=True)
            _ID_SET[name] = set()
    return _ID_SET[name]

def validate_mail_item(rt, pid, cnt):
    """校验一个邮件附件 -> (ok: bool, 规范化三元组 or None, 原因: str)

    ★为什么必须校验静态 id (铁律 #2): 客户端拿 `getObject(T,id):getStatic_X()` 去查表,
      拿到服务端编造的 id 会 nil 索引 -> 原生层 SIGSEGV。所以"能发什么"必须由**真表**决定,
      而不是由运营在网页里手输的数字决定。本函数同时被 /admin 的写入接口复用。
    """
    try:
        rt = int(rt)
    except Exception:
        return False, None, '奖励类型不是整数'
    # ★v36: 停用检查放在白名单**之前** (顺序很重要 —— 若某类型同时在白名单与停用表里,
    #   放后面就会被"在可用清单内"先放行)。目前 MAIL_ITEM_DISABLED 为空, 这段是给以后用的。
    if rt in MAIL_ITEM_DISABLED:
        return False, None, ('奖励类型 %s(%s) 已被**停用** —— 后台不再提供这一项 (%s)'
                             % (rt, MAIL_ITEM_DISABLED[rt],
                                '见 handlers_sys.MAIL_ITEM_DISABLED 的注释'))
    if rt not in MAIL_ITEM_TYPES:
        return False, None, ('奖励类型 %s 服务端未实现落库 (可用: %s)'
                             % (rt, '/'.join(str(t) for t in MAIL_ITEM_TYPES)))
    try:
        cnt = int(cnt)
    except Exception:
        return False, None, '数量不是整数'
    if cnt <= 0:
        return False, None, '数量必须 > 0'
    if rt in MAIL_ITEM_MONEY:
        return True, (rt, 0, cnt), ''          # 货币类: pid 归一化成 0
    try:
        pid = int(pid)
    except Exception:
        return False, None, '静态 id 不是整数'
    tbl, _col = MAIL_ITEM_TABLE[rt]
    ids = _table_ids(tbl)
    if ids and pid not in ids:
        return False, None, '静态 id %d 不在 %s 里 (客户端会 nil 索引 -> 崩)' % (pid, tbl)
    # ★2026-11-07（6 阶收口）：type=4 妖怪且 Star>=6 -> 拒绝。
    #   客户端进化以 L91.AdvanceID 为门控（5 阶为 0），6 阶正常玩法到不了；直发 6 阶
    #   会出现属性空白/队伍错位（v35 用户实测），修前只能靠后台删档。
    #   测试/排错需要时显式设 `YGZZ_ALLOW_STAGE6=1`。
    if rt == 4 and _os.environ.get('YGZZ_ALLOW_STAGE6') != '1':
        try:
            import monster_identity as _mi
            _d = _mi.describe(pid)
            if _d.get('known') and int(_d.get('star') or 0) >= 6:
                return False, None, ('第 6 阶形态（Star=6）客户端不支持：属性空白/队伍错位；'
                                     '正常玩法最高 5 阶。请改发链首或 1–5 阶。')
        except Exception:
            pass
    return True, (rt, pid, cnt), ''

_GM_MAIL_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'gm_mail.json')
_GM_TIME_PATH = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'gm_mail_time.json')
_GM_CACHE = {'mtime': None, 'rows': [], 'error': '', 'skipped': []}
_GM_TIME = {'mtime': None, 'map': {}}

def _gm_mail_times():
    """id -> 首次投递时间(ms) 的边车表。

    ★解决 D6: 原实现 `int(_c.get('time') or _tms)` —— time 不填就用 gm_mail.json 的 mtime。
      于是**任何一次编辑**(哪怕只改另一封邮件的一个字)都会让**所有**邮件的 time 变化。
      客户端 NetMailTotal:updateState 的判重键是 (id,title,time,emailtype),
      time 一变就被当成新邮件 -> 列表里同一封出现两行、三行……
      现在: 显式 time > 边车表里的历史值 > 首次出现时分配并落盘。写入边车表后**永不再变**。
    """
    try:
        mt = _os.path.getmtime(_GM_TIME_PATH)
    except Exception:
        _GM_TIME['map'] = {}
        _GM_TIME['mtime'] = None
        return _GM_TIME['map']
    if _GM_TIME['mtime'] == mt:
        return _GM_TIME['map']
    try:
        with open(_GM_TIME_PATH, 'rb') as f:
            _GM_TIME['map'] = {str(k): int(v) for k, v in json.loads(f.read().decode('utf-8')).items()}
    except Exception:
        _GM_TIME['map'] = {}
    _GM_TIME['mtime'] = mt
    return _GM_TIME['map']

def _gm_mail_save_times():
    try:
        with open(_GM_TIME_PATH, 'w', encoding='utf-8') as f:
            json.dump(_GM_TIME['map'], f, ensure_ascii=False, indent=1, sort_keys=True)
        _GM_TIME['mtime'] = _os.path.getmtime(_GM_TIME_PATH)
    except Exception as e:
        print('[MAIL] WARN 边车时间表写入失败: %r' % e, flush=True)

def gm_mail_raw():
    """读取并**校验** gm_mail.json -> (rows, error, skipped)。

    rows 行格式与内置 `_MAIL` 完全同构: [id, title, time_ms, emailType, read, [[rt,pid,cnt], ...]]
    解析失败时**保留上一份好配置**并记录 error (而不是返回空表):
      返回空表会让运营在打字中途看到"所有邮件突然消失", 反而更难判断。
    """
    try:
        mt = _os.path.getmtime(_GM_MAIL_PATH)
    except Exception:
        _GM_CACHE.update({'mtime': None, 'rows': [], 'error': '', 'skipped': []})
        return [], '', []
    if _GM_CACHE['mtime'] == mt:
        return _GM_CACHE['rows'], _GM_CACHE['error'], _GM_CACHE['skipped']
    rows, err, skipped = [], '', []
    try:
        with open(_GM_MAIL_PATH, 'rb') as f:
            # ★v59 续2: 用 utf-8-sig 解码 —— 运营用记事本/PowerShell 编辑过 gm_mail.json 会带 BOM，
            #   而 `json.loads(bytes.decode('utf-8'))` 遇 BOM 直接抛
            #   "Unexpected UTF-8 BOM (decode using utf-8-sig)" -> 整个 GM 邮件通道静默失效。
            #   （本轮真机就踩到：我自己用 pwsh ConvertTo-Json|Set-Content 写文件带了 BOM，
            #     结果 1055 拉列表时"名次奖励邮件投递失败"。）
            cfg = json.loads(f.read().decode('utf-8-sig'))
        if not isinstance(cfg, list):
            raise ValueError('顶层必须是数组')
        times = _gm_mail_times()
        _dirty_times = False
        _seen_ids = set()
        for c in cfg:
            if not isinstance(c, dict):
                skipped.append(('?', '条目不是对象')); continue
            try:
                mid = int(c['id'])
            except Exception:
                skipped.append((repr(c.get('id')), 'id 缺失或非整数')); continue
            if mid < GM_MAIL_ID_MIN:
                skipped.append((mid, 'id < %d (会与内置邮件 %s 撞号)' % (GM_MAIL_ID_MIN, [m[0] for m in _MAIL])))
                continue
            if mid in _seen_ids:
                skipped.append((mid, 'id 在文件里重复 (客户端按 id 删除, 重复会让列表错乱)')); continue
            _seen_ids.add(mid)
            # ---- 时间: 显式 > 边车 > 新分配并落盘 ----
            _t = c.get('time')
            if _t:
                try:
                    _t = int(_t)
                except Exception:
                    _t = None
            if not _t:
                _t = times.get(str(mid))
            if not _t:
                _t = int(mt * 1000)
                times[str(mid)] = _t
                _dirty_times = True
            # ---- 附件: 逐条校验, 非法条目**丢弃并告警**(不静默, 不假装发放) ----
            items = []
            _raw_items = list(c.get('items') or [])
            for it in _raw_items:
                if not isinstance(it, (list, tuple)) or len(it) < 3:
                    skipped.append((mid, '附件格式应为 [类型, 静态id, 数量]: %r' % (it,))); continue
                okv, norm, why = validate_mail_item(it[0], it[1], it[2])
                if not okv:
                    skipped.append((mid, '%s' % why)); continue
                items.append([norm[0], norm[1], norm[2]])
            if _raw_items and not items:
                # 声明了附件但一条都不合法 -> **整封丢弃**。
                #   留一封空邮件只会让运营以为"发出去了", 而玩家点领取得到 1403(无奖励)。
                skipped.append((mid, '全部附件都不合法 -> 整封邮件丢弃'))
                continue
            if len(items) > MAIL_AFFIX_MAX:
                skipped.append((mid, '附件 %d 项 -> 截断到 %d 项 (客户端只有 affix1~%d 个显示位, '
                                     '多发等于"领得到看不见")' % (len(items), MAIL_AFFIX_MAX, MAIL_AFFIX_MAX)))
                items = items[:MAIL_AFFIX_MAX]
            rows.append([mid, str(c.get('title') or 'GM 邮件'), int(_t),
                         int(c.get('type') or 1), int(c.get('read') or 0), items])
        if _dirty_times:
            _gm_mail_save_times()
    except Exception as e:
        err = '%r' % e
        print('[MAIL] ERROR gm_mail.json 解析失败, 继续沿用上一份好配置: %s' % err, flush=True)
        rows = _GM_CACHE['rows']
        skipped = [('*', '文件解析失败: %s' % err)]
    for _s in skipped:
        print('[MAIL] WARN 跳过/调整 GM 邮件条目: id=%s %s' % (_s[0], _s[1]), flush=True)
    _GM_CACHE.update({'mtime': mt, 'rows': rows, 'error': err, 'skipped': skipped})
    return rows, err, skipped

def _gm_mail():
    """兼容旧接口: 只要行 (列表/详情/领取/删除都走 _mail_all -> 这里)"""
    return gm_mail_raw()[0]

def gm_mail_write(cfg):
    """写回 gm_mail.json (供 /admin 使用)。调用方需先逐条 validate_mail_item。"""
    tmp = _GM_MAIL_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    _os.replace(tmp, _GM_MAIL_PATH)
    _GM_CACHE['mtime'] = None        # 强制下次重读
    return _GM_MAIL_PATH

def gm_mail_next_id():
    """下一个可用 GM 邮件 id (≥9000, 避开已用与已领取/已删除记录, 单调递增)"""
    used = set(int(m[0]) for m in _gm_mail())
    for x in (EQ_STATE.get('mail_claimed') or []):
        try:
            used.add(int(x))
        except Exception:
            pass
    for x in (EQ_STATE.get('mail_deleted') or []):
        try:
            used.add(int(x))
        except Exception:
            pass
    n = GM_MAIL_ID_MIN
    while n in used:
        n += 1
    return n

def _mail_all():
    # 内置邮件 + GM 邮件。列表/详情/领取/删除**统一走这里**, 避免四处漏改。
    return _MAIL + _gm_mail()

def _mail_claimed():
    # 已领取邮件 id 持久集 (2026-09-08: 修复"可重复反复领取"——服务器侧一次性)
    lst = EQ_STATE.setdefault('mail_claimed', [])
    if not isinstance(lst, list):
        EQ_STATE['mail_claimed'] = []
        lst = EQ_STATE['mail_claimed']
    return lst

def _mail_deleted():
    # ★2026-09-12 (v26 goal-round-5) MAIL_DEL(1056) 落库: 被删除的邮件 id 集
    #   与 _mail_claimed 分开存: "已领取"和"已删除"是两种语义, 合并会让"领了又删"的邮件
    #   在统计/回滚时无法区分 (将来要做"已删除恢复"就会发现分不开)。
    lst = EQ_STATE.setdefault('mail_deleted', [])
    if not isinstance(lst, list):
        EQ_STATE['mail_deleted'] = []
        lst = EQ_STATE['mail_deleted']
    return lst

def _mail_alive():
    # 未领取/未删除/仍有效的邮件 (读界: 已领的、已删的从列表消失)
    done = set(str(x) for x in _mail_claimed())
    gone = set(str(x) for x in _mail_deleted())
    return [m for m in _mail_all() if str(m[0]) not in done and str(m[0]) not in gone]
# 附件名 (1058 邮件详情 affix*name 渲染; 客户端直接用静态名; 2026-09-08 全量缓存, 消除每请求读盘)
#   ★2026-09-13 (v32): 表来源改用 MAIL_ITEM_TABLE 统一映射 (与校验器同一张表, 不会再出现
#     "校验说合法、名字取不到"的错位), 并改走 state.load_table (去掉 rfind(']') 与写死盘符)。
_MAIL_NAME_CACHE = {}
def _mail_name(kind, oid):
    ck = (str(kind), str(oid))
    if ck in _MAIL_NAME_CACHE:
        return _MAIL_NAME_CACHE[ck]
    try:
        _k = int(kind)
        if _k in MAIL_ITEM_MONEY:
            # 货币类附件没有静态表名, 用一个可读标签 (客户端只拿它显示)
            v = {3: '铜钱', 6: '鬼道积分', 7: '妖魂', 8: '唤妖符', 9: '体力'}.get(_k, '数量')
            _MAIL_NAME_CACHE[ck] = v
            return v
        _t, _nc = MAIL_ITEM_TABLE[_k]
        key = (_t, _nc)
        if key not in _MAIL_NAME_CACHE:
            _rows = load_table(_t)
            _h = {c: i for i, c in enumerate(_rows[0])}
            _map = {}
            for _r in _rows[1:]:
                try:
                    _map[str(_r[_h['ID']])] = str(_r[_h[_nc]] or _r[_h['ID']])
                except Exception:
                    continue
            _MAIL_NAME_CACHE[key] = _map
        v = _MAIL_NAME_CACHE[key].get(str(oid), str(oid))
        _MAIL_NAME_CACHE[ck] = v
        return v
    except Exception:
        return str(oid)

def handle_mail_read(body_json):
    # 1058 邮件详情: NetMailRead:recvMessage = emailitem msg[1] **32 字段**:
    #   [id,送信者名,送信者id,收信者名,收信者id,内容, affix1id,affix1name,affix1num,affix1Type, ...affix5Type,
    #    time, isRead(0/1), isHaveGoods(0没有/1有), emailType(0全局/1个人), title, reward(奖励组id)]
    #   ★ 2026-09-08 修复: 旧版仅 27 字段 -> 客户端 emailitem[28..32]=nil -> isHaveGoods=nil -> 附件渲染为空
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        emailid = int(req.get('emailid') or 0)
    except Exception:
        return b'[1400]'
    if str(emailid) in set(str(x) for x in _mail_claimed()):
        return b'[566]'                       # 邮件id过期请重新刷新列表 (已领)
    for m in _mail_all():
        if int(m[0]) == emailid:
            row = [m[0], '系统', 99999, '测试', 1, str(m[1]) + '，点击附件领取奖励。']
            for i in range(5):
                if i < len(m[5]):
                    rt, pid, cnt = str(m[5][i][0]), str(m[5][i][1]), int(m[5][i][2])
                    row += [int(pid), _mail_name(rt, pid), cnt, int(rt)]
                else:
                    row += [0, '', 0, 0]
            row += [m[2], 0 if int(m[4]) == 0 else 1, 1 if len(m[5]) else 0, 0,
                    m[1], 1 if len(m[5]) else 0]
            return ('[1,[' + json.dumps(row, ensure_ascii=False, separators=(',', ':')) + ']]').encode('utf-8')
    return b'[566]'                       # 邮件id过期请重新刷新列表

def _mail_public(m):
    # [[id,title,time,emailtype,state],..]
    return [m[0], m[1], m[2], m[3], m[4]]

def handle_mail_get(body_json=None):
    # 1055 拉取邮件列表 (仅未领).
    # ★ 2026-09-07 周/月卡每日奖励改走"每日任务"唯一通道 (客户端界面权威:
    #   SWeekMonthCard 渲染 T23_Function 182/183 -> 800014/800015 -> T02 组;
    #   T37 1016/1017 每日滚动, 1069 领取), 停用原每日 6:00 邮件补发通道
    #   (旧内容按 L06 文案=小仙桃/体力, 与界面/T02 不一致且导致每日双份不同奖励)。
    #   ⚠ 包裹层: NetMailTotal:recvMessage maillist=msg[1] -> 响应必须 [1,[items]]
    # ★v59 续2: 异界之门**名次奖励**的结算时机挂在这里 —— 用户口径"按玩家id通过邮件发放"，
    #   而"每天第一次拉邮件"就是私服里最接近"活动结算"的时机（跨天判定，幂等）。
    #   必须 try 包住：结算失败绝不能拖垮邮件列表（铁律 #202「可选动作不能拖垮主流程」）。
    try:
        from handlers_cardboss import settle_daily_rank_rewards
        _settled = settle_daily_rank_rewards()
    except Exception as _e:
        _settled = []
        print('[MAIL] 名次奖励结算跳过: %r' % (_e,))
    # ★v59 续2: 结算成功就**主动推 1059**，否则玩家要重开邮件界面才看得到新邮件
    #   （push_mails 内部走 handle_mail_total -> 本函数；settle 已把 rank_settled_day 置今天，
    #     所以这一次重入不会再结算，不会递归/重复投递 —— 幂等由 date 标记保证）
    if _settled:
        try:
            import admin_backend as _ab
            _n = _ab.push_mails()
            print('[MAIL] 名次奖励结算后已推 1059 x%d' % (_n or 0))
        except Exception as _e2:
            print('[MAIL] 名次奖励推送跳过: %r' % (_e2,))
    items = [_mail_public(m) for m in _mail_alive()]
    return ('[1,[' + json.dumps(items, separators=(',', ':')) + ']]').encode('utf-8')

def handle_mail_total(body_json=None):
    return handle_mail_get()

def handle_mail_receive(body_json):
    """1057 MAIL_RECEIVE —— 领取邮件奖励。

    客户端契约 (app/network/mail/NetMailReceive.lua + MailList.lua):
      sendMessage(_type, _emailid)   type: EMAIL_RECEIVE = {RCVALL=0 一键领取, RCVSINGLE=1 单个领取}
        调用点 MailList.lua:361  单个领取 -> (RCVSINGLE, mail_info.id)
               MailList.lua:436  一键领取 -> (RCVALL, 0)          <-- emailid 是 **0**
      recvMessage:
        msg[1] = 领取到的邮件 id 列表 -> `for _, _info in pairs(msg[1]) do table.insert(emailList, _info)`
                 -> 再 `for i,mailId in ipairs(emailList) do ...deleteItem(mailId)`
                 ★ MailList.lua:337/425/452 处的 deleteItem 会 `tonumber(info.id) == tonumber(mailId)`
                   -> **msg[1] 的元素必须是 id 标量**, 发 5 元组数组会让 tonumber(表)=nil
                   -> 删除静默失败 -> "领完邮件还在列表里" (v32 前的实际行为)
        msg[2] = roleInfo      -> SQGamer:setRoleInfo
        msg[3] = dropInfo      -> NetDropRewardData:refreshDropInfo (奖励飘字)

    ★2026-09-13 (v32) 三处修复:
      D3 一键领取: 原实现**从不读 req['type']**, 只按 emailid 匹配; 客户端一键领取传的是 emailid=0
         -> 匹配不到任何邮件 -> "点了没反应"(服务端返回成功但零奖励)。现在 type=0 = 领取**全部可领**。
      D1/D2 发放: 改走 handlers_bag.grant_entries() —— 全工程唯一发放实现。
         原邮件分支只实现了 1/2/3/6/7/9/18, 于是 4/5/14/22 静默丢失;
         且装备用 `len(equip_id)` 当 lv/exp 的键 -> 键不连续时装备永远没有等级行。
      D4 msg[1]: 改为**本次实际领取的 id 列表**(之前发的是"剩余存活邮件"的 5 元组数组)。
    """
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        rtype = int(req.get('type') or 0)
    except Exception:
        return b'[1401]'                       # 操作类型错误
    if rtype not in (0, 1):
        return b'[1401]'
    try:
        emailid = int(req.get('emailid') or 0)
    except Exception:
        return b'[1400]'                       # 领取邮件ID错误

    claimed_set = set(str(x) for x in _mail_claimed())
    deleted_set = set(str(x) for x in _mail_deleted())
    alive = [m for m in _mail_all()
             if str(m[0]) not in claimed_set and str(m[0]) not in deleted_set]

    # ★判定规则 (emailid 优先, 最不容易误伤):
    #   · emailid > 0 -> 只领这一封 (客户端 RCVSINGLE 的形态: type=1, emailid=id)
    #   · emailid == 0 -> 领全部可领 (客户端 RCVALL 的形态: type=0, emailid=0)
    #   为什么不严格按 type 判: 万一某处 UI 传了 (type=0, emailid=X) 这种不一致的组合,
    #   按 type 会**意外把全服邮件一次性领光**; 按 emailid 最多只领错一封。
    if emailid > 0:
        targets = [m for m in alive if int(m[0]) == emailid]
        if not targets:
            # 已领 / 不存在 / 已删 -> 566 "邮件id过期请重新刷新列表"
            #   客户端 NetMailReceive:recvMessage 对 566 走 m_failFunc (MailList 传的是 refreshListView)
            #   -> 列表自动刷新; 而 [0,{}] 只会弹一句"消息体为空"的错误提示。
            return b'[566]'
    else:
        targets = list(alive)
        if not targets:
            return b'[1402]'                   # 邮件列表为空
    # 只领取"有附件"的邮件; 没有附件的通知类邮件不消耗领取次数
    targets = [m for m in targets if m[5]]
    if not targets:
        return b'[1403]'                       # 无奖励可以领取

    # ---- 发放: 交给唯一实现 ----
    from handlers_bag import grant_entries      # 延迟导入: 避免任何 import 顺序耦合
    entries = []
    for m in targets:
        for it in m[5]:
            entries.append((int(it[0]), it[1], int(it[2])))
    drop_info = grant_entries(entries)
    _sk = _last_grant_skipped()
    if _sk:
        print('[MAIL] 领取时被跳过的附件: %s' % _sk, flush=True)
    for m in targets:
        m[4] = 1                               # 标记已读
        _mail_claimed().append(str(m[0]))
    save_state()

    claimed_ids = [int(m[0]) for m in targets]            # D4: id 标量列表
    data = [claimed_ids, json.loads(role_info_array()), json.loads(drop_info)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')


def _last_grant_skipped():
    # 取 handlers_bag 最近一次发放被跳过的条目 (只读, 不触发新发放)
    try:
        from handlers_bag import _GRANT_SKIPPED
        return list(_GRANT_SKIPPED)
    except Exception:
        return []


# ============================================================================
# ★2026-09-12 (v26 goal-round-5) 删除邮件 MAIL_DEL(1056)
# ----------------------------------------------------------------------------
#   客户端契约 (app/network/mail/NetMailDelete.lua):
#     常量 EMAIL_DELTYPE = { DELALL = 0 一键删除, DELSINGLE = 1 单个删除 }
#     sendMessage(_type, _emailid): msg = {type=操作类型, emailid=邮件id}
#       · 调用点: app/mail/MailList.lua:291 单个删除(DELSINGLE, mail_info.id)
#                 app/mail/MailList.lua:456 一键删除(DELALL, 0)
#     recvMessage: `for _,_info in pairs(msg[1])` -> mailList = [tonumber(id)...]
#       ★ msg[1] 必须是**表**(pairs 无"or {}"守卫); 兜底 [1,[[]]] 给的是空表 -> 不崩但列表不刷新
#       → 客户端拿这个 mailList 去更新"还剩下哪些邮件", 空表就等于"全删了"
#     m_errorCode: 0 消息体为空 / 1400 删除邮件ID错误 / 1401 操作类型错误 / 1402 邮件列表为空
# ----------------------------------------------------------------------------
def handle_mail_del(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        dtype = int(req.get('type') or 0)
    except Exception:
        return b'[1401]'                                  # 操作类型错误
    if dtype not in (0, 1):
        return b'[1401]'
    try:
        emailid = int(req.get('emailid') or 0)
    except Exception:
        return b'[1400]'                                  # 删除邮件ID错误
    gone = _mail_deleted()
    if dtype == 1:                                        # 单个删除
        if not any(int(m[0]) == emailid for m in _mail_all()):
            return b'[1400]'                              # 删除邮件ID错误 (不在表里)
        if str(emailid) not in [str(x) for x in gone]:
            gone.append(str(emailid))
    else:                                                 # 一键删除
        # ★ 语义定稿 (2026-09-12): "一键删除" = 清空**当前列表里所有可见邮件**。
        #   理由 (实测过两种方案才定的):
        #     · 已领取的邮件在 1057 之后就已经从 `_mail_alive()` 消失 -> 若限定"只删已领",
        #       领完再点一键删除会得到 1402 "可删列表为空" —— 与用户预期(清空列表)不符。
        #     · 客户端 MailList.lua:456 的调用点就是"清空"语义, 且按钮本身在没有可选邮件时不可点。
        #   所以: 清空所有可见邮件。未领取的也会被清掉 —— 这是**客户端 UI 已经做了前置确认**
        #   的操作 (点了"一键删除"就是同意), 服务端不再二次拦截, 免得又造出"点了没反应"。
        alive = _mail_alive()
        if not alive:
            return b'[1402]'                              # 邮件列表为空
        for m in alive:
            if str(m[0]) not in [str(x) for x in gone]:
                gone.append(str(m[0]))
    save_state()
    # 回包 = 剩下的邮件 id 列表 (与客户端 `mailList` 语义一致)
    #   ★ 包裹层: 客户端 `local statecode,msg = parseMessage(...)` -> msg = resp[1]
    #     然后 `for _,_info in pairs(msg[1])` -> 真正取的是 resp[1][1]
    #     → 必须发 `[1,[left]]`, 发 `[1,left]` 会让 msg[1] = 第一个 id (number) → pairs(number) 崩
    #     (与 v25 §10.1 图鉴 1072 "少包一层" 是同一类坑)
    left = [int(m[0]) for m in _mail_alive()]
    return ('[1,' + json.dumps([left], separators=(',', ':')) + ']').encode('utf-8')
