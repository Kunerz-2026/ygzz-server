# -*- coding: utf-8 -*-
r"""battle_session.py —— 战斗结算**一次性**的共享状态机（任务书 D 包的前置接口，A 先交付）。

## 它解决的是一类事故，不是"一个函数"

修前 `9003 / 9004 / 7009 / 1088 / 8006` **只信请求体**：把同一个包连发两次，服务端就
再发一次奖 —— 金币、妖塔层数、排位积分、任务进度、掉落全部翻倍。五处各写一遍
`if settled: return` 是不行的（**五份实现必然漂移**，本项目已吃过 roleInfo 九份副本的亏）。
⇒ 抽成一份状态机：谁要"这一局只认一次"，就来这里 `open/settle/close`。

## 三个词，别混

| 词 | 含义 | 谁调 |
|---|---|---|
| `open_session(mode, target)` | **本局开始**（= 客户端那条"开战"消息到了） | 1701（explore）/ 7010·7008（pvp）/ 1091（rank）/ 8005（cardboss）/ 1034（tower） |
| `settle(mode, target, token)` | **本局结算**，一次性 | 9003 / 9004 / 7009 / 1088 / 8006 |
| `close_session(mode, target)` | **本局作废/退出**（不再可结算） | 1706（探索退出）、开战失败、掉线清理 |

`require_open=True`（默认）表示"没有 open 就不许结算"——`7009/1088/8006` 用这个：

* `7009` 以 `7010/7008` 为前置（任务书 D③）
* `1088` 必须由 `1091` 开战
* `8006` 必须由 `8005` 开战

`9003/9004` 走 `require_open=False`（它们的开战点对客户端没有独立消息，硬加前置会把正常
游玩挡掉），但**仍然登记 token**：同一个结算包重放第二次会被 `replay` 拦下。

## token（一次性票号）

`settle` 的第三个参数。同一场战斗的两次结算必须给出**同一个 token** 才会被判重放，
所以它应当是"这一场战斗的唯一标识"：9003/8006 用 `(value 密文, time)`，
7009 用 `(battleid, 时间戳)`，1088 用 `(stage, 回合)`。`token=None` 表示
"不判重放，只靠会话是否已被消耗"——只在 `consume=True` 时有意义。

## 账号隔离

会话按**账号**存（`accounts.EQ_STATE.current().id`；单档时是 `'<single>'`）。
甲开的局乙看不见、也结算不了 —— 这是本项目已经吃过一次亏的地方（`_ScopedState` 注释里的
"我的东西被别人动了"）。

## 纪律

* 所有公开函数**绝不抛异常**（`DECISIONS` 2026-10-12：旁路异常被静默吞会伪装成业务缺陷；
  反过来说，结算判定失败也**不能**把连接打挂）——失败一律返回 `(False, 原因)`。
* 只在内存里（不落盘）：这是"这一局还没结算"的临时状态，重启后应当重新开战，
  而不是把上一次的会话带进下一次运行。
"""
import os
import threading
import time

# 模式常量（与协议号的对应关系写在这里，免得调用点各猜一套）
MODE_EXPLORE = 'explore'      # 9003 探索/秘境/世界图（开：1701 进大关卡；关：1706）
MODE_TOWER = 'tower'          # 9004 妖塔进层（开：1034 取塔数据）
MODE_PVP = 'pvp'              # 7009 妖怪对战（开：7010 战斗列表 / 7008 布阵）
MODE_RANK = 'rank'            # 1088 排位赛结算（开：1091 开战）
MODE_CARDBOSS = 'cardboss'    # 8006 卡池 BOSS 结算（开：8005 开战）
MODE_CHESS = 'chess'          # 1153 妖盟弈局结算（开：chess_start；一局只发一次）
MODE_WAR = 'war'              # 9001/9005/9006/9007：没有"开战"消息的战场，target = 协议号
MODE_INVADE = 'invade'        # 9008 妖主大入侵（开：1702 type=7，target = A05 行 id；一局只发一次）

MODES = (MODE_EXPLORE, MODE_TOWER, MODE_PVP, MODE_RANK, MODE_CARDBOSS, MODE_CHESS,
         MODE_WAR, MODE_INVADE)

# 会话默认存活时间（秒）。口径来自本项目既有约定（1090/1088 的 NETING_TTL 也是 30 分钟）：
# 一局战斗从开战到结算不可能超过半小时；超时视为"这局没了"，必须重新开战。
DEFAULT_TTL = 1800.0

