# -*- coding: utf-8 -*-
"""
admin_backend.py —— 私服网页后台 (挂在 game_server 已有的 18080 HTTP 端口上的 /admin)

设计取舍 (为什么是"进程内 HTTP 页面"而不是独立进程/独立端口的工具):
  ① 服务端把 EQ_STATE 放在**内存**里, 外部进程直接改 state.json 会被它下一次 save_state 覆盖。
     挂在同一个进程里才能既读到真状态、又改得动、还能 save_state() 落盘。
  ② 零新增依赖: 复用已有的 ThreadingHTTPServer, 只用标准库。
  ③ 能给**在线客户端**主动推送 -> 发完邮件玩家立刻看到, 不用重登。

为什么不 import game_server:
  坑点 #75 —— `import game_server` 会**启动整个服务器**(模块级绑定三端口进 accept 循环)。
  所以 game_server 在启动时调用 configure(...) 把需要的引用**注入**进来 (反向依赖)。

安全:
  · 必须带 token (work/admin_token.txt, 首次启动自动生成 32 位十六进制)。
    token 可用 `?t=...` 或 `X-Admin-Token` 头传; /admin 页面本身也要求带 token 才返回。
  · 18080 绑的是 0.0.0.0(局域网可达), 所以 token 不是可选项。
  · 所有写操作都做**参数校验 + 静态表校验** (见 handlers_sys.validate_mail_item) —— 这是防
    铁律 #2 (客户端拿编造 id 去查表 -> nil 索引 -> 原生层 SIGSEGV) 的唯一关口。
"""
import json
import os
import time

import handlers_sys as hs
import state as st

_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'admin_token.txt')
_UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'admin_ui.html')

# 由 game_server 在启动时注入 (见 configure)
# ⚠ `push_all` 这个**键名不许删/不许改名**: `handlers_sign.py:530` 与
#   `_v60_sdk_buy_test.py:61` 都按这个名字取用它。多账号下我们**不再用它**推私有载荷
#   (见 `_push_scope`), 但键必须留着。
DEP = {
    'push_all': None,        # push_all(msgid, payload_bytes) -> 推给所有在线连接, 返回条数
    'push_account': None,    # ★2026-10-02: push_account(aid, msgid, payload) -> 只推该账号
    'live_count': None,      # () -> 在线连接数
    'started_at': None,      # 服务器启动时间戳
    'base_dir': None,
}

# ★2026-10-02 (D5=B): 所有 `/admin/api/*` 必须显式带 `acct=`。
#   为什么要有这个开关而**缺省必须是 1**：它唯一存在的意义是"出事时一键退回旧行为"，
#   若缺省是 0，那所有人都跑在旧行为上，等于这条改造没做。
REQUIRE_ACCT = os.environ.get('YGZZ_ADMIN_REQUIRE_ACCT', '1') != '0'

# 这些接口**不需要** acct（它们与"操作哪个账号"无关）
NO_ACCT_ROUTES = ('/admin/api/accounts',)



def configure(**kw):
    DEP.update(kw)


def get_token():
    """读(或首次生成)后台 token"""
    try:
        with open(_TOKEN_FILE, 'r', encoding='utf-8') as f:
            t = f.read().strip()
        if t:
            return t
    except Exception:
        pass
    t = os.urandom(16).hex()
    try:
        with open(_TOKEN_FILE, 'w', encoding='utf-8') as f:
            f.write(t)
        print('[ADMIN] 已生成后台 token -> %s' % _TOKEN_FILE, flush=True)
    except Exception as e:
        print('[ADMIN] token 写入失败: %r (本次用临时 token)' % e, flush=True)
    return t


# ============================================================================
# 业务查询/操作
# ============================================================================
def _item_types():
    """给前端用的类型清单 (与 validate_mail_item 的白名单同源, 不会漂移)

    ★v36: `handlers_sys.MAIL_ITEM_DISABLED` 里的类型**不出现在清单里** —— 后台就不给这个选项,
      但校验器那边也拦着 (双保险, 防有人绕过 UI 直接 POST)。
    """
    LABEL = {1: '装备', 2: '道具', 3: '铜钱', 4: '妖怪', 5: '元灵', 6: '鬼道积分',
             7: '妖魂', 8: '唤妖符', 9: '体力', 14: '皮肤', 17: '玩家经验', 18: '宝箱', 22: '头像框'}
    out = []
    for t in hs.MAIL_ITEM_TYPES:
        if t in getattr(hs, 'MAIL_ITEM_DISABLED', {}):
            continue
        need = t in hs.MAIL_ITEM_TABLE
        out.append({'type': t, 'label': LABEL.get(t, str(t)),
                    'need_id': need,
                    'table': (hs.MAIL_ITEM_TABLE.get(t) or ('', ''))[0]})
    return out


