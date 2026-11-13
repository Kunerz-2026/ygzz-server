# -*- coding: utf-8 -*-
"""monster_identity.py —— 妖怪的"身份卡"(只读计算), 供 /admin 后台展示。

为什么需要它 (见 work/交接文档_v35_同角色多形态与发放校验缺口.md):
  用户报"邮件发了两个妖怪天师" —— 实际是**同一个角色的两个形态**(夜庭柯 201406 / 妖神夜庭柯 101306,
  两者 L91_StatusRender.Detail 逐字相同)被连续发了两个。`/admin` 的 type=4 搜索只显示
  `Type=/Quality=/Star=`, 运营**看不出**"我要发的这只跟包里那只其实是同一个角色"。
  本模块把**判断需要的信息**算出来摆在界面上 —— 但**不做任何拦截**:
  "该不该发未投放的怪"是运营口径, 不该由代码替用户决定 (v35 §3.1)。

本模块的定位与约束:
  · **只读**: 只读表(L91_Status / L91_StatusRender / L81_CP)与内存态 (state.EQ_STATE['monsters']),
    不写任何东西、不 import game_server (坑点 #75)。
  · **不固化任何设计语义**: 表格是懒加载 + 落盘缓存 (mtime 变了自动重建), 代码里没有
    "哪些 id 是玩家怪"的硬编码名单 —— 因为 v35 已证明那类判据(Type/UnitType/配方/羁绊)
    **没有一个是可靠的**, 写了就是把错误固化成期望值 (v33 坑点 #84)。
  · 所有字段都带 `_why`, 便于以后查"这个结论是拿哪一列算的"。

对外:
  describe(static_id) -> dict            单个 id 的身份卡
  annotate(item, box) -> dict            给 /admin/api/tables 的一行补 tag/warn
  box_index() -> {'by_static':.., 'by_detail':..}   玩家包里现有妖怪的索引
  char_key(static_id) -> str|None        同角色键 (优先用"详情文案逐字相同", 退化用 Name)
"""
import json
import os
import threading

import state as st

_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_PATH = os.path.join(_DIR, 'monster_identity_cache.json')
_LOCK = threading.RLock()
_MEM = {'mtime': None, 'data': None}


# ---------------------------------------------------------------------------
# 表索引 (静态, 与存档无关)
# ---------------------------------------------------------------------------
def _tables():
    """-> (L91 索引, Detail 索引)。带 mtime 的内存缓存 + 落盘缓存。"""
    p = os.path.join(st.table_dir(), 'L91_Status.json')
    try:
        mt = os.path.getmtime(p)
    except Exception:
        mt = None
    with _LOCK:
        if _MEM['mtime'] == mt and _MEM['data'] is not None:
            return _MEM['data']
    data = _build()
    with _LOCK:
        _MEM['mtime'] = mt
        _MEM['data'] = data
    try:                                    # 落盘缓存: 纯属加速, 坏了不影响正确性
        json.dump({'mtime': mt, 'data': data}, open(_CACHE_PATH, 'w', encoding='utf-8'),
                  ensure_ascii=False)
    except Exception:
        pass
    return data