# 重放票号的保留上限（每组 mode+target 一份）。超过就丢最早的那批 —— 票号只需要覆盖
# "同一场战斗重复发几次"这个量级，不需要无限长记忆。
_TOKEN_KEEP = 8

_SINGLE = '<single>'
_UNBOUND = '<unbound>'

_LOCK = threading.RLock()
_SESSIONS = {}     # aid -> {mode: {'target': int, 't0': float, 'exp': float}}
_TOKENS = {}       # aid -> {(mode, target): [(token, exp), ...]}


def _ttl_default():
    try:
        v = float(os.environ.get('YGZZ_BATTLE_TTL') or DEFAULT_TTL)
        return v if v > 0 else DEFAULT_TTL
    except Exception:
        return DEFAULT_TTL


def _now(now=None):
    return float(now) if now is not None else time.time()


def current_account(account=None):
    """当前账号键。`account` 显式给出时直接用（管理/工具路径）。

    ⚠ 单档模式与多账号模式返回**不同**的键空间，两者永不交叉：
    这不只是命名差别 —— 单档下所有人共用一个档，把单档键混进多账号键会让
    切换模式后读到上一个模式的残留会话。
    """
    if account is not None:
        return str(account)
    try:
        import accounts
        if accounts.ENABLED:
            cur = accounts.EQ_STATE.current()
            return cur.id if getattr(cur, 'id', None) else _UNBOUND
    except Exception:
        pass
    return _SINGLE


def _norm_mode(mode):
    if not isinstance(mode, str):
        return None
    m = mode.strip().lower()
    return m if m in MODES else None


def _norm_target(target):
    """目标 id 一律正整数。字符串数字也收（客户端 JSON 里 id 常见是字符串）。"""
    if isinstance(target, bool) or target is None:
        return None
    try:
        v = int(target)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _norm_token(token):
    if token is None:
        return None
    if isinstance(token, (str, bytes)):
        return token.decode('utf-8', 'replace') if isinstance(token, bytes) else token
    if isinstance(token, bool):
        return 'true' if token else 'false'
    if isinstance(token, (int, float)):
        return str(token)
    try:
        return repr(token)
    except Exception:
        return None


def _acct_book(aid):
    b = _SESSIONS.get(aid)
    if not isinstance(b, dict):
        b = _SESSIONS[aid] = {}
    return b


def _live(s, now):
    """这条会话现在还合法吗（`exp` 之后一律不合法）。"""
    return isinstance(s, dict) and now < s.get('exp', 0)


# ⚠ 会话**不按时间删**，只在被消费/被覆盖/被 close 时删。
#   理由：一个账号最多 5 个模式 ≤ 5 条记录，删不删都不影响内存；而"过期"与"没开过"
#   是两个不同的运维信号（一个是"这局拖太久/时钟跳了"，一个是"客户端根本没开战"），
#   若在读取路径上顺手删掉，"过期"就永远退化成"没开" —— 排查时会看错方向。
#   实测踩点：`_battle_session_test` 的 S3 正是这么红的（is_open 一次就把记录剪掉了）。
def _prune_tokens(aid, now):
    """票号才有必要按时间剪（它每场战斗一条，量级会累积）。"""
    tk = _TOKENS.get(aid)
    if not isinstance(tk, dict):
        return
    for k in list(tk.keys()):
        kept = [(t, e) for (t, e) in tk[k] if now < e]
        if kept:
            tk[k] = kept
        else:
            tk.pop(k, None)
    if not tk:
        _TOKENS.pop(aid, None)


def _token_book(aid):
    tk = _TOKENS.get(aid)
    if not isinstance(tk, dict):
        tk = _TOKENS[aid] = {}
    return tk


def open_session(mode, target, account=None, ttl=None, now=None):
    """开一局：`(True,'ok')` 或 `(False, 原因)`。重复 open **覆盖**上一局（重新开战）。"""
    try:
        m = _norm_mode(mode)
        if m is None:
            return False, 'bad_mode'
        t = _norm_target(target)
        if t is None:
            return False, 'bad_target'
        aid = current_account(account)
        n = _now(now)
        span = _ttl_default() if ttl is None else max(0.0, float(ttl))
        with _LOCK:
            _prune_tokens(aid, n)
            _acct_book(aid)[m] = {'target': t, 't0': n, 'exp': n + span}
        return True, 'ok'
    except Exception:
        return False, 'error'


