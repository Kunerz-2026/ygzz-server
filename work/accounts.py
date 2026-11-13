# -*- coding: utf-8 -*-
"""accounts.py —— B 任务（多账号 + 共用世界）的存储/分派层。

## 它解决的问题
现状是**单存档单账号**：`EQ_STATE` 是 `state.py` 的模块级全局 dict、0 个 handler 带 session、
存档是单文件、`push_all()` 把邮件/背包广播给所有连接 ⇒ 两个玩家上线共享同一个角色。

## 方案（`DECISIONS` 2026-10-01 已定，本文件是它的落地）
1. **身份 = 来源 IP**（用户 2026-10-02 拍板）。`Registry.login(peer_key)` 把 peer 映射到账号 id；
   同一个 peer 重登永远拿到同一个档（I6）。**只取 host 不取 port** —— 端口每次重连都变。
2. **不改那 914 处调用点**（`work\\*.py` 里非 `_` 开头的生产文件，HEAD 口径）：
   把 `EQ_STATE` 换成一个"按账号分派读写"的**代理对象**。代理只创建一次、**永不重新绑定** ——
   全工程几十处 `from state import EQ_STATE` 拿的是同一个引用，重绑会让那些引用指向旧对象。
3. **上下文用 `contextvars`**：本工程是"每连接一线程"的纯阻塞模型，但 contextvars 额外给出
   "未 set 时**明确未绑定**"的语义，这正是 I2 要的。**已知唯一串档入口**：子线程默认拿不到父上下文
   —— 本工程目前**没有**任何在 handler 里新起线程的地方（`_push_mail_delayed` 是死代码，
   全仓零调用点），所以只有 `game_server.py` 起连接线程那一处需要 `copy_context()`。

## 三条"错了会静默出事"的设计点（都有实测出处）
* **未绑定 → 每次新建空 dict**：不能返回共享哨兵 —— `_b5` 实测抓到"上一次误写的值留到下一次未绑定读"
  （读到 99999）。空 dict 是 falsy，更不能写成 `tls.d or default`（`clear()` 之后会**静默退回默认档**＝回档）。
* **落盘临时文件唯一化**（`<file>.<pid>.<tid>.tmp` + `os.replace` + 有界重试）：
  固定名 `.tmp` 在并发下会把半截 JSON 换上去（`state.py` 2026-10-02 修过同一个坑）。
  这里**故意让名字以 `.tmp` 结尾**，好让 `_b6` 的"没有残留 .tmp"判据真的能抓到残留。
* **索引读-改-写必须整段持锁**：`_b6` I7 实测 6 连接并发首登会撞 `PermissionError`，
  并可能分配出重复 id。

## 开关
`YGZZ_MULTI_ACCOUNT=1` 才启用（`ENABLED`）。**默认关闭** ⇒ 生产行为与改造前逐字节一致，
回归网那 77 套"单档假设"的套件因此不受影响。启用时 `game_server.py` 会 `accounts.enable()`。
"""
import contextvars
import copy
from contextlib import contextmanager
import json
import os
import shutil
import threading
import time

ENABLED = os.environ.get('YGZZ_MULTI_ACCOUNT') == '1'

_ACCT_DIR_NAME = 'accounts'
_INDEX_NAME = '_accounts.json'
# ★2026-10-02 (任务 F / D3=C): "客户端自报账号"在索引里的键前缀。
#   加前缀是为了与"来源 IP"那一类键**永不碰撞** —— 两类键共存于同一个 `_accounts.json`。
_USER_KEY_PREFIX = 'user:'

# ★2026-10-02 (任务 D): 跨账号展示 id 的命名空间基数。
#   问题：**每个账号的 ucode / commentId 都各自从 1 起计数** ⇒ A 的 `1` 与 B 的 `1` 是同一个
#   显示 id，"点别人的行"会打到自己的东西上（或"点赞打到别人那条"）。
#   做法：`display = owner_ns(aid) * NS_BASE + 原 id`，`owner_ns` **从账号 id 本身推出来**
#   （`u0002` -> 2）—— 不能用"枚举第几个"，那个会随账号增减漂移，同一行在不同时刻/不同人
#   屏幕上 id 不同，回传就对不上了。
#
#   ⚠⚠ **基数必须大到原始 id 够不着**：第一版取 `100000`，结果 `_fix26b_selftest.py` 的
#   D1（拿 `ucode="999999"` 试"未知 ucode 也要给可用数据"）当场判红 —— 因为 **`999999`
#   是一个合法的 ucode**（`alloc_monster_ucode` 从 1 起单调发号），却被读成"命名空间 9"。
#   取 `10**9` 之后，原始 id（评论 1..n、ucode 顺序发号）在现实中够不到这个量级；
#   且 10 位数仍在 JSON/Lua double 的精确整数范围内（2^53）。
NS_BASE = 10 ** 9
# 世界级键：与账号无关、所有玩家看到同一份（"共用世界"的核心）。
# ★2026-10-02 (D4=B) 把口径钉死，避免下一个人再把它当"待办清单"：
#   · **排行不是世界键** —— 它是"各账号私有真值的**派生视图**"（排序而已），走
#     `all_accounts_full()` 跨账号取标量（见 `handlers_rank._world_rows`）。
#     放进 `world` 等于制造**第二份真值**、必须双向同步。
#   · 同理：点赞数 = 计数（派生）、入侵伤害 = 各账号自有 —— 都不是世界键。
#   · 真正**必须**世界级的是"没有归属账号的**共享实体**"：**妖盟 / 妖盟 Boss**。
# ★★2026-11-05（执行书 C2「最小真实好友」）：**`friends` 进世界键** —— 上面 2026-10-02 那条
#   "好友 = 成对边（派生）"只对**已成立的好友关系**成立。真正无法派生的是这三样：
#     ① **申请**（A 发给 B 的待处理请求，属于"有一段单向待办"，不写在谁的档里都是半个真值）；
#     ② **每日赠收额度**（谁今天送过谁、领过谁 —— 跨账号的当日账，必须一处）；
#     ③ 接受/删除要**同时**改两个账号的投影，需要一份可重放的共享日志（`guild_transaction`）。
#   ⇒ 世界键里只放这三样（`edges`/`requests`/`gifts`），**好友关系本身仍以 `edges` 为准**
#     （双向各一条），不制造第二份"我的好友列表"真值。
WORLD_KEYS = ('chat_log', 'guilds', 'friends')