def search_items(rt, q, limit=60, all_forms=False, bonus_skin_only=True):
    """按类型搜静态表 -> [{id, name, extra}]

    ★v36（用户要求）**type=4 妖怪默认只列"链首"**：隐藏"皮肤形态怪"
      （`妖神夜庭柯`/`大天师`/`天师-暮成雪` 这类在 L91 里独立成行、但游戏内等于
        "同一个角色换一套皮肤"的形态 —— 判据就是 `L91.SourceID != ID`）。
      依据: 抽卡池 82 只**全是链首**（阶分布只有 01/11/21/31/51），高阶只能靠进化；
      而用户实测"直接发这些形态会出现属性空白/队伍界面错位，且可能还有未知 bug"。
      `all_forms=True` 可以看全部（排错用，UI 上有个勾选框）。
      ⚠ 只影响**搜索列表**，不影响已建好的邮件（它们照常生效/可撤销）。

    ★v37（用户要求）**type=14 皮肤默认只列"带属性加成"的**：
      L98_Monsterskin 共 434 个皮肤，**只有 22 个**带数值加成/特殊能力，其余是纯外观；
      用户实测发纯外观皮肤"只是皮肤出现，可能会有未知 bug"。
      `bonus_skin_only=False` 看全部（UI 上有个勾选框）。判据在 `monster_identity.skin_bonus()`。
    """
    rt = int(rt)
    if rt in hs.MAIL_ITEM_MONEY:
        return [{'id': 0, 'name': {3: '铜钱', 6: '鬼道积分', 7: '妖魂', 8: '唤妖符', 9: '体力',
                                   17: '玩家经验'}.get(rt, ''),
                 'extra': '（数值类, 不需要选物品; 数量就是数值大小）'}]
    if rt not in hs.MAIL_ITEM_TABLE:
        return []
    tbl, namecol = hs.MAIL_ITEM_TABLE[rt]
    rows = st.load_table(tbl)
    h = {c: i for i, c in enumerate(rows[0])}
    # ★v35: 类型 4(妖怪) 附"身份卡"标签 —— 见 monster_identity.py 与
    #   work/交接文档_v35_同角色多形态与发放校验缺口.md。
    #   起因: 用户连发两只同名怪(夜庭柯 201406 / 妖神夜庭柯 101306, 详情文案逐字相同),
    #   而旧列表只显示 Type=/Quality=/Star=, 运营**看不出**"这跟包里那只其实是同一个角色"。
    #   只提示, 不拦截 (该不该发未投放的怪是运营口径)。
    mi = None
    if rt in (4, 14):
        try:
            import monster_identity as mi
        except Exception as _e:
            print('[ADMIN] WARN monster_identity 不可用, 妖怪/皮肤标注关闭: %r' % _e, flush=True)
            mi = None
    out = []
    hidden = 0
    q = (q or '').strip().lower()
    for r in rows[1:]:
        try:
            _id = str(r[h['ID']])
            _nm = str(r[h[namecol]] or _id)
        except Exception:
            continue
        if q and q not in _nm.lower() and q not in _id:
            continue
        # ★2026-11-07（6 阶收口）：type=4 的 Star>=6 形态**始终隐藏**（即使勾了“全部形态”）。
        #   客户端不支持 6 阶（属性空白/队伍错位）；测试/排错需要时设 YGZZ_ALLOW_STAGE6=1。
        if rt == 4 and 'Star' in h and os.environ.get('YGZZ_ALLOW_STAGE6') != '1':
            try:
                if int(r[h['Star']] or 0) >= 6:
                    hidden += 1
                    continue
            except Exception:
                pass
        # ★v36: 妖怪默认**只列链首** —— 皮肤形态怪(SourceID != ID)不出现在列表里。
        #   注意计数放在 `hidden` 里并回报给前端, 这样"明明表里有 966 只却只看到 N 只"不会变成谜。
        if rt == 4 and not all_forms and mi is not None:
            try:
                if not mi.is_chain_head(_id):
                    hidden += 1
                    continue
            except Exception:
                pass
        # ★v37: 皮肤默认**只列带属性加成的** (434 个里只有 22 个有加成)。
        #   ⚠ 必须先要求"这个 id 真的是 L98 里的一行": L91(怪) 与 L98(皮肤) 的 id **存在同号撞车**
        #     (实测 10022 既是怪 阴主-三山 的一阶, 又是皮肤 阴磷阎罗), 不先卡这一步会把怪当皮肤放行。
        if rt == 14 and mi is not None:
            try:
                if not mi.is_skin_id(_id):
                    hidden += 1
                    continue
                if bonus_skin_only and not mi.is_bonus_skin(_id):
                    hidden += 1
                    continue
            except Exception:
                pass
        extra = ''
        for c in ('Type', 'Quality', 'Rarity', 'LvLimit', 'Star'):
            if c in h:
                try:
                    extra += '%s=%s ' % (c, r[h[c]])
                except Exception:
                    pass
        it = {'id': int(_id), 'name': _nm, 'extra': extra.strip()}
        if mi is not None:
            try:
                if rt == 4:
                    mi.annotate(it)                      # 妖怪: 链/阶/羁绊/已在包中
                elif rt == 14:
                    # ★v37: 皮肤行**不要**走妖怪身份卡 —— L91(怪)与 L98(皮肤) id 会同号撞车,
                    #   走错就会显示"链10022(1阶)·末阶"这种妖怪专属标签 (实测踩过)。
                    _ok_b, _bonus = mi.skin_bonus(_id)
                    _chain = mi.skin_target_chain(_id)
                    _tgt = ''
                    if _chain:
                        _nm2 = (mi.describe(_chain) or {}).get('name') or ''
                        _tgt = '给 %s(%s) 用' % (_chain, _nm2) if _nm2 else ('给链 %s 用' % _chain)
                    it['tag'] = ((_tgt + ' · ') if _tgt else '') + (
                        ('★带属性加成: ' + _bonus) if _ok_b else '纯外观(无属性加成)')
                    it['warn'] = [] if _ok_b else ['这个皮肤**没有任何属性加成**']
            except Exception as _e:
                it['tag'] = it.get('tag') or ''
                it.setdefault('warn', []).append('标注失败: %r' % _e)
        out.append(it)
        if len(out) >= int(limit):
            break
    if rt in (4, 14):
        for it in out:
            it['hidden_forms'] = hidden          # 同一个值挂在每行上: 前端不用额外接口
    return out