def close_session(mode, target=None, account=None, now=None):
    """关掉某局（`target` 给出时必须与当前局一致才关；`None` 表示无条件关）。

    返回 `True` 表示**确实关掉了一局在开的会话**（用于"退出后不许再结算"的判据）。
    """
    try:
        m = _norm_mode(mode)
        if m is None:
            return False
        aid = current_account(account)
        n = _now(now)
        with _LOCK:
            book = _SESSIONS.get(aid) or {}
            s = book.get(m)
            if not isinstance(s, dict):
                return False
            t = None if target is None else _norm_target(target)
            if t is not None and t != s.get('target'):
                return False
            book.pop(m, None)
            return True
    except Exception:
        return False


def is_open(mode, target=None, account=None, now=None):
    """这一局还开着吗？`target` 给出时要求一致。"""
    try:
        m = _norm_mode(mode)
        if m is None:
            return False
        aid = current_account(account)
        n = _now(now)
        with _LOCK:
            s = (_SESSIONS.get(aid) or {}).get(m)
            if not _live(s, n):
                return False
            if target is None:
                return True
            t = _norm_target(target)
            return t is not None and t == s.get('target')
    except Exception:
        return False


def session_target(mode, account=None, now=None):
    """当前局的目标 id（没有则 `None`）—— 结算时用来核对"结算的是不是开着的那一局"。"""
    try:
        m = _norm_mode(mode)
        if m is None:
            return None
        aid = current_account(account)
        with _LOCK:
            s = (_SESSIONS.get(aid) or {}).get(m)
            return s.get('target') if _live(s, _now(now)) else None
    except Exception:
        return None


def settle(mode, target, token=None, account=None, require_open=True, consume=True,
           now=None):
    """结算一次。返回 `(ok, reason)`。

    reason ∈ `ok / bad_mode / bad_target / no_open / wrong_target / expired / replay / error`

    * `require_open=True`：没有在开的局一律 `no_open`（**7009/1088/8006 必须用这个**）。
    * `consume=True`：结算成功即关闭会话（一局只认一次）。
    * `token`：同一场战斗的重放判据；`None` 表示不判（只用会话消耗兜底）。
    """
    try:
        m = _norm_mode(mode)
        if m is None:
            return False, 'bad_mode'
        t = _norm_target(target)
        if t is None:
            return False, 'bad_target'
        aid = current_account(account)
        n = _now(now)
        tok = _norm_token(token)
        with _LOCK:
            _prune_tokens(aid, n)
            book = _SESSIONS.get(aid) or {}
            s = book.get(m)
            if not _live(s, n):
                # 会话不存在 / 已过期：区分原因（过期与"没开"是不同的运维信号）
                if isinstance(s, dict):
                    if consume:
                        book.pop(m, None)
                    return False, 'expired'
                if require_open:
                    return False, 'no_open'
                # `require_open=False`：9003/9004 这类"客户端没有独立开战消息"的号，
                # 没有会话也允许结算；此时只剩**票号**这一道一次性判据。
                exp = n + _ttl_default()
            else:
                if s.get('target') != t:
                    return False, 'wrong_target'
                exp = s.get('exp', n)
            if tok is not None:
                key = (m, t)
                tk = _token_book(aid)
                used = tk.get(key) or []
                if any(x == tok for (x, _e) in used):
                    return False, 'replay'
                used.append((tok, exp))
                tk[key] = used[-_TOKEN_KEEP:]
            if consume:
                book.pop(m, None)
                if not book:
                    _SESSIONS.pop(aid, None)
        return True, 'ok'
    except Exception:
        return False, 'error'


def reset(account=None):
    """清空会话/票号。`account=None` 清全部，否则只清该账号。返回 `True`。"""
    try:
        with _LOCK:
            if account is None:
                _SESSIONS.clear()
                _TOKENS.clear()
            else:
                aid = current_account(account)
                _SESSIONS.pop(aid, None)
                _TOKENS.pop(aid, None)
        return True
    except Exception:
        return False


def debug_snapshot(account=None):
    """`{账号: {mode: target}}` —— 只读排查用（不含票号，避免把密文带进日志）。"""
    out = {}
    try:
        with _LOCK:
            if account is not None:
                aids = [current_account(account)]
            else:
                aids = sorted(set(_SESSIONS.keys()) | set(_TOKENS.keys()))
            for aid in aids:
                book = _SESSIONS.get(aid) or {}
                out[aid] = {m: (s.get('target') if isinstance(s, dict) else None)
                            for m, s in book.items()}
    except Exception:
        return {}
    return out
