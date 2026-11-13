# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 玩家状态与 JSON 文件持久化
# 集中管理 金币/等级/经验/装备/道具/怪物装备槽 等可变状态, 并提供 load_state/save_state
import os, json, threading, hashlib

_DIR = os.path.dirname(os.path.abspath(__file__))
# 支持 YGZZ_SAVEFILE 环境变量覆盖 (测试隔离: 测试服务器用临时存档, 不碰真实 state.json)
SAVEFILE = os.environ.get('YGZZ_SAVEFILE') or os.path.join(_DIR, 'state.json')   # 持久化文件路径

# 新号模板：run_server.py 设 YGZZ_NEW_PLAYER=starter -> 初始档（1 级/0 金/0 抽）；
# 测试/裸 game_server.py 不设 -> 保持回归用的测试档（8 级/999M/100 抽）。
_STARTER = (os.environ.get('YGZZ_NEW_PLAYER') or '').strip().lower() in (
    '1', 'starter', 'initial', 'true', 'yes')

# ---------- ★★2026-09-12 (v26) 存档写保护 (事故复盘后的加固) ----------
# 事故: 一个"验证用"的临时 python 进程 `import state; import handlers_profile; ...` 直接跑在
#   生产工作目录里 —— state 模块 import 时 SAVEFILE 默认 = work\state.json, EQ_STATE 是 _default_state(),
#   而 handlers_profile.handle_change_name 内部调用 save_state() -> 把**默认存档**整体覆盖掉了
#   用户 19KB 的真实存档 (5 分钟前还在跑的服务端内存里是好的, 磁盘已被写成 4KB 默认值)。
#   根因不是某一个 handler, 而是**"没显式声明自己在测试隔离下"的进程可以写生产存档**。
#
# 加固三条 (互相独立, 任一条都能拦住这类事故):
#   1) 声明式隔离: 测试脚本必须设 YGZZ_TEST_ISOLATED=1 (并在 _common 里设 SAVEFILE 到 _*.json);
#      设了之后**绝不允许**写生产 state.json —— 一写就抛, 而不是静默污染。
#   2) PROTECT_REAL_SAVEFILE: 生产服务端启动时显式置 True 才允许写生产存档;
#      任何**非服务端**进程 (import 后随手调 save_state) 默认写生产存档 -> 直接抛错。
#   3) backup_state(): 每次真正写生产存档前留一份轮转快照 (_backups/state-<ts>.json, 保留最近 20 份)。
_REAL_SAVEFILE = os.path.join(_DIR, 'state.json')
TEST_ISOLATED = os.environ.get('YGZZ_TEST_ISOLATED') == '1'
# 服务端入口 (game_server.py) 会置 True; 测试/临时脚本不会 -> 天然拦住"误写生产存档"
PROTECT_REAL_SAVEFILE = False
_BACKUP_DIR = os.path.join(_DIR, '_backups')
_BACKUP_KEEP = 20
_BACKUP_LAST = [0.0]

# ============================================================================
# ★v36 (2026-09-13) 存档版本化 + 去重快照 + 一键回档  (交接文档 v34 §3.2 的 P0-1)
# ----------------------------------------------------------------------------
# 旧实现的三个问题 (实测):
#   ① 快照是**每秒一张的近乎重复副本**: `_BACKUP_LAST` 只限流 1 秒,
#      而服务端每个动作都 save_state -> 实测 _backups 里 20 份里 18 份内容几乎一样,
#      真出事时"想回滚到某个有意义的点"根本挑不出来。
#   ② 没有版本号: 存档结构演进 (v32 加字段 / v34 加 monster_ueq 回填) 全靠
#      "load_state 里一堆幂等迁移段"兜着, 没有任何地方能回答"这份档是多少版的"。
#   ③ 没有回档入口: 只能人工 copy 文件, 而服务端把 EQ_STATE 放内存 ->
#      外部改 state.json 会被下一次 save_state 覆盖 (v33 已实测)。
# 本段解决 ①②③; 迁移链注册表 `_MIGRATIONS` 给后续版本加结构变更用 (只增不改)。
# ============================================================================
STATE_VERSION = 4              # 当前存档结构版本; 加字段/改语义时 +1 并注册迁移
#   v1 -> v2 (2026-09-13, v39): 元灵背包"同 id 多行"合并 (修"同一物品不堆叠")。
#   v2 -> v3 (2026-09-13, v40): 静态特性加"数据源指纹 + 出处" + 换表一次性重算 (P0-1)。
#   v3 -> v4 (2026-09-13, v42): 加 `prop_seeded` —— 道具种子判据从"键不存在就补"
#                              改成"**曾经拥有过**" (修"用光后重启又被重发")。
#     注意: apply_migrations() 原先只在 reload_from_file() 里被调用; 正常启动走的是
#     load_state() 里的**内联幂等迁移段**。所以 v2/v3/v4 的逻辑**两处都挂了**(同一个函数),
#     否则"重启服务端"不会触发, 用户要等到下次回档才生效。
#     (v39 已顺手补上: load_state() 末尾也会调 apply_migrations(data), 版本号不再空转。)
_BACKUP_TAG = 'state'          # 快照文件前缀
_BACKUP_MIN_INTERVAL = 120.0   # 距上次**保留**的快照至少间隔秒数 (配合内容哈希去重, 防刷屏)


def state_version_of(data=None):
    """读存档结构版本; 老档没有这个字段 -> 0 (走迁移链升上来)。"""
    d = EQ_STATE if data is None else data
    try:
        return int(d.get('state_version') or 0)
    except Exception:
        return 0


# --- 迁移链: {目标版本: fn(data) -> [变更说明, ...]} ---
#   约定: fn **必须幂等**(可能对同一份档跑多次), 且**只补/只改结构**, 不动玩家数值。
#   v1 = "把 v35 及以前那些散落在 load_state 里的迁移段正式记一次账" ——
#        那些迁移段本身仍然幂等常驻 (它们同时承担"新档补默认值"), 这里只负责**打版本号**。
_MIGRATIONS = {}


def _mig_v1(data):
    out = []
    if not data.get('state_version'):
        out.append('标记存档结构版本 = 1 (v35 及以前的字段布局)')
    return out


_MIGRATIONS[1] = _mig_v1


def _mig_v2(data):
    """v39: 元灵背包"同 (静态id, isload) 多行" -> 合并成一行 (修"同一物品不堆叠")。

    为什么不变量是"总和守恒": 只把分散在多个 ucode 行上的数量搬到第一行,
    **不产生也不销毁任何元灵**。所以这不是"动玩家数值", 而是把同一个物品的
    多份账目并成一份 —— 与 v1 的"只打版本号"同属结构整理。
    """
    return merge_ghost_rows(data)


_MIGRATIONS[2] = _mig_v2


def _mig_v3(data):
    """v40: 静态特性加"数据源指纹 + 出处" (P0-1)。

    第一次跑**不会改任何槽** —— backfill_feat_src 用的就是当前算法,
    与现状一致的记作"机器种的", 不一致的标 '?'(玩家动过) -> 永不自动重算。
    真正的重算只发生在**指纹变了**的那一次 (换表/改算法)。
    """
    return recompute_features_on_source_change(data)


_MIGRATIONS[3] = _mig_v3


def _mig_v4(data):
    """v42: 道具种子判据改为"曾经拥有过" (`prop_seeded`)。

    真正的发放逻辑在 load_state() 的内联段里 (那里能拿到"文件里有没有这个字段");
    这里只负责**打版本号**, 与 _mig_v1 同一分工。
    """
    if 'prop_seeded' not in data or not isinstance(data.get('prop_seeded'), list):
        data['prop_seeded'] = [str(x) for x in _PROP_DEFAULTS.keys()]
        return ['建立 prop_seeded (标记 %d 个默认种子为已发放 -> 用光后不再重发)' % len(data['prop_seeded'])]
    return []


_MIGRATIONS[4] = _mig_v4


def apply_migrations(data):
    """把 data 沿迁移链升到 STATE_VERSION。返回变更说明列表 (供日志/后台展示)。"""
    changes = []
    cur = state_version_of(data)
    if cur > STATE_VERSION:
        changes.append('⚠ 存档版本 %d 比程序支持的 %d 还新 —— 不做任何迁移 (可能是回退了程序版本)'
                       % (cur, STATE_VERSION))
        return changes
    for v in range(cur + 1, STATE_VERSION + 1):
        fn = _MIGRATIONS.get(v)
        if not fn:
            continue
        try:
            got = fn(data) or []
        except Exception as e:
            changes.append('⚠ 迁移到 v%d 失败: %r (保留原版本号)' % (v, e))
            return changes
        data['state_version'] = v
        for c in got:
            changes.append('v%d: %s' % (v, c))
    return changes


def _backup_files():
    """快照文件列表 (按名排序 = 按时间排序)。

    ⚠ **必须排除边车** `*.meta.json`: 它同样以 `state-` 开头、以 `.json` 结尾 ——
      第一版没排除, 结果 `files[-1]` 拿到的是"最新快照的元数据文件",
      内容哈希自然永不相等 -> 去重失效; 更糟的是 list_backups() 会把 20 份快照算成 40 条。
      (这个 bug 是隔离单测抓出来的 —— 生产上因为元数据是新加的, 一时看不出来。)
    """
    try:
        return sorted(f for f in os.listdir(_BACKUP_DIR)
                      if f.startswith(_BACKUP_TAG + '-') and f.endswith('.json')
                      and not f.endswith('.meta.json'))
    except Exception:
        return []


def _snapshot_meta(path):
    """读快照边车 (.meta.json); 没有就现场从文件算。"""
    mp = path + '.meta.json'
    try:
        with open(mp, 'r', encoding='utf-8') as f:
            m = json.load(f)
        if isinstance(m, dict):
            return m
    except Exception:
        pass
    m = {'file': os.path.basename(path)}
    try:
        with open(path, 'rb') as f:
            b = f.read()
        m['bytes'] = len(b)
        m['sha256'] = hashlib.sha256(b).hexdigest()[:16]
        d = json.loads(b.decode('utf-8'))
        if isinstance(d, dict):
            m['state_version'] = state_version_of(d)
            m['player_lv'] = d.get('player_lv')
            m['gold'] = d.get('gold')
            m['monsters'] = len(d.get('monsters') or {})
            m['mail_claimed'] = len(d.get('mail_claimed') or [])
    except Exception as e:
        m['error'] = repr(e)
    return m


def list_backups():
    """按时间倒序列出快照 (含边车元数据)。只读。"""
    out = []
    for f in _backup_files():
        p = os.path.join(_BACKUP_DIR, f)
        m = _snapshot_meta(p)
        m.setdefault('file', f)
        try:
            m['mtime'] = os.path.getmtime(p)
        except Exception:
            m['mtime'] = 0
        out.append(m)
    out.sort(key=lambda x: x.get('mtime') or 0, reverse=True)
    return out


def autosave_snapshot():
    """落盘前的自动快照**门槛** (单独抽出来, 便于隔离单测覆盖)。

    为什么不直接写在 _save_state_locked 里: 那里的"生产档/隔离档"判定会把
    整条备份路径挡在测试之外 —— 而"存档备份/回档"恰恰是最需要回归的东西 (P0-1)。
    抽出来后单测可以直接调本函数, 不需要伪造 _is_real_save()。
    """
    if not (_is_real_save() and PROTECT_REAL_SAVEFILE):
        return None
    return backup_state('autosave')


