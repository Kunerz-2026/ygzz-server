# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 评论系统 COMMENT_INFO(1171) / COMMENT_PRAISE(1172) / COMMENT_SEND(1173)
#   ★2026-09-12 (v30 施工轮 3) 新增实装。三条此前全落 config_data.resp_for 兜底 [1,[[]]]。
#
#   客户端契约（逐行取自 client Lua，与装机 client Lua 一致）:
#
#   1171 NetCommentInfo   req{id, type, page}
#        recvMessage: commentNum = tonumber(msg[1]) or 0        -- 评论**总数**(非本页数)
#                     commentList = msg[2]                     -- 本页最多 10 条
#                     每行 7 字段: [commentId, roleName, commentTime, commentContent,
#                                   laudNum, LaudBol, hotBol]
#                     末行 `emit(COMMENT_INFO, commentNum, commentData, 1, isRequestPage)`
#        ⚠⚠ 真机崩溃点（比审计器指纹更关键）: VCommentHome.lua:295
#              LsetString(..., (os.date("%Y.%m.%d", commonData[i].commentTime / 1000)))
#            —— commentTime **必须是非 nil 数字**（毫秒）。给 nil -> `nil / 1000` 直接抛 Lua 错误。
#            （审计器只标了 `pairs(msg[2])`；实际第一杀手是 commentTime/1000。）
#        ⚠ 分页语义: VCommentHome.lua:235 `self.totalPage = math.ceil(commentNum / 10)`
#            —— 第 1 页返回**总数**，后续页按 10 条切。所以不能只回本页条数。
#
#   1172 NetCommentPraise req{commentId, staticId, type}
#        recvMessage **只 emit(COMMENT_PRAISE)，不读任何 msg[N]** -> 成功回 [1,[]] 即可。
#        客户端点完赞在本地点赞数 +1（VCommentHome:setPraiseNum，:427）
#        ⚠ VCommentHome.lua:427 `self.sumComment[_tag].laudNum + 1` —— laudNum 必须是**数字**，
#          回 nil 会在点赞回调里抛错。
#
#   1173 NetSendComment   req{id, type, commentContent}
#        recvMessage 与 1171 同形状，末行 `emit(COMMENT_SEND, commentNum, commentData, 2)`
#        （第 3 参数 =2 让客户端把当前页列表整体重建，见 VCommentHome:refreshGoodList）。
#
#   ★ type 语义（两套编号，必须都认）:
#        查列表: 1=角色评 2=关卡评 3=剧情评
#        发/赞 : 7=角色评 8=关卡评 9=剧情评      （= 查列表 type + 6，见 VCommentHome.lua:380）
#        -> 内部统一归一到 base type (1/2/3)。
#
#   ★ 数据源: T42_PresetComment.json（5 条预设剧情评论，Tpye=3，StaticID 114~118）
#        字段: ID, Tpye, StaticID, CommentContent, ServerId, RoleName, CommentUp
#        这些是**官方自带**的评论，按 item 下发能让剧情/关卡评论页立刻有内容（而不是空白）。
#     玩家自己发的评论持久化在 EQ_STATE['comment']。
import json, time
from state import EQ_STATE, save_state, load_table

from state import table_dir as _table_dir   # ★2026-10-02 (审计 §1.6): 表根不再写死盘符
_DATA = _table_dir()
_PAGE_SIZE = 10                      # 客户端 totalPage = ceil(commentNum / 10)

# ---------- 服务端本地写死的兜底评论作者名（仅用于 1/2 型，官方表只有 3 型） ----------
#   铁律: 不编造会被客户端拿去查表的 id；这些都是纯文本展示字段，安全。
_AUTO_ROLES = ['测试', '路过的道士', '山中客', '小妖甲', '捉妖人', '云游散人']
_AUTO_TEXTS = ['这个关卡有点意思', '第一次打到这里，mark 一下', '有没有大佬带带',
               '据说这里掉好东西', '风景不错', '终于过了，感动']

_PRESET = []        # [{id,type,staticId,content,role,laud,ts}]