def _build():
    rows = st.load_table('L91_Status.json')
    H = rows[0]
    I = {c: k for k, c in enumerate(H)}

    def g(r, c, d=''):
        k = I.get(c)
        return str(r[k]) if k is not None and k < len(r) and r[k] is not None else d

    by_id = {}
    for r in rows[1:]:
        sid = g(r, 'ID')
        if not sid:
            continue
        by_id[sid] = {
            'id': sid, 'name': g(r, 'Name'), 'type': g(r, 'Type'),
            'star': g(r, 'Star'), 'unit_type': g(r, 'UnitType'),
            'faction': g(r, 'Type'), 'cpid': g(r, 'CPID'),
            'src': g(r, 'SourceID') or sid, 'adv': g(r, 'AdvanceID'),
            'skin': g(r, 'Skin'), 'exp_ratio': g(r, 'ExpRatio'),
        }

    # 链 (SourceID -> 成员 id, 按 id 排序)
    chains = {}
    for sid, d in by_id.items():
        chains.setdefault(d['src'], []).append(sid)
    for k in chains:
        chains[k].sort()

    # 同角色键: L91_StatusRender.Detail 逐字相同 => 同一个角色 (强判据)
    detail = {}
    try:
        rrows = st.load_table('L91_StatusRender.json')
        RH = rrows[0]
        RI = {c: k for k, c in enumerate(RH)}
        di = RI.get('Detail')
        ii = RI.get('ID')
        if di is not None and ii is not None:
            for r in rrows[1:]:
                d = str(r[di]) if di < len(r) and r[di] is not None else ''
                sid = str(r[ii]) if ii < len(r) and r[ii] is not None else ''
                if sid and d and d != '0':
                    detail.setdefault(d, []).append(sid)
    except Exception:
        pass

    # 羁绊 (L81_CP, CPType=2): 成员是"链首 id"
    fetters = {}
    try:
        cp = st.load_table('L81_CP.json')
        CH = cp[0]
        CI = {c: k for k, c in enumerate(CH)}
        for r in cp[1:]:
            if str(r[CI['CPType']]) != '2':
                continue
            fid = str(r[CI['ID']])
            nm = str(r[CI['CPName']]) if 'CPName' in CI else fid
            for m in str(r[CI['CPID']]).split('|'):
                m = m.strip()
                if m:
                    fetters.setdefault(m, []).append({'id': fid, 'name': nm})
    except Exception:
        pass

    by_detail = {}
    for d, ids in detail.items():
        for sid in ids:
            by_detail[sid] = sorted(ids)

    # ★ "还能不能突破"的**权威判据不是 L91.AdvanceID, 而是 L04_Evolution 里有没有这一阶的配方**。
    #   实测反例: 100005 的 L91.AdvanceID == 0, 但 L04 里 ID=1004/CardId=100005 有完整配方
    #   (3023x20 + 3024x20 + 石敢当六阶x1 + 1000 铜钱) —— 也就是**真能突破到 6 阶**。
    #   handlers_game._EVOL_ROWS 用的正是 L04, 所以这里跟它对齐。
    evolvable = set()
    try:
        ev = st.load_table('L04_Evolution.json')
        EH = ev[0]
        EI = {c: k for k, c in enumerate(EH)}
        for r in ev[1:]:
            try:
                evolvable.add(str(r[EI['CardId']]))
            except Exception:
                continue
    except Exception:
        pass

    return {'by_id': by_id, 'chains': chains, 'by_detail': by_detail, 'fetters': fetters,
            'detail_len': {sid: len(v) for sid, v in by_detail.items()},
            'evolvable': evolvable}


# ---------------------------------------------------------------------------
# 玩家包索引 (动态, 每次调用都从 EQ_STATE 现算 —— 后台改完立刻反映)
# ---------------------------------------------------------------------------
def box_index():
    """-> {'by_static': {static_id: [ucode,..]}, 'by_detail': {key: [(ucode,id),..]}, 'count': N}"""
    T = _tables()
    by_static, by_detail = {}, {}
    for uc, row in (st.EQ_STATE.get('monsters') or {}).items():
        try:
            sid = str(row[0])
        except Exception:
            continue
        by_static.setdefault(sid, []).append(str(uc))
        k = _ckey(sid, T)
        if k:
            by_detail.setdefault(k, []).append((str(uc), sid))
    return {'by_static': by_static, 'by_detail': by_detail,
            'count': len(st.EQ_STATE.get('monsters') or {})}


def _ckey(sid, T):
    """同角色键: 有 Detail 就用 Detail 文案本身; 没有就退化用 Name。"""
    d = T['by_detail'].get(str(sid))
    if d and len(d) >= 1:
        return 'D:' + ','.join(d)
    row = T['by_id'].get(str(sid))
    return 'N:' + row['name'] if row and row['name'] else None