# ★★2026-10-02（本轮）**形状表**：世界键的**容器类型**。
#   为什么必须有它：旧实现是 `self.world = {k: [] for k in WORLD_KEYS}` —— 把**每一个**
#   世界键都当列表。`guilds` 是 **dict** 形状（`guildId -> 妖盟`），于是
#   `world.setdefault('guilds', {})` 拿回一个**已存在的空列表**，紧接着 `g[k] = v`
#   当场 `TypeError: list indices must be integers`；而错误现场离"形状表写错了"很远。
#   ⇒ 新增世界键时**必须同时**在这里声明形状，`Registry._world_defaults()` 按它建。
#   ⚠ 判据不是"初始值是不是空"：空列表与空 dict 在 `if not x` 下不可分辨，
#     这正是旧口径能骗过一轮的原因 —— 见 `_guild_world_test.py` 的 G1。
WORLD_SHAPES = {'chat_log': list, 'guilds': dict, 'friends': dict}
_WORLD_FILE_NAME = '_world.json'
_GUILD_JOURNAL_NAME = '_guild_txn.json'


# ===========================================================================
class AccountsError(RuntimeError):
    pass


class _ScopedState(object):
    """按**账号**分派读写的 dict 代理（914 处调用点零改动）。

    覆盖了工程实际用到的全部形态（`_b5` 用 AST 分类过 2141 个节点）：
    下标读写 / `.get` / `.setdefault` / `.update` / `.pop` / `.clear` / `in` / 迭代 /
    `len()` / `keys()` / `items()` / `dict(proxy)`（`state.py:1445` 的生产序列化路径）。
    **工程里没有**任何 `isinstance(EQ_STATE, dict)` / `type()` / 真值判断 —— 实测全为 0 处。
    """

    def __init__(self):
        self._cv = contextvars.ContextVar('ygzz_account', default=None)
        self._unbound_hits = []          # 供自检："未绑定时的读写"必须能被发现

    # ---- 上下文 ----
    def bind(self, acc):
        if acc is None:
            raise ValueError('bind(None) 不被允许 —— 要清空请显式 unbind()')
        self._cv.set(acc)

    def unbind(self):
        self._cv.set(None)

    def current(self):
        return self._cv.get()

    def is_bound(self):
        return self._cv.get() is not None

    def _data(self):
        acc = self._cv.get()
        if acc is None:
            # ★I2：绝不落到某个真实账号。返回**每次新建**的空 dict：
            #   读 -> 显眼地空；写 -> 写进一个马上被丢弃的对象（不留痕迹）。
            self._unbound_hits.append(threading.current_thread().name)
            return {}
        return acc.data

    # ---- dict 协议 ----
    def __getitem__(self, k): return self._data()[k]
    def __setitem__(self, k, v): self._data()[k] = v
    def __delitem__(self, k): del self._data()[k]
    def __contains__(self, k): return k in self._data()
    def __iter__(self): return iter(self._data())
    def __len__(self): return len(self._data())
    def get(self, k, d=None): return self._data().get(k, d)
    def setdefault(self, k, d=None): return self._data().setdefault(k, d)
    def update(self, *a, **kw): return self._data().update(*a, **kw)
    def clear(self): return self._data().clear()
    def keys(self): return self._data().keys()
    def values(self): return self._data().values()
    def items(self): return self._data().items()
    def pop(self, k, *a): return self._data().pop(k, *a)
    def copy(self): return dict(self._data())
    def __eq__(self, other):
        # 工程里对 EQ_STATE 本身没有 == 比较（实测 0 处）；保留语义避免"恒 False"的暗雷。
        if isinstance(other, _ScopedState):
            return self._data() == other._data()
        return self._data() == other
    __hash__ = None

    def __repr__(self):
        acc = self._cv.get()
        return 'ScopedState(%s, %d keys)' % (
            acc.id if acc else '<未绑定>', len(self._data()))


EQ_STATE = _ScopedState()


class Account(object):
    __slots__ = ('id', 'name', 'data')

    def __init__(self, aid, name, data):
        self.id = aid
        self.name = name
        self.data = data

    def __repr__(self):
        return 'Account(%s, %r)' % (self.id, self.name)


