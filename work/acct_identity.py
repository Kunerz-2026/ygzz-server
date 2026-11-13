# -*- coding: utf-8 -*-
r"""acct_identity.py —— 「客户端自报账号」的身份解析（任务 F / D3=C, 2026-10-02）。

## 为什么单独一个模块
逻辑本身很短，但它**必须在 `game_server` 之外可导入**：
`import game_server` 会在**模块级**执行 `sys.stdout = _Tee(sys.stdout, _open_std_log(...))`
（`game_server.py:68`）—— 也就是说，谁在测试里 import 它，谁的 stdout 就被**重定向进
`work\logs\server-out.log`**，测试输出会静默写进**生产日志**（本轮真踩到：一个新套件
因此把 16 KB 测试输出写进了生产日志）。现有套件之所以没出事，是因为它们在**私有布局**
（`%TEMP%` 里的副本）里跑，`_BASE_DIR` 指到临时目录。
⇒ 把这两段抽出来，测试就不必 import 服务器。

## 链路（每一环都有出处）
`LoginHomeSdk.lua:976`（玩家在账号输入框里输的串 -> `SQManager:login_getMe().userData.userid`）
→ `SQLoginData:getUserID()`(:111) → `NetRoleInfo.lua:40` 的 `msg["userid"]`
→ 游戏 socket 的 **1004** 请求体 → `bind_userid_if_real` → `accounts.Registry.bind_user`。

**为什么需要它**：身份来源原本只有来源 IP，同一出口下无法区分玩家；userid 是唯一随游戏连接到达的标识。
"""

# 服务端在 `/ucenter/serverlist` 里签发的默认 userid（`game_server._userid_from_body` 的兜底）。
# ⚠ 这个"默认值"是**整套判据的核心**：默认路径（`JUMP_SDK=false`）下客户端上报的就是它，
#   照单全收会把真玩家改绑到一个新档（表现为"角色没了"）。
DEFAULT_USERID = '1'
_NULLISH = ('0', 'null', 'none', 'nil')


class AccountBindingError(RuntimeError):
    """真实 userid 绑定失败：调用方必须终止连接，不能沿用旧账号。"""


def userid_from_body(body):
    """从 `/ucenter/*` 的 **urlencoded 请求体**里取 `userid`；取不到回 `DEFAULT_USERID`。

    取不到就回默认值 ⇒ **默认路径行为逐字节不变**（关着账号框的人不受影响）。
    ⚠ 只认**恰好** `userid=` 这个键：`xuserid=bob` 不算（前缀陷阱）。
    """
    try:
        for kv in (body or b'').decode('utf-8', 'replace').split('&'):
            k, _, v = kv.partition('=')
            if k == 'userid' and v:
                from urllib.parse import unquote_plus
                return unquote_plus(v) or DEFAULT_USERID
    except Exception:
        pass
    return DEFAULT_USERID


def is_real_userid(uid):
    """这个 `userid` 是不是"玩家自己输的"（而不是服务端签发的默认值）。"""
    if uid is None:
        return False
    try:
        s = str(uid).strip()
    except Exception:
        return False
    if not s or s == DEFAULT_USERID or s.lower() in _NULLISH:
        return False
    return True


def bind_userid_if_real(body_obj, accounts_mod=None, log=None):
    """按客户端自报的 `userid` 改绑账号。返回 True = 这次真的换了绑定。

    · `body_obj` 可以是 dict / JSON 字符串 / bytes（两条分发路径给的东西不同：
      framing-A 给 `extract_body_json(raw)` 的字符串，LE 路径给已解析的 `obj`）。
    · `accounts_mod` 缺省 `import accounts`（注入是为了测试不必碰真注册表）。
    · **已经绑对了就不动**；真实 userid 绑定失败时清空旧上下文并抛 AccountBindingError。
    """
    import json as _json
    if accounts_mod is None:
        import accounts as accounts_mod            # noqa: F811
    if not (getattr(accounts_mod, 'ENABLED', False) and accounts_mod.REGISTRY is not None):
        return False
    d = body_obj
    if isinstance(d, (str, bytes, bytearray)):
        try:
            d = _json.loads(d)
        except Exception:
            return False
    if not isinstance(d, dict):
        return False
    uid = d.get('userid')
    if not is_real_userid(uid):
        return False
    uid = str(uid).strip()
    try:
        cur = getattr(accounts_mod.EQ_STATE.current(), 'id', None)
        if cur is not None and accounts_mod.REGISTRY.find('user:' + uid) == cur:
            return False                    # 已经绑对了
        accounts_mod.REGISTRY.bind_user(uid)
    except Exception as e:
        accounts_mod.EQ_STATE.unbind()
        raise AccountBindingError('userid=%r 账号改绑失败，必须结束连接' % uid) from e
    if log:
        log('ACCT 按客户端自报 userid 改绑: userid=%r -> %s'
            % (uid, getattr(accounts_mod.EQ_STATE.current(), 'id', '?')))
    return True