def char_key(static_id):
    T = _tables()
    return _ckey(static_id, T)


def describe(static_id):
    """单个 id 的身份卡 (纯函数, 只读表)。未知 id -> {'known': False}"""
    sid = str(static_id)
    T = _tables()
    row = T['by_id'].get(sid)
    box = box_index()
    if not row:
        return {'known': False, 'id': sid}

    chain = T['chains'].get(row['src'], [sid])
    names = []
    for c in chain:
        cr = T['by_id'].get(c)
        names.append({'id': c, 'name': cr['name'] if cr else '?',
                      'star': cr['star'] if cr else '?',
                      'final': bool(cr and str(cr['adv']) == '0')})

    # 同角色 (Detail 文案逐字相同) 的**别的进化链** —— 按链去重, 因为玩家看到的是"形态"而不是"哪一阶 id"
    same_ids_all = T['by_detail'].get(sid, [])
    same_chains = {}
    for x in same_ids_all:
        xr = T['by_id'].get(x)
        if not xr or xr['src'] == row['src']:
            continue
        same_chains.setdefault(xr['src'], xr['name'])
    same_in_box = []
    for csrc, cname in same_chains.items():
        for x in T['chains'].get(csrc, []):
            for uc in box['by_static'].get(x, []):
                same_in_box.append({'ucode': uc, 'id': x, 'chain_id': csrc, 'name': cname,
                                    'lv': (st.EQ_STATE.get('monster_lv') or {}).get(uc)})

    fet = T['fetters'].get(row['src'], [])
    in_box = box['by_static'].get(sid, [])
    # ★v40 (P1): 静态特性来源 (CN L91 / YGTW 1.25 / 无)
    _ft1, _ft2, _fsrc = feat_source_of(sid)

    # 链首/同链已存在
    chain_in_box = []
    for c in chain:
        for uc in box['by_static'].get(c, []):
            chain_in_box.append({'ucode': uc, 'id': c,
                                 'name': (T['by_id'].get(c) or {}).get('name', '?')})

    # 同一个角色在包里出现了几只 (含自己)
    same_key = _ckey(sid, T)
    same_all = box['by_detail'].get(same_key, []) if same_key else []

    return {
        'known': True,
        'id': sid,
        'name': row['name'],
        'faction': row['faction'],          # L91.Type, 官方注释=阵营
        'unit_type': row['unit_type'],      # L91.UnitType, 官方注释=类型 (切勿当"玩家怪"判据)
        'star': row['star'],
        'chain_id': row['src'],
        'chain': names,
        'chain_len': len(chain),
        'is_chain_head': row['src'] == sid,
        'is_final': str(row['adv']) == '0',
        'next_id': row['adv'] if str(row['adv']) != '0' else None,
        'fetters': fet,                      # L81_CP 里以本链链首为成员的所有羁绊
        'in_box': in_box,                    # 这个静态 id 已经在包里的 ucode 列表
        'chain_in_box': chain_in_box,        # 本链任意阶段在包里的
        'same_character_ids': same_ids_all,      # 详情文案逐字相同的所有 id (含本链)
        'same_character_chains': same_chains,    # {链首id: 该链名字} —— 排除自己这条链
        'same_character_in_box': same_in_box,
        'same_character_count_in_box': len(same_all),
        'feat_static': [_ft1, _ft2],         # ★v40 静态特性 id (0,0 = 无)
        'feat_source': 'state.feat_source_of(): primary table first, optional legacy fallback',
        '_why': {
            'chain': 'L91_Status.SourceID 相同的所有 id (官方注释: 原始ID)',
            'same_character': 'L91_StatusRender.Detail 逐字相同的 id (强判据); 按 SourceID 分组成"形态"',
            'fetters': 'L81_CP 里 CPType=2 且成员含本链链首的行',
            'in_box': "state.EQ_STATE['monsters'] 里 row[0] == 本 id",
            'feat_source': 'state.feat_source_of(): CN 1.31 L91.Tallent1/2 优先, 全 0 时回退 '
                           'legacy tables/L91_Status.json (台服 1.25 的逐怪分配)',
        },
    }