# ===========================================================================
class Registry(object):
    """账号 -> 存档。**全部常驻内存**：几十人规模下内存可忽略，换来"共用世界"可以直接遍历所有账号取数。"""

    _IDX_LOCK = threading.RLock()

    def __init__(self, root, seed=None):
        self.root = root
        self.seed = seed if seed is not None else {}
        self.by_id = {}
        self.online = {}          # account_id -> 连接描述集合（谁在线）
        self.world = self._world_defaults()
        self.state = EQ_STATE
        self._guild_lock = threading.RLock()
        self._guild_tx_owner = None
        self._guild_tx_depth = 0
        self._guild_tx_accounts = {}
        self._guild_tx_forced = set()
        self._guild_tx_aborted = False
        self._guild_tx_force_world = False
        self.guild_world_initialized = False
        os.makedirs(root, exist_ok=True)
        self._recover_guild_journal()
        self.world_preexisting = os.path.isfile(self._world_path())
        self._load_world()

    # ---- 世界级（I5）：落盘 + 形状 ----
    @staticmethod
    def _world_defaults():
        """按 `WORLD_SHAPES` 造一份空世界（**每个键的容器类型都要对**，见该表注释）。"""
        out = {}
        for k in WORLD_KEYS:
            out[k] = WORLD_SHAPES[k]()
        return out

    def _world_path(self):
        return os.path.join(self.root, _WORLD_FILE_NAME)

    def _load_world(self):
        """把 `_world.json` 读进 `self.world`。

        ★ 与"账号档解析失败"同一条纪律（`DECISIONS` 2026-10-02）：**不静默降级成空世界**。
          妖盟与入会申请是**不可再生**的共享数据，坏文件被当成"新装"会静默丢掉整个世界的妖盟，
          而服务端照常起来 —— 这正是最坏的一种失败方式。故：坏文件另存 `.corrupt-<ts>` 后抛错。
        ★ 文件**不存在**不是错误（新装/单档从没写过），此时保持空世界。
        """
        p = self._world_path()
        if not os.path.isfile(p):
            return self.world
        try:
            with open(p, encoding='utf-8') as f:
                d = json.load(f)
        except Exception as e:
            d = None
            self._world_corrupt(p, e)
        if not isinstance(d, dict):
            self._world_corrupt(p, '顶层不是 dict: %r' % (type(d).__name__,))
        out = self._world_defaults()
        for k in WORLD_KEYS:
            v = d.get(k, None)
            if v is None:
                continue                      # 老文件里没这个键 -> 用空形状补上（前向兼容）
            if not isinstance(v, WORLD_SHAPES[k]):
                self._world_corrupt(p, '世界键 %r 形状不对: 期望 %s, 实际 %s'
                                    % (k, WORLD_SHAPES[k].__name__, type(v).__name__))
            out[k] = v
        self.world = out
        self.guild_world_initialized = 'guilds' in d
        return self.world

    def _world_corrupt(self, p, why):
        q = p + '.corrupt-%s' % time.strftime('%Y%m%d-%H%M%S')
        try:
            shutil.copy2(p, q)
        except Exception:
            q = '(另存失败)'
        raise AccountsError('世界档 %s 解析失败: %r\n  已另存为 %s —— 请人工处置后再启动。'
                            % (p, why, q))

    def save_world(self):
        """妖盟事务内延迟写盘；其他调用持同一把锁保存完整世界快照。"""
        with self._guild_lock:
            if self._in_guild_tx():
                self._guild_tx_force_world = True
                return self._world_path()
            if os.path.isfile(self._guild_journal_path()):
                raise AccountsError('存在待恢复的妖盟事务；拒绝独立写世界档')
            p = self._save_json(self._world_path(), self.world)
            self.world_preexisting = True
            self.guild_world_initialized = True
            return p

    @staticmethod
    def _save_json(path, data):
        tmp = _tmp_name(path)
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            _replace(tmp, path)
        finally:
            _drop(tmp)
        return path

    def _guild_journal_path(self):
        return os.path.join(self.root, _GUILD_JOURNAL_NAME)

    def _in_guild_tx(self):
        return self._guild_tx_depth > 0 and self._guild_tx_owner == threading.get_ident()

    def _guild_snapshot_account(self, acc):
        if self._in_guild_tx() and acc is not None and acc.id not in self._guild_tx_accounts:
            self._guild_tx_accounts[acc.id] = (acc, copy.deepcopy(acc.data))

    def _recover_guild_journal(self):
        """重放已提交的完整快照。中途再失败时保留日志并拒绝继续。"""
        p = self._guild_journal_path()
        if not os.path.isfile(p):
            return False
        try:
            with open(p, encoding='utf-8') as f:
                d = json.load(f)
            world = d['world']
            accs = d['accounts']
            if d.get('version') != 1 or not isinstance(world, dict) or not isinstance(accs, dict):
                raise ValueError('事务日志结构错误')
            for k in WORLD_KEYS:
                if not isinstance(world.get(k), WORLD_SHAPES[k]):
                    raise ValueError('事务日志世界键形状错误: %s' % k)
            for aid, data in accs.items():
                if not _account_id_number(aid) or not isinstance(data, dict):
                    raise ValueError('事务日志账号无效: %r' % aid)
        except Exception as e:
            raise AccountsError('妖盟事务日志 %s 无法读取；拒绝启动/写入，请保留现场。' % p) from e
        try:
            self._save_json(self._world_path(), world)
            for aid, data in sorted(accs.items()):
                self._save_json(self._path(aid), data)
            os.remove(p)
        except Exception as e:
            raise AccountsError('妖盟事务日志 %s 重放失败；拒绝继续写入。' % p) from e
        self.world = world
        self.world_preexisting = True
        self.guild_world_initialized = True
        if not self._in_guild_tx():
            for aid, data in accs.items():
                if aid in self.by_id:
                    self.by_id[aid].data = copy.deepcopy(data)
        return True

    @contextmanager
    def guild_transaction(self, *accounts):
        """串行化妖盟判定与变更；日志先落盘，重启可幂等补齐两类 JSON。"""
        with self._guild_lock:
            if self._in_guild_tx():
                self._guild_tx_depth += 1
                try:
                    for acc in accounts:
                        self._guild_snapshot_account(acc)
                    yield
                except Exception:
                    self._guild_tx_aborted = True
                    raise
                finally:
                    self._guild_tx_depth -= 1
                return
            self._recover_guild_journal()
            self._guild_tx_owner = threading.get_ident()
            self._guild_tx_depth = 1
            self._guild_tx_accounts = {}
            self._guild_tx_forced = set()
            self._guild_tx_aborted = False
            self._guild_tx_force_world = False
            before_world = copy.deepcopy(self.world)
            current = self.state.current() if callable(getattr(self.state, 'current', None)) else None
            self._guild_snapshot_account(current)
            for acc in accounts:
                self._guild_snapshot_account(acc)
            journal_written = False
            committing = False
            try:
                yield
                if self._guild_tx_aborted:
                    raise AccountsError('嵌套妖盟操作失败；整个请求已撤销')
                changed = {aid: copy.deepcopy(acc.data)
                           for aid, (acc, old) in self._guild_tx_accounts.items()
                           if aid in self._guild_tx_forced or acc.data != old}
                if self.world != before_world or changed or self._guild_tx_force_world:
                    journal = {'version': 1, 'world': copy.deepcopy(self.world), 'accounts': changed}
                    committing = True
                    self._save_json(self._guild_journal_path(), journal)
                    journal_written = True
                    self._recover_guild_journal()
            except Exception as e:
                if not journal_written:
                    self.world = before_world
                    for acc, old in self._guild_tx_accounts.values():
                        acc.data = old
                if committing and not isinstance(e, AccountsError):
                    raise AccountsError('妖盟事务提交失败；未向客户端确认成功。') from e
                raise
            finally:
                self._guild_tx_owner = None
                self._guild_tx_depth = 0
                self._guild_tx_accounts = {}
                self._guild_tx_forced = set()
                self._guild_tx_aborted = False
                self._guild_tx_force_world = False

    def world_ensure(self):
        """仅首次迁移旧账号声明时调用；已有 guilds 世界档绝不再从缓存自举。"""
        changed = False
        accs = self.all_accounts_strict(require_index=False)
        # 1) 先补 `guild_id`（已有 id 或没有会名的跳过）
        groups = {}
        for acc in accs:
            aid = getattr(acc, 'id', None)
            d = acc.data if isinstance(acc.data, dict) else {}
            sg = d.get('sociaty')
            if not aid or not isinstance(sg, dict):
                continue
            name = str(sg.get('name') or '').strip()
            if not name:
                continue
            gid = str(sg.get('guild_id') or '')
            if gid:
                continue                       # 已经接上了
            groups.setdefault(name, []).append(acc)
        for name, members in sorted(groups.items()):
            gid = None
            for g in self.world['guilds'].values():
                if isinstance(g, dict) and str(g.get('name') or '') == name:
                    gid = g.get('id')
                    break
            if not gid:
                gid = self._next_guild_id()
            g = self.world['guilds'].setdefault(gid, {})
            if not isinstance(g, dict):
                g = self.world['guilds'][gid] = {}
            g.setdefault('id', gid)
            g['name'] = name
            g.setdefault('notice', '')
            g.setdefault('autoJoin', 0)
            g.setdefault('autoJoinThreshold', 0)
            g.setdefault('created', '')
            if not isinstance(g.get('members'), dict):
                g['members'] = {}
            lv, wealth = 1, 0
            for acc in members:
                sg = acc.data.get('sociaty') or {}
                self._guild_member_set(g, acc, 'A' if not g['members'] else 'M')
                # ★★ **必须顺手把弱引用写回去**：本函数改的是"谁在名册里"，而 `guild_of()`
                #   优先认 `sociaty.guild_id`。只改名册不写弱引用 ⇒ 下一趟
                #   `world_rebuild_roster()` 会按**旧**的 `guild_id` 反查到**旧的会**，
                #   把这个账号又拽回去（本轮实测：乙的档已改成"乙会"，自检后却又变回"甲会"，
                #   而"乙会"建出来了却一个人都没有）。写了弱引用，自检才是幂等的。
                sg['guild_id'] = str(gid)
                lv = max(lv, _i(sg.get('level', 1), 1))
                wealth += _i(sg.get('wealth', 0), 0)
            g['level'] = max(_i(g.get('level', 1), 1), lv)
            g['wealth'] = max(_i(g.get('wealth', 0), 0), wealth)
            g.setdefault('icon', _i((members[0].data.get('sociaty') or {}).get('icon', 0), 0))
            for acc in members:
                self._mirror_guild_into_account(acc, g)
            changed = True
        # 2) 清掉"世界键里指向不存在的会"的悬空引用（不删账号档的会名 —— 那是它自己的记录）
        for acc in accs:
            d = acc.data if isinstance(acc.data, dict) else {}
            sg = d.get('sociaty')
            if isinstance(sg, dict) and sg.get('guild_id'):
                if str(sg.get('guild_id')) not in self.world['guilds']:
                    sg['guild_id'] = ''
                    changed = True
        if changed:
            self.save_world()
        return changed

    def _next_guild_id(self):
        """**不复用**已删的 id —— 复用会让老客户端的回传打到新会上。"""
        mx = 0
        for gid in self.world['guilds'].keys():
            s = str(gid)
            if len(s) == 6 and s[0] == 'g' and s[1:].isdigit():
                mx = max(mx, int(s[1:]))
        return 'g%05d' % (mx + 1)

    def world_rebuild_roster(self):
        """仅首次迁移按旧账号会名建名册；正常启动由世界单向回灌账号。"""
        changed = False
        accs = self.all_accounts_strict(require_index=False)
        claims = {}                     # aid -> 声明的会名（'' = 未入会）
        for acc in accs:
            d = acc.data if isinstance(acc.data, dict) else {}
            sg = d.get('sociaty')
            name = ''
            if isinstance(sg, dict):
                name = str(sg.get('name') or '').strip()
            claims[acc.id] = name
        # ① 会名 -> 会（不存在就建）
        for aid, name in sorted(claims.items()):
            if not name:
                continue
            gid, g = None, None
            for k, g2 in sorted(self.world['guilds'].items()):
                if isinstance(g2, dict) and str(g2.get('name') or '').strip() == name:
                    gid, g = str(k), g2
                    break
            if gid is None:
                gid = self._next_guild_id()
                g = {'id': gid, 'name': name, 'icon': 0, 'level': 1, 'wealth': 0,
                     'notice': '', 'autoJoin': 0, 'autoJoinThreshold': 0,
                     'created': '', 'members': {}, 'applications': {}}
                self.world['guilds'][gid] = g
                changed = True
            # 该在**这个**会里：别的会里的同名成员记录一律摘掉
            for k, g2 in list(self.world['guilds'].items()):
                if not isinstance(g2, dict) or str(k) == gid:
                    continue
                mem = g2.get('members') if isinstance(g2.get('members'), dict) else {}
                if aid in mem:
                    del mem[aid]
                    changed = True
            mem = g.get('members') if isinstance(g.get('members'), dict) else {}
            if aid not in mem:
                acc = self._by_id_get(aid)
                # 名册首位（且原本没有盟主）= 自举出来的盟主（老会没有 `L` 时的兜底）
                has_leader = any(str((r or {}).get('position') or '') == 'L'
                                 for r in mem.values() if isinstance(r, dict))
                # ★★2026-10-08（本轮实测踩到）：这里原本写 'A'/'M' —— **全错**。
                #   客户端 `VSociatyMember.lua:307/308/480-486` 与
                #   `ControllerMSociatyInfoHome.lua:461` 认的职位码是 **"L"/"FL"/"A"/"N"**，
                #   而服务端 `guild_world._CAN_MANAGE = ("L", "FL")` 与客户端同一判据。
                #   ⇒ 给 'A'/'M' 的自举会里**没有任何人能管理**：审批入会、踢人、调职位、改公告、
                #     改入会类型、以及本轮新做的**弈局开启/升级**（1146/1148）全部回 `50005`。
                #     实测形态：三个账号档带会名迁移后，盟主账号职位的上位是 `A` —— 功能像没做。
                self._guild_member_set(g, acc, 'N' if has_leader else 'L')
                changed = True
        # ② 没声明会名的，不许留在任何名册里（含"已经被解散/退会"的残留）
        for k, g in list(self.world['guilds'].items()):
            if not isinstance(g, dict):
                continue
            mem = g.get('members') if isinstance(g.get('members'), dict) else {}
            for aid in list(mem.keys()):
                if not claims.get(aid):
                    del mem[aid]
                    changed = True
        # ③ 会等级照成员重算（沿用改造前的派生口径，不改变玩家看到的数字）
        #   ★★ **财富绝不能在这里"照成员求和"** —— 那是把"两套真值"原样搬进来：
        #     自举那一刻 `sociaty['wealth']` 已经是**会财富**（不是"我这个人的钱"），
        #     再求和一次就等于**每多一个成员就把会财富翻一倍**，而且每跑一次自检再翻一倍。
        #     改造前 `wealth = 成员之和` 之所以成立，是因为那时它真的是"每人各存一份的钱"。
        #     现在会财富是**世界键里的单一真值**（捐献/商店扣钱都只改它），只在**建会那一刻**
        #     用"成员之和"做一次初始快照（见 `world_ensure()`），之后只由捐献增减。
        #   ★ 另一个必须在这里做的：**没有管理层的老会要补一个盟主**。
        #     自举出来的会没有 `L`，而 1126 审批/1127 踢人/1130 调职位/1128 公告/1132 入会类型
        #     **全部**要 `can_manage()` ⇒ 那种会里**没有任何人能管理**（建会的人从没点过"建会"）。
        #     规则：只有名册里连 `A` 都没有时才补，且补最早加入的那个（稳定、可预期）。
        for k, g in list(self.world['guilds'].items()):
            if not isinstance(g, dict):
                continue
            mem = g.get('members') if isinstance(g.get('members'), dict) else {}
            if not mem:
                continue
            lv = 1
            any_leader = any(str((r or {}).get('position') or '') == 'L'
                             for r in mem.values() if isinstance(r, dict))
            if not any_leader:
                first = sorted(mem.keys())[0]
                row = mem[first] if isinstance(mem[first], dict) else {}
                # ★★2026-10-08：补的必须是 **"L"**（客户端与服务端 `_CAN_MANAGE` 同一判据）。
                #   这里原本补 'A'，而 'A'（=管理员）**不在** `_CAN_MANAGE` 里 ⇒ "补了盟主"
                #   这句注释是假的：补完之后仍然没有人能管理（审批/踢人/调职位/公告/弈局全 50005）。
                if str(row.get('position') or '') in ('', 'N', 'M', 'A'):
                    row['position'] = 'L'
                    mem[first] = row
                    any_leader = True
                    changed = True
            for aid in mem.keys():
                acc = self._by_id_get(aid)
                sg = (acc.data.get('sociaty') if acc is not None else None) or {}
                lv = max(lv, _i(sg.get('level', 1), 1))
            if _i(g.get('level', 1), 1) != lv:
                g['level'] = lv
                changed = True
        # ④ 把结果回灌进各账号的缓存（会名/会 id/等级/财富/公告）
        import guild_world
        for acc in accs:
            if guild_world.sync_account(acc):
                changed = True
        if changed:
            self.save_world()
        return changed

    def world_sync_accounts(self):
        """已有世界档启动时只从名册回灌缓存，不让旧账号声明反写世界。"""
        import guild_world
        accs = self.all_accounts_strict()
        known = {acc.id for acc in accs}
        for g in self.world['guilds'].values():
            if not isinstance(g, dict) or not isinstance(g.get('members'), dict):
                raise AccountsError('妖盟世界名册结构错误；拒绝启动')
            for aid in g['members']:
                if aid not in known:
                    raise AccountsError('妖盟成员 %s 的账号档不存在；拒绝启动' % aid)
        changed = False
        for acc in accs:
            if guild_world.sync_account(acc):
                self.save(acc)
                changed = True
        return changed

    def _by_id_get(self, aid):
        try:
            return self.get_or_create(aid)
        except Exception:
            return None

    def _guild_member_set(self, g, acc, position='M'):
        """把 `acc` 写进 `g['members']`（**键是账号 id 的字符串**：JSON 的键只能是字符串）。"""
        if not isinstance(g.get('members'), dict):
            g['members'] = {}
        row = g['members'].get(acc.id)
        if not isinstance(row, dict):
            row = {}
            g['members'][acc.id] = row
        row['position'] = position
        row.setdefault('join_at', int(time.time()))
        row.setdefault('offer', 0)
        return row

    def _mirror_guild_into_account(self, acc, g):
        """把**世界键里的会**回灌到该账号档的 `sociaty` 缓存。

        ⚠ 它现在**委托** `guild_world.sync_account()`（唯一实现）—— 这份逻辑原先在这里写了一份，
          而 `handlers_sociaty._sg()` 每次取会数据都要跑一遍 ⇒ 两份实现必然漂移。
          `guild_world` 反查本模块只走 `accounts.REGISTRY` 这个**运行期**引用，不构成循环 import。
        """
        import guild_world
        guild_world.sync_account(acc)
        return (acc.data or {}).get('sociaty') or {}

    # ---- 账号索引（I6）----
    def _index_path(self):
        return os.path.join(self.root, _INDEX_NAME)

    def _load_index(self):
        p = self._index_path()
        try:
            with open(p, encoding='utf-8') as f:
                d = json.load(f, object_pairs_hook=self._index_pairs)
        except FileNotFoundError as e:
            if self._known_account_ids():
                raise AccountsError('账号索引 %s 缺失，但已有账号；拒绝重新分配身份。' % p) from e
            return {}
        except Exception as e:
            raise AccountsError('账号索引 %s 读取失败；保留原文件，请人工处置。' % p) from e
        if not isinstance(d, dict):
            raise AccountsError('账号索引 %s 顶层必须是 dict' % p)
        seen = set()
        for key, aid in d.items():
            if not isinstance(key, str) or not key.strip():
                raise AccountsError('账号索引 %s 含空身份键' % p)
            if not _account_id_number(aid):
                raise AccountsError('账号索引 %s 含非法账号 id: %r' % (p, aid))
            if aid in seen:
                raise AccountsError('账号索引 %s 多个身份指向同一账号: %s' % (p, aid))
            seen.add(aid)
        if not d and self._known_account_ids():
            raise AccountsError('账号索引 %s 为空，但已有账号；拒绝重新分配身份。' % p)
        return d

    @staticmethod
    def _index_pairs(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise AccountsError('账号索引含重复身份键: %r' % key)
            out[key] = value
        return out

    def _known_account_ids(self):
        """孤立存档和已加载账号也占用 id；索引条目数量不能作为发号器。"""
        ids = set(self.by_id)
        try:
            for name in os.listdir(self.root):
                if name.endswith('.json') and _account_id_number(name[:-5]):
                    ids.add(name[:-5])
        except OSError as e:
            raise AccountsError('无法核对账号目录 %s；拒绝分配身份。' % self.root) from e
        return ids

    def _save_index(self, idx):
        p = self._index_path()
        tmp = _tmp_name(p)
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(idx, f, ensure_ascii=False, indent=2)
            _replace(tmp, p)
        finally:
            _drop(tmp)

    def login(self, device_key):
        """登录/注册：同一 device_key 永远得到同一个账号（I6）。

        ⚠ 先取妖盟/请求锁再取索引锁；与 get_or_create 的锁顺序一致。
        读索引 -> 分配 id -> 写索引仍是不可分割的临界区。
        """
        if not isinstance(device_key, str) or not device_key.strip():
            raise AccountsError('device_key 不能为空（否则所有人共用一档）')
        with self._guild_lock:
            with Registry._IDX_LOCK:
                idx = self._load_index()
                aid = idx.get(device_key)
                if not aid:
                    used = set(idx.values()) | self._known_account_ids()
                    aid = 'u%04d' % (max((_account_id_number(x) for x in used), default=0) + 1)
                    idx[device_key] = aid
                    self._save_index(idx)
            return self.get_or_create(aid)

    def get_or_create(self, aid):
        with self._guild_lock:
            if not self._in_guild_tx():
                self._recover_guild_journal()
            with Registry._IDX_LOCK:
                if aid in self.by_id:
                    acc = self.by_id[aid]
                    self._guild_snapshot_account(acc)
                    return acc
                acc = self._load(aid)
                self.by_id[aid] = acc
                self._guild_snapshot_account(acc)
                return acc

    def find(self, device_key):
        """只查不建：给"这个 peer 之前登录过吗"这类只读判断用。"""
        return self._load_index().get(device_key)

    def exists(self, aid):
        """这个账号**已经在磁盘上或已加载**吗（只查不建）。

        ★ 与 `get_or_create` 的区别就是这条的全部意义。后台把一个拼错的 `acct` 传进来时，
          `get_or_create` 会拿种子档**凭空调一个新账号**，而且它进 `by_id`；
          还会让后台拼错的账号出现在跨账号视图中。⇒ 所有来自 URL 的账号 id 一律走
          `exists()`，不存在就 404；登录发号另行避开所有已占用 id。
        """
        if aid in self.by_id:
            return True
        try:
            return os.path.isfile(self._path(aid))
        except Exception:
            return False

    # ---- 落盘（I4）----
    def _path(self, aid):
        return os.path.join(self.root, aid + '.json')

    def _load(self, aid):
        p = self._path(aid)
        data = None
        if os.path.isfile(p):
            try:
                with open(p, encoding='utf-8') as f:
                    data = json.load(f)
            except Exception as e:
                # ★不允许静默降级成种子档：那等于把玩家档换成空档，且下一次 save 就覆盖回磁盘。
                #   与 state.py 2026-10-02 那条"存档解析失败时拒绝启动"同一个理由。
                q = p + '.corrupt-%s' % time.strftime('%Y%m%d-%H%M%S')
                try:
                    shutil.copy2(p, q)
                except Exception:
                    q = '(另存失败)'
                raise AccountsError('账号档 %s 解析失败: %r\n  已另存为 %s —— 请人工处置后再启动。'
                                    % (p, e, q))
        if data is None:
            data = json.loads(json.dumps(self.seed))     # 深拷贝种子
        if not isinstance(data, dict):
            raise AccountsError('账号档 %s 不是 dict' % p)
        # ★★2026-10-18（真机：背包出现两本 "Dummy" 技能书）：多账号模式下账号档是
        #   `json.load` 直接进内存的，**不经过 `state.load_state()` 的迁移段** ——
        #   于是 `_purge_skill_bag_data()` 只对"模板档"生效，真实玩家档里的历史脏行
        #   （id<=0 的技能行，2026-10-11 那次错误"补齐"留下的 sk1/sk8）永远清不掉。
        #   这里在**每次加载账号档**时跑一遍同一套技能迁移：只删 id<=0 的不可渲染行，
        #   并把它们的 ucode 从 `skill_produced` 摘掉（不编造、不重建）。
        #   吞异常：迁移问题不能阻止玩家进游戏（失败时保持原样，至少不会更坏）。
        try:
            import state as _st
            _st._purge_skill_bag_data(data)
        except Exception as _e:
            print('[ACCT] !! 账号档技能迁移失败(不影响登录): %r' % (_e,), flush=True)
        # ★★2026-10-18（UG 任务 1312 映射反转）：账号档同样不经过 `state.load_state()`，
        #   所以把 `handlers_ugtask.migrate_story_flags` 也挂在这里 —— 清掉误记的
        #   `fplot`、按 `story_seen` 补 `aplot`（幂等，打 `ug.ver`）。吞异常同技能迁移。
        try:
            from handlers_ugtask import migrate_story_flags as _mig_ug
            _mig_ug(data)
            # ★★2026-10-22（审查判红 B2）：按-Value CT 的旧共享计数（妖卵刷出来的 CT=23 等）
            #   搬进 `ug_ctv_legacy` 并清掉 —— 与模板档路径同一条迁移，幂等。
            from handlers_ugtask import migrate_value_scoped_ct as _mig_ctv
            if _mig_ctv(data):
                print('[ACCT] 隔离按-Value CT 旧共享计数 -> aid=%s' % (aid,), flush=True)
        except Exception as _e:
            print('[ACCT] !! 账号档 UG 剧情标记迁移失败(不影响登录): %r' % (_e,), flush=True)
        # ★★2026-10-20（主线 21016 卡死）：初始怪「江酌月」(300601) 补发 —— 与模板档同一条迁移
        #   （见 state.ensure_initial_monsters 的取证）。账号档不经过 load_state，必须挂在这里。
        try:
            import state as _st2
            if _st2.ensure_initial_monsters(data):
                print('[ACCT] 补发初始怪 江酌月(300601) -> aid=%s' % (aid,), flush=True)
        except Exception as _e:
            print('[ACCT] !! 账号档初始怪补发失败(不影响登录): %r' % (_e,), flush=True)
        trace('load aid=%s file=%s from_disk=%s recharge=%s'
              % (aid, os.path.basename(p), os.path.isfile(p),
                 (data.get('recharge') or {}).get('total')))
        return Account(aid, data.get('role_name') or aid, data)

    def save(self, acc):
        with self._guild_lock:
            if self._in_guild_tx():
                self._guild_snapshot_account(acc)
                self._guild_tx_forced.add(acc.id)
                return self._path(acc.id)
            if os.path.isfile(self._guild_journal_path()):
                raise AccountsError('存在待恢复的妖盟事务；拒绝独立写账号档')
            # ★2026-10-12 (多人单服排查): 串档类故障的唯一硬证据是"写盘时到底绑了谁"。
            #   默认关（`trace_on()` 为假时一个字节都不写），生产路径逐字节不变。
            trace('save aid=%s file=%s recharge=%s shop666=%s'
                  % (acc.id, os.path.basename(self._path(acc.id)),
                     (acc.data.get('recharge') or {}).get('total'),
                     (acc.data.get('shop_buy') or {}).get('666')))
            return self._save_json(self._path(acc.id), acc.data)

    def save_current(self):
        acc = self.state.current()
        if acc is None:
            raise AccountsError('save_current(): 当前线程没有账号上下文 —— 拒绝落盘(防串档)')
        self.save(acc)

    # ---- 共用世界（I5）----
    def all_accounts(self):
        return list(self.by_id.values())

    def all_accounts_full(self):
        """**磁盘上的全部账号**(不只是本进程已加载的那些) —— 跨账号取数用这个。

        ⚠ 为什么不能直接用 `all_accounts()`: 它返回 `by_id`, 而 `by_id` 只由 `get_or_create`
        填充, `get_or_create` 又只在"某个 peer 登录"时被调 ⇒ **本次运行中没连过的老玩家不在
        榜上**。失败形态是"排行榜少人", **不报错**, 极难发现(见 DECISIONS 2026-10-02)。
        索引 `_accounts.json`(`_load_index()`) 里才有全量 `peer -> aid` 映射。
        ⚠ **只收"文件真的存在"的 id**:  否则 `get_or_create` 会拿种子档**凭空造**一个账号出来
        (与"不编造数据"的铁律相冲, 而且会落盘)。
        """
        ids = set(self.by_id.keys())
        try:
            ids |= set(self._load_index().values())
        except Exception:
            pass
        out = []
        for aid in sorted(ids):
            try:
                if aid in self.by_id or os.path.isfile(self._path(aid)):
                    out.append(self.get_or_create(aid))
            except Exception:
                continue        # 坏档不能让整条榜挂掉(与 list 类接口同口径)
        return out

    def all_accounts_strict(self, require_index=True):
        """启动修复专用：索引或任一已存在账号坏档都必须显式失败。"""
        ids = set(self.by_id) | self._known_account_ids()
        if require_index or os.path.isfile(self._index_path()):
            ids |= set(self._load_index().values())
        out = []
        for aid in sorted(ids):
            if aid in self.by_id or os.path.isfile(self._path(aid)):
                out.append(self.get_or_create(aid))
        return out

    def online_accounts(self):
        return [self.get_or_create(a) for a in list(self.online.keys())]

    def broadcast(self, payload):
        """世界级广播。返回值是**账号 id 列表**；真正发帧由调用方（game_server）做。"""
        return list(self.online.keys())

    def world_chat_append(self, row):
        log = self.world.setdefault('chat_log', [])
        log.append(row)
        if len(log) > 200:
            del log[:-200]
        return row

    # ---- 生产接线用的扩展 ----
    def bind_peer(self, peer):
        """把 peer 绑到它的账号上；首次登录即签发。返回 Account。"""
        self.state.bind(self.login(peer))

    def bind_admin(self, aid=None):
        """`/admin` 没有 peer 身份，必须显式指定要操作哪个账号。

        规则（简单、可预期、且**不会静默写进别人的档**）：
          · 传了 `aid` 就用它，**但它必须已存在**（★2026-10-02 D5=B：不再 `get_or_create`，
            见 `exists()` 的 docstring —— 拼错的 id 凭空调号会导致真串档）；
          · 否则用 `u0001`（第一个签发的账号＝本机玩家）；
          · 都不存在就**保持未绑定** —— 于是写操作落进一次性空 dict，`save_state()` 会拒绝，
            表现为"后台报错"而不是"悄悄改了某人的档"。
        """
        if aid:
            if not self.exists(aid):
                self.state.unbind()
                return False
            self.state.bind(self.get_or_create(aid))
            return True
        if self.exists('u0001'):
            self.state.bind(self.get_or_create('u0001'))
            return True
        self.state.unbind()
        return False

    def bind_user(self, userid):
        """按**客户端自报的账号**绑定（任务 F / D3=C：打开客户端账号输入框）。

        链路（逐环有出处）：`LoginHomeSdk.lua:976` 把玩家在输入框里输的字符串写进
        `SQManager:login_getMe().userData.userid` → `SQLoginData:getUserID()`(:111) →
        `NetRoleInfo:sendMessage` 的 `msg["userid"]`(network/gamer/NetRoleInfo.lua:40) →
        游戏 socket 的 **1004** 请求体。

        ⚠ 索引键加 `user:` 前缀，与"来源 IP"那一类键**永不碰撞**（共存于同一个
          `_accounts.json`）。
        ⚠ 调用方必须自己判"这个 userid 是不是玩家真输的" —— 默认路径下客户端上报的是
          **服务端自己**签发的 `'1'`，照单全收会把真玩家换成一个新档（表现为"角色没了"）。
          判据在 `game_server._bind_userid_if_real`。
        """
        self.state.bind(self.login(_USER_KEY_PREFIX + str(userid)))
        return True


def _account_id_number(aid):
    """只接受发号器的规范输出，避免路径穿越及 u00001/u0001 命名空间碰撞。"""
    if not isinstance(aid, str) or len(aid) < 5 or aid[0] != 'u':
        return 0
    if not all('0' <= c <= '9' for c in aid[1:]):
        return 0
    try:
        n = int(aid[1:])
        return n if n > 0 and aid == 'u%04d' % n else 0
    except ValueError:
        return 0


def owner_ns(aid):
    """账号 id -> 命名空间序号（`u0002` -> 2）。推不出来 -> 0（表示"不参与命名空间"）。

    ★ **全工程跨账号展示 id 只许用这一个函数算序号**（评论用了、妖神榜 1065/1066 也用）：
      它由**账号 id** 推出，因此任何一方、任何时刻算出来都一样。
    """
    try:
        s = str(aid or '')
        if s[:1] == 'u' and s[1:].isdigit():
            return int(s[1:])
    except Exception:
        pass
    return 0


def ns_id(aid, raw_id):
    """`raw_id` -> 跨账号展示 id（序号为 0 时原样返回，即"不参与命名空间"）。"""
    n = owner_ns(aid)
    try:
        return n * NS_BASE + int(raw_id) if n else int(raw_id)
    except Exception:
        return raw_id


def split_ns_id(display_id):
    """展示 id -> `(owner_ns, raw_id)`；没命名空间的 -> `(0, display_id)`。"""
    try:
        v = int(display_id)
    except Exception:
        return 0, display_id
    if v >= NS_BASE:
        return v // NS_BASE, v % NS_BASE
    return 0, v


def parse_acct_param(path):
    """从 URL 查询串里取并**校验** `acct`。返回 `(aid_or_None, err_or_None)`。

    · `(None, None)` —— 没带这个参数
    · `(aid,  None)` —— 带了且格式合法
    · `(None, '<原因>')` —— 带了但**非法**（调用方必须拒绝，不能回落）

    ★ 与 `game_server.HTTP.h()` **共用同一个解析器**：否则会出现"校验严、绑定松"的分叉
      —— 一处按严格规则拒、另一处按裸 `split('acct=')` 取，绕过就从那条缝里来。
    ★ 只认 `?` 之后的查询串、只认**第一个** `acct=`、**不做 percent-decode**：
      值必须是字面 `u` + 4 位数字。于是 `../state` / `%61cct=u0002` / `?x=acct=u0002`
      这些形态全部被拒（`../state` 曾可越出 `work/accounts/` 读到别处的 json）。
    """
    q = path.split('?', 1)
    if len(q) < 2:
        return None, None
    raw = None
    for kv in q[1].split('&'):
        k, _, v = kv.partition('=')
        if k == 'acct':
            raw = v
            break
    if not raw:
        return None, None
    if len(raw) == 5 and raw[0] == 'u' and raw[1:].isdigit():
        return raw, None
    return None, 'acct 格式非法: %r（只接受 u + 4 位数字, 如 u0001）' % raw


# ===========================================================================
# ---- 写盘留痕（默认关；见 `trace()`）--------------------------------------------
_TRACE_FLAG = '_acct_trace.json'


def trace_on():
    """留痕开关：环境变量 `YGZZ_ACCT_TRACE=1`，或本模块目录下存在 `_acct_trace.json`。

    ★ 为什么要第二种开法：`_isolated_regression.py` 会**清空子进程里所有 `YGZZ_*` 变量**，
      所以在套件里设环境变量一律无效（本轮实测踩到）。标志文件不受影响。
    ⚠ 文件名后缀必须落在副本白名单里（`.json/.py/.txt/.md`…）—— 用 `.flag` 的话标志
      根本进不了隔离副本（本轮又踩到一次）。
    ⚠ 标志文件要写进**运行目录**（套件自己那份 `runwork`），不是 `work\\`（那只是镜像）。
    """
    if os.environ.get('YGZZ_ACCT_TRACE') == '1':
        return True
    try:
        return os.path.isfile(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           _TRACE_FLAG))
    except Exception:
        return False