def _load_json(name):
    # ★v60 闭环 v58 §10 缺口 #3: 改走 state.load_table —— 按「尾字段 u32 长度」精确剥离,
    #   不再用 c[:c.rfind(']')+1]（长度字段含 0x5D 时会切错 -> 调用方 except -> 静默空表,
    #   实测 L98_AIPriority / T32_Achievement）。护栏: work/_v60_legacy_tail_guard_test.py
    #   ⚠ 必须**逐行复制**: load_table 的结果是**带缓存的同一个对象**, 而旧实现每次返回全新对象。
    #     调用方若原地改行(排序/填字段)会污染全局缓存 -> 这里保持"每次都是新对象"的原语义。
    return [list(_r) for _r in load_table(name, _DATA)]


def _bootstrap():
    global _PRESET
    if _PRESET:
        return
    try:
        t = _load_json('T42_PresetComment.json')
        h = {c: k for k, c in enumerate(t[0])}

        def _g(r, col, d=''):
            return r[h[col]] if col in h and h[col] < len(r) else d

        _base_ts = int(time.time() * 1000) - 3 * 86400 * 1000
        for _i, r in enumerate(t[1:]):
            try:
                _pid = int(_g(r, 'ID', 0) or 0)
                if not _pid:
                    continue
                _PRESET.append({
                    'id': _pid,
                    'type': int(_g(r, 'Tpye', 0) or 0),
                    'staticId': int(_g(r, 'StaticID', 0) or 0),
                    'content': str(_g(r, 'CommentContent', '')),
                    'role': str(_g(r, 'RoleName', '')),
                    'laud': int(_g(r, 'CommentUp', 0) or 0),
                    # ⚠ 毫秒时间戳: 客户端 `os.date("%Y.%m.%d", ts / 1000)`，给秒会显示 1970 年
                    'ts': _base_ts + _i * 3600 * 1000,
                })
            except Exception:
                continue
    except Exception:
        _PRESET = []