def base_of(static_id):
    """返回该 id 所在进化链的**链首(1 阶) id**; 未知 id 返回 None。

    ★v35 §3.3: 用户报"邮件发妖怪有问题"的根因 —— 后台直发了 6 阶形态。
      正常玩法只会发链首(1 阶), 靠进化往上走。本函数给后台的"自动降阶"开关用。
      判据: L91_Status.SourceID 相同的所有 id 按数值排序, 第一个就是链首。
      (实测这条链上 Star == AdvanceLevel, 链首即 Star=1 那一行。)
    """
    T = _tables()
    row = T['by_id'].get(str(static_id))
    if not row:
        return None
    chain = T['chains'].get(row['src'])
    return chain[0] if chain else None


def stage_of(static_id):
    """返回 (第几阶, 共几阶); 未知返回 (0, 0)。"""
    T = _tables()
    row = T['by_id'].get(str(static_id))
    if not row:
        return 0, 0
    chain = T['chains'].get(row['src']) or []
    try:
        return chain.index(str(static_id)) + 1, len(chain)
    except ValueError:
        return 0, len(chain)


def max_stage_of(static_id):
    """这条链**客户端可达**的最高阶（沿 `L91.AdvanceID` 从链首走；5 阶行 AdvanceID=0 -> 5）。

    ★2026-11-07 修正（6 阶收口）：
      修前按 L04_Evolution 是否有配方推“6 阶可达”，但**客户端进化界面以 `L91.AdvanceID`
      为门控**（`CultivateEvolution.lua:212/275`），而 5 阶行的 AdvanceID 在
      1.28 / 1.28g / 1.29v / 1.30 装机 / 1.31 五套表里**全是 0**；服务端 6003 也在
      `AdvanceID==0` 时回 `[78]`。⇒ L04 里 5→6 的配方是**不可达数据**，不能拿来推
      “玩家能到 6 阶”。6 阶只能从后台/邮件直接发进来，而直发会出现属性空白/队伍错位。
    """
    T = _tables()
    row = T['by_id'].get(str(static_id))
    if not row:
        return 0
    chain = T['chains'].get(row['src']) or []
    if not chain:
        return 0
    sid = chain[0]
    seen = set()
    n = 0
    while sid and sid not in seen and n <= len(chain) + 1:
        seen.add(sid)
        n += 1
        nxt = (T['by_id'].get(sid) or {}).get('adv', '0')
        if str(nxt) in ('', '0', 'None'):
            break
        sid = str(nxt)
    return n


def is_final_stage(static_id):
    """客户端是否把这一阶当“末阶”（`L91.AdvanceID == 0`）。

    ★2026-11-07 修正：修前注释说“不要用 AdvanceID 判，5 阶在 L04 有配方”，但客户端
    进化界面正是用 AdvanceID 门控、服务端 6003 也在 AdvanceID==0 时回 `[78]`；
    L04 的 5→6 配方不可达。所以对“玩家能不能继续进化”来说，AdvanceID 才是权威。
    """
    T = _tables()
    row = T['by_id'].get(str(static_id))
    return bool(row) and str(row.get('adv')) in ('', '0', 'None')