def trace(line):
    """串档排查留痕：把 `line` 追加到 `accounts/_acct-trace.log`（并打一行 stdout）。

    ★★ **自包含**是硬要求：绝不能 `import game_server` —— 那会重跑它的模块级代码，
      其中 `sys.stdout = _Tee(sys.stdout, …)` 会把**调用方的 stdout 换掉**
      （实测 `_saveguard_selftest` 立刻在 `sys.stdout.buffer` 上报 AttributeError，
      日志还被偷偷写进生产 `server-out.log`）。
    默认（开关不在）**一个字节都不写** —— 生产路径逐字节不变。
    """
    if not trace_on():
        return False
    try:
        print('[ACCT-TRACE] %s' % line, flush=True)
    except Exception:
        pass
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               '_acct-trace.log'), 'a', encoding='utf-8') as f:
            f.write('%s | %s\n' % (time.strftime('%H:%M:%S'), line))
    except Exception:
        pass
    return True


def _tmp_name(p):
    """唯一临时名；**故意以 `.tmp` 结尾** —— 让 `_b6` 的"没有残留 .tmp"判据真的抓得到残留。"""
    return '%s.%d.%d.tmp' % (p, os.getpid(), threading.get_ident())


def _replace(src, dst, tries=8):
    """`os.replace` + 有界退避：Windows 上目标被读侧占用会抛 PermissionError（一次落盘全丢）。"""
    last = None
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            last = e
            time.sleep(0.02 * (i + 1))
    raise AccountsError('os.replace 失败 %s -> %s: %r' % (src, dst, last))