def _item_display(it):
    """[type,id,cnt] -> '道具 桃木剑(1010) ×5' 这样的人类可读串"""
    try:
        t, i, c = int(it[0]), int(it[1]), int(it[2])
    except Exception:
        return str(it)
    label = {1: '装备', 2: '道具', 3: '铜钱', 4: '妖怪', 5: '元灵', 6: '鬼道积分',
             7: '妖魂', 8: '唤妖符', 9: '体力', 14: '皮肤', 17: '玩家经验', 18: '宝箱', 22: '头像框'}.get(t, 'type%d' % t)
    try:
        nm = hs._mail_name(t, i)
    except Exception:
        nm = str(i)
    if t in hs.MAIL_ITEM_MONEY:
        return '%s ×%d' % (label, c)
    return '%s %s(%d) ×%d' % (label, nm, i, c)


def _cfg_list():
    """读 gm_mail.json 原始配置 (不做校验, 用于编辑/展示)"""
    try:
        with open(hs._GM_MAIL_PATH, 'rb') as f:
            v = json.loads(f.read().decode('utf-8'))
        return v if isinstance(v, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        raise ValueError('gm_mail.json 解析失败: %r' % e)


def list_mails():
    """配置里的 GM 邮件 + 运行时状态 (生效/被跳过/已领/已删)"""
    cfg = _cfg_list()
    rows, err, skipped = hs.gm_mail_raw()
    live = {r[0]: r for r in rows}
    claimed = set(str(x) for x in (st.EQ_STATE.get('mail_claimed') or []))
    deleted = set(str(x) for x in (st.EQ_STATE.get('mail_deleted') or []))
    skip_by_id = {}
    for sid, why in skipped:
        skip_by_id.setdefault(str(sid), []).append(why)
    out = []
    for c in cfg:
        try:
            mid = int(c.get('id'))
        except Exception:
            mid = None
        lr = live.get(mid)
        out.append({
            'id': mid,
            'title': c.get('title'),
            'read': c.get('read', 0),
            'time': (lr[2] if lr else c.get('time')),
            'items': c.get('items') or [],
            'effective_items': (lr[5] if lr else []),
            'items_display': [_item_display(it) for it in (lr[5] if lr else (c.get('items') or []))],
            'visible': bool(lr) and str(mid) not in claimed and str(mid) not in deleted,
            'claimed': str(mid) in claimed,
            'deleted': str(mid) in deleted,
            'problems': skip_by_id.get(str(mid), []),
        })
    return {'mails': out, 'parse_error': err,
            'skipped': [{'id': s[0], 'why': s[1]} for s in skipped]}


def create_mail(payload):
    """新建一封 GM 邮件 -> (ok, data_or_error)

    ★v35 §3.3 新增 payload 字段 **auto_base** (bool, 默认 False):
      勾上后, type=4(妖怪) 附件若是**高阶形态**(≥2 阶), 自动换成**链首(1 阶)**。
      起因: 用户报"邮件发妖怪有问题 —— 属性空白/队伍界面错位", 真因是后台直发了 6 阶形态
      (正常玩法只发 1 阶链首, 靠进化往上走)。**默认关** —— 想发满阶怪测试是合理需求。
    """
    title = str((payload or {}).get('title') or 'GM 邮件').strip()[:60]
    raw_items = (payload or {}).get('items') or []
    if not isinstance(raw_items, list) or not raw_items:
        return False, '至少要有一个附件'
    if len(raw_items) > hs.MAIL_AFFIX_MAX:
        return False, '附件最多 %d 个 (客户端只有 affix1~%d 个显示位)' % (hs.MAIL_AFFIX_MAX, hs.MAIL_AFFIX_MAX)
    auto_base = bool((payload or {}).get('auto_base'))
    notes = []
    items = []
    for it in raw_items:
        if not isinstance(it, (list, tuple)) or len(it) < 3:
            return False, '附件格式应为 [类型, 静态id, 数量]: %r' % (it,)
        okv, norm, why = hs.validate_mail_item(it[0], it[1], it[2])
        if not okv:
            return False, why
        if auto_base:
            try:
                import monster_identity as _mi
                norm, note = _mi.normalize_attachment(norm)
                if note:
                    notes.append(note)
            except Exception as e:
                print('[ADMIN] WARN auto_base 失败, 按原样发放: %r' % e, flush=True)
        items.append([norm[0], norm[1], norm[2]])
    cfg = _cfg_list()
    # id: 显式指定(需≥9000且未占用) 或自动分配
    want = (payload or {}).get('id')
    used = set()
    for c in cfg:
        try:
            used.add(int(c.get('id')))
        except Exception:
            pass
    if want:
        try:
            want = int(want)
        except Exception:
            return False, 'id 必须是整数'
        if want < hs.GM_MAIL_ID_MIN:
            return False, 'id 必须 >= %d (低于会与内置邮件撞号)' % hs.GM_MAIL_ID_MIN
        if want in used:
            return False, 'id %d 已被占用' % want
        mid = want
    else:
        mid = hs.gm_mail_next_id()
    # ★ time 显式写入: 不写的话读取端会去要边车表, 首次分配的是**文件 mtime**,
    #   虽然也稳定, 但显式写下来最直观、也便于运营改历史时间。
    cfg.append({'id': mid, 'title': title, 'type': 1,
                'read': int((payload or {}).get('read') or 0),
                'time': int((payload or {}).get('time') or int(time.time() * 1000)),
                'items': items})
    hs.gm_mail_write(cfg)
    cleared = []
    if (payload or {}).get('clear_ledger'):
        # 重发同一封(同 id)时必须清掉账本, 否则玩家那边永远看不到
        for k in ('mail_claimed', 'mail_deleted'):
            lst = st.EQ_STATE.get(k) or []
            keep = [x for x in lst if str(x) != str(mid)]
            if len(keep) != len(lst):
                st.EQ_STATE[k] = keep
                cleared.append(k)
        if cleared:
            st.save_state()
    return True, {'id': mid, 'items': items, 'cleared': cleared, 'auto_base': auto_base, 'notes': notes}


def delete_mail(mid, clear_ledger=True):
    try:
        mid = int(mid)
    except Exception:
        return False, 'id 必须是整数'
    cfg = _cfg_list()
    left = [c for c in cfg if str(c.get('id')) != str(mid)]
    if len(left) == len(cfg):
        return False, '配置里没有 id=%d 的邮件' % mid
    hs.gm_mail_write(left)
    cleared = []
    if clear_ledger:
        for k in ('mail_claimed', 'mail_deleted'):
            lst = st.EQ_STATE.get(k) or []
            keep = [x for x in lst if str(x) != str(mid)]
            if len(keep) != len(lst):
                st.EQ_STATE[k] = keep
                cleared.append(k)
        if cleared:
            st.save_state()
    return True, {'id': mid, 'cleared': cleared}


def push_mails():
    """给**当前被绑定账号**推 1059 (完整邮件列表) -> 客户端 NetMailTotal:updateState 按
    (id,title,time,emailtype) 判重, 幂等, 不会重复插入。

    ★2026-10-02 (D5=B): 改走 `_push_scope` —— 1059 是"世界邮件表 + **当前账号的**
      已领/已删标记"的混合体，广播给所有在线连接就是跨账号数据泄漏。
    """
    payload = hs.handle_mail_total()
    return _push_scope(1059, payload)


def push_bag():
    """推 1033 道具背包 (改完玩家数据后刷新界面用) —— 同上, `prop_bag_json()` 直读
    `EQ_STATE['prop']`，是**账号私有**内容，绝不能广播。"""
    try:
        import handlers_bag as hb
        payload = hb.prop_bag_json()
    except Exception:
        return 0
    return _push_scope(1033, payload)


def _push_scope(msgid, payload):
    """把一帧推给**它该去的地方**：多账号 -> 只推被绑定账号；单档 -> 原样 `push_all`。

    为什么必须有这一层：`/admin` 的推送对象是"被绑定的那个账号"，而 `push_all` 把同一份
    bytes 发给**每一个**在线连接。`game_server.py:1116-1121` 的注释早就写明了这个坑，
    但上一轮只修了 8084（`handlers_sign.py:526-531`），admin 这两处漏了。

    ⚠ 多账号下**没绑定就一条都不推**（返回 0），绝不退回广播 ——
      那正是"看起来正常、其实泄漏了"的形态。
    """
    if isinstance(payload, str):
        payload = payload.encode('utf-8')
    try:
        import accounts as A
    except Exception:
        A = None
    if A is not None and A.ENABLED and A.REGISTRY is not None:
        fn = DEP.get('push_account')
        try:
            acc = A.EQ_STATE.current()
        except Exception:
            acc = None
        if fn and acc is not None:
            return fn(acc.id, msgid, payload)
        return 0
    fn = DEP.get('push_all')
    return fn(msgid, payload) if fn else 0


def status():
    live = None
    try:
        live = DEP['live_count']() if DEP.get('live_count') else None
    except Exception:
        pass
    savefile = st.SAVEFILE
    try:
        sz = os.path.getsize(savefile)
    except Exception:
        sz = -1
    up = int(time.time() - DEP['started_at']) if DEP.get('started_at') else None
    return {
        'pid': os.getpid(),
        'uptime_sec': up,
        'savefile': savefile,
        'save_bytes': sz,
        'live_conns': live,
        'real_save_write_allowed': bool(st.PROTECT_REAL_SAVEFILE),
        'gm_mail_path': hs._GM_MAIL_PATH,
        'mail_types': _item_types(),
        'money_types': list(hs.MAIL_ITEM_MONEY),
        'affix_max': hs.MAIL_AFFIX_MAX,
        'gm_mail_id_min': hs.GM_MAIL_ID_MIN,
        # ★v35 §3.3: 前端据此显示"自动降阶"勾选框
        'auto_base_supported': True,
        # ★v36: 存档版本化/快照/回档
        'state_version': st.STATE_VERSION,
        'save_version': st.state_version_of(),
        'backup_dir': st._BACKUP_DIR,
        'backup_keep': st._BACKUP_KEEP,
        # ★v40 (P1): 静态特性来源覆盖 —— 让运营看到"哪些怪根本没有特性可发", 而不是以为漏了
        #   用局部 import: 与 120/281/565 行处保持一致 (monster_identity 读表较重, 且要容忍它不可用)
        'feat_coverage': _feat_coverage(),
        'feat_source_fp': st.EQ_STATE.get('feat_source_fp'),
    }


def _feat_coverage():
    try:
        import monster_identity as _mi
        return _mi.feat_source_coverage()
    except Exception as _e:
        return {'error': 'monster_identity 不可用: %r' % _e}


def player():
    g = st.EQ_STATE
    return {
        'player_lv': g.get('player_lv'), 'player_exp': g.get('player_exp'),
        'gold': g.get('gold'), 'soulcoin': g.get('soulcoin'),
        'kidou': g.get('kidou'), 'eggcoin': g.get('eggcoin'),
        'prop_kinds': len(g.get('prop') or {}),
        'equip_kinds': len(g.get('equip_id') or {}),
        'monsters': len(g.get('monsters') or {}),
        'ghost': len(g.get('ghost') or []),
        'treasure_bag': g.get('treasure_bag') or {},
        'mail_claimed': sorted(str(x) for x in (g.get('mail_claimed') or [])),
        'mail_deleted': sorted(str(x) for x in (g.get('mail_deleted') or [])),
        'tower_plies': (g.get('tower') or {}).get('plies'),
    }


# ============================================================================
# ★v36 存档快照 / 一键回档 (交接文档 v34 §3.2 的 P0-1 后台侧)
# ============================================================================
def save_backups(limit=40):
    """列出快照 (最新在前)。只读。"""
    try:
        return {'state_version': st.STATE_VERSION,
                'savefile': st.SAVEFILE,
                'backup_dir': st._BACKUP_DIR,
                'keep': st._BACKUP_KEEP,
                'items': st.list_backups()[:int(limit)]}
    except Exception as e:
        return {'error': '%r' % e, 'items': []}


def save_snapshot(note='', acct=None):
    """手动打一份快照 (不参与去重/限流) -> (ok, data_or_error)

    ★2026-11-14：多账号模式下打的是**该账号的档**（`work\\accounts\\<aid>.json`），
      不再是模板 state.json —— 否则后台打出来的快照回档时用不上（见 `_restore_account`）。
    """
    if _multi():
        try:
            import accounts as _A
        except Exception as e:
            return False, '多账号模块不可用: %r' % (e,)
        aid = str(acct or '')
        if not aid or not _A.REGISTRY.exists(aid):
            return False, '多账号模式：要先在右上角选一个**已存在**的账号（当前 acct=%r）' % (acct,)
        try:
            name = _A.REGISTRY.snapshot_account(_A.REGISTRY.get_or_create(aid),
                                                tag='acct-manual', note=str(note or '')[:120],
                                                force=True)
        except Exception as e:
            return False, '%r' % (e,)
        if not name:
            return False, '该账号还没有存档文件（没进过游戏？）'
        return True, {'file': name, 'aid': aid}
    try:
        p = st.backup_state('manual', note=str(note or '')[:120])
        if not p:
            return False, '快照失败 (存档读不到?)'
        return True, {'file': os.path.basename(p)}
    except Exception as e:
        return False, '%r' % e


def _restore_account_snapshot(name, expected_sha=None, acct=None, _A=None):
    """多账号模式的回档：把一个**账号档快照**写回 `work\\accounts\\<aid>.json`。

    与单档回档的差别：
      · 快照的 `.meta.json` 里必须有 `aid`（`accounts.snapshot_account` 写的）；
        没有 `aid` 的是"模板快照"（单档时代的 state.json），**不能**用来回账号 —— 拒绝并说清原因；
      · 回档前先给当前档打一份 `acct-pre-restore` 留底（所以回档本身也能回滚）；
      · 写完把内存里的账号对象踢掉（`evict`），并断开在线连接，让客户端重连后读新档。
    """
    import hashlib as _h
    d = _A.REGISTRY._snapdir()
    if not name or os.path.basename(name) != name or not name.endswith('.json'):
        return False, '快照名不合法（只接受 _backups 下的纯文件名）: %r' % (name,)
    src = os.path.join(d, name)
    if not os.path.isfile(src):
        return False, '快照不存在: %s' % name
    try:
        with open(src + '.meta.json', 'r', encoding='utf-8') as f:
            meta = json.load(f)
    except Exception:
        meta = {}
    aid = str(meta.get('aid') or '')
    if not aid:
        return False, ('这是**模板快照**（单档时代的 state.json），不含任何玩家档，多账号下不能用它回档。'
                       '请选标签为 acct-* 的账号快照；没有的话先点「立刻打一份快照」。')
    if acct and str(acct) != aid:
        return False, '快照属于账号 %s，与右上角选中的 %s 不一致（防串档）' % (aid, acct)
    try:
        with open(src, 'rb') as f:
            b = f.read()
    except Exception as e:
        return False, '快照读不到: %r' % (e,)
    sig = _h.sha256(b).hexdigest()[:16]
    if expected_sha and str(expected_sha)[:16] != sig:
        return False, '快照哈希不符（期望 %s，实际 %s）—— 拒绝回档' % (str(expected_sha)[:16], sig)
    # ① 回档前留底（保护性，不受限流）
    try:
        _A.REGISTRY.snapshot_account(_A.REGISTRY.get_or_create(aid), tag='acct-pre-restore',
                                     note='回档前自动留底 (%s)' % name, force=True)
    except Exception as e:
        print('[ADMIN] WARN 回档前留底失败: %r' % (e,), flush=True)
    # ② 原子替换账号档
    dst = _A.REGISTRY._path(aid)
    tmp = dst + '.tmp'
    try:
        with open(tmp, 'wb') as f:
            f.write(b)
        os.replace(tmp, dst)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False, '写账号档失败: %r' % (e,)
    # ③ 丢掉缓存 + 踢连接
    try:
        _A.REGISTRY.evict(aid)
    except Exception:
        pass
    dropped = 0
    try:
        if DEP.get('drop_all'):
            dropped = DEP['drop_all']()
    except Exception as e:
        print('[ADMIN] WARN 断连失败: %r' % (e,), flush=True)
    return True, {'msg': '已回档到账号 %s 的 %s' % (aid, meta.get('created', name)),
                  'dropped_conns': dropped,
                  'player_lv': meta.get('player_lv'), 'gold': meta.get('gold'),
                  'monsters': meta.get('monsters'), 'aid': aid, 'file': name}


def save_restore(name, expected_sha=None, drop_conns=True, acct=None):
    """一键回档 -> (ok, data_or_error)。

    ⚠ 语义与风险 (写进返回值, 后台必须原样展示给运营):
      · 只回**玩家存档**; gm_mail.json / 后台 token / 快照本身 不受影响;
      · 回档前会**自动留底** (tag=pre-restore), 所以回档本身也能再回滚;
      · 回档后**内存里的 EQ_STATE 会重新载入** —— 这是必须的, 否则下一次 save_state
        会把旧的(回档前的)内存态写回磁盘, 回档白做 (v33 已实测这个坑);
      · 因为状态被整体换掉, **在线客户端必须重连**; drop_conns=True 时服务端主动断开所有连接。

    ★★2026-10-02 (D5=B) **多账号下本函数直接拒绝** —— 这不是"没做", 是这个实现在多账号下
      本来就不安全:
        · 快照源是 `SAVEFILE`(= 模板 `work\\state.json`, 见 `state.py:230`),
          而**账号档 `work\\accounts\\*.json` 从来没有任何快照** ⇒ 上面承诺的"回档本身也能再回滚"
          在多账号下**不成立**: 覆盖的是 u0001 的档, 却没有它的留底。
        · 更糟的是回档会先换掉 `work/state.json`(模板) 再 `reload_from_file()` 灌进被绑定账号 ——
          模板被改、账号档被覆盖, 两处都动, 且**不可回滚**。
      要单独备份/还原某个账号: 直接拷 `work\\accounts\\<id>.json`(它就是那个玩家的完整档)。
    """
    try:
        import accounts as _A
    except Exception:
        _A = None
    if _A is not None and _A.ENABLED and _A.REGISTRY is not None:
        # ★★2026-11-14：多账号下**改成回账号档**（模板 state.json 的快照在这里没有意义）。
        #   修前这里直接拒绝 ⇒ 后台只会显示"回档失败: 400"，运营完全不知道该怎么办。
        return _restore_account_snapshot(name, expected_sha, acct=acct, _A=_A)
    ok, msg = st.restore_from_backup(name, expected_sha=expected_sha)
    if not ok:
        return False, msg
    try:
        st.reload_from_file()
    except Exception as e:
        return False, '%s —— 但重新载入内存失败: %r (服务端状态与磁盘可能不一致, 建议重启服务端)' % (msg, e)
    dropped = 0
    if drop_conns and DEP.get('drop_all'):
        try:
            dropped = DEP['drop_all']()
        except Exception as e:
            print('[ADMIN] WARN 断连失败: %r' % e, flush=True)
    try:
        st.save_state()          # 立刻落盘一次: 让磁盘与内存一致, 并产生一份新快照
    except Exception as e:
        # ★2026-10-02 (D5=B): 原来这里只打一行 WARN, 然后**照样 return True** ——
        #   于是"回档成功"是假的: ① 快照已经覆盖了磁盘、③ 已经踢光所有连接,
        #   但内存态**没能落盘**, 磁盘与内存从此不一致。报成功比报失败危险得多。
        print('[ADMIN] ERROR 回档后落盘失败: %r' % e, flush=True)
        return False, ('%s —— 但回档后**落盘失败**: %r'
                       '(磁盘已改、内存未落, 服务端状态不一致, 请重启服务端)' % (msg, e))
    return True, {'msg': msg, 'dropped_conns': dropped,
                  'player_lv': st.EQ_STATE.get('player_lv'),
                  'gold': st.EQ_STATE.get('gold'),
                  'monsters': len(st.EQ_STATE.get('monsters') or {}),
                  'state_version': st.state_version_of()}


def _multi():
    """多账号真的在用吗。**所有新行为都 gate 在这上面** —— 单档模式下逐字节不变。"""
    try:
        import accounts as A
    except Exception:
        return False
    return bool(A.ENABLED and A.REGISTRY is not None)


def _current_aid():
    try:
        import accounts as A
        return getattr(A.EQ_STATE.current(), 'id', None)
    except Exception:
        return None


def accounts_list():
    """给 UI 账号下拉框：**磁盘上的全部账号**（不只是本进程已加载的）。

    三个坑（都实测过）：
      · `all_accounts()` 只返回 `by_id`，而服务端**刚启动时它是空的** ⇒ 不能当选列表来源；
        这里直接**扫目录**，最可靠。
      · `_accounts.json` 与账号档**同目录**，必须排除。
      · **不要用 `_load()`**：它对坏档 `raise AccountsError` ⇒ 一个坏档让整个下拉 500。
        这里 per-file try/except 并标 `corrupt:true`（坏档要**看得见**，不是藏起来）。
    """
    if not _multi():
        return {'ok': True, 'multi_account': False, 'accounts': [],
                'note': '多账号未启用（设 YGZZ_MULTI_ACCOUNT=1）'}
    import accounts as A
    root = A.REGISTRY.root
    online = set(A.REGISTRY.online.keys())
    try:
        names = sorted(os.listdir(root))
    except Exception as e:
        return {'ok': False, 'error': '%r' % e, 'accounts': []}
    out = []
    for n in names:
        if not n.endswith('.json') or n == A._INDEX_NAME:
            continue
        aid = n[:-5]
        if not (len(aid) == 5 and aid[0] == 'u' and aid[1:].isdigit()):
            continue
        p = os.path.join(root, n)
        rec = {'id': aid, 'online': aid in online, 'corrupt': False}
        try:
            s = os.stat(p)
            rec['bytes'] = s.st_size
            rec['mtime'] = int(s.st_mtime * 1000)
        except Exception:
            rec['bytes'] = -1
        try:
            with open(p, encoding='utf-8') as f:
                d = json.load(f)
            rec['role_name'] = d.get('role_name') or ('未命名#' + aid)
            rec['player_lv'] = d.get('player_lv')
        except Exception as e:
            rec['corrupt'] = True
            rec['error'] = '%r' % e
            rec['role_name'] = '(坏档)'
        out.append(rec)
    return {'ok': True, 'multi_account': True, 'root': root,
            'current': _current_aid(), 'required': REQUIRE_ACCT, 'accounts': out}


def tail_log(name='server_debug.log', lines=200):
    base = DEP.get('base_dir') or os.path.dirname(os.path.abspath(__file__))
    # ★2026-10-02 (D5=B): 原来直接 `os.path.join(base, name)`, 而 `name` 来自 query ⇒
    #   `?file=..\\..\\admin_token.txt` 就是**任意文件读**(同类洞 CDN 早已修, 见 sec_hardening A1-A4)。
    #   照抄 `state.py:358` 的 basename 校验: 只允许**裸文件名**。
    if not name or name in ('.', '..') or os.path.basename(name) != name:
        return {'file': name, 'error': '非法文件名: 只允许裸文件名(不含路径分隔符)', 'lines': []}
    p = os.path.join(base, name)
    try:
        with open(p, 'rb') as f:
            data = f.read()
        txt = data.decode('utf-8', 'replace').splitlines()
        return {'file': name, 'lines': txt[-int(lines):]}
    except Exception as e:
        return {'file': name, 'error': '%r' % e, 'lines': []}


# ============================================================================
# HTTP 路由
# ============================================================================
def _q(path):
    """极简 query 解析 (?a=1&b=2)"""
    out = {}
    if '?' in path:
        for kv in path.split('?', 1)[1].split('&'):
            if not kv:
                continue
            k, _, v = kv.partition('=')
            from urllib.parse import unquote_plus
            out[unquote_plus(k)] = unquote_plus(v)
    return out


def handle(handler, path, body, send_json, send_bytes, send_text):
    """返回 True 表示已应答。

    handler: BaseHTTPRequestHandler (用来读 header)
    send_json(o) / send_bytes(b, ctype) / send_text(s, ctype) —— 由 game_server 提供的应答器
    """
    route = path.split('?', 1)[0].rstrip('/')
    qs = _q(path)
    token = get_token()
    got = qs.get('t') or (handler.headers.get('X-Admin-Token') or '')
    if got != token:
        send_text('403 后台 token 无效。请用 http://127.0.0.1:18080/admin?t=<token> 打开 '
                  '(token 在 work/admin_token.txt)', 'text/plain; charset=utf-8', code=403)
        return True

    # ★★2026-10-02 (D5=B) 强制 acct：多账号下所有 `/admin/api/*` 都必须显式说明"操作哪个账号"。
    #
    #   为什么要它：修前缺 `acct` 会**静默落到 u0001**（`accounts.bind_admin`）——
    #   运营少打一个参数就把东西发给了另一个人，而且**没有任何提示**。
    #
    #   三条不可颠倒的规则：
    #     ① 位置在 **token 闸之后**：否则 `_admin_selftest.py:74-77` 的"无 token -> 403"
    #        会先变成 400 而判红（鉴权必须排在参数校验之前）。
    #     ② **只 gate 在多账号真的启用时**（`_multi()`）：单档下只有一个档，"哪个账号"这个问题
    #        根本不存在 —— 若也强制 acct，会把 80 套回归网里所有 /admin 调用一起打红。
    #     ③ `/admin` 页面与 `/admin/api/accounts` **豁免**：前者是入口（旧链接/启动脚本要用），
    #        后者是"有哪些账号可选"本身。
    if route.startswith('/admin/api/') and route not in NO_ACCT_ROUTES and _multi():
        import accounts as _A
        _acct, _aerr = _A.parse_acct_param(path)
        if _aerr:
            send_json({'ok': False, 'error': _aerr}, code=400)      # 格式非法（含路径穿越）
            return True
        if not _acct and REQUIRE_ACCT:
            send_json({'ok': False, 'error':
                       '缺少 acct=<账号id>。多账号下本后台**没有缺省账号**'
                       '（缺省会让人改错人）。可用账号见 /admin/api/accounts。'}, code=400)
            return True
        if _acct and not _A.REGISTRY.exists(_acct):
            # ⚠ 必须 404 **且不创建**：走 get_or_create 会凭空调号，而新号 id 会与
            #   真玩家首登撞号（login() 用 len(idx)+1）＝ 真串档。
            send_json({'ok': False, 'error': '账号不存在: %s（**不会自动创建**）' % _acct},
                      code=404)
            return True

    if route in ('/admin', '/admin/index.html'):
        try:
            with open(_UI_FILE, 'rb') as f:
                send_bytes(f.read(), 'text/html; charset=utf-8')
        except Exception as e:
            send_text('admin_ui.html 读取失败: %r' % e, 'text/plain; charset=utf-8', code=500)
        return True

    def jbody():
        try:
            return json.loads(body.decode('utf-8')) if body else {}
        except Exception:
            return {}

    try:
        if route == '/admin/api/accounts':
            # ★2026-10-02 (D5=B): 账号下拉框的数据源。**不需要 acct**（它就是回答"有哪些账号"）。
            send_json(accounts_list())
        elif route == '/admin/api/status':
            send_json(status())
        elif route == '/admin/api/player':
            send_json(player())
        elif route == '/admin/api/tables':
            _af = qs.get('all_forms') in ('1', 'true', 'yes')
            _bs = qs.get('bonus_skin_only', '1') not in ('0', 'false', 'no')
            send_json({'items': search_items(qs.get('type') or 0, qs.get('q'), qs.get('limit') or 60,
                                             all_forms=_af, bonus_skin_only=_bs),
                       'all_forms': _af, 'bonus_skin_only': _bs})
        elif route == '/admin/api/monster_info':
            # ★v35: 妖怪身份卡 (只读)。type=4 专用, 给 UI 展开"这只到底是谁、包里有没有同角色"。
            try:
                import monster_identity as mi
                send_json(mi.describe(qs.get('id') or 0))
            except Exception as e:
                send_json({'known': False, 'error': '%r' % e}, code=500)
        elif route == '/admin/api/mails':
            _m = (handler.command or 'GET').upper()
            if _m == 'GET':
                send_json(list_mails())
            elif _m == 'POST':
                okv, data = create_mail(jbody())
                send_json({'ok': okv, 'data': data}, code=200 if okv else 400)
            elif _m == 'DELETE':
                okv, data = delete_mail(qs.get('id'), clear_ledger=(qs.get('clear_ledger', '1') != '0'))
                send_json({'ok': okv, 'data': data}, code=200 if okv else 400)
            else:
                send_json({'ok': False, 'error': '不支持的方法 %s' % _m}, code=405)
        elif route == '/admin/api/push_mails':
            send_json({'pushed': push_mails()})
        elif route == '/admin/api/push_bag':
            send_json({'pushed': push_bag()})
        elif route == '/admin/api/save':
            # ★v36: 存档快照 / 回档 (P0-1)
            _m = (handler.command or 'GET').upper()
            if _m == 'GET':
                send_json(save_backups(qs.get('limit') or 40))
            elif _m == 'POST':
                b = jbody()
                act = str(b.get('action') or 'snapshot')
                if act == 'snapshot':
                    okv, data = save_snapshot(b.get('note') or '',
                                              acct=(qs.get('acct') or b.get('acct')))
                    send_json({'ok': okv, 'data': data}, code=200 if okv else 400)
                elif act == 'restore':
                    okv, data = save_restore(b.get('file'), b.get('sha'),
                                             acct=(qs.get('acct') or b.get('acct')),
                                             drop_conns=(b.get('drop_conns', True) is not False))
                    send_json({'ok': okv, 'data': data}, code=200 if okv else 400)
                else:
                    send_json({'ok': False, 'error': '不支持的 action: %s (可用 snapshot/restore)' % act}, code=400)
            else:
                send_json({'ok': False, 'error': '不支持的方法 %s' % _m}, code=405)
        elif route == '/admin/api/log':
            send_json(tail_log(qs.get('file') or 'server_debug.log', qs.get('lines') or 200))
        elif route == '/admin/api/validate':
            b = jbody()
            okv, norm, why = hs.validate_mail_item(b.get('type'), b.get('id'), b.get('count') or 1)
            send_json({'ok': okv, 'normalized': norm, 'error': why})
        else:
            send_json({'ok': False, 'error': '未知接口 %s' % route}, code=404)
    except Exception as e:
        import traceback
        traceback.print_exc()
        send_json({'ok': False, 'error': '%r' % e}, code=500)
    return True