def normalize_attachment(item):
    """把一条邮件附件 [type,id,cnt] 归一化 -> (新附件, 说明 or '')。

    只处理 **type=4(妖怪)**: 若目标是 ≥2 阶形态, 换成链首(1 阶)。
    其余类型原样返回。**这是"自动降阶"开关的唯一实现** (admin_backend 调用)。
    """
    try:
        t, i, c = int(item[0]), int(item[1]), int(item[2])
    except Exception:
        return item, ''
    if t != 4:
        return item, ''
    b = base_of(i)
    if b is None or str(b) == str(i):
        return item, ''
    idx, n = stage_of(i)
    T = _tables()
    nm_old = (T['by_id'].get(str(i)) or {}).get('name', '?')
    nm_new = (T['by_id'].get(str(b)) or {}).get('name', '?')
    return [t, int(b), c], ('妖怪 %s(%s) 是第 %d/%d 阶 -> 已自动降为链首 %s(%s)'
                            % (i, nm_old, idx, n, b, nm_new))


def is_chain_head(static_id):
    """是不是这条进化链的**链首(1 阶)**。

    ★v36（用户要求）：后台/邮件的妖怪搜索**只列链首**。
      用户原话："我的皮肤是指妖怪发放的穿着皮肤的单独怪" ——
      也就是 `妖神夜庭柯` / `大天师` / `天师-暮成雪` 这类**在 L91 里是独立怪、但在游戏里
      等于"同一个角色穿着另一套皮肤"的形态**。它们的共同特征就是 **SourceID 不等于自己**。
      · 正常产出（抽卡池 82 只）**全是链首**（阶分布只有 01/11/21/31/51）；
      · 高阶形态只能靠 L04 一级级突破 → 后台没必要让人手选这些。
    """
    T = _tables()
    row = T['by_id'].get(str(static_id))
    return bool(row) and str(row.get('src')) == str(static_id)


def chain_head_of(static_id):
    """链首 id (等价于 base_of, 语义更直白)。"""
    return base_of(static_id)


# ---------------------------------------------------------------------------
# ★v37: 怪物皮肤(L98_Monsterskin)的"属性加成"判定 —— 后台发皮肤时用
# ---------------------------------------------------------------------------
# 用户 2026-09-13 要求：「皮肤的话只把有属性加成的皮肤写进后台能发的吧」。
# 为什么这条判据必要 (实测):
#   L98_Monsterskin 共 **434 个皮肤, 其中只有 22 个带数值加成**; 其余全是纯外观。
#   而用户之前邮件发到的 `11301/11302/11303 哪咤`(3 个)、`11001 天师-暮成雪`、`50001 大天师`
#   的加成列 **全是 0** —— 也就是"发了只是换张皮", 这与他"只是皮肤出现, 可能有未知 bug"的观察一致。
#   ⚠ 注意区分两类东西:
#     · **皮肤 id**(L98 的行, 走附件类型 14) —— 本函数判定的对象;
#     · **皮肤形态怪**(L91 里独立的另一只怪, 如 101306 妖神夜庭柯) —— 另一类, 见 is_chain_head()。
#     有意思的是: `20142 妖神夜庭柯` **这个皮肤**是带加成的 (CrtDmgValue=10)。
# ---------------------------------------------------------------------------
# 数值型加成列 (单位见 L98 表: Atk/Def/HP 是百分比, Crt 是暴击强度, 各 Boost/Rate 是百分比/千分比)
_SKIN_BONUS_COLS = ('AtkValue', 'DefValue', 'HPValue', 'CrtValue', 'CrtDmgValue',
                    'BurnBoost', 'PoisonBoost', 'HealBoost', 'VampireBoost', 'DrainBoost',
                    'FreezeRate', 'KnocOutRate', 'FetterRate', 'HypnotizeRate', 'ExpBoostRatio')
# 布尔型特殊能力列 (0/1 或文字; 非 0 即"有特殊能力", 也算有加成)
_SKIN_FLAG_COLS = ('PoisonImmune', 'BurnImmune', 'FreezeImmune', 'KnockOutImmune', 'FetterImmune',
                   'HypnotizeImmune', 'PushOffImmune', 'RestrainHuman', 'RestrainGhost',
                   'RestrainMonster', 'RestrainSpirit', 'RestrainElf', 'SkipObstacle',
                   'Invisable', 'AvoidObstacle', 'Hiker')