def _drop(p):
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


def _i(v, d=0):
    """宽松取整（世界档自举时用；与各 handler 里的 `_i` 同口径）。"""
    try:
        return int(v)
    except Exception:
        return d


# ===========================================================================
# 进程级单例 + 生产接线
# ===========================================================================
REGISTRY = None
# `state.load_state()` 在多账号模式下把"模板档"放这里（新账号由它深拷贝派生）。
# 第一个账号因此继承**现有玩家**的档 ⇒ 上多账号不会把 LV28「测试」变成空档。
SEED = {}


def set_seed(data):
    global SEED
    SEED = data if isinstance(data, dict) else {}
    return SEED


def account_root(work_dir):
    return os.path.join(work_dir, _ACCT_DIR_NAME)


def enable(work_dir, seed=None):
    """由 `game_server.py` 的 `__main__` 调用（**只有真服务端入口才允许**）。

    `seed` 缺省用 `state.load_state()` 放下的那份模板（`SEED`）。
    """
    global REGISTRY, ENABLED
    ENABLED = True
    REGISTRY = Registry(account_root(work_dir), seed=SEED if seed is None else seed)
    return REGISTRY


def peer_key(addr):
    """`('127.0.0.1', 59230)` -> `'127.0.0.1'`。

    ⚠ **只取 host**：源端口每次重连都变，带上端口等于每次登录都换新档。
    ⚠ 已知边界（写进 HANDOFF，不是 bug 而是方案的固有代价）：`adb reverse` 下**所有**真机
      在服务端看来都是 `127.0.0.1` ⇒ 它们会共用同一个档。真多人需要换身份来源（见 DECISIONS）。
    """
    if isinstance(addr, (tuple, list)) and addr:
        return str(addr[0])
    return str(addr)


# ---------------------------------------------------------------------------
# 推送接线：由 `game_server.py` 在启动时注册，避免 `handlers_*` 反向 import game_server
# （那会构成循环 import —— game_server 是要 import handlers_sign 的）。
# ---------------------------------------------------------------------------
_PUSH_ACCOUNT = None


def register_push(fn):
    global _PUSH_ACCOUNT
    _PUSH_ACCOUNT = fn
    return fn


def push_account(aid, msgid, payload):
    if _PUSH_ACCOUNT is None or not aid:
        return 0
    return _PUSH_ACCOUNT(aid, msgid, payload)


def push_current(msgid, payload):
    """只推给**当前线程绑定的那个账号** —— 账号私有推送的正确形态。"""
    acc = EQ_STATE.current()
    if acc is None:
        return 0
    return push_account(acc.id, msgid, payload)