def backup_state(tag='auto', note=''):
    """写一份**有意义的**快照 (去重 + 限流) -> 路径 or None。

    与旧实现的区别: 先算内容哈希, 与**最近一份**快照相同就直接跳过 (不产生垃圾副本);
    保留策略从"按文件数砍"改成"按数量 + 只删最老的自动快照"。

    ★ 快照对象是 **SAVEFILE**(而不是写死 _REAL_SAVEFILE) —— 这样隔离测试也能覆盖本函数,
      否则"备份/回档"这条最需要回归的路径永远测不到 (旧实现就踩了这个坑)。
    """
    import time as _t
    now = _t.time()
    src = SAVEFILE
    try:
        with open(src, 'rb') as f:
            b = f.read()
        sig = hashlib.sha256(b).hexdigest()[:16]
    except Exception:
        return None
    # ★ 保护性快照 (回档前留底 / 手动关键点)**无条件落盘**, 不参与去重:
    #   去重的目的是"别刷垃圾副本", 而这两类恰恰是"必须留下痕迹"的 ——
    #   实测坑: 回档前那份留底与上一份内容相同就被去重跳过, 结果"回档本身无法回滚"。
    protective = tag in ('pre-restore', 'manual', 'pre-smoke', 'server-start')
    try:
        files = _backup_files()
        if files and not protective:
            last = os.path.join(_BACKUP_DIR, files[-1])
            lm = _snapshot_meta(last)
            if lm.get('sha256') == sig:
                _BACKUP_LAST[0] = now
                return None                      # 内容没变 -> 不产生重复快照
    except Exception:
        pass
    if now - _BACKUP_LAST[0] < _BACKUP_MIN_INTERVAL and tag in ('autosave', 'auto'):
        return None                              # 自动快照限流 (手动/关键点不受限)
    _BACKUP_LAST[0] = now
    try:
        os.makedirs(_BACKUP_DIR, exist_ok=True)
        # ★ 文件名必须**唯一**: 只精确到秒的话, 同一秒内的两次快照会**同名互相覆盖**
        #   (隔离单测抓到的真 bug —— 生产上表现为"刚存的关键快照莫名其妙没了")。
        base = '%s-%s-%s' % (_BACKUP_TAG, _t.strftime('%Y%m%d-%H%M%S'), tag)
        name = base + '.json'
        n = 1
        while os.path.exists(os.path.join(_BACKUP_DIR, name)):
            n += 1
            name = '%s-%d.json' % (base, n)
        dst = os.path.join(_BACKUP_DIR, name)
        with open(dst, 'wb') as f:
            f.write(b)
        meta = {'file': name, 'tag': tag, 'note': note, 'bytes': len(b), 'sha256': sig,
                'state_version': None, 'player_lv': None, 'gold': None, 'monsters': None,
                'mail_claimed': None, 'created': _t.strftime('%Y-%m-%d %H:%M:%S')}
        try:
            d = json.loads(b.decode('utf-8'))
            if isinstance(d, dict):
                meta['state_version'] = state_version_of(d)
                meta['player_lv'] = d.get('player_lv')
                meta['gold'] = d.get('gold')
                meta['monsters'] = len(d.get('monsters') or {})
                meta['mail_claimed'] = len(d.get('mail_claimed') or [])
        except Exception:
            pass
        try:
            with open(dst + '.meta.json', 'w', encoding='utf-8') as f:
                json.dump(meta, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        # 保留策略: 超量时只删**最老的 autosave**, 手动/关键点快照 (tag 不是 autosave/auto) 不删
        files = _backup_files()
        if len(files) > _BACKUP_KEEP:
            for old in files[:len(files) - _BACKUP_KEEP]:
                om = _snapshot_meta(os.path.join(_BACKUP_DIR, old))
                if om.get('tag') in ('autosave', 'auto'):
                    for suffix in ('', '.meta.json'):
                        try:
                            os.remove(os.path.join(_BACKUP_DIR, old + suffix))
                        except Exception:
                            pass
        return dst
    except Exception:
        return None


def _tmp_path():
    """落盘用的临时文件名 —— **每次调用都唯一** (审计 §9 高severity #1)。

    旧实现两条落盘路径都用**同一个固定名字** `SAVEFILE + '.tmp'`:
      · `save_state()`          -> `_save_state_locked()`  (持 `_SAVE_LOCK`)
      · `restore_from_backup()` -> 由 `/admin` 回档的 **HTTP 线程**调用, **不持锁**
    两者并发时两次写会交错落进同一个文件, 然后 `os.replace` 把**半截 JSON** 换成正式存档
    (存档是本项目唯一不可再生资产)。加上 pid + 线程号后, 两条路径各写各的临时文件,
    共享路径在结构上不存在 —— 这比"给回档也加一把锁"更硬(不依赖调用方记得加锁)。
    """
    return '%s.tmp.%d.%d' % (SAVEFILE, os.getpid(), threading.get_ident())


def _drop_tmp(path):
    """清掉落盘临时文件(失败路径收尾用)。"""
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def _atomic_replace(tmp, dst, tries=8):
    """`os.replace(tmp, dst)` + **有界重试**。

    为什么需要: 本文件是多线程落盘(27001/27002 业务线程 + 18080 每请求一线程, 还有
    `autosave_snapshot()`/`backup_state()` 这类**读** SAVEFILE 的路径)。Windows 上
    `os.replace` 目标文件只要被别的线程以任何模式打开着, 就会抛
    `PermissionError(13, '拒绝访问。')` —— 实测在并发压力下必现(见 `_task4_severity_test.py` S2)。
    旧实现没有这一步: 失败会向上抛, **这一次落盘就整个丢了**。
    重试而不是放弃: 读侧都是"打开-读完-关"的短窗口, 几十毫秒就够。
    """
    import time as _t
    last = None
    for i in range(tries):
        try:
            os.replace(tmp, dst)
            return True
        except PermissionError as _e:      # Windows 共享冲突; 其它异常该抛就抛
            last = _e
            _t.sleep(0.03 * (i + 1))
    raise last


def restore_from_backup(name, expected_sha=None):
    """用快照**覆盖**生产存档 -> (ok, 说明)。

    安全检查 (顺序不可变):
      ① 名字必须是 `_backups/` 下的**纯文件名** (拒绝路径穿越);
      ② 目标存在且是合法 JSON dict;
      ③ 覆盖前**先把当前档另存一份** (tag=pre-restore) —— 回档本身也必须可回滚;
      ④ 若给了 expected_sha, 校验快照哈希一致 (防"看着是这份、实际不是");
      ⑤ 原子替换 (写 .tmp -> os.replace)。
    ⚠ 调用方负责: **停机/断连 + 重新 load_state()** —— 服务端把 EQ_STATE 放内存,
      只改文件不重载, 下一次 save_state 会把内存里的旧状态写回去 (v33 已实测这个坑)。
    """
    import shutil
    if not name or os.path.basename(name) != name or not name.endswith('.json'):
        return False, '快照名不合法 (只接受 _backups/ 下的文件名)'
    src = os.path.join(_BACKUP_DIR, name)
    if not os.path.isfile(src):
        return False, '快照不存在: %s' % name
    try:
        with open(src, 'rb') as f:
            b = f.read()
        d = json.loads(b.decode('utf-8'))
        if not isinstance(d, dict):
            return False, '快照不是 JSON 对象'
    except Exception as e:
        return False, '快照不可读: %r' % e
    sig = hashlib.sha256(b).hexdigest()[:16]
    if expected_sha and str(expected_sha)[:16] != sig:
        return False, '快照哈希不符 (期望 %s, 实际 %s) —— 拒绝回档' % (str(expected_sha)[:16], sig)
    if os.path.isfile(SAVEFILE):
        backup_state('pre-restore', note='回档前自动留底 (restore %s)' % name)
    try:
        # ★2026-10-02 (审计 §9 高severity #1): 与 save_state **共用同一把锁 + 独立临时文件**。
        #   _SAVE_LOCK 定义在本文件下文(存档写入互斥锁一段); 模块级名在**调用时**解析, 无顺序问题。
        with _SAVE_LOCK:
            tmp = _tmp_path()
            try:
                with open(tmp, 'wb') as f:
                    f.write(b)
                _atomic_replace(tmp, SAVEFILE)
            finally:
                _drop_tmp(tmp)
    except Exception as e:
        return False, '写回失败: %r' % e
    return True, '已用 %s 覆盖存档 %s (sha256=%s, %d 字节)' % (name, os.path.basename(SAVEFILE), sig, len(b))


def load_into_EQ_STATE(data):
    """把一份 state dict **原地**灌进全局 EQ_STATE (保持对象同一性)。

    为什么必须原地: 全工程几十处 `from state import EQ_STATE` —— 重新赋值会让那些
    引用还指向旧 dict (v33 §2.5 的 roleInfo 分叉就是这类"两份实现"的教训)。
    """
    if not isinstance(data, dict):
        return False
    mueq = data.get('monster_ueq')
    if isinstance(mueq, dict):
        for k, v in list(mueq.items()):
            if isinstance(v, list):
                mueq[k] = tuple(v)
    EQ_STATE.clear()
    EQ_STATE.update(data)
    return True


def reload_from_file(path=None):
    """按 SAVEFILE(或指定文件) 重新载入内存态 —— 回档后必须调它。

    读保护沿用 load_state 的语义: 非授权进程读生产档直接抛 (不返回 False),
    因为"读到一半的混合态"比直接失败危险得多。
    """
    p = path or SAVEFILE
    if _is_real_save(p) and not PROTECT_REAL_SAVEFILE:
        raise RuntimeError('reload_from_file(): 未授权进程试图读取生产存档 %s —— 拒绝。' % _REAL_SAVEFILE)
    with open(p, 'r', encoding='utf-8') as f:
        loaded = json.load(f)
    if not isinstance(loaded, dict):
        return False
    apply_migrations(loaded)
    load_into_EQ_STATE(loaded)
    # 与 load_state 一致的后置修复: 装备槽 tuple 化由 load_into_EQ_STATE 做了
    try:
        mueq = EQ_STATE.get('monster_ueq')
        if isinstance(mueq, dict):
            for _uc in [str(u) for u in _MONSTER_BASE] + [str(u) for u in (EQ_STATE.get('monsters') or {})]:
                mueq.setdefault(_uc, ('0', '0', '0'))
    except Exception:
        pass
    return True


def _is_real_save(path=None):
    # ★★2026-10-02 (审计 §11.2 / §13 排名 3, P0): 存档闸门的大小写/写法绕过。
    #   旧实现 `os.path.abspath(a) == os.path.abspath(b)` —— Windows 路径**大小写不敏感**,
    #   而 abspath 只做规范化、**不做大小写折叠** ⇒ 修前实测:
    #       _is_real_save(r'<repo>\ygzz\work\state.json')  -> True
    #       _is_real_save(r'<repo>\ygzz\work\STATE.JSON')  -> False  ★同一个物理文件
    #       _is_real_save(r'<REPO>\ygzz\work\state.json')  -> False  ★同一个物理文件
    #     而三者的 os.path.isfile() 全是 True ⇒ 改一下 SAVEFILE 的写法就能同时关掉
    #     读闸(:785)与写闸(:1296-1307), 直接退回 v26「临时脚本覆盖生产存档」的事故形态。
    #   修法: normcase(realpath()) —— realpath 解掉 `.`/`..`/符号链接, normcase 折叠大小写与斜杠。
    #   ⚠ 不要只加 normcase: `abspath` 不解析符号链接, 而 realpath 会, 两者都需要。
    try:
        return os.path.normcase(os.path.realpath(path or SAVEFILE)) == \
               os.path.normcase(os.path.realpath(_REAL_SAVEFILE))
    except Exception:
        return False

# 旧的 backup_state() 已由上面的 v36 版本取代 (去重快照 + 边车元数据 + 保留策略),
# 定义位置见本文件 "存档版本化" 段 —— 这里不再保留旧实现, 避免两个同名函数互相覆盖。

# ---------- 装备强化 CULTIVATE_INTENSITY(6006) 常量 ----------
# bag.baseType 枚举 (客户端 app\bag\BagConfig.lua) —— 用于构造/判断消耗材料列表
BASE_EQUIP = 1   # 装备
BASE_PROP  = 2   # 道具
ENHANCE_COST = 80  # 每次强化固定金币消耗

# 初始装备背包 (镜像 1030 EQUIP_BAG): ucode -> (static id, isload)
#   字段: [ucode,id,level,exp,0,0,islock,isload,0,0,0,0,0,0] (14字段, SQEquip:insertItem)
_EQUIP_DEFAULTS = {
    '1':(1001,1), '2':(1002,0), '3':(1003,0), '4':(1006,1), '5':(1004,1),
    '6':(1006,1), '7':(1002,1), '8':(1004,1), '9':(1006,1), '10':(1003,1), '11':(1004,1),
}
# 初始道具背包 (镜像 1033 PROP_BAG): ucode -> count
_PROP_DEFAULTS = {'1':5, '4000101':300, '4000201':200, '4000301':200,
                  '300002':5, '300003':5,   # 残章书(type33, 只可卖)
                  '5000101':150,            # 皮肤碎片(36, 50片合1); 其它碎片(头像)暂不 seed(客户端渲染未验证, 会卡死)
                  '1006':20}                # 体力道具(L02 1006, 探索/入侵/PVE/妖塔消耗; roleInfo 第5字段=剩余体力)
# 道具 ucode -> 静态ID (道具背包[ucode,id,count]); 与 1033 一致
#   '1'=1001(一般道具) ; 4000101=N级妖怪碎片(10片合1) ; 4000201=R级(20片合1) ; 4000301=SR级(30片合1)
_PROP_ID_DEFAULTS = {'1':1001, '4000101':4000101, '4000201':4000201, '4000301':4000301,
                     '300002':300002, '300003':300003, '5000101':5000101,
                     '1006':1006}
# 元灵背包 (GHOST_BAG 1031): [ucode, id, count, isload] (SQGhost:insertItem)
#   id 见 L03_Equipghost (1020-1025 元灵, scrap 碎片 100020-100025 x5片合1)
_GHOST_DEFAULTS = [
    ['1', 1020, 1, 0],
    ['2', 1021, 1, 0],
    ['3', 1022, 1, 0],
]
# 礼包/周卡月卡 (GIFT_BAG_GET 1097): [ucode, type(98礼包/97卡), id, count]
#   type 98=bag.baseType.giftbag (SDGiftbag) ; 97=bag.baseType.card (SDCardGift)
#   礼包 id1049一日赠礼/1050会员礼包 (L10_GiftInfo); 卡 id601周卡/602月卡 (G02_MyCard)
_GIFTBAG_DEFAULTS = [
    ['g1', 98, 1049, 1],
    ['g2', 98, 1050, 1],
    ['c1', 97, 601, 1],
    ['c2', 97, 602, 1],
]

# 装备 static id -> 类型 (type1=攻/ATK, type2=防/DEF, type3=命/HP)。默认 type1。
#   与 1030 装备背包对照: id1001/1002/1003=攻, id1004/1005=防, id1006=命
_EQUIP_TYPE = {1001:1, 1002:1, 1003:1, 1004:2, 1005:2, 1006:3}
# 类型 -> 怪物item字段 (7=HP,8=ATK,9=DEF) 和 monster_ueq 元组下标 (0=hp,1=atk,2=def)
_TYPE_FIELD   = {1:8, 2:9, 3:7}
_TYPE_UEQ_IDX = {1:1, 2:2, 3:0}

# 怪物静态基础 (ucode -> (mid, skin, num, skills)); 与 MONSTER_BAG_JSON 三只怪一致
_MONSTER_BASE = {
    '1': (100031, 11301, 100, [11001, 11006, 11011, 0, 0, 0]),
    '2': (200101, 20011, 100, [10236, 10226, 10061, 0, 0, 0]),
    '3': (201001, 20101, 100, [10356, 10361, 10366, 0, 0, 0]),
}

def _default_state():
    # 构造初始状态 (与原先 single-file EQ_STATE 默认值完全一致)
    return {
        'gold': 0 if _STARTER else 999999999,               # 初始档 0；测试档超大值保证强化判定不拦
        'eggcoin': 0,                                       # eggcoin (妖魂币)
        'soulcoin': 0 if _STARTER else 100000,              # 初始档 0；测试档 10 万
        'lv': {u:1 for u in _EQUIP_DEFAULTS},               # 装备 ucode -> level
        'exp': {u:0 for u in _EQUIP_DEFAULTS},              # 装备 ucode -> exp
        'equip_id': {u:_EQUIP_DEFAULTS[u][0] for u in _EQUIP_DEFAULTS},     # ucode -> static id
        'equip_isload': {u:_EQUIP_DEFAULTS[u][1] for u in _EQUIP_DEFAULTS}, # ucode -> isload
        'prop': dict(_PROP_DEFAULTS),                       # 道具 ucode -> count
        'prop_id': dict(_PROP_ID_DEFAULTS),                # 道具 ucode -> static id
        # ★v42: 种子道具"曾经发放过"的流水 —— 判据从"键不存在就补"改成"曾经拥有过"。
        #   新档把这些种子直接记为已发, 所以用光之后重启不会重发。
        'prop_seeded': [str(x) for x in _PROP_DEFAULTS.keys()],
        'ghost': [list(x) for x in _GHOST_DEFAULTS],       # 元灵 [[ucode,id,count,isload],..]
        'giftbag': ([] if _STARTER else [list(x) for x in _GIFTBAG_DEFAULTS]),  # 初始档不带免费礼包/周月卡
        # 每个怪物的 7-9 槽当前值 (hp, atk, def): 已装备=装备ucode字符串, 空槽=0
        #   与 MONSTER_BAG_JSON/_mon 初始 uequip 一致: 怪物"1"=("4","1","5") / "2"=("6","7","8") / "3"=("9","10","11")
        'monster_ueq': {
            '1': ('4', '1', '5'),
            '2': ('6', '7', '8'),
            '3': ('9', '10', '11'),
        },
        # ---------- 抽卡/召唤 (8001/8002) ----------
        'monsters': {},                # 抽到的怪: ucode -> [mid, skin, num, skills(6)]
        'monster_base_override': {},   # 初始怪进化覆盖: ucode -> [mid, skin, num, skills(6)] (6003 持久化, 防重启回退)
        'monster_lv': {},              # 妖怪 ucode -> 等级 (吞噬/养成升级, 持久)  默认1
        'monster_exp': {},             # 妖怪 ucode -> 当前等级累积经验 (持久)     默认0
        'monster_grow': {},            # 妖怪 ucode -> 成长值 (item字段6, 吞噬持久, 封顶GrowMax)
        'monster_add': {},             # 妖怪 ucode -> [命,攻,防,暴] 加成 (item字段10-13, 吞噬持久)
        'monster_skin_eq': {},         # 妖怪 ucode -> 当前穿戴皮肤id (SKIN_REPLACE 1501)
        'monster_skin_open': {},       # 妖怪 ucode -> [已解锁皮肤id,..] (SKIN_GET 1500)
        # ---------- 特性系统 (L97; 6007 单特性升级 / 6008 特性总等级) ----------
        'monster_feat': {},            # 妖怪 ucode -> [6] 槽: ''(空) 或 'id#lv#isUse' (客户端 analysisSpeciality '#'/'&' 格式)
        'monster_feat_src': {},        # ★v40 妖怪 ucode -> 种子出处 't1:t2' (或 '?'=来源不明/玩家动过); 换表重算用
        'feat_source_fp': '',          # ★v40 静态特性数据源指纹 (算法版本+两个源表哈希); 变了才触发重算
        'monster_featexp': {},         # 妖怪 ucode -> 特性总经验 (怪物item字段26, L97_1 阶梯)
        'monster_featlev': {},         # 妖怪 ucode -> 特性总等级 0-6 (怪物item字段27)
        # ---------- 周月卡 (1098; 文案: 使用后立即…, 每日6点邮件发放, 持续7/30日) ----------
        'card_daily': {},              # 'week'/'month' -> {'start': ts, 'granted': N} (N=已发放天数)
        # ---------- 技能 ----------
        'skills': {},                  # 技能袋 ucode -> [id, level, isopen, ischange?] (isload 由 monster_skills 推算)
        'monster_skills': {},          # 妖怪 ucode -> [6 技能袋ucode/0] 已习得技能(覆盖 L91 基础技能)
        'skill_produced': [],          # 1177 合成/掉落产生的技能袋 ucode (迁移保留集: 未装配的学习/合成技能不因重启被清)
        'next_skill_ucode': 1,         # 合成技能袋 ucode 计数器 (sk1, sk2, ...)
        # ---------- 礼包/皮肤/宝箱 (2026-09-07 G03 权威实装) ----------
        'skin_owned': {},              # 皮肤id -> 1 (G03 Type14 发放记录; SKIN_GET/替换校验用)
        'treasure_bag': {},            # 宝箱 ucode(=箱静态id) -> [静态箱id, count] (L103; 1060/1062; 与 L02 道具 id 同号不可放 prop)
        'next_monster_ucode': 4,       # 下一个可用怪物 ucode (初始3只占 1-3)
        'monster_deleted': [],         # 已被吞噬/移除的初始怪 ucode (初始3只不在 monsters 字典)
        'draw': {'ticket': 0 if _STARTER else 100, 'luck': 0, 'five_ensure': 0},  # 唤妖符/幸运值/五连保底标记
        # ---------- 玩家等级 ----------
        'player_lv': 1 if _STARTER else 8,   # 初始档 1 级；测试档 8 级（解锁挑战/道场/妖塔）
        'player_exp': 0,
        # ---------- 商城 ----------
        'shop_buy': {},                # 商品id -> 已购次数 (终身限购 LimitType=1 持久化)
        'next_giftbag_ucode': 1 if _STARTER else 3,       # 初始档从 g1 起；测试档 g1/g2 已被默认礼包占用
        'next_card_ucode': 1 if _STARTER else 3,          # 初始档从 c1 起；测试档 c1/c2 已被默认周/月卡占用
        # ---------- 任务 ----------
        'task_state': {},              # 任务id -> [当前完成值, 状态(0未完成 1可领 2已领)]
        'task_day': '',                # 每日任务重置日期 (YYYY-MM-DD)
        'kidou': 0,                    # 鬼道积分 (任务奖励等)
        # ---------- PVP ----------
        'pvp': {'teamnum': 1, 'mapid': 1, 'streak': 0, 'streak_max': 0,
                'streak_date': '', 'boxes': {'5': 0, '10': 0, '15': 0}, 'rank_day': ''},
        # ---------- 公会 ----------
        'sociaty': {'name': '', 'icon': 0, 'level': 1, 'rank': 0, 'wealth': 0, 'notice': '',
                    'id': '', 'created': '', 'todaydonate': 0, 'donate_date': ''},
        # ---------- 卡池BOSS ----------
        'cardboss': {'pray': 0, 'times': 0, 'date': ''},
        # ---------- 体力 (2026-09-08: 恢复计时 lazy; last_ts=恢复进度锚点(epoch秒), timerid=当前计时器唯一标识) ----------
        'vitality': {'buy': 0, 'buy_date': '', 't1': 0, 't2': 0, 't_date': '',
                     'last_ts': 0.0, 'timerid': 1},
        # ---------- 妖塔 ----------
        'tower': {'plies': 0, 'stars': {}},
        # ---------- 头像 ----------
        'head': {'own': {1001: 1}, 'eq': 1001, 'frame_eq': 1001, 'frame_own': {1001: 1}},
        # ---------- 手札已读 (1047 LETTER_UPDATE 真机确认客户端会发) ----------
        'letter_done': [],           # 已读手札 id 列表 (1047 记录; 1046 推送时置 state=1 -> 不再弹)
        # ---------- 2026-09-08 时间系统实装基座 ----------
        'vital_extra': {'day': '', 'val': 0},   # 奖励型活跃 (T02 ty21) 当日递补 (1068 今日活跃并入)
        'aggress': {'date': '', 'used': {}},    # 妖怪入侵: 每日免费挑战次数扣减持久化 (groupid -> 已用)
        'login7': {'days': 0, 'claimed': 0, 'last': ''},        # 8日登录 (A01 101; days=累计登录日)
        'signin30': {'days': 0, 'claimed': [], 'last': ''},     # 30日签到 (A21; claimed=已领 A21 全局id)
        'qualify': {'season': '', 'points': 0, 'reward': 0,     # 排位赛季: 积分/赛季奖励领取
                    'daily': '', 'daily_n': 0,                  # 今日剩余挑战
                    'week': '', 'week_win': 0, 'boxes': {},     # 本周胜次/周胜次箱
                    'report': []},                              # 今日战报 [[...]..]
        'nightmarket': {'token': 0, 'buy_date': '', 'buy': 0,   # 夜市: 信物/每日购买/进入次数
                        'join_date': '', 'join': 0},
        # ---------- 金币流转 (T37 26/27 每日"获得/消耗金币"任务; 2026-09-08 开启计数值) ----------
        'gold_flow': {'day': '', 'earn': 0, 'spend': 0},
        # ---------- 探索形象 (S20_ExploreBody; roleInfo[29]; 1184/1185/1186) ----------
        'explore_role_id': 1010,                       # 当前使用中的探索形象 BodyID (默认 江酌月)
        'explore_active': [],                          # 已激活的形象 BodyID 列表 (1184 列表)
    }

# 当前运行状态 (默认为初始值; 启动时 load_state() 覆盖为持久化值)
EQ_STATE = _default_state()

# ★★2026-10-02 (B 任务 · 多账号隔离): 多账号模式下把 EQ_STATE 换成"按账号分派读写"的代理。
#   只在 `YGZZ_MULTI_ACCOUNT=1` 时启用 —— **默认关闭**，于是改造前那 77 套"单档假设"的
#   回归网与生产行为都不受影响（这个开关的失败形态是"多账号没生效"，不是"单档被改坏"）。
#   代理**只在这里创建一次、永不重新绑定**：全工程几十处 `from state import EQ_STATE`
#   拿的是同一个引用（见下面 load_into_EQ_STATE 的 docstring 与 v33 §2.5 的教训）。
import accounts as _accounts
if _accounts.ENABLED:
    EQ_STATE = _accounts.EQ_STATE

def get_monster_base(ucode):
    # 怪物基础数据 (mid, skin, num, skills): 优先查进化持久化覆盖 -> 初始3只 -> 抽到的怪
    #   (2026-09-07: 初始怪进化(6003)只更新内存 _MONSTER_BASE 曾致重启回退, 现以 monster_base_override 持久化)
    ov = EQ_STATE.get('monster_base_override', {}).get(str(ucode))
    if ov:
        try:
            return (int(ov[0]), int(ov[1]), int(ov[2]), list(ov[3]))
        except Exception:
            pass
    if str(ucode) in EQ_STATE.get('monster_deleted', []):
        return None                      # 已被吞噬/移除的初始怪
    b = _MONSTER_BASE.get(ucode)
    if b is not None:
        return b
    m = EQ_STATE.get('monsters', {}).get(ucode)
    if m is None:
        return None
    mid, skin, num, skills = m
    return (mid, skin, num, list(skills))

def monster_base_in(data, ucode):
    """从**任意账号档 dict** 里取怪的基础数据 -> `(mid, skin, num, skills)` 或 None。

    与 `get_monster_base` **同口径**（override -> 全局初始 3 只 -> 本档 `monsters`），但**不读
    当前账号上下文**。给跨账号取数用（任务 D：1065/1066 要展示**别人**的怪）。

    ⚠ 为什么不 `bind()` 到别人再调 `get_monster_base`：`save_state()` 按**当前上下文**路由，
      在别人的上下文里触发落盘就会写**别人的档** —— 这是任务 D 明令禁止的形态。
      ⇒ 跨账号一律"只读数据 dict"。
    """
    try:
        d = data if isinstance(data, dict) else {}
        uc = str(ucode)
        ov = (d.get('monster_base_override') or {}).get(uc)
        if ov:
            try:
                return (int(ov[0]), int(ov[1]), int(ov[2]), list(ov[3]))
            except Exception:
                pass
        if uc in (d.get('monster_deleted') or []):
            return None
        b = _MONSTER_BASE.get(uc)
        if b is not None:
            return b
        m = (d.get('monsters') or {}).get(uc)
        if m is None:
            return None
        mid, skin, num, skills = m
        return (mid, skin, num, list(skills))
    except Exception:
        return None


def all_monster_ucodes_in(data):
    """任意账号档里的全部怪 ucode（与 `all_monster_ucodes` 同口径，只看传入的 dict）。"""
    try:
        d = data if isinstance(data, dict) else {}
        dead = d.get('monster_deleted') or []
        ucs = [u for u in sorted(_MONSTER_BASE.keys(), key=int) if str(u) not in dead]
        ucs += sorted((d.get('monsters') or {}).keys(), key=int)
        return ucs
    except Exception:
        return []


def ensure_monster_ueq(ucode):
    """怪物装备槽 (hp/atk/def 3 格) 的**唯一**读取+幂等补种入口。★v34 新增 (bug#1 根因修复)

    事故复盘 (2026-09-13 真机, 用户报"妖怪装不上装备, 只有哪吒和月主能装"):
      造怪在工程里有 **4 条路径**, 其中 2 条 (handlers_game 商城直发 gt4 / 宝箱抽怪
      `_add_monster_here`) 只写 `monsters[uc]`、**没写 `monster_ueq[uc]`**;
      另 2 条 (handlers_bag._add_monster / handlers_draw 抽卡) 写了。
      → 该怪在 1032 里正常显示 (monster_bag_json 兜底 uequip=('0','0','0')), 但一上装:
        handle_equip_load 前置校验 `monsterucode not in EQ_STATE['monster_ueq']` → `[0,{}]`
        → 客户端 NetEquipLoad.m_errorCode[0] = "LOCAL_UI_SYSTEM_108325" = **"错误，消息体为空"**。
      备份存档实测: ucode 107 (阴主-三山) / 176 (血红蝶-芝罘) 正是那两条路径造出来的 (num=1)。

    修法 (两层, 缺一不可):
      ① 造怪处统一登记 (handlers_game 两处已补) —— 治本;
      ② **谁读谁补种** (本函数) —— 治已存在的坏存档 + 防将来又冒出新路径 (与 ensure_features
         / add_box 同款惰性模式)。补种只写 ('0','0','0') 空槽, 不动任何既有穿戴。
    返回: 3 元组槽位; 该 ucode 根本不是怪物 -> None。
    """
    ucode = str(ucode)
    mueq = EQ_STATE.setdefault('monster_ueq', {})
    v = mueq.get(ucode)
    if isinstance(v, (list, tuple)):
        if len(v) >= 3:
            return tuple(v[:3])
        # 形状坏 (历史脚本可能塞过 20 元组/短元组) -> 规整成 3 格, 保留前 3 个
        fixed = tuple([str(x) if x not in (None, '') else '0' for x in list(v)[:3]] + ['0'] * 3)[:3]
        mueq[ucode] = fixed
        return fixed
    if get_monster_base(ucode) is None:
        return None
    if v is not None:
        print('[state] WARN monster_ueq[%s] 形状异常 (%r) -> 重置为 3 空格' % (ucode, v), flush=True)
    mueq[ucode] = ('0', '0', '0')
    return mueq[ucode]

def get_monster_skin(ucode):
    # 当前应展示的皮肤id: 穿戴皮肤(monster_skin_eq, 1501 写入) 优先, 否则基础皮肤(L91/L98 阶段外形)
    #   ★2026-09-11 (v21 §3.5 "皮肤穿戴后不固定"): 服务器共有 5 处下发怪物 skin 字段
    #     (1032 怪袋 / 3200 布阵 / 掉落 / PVP 阵容 / 编队), 旧实现只有 _mon_with_state 读 eq_skin,
    #     其余走 get_monster_base 的基础皮肤 -> 同一只怪在不同界面显示不同皮肤(阴主·三山"变回原皮肤")。
    #     统一用本函数取皮肤, 保证"穿上即固定"。
    b = get_monster_base(ucode)
    if b is None:
        return 0
    try:
        eq = (EQ_STATE.get('monster_skin_eq') or {}).get(str(ucode))
        if eq:
            return int(eq)
    except Exception:
        pass
    try:
        return int(b[1])
    except Exception:
        return 0

def get_monster_lv(ucode):
    # 妖怪等级 (未记录则默认1)
    try:
        return int(EQ_STATE.get('monster_lv', {}).get(ucode, 1))
    except Exception:
        return 1

def get_monster_exp(ucode):
    try:
        return int(EQ_STATE.get('monster_exp', {}).get(ucode, 0))
    except Exception:
        return 0

def get_monster_grow(ucode):
    try:
        return int(EQ_STATE.get('monster_grow', {}).get(ucode, 0))
    except Exception:
        return 0

def get_monster_add(ucode):
    # [命,攻,防,暴] 加成
    try:
        a = EQ_STATE.get('monster_add', {}).get(ucode)
        if isinstance(a, (list, tuple)) and len(a) >= 4:
            return [int(x) for x in a[:4]]
    except Exception:
        pass
    return [0, 0, 0, 0]

def set_monster_grow_add(ucode, grow, addhp, addatk, adddef, addcri):
    EQ_STATE['monster_grow'][ucode] = int(grow)
    EQ_STATE['monster_add'][ucode] = [int(addhp), int(addatk), int(adddef), int(addcri)]

def all_monster_ucodes():
    # 全部怪物 ucode (初始3只在前, 抽到的按 ucode 数值升序; 已吞噬移除的不含)
    ucs = [u for u in sorted(_MONSTER_BASE.keys(), key=int) if str(u) not in EQ_STATE.get('monster_deleted', [])]
    ucs += sorted(EQ_STATE.get('monsters', {}).keys(), key=int)
    return ucs

def ensure_skills():
    # 技能袋 = 已拥有技能: 怪物自带技能(装配 isload=1, 名称/等级解析源, 怪面板依赖) + 学习/合成(未装配 isload=0)
    #   客户端背包技能页(NetSkillBag/SkillBase:getSkillList)只显示 isload==0 的技能 -> 自带技能天然不进背包页
    s = EQ_STATE.setdefault('skills', {})
    ms = EQ_STATE.setdefault('monster_skills', {})
    for uc in all_monster_ucodes():
        b = get_monster_base(uc)
        if not b:
            continue
        if uc not in ms:
            ms[uc] = ([str(sid) if sid else '0' for sid in b[3]] + ['0'] * 6)[:6]
        for sid in b[3]:
            if not sid:
                continue
            k = str(sid)
            if k not in s:
                s[k] = [sid, 1, 1]
    # 2026-09-07 补: 当前怪 monster_skills 装配引用必须全部存在于袋
    #   (怪 item 字段14-19 / 替换1505 / 强化1504 均以袋 ucode 为准; 旧存档曾出现装配但袋缺9条)
    for slots in ms.values():
        for u in slots:
            if u and str(u) != '0' and str(u) not in s:
                try:
                    s[str(u)] = [int(u), 1, 1]
                except Exception:
                    pass
    # ★★2026-10-11 更正我自己上一版的一个**错误修法**（留在原处, 免得下一轮重犯）:
    #   我曾在这里"幂等补齐 `skill_produced` 里背包没有的项"，写的是 `s[k] = [int(k), 1, 1]`
    #   —— 而 `sk1` 这种 ucode **不是数字**，于是落进 `except` 兜底写成 `[0, 1, 1]`，
    #   把**技能 id = 0**（非法）灌进了**生产存档**。实测生产档真的多了两条
    #   `sk1 / sk8 -> [0,1,1]` ⇒ **修法本身在造脏数据**，比不修更坏。
    #   真正的约束: 技能袋条目的槽 0 是**技能 id**（合成时取 `_PIECES[碎片id]['lv1_row']`，
    #   见 `handlers_skill.py:159`），而存档里**没有**记录"这本书是哪个碎片合成的"
    #   ⇒ **光凭 ucode 无法重建**，任何"补齐"都只能是编造。
    #   ⇒ 正确处置: **不补**，只把"悬空"报出来。悬空项既不会被 `purge_skill_bag` 清掉
    #     （它把 `skill_produced` 当保留集读）、也永远进不了袋 —— 那是一致的状态，
    #     不是需要"修"的数据损坏。
    _prod = EQ_STATE.get('skill_produced') or []
    if isinstance(_prod, list):
        _dangling = [str(x) for x in _prod
                     if str(x) and str(x) != '0' and str(x) not in s]
        if _dangling:
            print('[SKILL] 注意: skill_produced 有 %d 项不在技能袋中: %s '
                  '（不清理、也不重建 —— 存档没记"它是哪个碎片合成的", 见 state.py 注释）'
                  % (len(_dangling), _dangling), flush=True)
    return s

def purge_skill_bag():
    # 2026-09-07 迁移: 清理技能袋历史污染条目
    #   根因: 旧调试/清洗脚本(_fix_state_backup/_restore_state)按"历史拥有过的怪"基础技能灌入,
    #   吞噬怪后技能残留(键只 pop 怪 ucode, 袋键是技能 id -> 永不命中), 导致"从未拥有的怪的自带技能"
    #   以 isload=0 出现在技能页 (实测量: 55 条中 30 条不属于任何当前怪 b[3]).
    #   规则: 保留 ①当前任何所拥有怪 b[3] 的非零技能 ②当前任何怪 monster_skills 装配的技能(学习/替换产出)
    #        其余剔除 (含历史怪/图鉴怪技能).
    #   注意: 1504 强化只改袋内条目([id,lv,isopen]), 不新增键; 1503/1505 只重排装配 -> 均不影响保留集.
    s = EQ_STATE.get('skills')
    if not isinstance(s, dict) or not s:
        return 0
    # ★★2026-10-18（真机：背包出现两本 "Dummy" 技能书）：技能袋条目的槽 0 是**技能静态 id**，
    #   id<=0 的行任何客户端表都查不到 ⇒ 背包技能页显示 "Dummy"。历史来源是 2026-10-11
    #   那次"幂等补齐 skill_produced"的错误修法（见 `ensure_skills` 注释）：`sk1/sk8`
    #   这种非数字 ucode 被 `int()` 失败后写成 `[0,1,1]` 灌进了生产档。
    #   这种行**无法重建**（存档没记它是哪个碎片合成的），正确处置 = 删行 + 从
    #   `skill_produced` 里摘掉那个 ucode（否则下面的保留集又会把它当成"产出物"）。
    _bad = set()
    for _k in list(s.keys()):
        _row = s.get(_k)
        try:
            _sid = int(_row[0]) if isinstance(_row, (list, tuple)) and _row else 0
        except Exception:
            _sid = 0
        if _sid <= 0:
            s.pop(_k, None)
            _bad.add(str(_k))
    if _bad:
        print('[SKILL] 清理非法技能行(id<=0): %s' % sorted(_bad), flush=True)
        _pl = EQ_STATE.get('skill_produced')
        if isinstance(_pl, list):
            EQ_STATE['skill_produced'] = [x for x in _pl if str(x) not in _bad]
    keep = set()
    for uc in all_monster_ucodes():
        b = get_monster_base(uc)
        if b:
            for sid in b[3]:
                if sid:
                    keep.add(str(sid))
    for slots in (EQ_STATE.get('monster_skills') or {}).values():
        for u in slots:
            if u and str(u) != '0':
                keep.add(str(u))
    # 2026-09-07(第二轮) 技能学习/合成链: 1177 产生的未装配技能书必须保留 (与 _purge_skill_bag_data 同规则)
    for _sc in (EQ_STATE.get('skill_produced') or []):
        keep.add(str(_sc))
    removed = [k for k in s if str(k) not in keep]
    for k in removed:
        s.pop(k, None)
    return len(removed)

## ---------------------------------------------------------------- 初始怪补发
## ★★2026-10-20（主线 21016 卡死取证）：**江酌月(300601) 是建号自带的初始怪**，不是任务奖励。
##   证据：① `L90_Initialise`（"初始化"表）**只有一行** = 300601 江酌月（皮肤/技能 30061 /
##   10586,10591）；② 主线 21016「江酌月·主角的光环-1」在第 2 章就要求"用它升到 5 级"；
##   ③ `T02_Reward` 里 300601 与「江酌月·魂」(4300601) **都是 0 命中** ⇒ 没有任何任务发得出来；
##   ④ 魂的唯一产出是抽卡转盘（`D201_DoubleDrawItem` 活动 8/20）—— 主线不可能要求每个玩家先抽卡。
##   ⇒ 本仓的老档（含模板档）缺这一行，按**幂等迁移**补：只补"从没有过这只怪"的档。
##   ⚠ 已经被玩家当材料吞噬掉的不复活：`init_mon_ver` 一旦打过就不再发。
_JZY_MID = 300601
_JZY_ROW = [300601, 30061, 100, [10586, 10591, 0, 0, 0, 0]]   # 同 handlers_draw._MON_STATIC[300601]
_JZY_VER = 1


def ensure_initial_monsters(data):
    """补发初始怪「江酌月」；幂等。返回 1 = 本次补了，0 = 没动。"""
    if not isinstance(data, dict):
        return 0
    try:
        if int(data.get('init_mon_ver') or 0) >= _JZY_VER:
            return 0
    except Exception:
        pass
    mons = data.get('monsters')
    if not isinstance(mons, dict):
        mons = data['monsters'] = {}
    for row in mons.values():
        if isinstance(row, (list, tuple)) and row and str(row[0]) == str(_JZY_MID):
            data['init_mon_ver'] = _JZY_VER
            return 0
    for row in _MONSTER_BASE.values():          # 初始 3 只也可能就是它（防重复）
        if row and str(row[0]) == str(_JZY_MID):
            data['init_mon_ver'] = _JZY_VER
            return 0
    n = 4
    try:
        n = max(4, int(data.get('next_monster_ucode') or 4))
    except Exception:
        n = 4
    for k in mons:
        try:
            n = max(n, int(k) + 1)
        except Exception:
            continue
    while str(n) in _MONSTER_BASE:
        n += 1
    uc = str(n)
    mons[uc] = list(_JZY_ROW)
    data.setdefault('monster_ueq', {})[uc] = ['0', '0', '0']
    data.setdefault('monster_lv', {})[uc] = 1
    data.setdefault('monster_exp', {})[uc] = 0
    data['next_monster_ucode'] = n + 1
    data['init_mon_ver'] = _JZY_VER
    return 1


def _purge_skill_bag_data(data):
    # load_state 迁移阶段专用: 对尚未装载 EQ_STATE 的 data dict 执行技能体系清理
    # (与 purge_skill_bag 规则一致, 但直接遍历 data 中的 monsters/deleted/monster_skills)
    # 2026-09-07 附加: 一并清除 monster_skills 中"已删除怪"残留键 (实测 23 键 vs 4 当前怪).
    s = data.get('skills')
    ms = data.get('monster_skills')
    deleted = set(str(x) for x in (data.get('monster_deleted') or []))
    # ★★2026-10-18：迁移阶段同样先扔掉 `skills` 里 id<=0 的非法行，并把它们的 ucode 从
    #   `skill_produced` 摘掉（与 `purge_skill_bag` 同规则；真机 "Dummy" 技能书就是这么来的）。
    if isinstance(s, dict) and s:
        _bad = set()
        for _k in list(s.keys()):
            _row = s.get(_k)
            try:
                _sid = int(_row[0]) if isinstance(_row, (list, tuple)) and _row else 0
            except Exception:
                _sid = 0
            if _sid <= 0:
                s.pop(_k, None)
                _bad.add(str(_k))
        if _bad and isinstance(data.get('skill_produced'), list):
            data['skill_produced'] = [x for x in data['skill_produced'] if str(x) not in _bad]
    cur_ucs = [str(u) for u in _MONSTER_BASE if str(u) not in deleted]
    cur_ucs += [str(u) for u in (data.get('monsters') or {})]
    _cur = set(cur_ucs)
    removed_keys = 0
    if isinstance(ms, dict):
        for uc in [str(k) for k in ms.keys()]:
            if uc not in _cur:
                del ms[uc]
                removed_keys += 1
    keep = set()
    # 初始三只 (不在 monsters 字典, 用 monster_deleted 标记)
    for uc, b in _MONSTER_BASE.items():
        if str(uc) not in deleted:
            for sid in b[3]:
                if sid:
                    keep.add(str(sid))
    # 抽到的怪 (monsters: ucode -> [mid, skin, num, skills(6)])
    for uc, m in (data.get('monsters') or {}).items():
        if not isinstance(m, (list, tuple)) or len(m) < 4:
            continue
        for sid in m[3]:
            if sid:
                keep.add(str(sid))
    # 当前怪装配 (清理后的 monster_skills)
    for slots in (ms or {}).values():
        for u in (slots or []):
            if u and str(u) != '0':
                keep.add(str(u))
    # 2026-09-07(第二轮) 技能学习/合成链: 1177 产生的未装配技能书必须保留
    #   (技能袋 = 怪自带技能 ∪ 学习/合成产出; 学习/合成产出仅由 1502/1177 写入, 无怪引用)
    for _sc in (data.get('skill_produced') or []):
        keep.add(str(_sc))
    n_s = 0
    if isinstance(s, dict) and s:
        removed = [k for k in s if str(k) not in keep]
        for k in removed:
            s.pop(k, None)
        n_s = len(removed)
    return n_s, removed_keys

def skill_isload_map():
    # ucode -> isload: 任一怪物已装配该技能即 1
    m = {}
    for uc, slots in (EQ_STATE.get('monster_skills') or {}).items():
        for u in slots:
            u = str(u) if u is not None else '0'
            if u and u != '0':
                m[u] = 1
    return m

def load_state():
    # 启动时读取 SAVEFILE; 不存在则沿用默认值。原地更新 EQ_STATE,
    # 使所有 `from state import EQ_STATE` 的模块保持引用同一个 dict 对象。
    # ★★2026-09-12 (v26) 读保护: 未授权进程**不得**把生产存档载入内存 ——
    #   事故链是 "临时脚本 load 生产存档 -> 某 handler save_state -> 覆盖";
    #   写闸门已拦住后半段, 这里再拦住前半段: 非服务端进程读到生产存档就直接抛,
    #   让它**立刻暴露**而不是带着用户数据继续跑 (避免"读出真实数据 -> 写回默认"的混合态)。
    if _is_real_save() and not PROTECT_REAL_SAVEFILE:
        raise RuntimeError(
            'load_state(): 未授权进程试图读取生产存档 %s —— 拒绝。\n'
            '  临时脚本/单测请改用隔离存档:\n'
            '    set YGZZ_SAVEFILE=...\\_xxx_state.json    (或 state.SAVEFILE = ...)\n'
            '  只有 game_server.py 启动时会置 state.PROTECT_REAL_SAVEFILE = True。' % _REAL_SAVEFILE)
    data = _default_state()
    # ★v42: `prop_seeded` 要用"**文件里到底有没有**"来判断老档, 不能用内存默认值 ——
    #   _default_state() 已经给了它一份"全部种子"的默认值, 直接看内存的话
    #   "老档迁移"分支永远不会触发(功能仍对, 但日志/版本记账就成了死代码)。
    #   先置 None, 后面用文件覆盖; 文件没有 -> 保持 None -> 迁移分支正常接手。
    data['prop_seeded'] = None
    if os.path.isfile(SAVEFILE):
        try:
            with open(SAVEFILE, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                # 用加载值覆盖默认值; 缺失字段保留默认 (兼容旧/新字段差异)
                for k, v in loaded.items():
                    data[k] = v
        except Exception as _e:
            # ★★2026-10-02 (审计 §9 高severity #4): **禁止**静默降级成默认档。
            #   旧实现是 `except Exception: pass  # 文件损坏则忽略, 用默认值` —— 后果链:
            #     存档损坏 -> 以 `player_lv=8` 的**空档**启动 -> 服务端照常接受连接
            #     -> 任意一次 save_state() 就把空档**覆盖**到生产档上。
            #   唯一的缓解是启动时的 backup_state(), 而那份快照与损坏档之间隔着整个启动过程,
            #   运维看到的现象是"服务器起来了, 玩家数据全没了" —— 最坏的一种失败方式。
            #   存档是本项目唯一不可再生的资产 ⇒ 宁可**拒绝启动**, 并要求人工处置。
            #   (先复制一份现场: 即使人工要分析, 也不必去动原文件。)
            import shutil as _sh, time as _t
            _q = '%s.corrupt-%s' % (SAVEFILE, _t.strftime('%Y%m%d-%H%M%S'))
            try:
                _sh.copy2(SAVEFILE, _q)
            except Exception:
                _q = '(现场另存失败)'
            raise RuntimeError(
                'load_state(): 存档 %s **解析失败** —— 拒绝以默认档启动。\n'
                '  为什么拒绝: 用空档启动后, 任何一次 save_state() 都会把空档覆盖到生产档,\n'
                '              而存档是本项目唯一不可再生的资产。\n'
                '  原始异常: %r\n'
                '  现场已另存: %s\n'
                '  处置建议: ① 看 %s 里的 state-*.json 快照(服务端每次启动/每次写盘前都会留)\n'
                '            ② 或从 /admin 后台回档; 确认后再把正确的档放回 %s 并启动。'
                % (SAVEFILE, _e, _q, _BACKUP_DIR, SAVEFILE))
    # JSON 中 monster_ueq 的值是 list, 还原为 tuple (与默认结构一致)
    mueq = data.get('monster_ueq')
    if not isinstance(mueq, dict):
        mueq = {}
        data['monster_ueq'] = mueq
    for k, v in mueq.items():
        if isinstance(v, (list, tuple)):
            data['monster_ueq'][k] = tuple(v)
    # ---- ★v34 迁移: 给"没有装备槽记录"的怪补 ('0','0','0') ----
    #   根因见 ensure_monster_ueq() 的注释: 商城直发 gt4 / 宝箱抽怪 两条造怪路径漏登记
    #   monster_ueq, 导致这些怪**永久无法上装** (6004 → [0,{}] → "错误，消息体为空")。
    #   这里对存量存档做一次幂等回填 (不动任何既有槽位值), 只补缺失的键。
    _deleted0 = set(str(x) for x in (data.get('monster_deleted') or []))
    _alive0 = [str(u) for u in _MONSTER_BASE if str(u) not in _deleted0]
    _alive0 += [str(u) for u in (data.get('monsters') or {})]
    _backfilled = []
    for _uc in _alive0:
        if _uc not in mueq:
            mueq[_uc] = ('0', '0', '0')
            _backfilled.append(_uc)
    global _V34_BACKFILL
    _V34_BACKFILL = _backfilled
    if _backfilled:
        print('[state] v34 迁移: 回填缺失的 monster_ueq 装备槽 %s (原本这些怪一上装就报"消息体为空")'
              % sorted(_backfilled, key=lambda x: int(x) if x.isdigit() else 0), flush=True)
    # 道具背包迁移(兼容旧 state.json 无 prop_id): 为每个道具 ucode 补 id。
    #   已有 prop_id 的用其值; 缺失的默认 1001 (ucode=='1') 或 ucode 本身(碎片/大ID)。
    prop = data.get('prop') or {}
    pid = data.get('prop_id')
    if not isinstance(pid, dict):
        pid = {}
        data['prop_id'] = pid
    for uc in list(prop.keys()):
        if str(uc) not in pid:
            pid[str(uc)] = 1001 if str(uc) == '1' else int(str(uc))
    # 道具背包迁移: 种子道具**只在首次发放, 之后即使玩家用光也不再补**。
    # ★v42 修 (那条一直挂着的口子): 旧判据是"**键当前不存在**就补" ——
    #   玩家把某个种子道具用光后, **每次重启都会被重发一份**
    #   (v40 重启预演里实测补的是 5000101 密藏皮肤·魄 x150, 等于可无限刷)。
    #   正确判据是"**曾经拥有过**": 把已发过的 id 记进 data['prop_seeded'], 之后永不重发。
    #   迁移口径: 老档没有 prop_seeded -> **把 _PROP_DEFAULTS 全部一次性标记为"已发过"**,
    #   不补发任何一个。理由: 旧代码在**每次装载**都会补, 所以"现在缺的"必然是玩家**花掉的**,
    #   而不是"从没发过" -> 补发就是凭空造物。 (新档走 _default_state(), 本来就自带这些种子。)
    _seeded = data.get('prop_seeded')
    if not isinstance(_seeded, list):
        _seeded = [str(x) for x in _PROP_DEFAULTS.keys()]
        data['prop_seeded'] = _seeded
        print('[state] v42 道具种子迁移: 标记 %d 个默认种子为"已发放" -> 以后用光不再重发'
              % len(_seeded), flush=True)
    _seen = set(str(x) for x in _seeded)
    for uc, cnt in _PROP_DEFAULTS.items():
        u = str(uc)
        if u not in _seen:
            if u not in prop:
                prop[u] = cnt          # 真正"从没发过"的才补
            _seeded.append(u)          # 补过/已有 -> 都记为已发, 之后不再补
            _seen.add(u)
        if u not in pid:
            pid[u] = _PROP_ID_DEFAULTS.get(uc, int(uc))
    # 礼包/卡背包迁移: 缺失则补默认 (GIFT_BAG_GET 1097)
    gb = data.get('giftbag')
    if not isinstance(gb, list):
        data['giftbag'] = [list(x) for x in _GIFTBAG_DEFAULTS]
    # 元灵背包迁移: 缺失则补默认 (GHOST_BAG 1031)
    gh = data.get('ghost')
    if not isinstance(gh, list):
        data['ghost'] = [list(x) for x in _GHOST_DEFAULTS]
    # ---- ★v51: 元灵"完整件不堆叠" —— 碎片可堆叠, 合成出来的完整元灵一格一个 ----
    #   用户裁定 (2026-09-13): "是包含所有元灵，是碎片合成出来后的完整元灵都被这个影响，
    #     要不然要重复启动游戏才正确显示和使用。"
    #   背景: v39 的 merge_ghost_rows 当年是为"同一物品占多格"加的**一刀切**修复 ——
    #     它把**完整元灵**也一起合并了。而完整元灵 (L03_Equipghost 的 1020-1054) 是
    #     由碎片 (L02 Type=31, 每件需 5~25 个碎片) 合成出来的**成品**,
    #     官方语义是"一格一个"; 只有散装碎片/低阶才堆叠。
    #   ★ 现在改为 split_ghost_rows: 把 count>1 的完整元灵行拆成 N 行各 1 个。
    #     **数量总和严格守恒** —— 只改"占几格", 不产生也不销毁任何元灵。
    #   ⚠ 刻意**不再**把 merge_ghost_rows 挂在加载路径上: 它会立刻把刚拆开的又并回去。
    #     v39 要防的"同一元灵占多格"只在**碎片类**上才是 bug, 而碎片类现在仍然由
    #     add_ghost 正常合并 (见 add_ghost 的 allow_stack)。
    _ghsplit = split_ghost_rows(data)
    for _c in _ghsplit:
        print('[state] v51 完整元灵拆分(不堆叠): %s' % _c, flush=True)
    global _V51_GHOST_SPLIT
    _V51_GHOST_SPLIT = list(_ghsplit)
    #   发号计数器补齐: 老档没有 next_ghost_ucode; 且历史遗留的 max(ghN)+1 与
    #   next_giftbag_ucode 两套发号互不推进 -> 这里按现有最大值补一次, 杜绝重号。
    if not isinstance(data.get('next_ghost_ucode'), int):
        _ghmax = 0
        for _g in (data.get('ghost') or []):
            try:
                _u = str(_g[0])
                if _u.startswith('gh') and _u[2:].isdigit():
                    _ghmax = max(_ghmax, int(_u[2:]))
            except Exception:
                continue
        data['next_ghost_ucode'] = _ghmax + 1
    # ---- ★v40 (P0-1) 静态特性: 数据源指纹 + 一次性重算 ----
    #   只在指纹真的变了时才动手 (老档没有 feat_source_fp -> 第一次会建立它并只做"出处回填",
    #   不会改动任何槽 —— 因为 backfill 用的就是当前算法)。详见函数头注释。
    _featch = recompute_features_on_source_change(data)
    for _c in _featch:
        print('[state] v40 特性数据源: %s' % _c, flush=True)
    global _V40_FEAT_CHANGED
    _V40_FEAT_CHANGED = list(_featch)
    # 商城购买记录/礼包计数器迁移 (旧存档无此字段)
    if not isinstance(data.get('shop_buy'), dict):
        data['shop_buy'] = {}
    # ---- giftbag ucode 去重迁移 (2026-09-06 真机 bug: 默认 g1/g2 与商店赠品 g1/g2 同号,
    #      _use_giftbag 匹配到第一个 count=0 条目 -> "消息体为空"; 重命名冲突条目为 g%d 新号) ----
    gb = data.get('giftbag')
    if isinstance(gb, list):
        seen = {}
        for item in gb:
            if not (isinstance(item, list) and len(item) >= 4):
                continue
            uc = str(item[0])
            if uc in seen:
                # 同号冲突: 若已有条目 count>0 优先保留, 冲突条目换新号
                nxt = data.get('next_giftbag_ucode', 1) if uc.startswith('g') else data.get('next_card_ucode', 1)
                pre = 'g' if uc.startswith('g') else 'c'
                item[0] = pre + str(nxt)
                if pre == 'g':
                    data['next_giftbag_ucode'] = nxt + 1
                else:
                    data['next_card_ucode'] = nxt + 1
                seen[item[0]] = True
            else:
                seen[uc] = True
    # 计数器从现有最大号继续 (防再次冲突)
    def _max_uc(pre):
        vs = []
        for item in (data.get('giftbag') or []):
            if isinstance(item, list) and len(item) >= 2 and isinstance(item[0], str) \
                    and item[0].startswith(pre) and item[0][1:].isdigit():
                vs.append(int(item[0][1:]))
        return (max(vs) + 1) if vs else 1
    data['next_giftbag_ucode'] = max(int(data.get('next_giftbag_ucode', 1)), _max_uc('g'))
    data['next_card_ucode'] = max(int(data.get('next_card_ucode', 1)), _max_uc('c'))
    # 旧bug产物迁移: 进化道具(2005-2008)曾被误发为 97卡 -> 转正为道具背包 (用户已付费)
    _gb = data.get('giftbag')
    if isinstance(_gb, list):
        _fixed = []
        _prop = data.setdefault('prop', {})
        _pid = data.setdefault('prop_id', {})
        for item in _gb:
            try:
                if len(item) >= 4 and int(item[1]) == 97 and int(item[2]) in (2005, 2006, 2007, 2008):
                    _uc = str(item[2])
                    _prop[_uc] = _prop.get(_uc, 0) + int(item[3] or 0)
                    _pid.setdefault(_uc, int(item[2]))
                    continue
            except Exception:
                pass
            _fixed.append(item)
        data['giftbag'] = _fixed
    if 'task_state' not in data or not isinstance(data.get('task_state'), dict):
        data['task_state'] = {}
    # ---- 2026-09-07 私服改造: 保证测试资源 (旧存档经多次迁移可能丢失) ----
    #   1) 周卡(601)/月卡(602) 背包卡 c1/c2 必须存在且 count>=1 (1097 礼包列表 / 1098 使用依赖)
    #   2) 妖豆 eggcoin 充足 (消耗品页妖豆礼包购买)
    #   3) 特性槽位 v2: 按 L97 UnLockLevel 类别放置 (旧存档槽位是"硬塞1/2"错误布局 -> 强制重播种)
    _gb2 = data.setdefault('giftbag', [])
    if not isinstance(_gb2, list):
        _gb2 = []
        data['giftbag'] = _gb2
    if not _STARTER:
        _have_card = any(isinstance(x, list) and len(x) >= 3 and str(x[0]) in ('c1', 'c2') for x in _gb2)
        if not _have_card:
            _gb2.append(['c1', 97, 601, 1])
            _gb2.append(['c2', 97, 602, 1])
            data['next_card_ucode'] = max(int(data.get('next_card_ucode', 1)), 3)
        else:
            for _x in _gb2:
                if isinstance(_x, list) and len(_x) >= 4 and str(_x[0]) in ('c1', 'c2') and int(_x[3]) <= 0:
                    _x[3] = 1
        if int(data.get('eggcoin', 0)) < 100000:
            data['eggcoin'] = 999999
    if data.get('feat_class_v2') is not True:
        data['monster_feat'] = {}
        data['feat_class_v2'] = True
    # 清理旧实现产物: 礼包被误存为 97卡 (g5=2002 等) —— 直接发奖后不再需要
    _gb3 = data.get('giftbag')
    if isinstance(_gb3, list):
        _gb3[:] = [x for x in _gb3
                   if not (isinstance(x, list) and len(x) >= 3 and str(x[1]) == '97'
                           and str(x[2]) in ('2002', '2004'))]
    # ---- 2026-09-07 月卡任务补偿(v3): 已领取(防刷)但当时未发礼包卡 -> 补"每日妖怪礼包"(2048)x2 ----
    #   幂等: 仅当背包无 2048 卡时补一张 (领取后次日再领正常走 ty=98 发卡)
    _ts2 = data.setdefault('task_state', {})
    _t1017 = _ts2.get('1017')
    _gb4 = data.setdefault('giftbag', [])
    if not isinstance(_gb4, list):
        _gb4 = []
        data['giftbag'] = _gb4
    _prop2 = data.setdefault('prop', {})
    _prop2.pop('9002048', None)                       # 清理上一版直发碎片残留
    if isinstance(_t1017, list) and len(_t1017) >= 2 and _t1017[1] == 2 \
            and not any(isinstance(x, list) and len(x) >= 3 and str(x[2]) == '2048' and int(x[3]) > 0
                        for x in _gb4):
        _gb4.append(['g%d' % data.get('next_giftbag_ucode', 1), 98, 2048, 2])
        data['next_giftbag_ucode'] = int(data.get('next_giftbag_ucode', 1)) + 1
    if 'task_day' not in data:
        data['task_day'] = ''
    if 'kidou' not in data:
        data['kidou'] = 0
    if 'monster_feat' not in data or not isinstance(data.get('monster_feat'), dict):
        data['monster_feat'] = {}
    if 'monster_feat_src' not in data or not isinstance(data.get('monster_feat_src'), dict):
        data['monster_feat_src'] = {}          # ★v40 老档没有 -> 由重算/backfill 首次建立
    if 'monster_featexp' not in data or not isinstance(data.get('monster_featexp'), dict):
        data['monster_featexp'] = {}
    if 'monster_featlev' not in data or not isinstance(data.get('monster_featlev'), dict):
        data['monster_featlev'] = {}
    if 'card_daily' not in data or not isinstance(data.get('card_daily'), dict):
        data['card_daily'] = {}
    if 'pvp' not in data or not isinstance(data.get('pvp'), dict):
        data['pvp'] = {'teamnum': 1, 'mapid': 1, 'streak': 0, 'streak_max': 0,
                       'streak_date': '', 'boxes': {'5': 0, '10': 0, '15': 0}, 'rank_day': ''}
    else:
        pvp = data['pvp']
        pvp.setdefault('teamnum', 1)
        pvp.setdefault('mapid', 1)
        pvp.setdefault('streak', 0)
        pvp.setdefault('streak_max', 0)
        pvp.setdefault('streak_date', '')
        pvp.setdefault('boxes', {'5': 0, '10': 0, '15': 0})
        pvp.setdefault('rank_day', '')
    if 'sociaty' not in data or not isinstance(data.get('sociaty'), dict):
        data['sociaty'] = {'name': '', 'icon': 0, 'level': 1, 'rank': 0, 'wealth': 0, 'notice': '',
                           'id': '', 'created': '', 'todaydonate': 0, 'donate_date': ''}
    else:
        sg = data['sociaty']
        for k, v in (('name', ''), ('icon', 0), ('level', 1), ('rank', 0), ('wealth', 0),
                     ('notice', ''), ('id', ''), ('created', ''), ('todaydonate', 0), ('donate_date', '')):
            sg.setdefault(k, v)
    if 'cardboss' not in data or not isinstance(data.get('cardboss'), dict):
        data['cardboss'] = {'pray': 0, 'times': 0, 'date': '', 'bosses': {}, 'active': 0, 'active_day': ''}
    else:
        cb = data['cardboss']
        cb.setdefault('pray', 0)
        cb.setdefault('times', 0)
        cb.setdefault('date', '')
        # ★v59 (2026-09-16) 异界之门 28 关: 逐 BOSS 计数/祈愿 + 当天展示的 BOSS
        #   旧档只有 pray/times/date -> 这里补空容器; 老 BOSS(1001) 的历史 times 归到它自己名下,
        #   语义 = "1001 今天已挑战 N 次"（旧版是全局 N 次, 对单 BOSS 玩家完全等价）。
        if not isinstance(cb.get('bosses'), dict):
            cb['bosses'] = {}
        cb.setdefault('active', 0)
        cb.setdefault('active_day', '')
        cb.setdefault('free_used', 0)             # 当日免费挑战总消耗（统计用；可用性按逐BOSS times 判）
        if cb['times'] and '1001' not in cb['bosses']:
            cb['bosses']['1001'] = {'pray': int(cb.get('pray') or 0),
                                    'times': int(cb.get('times') or 0),
                                    'date': cb.get('date') or ''}
    if 'vitality' not in data or not isinstance(data.get('vitality'), dict):
        data['vitality'] = {'buy': 0, 'buy_date': '', 't1': 0, 't2': 0, 't_date': ''}
    else:
        vt = data['vitality']
        vt.setdefault('buy', 0)
        vt.setdefault('buy_date', '')
        vt.setdefault('t1', 0)
        vt.setdefault('t2', 0)
        vt.setdefault('t_date', '')
        vt.setdefault('last_ts', 0.0)
        vt.setdefault('timerid', 1)
    if 'tower' not in data or not isinstance(data.get('tower'), dict):
        data['tower'] = {'plies': 0, 'stars': {}}
    else:
        data['tower'].setdefault('plies', 0)
        data['tower'].setdefault('stars', {})
    if 'head' not in data or not isinstance(data.get('head'), dict):
        data['head'] = {'own': {1001: 1}, 'eq': 1001, 'frame_eq': 1001, 'frame_own': {1001: 1}}
    else:
        hd = data['head']
        hd.setdefault('own', {1001: 1})
        hd.setdefault('eq', 1001)
        hd.setdefault('frame_eq', 1001)
        hd.setdefault('frame_own', {1001: 1})
        if not hd['own']:
            hd['own'][1001] = 1
        if not hd['frame_own']:
            hd['frame_own'][1001] = 1
        # JSON 键字符串化归一 (head/frame 字典键必须是 int)
        hd['own'] = {int(k): v for k, v in hd['own'].items()}
        hd['frame_own'] = {int(k): v for k, v in hd['frame_own'].items()}
    if 'treasure_bag' not in data or not isinstance(data.get('treasure_bag'), dict):
        data['treasure_bag'] = {}      # 宝箱独立容器 ucode -> [boxId, count] (L103 id 与 L02 道具 id 同号, 不能放 prop)
    if not isinstance(data.get('letter_done'), list):
        data['letter_done'] = []
    if not isinstance(data.get('monster_deleted'), list):
        data['monster_deleted'] = []
    # ---- 2026-09-08 时间系统基座迁移 ----
    if not isinstance(data.get('vital_extra'), dict):
        data['vital_extra'] = {'day': '', 'val': 0}
    if not isinstance(data.get('aggress'), dict):
        data['aggress'] = {'date': '', 'used': {}}
    if not isinstance(data.get('login7'), dict):
        data['login7'] = {'days': 0, 'claimed': 0, 'last': ''}
    if not isinstance(data.get('signin30'), dict):
        data['signin30'] = {'days': 0, 'claimed': [], 'last': ''}
    if not isinstance(data.get('qualify'), dict):
        data['qualify'] = {'season': '', 'points': 0, 'reward': 0, 'daily': '', 'daily_n': 0,
                           'week': '', 'week_win': 0, 'boxes': {}, 'report': []}
    if not isinstance(data.get('nightmarket'), dict):
        data['nightmarket'] = {'token': 0, 'buy_date': '', 'buy': 0, 'join_date': '', 'join': 0}
    if 'player_lv' not in data:
        data['player_lv'] = 1
    if 'player_exp' not in data:
        data['player_exp'] = 0
    # 初始档 1 级；测试档 8 级（解锁挑战/冲虚道场 Lv4、妖塔 Lv5）
    _min_lv = 1 if _STARTER else 8
    if int(data.get('player_lv', 1)) < _min_lv:
        data['player_lv'] = _min_lv
        data['player_exp'] = 0
    if 'next_giftbag_ucode' not in data:
        data['next_giftbag_ucode'] = max(
            [int(u[1:]) for u, *_ in (data.get('giftbag') or []) if isinstance(u, str) and u.startswith('g')] + [0]) + 1
    if 'next_monster_ucode' not in data:
        data['next_monster_ucode'] = max([int(u) for u in (data.get('monsters') or {})] + [0]) + 1
    if not isinstance(data.get('treasure_bag'), dict):
        data['treasure_bag'] = {}
    # ---- 2026-09-07(第二轮) 迁移①: 道具背包 ucode 统一合并 ----
    #   根因: 历史发放路径曾用 900xxx 前缀 ucode (900001 等), 与静态id ucode 共存 ->
    #   同一种道具在背包拆成多格, 进化界面只认其中一格 (用户: "哪吒材料 4/6 但背包明明有36个")
    #   规则: 同 pid 的多个 ucode 合并计数到 静态id ucode, 删除历史 ucode
    _prop = data.setdefault('prop', {})
    _pid = data.setdefault('prop_id', {})
    _by_pid = {}
    for _uc in list(_prop.keys()):
        _p = _pid.get(_uc)
        if _p is None or not str(_p).isdigit():
            continue
        _by_pid.setdefault(str(_p), []).append((str(_uc), int(_prop[_uc])))
    _merged_n = 0
    for _p, _items in _by_pid.items():
        if len(_items) <= 1:
            continue
        _total = sum(c for _, c in _items)
        for _uc, _ in _items:
            if _uc != _p:
                _prop.pop(_uc, None)
                _pid.pop(_uc, None)
        _prop[_p] = _total
        _pid[_p] = int(_p)
        _merged_n += 1
    if _merged_n:
        print('[state] merge_prop ucodes=%d' % _merged_n, flush=True)
    # ---- 2026-09-07(第二轮) 迁移②: 吞噬残留装备释放 (isload=1 但无存续怪 ueq 引用) ----
    _deleted2 = set(str(x) for x in (data.get('monster_deleted') or []))
    _ref = set()
    for _uc, _u in (data.get('monster_ueq') or {}).items():
        if str(_uc) in _deleted2:
            continue
        for _x in (_u or ()):
            if _x and str(_x) != '0':
                _ref.add(str(_x))
    _el = data.setdefault('equip_isload', {})
    _released = 0
    for _e in list(_el.keys()):
        if int(_el[_e] or 0) == 1 and str(_e) not in _ref:
            _el[_e] = 0
            _released += 1
    if _released:
        print('[state] release orphan equip_isload=%d' % _released, flush=True)
    # ---- 2026-09-07 技能袋迁移: 清历史污染 (见 _purge_skill_bag_data) ----
    #   注意: 必须在 EQ_STATE 装载前对 data 执行 (EQ_STATE['monsters'] 尚未更新)
    if not isinstance(data.get('skill_produced'), list):
        data['skill_produced'] = []
    if 'next_skill_ucode' not in data:
        # 从现有 sk* 技能袋 ucode 最大号续 (旧档迁移)
        _sk = [int(u[2:]) for u in (data.get('skills') or {}) if str(u).startswith('sk') and str(u)[2:].isdigit()]
        data['next_skill_ucode'] = (max(_sk) + 1) if _sk else 1
    _removed, _rkeys = _purge_skill_bag_data(data)
    if _removed or _rkeys:
        print('[state] purge_skill_bag removed_skills=%d removed_mon_skill_keys=%d' % (_removed, _rkeys), flush=True)
    # ---- 2026-10-20 初始怪补发: 江酌月(300601) 是建号自带（见 ensure_initial_monsters） ----
    if ensure_initial_monsters(data):
        print('[state] grant initial monster 江酌月(300601)', flush=True)
    # ---- ★v39: 把内联迁移段的成果正式记进"存档结构版本" ----
    #   补的缺口: apply_migrations() 此前**只在 reload_from_file() 里被调用** ——
    #   也就是说"正常重启服务端"这条路径永远不会推进 state_version, 存档一直挂着旧版本号
    #   (v36 P0-1 的版本化在重启路径上其实是空的; 实测: 内联迁移已合并元灵, 版本号仍是 1)。
    #   必须在 EQ_STATE.update(data) **之前**调用 —— 铁律 #100: 迁移要落在 data 上,
    #   否则 save_state 会把旧的 EQ_STATE 写回磁盘。
    _ver_before = state_version_of(data)
    _mig_msgs = apply_migrations(data)
    _ver_after = state_version_of(data)
    for _mig_msg in _mig_msgs:
        print('[state] %s' % _mig_msg, flush=True)
    if _accounts.ENABLED:
        # ★★2026-10-02 (B 任务): 多账号启动路径 —— 这份档不是"某人的档"，而是**新账号的模板**。
        #   做法：先 bind 一个**空 data** 的种子账号，让下面原有的 clear()+update(data) 与
        #   后续的 ensure_skills / 回填 / 落盘全部照常作用在它身上，最后把它当 seed 交给 accounts。
        #   ⚠ 必须用**空** dict 起步：直接 bind 一个 data 就是它自己的账号，
        #     `clear()` 会把 data 清空、紧接着 `update(data)` 就什么也灌不进去（静默得到空档）。
        _seed_acc = _accounts.Account('__seed__', '__seed__', {})
        EQ_STATE.bind(_seed_acc)
        _accounts.set_seed(_seed_acc.data)
    EQ_STATE.clear()
    EQ_STATE.update(data)
    _LAST_GOLD_FALLBACK[0] = int(EQ_STATE.get('gold', 0))    # 落盘差追踪基线 (save_state 净差累计当日金币流转)
    ensure_skills()   # 技能袋幂等种子 (从已拥有怪物的基础技能, 之后由各 handler 调用保持幂等)
    # ---- ★v34: 回填结果立即落盘 (只在真发生回填的那一次) ----
    #   不做的话, 磁盘存档直到"下一次业务写操作"才更新 —— 中途重启会再回填一次 (幂等, 无害),
    #   但 @admin/外部只读工具看到的仍是"缺槽"的旧档, 容易误判。
    #   必须在 EQ_STATE.update(data) **之后**调用: 否则 save_state 会把**旧的 EQ_STATE** 写回磁盘 (毁档)。
    if _V34_BACKFILL:
        try:
            save_state()
            print('[state] v34 回填已落盘: %s' % _V34_BACKFILL, flush=True)
        except Exception as _e:
            print('[state] v34 回填落盘失败 (不影响运行; 下次业务写操作仍会写入): %r' % _e, flush=True)
    # ---- ★v39: 元灵堆叠合并的结果也立即落盘 ----
    #   为什么必须显式落盘: 合并只改了内存(data -> EQ_STATE), 而 EQ_STATE 要等到"下一次业务写操作"
    #   才会写回磁盘。不落盘的话: 磁盘上仍是 5 行/旧版本号, 外部只读工具(含 /admin 概览、
    #   以及任何直接读 state.json 的排查脚本)看到的都是**没合并**的样子, 容易误判成"没生效"。
    #   (实测: 重启后客户端收到的 1031 已经是合并态 n=59, 但 state.json 还停在旧结构。)
    #   必须在 EQ_STATE.update(data) **之后**调用 (铁律 #100 的反向约束: 这里正好相反 ——
    #   落盘写的是 EQ_STATE, 所以内存必须先装好, 否则会把旧内存写回磁盘 = 毁档)。
    if _V51_GHOST_SPLIT:
        try:
            save_state()
            print('[state] v51 完整元灵拆分已落盘 (%d 条)' % len(_V51_GHOST_SPLIT), flush=True)
        except Exception as _e:
            print('[state] v51 拆分落盘失败 (不影响运行; 下次业务写操作仍会写入): %r' % _e, flush=True)
    # ---- ★v40: 特性重算/指纹 也立即落盘 (同 v39 的理由: 否则外部只读工具看到的是旧结构) ----
    #   注意: 首次为老档"建立指纹"也会走到这里 (只是没有槽变动) —— 那也必须落盘,
    #   否则每次启动都会重新 backfill 一遍, 且永远显示"没有指纹"。
    if _V40_FEAT_CHANGED:
        try:
            save_state()
            print('[state] v40 特性数据源已落盘 (%d 条)' % len(_V40_FEAT_CHANGED), flush=True)
        except Exception as _e:
            print('[state] v40 落盘失败 (不影响运行; 下次业务写操作仍会写入): %r' % _e, flush=True)
    # ---- ★v42: 迁移链本身产生了变更(版本号推进/字段新增) -> 立即落盘 ----
    #   与 v39/v40 同一类坑: 内存已经对了, 磁盘还挂着旧结构, 外部只读工具会误判"没生效"。
    #   ⚠ 判据必须用**版本号有没有变**, 不能只看 apply_migrations 返回的**消息列表** ——
    #      `_mig_v1`/`_mig_v4` 这类"只打版本号"的迁移**不返回消息**, 只看消息会漏掉它们
    #     (第一版就是这么写的, 结果版本号升到 4 了磁盘还写着 3)。
    #   稳定档上版本号不变 -> 不会每次启动都写盘。
    if _mig_msgs or _ver_after != _ver_before:
        try:
            save_state()
            print('[state] 迁移链变更已落盘 (state_version=%d)' % state_version_of(), flush=True)
        except Exception as _e:
            print('[state] 迁移链落盘失败 (不影响运行; 下次业务写操作仍会写入): %r' % _e, flush=True)

def safe_groups(g):
    # ⚠️ 历史遗留 (原注释声称"客户端 CJson2Table 会解包单元素数组"——经客户端源码核实为误判:
    #    CJson2Table = cjson.decode, 无任何数组解包; NetDropRewardData:refreshDropInfo 用
    #    ipairs/pairs 直接遍历, 单元素组/单元素 goodslist 天然无碍)。
    #    原实现 len==1 时返回 [g], 导致所有调用方服务器端解包崩溃 / 客户端 goodsInfo 变成表。
    #    保留本函数仅为兼容历史 import; 业务代码必须直接原样传数组 (见 handlers_game._drop)。
    return g

# ★★2026-10-12 (多人单服 · 跨账号基线): 这原本是**模块级全局** `[None]` —— 全进程一个值。
#   单档时代它是对的；多账号下它是**串账的**：
#     玩家 A 落盘后基线 = A 的金币；玩家 B 下一次落盘时 `_prev` 是 **A 的金币**，
#     于是 `_d = B的金币 − A的金币` 被记进 **B 的** `gold_flow`。
#     实测形态：B 什么都没干，`gold_flow.earn/spend` 却凭空涨一大截；
#     而 `handlers_opsact._consume_count()` 正是读 `gold_flow.spend` 去判"消费活动进度" ⇒
#     **活动进度会凭别人的金币变动而变**（玩家可见，且完全查不出原因）。
#   做法：基线按**账号 id** 存。三个理由：
#     ① 不写进账号档（否则每个账号都多一个内部字段，且"它该等于多少"没有语义）；
#     ② 不用 contextvars —— 基线是"这个账号上次落盘时的金币"，跨线程共享才是对的
#        （同一账号的两条连接落盘时必须看到同一个基线，否则又会算错差值）；
#     ③ 退出时清空，避免长跑进程里账号数无界增长。
_LAST_GOLD = {}
_LAST_GOLD_GUARD = threading.RLock()
_LAST_GOLD_FALLBACK = [None]      # 未启用多账号（或未绑定）时的单档基线；与改造前等价


def _gold_baseline_key():
    """当前上下文的基线键：多账号下是账号 id，否则是 `None`（单档）。"""
    if not _accounts.ENABLED:
        return None
    acc = _accounts.EQ_STATE.current()
    return acc.id if acc is not None else None


def _gold_baseline_get():
    if not _accounts.ENABLED:
        return _LAST_GOLD_FALLBACK[0]
    key = _gold_baseline_key()
    if key is None:
        return _LAST_GOLD_FALLBACK[0]
    with _LAST_GOLD_GUARD:
        return _LAST_GOLD.get(key)


def _gold_baseline_set(v):
    if not _accounts.ENABLED:
        _LAST_GOLD_FALLBACK[0] = v
        return
    key = _gold_baseline_key()
    if key is None:
        _LAST_GOLD_FALLBACK[0] = v
        return
    with _LAST_GOLD_GUARD:
        _LAST_GOLD[key] = v


def gold_baseline_reset(aid=None):
    """清掉某个账号（缺省：当前账号）的金币基线。断开连接时调用。

    为什么断开要清：不清的话，同一账号下一次登录时 `_prev` 还是**上次会话**最后的金币，
    中间若发生过运营补发/回档，差值会被算成"今天赚的"。
    """
    if aid is None:
        aid = _gold_baseline_key()
    with _LAST_GOLD_GUARD:
        if aid is None:
            return _LAST_GOLD_FALLBACK[0]
        return _LAST_GOLD.pop(aid, None)

# ★v34: load_state 回填了哪些怪的 monster_ueq 槽 (空 = 无需落盘); 见 load_state 末尾
_V34_BACKFILL = []
# ★v39: (已退役) 曾用于"元灵行合并"; 保留变量名仅为兼容旧排查脚本, 不再写入
_V39_GHOST_MERGED = []
# ★v51: load_state 把哪些"完整元灵"的堆叠拆成了一格一个 (空 = 无需落盘); 见 load_state 末尾
_V51_GHOST_SPLIT = []
# ★v40: load_state 因"数据源指纹变化"重算了哪些怪的特性 (空 = 无需落盘); 见 load_state 末尾
_V40_FEAT_CHANGED = []

# ★★2026-09-13 存档写入互斥锁 (v32)
#   为什么必须有: 服务端是**多线程**的 —— start_game_socket() 给 27001 和 27002 各起一个
#   accept 线程, 每个连接再起一个 handle_game 线程; 18080 的 ThreadingHTTPServer 也是每请求一线程。
#   而 save_state() 的落盘用的是**固定临时文件名** SAVEFILE+'.tmp':
#       tmp = SAVEFILE + '.tmp' ; json.dump(dump, tmp) ; os.replace(tmp, SAVEFILE)
#   两个线程同时进来 -> 两次 json.dump 交错写同一个 tmp -> os.replace 把**半截 JSON** 换成正式存档。
#   后果是存档损坏(而且有备份也未必能及时发现)。单客户端时不易触发, 但 27002/18080 都是敞开的。
#   ★2026-10-02 (审计 §9 高severity #1): 临时文件名已改为 `_tmp_path()`(**每次唯一**,
#     含 pid+线程号), 并让 `restore_from_backup()` 也持本锁 —— 双保险:
#     锁解决"同进程并发", 唯一名解决"任何忘记加锁的路径"。
#   用 RLock 而不是 Lock: backup_state() 在持锁路径上被调用, 将来若内部再调 save_state 也不会自锁死。
_SAVE_LOCK = threading.RLock()

# ★★2026-10-02 (审计 §9 高severity #3 / §13 排名 7): 实例 id 取号的**唯一原子入口**。
#   修前实测: 5 处调用点各自做"读-改-写"或"扫描 max+1", **没有任何互斥** ——
#     · handlers_bag.py:979   `uc = str(EQ_STATE['next_monster_ucode']); EQ_STATE[...] += 1`
#     · handlers_draw.py:250  同上
#     · handlers_game.py:460 / :1272  `ucnt = EQ_STATE.get(...); EQ_STATE[...] = ucnt + 1`
#     · handlers_bag.py:588   `_next_equip_ucode()` = max(在用号) + 1
#   而服务端是**多线程**的(27001/27002 各一个 accept 线程 + 每连接一线程, 18080 每请求一线程)
#   ⇒ 两个线程同时发怪/发装备会**拿到同一个 ucode**, 后写的把先写的整只覆盖掉。
#   用户现象是"抽到了但包里没有 / 少了一只", 而**日志上什么都看不出来**(物品凭空消失)。
#   ⇒ 全部收敛到下面两个函数: 持锁 + 单调 + **跳过已占用号**
#     (即使计数器是旧的/被手工改过, 也不会撞上在用的号)。
_ALLOC_LOCK = threading.RLock()
# 装备发号游标: **只在内存**(不落盘) —— 避免给存档平添一个新键(存档键数有测试在盯)。
_NEXT_EQUIP_UC = [0]


def alloc_monster_ucode():
    """怪物 ucode 发号(原子 + 单调 + 跳过占用)。返回 str。"""
    with _ALLOC_LOCK:
        mons = EQ_STATE.get('monsters') or {}
        try:
            n = int(EQ_STATE.get('next_monster_ucode'))
        except Exception:
            n = 0
        if n <= 0:
            n = max([int(u) for u in mons if str(u).isdigit()] + [0]) + 1
        while str(n) in mons:
            n += 1
        EQ_STATE['next_monster_ucode'] = n + 1
        return str(n)


def alloc_equip_ucode():
    """装备 ucode 发号(原子 + 单调 + 跳过占用)。返回 str。

    旧实现(`handlers_bag._next_equip_ucode`)是"扫描在用号取 max+1":
    并发下两次调用必然返回同一个号; 即使不并发, 调用方在 insert 之前再取一次也会撞号。
    """
    with _ALLOC_LOCK:
        eq = EQ_STATE.setdefault('equip_id', {})
        n = _NEXT_EQUIP_UC[0]
        if n <= 0:
            ucs = [int(u) for u in eq.keys() if str(u).isdigit()]
            n = (max(ucs) + 1) if ucs else 1
        while str(n) in eq:
            n += 1
        _NEXT_EQUIP_UC[0] = n + 1
        return str(n)

def save_state():
    # 全程持锁: gold_flow 净差累计 + 备份 + 序列化 + 原子替换 必须是一个不可分割的动作。
    # ⚠ 锁的**持有范围**只包含落盘, 不含各 handler 的状态修改 (那需要全局事务, 不在本次范围内);
    #   本锁解决的是"存档文件被写坏", 不是"业务状态并发读改写"。
    with _SAVE_LOCK:
        _save_state_locked()

def _save_state_locked():
    # 每次改状态后写入 SAVEFILE (monster_ueq 的 tuple 序列化为 list, 便于 JSON)
    # 2026-09-08: 顺带按"距上次落盘的净差"累计 当日获得/消耗金币 (T37 26/27 任务计数源; 无需改各调用点)
    _g = int(EQ_STATE.get('gold', 0))
    _prev = _gold_baseline_get()
    if _prev is not None and _g != _prev:
        gf = EQ_STATE.setdefault('gold_flow', {'day': '', 'earn': 0, 'spend': 0})
        import time as _t
        _today = _t.strftime('%Y-%m-%d')
        if gf.get('day') != _today:
            gf['day'] = _today
            gf['earn'] = 0
            gf['spend'] = 0
        _d = _g - _prev
        if _d > 0:
            gf['earn'] = int(gf.get('earn', 0)) + _d
        else:
            gf['spend'] = int(gf.get('spend', 0)) + (-_d)
    _gold_baseline_set(_g)
    # ★2026-10-12 (串档排查): 写盘时"到底绑了谁 + 基线是多少"是唯一能一击定论的证据。
    #   ⚠⚠ **绝不能**为了留痕去 `import game_server`：那样会重跑它的模块级代码，
    #      其中 `sys.stdout = _Tee(sys.stdout, …)` 会把**调用方的 stdout 换掉**
    #      （实测 `_saveguard_selftest` 立刻报 `'_Tee' object has no attribute 'buffer'`，
    #      而且日志被偷偷写进生产 server-out.log）。留痕必须**自包含**。
    try:
        _acc = EQ_STATE.current() if _accounts.ENABLED else None
        _accounts.trace('save_state aid=%s gold=%s prev=%s flow=%s'
                        % (getattr(_acc, 'id', '<单档>'), _g, _prev,
                           EQ_STATE.get('gold_flow')))
    except Exception:
        pass
    # ★★2026-10-02 (B 任务): 多账号模式下**一人一档** —— 写当前账号的文件，
    #   不碰生产 state.json，也就不走单档那套写保护闸门（它保护的是"那一个文件"）。
    #   `REGISTRY` 尚未 enable（服务端启动阶段）时**故意落回**单档路径：那时写的正是
    #   作为模板的那份 state.json，行为与改造前一致。
    #   未绑定账号时 `save_current()` 会抛 AccountsError —— **拒绝**而不是静默写进别人的档。
    if _accounts.ENABLED and _accounts.REGISTRY is not None:
        _accounts.REGISTRY.save_current()
        return
    # ★★2026-09-12 (v26) 写保护闸门 (顺序: 隔离声明 -> 服务端授权 -> 备份)
    if _is_real_save():
        if TEST_ISOLATED:
            raise RuntimeError(
                'save_state(): 生产存档 %s 在"测试隔离"模式下被写入 —— 拒绝。\n'
                '  说明: YGZZ_TEST_ISOLATED=1 表示这是测试进程, 必须先把 state.SAVEFILE 指到 _*.json。'
                % _REAL_SAVEFILE)
        if not PROTECT_REAL_SAVEFILE:
            raise RuntimeError(
                'save_state(): 生产存档 %s 被一个**未授权**进程写入 —— 拒绝。\n'
                '  说明: 只有 game_server.py 启动时会置 state.PROTECT_REAL_SAVEFILE = True。\n'
                '  临时脚本/单测请先 `state.SAVEFILE = <隔离文件>` + `os.environ["YGZZ_TEST_ISOLATED"]="1"`,\n'
                '  或用 `YGZZ_SAVEFILE=<隔离文件> python <script>` 启动。' % _REAL_SAVEFILE)
    if _is_real_save() and PROTECT_REAL_SAVEFILE:
        autosave_snapshot()
    dump = dict(EQ_STATE)
    mueq = EQ_STATE.get('monster_ueq')
    if isinstance(mueq, dict):
        dump['monster_ueq'] = {k: list(v) for k, v in mueq.items()}
    # ★2026-10-02 (审计 §9 高severity #1): 临时文件唯一化(旧: 固定 SAVEFILE+'.tmp',
    #   与 restore_from_backup 共用 -> /admin 回档与业务线程并发可换上半截 JSON)。
    tmp = _tmp_path()
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(dump, f, ensure_ascii=False, indent=2)
        _atomic_replace(tmp, SAVEFILE)
    finally:
        _drop_tmp(tmp)

def vitality_timerid():
    # roleInfo[13] 恢复体力计时器唯一标识 (客户端 SQGamer TimerId; 3002 回传校验; handlers_vitality 递增)
    try:
        return int((EQ_STATE.get('vitality') or {}).get('timerid', 1))
    except Exception:
        return 1

# 体力上限。**唯一出处**：`role_info_array()` 第 17 个字段（1-based）= 20，客户端
#   `SQGamer:getMaxVitality()` 读的就是它。凡是"体力到顶就不许再进账"的地方
#   （商城体力商品 / 礼包体力 / grant_one 飘字）都必须引用本常量，别再各写一个字面量 20
#   —— 2026-10-16 的 B 包就是因为三处各写 20、而只有一处报的是真实入账量。
VITALITY_MAX = 20

# ---------- 金币流转计数 (T37 ct26 累计获得 / ct27 累计消耗; 每日滚动) ----------
def gold_flow_reset():
    gf = EQ_STATE.setdefault('gold_flow', {'day': '', 'earn': 0, 'spend': 0})
    import time as _t
    today = _t.strftime('%Y-%m-%d')
    if gf.get('day') != today:
        gf['day'] = today
        gf['earn'] = 0
        gf['spend'] = 0
    return gf

def gold_earned(n):
    g = gold_flow_reset()
    g['earn'] = int(g.get('earn', 0)) + max(0, int(n or 0))

def gold_spent(n):
    g = gold_flow_reset()
    g['spend'] = int(g.get('spend', 0)) + max(0, int(n or 0))

def gold_earn_today():
    return int(gold_flow_reset().get('earn', 0))

def gold_spend_today():
    return int(gold_flow_reset().get('spend', 0))

def explore_role_id():
    try:
        return int(EQ_STATE.get('explore_role_id', 1010))
    except Exception:
        return 1010

# ---------- 装备静态数据 (L01_Equipments; 2026-09-08 全表载入 ----------
#   旧实现仅硬编码 1001-1006 类型 -> 其余装备(商城/邮件/礼包)一律默认攻型 -> 怪物三槽位只能攻槽,
#   "三个槽位却只能使用一个槽位"根因. 客户端 EquipList/EquipInfo 按 SDEquip:getStatic_Type() 过滤槽位.
_EQUIP_TYPE_TABLE = None
_EQUIP_LEVEL_TABLE = None
_EQUIP_RATIO_TABLE = None
_EQUIP_LVLIMIT_TABLE = None      # ★v34: L01.LvLimit (穿戴门槛; 客户端 SDEquip:getStatic_LvLimit)
_EQUIP_CLASS_TABLE = None        # ★v34: L01.Class (品级; 客户端 SDEquip:getStatic_Quality 即取此列)

def _load_equip_static():
    global _EQUIP_TYPE_TABLE, _EQUIP_LEVEL_TABLE, _EQUIP_RATIO_TABLE
    global _EQUIP_LVLIMIT_TABLE, _EQUIP_CLASS_TABLE
    if _EQUIP_TYPE_TABLE is not None:
        return
    _EQUIP_TYPE_TABLE, _EQUIP_LEVEL_TABLE, _EQUIP_RATIO_TABLE = {}, {}, {}
    _EQUIP_LVLIMIT_TABLE, _EQUIP_CLASS_TABLE = {}, {}
    try:
        import json as _j
        d = os.path.join(table_dir(), 'L01_Equipments.json')
        raw = open(d, 'rb').read().decode('utf-8', 'replace')
        i = raw.rfind(']')
        t = _j.loads(raw[:i + 1])
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                _eid = int(r[h['ID']])
                _EQUIP_TYPE_TABLE[_eid] = int(r[h['Type']] or 1)
                _EQUIP_LEVEL_TABLE[_eid] = int(r[h['Level']] or 30)
                _EQUIP_RATIO_TABLE[_eid] = float(r[h['ExpRatio']] or 1.0)
                # LvLimit: 穿戴等级门槛。99 = 客户端 EquipList 里被过滤掉的"限定强化石"
                #   (EquipList:initData `if limitlv ~= 99 then` / EquipInfo 同款)
                _EQUIP_LVLIMIT_TABLE[_eid] = int(float(r[h['LvLimit']] if r[h['LvLimit']] not in (None, '') else 1))
                _EQUIP_CLASS_TABLE[_eid] = int(float(r[h['Class']] or 1))
            except Exception:
                continue
    except Exception:
        pass

def equip_lv_limit(eid):
    # 穿戴等级门槛 L01.LvLimit (99 = 该装备不进上装列表)
    _load_equip_static()
    eid = int(eid) if str(eid).isdigit() else 0
    return _EQUIP_LVLIMIT_TABLE.get(eid, 1)

def equip_class_static(eid):
    # 品级 L01.Class (客户端 SDEquip:getStatic_Quality() -> m_Class; EquipList 排序第一关键字)
    _load_equip_static()
    eid = int(eid) if str(eid).isdigit() else 0
    return _EQUIP_CLASS_TABLE.get(eid, 1)

def equip_type_static(eid):
    # 装备类型: 1攻/2防/3命 (L01.Type; 缺省回退旧硬编码 -> 1)
    _load_equip_static()
    eid = int(eid) if str(eid).isdigit() else 0
    return _EQUIP_TYPE_TABLE.get(eid, _EQUIP_TYPE.get(eid, 1))

def equip_level_cap(eid):
    # 强化等级上限 = L01.Level 列 (客户端 m_maxLevel = SDEquip:getStatic_Level(); 1001 系 = 30)
    _load_equip_static()
    eid = int(eid) if str(eid).isdigit() else 0
    return _EQUIP_LEVEL_TABLE.get(eid, 30)

def equip_exp_ratio(eid):
    # L01.ExpRatio (材料经验系数; 客户端公式 (100+T22[级].ForgeExp)*ExpRatio)
    _load_equip_static()
    eid = int(eid) if str(eid).isdigit() else 0
    return _EQUIP_RATIO_TABLE.get(eid, 1.0)

# ---------- 玩家等级 (L101_PlayerExp 表驱动; 战斗胜利等获得经验) ----------
_PLAYER_EXP_TABLE = None

def player_add_exp(n):
    global _PLAYER_EXP_TABLE
    if n <= 0:
        return
    if _PLAYER_EXP_TABLE is None:
        tbl = {}
        try:
            # ★v37: 改用 state.load_table (两个判据都试 + json 校验), 替掉旧的
            #   `raw.rfind(b']')` —— 那是 v33 §6 遗留的 28 处危险惯用法之一
            #   (长度字段低字节恰为 ']' 时会切错 -> 静默空表 -> 升级公式全错)。
            t = load_table('L101_PlayerExp.json')
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                try:
                    tbl[int(r[h['Lv']])] = int(r[h['Exp']])
                except Exception:
                    continue
        except Exception as _e:
            print('[state] WARN L101_PlayerExp 读取失败: %r (玩家经验将无法升级)' % _e, flush=True)
        _PLAYER_EXP_TABLE = tbl
    if not _PLAYER_EXP_TABLE:
        # 表读不到时**不能假装升级成功**: 只累加经验, 不动等级 (旧实现会给 max_lv=200 的假上限)
        EQ_STATE['player_exp'] = int(EQ_STATE.get('player_exp', 0)) + int(n)
        return
    lv = int(EQ_STATE.get('player_lv', 1))
    exp = int(EQ_STATE.get('player_exp', 0)) + int(n)
    max_lv = max(_PLAYER_EXP_TABLE.keys())
    while lv < max_lv and exp >= _PLAYER_EXP_TABLE.get(lv, 99999999):
        exp -= _PLAYER_EXP_TABLE[lv]
        lv += 1
    EQ_STATE['player_lv'] = lv
    EQ_STATE['player_exp'] = exp

# ============================================================================
# ---------- 特性系统支撑 (L91_Status ⟶ Tallent1/2, SourceID, AdvanceID, Star) ----------
#   客户端事实 (SQMonster.lua):
#     怪物item 字段20-25 = feature1-6, 每个槽是字符串 "id#lv#isUse"(多个"&"连接的历史链, 服务端只写当前档)
#     字段26-27 = featureexp / featurelev (特性总经验/总等级 0-6, L97_1 阶梯)
#     同卡判定: 特性本体卡 / 进化素材 = L91 SourceID 相同 (getSpecialtyData: sourceID == monsterSourceID)
_L91_T = None

def _l91_info():
    global _L91_T
    if _L91_T is None:
        tbl = {}
        try:
            d = os.path.join(table_dir(), 'L91_Status.json')
            raw = open(d, 'rb').read()
            i = raw.rfind(b']')
            t = json.loads(raw[:i + 1].decode('utf-8'))
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                try:
                    mid = int(r[0])
                    tbl[mid] = (int(r[h['Tallent1']] or 0), int(r[h['Tallent2']] or 0),
                                int(r[h['SourceID']] or 0), int(r[h['AdvanceID']] or 0),
                                int(r[h['Star']] or 1))
                except Exception:
                    continue
        except Exception:
            pass
        _L91_T = tbl
    return _L91_T

def get_monster_info_l91(ucode):
    # -> (tallent1, tallent2, sourceid, advanceid, star) 或 None (无此怪)
    base = get_monster_base(ucode)
    if base is None:
        return None
    try:
        return _l91_info()[int(base[0])]
    except Exception:
        return (0, 0, 0, 0, 1)

def get_monster_source(ucode):
    # 同卡判定基准 (6007 本体卡 / 6003 进化素材): 进化链内所有阶段 SourceID 相同
    info = get_monster_info_l91(ucode)
    return info[2] if info else 0

def get_monster_advance(ucode):
    # 下一进化阶段静态id (L91.AdvanceID == 0 => 已是最高阶段)
    info = get_monster_info_l91(ucode)
    return info[3] if info else 0

def get_monster_star(ucode):
    # 当前阶段星级 (L91.Star; 进化后由新静态id决定)
    info = get_monster_info_l91(ucode)
    return info[4] if info else 1

_L91_ATTR = None

def _l91_attrs():
    # static id -> (品质 Quality, 种族 Type, 品阶 Star)
    #   ★v41 新增: L04_Evolution 的 "非指定怪" 列 (Monster1/Monster2) 格式是
    #   "品质|种族|品阶" (客户端 CultivateEvolution.lua:889-915 就是这么比的),
    #   服务端要校验就必须能按静态 id 取到这三个属性。
    global _L91_ATTR
    if _L91_ATTR is None:
        tbl = {}
        try:
            d = os.path.join(table_dir(), 'L91_Status.json')
            raw = open(d, 'rb').read()
            i = raw.rfind(b']')
            t = json.loads(raw[:i + 1].decode('utf-8'))
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                try:
                    tbl[int(r[0])] = (str(r[h['Quality']] or '0'),
                                      str(r[h['Type']] or '0'),
                                      str(r[h['Star']] or '1'))
                except Exception:
                    continue
        except Exception:
            pass
        _L91_ATTR = tbl
    return _L91_ATTR

def monster_static_attrs(mid):
    """静态 id -> (品质, 种族, 品阶); 未知 -> ('0','0','1')"""
    try:
        return _l91_attrs().get(int(mid), ('0', '0', '1'))
    except Exception:
        return ('0', '0', '1')

_TALLENT_CLASS = None

def _tallent_class_map():
    # L97_Tallents: id -> UnLockLevel (=客户端槽位类别: 1基础 2增量 3暴击 4控制 5克制 6特殊)
    #   客户端 SkillHome.getClassSpecialityData + SDSpecialty.getUnLockLevelData 按此列组织六类槽位;
    #   ensure_features 必须按类别放槽, 否则"基础槽显示三眼/详情弹窗错位/强化 isLock 失败禁用" (2026-09-07 真机实证)
    global _TALLENT_CLASS
    if _TALLENT_CLASS is not None:
        return _TALLENT_CLASS
    m = {}
    try:
        d = os.path.join(table_dir(), 'L97_Tallents.json')
        raw = open(d, 'rb').read()
        i = raw.rfind(b']')
        t = json.loads(raw[:i + 1].decode('utf-8'))
        h = {c: k for k, c in enumerate(t[0])}
        for r in t[1:]:
            try:
                m[int(r[h['ID']])] = int(r[h['UnLockLevel']] or 1)
            except Exception:
                continue
    except Exception:
        pass
    _TALLENT_CLASS = m
    return m

def feat_slots_for(tallents):
    # 特性列表 -> 6槽 (槽串 "id#lv#isUse", 同槽多特征用 & 连接; isUse=1 仅首个生效档)
    #   槽位 = 特征类别的 UnLockLevel (1-6); 未知类别兜底按输入顺序填空槽
    cmap = _tallent_class_map()
    slots = [''] * 6
    used = set()
    for t in tallents:
        if not t or int(t) <= 0:
            continue
        cls = cmap.get(int(t), 0)
        if 1 <= cls <= 6:
            idx = cls - 1
        else:
            idx = 0
            while idx < 6 and slots[idx]:
                idx += 1
            if idx >= 6:
                continue
        used.add(idx)
        fs = '%d#%d#%d' % (int(t), 0, 1 if not slots[idx] else 0)
        slots[idx] = slots[idx] + '&' + fs if slots[idx] else fs
    return slots

def ensure_features(ucode):
    # 特性槽幂等种子: 优先 L91 Tallent1/2 (CN 1.31 全 0=渠道阉割), 回退【YGTW 1.25 逐怪特性分配】
    #   (2026-09-07 破案: 广环老版 L91_Status 的 Tallent1/2 是完整逐怪分配, 247/247 id 与 CN 1.31 重合;
    #     特性 id 1001-1028 全部在 CN 1.31 L97_Tallents 有定义 -> 同游戏同怪物空间, 直接采用)
    if ucode in EQ_STATE['monster_feat']:
        return EQ_STATE['monster_feat'][ucode]
    info = get_monster_info_l91(ucode)
    t1, t2 = (info[0], info[1]) if info else (0, 0)
    if not t1 and not t2:
        t1, t2 = _ygtw_tallents(ucode)
    slots = feat_slots_for([t1, t2])
    EQ_STATE['monster_feat'][ucode] = slots
    # ★v40 (P0-1): 种子同时**记下出处** (t1:t2), 这样将来换表时能安全重算 ——
    #   见 recompute_features_on_source_change()。不记的话就只能靠"猜"哪些槽是机器种的。
    EQ_STATE.setdefault('monster_feat_src', {})[ucode] = '%d:%d' % (t1, t2)
    return slots

# ============================================================================
# ---------- ★v40 (P0-1) 静态特性"数据源指纹 + 一次性重算" ----------
# 要解决的问题 (v38 独立复核给 P0-1 的定义):
#   ensure_features() 是**惰性补种且只补一次** —— `if ucode in monster_feat: return`。
#   于是同一只怪, "换表之前入库的" 与 "换表之后入库的" 会拿到**不同的特性**,
#   而存档里没有任何东西能回答"这条槽是按哪版数据种的"。
#   换表场景是真实存在的: 换 YGTW 版本 / 修某一行错数据 / CN L91 补回 Tallent 列。
#
# 设计 (三条不变量):
#   ① **指纹** fp = 算法版本 + [CN L91_Status, YGTW L91_Status] 的内容哈希。
#      存档存 feat_source_fp; 与当前 fp 不一致 -> 触发一次重算。
#   ② **只重算"玩家没动过"的怪**。判据不是"槽长什么样", 而是**出处比对**:
#      种子时把 (t1,t2) 记进 monster_feat_src; 重算前先比
#      "当前槽的特性 id 序列" vs "用记录的 (t1,t2) 重新生成的 id 序列" ——
#        相等 => 组成没被玩家改过 => 换成新数据源的组成, 并把仍存在的 id 的 **lv 原样保留**;
#        不等 => 玩家动过 (1183 特性扩充加了 id / 1181 换档 / 1181 吞噬清理) => **整只跳过**。
#      ⚠ 为什么不能一律重算: 槽串 'id#lv#isUse' 里带着**玩家练出来的等级**,
#        一刀切重算会把玩家的养成抹掉。
#   ③ 出处未知 (老档没有 monster_feat_src) -> 首次回填: 组成与当前算法一致就认作"机器种的",
#      否则标 '?' (来源不明/玩家动过) -> **永不自动重算**。
# ============================================================================
FEAT_ALGO_VERSION = 1      # 1 = CN L91.Tallent1/2 优先, 全 0 时回退 YGTW (v38 及以前的行为)
_FEAT_FP_KEY = 'feat_source_fp'
_FEAT_SRC_KEY = 'monster_feat_src'
_FEAT_SRC_UNKNOWN = '?'
#   ★ 若将来真的改了取值算法 (例如实装"按名字回退"), **必须**把 FEAT_ALGO_VERSION +1,
#     否则指纹不变 -> 老怪不会被重算 -> 又回到"同怪不同数据"。
#     ("按名字回退"已实测**不成立**: 见 _audit_feat_name_fallback*.py / v40 总档 P0-2。)


def _feat_source_paths():
    p = os.path.join(table_dir(), 'L91_Status.json')
    alt = table_dir_alt()
    return (p, os.path.join(alt, 'L91_Status.json')) if alt else (p,)


def feat_source_fingerprint():
    """静态特性数据源指纹: 算法版本 + 两个源表的内容哈希 (任一不可读 -> 'missing')。"""
    import hashlib
    parts = ['v%d' % FEAT_ALGO_VERSION]
    for p in _feat_source_paths():
        try:
            with open(p, 'rb') as f:
                parts.append(hashlib.sha256(f.read()).hexdigest()[:16])
        except Exception:
            parts.append('missing')
    return ':'.join(parts)


def _feat_ids_of_slots(slots):
    """槽列表 -> 每槽的**特性 id 序列** (丢掉 #lv#isUse, 只比组成)。"""
    out = []
    for s in (slots or []):
        ids = []
        for seg in str(s or '').split('&'):
            if '#' in seg:
                ids.append(seg.split('#')[0])
        out.append(tuple(ids))
    return tuple(out)


def _feat_apply_ids(new_ids, old_slots):
    """把新组成套到槽上, 并**保留**旧槽里同名特性的 lv (玩家的养成不能丢)。

    isUse 按"每槽首个生效档=1, 其余=0"重排 (与 feat_slots_for 的机器语义一致)。
    """
    old_lv = {}
    for s in (old_slots or []):
        for seg in str(s or '').split('&'):
            p = seg.split('#')
            if len(p) >= 2 and p[0] not in old_lv:
                old_lv[p[0]] = p[1]
    out = []
    for ids in new_ids:
        parts = ['%s#%s#%d' % (fid, old_lv.get(fid, '0'), 1 if j == 0 else 0)
                 for j, fid in enumerate(ids)]
        out.append('&'.join(parts))
    while len(out) < 6:
        out.append('')
    return out[:6]


def _feat_mid_of(d, ucode):
    """从**给定 dict** 里解出 ucode 的静态怪 id —— 不能用 get_monster_base()!

    ⚠ 为什么必须单独写一个 (v40 实现时踩到的坑):
      `get_monster_base()` 读的是 **EQ_STATE**(模块级内存态), 而本模块的迁移/重算
      都是在 `load_state()` 里、**EQ_STATE.update(data) 之前**对 `data` 调用的
      (铁律 #100: 迁移必须落在 data 上)。那一刻 EQ_STATE 还是**上一次的旧内容**
      —— 服务端刚启动时更是只有 _default_state() 的初始 3 只(ucode 1/2/3),
      玩家实际持有的 ucode 4..10 **一个都查不到** -> 全部退化成 mid=0 -> 整批重算错。
    语义与 get_monster_base 保持一致 (override -> deleted -> 初始表 -> monsters)。
    """
    u = str(ucode)
    ov = (d.get('monster_base_override') or {}).get(u)
    if ov:
        try:
            return int(ov[0])
        except Exception:
            pass
    if u in [str(x) for x in (d.get('monster_deleted') or [])]:
        return 0
    b = _MONSTER_BASE.get(u)
    if b is not None:
        return int(b[0])
    m = (d.get('monsters') or {}).get(u)
    if m is None:
        return 0
    try:
        return int(m[0])
    except Exception:
        return 0


def backfill_feat_src(data=None):
    """为老档建立/维护"特性出处" (幂等)。返回 (认作机器种的, 标记来源不明的, 清掉的孤儿数)。

    ★v41: 顺带清孤儿 —— 怪被吞噬/进化献祭移除后, 出处表里可能还留着它的键。
      功能上无害 (重算只遍历 monster_feat), 但会让"条数一致"这类不变量失真、存档越用越脏。
    """
    d = EQ_STATE if data is None else data
    src = d.get(_FEAT_SRC_KEY)
    if not isinstance(src, dict):
        src = {}
        d[_FEAT_SRC_KEY] = src
    ok = unk = 0
    pruned = 0
    feats = d.get('monster_feat') or {}
    for _k in [k for k in list(src.keys()) if k not in feats]:
        src.pop(_k, None)
        pruned += 1
    ok = unk = 0
    for uc, slots in feats.items():
        if str(uc) in src:
            continue
        try:
            mid = _feat_mid_of(d, uc)
            t1, t2 = static_tallents(mid) if mid else (0, 0)
            if _feat_ids_of_slots(slots) == _feat_ids_of_slots(feat_slots_for([t1, t2])):
                src[str(uc)] = '%d:%d' % (t1, t2)      # 与当前算法一致 -> 认作机器种的
                ok += 1
            else:
                src[str(uc)] = _FEAT_SRC_UNKNOWN       # 玩家动过/来源不明 -> 永不自动重算
                unk += 1
        except Exception:
            src[str(uc)] = _FEAT_SRC_UNKNOWN
            unk += 1
    return ok, unk, pruned


def recompute_features_on_source_change(data=None):
    """数据源指纹变了 -> 一次性把"玩家没动过"的怪的特性重算到新数据源。返回变更说明。

    ⚠ 必须在 EQ_STATE 装载**之前**对 data 调用 (铁律 #100: 迁移要落在 data 上)。
    只改 monster_feat / monster_feat_src / feat_source_fp;
    **不动** monster_featexp / monster_featlev (那是另一套字段)。
    """
    out = []
    d = EQ_STATE if data is None else data
    cur = feat_source_fingerprint()
    old = d.get(_FEAT_FP_KEY)
    # ★v41: 出处回填 + 孤儿清理**每次装载都跑** (幂等且很便宜) ——
    #   第一版把它放在 `if old == cur: return` 之后, 结果指纹一旦稳定, 清理就永远不执行,
    #   存档里的孤儿键会一直留着。("会改变存档的维护动作"不能挂在"指纹变化"这个稀有事件上。)
    _ok, _unk, _pruned = backfill_feat_src(d)
    if _ok or _unk or _pruned:
        out.append('出处维护: 新回填 %d 只 / 来源不明 %d 只 / 清孤儿 %d 个' % (_ok, _unk, _pruned))
        if _unk:
            print('[state] v40 特性出处回填: 机器种的 %d 只 / 来源不明(玩家动过?) %d 只 -> 后者不参与自动重算'
                  % (_ok, _unk), flush=True)
    if old == cur:
        return out
    src = d.get(_FEAT_SRC_KEY) or {}
    feats = d.get('monster_feat') or {}
    changed = skipped = gone = 0
    for uc, slots in list(feats.items()):
        s = src.get(str(uc))
        if not s or s == _FEAT_SRC_UNKNOWN:
            skipped += 1
            continue
        try:
            ot1, ot2 = (int(x) for x in str(s).split(':'))
        except Exception:
            src[str(uc)] = _FEAT_SRC_UNKNOWN
            skipped += 1
            continue
        # ① 组成是否还是"机器种的那份"? 不是 -> 玩家动过, 整只跳过并永久标记
        if _feat_ids_of_slots(slots) != _feat_ids_of_slots(feat_slots_for([ot1, ot2])):
            src[str(uc)] = _FEAT_SRC_UNKNOWN
            skipped += 1
            continue
        # ② 取新数据源的值 (★用 data 局部解析, 不能用 get_monster_base —— 见 _feat_mid_of)
        try:
            mid = _feat_mid_of(d, uc)
            nt1, nt2 = static_tallents(mid) if mid else (0, 0)
        except Exception:
            gone += 1
            continue
        if (nt1, nt2) == (ot1, ot2):
            continue                                   # 这只怪在新表里没变
        feats[uc] = _feat_apply_ids(_feat_ids_of_slots(feat_slots_for([nt1, nt2])), slots)
        src[str(uc)] = '%d:%d' % (nt1, nt2)
        changed += 1
        out.append('怪 %s(静态%d): 特性 %s -> %s' % (uc, mid, s, src[str(uc)]))
    d[_FEAT_FP_KEY] = cur
    out.append('数据源指纹 %s -> %s; 重算 %d 只 / 跳过(玩家动过或来源不明) %d 只 / 取不到静态 %d 只'
               % (old, cur, changed, skipped, gone))
    return out


_YGTW = None

def _ygtw_lookup():
    # mid -> (tallent1, tallent2) (范围过滤 1001-1028); 未命中 -> (0, 0)
    global _YGTW
    if _YGTW is None:
        tbl = {}
        try:
            _alt = table_dir_alt()
            if not _alt:
                raise FileNotFoundError('YGZZ_TABLE_DIR_ALT not set')
            d = os.path.join(_alt, 'L91_Status.json')
            raw = open(d, 'rb').read()
            i = raw.rfind(b']')
            t = json.loads(raw[:i + 1].decode('utf-8'))
            h = {c: k for k, c in enumerate(t[0])}
            for r in t[1:]:
                try:
                    mid = int(r[0])
                    t1 = int(r[h['Tallent1']] or 0)
                    t2 = int(r[h['Tallent2']] or 0)
                    f = lambda x: x if 1001 <= x <= 1028 else 0
                    if t1 or t2:
                        tbl[mid] = (f(t1), f(t2))
                except Exception:
                    continue
        except Exception:
            pass
        _YGTW = tbl
    return _YGTW

def ygtw_tallents_mid(mid):
    # 静态 id -> (t1, t2) (YGTW 1.25 直接查表; 无此怪/全 0 -> (0,0))
    try:
        return _ygtw_lookup().get(int(mid), (0, 0))
    except Exception:
        return (0, 0)

def static_tallents(mid):
    # 静态特性唯一入口: CN 1.31 L91 (渠道版 Tallent 全 0) 回退 YGTW 1.25 逐怪分配
    #   2026-09-07 破案: YGTW 247 只怪 id 与 CN 1.31 完全重合, 特性 id 1001-1028 全部在 L97_Tallents 有定义
    info = _l91_info().get(int(mid))
    t1, t2 = (info[0], info[1]) if info else (0, 0)
    if not t1 and not t2:
        t1, t2 = ygtw_tallents_mid(mid)
    return (t1, t2)

def _ygtw_tallents(ucode):
    # 旧接口 (按 ucode): 委托 static_tallents (保持兼容; 调用方=ensure_features 回退)
    return static_tallents(int(get_monster_base(ucode)[0])) if get_monster_base(ucode) else (0, 0)

def card_state_fields():
    # 周卡/月卡 -> roleInfo 字段 (SQGamer roleInfo[18/19/26/27] 1-based; _role_info 0-based [17/18/25/26]):
    #   权威源 = G03_SpellGroup 6011/6021 行 (客户端 SWeekMonthCard 界面渲染):
    #     6011 周卡: 立得妖魂120 | 7日每日赠送: 5%金币加成 (T23 1007='5' -> coinbuff=105; 界面图标 Gold_105.png)
    #     6021 月卡: 立得妖魂800 | 30日每日赠送: 20%经验加成 (T23 1002='20' -> expbuff=120; 界面图标 Exp_120.png)
    #   客户端: WarData.getBattleExperience percent=BuffNum/100 (expbuff), 金币 buff 同理 (SQGamer [19] coinbuff)
    #   [26] WeekLastTime  = 周卡过期【绝对时间戳】(SWeekMonthCard lastTime - os.time() <=0 => 可再次购买)
    #   [27] MonthLastTime = 月卡过期绝对时间戳
    import time
    now = time.time()
    cd = EQ_STATE.get('card_daily', {})
    wk = cd.get('week') or {}
    mo = cd.get('month') or {}
    wk_end = float(wk.get('start', 0)) + 7 * 86400 if float(wk.get('start', 0)) > 0 else 0
    mo_end = float(mo.get('start', 0)) + 30 * 86400 if float(mo.get('start', 0)) > 0 else 0
    wk_active = wk_end > now
    mo_active = mo_end > now
    # 100 = 无加成; 周卡 +5%金币 -> 105; 月卡 +20%经验 -> 120
    expbuf = str(120 if mo_active else 100)
    coinbuf = str(105 if wk_active else 100)
    return expbuf, coinbuf, int(wk_end if wk_active else 0), int(mo_end if mo_active else 0)

def get_feat_parts(ucode):
    # _mon_with_state 用: (slots6, featureexp, featurelev); 无此怪 -> None
    # 客户端契约 (SpecialtyUpgrade.lua update/updatePreview, SQMonster.getfeatureLv nil 兜底=1):
    #   特性总等级取值 1-6, 初始=1, 阈值表 L97_1 行(FeatureLV=当前lv).UpgradeExp = 升到下一级所需;
    #   服务器发出 0 会让客户端 update() 查 L97_1 row(0)=nil -> tonumber(nil) 崩溃 (2026-09-07 复核修复)
    base = get_monster_base(ucode)
    if base is None:
        return None
    lv = int(EQ_STATE['monster_featlev'].get(ucode, 1) or 1)
    if lv < 1:
        lv = 1
    elif lv > 6:
        lv = 6
    return (ensure_features(ucode),
            int(EQ_STATE['monster_featexp'].get(ucode, 0)),
            lv)

# ============ 可叠加背包统一入包 (2026-09-12 用户反馈 §2) ============
#   客户端 礼包/卡 背包项 = [ucode, type(98礼包/97卡), 静态id, count] (SQGiftbag:insertItem)
#   元灵 背包项 = [ucode, 静态id, count, isload] (SQGhost:insertItem)
#   旧实现每次发放都新建一个 ucode -> 同一件礼包占满多格 (用户实测: 月卡每日"妖怪礼包"2048 变成 g6/g7/g8/g9/g10 五格)。
#   官方语义 = 同 (类型,静态id) 叠加到同一个 ucode 的 count 上; 已用尽(count<=0)的旧条目不再复用。
def add_giftbag(btype, gid, num=1):
    # 礼包/卡入包 (叠加): 返回 (ucode, 叠加后总数)
    #   匹配优先级: ①同(type,静态id)且 count>0 -> 就地累加
    #               ②同(type,静态id)但 count<=0 的空壳 -> 复用该 ucode 重新计数 (避免背包位无限增长)
    #               ③都没有 -> 新建 ucode
    btype, gid, num = int(btype), int(gid), max(1, int(num or 1))
    gb = EQ_STATE.setdefault('giftbag', [])
    reuse = None
    for g in gb:
        try:
            if int(g[1]) != btype or int(g[2]) != gid:
                continue
            if int(g[3]) > 0:
                g[3] = int(g[3]) + num
                return str(g[0]), int(g[3])
            if reuse is None:
                reuse = g
        except Exception:
            continue
    if reuse is not None:
        reuse[3] = num
        return str(reuse[0]), num
    if btype == 98:
        ucnt = int(EQ_STATE.get('next_giftbag_ucode', 1))
        uc = 'g%d' % ucnt
        EQ_STATE['next_giftbag_ucode'] = ucnt + 1
    else:
        ucnt = int(EQ_STATE.get('next_card_ucode', 3))
        uc = 'c%d' % ucnt
        EQ_STATE['next_card_ucode'] = ucnt + 1
    gb.append([uc, btype, gid, num])
    return uc, num

def merge_ghost_rows(data=None):
    """把元灵背包里**同 (静态id, isload) 的多行合并成一行** (v39 堆叠修复)。返回变更说明。

    为什么需要 (2026-09-13 实测):
      v39 之前有 **3 个入口绕过了 add_ghost 的合并逻辑**, 直接 `EQ_STATE['ghost'].append(...)`
      新建一行 —— 元灵碎片合成 / 抽奖棋盘 gt5 / 任务奖励 ty5。
      后果: 同一个元灵在背包里占多个格子。实测用户存档 id=1020 同时存在
      `[['1',1020,1],['gh1',1020,2],['gh2',1020,20]]` 三行, 客户端 1031 按 ucode 建格
      -> 界面显示"命 x1 / x2 / x20"三格, 而不是"命 x23"一格。

    不变量 (**这是本函数敢放进迁移链的依据**):
      按 (id, isload) 分组的数量总和守恒 —— 只搬数字, 不产生也不销毁任何元灵。
    幂等: 合并过的档再跑一次返回 [] (没有第二行可并)。
    保留空壳: count<=0 的行**原样保留**, 因为 consume_ghost/寄妖 靠它维持 ucode 映射;
      且 ghost_bag_json() 只下发 cnt>0 的行, 客户端看不到空壳。
    返回: ['元灵 id=1020 isload=0: 合并 ucode gh2(20) -> 1(合计 21)', ...]
    """
    out = []
    d = EQ_STATE if data is None else data
    gl = d.get('ghost')
    if not isinstance(gl, list) or not gl:
        return out
    kept = []
    by_key = {}
    for row in gl:
        if not (isinstance(row, list) and len(row) >= 3):
            kept.append(row)
            continue
        try:
            gid = int(row[1])
            cnt = int(row[2])
            isload = int(row[3]) if len(row) > 3 else 0
        except Exception:
            kept.append(row)
            continue
        if cnt <= 0:
            kept.append(row)          # 空壳行: 不动
            continue
        key = (gid, isload)
        tgt = by_key.get(key)
        if tgt is None:
            by_key[key] = row
            kept.append(row)
        else:
            tgt[2] = int(tgt[2]) + cnt
            out.append('元灵 id=%d isload=%d: 合并 ucode %s(%d) -> %s(合计 %d)'
                       % (gid, isload, row[0], cnt, tgt[0], int(tgt[2])))
    if out:
        d['ghost'] = kept
    return out


def split_ghost_rows(data=None):
    """★v51: 把**完整元灵**(L03_Equipghost 里的 1020-1054) 的 count>1 行拆成 N 行各 1 个。
    返回变更说明。

    为什么 (2026-09-13 用户裁定):
      "是包含所有元灵，是碎片合成出来后的完整元灵都被这个影响，
       要不然要重复启动游戏才正确显示和使用。"
      元灵是**五级递进**的成品链 (L03_Equipghost / 碎片来自 L02 Type=31):
        灵石(5碎片) → 隐玉(10) → 元灵(20) → 魂玉(25)
      碎片是散装掉落物 (几十上百个很正常) -> **可以堆叠**;
      完整元灵是 5~25 个碎片合成出来的**成品** -> 官方语义是**一格一个**。
      v39 的 merge_ghost_rows 当年是为"同一物品占多格"做的一刀切修复,
      把成品也一起并了 —— 结果是"寄妖 1 个却整叠消失/要重启才正确"这类问题反复出现。

    不变量 (**敢放进加载路径的依据**): 按 (id, isload) 分组的**数量总和守恒** ——
      只改"占几格", 不产生也不销毁任何元灵。
    空壳 (count<=0) 原样保留 (consume_ghost/寄妖 靠它维持 ucode 映射)。
    幂等: 拆过的档再跑返回 []。
    返回: ['元灵 id=1052 isload=0: ucode gh2(2) 拆成 2 格', ...]
    """
    out = []
    d = EQ_STATE if data is None else data
    gl = d.get('ghost')
    if not isinstance(gl, list) or not gl:
        return out
    # ★防重号: next_ghost_ucode() 是按现有 `ghN` 的最大值 +1 发号,
    #   但本工程历史上有**非 gh 前缀**的 ucode (`'1'`/`'2'`/`'3'` 是默认种子)。
    #   拆 23 格时会发到 gh1..gh22, 而 `gh1` 可能**已经被别的元灵占用** (实测 1049 的空壳就叫 gh1)
    #   -> 撞号。所以这里显式跳过所有"已在用"的 ucode。
    used = set()
    for _r in gl:
        try:
            used.add(str(_r[0]))
        except Exception:
            continue
    new_rows = []
    for row in gl:
        if not (isinstance(row, list) and len(row) >= 3):
            new_rows.append(row)
            continue
        try:
            gid = int(row[1])
            cnt = int(row[2])
            isload = int(row[3]) if len(row) > 3 else 0
        except Exception:
            new_rows.append(row)
            continue
        if cnt <= 1:
            new_rows.append(row)              # 0=空壳 / 1=已经一格一个: 不动
            continue
        # 第一格复用原 ucode, 其余重新发号 (发号唯一入口, 并跳过已占用的号)
        new_rows.append([row[0], gid, 1, isload])
        used.add(str(row[0]))
        for _ in range(cnt - 1):
            uc = next_ghost_ucode()
            while str(uc) in used:
                uc = next_ghost_ucode()
            used.add(str(uc))
            new_rows.append([uc, gid, 1, isload])
        out.append('元灵 id=%d isload=%d: ucode %s(%d) 拆成 %d 格各 1'
                   % (gid, isload, row[0], cnt, cnt))
    if out:
        d['ghost'] = new_rows
    return out


def next_ghost_ucode():
    """取下一个元灵 ucode ('gh%d') —— 元灵发号的**唯一入口**。

    ★为什么不直接读计数器 (v39 修的第二个坑):
      v39 之前元灵发号有**三套实现**互不知情:
        ① add_ghost / handlers_task  : 读 `next_giftbag_ucode` (与礼包共用!)
        ② handlers_bag 碎片合成      : `max(现有 ghN)+1`
        ③ handlers_lottery gt5       : `max(现有 ghN)+1`
      ②③ 从不推进 ① 的计数器 -> 一旦计数器**落后于现有最大 ghN**
      (旧档没有该字段 / 回档到旧快照 / 计数器被重置), ① 就会发出一个**已存在的号**。
      而 `ghost` 是 **list**, 同号行会并存; 客户端 `mBagList` 以 ucode 为键 ->
      后一行覆盖前一行 -> 物品在界面上**凭空消失**, find_ghost() 也会命中错行。
      所以这里**反查现有最大值取大者**, 保证发号只增、永不复用。
    """
    n = 1
    try:
        n = max(1, int(EQ_STATE.get('next_ghost_ucode', 1) or 1))
    except Exception:
        n = 1
    for g in EQ_STATE.get('ghost') or []:
        try:
            u = str(g[0])
            if u.startswith('gh') and u[2:].isdigit():
                n = max(n, int(u[2:]) + 1)
        except Exception:
            continue
    EQ_STATE['next_ghost_ucode'] = n + 1
    return 'gh%d' % n


def _fresh_ghost_ucode():
    """取一个**当前未被占用**的元灵 ucode。

    ★为什么不能直接用 next_ghost_ucode():
      它是按现有 `ghN` 的最大值 +1 发号, 只认 `gh` 前缀。但本工程历史上有
      **非 gh 前缀**的 ucode —— 默认种子就是 `'1'`/`'2'`/`'3'`。
      实测踩到: 把 `['1',1020,23]` 拆 23 格时会依次发出 gh1..gh22,
      而 `gh1` 已经被 id=1049 的空壳占用 -> **撞号** (ghost 是 list,
      客户端 mBagList 以 ucode 为键 -> 后行覆盖前行 = 物品消失)。
      这里显式跳过所有已在用的号。
    """
    used = set()
    for _r in (EQ_STATE.get('ghost') or []):
        try:
            used.add(str(_r[0]))
        except Exception:
            continue
    for _ in range(10000):
        uc = next_ghost_ucode()
        if str(uc) not in used:
            return uc
    return uc


def add_ghost_rows(gid, num=1, isload=0, allow_stack=None):
    """★v51: 元灵入包的**推荐入口** —— 返回本次**全部**受影响的背包行。

    返回: [[ucode, gid, 最新count, isload], ...]
      · 可堆叠 (碎片类): 恒 1 行, count 是**累计值** (客户端 SQGhost:insertItem 是覆盖式)。
      · 不可堆叠 (完整元灵): num 个 -> num 行, 每行 count=1。

    ★v51 语义变更 (用户裁定 2026-09-13): **完整元灵不堆叠, 一格一个**。
      元灵是五级递进的成品链 (L03_Equipghost; 碎片在 L02 Type=31):
        灵石(5碎片) → 隐玉(10) → 元灵(20) → 魂玉(25)
      碎片是散装掉落 (几十上百个), 堆叠合理; 成品是 5~25 个碎片合成的, 官方一格一个。
      v39 的 merge_ghost_rows 当年是为"同一物品占多格"做的一刀切修复, 把成品也一起并了,
      于是"寄妖 1 个却整叠消失 / 要重启才正确"这类问题反复出现。

    ★为什么必须返回"全部行"而不是"最后一行":
      非堆叠时发 3 个会开 3 个新格子; 只回最后一格 -> 客户端 NetDropRewardData
      style==5 分支是 `for _,goodsInfo in pairs(goodslist) do ghost_insertItem(...)` ——
      载荷里没有的行不会被建出来, 界面少格子, **直到下次整包 1031 才补齐**。
      用户报的"要重复启动游戏才正确显示和使用"正是这个症状。

    allow_stack: None=按 _ghost_stacks() 自动判定; True/False 可强制覆盖 (给特殊场景/测试)。
    """
    gid, num = int(gid), max(1, int(num or 1))
    isload = int(isload or 0)
    if allow_stack is None:
        allow_stack = _ghost_stacks(gid)
    gl = EQ_STATE.setdefault('ghost', [])
    if not allow_stack:
        # 完整元灵: 一格一个 (num 个就开 num 行)
        out = []
        for _ in range(num):
            uc = _fresh_ghost_ucode()
            gl.append([uc, gid, 1, isload])
            out.append([uc, gid, 1, isload])
        return out
    # ---- 以下为"可堆叠"路径 (碎片类 / 显式 allow_stack=True) ----
    #   ①同(id,isload)且 count>0 -> 就地累加 (返回累计数)
    #   ②同(id,isload)的空壳     -> 复用该 ucode 重新计数
    #   ③都没有                 -> 新建 ucode
    #   ★isload 参与匹配 (v39): 已"寄妖进装备"的行不该再吸收新的散装元灵。
    reuse = None
    for g in gl:
        try:
            if int(g[1]) != gid:
                continue
            if int(g[3] if len(g) > 3 else 0) != isload:
                continue
            if int(g[2]) > 0:
                g[2] = int(g[2]) + num
                return [[str(g[0]), gid, int(g[2]), isload]]
            if reuse is None:
                reuse = g
        except Exception:
            continue
    if reuse is not None:
        reuse[2] = num
        return [[str(reuse[0]), gid, num, isload]]
    uc = _fresh_ghost_ucode()
    gl.append([uc, gid, num, isload])
    return [[uc, gid, num, isload]]


def add_ghost(gid, num=1, isload=0, allow_stack=None):
    """兼容壳: 返回 (ucode, 该行最新数量) —— 只反映**最后一次**入包的那一行。

    ★新代码请改用 `add_ghost_rows()` 并把返回的**全部行**放进 drop 载荷 (见其文档);
      本函数保留给"只要一个 ucode/count"的旧调用点与自测。
    """
    rows = add_ghost_rows(gid, num, isload, allow_stack)
    if not rows:
        return None, 0
    last = rows[-1]
    return str(last[0]), int(last[2])


# ★v51: 哪些元灵静态id 属于"完整成品" (不堆叠)
#   权威判据 = L03_Equipghost 里存在的 id (1020-1054 + 999999 Dummy)。
#   其它 id (例如历史遗留/表外) 一律按**可堆叠**处理, 避免"表读不到就变成一格一个"的意外。
_GHOST_NO_STACK = None
def _ghost_no_stack_set():
    global _GHOST_NO_STACK
    if _GHOST_NO_STACK is None:
        ids = set()
        try:
            d = os.path.join(table_dir(), 'L03_Equipghost.json')
            raw = open(d, 'rb').read().decode('utf-8', 'replace')
            t = json.loads(raw[:raw.rfind(']') + 1])
            for r in t[1:]:
                try:
                    gid = int(r[0])
                    if gid != 999999:            # Dummy 不参与
                        ids.add(gid)
                except Exception:
                    continue
        except Exception:
            ids = set()
        _GHOST_NO_STACK = ids
    return _GHOST_NO_STACK


def _ghost_stacks(gid):
    """该静态id 的元灵是否**允许堆叠**。现在是"完整元灵 -> False"。
    表读不到时返回 True (保守: 宁可堆叠也不要意外把物品拆成一堆格子)。"""
    ids = _ghost_no_stack_set()
    if not ids:
        return True
    return int(gid) not in ids

# ---------- ★v32 宝箱入包 (唯一入口) ----------
# 为什么必须收敛到这一层 (审计出来的真缺口):
#   1060 TREASURE_GET 的实现 handlers_profile._owned_boxes() 是**直接把 treasure_bag 下发**的,
#   不做任何过滤; 而客户端渲染每个箱子要按 id 查 L103_TreasureBox (SDFactory SDBox) ——
#   拿到表外的 id 就是"取 nil 的字段" -> 原生层 SIGSEGV (铁律 #2 的经典成因)。
#   此前有 **3 个入口是裸写** treasure_bag (礼包 handlers_bag / 活动 handlers_activity / 任务 handlers_task),
#   只有 1061 商店购买与 /admin 的写前校验做了 L103 校验。
#   实测: 全库所有 type=18 引用当前都落在 L103 内(见 _audit_treasure_boxes.py), 所以**今天没有活的坏数据**;
#   集中到这里是为了让"将来也不可能" —— 任何入口都无法再把一个开不了/会崩的箱子塞进玩家背包。
_BOX_IDS = None

def box_ids():
    """L103_TreasureBox 的合法箱 id 集合 (惰性加载 + 缓存)"""
    global _BOX_IDS
    if _BOX_IDS is None:
        try:
            _rows = load_table('L103_TreasureBox')
            _h = {c: i for i, c in enumerate(_rows[0])}
            _BOX_IDS = set(int(r[_h['ID']]) for r in _rows[1:]
                           if str(r[_h['ID']]).lstrip('-').isdigit())
        except Exception as _e:
            print('[BOX] WARN 读 L103_TreasureBox 失败: %r (本次不校验箱 id)' % _e, flush=True)
            _BOX_IDS = set()
    return _BOX_IDS

def add_box(bid, num=1):
    """宝箱入包 (同 id 叠加) —— 与 add_ghost / add_giftbag 同一族的"入包原语"。

    返回 (ucode, 累计数量); id 非法返回 (None, 0) 并打 WARN。
    ⚠ 表读失败时**放行**(fail-open): 那属于"环境坏了", 不该把整个发放链路一起卡死;
      真正的坏 id 场景(有人填错数字)仍然会被拦下。
    """
    try:
        bid = int(bid)
        num = max(1, int(num or 1))
    except Exception:
        return None, 0
    _ids = box_ids()
    if _ids and bid not in _ids:
        print('[BOX] WARN 拒绝入包: 箱 id %d 不在 L103_TreasureBox 内 '
              '(客户端查不到箱表 -> 渲染会崩)' % bid, flush=True)
        return None, 0
    tb = EQ_STATE.setdefault('treasure_bag', {})
    uc = str(bid)
    prev = tb.get(uc, [bid, 0])
    tb[uc] = [bid, int(prev[1]) + num]
    return uc, int(tb[uc][1])

# ---------- 装备寄妖/附魔 (CULTIVATE_ENCHANT 6007) ----------#   ★2026-09-12 (v26 P1) 新增。客户端契约 (app/network/cultivate/NetEquipEnchant.lua):
#     请求 {equipucode, ghostucode, slot}   slot ∈ 1..4
#     响应 [1,[eggcoin, soulcoin, goldcoin, ghostucode, equipInfo]]
#       · ghostucode ≠ "" -> 客户端 ghost_deleteItem(ghostucode)  (元灵被"寄"进装备, 离开背包)
#       · equipInfo = 14 字段装备 item -> 客户端 equip_updateItem
#   14 字段装备 item 的寄妖位 (app/sqdata/SQEquip.lua:32~45 insertItem):
#     [5]=uappend1 [6]=uappend2 [13]=uappend3 [14]=uappend4 —— 值 = **元灵的静态 id (SDGhost)**
#     (客户端 getEnchantAttr 用 `SDFactory:getObject(SD_OBJS.SDGhost, id)` 直接按静态 id 查表,
#      所以这里绝不能塞元灵 ucode/实例 id)
#   槽位上限 = L01_Equipments.Slot (客户端 SDEquip:getStatic_EnchantCount(); 全表取值 2/3/4)
#   单次消耗 = T23_Function[110].Value = 1000 金币 (MacroConfig.EnchantGold=110 -> EquipEnchant.lua:676)
def get_equip_append(ucode):
    # 返回 [a1, a2, a3, a4] —— 装备 4 个寄妖位上的元灵静态 id (0=空)
    try:
        row = (EQ_STATE.get('equip_append') or {}).get(str(ucode))
        if isinstance(row, list):
            out = [0, 0, 0, 0]
            for i in range(min(4, len(row))):
                try:
                    out[i] = int(row[i] or 0)
                except Exception:
                    out[i] = 0
            return out
    except Exception:
        pass
    return [0, 0, 0, 0]

def set_equip_append(ucode, idx, ghost_static_id):
    # idx: 1..4 (对应 uappend1..4)
    row = get_equip_append(ucode)
    idx = int(idx)
    if 1 <= idx <= 4:
        row[idx - 1] = int(ghost_static_id or 0)
    EQ_STATE.setdefault('equip_append', {})[str(ucode)] = row
    return row

def ghost_bag():
    # 元灵背包: [[ucode,静态id,数量,isload], ...] (GHOST_BAG 1031 / NetGhostBag:insertItem)
    return EQ_STATE.setdefault('ghost', [])

def find_ghost(ghostucode):
    # 按 ucode 找元灵行 (引用返回, 可直接改数量)
    g = str(ghostucode)
    for row in ghost_bag():
        try:
            if str(row[0]) == g:
                return row
        except Exception:
            continue
    return None

def consume_ghost(ghostucode, num=1):
    # 消耗 N 个元灵: 数量减到 0 时保留空壳行 (与 add_ghost 的"空壳复用"约定一致),
    #   客户端 ghost_deleteItem 会整行删掉, 所以空壳只是为了服务端不丢 ucode 映射。
    row = find_ghost(ghostucode)
    if row is None:
        return False
    try:
        have = int(row[2] or 0)
    except Exception:
        have = 0
    if have < int(num):
        return False
    row[2] = have - int(num)
    return True

# ---------- 图鉴 (M01_Manual) 解锁记录 ----------
#   客户端 MEquipHome/VIllustrateHome 只从服务器取"已解锁的图鉴 ID 列表" (1072/1073),
#   其余(名称/品质/Lv30属性)全部由客户端按 M01_Manual + L01_Equipments 本地计算。
def note_manual_unlock(eid):
    # 记录"曾经获得过的装备静态id" -> 装备图鉴永久解锁 (拾取即记, 不因分解/强化消耗而回收)
    try:
        eid = int(eid)
    except Exception:
        return
    if eid <= 0:
        return
    mu = EQ_STATE.setdefault('manual_unlock', {})
    if str(eid) not in mu:
        mu[str(eid)] = 1

def manual_unlocked_equip_ids():
    # 已解锁的装备静态id 集合 = 曾获得过的 (manual_unlock) ∪ 当前背包持有的
    s = set()
    for k in (EQ_STATE.get('manual_unlock') or {}):
        try:
            s.add(int(k))
        except Exception:
            pass
    for v in (EQ_STATE.get('equip_id') or {}).values():
        try:
            s.add(int(v))
        except Exception:
            pass
    return s


# ============================================================================
# ★★2026-09-13 (v32) 共享基础设施三件套
#   · load_table()        —— 唯一正确的"表 JSON 读取器"(修 rfind(']') 惯用法)
#   · role_info_array()   —— 唯一权威的 roleInfo 30 字段 (修两份实现分叉)
#   · 下面 handlers 侧再抽出的 grant_entries() 是唯一发放实现
# ============================================================================

_TABLE_CACHE = {}

def table_dir():
    # Data tables are NOT bundled. Set YGZZ_TABLE_DIR or place them in <repo>/data/tables.
    env = (os.environ.get('YGZZ_TABLE_DIR') or '').strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.normpath(os.path.join(_DIR, '..', 'data', 'tables'))


def table_dir_alt():
    # Optional secondary data directory for legacy table sets. Returns None if unset.
    env = (os.environ.get('YGZZ_TABLE_DIR_ALT') or '').strip()
    return os.path.abspath(os.path.expanduser(env)) if env else None

def split_table_payload(d):
    """剥离数据表 JSON 的尾部封装块, 返回 (正文 bytes, 封装块 bytes)。

    ★尾块形态 (2026-09-13 全量核对: 两个轨道各 143 张表 + 各 1150 个 Lua):
      **A) [正文][0~3 字节填充][u32LE 正文长度]**  —— 142/143 张表、1150/1150 个 Lua
      **B) 纯 JSON, 完全没有尾块**                —— 仅 `T41_CodeKey.json`
         它的末尾 4 个字节正好是 `]\\r\\n]`, 被 u32LE 读出来是 0x5D0A0D5D = 1560939869,
         远超文件长度, 所以本函数会走"没有尾块"的兜底 —— 这时**必须原样返回**,
         绝不能 `d[:-4]` (那会把 JSON 的收尾 `]` 一起切掉 -> json.loads 必炸)。

    ★为什么必须看长度字段而不是 rfind(']'):
      全工程 30+ 处用的 `raw[:raw.rfind(']')+1]` 在一种情况下会切错:
      当"正文长度"这个 u32 的低字节恰好是 0x5D(']') 时, rfind 找到的是**长度字段里的那个字节**。
      实测 `data/tables/L98_AIPriority.json` size=612 u32len=605=0x25D
      -> rfind(']')=608 -> 切出的串多带 3 个 \\x00 和一个 ']'
      -> json.loads 抛 "Extra data: line 16 column 2 (char 505)" -> 调用方 except 掉 -> **静默空表**。
    """
    if len(d) < 5:
        return d, b''
    n = int.from_bytes(d[-4:], 'little')
    if 0 < n <= len(d) - 4:
        head = d[:n]
        if b'\xef\xbf\xbd' not in head:
            return head, d[n:]
    return d, b''                                   # 形态 B (或无尾块): 原样返回, 不切

def table_head(d):
    """给数据表用: 依次试两种形态并用 **json.loads 校验**, 返回 (正文 bytes, 形态名)。

    为什么要"试 + 校验"而不是只按长度切: 形态 B 的长度字段是**假**的 (来自末尾的 `]\\r\\n]`),
    只看长度会切错; 反过来只按 `rfind(']')` 又会踩 L98_AIPriority 那种"长度字段里含 ]"的坑。
    两个判据都不可单独信, 所以两个都试, 谁能让 json.loads 通过就用谁。
    """
    cands = []
    n = int.from_bytes(d[-4:], 'little') if len(d) >= 4 else 0
    if 0 < n <= len(d) - 4:
        cands.append(('tail-u32', d[:n]))
    j = d.rfind(b']')
    if j >= 0:
        cands.append(('last-bracket', d[:j + 1]))
    cands.append(('whole', d))
    last = None
    for how, head in cands:
        try:
            json.loads(head.decode('utf-8'))
            return head, how
        except Exception as e:
            last = e
    raise ValueError('数据表尾部形态无法识别 (试过 tail-u32 / last-bracket / whole): %r' % last)

def load_table(name, dirpath=None):
    """读一张明文数据表 -> list(行), [0] 是表头行。失败抛异常 (不静默返回空表)。

    ★口径统一: 旧代码四处都是 `except Exception: pass`, 表读坏时表现为"功能莫名其妙不生效",
      比直接抛错难查 10 倍。本函数**显式抛**, 让问题在第一次调用就暴露。
    """
    key = (name, dirpath)
    if key in _TABLE_CACHE:
        return _TABLE_CACHE[key]
    d = dirpath or table_dir()
    p = os.path.join(d, name)
    if not os.path.exists(p) and not name.endswith('.json'):
        p = os.path.join(d, name + '.json')      # 调用方有带/不带扩展名两种写法, 都容忍
    with open(p, 'rb') as f:
        head, _how = table_head(f.read())        # 兼容"有尾字段"与"纯 JSON 无尾块"两种形态
    rows = json.loads(head.decode('utf-8'))
    _TABLE_CACHE[key] = rows
    return rows

def load_table_index(name, key_col='ID', dirpath=None):
    """读表 -> {str(主键): 行} (主键列缺失/重复时后者胜)"""
    rows = load_table(name, dirpath)
    h = {c: i for i, c in enumerate(rows[0])}
    if key_col not in h:
        raise KeyError('%s 没有列 %s (实际表头=%s)' % (name, key_col, rows[0]))
    ki = h[key_col]
    out = {}
    for r in rows[1:]:
        try:
            out[str(r[ki])] = r
        except Exception:
            continue
    return out

# ============================================================================
# ★v60 表写入器 —— 唯一正确的"数据表写入器"（与 load_table 互为逆运算）
#
# 为什么必须有它（2026-09-16 实测出来的**静默空表**）:
#   服务端表的字节形态 = [JSON 正文][0~3 pad][u32LE 正文长度]
#   而全工程仍有 60+ 处用旧惯用法读表:
#       raw = open(p,'rb').read(); raw[:raw.rfind(']')+1]
#   它找的是**整份文件里最后一个 ']'**。0x5D 正是 ']' 的 ASCII ——
#   一旦"正文长度"这 4 个字节里**任何一个等于 0x5D**，
#   rfind(']') 找到的就是长度字段里的那个字节，切出来的串带上 pad + 半个长度字段
#   -> json.loads 抛错 -> 调用方 except 掉 -> **静默空表**（功能莫名失效，比崩还难查）。
#
#   实测 (work/_v60_legacy_scan.py):
#     · 旧加载器读不出来的表 = 2 张: L98_AIPriority(605=0x25D, 无人用 rfind 读, 无害)
#                                     T32_Achievement(23966=0x5D9E, handlers_batch:199 用它 -> 成就静默空!)
#     · 用 rfind(']') 读表的 handler 调用点 = 64 处 / 24 文件
#   ⇒ 与其改 64 处调用点（风险大、易漏），**更稳的做法是让写入端永不产生这种尾巴**:
#     把正文长度补到"4 个字节里不含 0x5D"为止（多出来的是正文末尾的空白，JSON 允许）。
#     这样 rfind(']') 找到的一定是正文真正的收尾 ']'，64 处调用点**全部同时安全**。
#
# ★护栏: work/_v60_legacy_tail_guard_test.py（挂在回归网 SUITES 里）
#   对每张表断言 ① 尾部 4 字节不含 0x5D ② 旧加载器与 load_table 结果一致，
#   并自带反例断言"种一个含 0x5D 的尾巴 -> 必须被抓住"（否则护栏是假的）。
TAIL_FORBIDDEN = 0x5D


def tail_safe_len(n):
    """返回 >= n 的最小长度 L，使 L 的 4 个 LE 字节里都不出现 0x5D。"""
    L = int(n)
    while any(b == TAIL_FORBIDDEN for b in L.to_bytes(4, 'little')):
        L += 1
    return L


def write_table(name, rows, dirpath=None):
    """把 rows 写成服务端形态的数据表（正文 + pad + u32LE 尾块），
    并保证**旧 rfind(']') 加载器也能读出同一份内容**。返回写入的正文长度。"""
    d = dirpath or table_dir()
    fn = name if str(name).endswith('.json') else str(name) + '.json'
    p = os.path.join(d, fn)
    body = json.dumps(rows, ensure_ascii=False, indent=1).replace('\n', '\r\n').encode('utf-8')
    L = tail_safe_len(len(body))
    body = body + b' ' * (L - len(body))          # 补的是正文末尾空白 -> json.loads 仍然成立
    pad = b'\x00' * ((4 - L % 4) % 4)
    with open(p, 'wb') as f:
        f.write(body + pad + L.to_bytes(4, 'little'))
    # ★2026-10-11 修：原来只 pop `(fn, dirpath)` / `(name, dirpath)`，而 `load_table` 缓存用的键是
    #   `(name, dirpath)`，**调用方传的是它自己那个 dirpath 字符串**（常见是 `None` 表示"用默认表根"）。
    #   于是"显式传 dirpath 写入"时两个键对不上 -> 写完立刻 `load_table(name)` 会拿到**旧内容**。
    #   现场：`work\_table_migrate.py` 的"双证回读"当场判红（不是它错，是这个 bug 被抓到了）。
    #   ⇒ 按"名字"清掉**所有 dirpath 变体**，不再依赖调用方传什么。
    for _k in [k for k in list(_TABLE_CACHE) if k[0] in (fn, str(name))]:
        _TABLE_CACHE.pop(_k, None)
    return L


def legacy_reader_ok(raw):
    """旧惯用法的判据：raw[:raw.rfind(']')+1] 能否 json.loads。
    给护栏测试用（生产代码不要用它读表）。"""
    i = raw.rfind(b']')
    if i < 0:
        return False, 'no-bracket'
    try:
        json.loads(raw[:i + 1].decode('utf-8'))
        return True, 'ok'
    except Exception as e:
        return False, str(e)[:80]


# ---------- roleInfo: 唯一权威实现 ----------
def role_info_array():
    """roleInfo 30 字段 (客户端 SQGamer:setRoleInfo 逐字段读) —— **全工程唯一实现**。

    ★2026-09-13 (v32) 分叉事故复盘:
      此前有两份**不同**的实现:
        · handlers_sys.role_info_array  : vit 正确, 但 field[7] 妖塔层数硬编码 1
        · handlers_bag.role_info_array  : 妖塔层数正确, 但 field[5] 体力硬编码 20、
                                          field[9]/[10] 等级/经验硬编码 1/0
      因为 game_server.py 里 `from handlers_bag import *` 在 `from handlers_sys import *` **之前**,
      外部调用拿到的是 handlers_sys 的;  而 handlers_bag **内部**5 处调用 (礼包1098/合成1177/
      碎片1168/道具1179) 按模块全局名解析, 拿到的是它自己那份。
      -> 结果: 用礼包/合成后, 客户端 roleInfo 的等级被刷成 1、经验刷成 0、体力被刷成 20;
              而领邮件时妖塔层数被刷成 1。两边各错一半, 症状不同所以一直没被当成同一个 bug。

    权威布局 (0-based 下标; 代码注释里的 [n] 是 1-based, 差一位):
      [0]account [1]rolename [2]gold [3]soulcoin [4]体力(道具1006, 上限20) [5]1
      [6]妖塔层数(≥1) [7]400 [8]player_lv [9]player_exp [10]eggcoin [11]'1'
      [12]体力计时器id [13]kidou [14]0 [15]0 [16]20
      [17]expbuff 总百分比(月卡120) [18]coinbuff 总百分比(周卡105) [19]'0' [20]'0'
      [21]0 [22]1(=roleId) [23]头像id(=客户端 img, 见下) [24]0 [25]周卡到期时间戳 [26]月卡到期时间戳 [27]0
      [28]explore_role_id [29]false
    与 game_server.role_info_json() 逐字段同构 (后者已改为直接复用本函数, 从此不可能再分叉)。

    ★★★ v58 (2026-09-16) 修一个"懒占位"引发的 P0 —— 字段 [11] 是【新手引导】字段:
      它原来是**字符串 '1'** 当占位, 但客户端对这个字段的格式要求是
      `引导id&状态(:引导id&状态)*` (见 SQGamer.lua:202 读 roleInfo[12] -> 按 ':' 与 '&' 拆)。
        · 发 '1'   -> guideArray = {{"1"}} -> 【info[2] 为 nil】
        · 客户端 NetUpdateInfo:sendMessage 第 24 行 `info[1].."&"..info[2]`
          -> attempt to concatenate a nil value -> 抛错
        · 而这个调用点在 CardTips.netCallFunc 里**没有 pcall**
          -> 整个抽卡回调中断 -> m_ToCardReward 不执行
          -> ★ 用户现象: **「符咒一抽」(cardtype=3) 抽完不出动画、不弹结果**
             (monster 其实已经进包、符也扣了; 五连抽 cardtype=4 不走这条路所以正常)
      修法: 发 `'0&0'` —— 拆出来是 {{"0","0"}}, info[1]/info[2] 都有值, 第 24 行不再抛错;
            且 id='0' 不匹配任何真实引导 id, 语义等价于"没有引导数据"。
      ⚠ 客户端侧另加了一道防御守卫 (NetUpdateInfo.lua 的 sendMessage), 纵深防御。
      详见 work\\交接文档_v58_换表修复尝试与回滚_20260916.md §14
    """
    expbuf_s, coinbuf_s, wk_end, mo_end = card_state_fields()
    vit = min(20, int((EQ_STATE.get('prop') or {}).get('1006', 0)))
    # 妖塔层数 1 起: 0 会让客户端 ceil(0/10)=0 去取不存在的 Lv0 CSB 而黑屏
    # ★★2026-10-18（真机：打完第 6 层不解锁第 7 层）：客户端的 gamer_getTowerPlies()
    #   语义是**当前可打层**（`TowerInfo.lua:164-165` 判 `towernum > plies` = 未解锁；
    #   `TowerScroll.lua:368-369` 也是 `plies >= num` 才可进），不是"已通关层数"。
    #   修前沿用 `plies`（已通关数）⇒ 打完 N 层后第 N+1 层永远打不开。
    _tw_cleared = int((EQ_STATE.get('tower') or {}).get('plies', 0) or 0)
    tower_plies = min(99, max(1, _tw_cleared + 1))
    fields = [
        '1', str(EQ_STATE.get('role_name') or '测试').replace('"', "'"), int(EQ_STATE.get('gold', 0)), int(EQ_STATE.get('soulcoin', 0)),
        vit, 1, tower_plies, 400,
        int(EQ_STATE.get('player_lv', 1)), int(EQ_STATE.get('player_exp', 0)),
        int(EQ_STATE.get('eggcoin', 0)),
        '1', vitality_timerid(), int(EQ_STATE.get('kidou', 0)), 0, 0, 20,
        expbuf_s, coinbuf_s, '0', '0', 0, 1, 0, 0,
        wk_end, mo_end,
        0, explore_role_id(), False,
    ]
    # ★★2026-10-02（本轮）**roleId 真值化**：0-based `fields[22]` = 客户端 `roleInfo[23]`
    #   = `SQGamer:setLeaderID()` 的来源（`SQGamer.lua:211` -> `:254`）。
    #   修前恒发字面量 `1` ⇒ "每个账号的 roleId 都是 1"，客户端只能靠"服务端特意给我方那行
    #   发 1"来认自己（跨账号名册/榜单都得为观察者定制）。现在发**当前账号的真实 id**
    #   （`accounts.owner_ns`：`u0002` -> 2），与 `work\guild_world.role_id_of()`
    #   **同一个算法**（两边必须一起改）。
    #   ⚠ 三条必须同时满足，否则是**静默错认**：
    #     ① **必须写 `fields[22]`**（0-based）—— `fields[23]` 已在下面被写成"头像 id"，
    #        写错槽位会**覆盖头像**而 roleId 仍为 1（本轮调研查实）；
    #     ② **单档保持 1**（`accounts.ENABLED` 为假时整段不执行）—— 单档没有账号上下文，
    #        `owner_ns` 会得到 0，而客户端 `LeaderID` 的默认值也是 1；
    #     ③ **类型必须是 JSON number**：`SQGamer.lua:211` **不做 `tonumber`**，而客户端
    #        11 处 `==` 的另一侧是裸整数 ⇒ 发成字符串会让所有比较**静默 false**。
    #   （SDK/BI 那一路**零格式约束**：`SDKChina`/`SQBIData`/`SDKCallBack` 全是 `%s`/字符串
    #     拼接，BI 那条还用引号包成字符串；且私服侧没有任何端点解析它 —— 本轮调研已核。）
    if _accounts.ENABLED:
        _cur = _accounts.EQ_STATE.current()
        if _cur is not None and getattr(_cur, 'id', None):
            fields[22] = int(_accounts.owner_ns(_cur.id) or 1)
    # ★v58: 字段 [11] = 新手引导, 必须是 `id&state` 形式 (不能是裸的 '1')。
    #   '0&0' = "无引导数据" 的安全占位; 详见本函数 docstring。
    fields[11] = '0&0'
    # ★★2026-10-02 (审计 §8 收敛的配套): 字段 [23] (0-based) = 客户端 roleInfo[24] `img` = **当前头像 id**
    #   (SQGamer.lua:213 `local img = tonumber(roleInfo[24])`)。
    #   收敛 9 份 _role_info() 副本时发现: 这是**唯一**"副本比权威更全"的字段 ——
    #   handlers_profile._role_info 一直发 `int(_hd().get('eq', DEFAULT_HEAD))`(=1001),
    #   而权威实现发的是 0。若只做"统一走权威", 1061/1062/1071 等路径的头像会从 1001 **掉成 0**。
    #   ⇒ 把头像**挪进权威实现**: 这样"收敛成一份实现"不再等于"少一个字段"。
    #   ⚠ 只读, 不 setdefault —— 取字段不应该改存档; head 未初始化时用 1001
    #     (与 handlers_profile.DEFAULT_HEAD / _hd() 的默认值同值)。
    fields[23] = int((EQ_STATE.get('head') or {}).get('eq') or 1001)

    def _fld(x):
        if isinstance(x, bool):
            return 'true' if x else 'false'
        if isinstance(x, str):
            return '"%s"' % x
        return str(x)

    return '[' + ','.join(_fld(x) for x in fields) + ']'