def _skin_index():
    """L98_Monsterskin -> {str(id): row_dict}。带 mtime 缓存 (皮肤表基本不变)。"""
    p = os.path.join(st.table_dir(), 'L98_Monsterskin.json')
    try:
        mt = os.path.getmtime(p)
    except Exception:
        mt = None
    with _LOCK:
        c = _MEM.get('skin')
        if c and c[0] == mt:
            return c[1]
    out = {}
    try:
        rows = st.load_table('L98_Monsterskin.json')
        H = [str(x) for x in rows[0]]
        I = {c: k for k, c in enumerate(H)}
        for r in rows[1:]:
            out[str(r[I['ID']])] = {c: r[I[c]] for c in H if c in I}
    except Exception:
        out = {}
    with _LOCK:
        _MEM['skin'] = (mt, out)
    return out


def _f(v):
    try:
        return float(str(v))
    except Exception:
        return 0.0


def is_skin_id(static_id):
    """这个 id 是不是**皮肤表(L98_Monsterskin)里的一行**。

    ★v37 为什么必须有这一步: L91(怪) 与 L98(皮肤) 的 id **存在同号撞车** ——
      实测 `10022` 既是怪 `阴主-三山` 的一阶, 又是皮肤 `阴磷阎罗`;
      `10032` 既是怪 `阳主-芝罘` 的一阶, 又是皮肤 `青溪避世-芝罘`。
      不先卡"它在 L98 里", 就会把**怪**当成皮肤放行 (后台列表里就会出现一堆"表外 id · 用于链…")。
    """
    return str(static_id) in _skin_index()


def skin_bonus(static_id):
    """皮肤 id(L98 行) 的属性加成摘要 -> (有加成: bool, 人类可读串: str)。

    没有这个皮肤 id / 表读不到 -> (False, '') —— **宁可不显示也不误报**。
    """
    row = _skin_index().get(str(static_id))
    if not row:
        return False, ''
    parts = []
    for c in _SKIN_BONUS_COLS:
        v = _f(row.get(c))
        if v:
            parts.append('%s=%s' % (c, ('%g' % v)))
    for c in _SKIN_FLAG_COLS:
        v = str(row.get(c) or '0').strip()
        if v not in ('0', '', '0.0'):
            parts.append('%s=%s' % (c, v))
    if not parts:
        return False, ''
    return True, ' '.join(parts)


def is_bonus_skin(static_id):
    """这个皮肤(L98 id) 带不带属性加成 —— 后台"只发带加成的皮肤"的判据。"""
    return skin_bonus(static_id)[0]


def skin_target_chain(static_id):
    """皮肤对应的怪(链首 id) —— L98.MonsterID 是 'id|id|...' 阶段序列, 取第一个。

    用途: 后台列表里告诉运营"这个皮肤是给哪只怪用的"。
    """
    row = _skin_index().get(str(static_id))
    if not row:
        return ''
    first = str(row.get('MonsterID') or '').split('|')[0].strip()
    if not first:
        return ''
    T = _tables()
    r = T['by_id'].get(first)
    if r:                                  # 已经是链首就返回它自己, 否则回链首
        return str(r['src'])
    return first