def _req(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    return req if isinstance(req, dict) else {}


def _i(v, d=0):
    try:
        return int(v)
    except Exception:
        return d


def _base_type(t):
    """7/8/9 -> 1/2/3 ; 1/2/3 原样 ; 其它 -> 3(剧情)"""
    t = _i(t, 3)
    if t > 6:
        t -= 6
    return t if t in (1, 2, 3) else 3


def _store(create=True):
    """评论持久化容器。⚠ create=False 时**只读**，绝不 setdefault
    —— 否则只读接口(1171)也会凭空建出空键并被 autosave 写进存档。"""
    v = EQ_STATE.get('comment')
    if not isinstance(v, dict):
        if not create:
            return {}
        v = {}
        EQ_STATE['comment'] = v
    return v


def _key(btype, item_id):
    return '%d_%d' % (btype, item_id)


def _laud_override(create=True):
    """官方预设评论(T42)的点赞增量落在这里 —— 不改静态表文件本身。
    键 = commentId(字符串) -> 增量 int。
    ⚠ create=False 时**只读**，绝不 setdefault —— 否则"点赞失败/无参"的调用会凭空建出空键,
       再被服务端 autosave 写进存档（2026-09-12 实测踩到）。"""
    v = EQ_STATE.get('comment_laud_override')
    if not isinstance(v, dict):
        if not create:
            return {}
        v = {}
        EQ_STATE['comment_laud_override'] = v
    return v


# ==================== ★★2026-10-02 (D4=B) 评论真·跨账号 ====================
# 修前 `_rows_for` 只合并 [官方预设] + [**自己档**的 comment] ⇒ 多人世界里**看不到彼此的评论**。
#
# **两条必须解决的形状问题**（不解决就只会多出一堆错行）：
#   ① **commentId 会撞号**：每个账号的自建评论 id 都从 1000 起各自计数 ⇒ A 的第 3 条与 B 的
#      第 3 条是**同一个 id**。展示给客户端时必须**全局唯一**，否则"点赞"会打到别人那条。
#      做法：`display_id = owner_idx * 100000 + cid`，`owner_idx` **从账号 id 本身推出来**
#      （`u0002` -> 2）⇒ 任何一方算出来的都一样，**不依赖"谁排第几"这种会漂的枚举序**。
#      ⚠ 多账号下**自己的行也照样加命名空间** —— 否则同一行在两个人屏幕上 id 不同，点赞就对不上。
#      （单档模式不加，行为逐字节不变。）
#   ② **赞数跨账号**：`praised` 记在**各自**的档里 ⇒ 一条评论收到的赞 =
#      它的基线 `laud` + **有多少个账号的 `praised` 里有它**。
#      这是"派生即唯一真值"：**不新开 world 键**（开了就要双向同步，而 `world` 根本不落盘）。
#      ⚠ 因此点赞**只写自己的 `praised`，绝不写别人的档** —— 与任务 D 那条
#        "只读标量、绝不 bind 到别人"的安全约束一致（`save_state()` 会写**当前上下文**的档）。
# （基数见 accounts.NS_BASE —— 全工程只有那一份；本文件不再自带常量）


def _owner_idx(aid):
    """账号 id -> 命名空间序号（`u0002` -> 2）。推不出来 -> 0（不参与命名空间）。

    ★ 实现委托给 `accounts.owner_ns` —— **全工程跨账号展示 id 只许有一份序号算法**
      （评论用了，妖神榜 1065/1066 也用）。两处各写一份必然漂移，而漂移的后果是
      "同一行在不同人屏幕上 id 不同、点赞/点行对不上"，且**不报错**。
    """
    try:
        from accounts import owner_ns
        return owner_ns(aid)
    except Exception:
        return 0


def _display_id(aid, cid):
    """`(账号, 原始 commentId)` -> 跨账号展示 id。**基数与序号算法都只有一份**（`accounts`）。

    ⚠ 这里原来自己写了个 `# （基数见 accounts.NS_BASE —— 全工程只有那一份；本文件不再自带常量）`。第一版能跑，但等于把"基数"变成第二份真值
      —— 而 `accounts.NS_BASE` 后来因为**与合法 ucode 撞号**被抬到 `10**9`
      （`_fix26b` 的 D1 当场判红）。两处各写一份基数，迟早会出现"评论与怪物的 id 规则不同"
      这种没人能一眼看懂的状态。⇒ 一律 `accounts.ns_id` / `accounts.split_ns_id`。
    """
    try:
        from accounts import ns_id
        return ns_id(aid, _i(cid))
    except Exception:
        return _i(cid)


def _split_display_id(did):
    """`display_id` -> (owner_idx, 原 cid)。没命名空间的 id -> (0, did)。"""
    try:
        from accounts import split_ns_id
        return split_ns_id(did)
    except Exception:
        return 0, _i(did)


def _world_accounts():
    """多账号下返回 `(我的 aid, 全部账号)`；单档/未启用 -> None（调用方走原路径）。

    ⚠ 判据必须是 `ENABLED and REGISTRY is not None`，**不能**只判"结果是不是空列表"
      ——"世界里只有我一个"是**合法**结果，与"根本没启用"是两件事。
    """
    try:
        from accounts import ENABLED, REGISTRY, EQ_STATE as _AS
    except Exception:
        return None
    if not (ENABLED and REGISTRY is not None):
        return None
    try:
        me = getattr(_AS.current(), 'id', None)
    except Exception:
        me = None
    try:
        return me, REGISTRY.all_accounts_full()
    except Exception:
        return None


def _bucket_of(data, btype, item_id):
    """从**账号档的原始 dict** 里取这个 (btype,item_id) 的评论桶（只读，绝不建键）。"""
    cm = (data or {}).get('comment') if isinstance(data, dict) else None
    if not isinstance(cm, dict):
        return {}
    b = cm.get(_key(btype, item_id))
    return b if isinstance(b, dict) else {}


def _rows_for(btype, item_id):
    """合并 [官方预设(仅 type3 且 staticId 命中)] + [各账号的玩家自建] -> 统一行 dict 列表。

    ★2026-10-02 (D4=B): 多账号下别人的评论也并进来，commentId 命名空间化，赞数跨账号求和。
      单档模式（`_world_accounts()` 返回 None）走原路径，行为**逐字节不变**。
    """
    _bootstrap()
    ov = _laud_override(create=False)
    out = []
    if btype == 3:
        for p in _PRESET:
            if p['staticId'] == item_id:
                out.append({'id': p['id'], 'role': p['role'], 'ts': p['ts'],
                            'content': p['content'],
                            'laud': p['laud'] + _i(ov.get(str(p['id']), 0)),   # ★ 叠加点赞增量
                            'mine': False, '_aid': None})

    wa = _world_accounts()
    if wa is None:
        # ---- 单档：原样（身份 = 自己）----
        bucket = _store(create=False).get(_key(btype, item_id)) or {}
        praised = bucket.get('praised') or {} if isinstance(bucket, dict) else {}
        srcs = [(None, bucket if isinstance(bucket, dict) else {}, True)]
    else:
        me_id, accs = wa
        bucket = _store(create=False).get(_key(btype, item_id)) or {}
        praised = bucket.get('praised') or {} if isinstance(bucket, dict) else {}
        srcs = []
        for acc in accs:
            d = acc.data if isinstance(acc.data, dict) else {}
            # ⚠ 自己那一份用**内存里**的桶（`_store`），不要读磁盘副本 —— 内存里可能有
            #   还没 `save_state()` 的改动（刚发完一条评论就是这种情形）。
            srcs.append((acc.id, bucket if acc.id == me_id else _bucket_of(d, btype, item_id),
                         acc.id == me_id))

    for aid, b, is_mine in srcs:
        for cid, c in (b.get('list') or {}).items():
            if not isinstance(c, dict):
                continue
            fallback_role = ('未命名#' + aid) if aid else '测试'
            out.append({'id': (_display_id(aid, cid) if aid else _i(cid)),
                        'role': str(c.get('role') or fallback_role),
                        'ts': _i(c.get('ts', 0)),
                        'content': str(c.get('content', '')),
                        'laud': _i(c.get('laud', 0)),
                        'mine': bool(is_mine), '_aid': aid})

    # ---- 跨账号赞数：基线 laud + "有多少个账号赞过它" ----
    if wa is not None:
        praised_sets = [(b.get('praised') or {}) for (_a, b, _m) in srcs
                        if isinstance(b.get('praised'), dict)]
        my_praised_d = praised if isinstance(praised, dict) else {}
        for r in out:
            if r.get('_aid') is None:
                continue                       # 官方预设：走 `comment_laud_override`，不动
            extra = sum(1 for p in praised_sets if str(r['id']) in p)
            # 自己赞自己那条：`handle_comment_praise` 已经把它加进基线 `laud` 了 ⇒ 减掉自己那份
            if r.get('mine') and str(r['id']) in my_praised_d:
                extra -= 1
            r['laud'] = _i(r['laud']) + max(0, extra)

    for r in out:
        r['laudbol'] = 1 if str(r['id']) in (praised if isinstance(praised, dict) else {}) else 0
    return out



def _pack(rows, page):
    """排序 + 分页 + 组装 7 字段行。返回 (总数, 本页 rows)"""
    rows.sort(key=lambda r: (-int(r.get('laud') or 0), int(r.get('id') or 0)))
    total = len(rows)
    n = max(1, int(page or 1))
    out = []
    for r in rows[(n - 1) * _PAGE_SIZE: n * _PAGE_SIZE]:
        out.append([
            int(r['id']),                 # 1 commentId
            str(r.get('role') or ''),     # 2 roleName
            int(r.get('ts') or 0),        # 3 commentTime  ★ 毫秒非 nil(VCommentHome:295 会 /1000)
            str(r.get('content') or ''),  # 4 commentContent
            int(r.get('laud') or 0),      # 5 laudNum      ★ 数字非 nil(:427 会 +1)
            int(r.get('laudbol') or 0),   # 6 LaudBol      0=可赞 1=已赞
            1 if int(r.get('laud') or 0) >= 3 else 0,   # 7 hotBol 热评
        ])
    return total, out


def _resp(total, rows):
    # [1,[commentNum, commentList]]  (parseMessage: msg=resp[#resp]; msg[1]=num, msg[2]=list)
    return ('[1,' + json.dumps([total, rows], separators=(',', ':'), ensure_ascii=False) + ']').encode('utf-8')


# ============================ 1171 获取评论页面信息 ============================
def handle_comment_info(body_json=None):
    req = _req(body_json)
    item_id = _i(req.get('id'))
    btype = _base_type(req.get('type'))
    page = _i(req.get('page'), 1) or 1
    rows = _rows_for(btype, item_id)
    total, page_rows = _pack(rows, page)
    return _resp(total, page_rows)


# ============================ 1173 发送评论 ============================
def handle_comment_send(body_json=None):
    req = _req(body_json)
    item_id = _i(req.get('id'))
    btype = _base_type(req.get('type'))
    content = str(req.get('commentContent') or '')[:80]      # 客户端输入框 maxLength=80
    if not content.strip():
        return b'[1001]'                                     # 空内容（客户端本机已拦，双保险）
    st = _store()
    k = _key(btype, item_id)
    bucket = st.get(k)
    if not isinstance(bucket, dict):
        bucket = {'list': {}, 'praised': {}, 'next': 1}
        st[k] = bucket
    bucket.setdefault('list', {})
    bucket.setdefault('praised', {})
    # 玩家评论 id 从 **1** 起（`next` 初值 1；官方预设用 215~219，不冲突）。
    # ⚠ 这里原来写"从 1000 起"，与代码不符 —— 实测第一条评论的 id 就是 1。
    #   这个偏差在**多账号**下会放大：`display_id = owner_idx*100000 + cid`，写错基数就会
    #   让人以为命名空间算错了（`_comment_world_test.py` 的 C2 判据就是照实测算的）。
    nid = _i(bucket.get('next'), 1) or 1
    while str(nid) in bucket['list']:
        nid += 1
    bucket['next'] = nid + 1
    bucket['list'][str(nid)] = {
        'role': str(EQ_STATE.get('role_name') or '测试'),
        'ts': int(time.time() * 1000),                       # 毫秒
        'content': content,
        'laud': 0,
    }
    # ★2026-10-02 (成就进度写入方): T32 Type=906「评论次数N」-> 组 1002/级别1(滔滔不绝)
    from handlers_achievement import note_ach
    note_ach(906, 0, 1)
    save_state()
    rows = _rows_for(btype, item_id)
    total, page_rows = _pack(rows, 1)                        # 发完回第 1 页（客户端 type==2 会重建列表）
    return _resp(total, page_rows)


# ============================ 1172 评论点赞 ============================
def handle_comment_praise(body_json=None):
    req = _req(body_json)
    cid = _i(req.get('commentId'))
    item_id = _i(req.get('staticId'))
    btype = _base_type(req.get('type'))
    if not cid:
        return b'[0,{}]'
    # ⚠ 校验全部参数之后才碰状态：_store()/_laud_override() 会建键, 空参数路径绝不能调用
    st = _store()
    k = _key(btype, item_id)
    bucket = st.get(k)
    if not isinstance(bucket, dict):
        bucket = {'list': {}, 'praised': {}, 'next': 1}
        st[k] = bucket
    bucket.setdefault('list', {})
    praised = bucket.setdefault('praised', {})
    if str(cid) in praised:
        return b'[1,[]]'                                     # 已赞过 -> 幂等（客户端本地已 +1，不重复加）
    praised[str(cid)] = int(time.time())
    # ⚠ CT=30 的落点**不在这里**：1172 是"评论点赞"，而任务「排行榜点赞 / 围观大佬」对应的是
    #   1174 RANKING_PRAISE（见本文件 `handle_ranking_praise`）。2026-10-18 真机验证：
    #   在布阵排行榜点赞后只有 1174、没有 1172。
    # ★2026-10-02 (成就进度写入方): T32 Type=907「点赞次数N」-> 组 1002/级别2(深得朕心)
    #   位置在幂等判定的**后面** ⇒ 重复点赞(上面那条 `return b'[1,[]]'`)**不会**重复计数。
    from handlers_achievement import note_ach
    note_ach(907, 0, 1)
    # ★2026-10-02 (D4=B): `commentId` 可能是**别人的**评论（多账号下带 `owner_idx*100000`
    #   命名空间，见 `_display_id`）。那种情况**只记"我赞过"**（就落在自己档的 `praised` 里），
    #   **绝不去写别人的档** —— 赞数由 `_rows_for` 跨账号求和得出（派生即唯一真值）。
    #   去写别人的档会踩 `save_state()` 的路由（它写的是**当前上下文**的档），
    #   那正是任务 D 明令禁止的形态。
    owner_idx, raw_cid = _split_display_id(cid)
    _wa = _world_accounts()
    my_idx = _owner_idx(_wa[0]) if _wa is not None else 0
    if owner_idx and owner_idx != my_idx:
        save_state()
        return b'[1,[]]'
    # 自己的评论（可能带命名空间）-> 用**原始 cid** 去自己的桶里找；官方预设 -> 原路径
    lookup = str(raw_cid) if owner_idx else str(cid)
    if lookup in bucket['list']:
        bucket['list'][lookup]['laud'] = _i(bucket['list'][lookup].get('laud', 0)) + 1
    else:
        # 官方预设评论(T42)的点赞增量单独存, 不改静态表文件
        ov = _laud_override()
        ov[str(cid)] = _i(ov.get(str(cid), 0)) + 1
    save_state()
    return b'[1,[]]'                                         # recvMessage 只 emit，不读字段


# ============================================================
# 同轮补齐的 3 条"只查 statecode、不读任何字段"的协议
#   ⚠ 它们的兜底响应 [1,[[]]] 本来就不会崩（recvMessage 里没有任何 msg[N] 访问），
#     实装它们的价值是：① 让 _audit_netmap 的"未覆盖"归零；
#                   ② 把客户端**上行**的数据落一份到存档，便于日后排查；
#                   ③ 语义正确（不再走"未知消息"兜底）。
# ============================================================

# ---------------- 1067 RANK_SENDATTR 妖神排位:上报怪物属性 ----------------
#   NetRankingSendAttr:sendMessage(_attr) -> req{"data": <任意>}
#   recvMessage: 只判 statecode，**不读任何字段** -> [1,[]]
#   ★ 语义: 客户端把"上阵怪物属性"上报服务器用于排行展示，服务端只存不算。
def handle_rank_send_attr(body_json=None):
    req = _req(body_json)
    data = req.get('data')
    if data is None:
        return b'[0,{}]'
    st = EQ_STATE.setdefault('rank_sendattr', {})
    if not isinstance(st, dict):
        st = {}
        EQ_STATE['rank_sendattr'] = st
    # 只保留最近一次上报（体积可控，不无限增长）
    try:
        st['last'] = data if not isinstance(data, (bytes, bytearray)) else str(data)[:2000]
        st['ts'] = int(time.time())
        st['n'] = _i(st.get('n', 0)) + 1
    except Exception:
        pass
    save_state()
    return b'[1,[]]'


# ---------------- 1174 RANKING_PRAISE 排行榜点赞 ----------------
#   NetRankingPraise:sendMessage(_staticId, _type) -> req{"staticId","type"}
#   recvMessage: 只 emit(RANKING_PRAISE) 后 UI 自刷，**不读任何字段** -> [1,[]]
#   ★ 语义: 给别人点赞; 私服没有其他玩家, 但要按"每人每榜只能赞一次"做幂等,
#     否则客户端反复点会让本地计数虚增（UI 侧自行 +1）。
def handle_ranking_praise(body_json=None):
    req = _req(body_json)
    sid = _i(req.get('staticId'))
    rtype = _i(req.get('type'))
    if not sid:
        return b'[0,{}]'
    st = EQ_STATE.setdefault('rank_praise', {})
    if not isinstance(st, dict):
        st = {}
        EQ_STATE['rank_praise'] = st
    k = '%d_%d' % (rtype, sid)
    if k in st:
        return b'[1,[]]'                                     # 已赞过 -> 幂等, 不重复计
    st[k] = int(time.time())
    # ★★2026-10-18（任务链收口 · 修正落点）：CT=30「排行榜点赞 / 围观大佬」的事件源是
    #   **1174 RANKING_PRAISE**（`NetRankingPraise.lua`，排行榜列表里那个大拇指），
    #   **不是** 1172 评论点赞（那是评论页的）。上一版接在 1172 上，真机在布阵排行榜点
    #   赞后 `a07_ct['30']` 纹丝不动 —— 因为客户端根本没发 1172。
    #   口径：首次点赞记 1（上面 `if k in st: return` 已挡重复）。
    try:
        from handlers_task import bump_task as _bt30, CT_PRAISE as _CT_PRAISE
        _bt30(_CT_PRAISE, 1)
    except Exception as _e:
        print('[PRAISE] !! bump CT=30 失败(不影响点赞本身): %r' % (_e,), flush=True)
    save_state()
    return b'[1,[]]'


# ---------------- 1607 RESULT_VERIFICATION 战斗结果校验 ----------------
#   NetWarVerification:sendMessage() 发**空包** req{}
#   recvMessage: 只判 statecode，**不读任何字段** -> [1,[]]
#   ★ 语义: 官方用于把战斗结果的关键数据回传服务器做**反作弊校验**（客户端不回包则视为通过）。
#     私服无校验需求, 只做"收到并计数", 不回 0/错误码（避免客户端误报"数据异常"）。
def handle_war_verification(body_json=None):
    st = EQ_STATE.setdefault('war_verify', {})
    if not isinstance(st, dict):
        st = {}
        EQ_STATE['war_verify'] = st
    st['n'] = _i(st.get('n', 0)) + 1
    st['ts'] = int(time.time())
    save_state()
    return b'[1,[]]'