def feat_source_of(static_id):
    """该静态 id 的**静态特性来源**。-> (t1, t2, source)

    source:
      'CN L91'   —— CN 1.31 L91_Status 的 Tallent1/2 有值 (实测: 966 行**全为 0**, 见下)
      'YGTW 1.25'—— CN 全 0 时回退的台服老版逐怪分配 (247 行 / 39 个名字带特性)
      None       —— **无静态特性来源** (该怪在数据层面就没有特性可发)

    ★v40 (P1) 为什么要把它摆到运营面前:
      实测 CN 1.28 / 1.28g / 1.29v / 1.31 / 目标客户端 **五个版本的 L91 Tallent1/2 全部为 0**,
      而台服 YGTW 1.25 只有 247 行(39 个名字)有分配 -> 按 id 只能覆盖 CN 966 行里的 247 行。
      剩下的怪**不是我们漏发了, 而是数据边界**:
        · 玩家最常持有的"6 位 id 角色卡"(540 行)里, 大量怪根本没有静态特性;
        · 按名字回退**经实测不成立** (见 _audit_feat_name_fallback*.py: 名字能对上的 51 行
          **全部是 HeroType=2 副本怪物**, GrowMax=0/CPID=0, 与同名的角色卡不是同一实体)。
      ⇒ 正确做法是"标注清楚", **不是替它编一个特性 id**
        (铁律 #2: 编造 id -> 客户端查表 nil 索引 -> 原生层 SIGSEGV)。
    """
    try:
        info = st._l91_info().get(int(static_id))
    except Exception:
        info = None
    if info:
        try:
            t1 = int(info[0] or 0)
            t2 = int(info[1] or 0)
            if t1 or t2:
                return (t1, t2, 'CN L91')
        except Exception:
            pass
    try:
        y1, y2 = st.ygtw_tallents_mid(int(static_id))
        if y1 or y2:
            return (int(y1), int(y2), 'YGTW 1.25')
    except Exception:
        pass
    return (0, 0, None)


def feat_source_coverage():
    """全表口径的"特性来源覆盖"统计 (给后台概览用)。"""
    T = _tables()
    from collections import Counter
    c = Counter()
    per_name = {}
    for sid in T['by_id']:
        _t1, _t2, src = feat_source_of(sid)
        c[src or '无来源'] += 1
        if src:
            per_name.setdefault(T['by_id'][sid]['name'], set()).add(src)
    return {
        'rows': sum(c.values()),
        'by_source': dict(c),
        'names_with_source': len(per_name),
        'names_total': len(set(r['name'] for r in T['by_id'].values())),
        'note': 'CN 1.28/1.28g/1.29v/1.31/目标客户端 的 L91 Tallent1/2 实测全为 0; '
                'YGTW 1.25 只有 247 行/39 个名字有逐怪分配。无来源 = 数据边界, 不是漏发。',
    }


def _warn(row, in_box, same_in_box, same_all, sid):
    w = []
    # ★v40 (P1): 静态特性来源标注 —— 让运营一眼看出"这是数据边界, 不是我忘了发"。
    #   放在最前面, 因为它是"这只怪该不该发/发了会怎样"的第一手信息。
    try:
        _t1, _t2, _fsrc = feat_source_of(sid)
        if not _fsrc:
            w.append('· **无静态特性来源**: 该怪在 CN 1.31 的 L91 Tallent1/2 为 0, 台服 YGTW 1.25 '
                     '也没有它的逐怪分配 —— 通过任何正常/后台途径获得它, 特性栏都会是空的。'
                     '这是**数据边界**, 不是漏发; **不要为它编特性 id**'
                     '(铁律 #2: 编造 id -> 客户端查表 nil 索引 -> 原生层 SIGSEGV)。')
    except Exception:
        pass
    # ★ v35 (2026-09-13, 用户报"邮件发妖怪有问题 —— 属性空白/队伍界面错位") 的头号判据:
    #   L91 里**同一条链的每个成员就是"一阶"**, 且实测 Star == AdvanceLevel
    #   (天师-暮成雪 100001..100006 = 1..6 阶/1..6 星)。**正常玩法只会发链首(1 阶)**,
    #   靠进化往上走; 后台/邮件直接发末阶 = 给客户端一只"跳过整条进化链"的怪。
    #   实测: 用户包里 4 只来路异常的怪(100006/500006/101306/201406)**全部是 6 阶**,
    #   而正常获得的 哪吒(4 阶)/月主(4 阶)/阴主(4 阶) 全部正常。
    #   这里**只提示不拦截** —— "我就想发满阶的怪来测试"是合理需求。
    try:
        T = _tables()
        chain = T['chains'].get(str((row or {}).get('src')), [])
        if chain and sid in chain:
            idx = chain.index(sid) + 1
            try:
                _star = int((row or {}).get('star') or 0)
            except (TypeError, ValueError):
                _star = 0
            if idx >= 6 or _star >= 6:
                w.append('⚠ 第 6 阶（Star=%s）：**客户端不可达**（5 阶 AdvanceID=0，正常玩法最高 5 阶）。'
                         '直接发会出现属性空白/队伍错位（v35 用户实测）；后台/邮件现已默认拒绝。'
                         '请改发链首 %s 或 1–5 阶。'
                         % ((row or {}).get('star'), chain[0]))
            elif idx >= 5:
                w.append('· 第 %d/%d 阶（Star=%s）：**客户端最高阶**（AdvanceID=0，不能继续进化）；'
                         '正常发放链首 %s。'
                         % (idx, len(chain), (row or {}).get('star'), chain[0]))
            elif idx > 1:
                w.append('· 第 %d/%d 阶（正常发放的是链首 %s）' % (idx, len(chain), chain[0]))
    except Exception:
        pass
    # ★ 占位/测试行: L91 里 ID=99999999 的行 Name 就是 "Dummy"。
    #   它**能通过** /admin 的 id ∈ L91 校验, 发出去客户端会拿到一只叫 Dummy 的怪 —— 纯看名字就该拦一下。
    nm = (row or {}).get('name') or ''
    if nm.strip().lower() in ('dummy', 'test', '测试') or nm.strip() == '':
        w.append('⚠ 这一行的名字是「%s」, 像是**占位/测试行**, 不是正式妖怪' % (nm or '(空)'))
    if in_box:
        w.append('包里已经有这只(ucode %s), 再发一只就是第二只同 id' % '/'.join(in_box))
    if same_in_box:
        # 按"形态(进化链)"去重, 并排除与本行同链的 (那是"同一只的另一阶", 不是同角色的别的形态)
        seen = set()
        forms = []
        for x in same_in_box:
            if x.get('chain_id') in seen:
                continue
            seen.add(x.get('chain_id'))
            forms.append('%s「%s」' % (x['id'], x['name']))
        if forms:
            w.append('⚠ 与包里 %s 是**同一个角色的不同形态**(详情文案逐字相同), 不是同一只怪'
                     % '/'.join(forms))
    if len(same_all) > 1:
        w.append('发完之后, 这个角色在包里会有 %d 个形态' % len(same_all))
    return w


def annotate(item, box=None):
    """给 /admin/api/tables 的一行补 tag/warn (只对 type=4 调用)。

    item: {'id':int, 'name':str, 'extra':str}
    返回同一个 dict (就地补字段), 便于 UI 直接渲染。
    """
    d = describe(item.get('id'))
    if not d.get('known'):
        item['tag'] = '表外 id'
        item['warn'] = ['L91_Status 里没有这个 id']
        return item
    segs = ['链%s(%d阶)' % (d['chain_id'], d['chain_len'])]
    if d['is_chain_head']:
        segs.append('链首')
    if d['is_final']:
        segs.append('末阶')
    if d['fetters']:
        segs.append('羁绊:' + ','.join(f['name'] for f in d['fetters'][:2]))
    else:
        segs.append('无羁绊')
    if d['in_box']:
        segs.append('★已在包中(ucode %s)' % '/'.join(d['in_box']))
    # ★v40 (P1): 特性来源进 tag —— 列表页扫一眼就能看出"这只怪发出去有没有特性"
    if d.get('feat_source'):
        segs.append('特性:%s(%s)' % ('+'.join(str(x) for x in d.get('feat_static') or [] if x),
                                    d['feat_source']))
    else:
        segs.append('⚠无静态特性来源')
    item['tag'] = ' · '.join(segs)
    item['warn'] = d['warn']
    item['same_character_in_box'] = d['same_character_in_box']
    item['feat_source'] = d.get('feat_source')
    return item
