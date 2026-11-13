# -*- coding: utf-8 -*-
r"""ygzz_auth.py —— 单服登录注册的**核心**：账号口令、会话票据、身份判定。

## 为什么单独一个模块（与 `acct_identity.py` 同一个理由）
`import game_server` 会在模块级把 `sys.stdout` 换成 `_Tee`（`game_server.py:81`），
测试里一 import 就把测试输出写进**生产日志**。所以登录鉴权这部分必须能在
`game_server` 之外被单独导入、单独测试。

## 它解决的问题（改造前的真实链路）
改造前身份有**两条互不相干的来源**，都不构成鉴权：
1. `accounts.Registry.login(peer_key)` —— 身份 = **来源 IP**（用户 2026-10-02 拍板）。
   本机 模拟器 下所有客户端在服务端看来都是 `192.0.2.10` ⇒ 真多人分不开档。
2. `acct_identity.bind_userid_if_real()` —— 身份 = 客户端在 1004 里**自报的 userid**。
   `userid` 由服务端在 `/ucenter/serverlist` 里回显（`_userid_from_body`），客户端下一个
   包里原样送回来 ⇒ **任何人填别人的 userid 就能进别人的档**，且**没有任何口令校验**。

## 改造后的链路（逐环有客户端出处）
```
[HTTP] POST /ucenter/hoolailogin?    body: type=RG_LO/AP_LO, account, password
       └─ 注册（type=RG_LO）或 登录（其它）  ← `LoginHomeSdk.lua:2125 apRegister()`
                                              `MLoginHome.lua:218 connectServer()`
       响应（**base64**，客户端 `CJson2Table(decodeBase64(...))`）：
           {code:1, gameData:…, serverList:[…],
            userData:{userid:…, username:…, openid:…, macid:…}}   ← SQLoginData:setData
[HTTP] POST /ucenter/serverlist?     body: userid=…&ticket=…      ← `MLoginHome.lua:479`
       响应同上（`SQLoginData:setData` 再取一次 userData）；**必须携票**，只带 userid 一律 1013
[游戏 socket :27001/:27002]  1007 SDK_LOGIN  {userid, serverid, time_zone}
                             1004 ROLE_INFO  {userid, ticket, seqid, language, severid}
                             1002 ROLE_CREATE {rolename, ticket}
       └─ 服务端用 **ticket** 查会话（`lookup_ticket`），再核对 userid 与会话一致，
          才 bind 账号；否则拒绝绑定并结束连接。
```
`userid` **由服务端签发**（不再回显客户端送来的值），客户端把它与 `userData.macid`
（同一次登录的**票据**）一起带回来。★2026-10-26（审查 B1）：**之前只有 userid、
没有票据**——那等于"谁拿到一个仍有效的 userid 就能进谁的档"。现在 1004/1002/刷新
三处都要求票据；客户端改动见 the client role-info handler
（`msg["ticket"] = SQManager:login_getMe().userData.macid`）等三处。

## 两条"错了会静默出事"的设计点
* **未认证 ≠ 新账号**：找不到有效票据时**拒绝绑定**（连接结束）。绝不"发一个新号"——
  发新号会让玩家看到"角色没了"，而且他下一次真登录时会写回一份空档。
* **票据一次性 + 顶号**：同账号第二次 HTTP 登录会让**上一次**签发的票据立刻失效。
  否则"两台设备同时在线写同一个档"就是另一种串档（读改写交错）。
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

# ---- 账号名与口令的口径 ------------------------------------------------------------
# 客户端账号输入框：`MeEditBox` + `setMaxLength(ebML)`，`ebML=32`（LoginHomeSdk.lua:1932/2028）。
# 客户端注册框同样 32（:2028）⇒ 服务端上限取 32，与客户端一致，避免"客户端存得下、服务端不收"。
ACCOUNT_MAX = 32
EMAIL_MAX = 254
# ⚠ 客户端注册框仍是 32 字符（LoginHomeSdk.lua:2028）；手机包重打时要同步抬到 254，
#    否则“服务端收邮箱、客户端存不下”。服务端先放开，不影响现有用户名登录。
ACCOUNT_MIN = 3
PASSWORD_MIN = 6
PASSWORD_MAX = 64

# 用户名只允许字母/数字/下划线/点/减号/汉字。⚠ 不允许 `:` —— `accounts.py` 的索引键是
# `user:<userid>`，让账号名里带 `:` 就能构造出与另一类键同形的串（`user:a:b`）。
_ACCOUNT_RE = re.compile(r'^[A-Za-z0-9_.\-\u4e00-\u9fff]+$')
# 邮箱账号：用户 2026-10-27 决定测试服开放邮箱注册。只做形状校验，不做 DNS/MX；
# 本期不发验证码、不做找回（没有 SMTP），邮箱等价于“带 @ 的账号名”。
_EMAIL_RE = re.compile(r'^[A-Za-z0-9_.+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$')

# PBKDF2 迭代次数：本机（Python 3.14 / 单核）实测 ~30 ms/次，足以让"在线爆破"变得不划算，
# 又不会让登录变卡（登录是低频动作，且不在每帧路径上）。
_PBKDF2_ITERS = 120000
_PBKDF2_DKLEN = 32

# 票据寿命：客户端从"HTTP 登录"到"游戏 socket 1004"之间通常 < 5 秒；
# 断线重连（ReconnectManager）会**重新走一次 HTTP 登录**，所以不需要长寿命。
# 取 15 分钟是为了覆盖"登录后停在选服界面很久"的真实玩法，同时短到"票据被猜中"没有价值。
TICKET_TTL = 900.0

# 登录失败节流：同一账号连续失败 N 次后锁定 M 秒。没有它，PBKDF2 只挡住了"离线爆破"，
# 挡不住"在线慢慢试"（而且每次失败都要跑一次 PBKDF2 = 30 ms，等于给我自己一个放大攻击面）。
LOCK_AFTER_FAILS = 8
LOCK_SECONDS = 300.0

# 自助重置码：独立节流（不把登录一起锁死）。6 位数字，注册时生成一次。
RESET_LOCK_AFTER = 5
RESET_LOCK_SECONDS = 600.0


def new_reset_code():
    """6 位数字重置码（明文只在注册/重置响应里回一次）。"""
    return '%06d' % secrets.randbelow(1000000)


class AuthError(RuntimeError):
    """鉴权失败。`code` 是回给客户端的 **statecode**，`msg` 是给人的说明。"""

    def __init__(self, code, msg, http_code=None):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        # 给 HTTP 端用的错误码（客户端 `UCENTER_ERRPR_CODE` / `netdata.descode`）。
        # 缺省与游戏 socket 的 statecode 一致，调用方可覆盖。
        self.http_code = code if http_code is None else http_code


# ---- HTTP 错误码（客户端 `LoginHomeSdk` / `MLoginHome` 的 `code ~= 1` 分支）----------
# 这些是**客户端自己的**错误码表里的值，不是我编的；挑法见每条后面的出处。
HTTP_OK = 1
HTTP_BAD_PARAM = 1011          # "缺少必要参数"（NetSdkLogin 表里有 1011）
HTTP_NO_ACCOUNT = 1012         # "未发现账号信息"
HTTP_BAD_PASSWORD = 1013       # "token错误" —— 客户端用来表示"凭据不对"
HTTP_DUPLICATE = 1005          # 注册重名：客户端注册失败表 [4]="此账号已被占用"走的是游戏 socket；
                               # HTTP 侧没有专用码，用 1005 并在 `descode` 里写清中文
HTTP_LOCKED = 10               # "账号被锁定，请联系客服人员"
HTTP_SERVER_CLOSED = 11        # "服务器没开启或者服务器已经关闭"
# ★2026-10-26（审查 H2）：存量档**不再允许自助认领**。码用 1014（客户端表里没有专用项，
#   但 HTTP 失败一律显示 `descode`，够玩家看懂"找管理员迁移"）。真正的认领入口是
#   `_auth_admin.py claim <账号>`（运维执行、留痕），不是"谁先来谁设密码"。
HTTP_CLAIM_FROZEN = 1014

# ---- 游戏 socket 1007/1004 的 statecode（`NetSdkLogin.m_errorCode`）-----------------
SOCKET_OK = 1
SOCKET_NO_ACCOUNT = 13         # 未找到此账号
SOCKET_BAD_PASSWORD = 12       # 账号或密码输入错误
SOCKET_LOCKED = 10             # 账号被锁定
SOCKET_FAIL = 7                # 登录失败
SOCKET_NO_TICKET = 1011        # 缺少必要参数（= 没带有效会话票据）
SOCKET_BAD_TICKET = 1013       # token 错误（= 票据过期/被顶掉）


class Session(object):
    """一次**已认证的 HTTP 登录**。游戏 socket 只有找到它才允许绑定账号。"""

    __slots__ = ('ticket', 'userid', 'account', 'aid', 'issued_at', 'expires_at',
                 'revoked', 'used_at', 'peer', 'serial')

    def __init__(self, ticket, userid, account, aid, ttl, peer=None, serial=0):
        self.ticket = ticket
        self.userid = userid
        self.account = account
        self.aid = aid
        self.issued_at = time.time()
        self.expires_at = self.issued_at + ttl
        self.revoked = False
        self.used_at = None
        self.peer = peer
        # 同账号连续登录的序号（第 N 次登录）：用来判定"哪一条连接是新的"。
        self.serial = serial

    def alive(self, now=None):
        now = time.time() if now is None else now
        return (not self.revoked) and now < self.expires_at

    def why_dead(self, now=None):
        now = time.time() if now is None else now
        if self.revoked:
            return 'revoked'
        if now >= self.expires_at:
            return 'expired'
        return ''

    def to_public(self):
        return {'ticket': self.ticket, 'userid': self.userid, 'account': self.account,
                'aid': self.aid, 'expires_at': self.expires_at, 'serial': self.serial}


def now():
    return time.time()


# ---- 口令 ---------------------------------------------------------------------
def hash_password(password, salt=None, iters=_PBKDF2_ITERS):
    """返回 `(salt_hex, hash_hex, iters)`。salt 不传就随机生成。"""
    if salt is None:
        salt = secrets.token_hex(16)
    if isinstance(salt, str):
        salt_b = bytes.fromhex(salt)
    else:
        salt_b = salt
    dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt_b, iters, _PBKDF2_DKLEN)
    return salt_b.hex(), dk.hex(), iters


def verify_password(password, salt_hex, hash_hex, iters=_PBKDF2_ITERS):
    """常数时间比对。任何异常一律返回 False（"校验不了"绝不等于"通过"）。"""
    try:
        dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                 bytes.fromhex(salt_hex), int(iters), _PBKDF2_DKLEN)
    except Exception:
        return False
    return hmac.compare_digest(dk.hex(), str(hash_hex))


# ---- 账号名/口令校验 -------------------------------------------------------------
def check_account_name(name):
    """返回 `(ok, reason)`。reason 是**给人看的中文**，会原样进 `descode`。"""
    if not isinstance(name, str):
        return False, '账号必须是字符串'
    s = name.strip()
    if '@' in s:
        if len(s) > EMAIL_MAX:
            return False, '输入用户名太长'
        if not _EMAIL_RE.match(s):
            return False, '账号含有不合法字符'
        return True, ''
    if len(s) < ACCOUNT_MIN:
        return False, '账号至少 %d 个字符' % ACCOUNT_MIN
    if len(s) > ACCOUNT_MAX:
        return False, '输入用户名太长'          # 客户端 NetRegister [2] 的原话
    if not _ACCOUNT_RE.match(s):
        return False, '账号含有不合法字符'      # 客户端 NetRegister [3] 的原话
    return True, ''


def check_password(pwd):
    if not isinstance(pwd, str):
        return False, '密码必须是字符串'
    if len(pwd) < PASSWORD_MIN:
        return False, '密码至少 %d 个字符' % PASSWORD_MIN
    if len(pwd) > PASSWORD_MAX:
        return False, '密码太长（上限 %d）' % PASSWORD_MAX
    if pwd.strip() == '':
        return False, '密码不能全是空白'
    return True, ''


def new_userid():
    """服务端签发的 `userid`。

    形如 `154079178310301434`（`config.lua:81` 的 `USER_ID` 就是这个形状：18 位数字），
    保证客户端把它当**不透明字符串**存放/回传时不会变形（`NetRoleInfo` 直接
    `msg["userid"] = SQManager:login_getMe():getUserID() or 0` —— 给数字会被 Lua 转成
    number，19 位以内仍在 double 精确整数范围内，但我们一律给字符串，避免任何转换）。
    ⚠ 必须**全局唯一且不可预测**：它是票据的检索键，可预测就等于票据可枚举。
    """
    return '%018d' % secrets.randbelow(10 ** 18)


# ---- 账号库（口令 + 注册/登录/锁定）----------------------------------------------
class AuthStore(object):
    """口令库。**独立文件**（`_auth.json`），与 `_accounts.json` 索引分开：

    `_accounts.json` 是"peer/自报 userid -> 账号 id"的**映射**（可以重建：`_known_account_ids`
    会扫账号目录）；口令**不能重建**，所以放独立文件、独立原子写、独立备份。
    分成两个文件还有一个好处：索引重建（`_load_index` 的修复路径）永远不会碰到口令。

    结构::

        {"version": 1,
         "accounts": {
            "alice": {"aid": "u0001", "salt": "...", "hash": "...", "iters": 120000,
                      "legacy": false, "created": 1234.5, "last_login": 1234.5,
                      "fails": 0, "locked_until": 0.0}
         },
         "users": {"<userid>": "alice"}}      # 服务端签发的 userid -> 账号
    """

    VERSION = 1

    def __init__(self, root, log=None):
        self.root = root
        self._path = os.path.join(root, '_auth.json')
        self._lock = threading.RLock()
        self._log = log
        self._data = None

    # ---- 落盘（原子替换 + 唯一临时名；理由同 accounts._save_json）----
    def _load(self):
        with self._lock:
            if self._data is not None:
                return self._data
            data = None
            if os.path.isfile(self._path):
                try:
                    with open(self._path, encoding='utf-8') as f:
                        data = json.load(f)
                except Exception as e:
                    # ⚠ 与 accounts._load 同一条铁律：**不静默降级**。
                    #   降级成空库 = 所有玩家的口令凭空消失 = 谁都能重新注册同名账号拿走存档。
                    q = self._path + '.corrupt-%s' % time.strftime('%Y%m%d-%H%M%S')
                    try:
                        import shutil
                        shutil.copy2(self._path, q)
                    except Exception:
                        q = '(另存失败)'
                    raise AuthError(HTTP_SERVER_CLOSED,
                                    '口令库 %s 解析失败: %r\n  已另存为 %s —— 请人工处置后再启动。'
                                    % (self._path, e, q))
            if not isinstance(data, dict):
                data = {}
            if not isinstance(data.get('accounts'), dict):
                data['accounts'] = {}
            if not isinstance(data.get('users'), dict):
                data['users'] = {}
            data.setdefault('version', self.VERSION)
            self._data = data
            return data

    def _save(self):
        """调用方必须已持 `self._lock`。"""
        d = self._data
        tmp = '%s.%d.%d.tmp' % (self._path, os.getpid(), threading.get_ident())
        try:
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False, indent=2, sort_keys=True)
            for _ in range(8):
                try:
                    os.replace(tmp, self._path)
                    return
                except PermissionError:
                    time.sleep(0.02)
            os.replace(tmp, self._path)
        finally:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

    def _logln(self, s):
        if self._log:
            try:
                self._log(s)
            except Exception:
                pass

    # ---- 查询 ----
    def accounts(self):
        return self._load()['accounts']

    def get(self, account):
        return self._load()['accounts'].get(_norm(account))

    def exists(self, account):
        return _norm(account) in self._load()['accounts']

    def account_of_userid(self, userid):
        return self._load()['users'].get(str(userid))

    def registered_count(self):
        return len(self._load()['accounts'])

    # ---- 注册 ----
    def register(self, account, password, aid, reset_code=None):
        """注册。已存在即 `HTTP_DUPLICATE`（**不**覆盖口令、**不**改 aid）。

        ★2026-11-07：可选 `reset_code`（6 位数字，客户端注册时由服务端生成）。
          只存 PBKDF2 哈希；明文只在注册成功响应里回一次。用于“忘记密码”自助重置。
        """
        key = _norm(account)
        ok, why = check_account_name(key)
        if not ok:
            raise AuthError(HTTP_BAD_PARAM, why)
        with self._lock:
            d = self._load()
            # ★★顺序**不可换**：先判重名、再校验口令长度。
            #   反过来的话，"账号已存在 + 口令太短"会回"密码至少 6 个字符"（HTTP_BAD_PARAM），
            #   于是**同一账号在两种口令下的应答不同** —— 这就是一个"这个账号存不存在"的
            #   预言机，而它本可以只用一条"已被占用"回答完。
            if key in d['accounts']:
                # ★重复注册必须**明确失败**，且不能有任何副作用（旧口令/旧档都得留原样）。
                self._logln('AUTH 重复注册被拒: account=%r aid=%r' % (key, aid))
                raise AuthError(HTTP_DUPLICATE, '此账号已被占用')
            ok, why = check_password(password)
            if not ok:
                raise AuthError(HTTP_BAD_PARAM, why)
            salt, h, iters = hash_password(password)
            rec = {
                'aid': aid, 'salt': salt, 'hash': h, 'iters': iters,
                'legacy': False, 'created': now(), 'last_login': 0.0,
                'fails': 0, 'locked_until': 0.0,
            }
            if reset_code:
                rsalt, rh, riters = hash_password(str(reset_code))
                rec.update({'reset_salt': rsalt, 'reset_hash': rh, 'reset_iters': riters,
                            'reset_fails': 0, 'reset_locked_until': 0.0})
            d['accounts'][key] = rec
            self._save()
        self._logln('AUTH 注册成功: account=%r aid=%r reset=%s'
                    % (key, aid, 'yes' if reset_code else 'no'))
        return d['accounts'][key]

    # ---- 登录 ----
    def login(self, account, password):
        """校验口令并返回账号记录（dict）。失败抛 `AuthError`。

        ⚠ 失败路径**必须**同时做三件事：计数 + 落盘 + 抛错。
        "只抛错不计数"等于没限速（在线爆破 30 ms 一次，一晚上能试 10 万次）。
        """
        key = _norm(account)
        with self._lock:
            d = self._load()
            rec = d['accounts'].get(key)
            if rec is None:
                # 与"密码错"区分（客户端有 1012 未发现账号信息 vs 1013 token 错误）。
                # ⚠ 这不构成"账号枚举"以外的信息泄漏 —— 注册接口本来就必须回"已被占用"，
                #   所以账号存在性在注册接口上本来就是公开的。
                raise AuthError(HTTP_NO_ACCOUNT, '未找到此账号')
            t = now()
            if float(rec.get('locked_until') or 0) > t:
                raise AuthError(HTTP_LOCKED, '账号被锁定，请 %d 秒后再试'
                                % int(float(rec['locked_until']) - t + 1))
            if rec.get('legacy') and not rec.get('hash'):
                # 存量档"未认领"：**不允许**用任意口令登录（那就是无口令登录）。
                # 认领走 `claim()`，入口在 HTTP 登录里显式判定。
                raise AuthError(HTTP_BAD_PASSWORD, '该账号尚未设置密码')
            if not verify_password(password, rec.get('salt'), rec.get('hash'),
                                   rec.get('iters') or _PBKDF2_ITERS):
                rec['fails'] = int(rec.get('fails') or 0) + 1
                if rec['fails'] >= LOCK_AFTER_FAILS:
                    rec['locked_until'] = t + LOCK_SECONDS
                    rec['fails'] = 0
                    self._logln('AUTH 账号锁定: account=%r (%d 次失败)' % (key, LOCK_AFTER_FAILS))
                self._save()
                raise AuthError(HTTP_BAD_PASSWORD, '账号或密码输入错误')
            rec['fails'] = 0
            rec['locked_until'] = 0.0
            rec['last_login'] = t
            self._save()
            return dict(rec)

    # ---- 存量档认领 ----
    def claim(self, account, password, aid):
        """给**存量档**设置口令（首次登录即认领）。

        为什么必须有这一步：改造前 `_accounts.json` 里已有 3 个玩家档（`u0001~u0003`），
        它们没有口令。若直接要求口令，这三个玩家**永远进不去**（"角色没了"）；
        若允许无口令登录，则整条鉴权形同虚设。⇒ 认领：第一次用某个账号名 + 口令
        声明"我是这个档的主人"，之后就必须凭该口令登录。

        ⚠ **认领只对"尚无口令"的记录开放**；已经有口令的记录走 `login()`（不能靠认领改口令）。
        ★★2026-10-26（审查 H2）：**不再允许"先到先得"式自助认领**。旧实现在这里只有
          "账号名存在、且没设过密码"两个条件 —— 攻击者只要知道旧档的账号名（`_accounts.json`
          里的 `user:` 键、或玩家自己的账号名），抢在本人之前设一次密码就把档拿走了。
          现在必须由运维先执行 `_auth_admin.py claim <账号>` 放行（写 `claimable: true` 并留痕），
          玩家才能真正认领。放行是一次性的：认领成功后该标记自动失效。
        """
        key = _norm(account)
        ok, why = check_account_name(key)
        if not ok:
            raise AuthError(HTTP_BAD_PARAM, why)
        ok, why = check_password(password)
        if not ok:
            raise AuthError(HTTP_BAD_PARAM, why)
        with self._lock:
            d = self._load()
            rec = d['accounts'].get(key)
            if rec is None:
                raise AuthError(HTTP_NO_ACCOUNT, '未找到可认领的账号')
            if rec.get('hash'):
                raise AuthError(HTTP_DUPLICATE, '该账号已设置密码，请直接登录')
            if not rec.get('claimable'):
                self._logln('AUTH 认领被拒（未放行）: account=%r aid=%r' % (key, aid))
                raise AuthError(HTTP_CLAIM_FROZEN,
                                '该角色需要管理员迁移后方可认领（请联系管理员）')
            salt, h, iters = hash_password(password)
            rec.update({'salt': salt, 'hash': h, 'iters': iters, 'legacy': True,
                        'claimed': now(), 'aid': aid, 'last_login': now(),
                        'claimable': False, 'fails': 0, 'locked_until': 0.0})
            self._save()
        self._logln('AUTH 存量档认领: account=%r aid=%r' % (key, aid))
        return dict(rec)

    def set_claimable(self, account, allow=True):
        """★运维专用：放行/收回某个存量档的自助认领（`_auth_admin.py claim/reclaim-lock`）。"""
        with self._lock:
            d = self._load()
            rec = d['accounts'].get(_norm(account))
            if rec is None:
                raise AuthError(HTTP_NO_ACCOUNT, '未找到此账号')
            if rec.get('hash'):
                raise AuthError(HTTP_DUPLICATE, '该账号已有密码，不需要认领放行')
            rec['claimable'] = bool(allow)
            rec['claimable_at'] = now() if allow else 0.0
            self._save()
        self._logln('AUTH 认领放行=%r account=%r' % (bool(allow), account))
        return dict(rec)

    # ---- 改密 / 运维重置（★2026-10-26 审查 M3）----------------------------------
    def change_password(self, account, old_password, new_password):
        """改密：**必须验旧密**。成功后返回记录；调用方负责吊销该账号全部会话。

        为什么把吊销放在调用方：会话表在 `game_server.AUTH['sessions']` 里，
        本模块不持有它（测试可以只建 AuthStore）。口径写在调用点的注释里。
        """
        key = _norm(account)
        ok, why = check_password(new_password)
        if not ok:
            raise AuthError(HTTP_BAD_PARAM, why)
        with self._lock:
            d = self._load()
            rec = d['accounts'].get(key)
            if rec is None:
                raise AuthError(HTTP_NO_ACCOUNT, '未找到此账号')
            if rec.get('legacy') and not rec.get('hash'):
                raise AuthError(HTTP_CLAIM_FROZEN, '该存量档尚未认领，请先走管理员迁移')
            if not verify_password(old_password, rec.get('salt'), rec.get('hash'),
                                   rec.get('iters') or _PBKDF2_ITERS):
                rec['fails'] = int(rec.get('fails') or 0) + 1
                self._save()
                raise AuthError(HTTP_BAD_PASSWORD, '原密码不正确')
            salt, h, iters = hash_password(new_password)
            rec.update({'salt': salt, 'hash': h, 'iters': iters,
                        'fails': 0, 'locked_until': 0.0, 'pwd_changed': now()})
            self._save()
        self._logln('AUTH 改密成功: account=%r' % (key,))
        return dict(rec)

    def reset_password(self, account, new_password):
        """★运维专用：重置口令（无旧密，供 `_auth_admin.py reset`）。

        ⚠ 这是"最高权限"操作：调用方必须①拒绝在服务端运行时执行（否则内存库会覆盖回去），
          ②重启服务端后**所有旧票据自然失效**（会话表只在内存里）。
        """
        key = _norm(account)
        ok, why = check_password(new_password)
        if not ok:
            raise AuthError(HTTP_BAD_PARAM, why)
        with self._lock:
            d = self._load()
            rec = d['accounts'].get(key)
            if rec is None:
                raise AuthError(HTTP_NO_ACCOUNT, '未找到此账号')
            salt, h, iters = hash_password(new_password)
            rec.update({'salt': salt, 'hash': h, 'iters': iters, 'legacy': bool(rec.get('legacy')),
                        'fails': 0, 'locked_until': 0.0, 'pwd_changed': now(),
                        'claimable': False})
            self._save()
        self._logln('AUTH 口令被运维重置: account=%r' % (key,))
        return dict(rec)

    def reset_with_code(self, account, code, new_password):
        """★2026-11-07 自助重置：账号 + 重置码 + 新密码 -> (rec, 新重置码)。

        · 注册时由 `register(..., reset_code=...)` 存哈希；这里只比对哈希，不存明文；
        · 重置成功后**轮换**重置码（返回新码），旧码立即失效；
        · 失败 5 次锁 10 分钟（独立于登录锁定，避免把登录一起锁死）；
        · 会话吊销由调用方（HTTP 层）用 `Sessions.revoke_account(aid)` 做。
        """
        key = _norm(account)
        ok, why = check_password(new_password)
        if not ok:
            raise AuthError(HTTP_BAD_PARAM, why)
        code = str(code or '').strip()
        if not code:
            raise AuthError(HTTP_BAD_PARAM, '请输入重置码')
        with self._lock:
            d = self._load()
            rec = d['accounts'].get(key)
            if rec is None:
                raise AuthError(HTTP_NO_ACCOUNT, '未找到此账号')
            rsalt, rh = rec.get('reset_salt'), rec.get('reset_hash')
            if not rsalt or not rh:
                raise AuthError(HTTP_BAD_PASSWORD, '该账号没有重置码，请联系开服者重置')
            t = now()
            if float(rec.get('reset_locked_until') or 0) > t:
                raise AuthError(HTTP_LOCKED, '重置码错误次数过多，请 %d 秒后再试'
                                % int(float(rec['reset_locked_until']) - t + 1))
            if not verify_password(code, rsalt, rh, rec.get('reset_iters') or _PBKDF2_ITERS):
                rec['reset_fails'] = int(rec.get('reset_fails') or 0) + 1
                if rec['reset_fails'] >= RESET_LOCK_AFTER:
                    rec['reset_locked_until'] = t + RESET_LOCK_SECONDS
                    rec['reset_fails'] = 0
                self._save()
                raise AuthError(HTTP_BAD_PASSWORD, '重置码错误')
            salt, h, iters = hash_password(new_password)
            new_code = new_reset_code()
            nsalt, nh, niters = hash_password(new_code)
            rec.update({
                'salt': salt, 'hash': h, 'iters': iters,
                'reset_salt': nsalt, 'reset_hash': nh, 'reset_iters': niters,
                'reset_fails': 0, 'reset_locked_until': 0.0,
                'fails': 0, 'locked_until': 0.0, 'pwd_changed': now(),
            })
            self._save()
        self._logln('AUTH 自助重置成功: account=%r（重置码已轮换）' % (key,))
        return dict(rec), new_code

    def unlock(self, account):
        with self._lock:
            d = self._load()
            rec = d['accounts'].get(_norm(account))
            if rec is None:
                raise AuthError(HTTP_NO_ACCOUNT, '未找到此账号')
            rec['fails'] = 0
            rec['locked_until'] = 0.0
            self._save()
            return dict(rec)

    def mark_claimable_guest(self, account, why='guest-migration'):
        """★2026-11-07：把“游客自动建号”的账号改成**可认领**（首次密码登录即认领）。

        只给 `_migrate_guest_accounts.py` 用：调用前必须已核对该账号的 hash 确实是
        `guest-<crc32(name)>`，否则等于把别人的号变成“谁先登谁拿走”。
        认领后 `claim()` 会写入真口令并把 `claimable` 置 False。
        """
        key = _norm(account)
        with self._lock:
            d = self._load()
            rec = d['accounts'].get(key)
            if rec is None:
                raise AuthError(HTTP_NO_ACCOUNT, '未找到此账号')
            rec.update({'legacy': True, 'salt': '', 'hash': '',
                        'claimable': True, 'claimable_at': now(),
                        'fails': 0, 'locked_until': 0.0,
                        'migrated_at': now(), 'migrated_why': str(why)[:40]})
            self._save()
        self._logln('AUTH 游客号转为可认领: account=%r why=%s' % (key, why))
        return dict(rec)
        return dict(rec)

    # ---- 存量迁移 ----
    def seed_legacy(self, entries, claimable=()):
        """把"磁盘上已有、但口令库里没有"的账号登记为**未认领**。

        `entries`: `[(account, aid), …]`；`claimable`: 允许自助认领的账号名（运维显式放行）。
        幂等：已有记录一律不动（尤其**不覆盖口令**）。

        ★2026-10-26（审查 H2）：新登记的记录默认 `claimable: False` —— "存在旧档"本身
          不再等于"谁先来谁设密码"。放行由 `_auth_admin.py claim` 或环境变量
          `YGZZ_CLAIM_ALLOW` 显式给出（后者只给一次性迁移用，启动日志会逐条打印）。
        """
        allow = {_norm(x) for x in (claimable or ()) if x}
        if not entries:
            return 0
        n = 0
        with self._lock:
            d = self._load()
            for account, aid in entries:
                key = _norm(account)
                if not key or key in d['accounts']:
                    continue
                ok, _why = check_account_name(key)
                if not ok:
                    continue
                d['accounts'][key] = {
                    'aid': aid, 'salt': '', 'hash': '', 'iters': _PBKDF2_ITERS,
                    'legacy': True, 'created': now(), 'last_login': 0.0,
                    'fails': 0, 'locked_until': 0.0,
                    'claimable': key in allow,
                }
                n += 1
            if n:
                self._save()
        if n:
            self._logln('AUTH 存量档登记为未认领: %d 个（放行 %d 个）' % (n, len(allow)))
        return n

    def bind_userid(self, userid, account):
        """把服务端签发的 userid 记到账号上（一个账号可以有多个历史 userid）。"""
        with self._lock:
            d = self._load()
            d['users'][str(userid)] = _norm(account)
            self._save()

    def forget_userid(self, userid):
        with self._lock:
            d = self._load()
            if str(userid) in d['users']:
                d['users'].pop(str(userid), None)
                self._save()
                return True
        return False


def _norm(account):
    """账号名的规范化口径：**只去首尾空白**，不做大小写折叠。

    为什么不做 lower()：客户端注册/登录框原样把玩家输入发上来，折叠大小写会让
    "Alice" 与 "alice" 变成同一个账号（有人会觉得方便，但那是**别人抢注你的账号名**的
    一条捷径）。保持严格区分，注册时"已被占用"的判定也随之严格。
    """
    return account.strip() if isinstance(account, str) else ''


# ---- 会话（票据）----------------------------------------------------------------
class SessionRegistry(object):
    """已认证会话表：`userid -> Session`。

    线程安全：服务端是"每连接一线程 + ThreadingHTTPServer"，HTTP 登录与游戏 socket
    分属不同线程，必须持锁（否则"签发/校验"之间会出现半截状态）。
    """

    def __init__(self, ttl=TICKET_TTL, max_sessions=4096, log=None, on_revoke=None):
        self._lock = threading.RLock()
        self._by_user = {}
        self._by_ticket = {}
        self.ttl = ttl
        self.max_sessions = max_sessions
        self._log = log
        self._on_revoke = on_revoke
        self._serial = 0
        self.issued_total = 0
        self.revoked_total = 0
        self.expired_total = 0

    def _logln(self, s):
        if self._log:
            try:
                self._log(s)
            except Exception:
                pass

    def issue(self, userid, account, aid, peer=None, kick=True):
        """签发一张新票据，并**顶掉**该 userid 上一次的票据（返回被顶掉的 Session 或 None）。

        ⚠ "顶掉"是**同账号重复登录**的判据：客户端会拿着被顶掉的票据连游戏 socket，
        服务端据此拒绝它 —— 这就是"旧连接失效后不能继续写档"的入口。
        ⚠ `on_revoke` 回调（=踢掉旧连接）**在本方法返回后**才触发，不在锁里触发：
          回调要去拿连接的写锁，而写锁的持有者可能正在等本注册表的锁
          （`_bind_userid_if_real` 就是先拿连接锁再读账号上下文）—— 那样就是死锁。

        ★2026-10-26（审查 B1，刷新语义）：`kick=False` 用于**服务端列表刷新**
          （`/ucenter/serverlist` 携票换新票）。刷新不是"另一台设备登录"，不能把
          正在游戏的连接踢下线；此时旧票照样作废（不可再用），只是不触发断连回调。
        """
        with self._lock:
            self._prune_locked()
            old = self._by_user.get(str(userid))
            if old is not None:
                old.revoked = True
                self._by_ticket.pop(old.ticket, None)
                self.revoked_total += 1
            self._serial += 1
            s = Session(secrets.token_urlsafe(24), str(userid), account, aid,
                        self.ttl, peer=peer, serial=self._serial)
            self._by_user[s.userid] = s
            self._by_ticket[s.ticket] = s
            self.issued_total += 1
            if len(self._by_user) > self.max_sessions:
                self._prune_locked(force=True)
        if old is not None:
            self._logln('AUTH 顶号: userid=%s aid=%s 旧票据作废%s'
                        % (userid, old.aid, '' if kick else '（刷新换票，不断连）'))
            if kick:
                self._fire(old, 'revoked')
        return old, s

    def _fire(self, sess, why):
        if self._on_revoke is None:
            return
        try:
            self._on_revoke(sess, why)
        except Exception as e:
            self._logln('AUTH on_revoke 回调异常: %r' % (e,))

    def _prune_locked(self, force=False):
        t = now()
        dead = [u for u, s in self._by_user.items() if not s.alive(t)]
        for u in dead:
            s = self._by_user.pop(u, None)
            if s is not None:
                self._by_ticket.pop(s.ticket, None)
                self.expired_total += 1
        if force and len(self._by_user) > self.max_sessions:
            # 极端情况（大量 userid 只登录不连游戏）：按签发时间淘汰最老的。
            # 不淘汰会让内存随"登录次数"无界增长 —— 这本身就是一个 DoS 面。
            for u, _s in sorted(self._by_user.items(), key=lambda kv: kv[1].issued_at)[
                    :len(self._by_user) - self.max_sessions]:
                _old = self._by_user.pop(u, None)
                if _old is not None:
                    _old.revoked = True
                    self._by_ticket.pop(_old.ticket, None)

    def lookup_userid(self, userid):
        """按 userid 找**仍然有效**的会话；无效返回 `(None, why)`。"""
        with self._lock:
            s = self._by_user.get(str(userid))
            t = now()
            if s is None:
                return None, 'unknown'
            if not s.alive(t):
                why = s.why_dead(t)
                self._by_user.pop(s.userid, None)
                self._by_ticket.pop(s.ticket, None)
                if why == 'expired':
                    self.expired_total += 1
                return None, why
            return s, ''

    def lookup_ticket(self, ticket):
        with self._lock:
            s = self._by_ticket.get(str(ticket))
            t = now()
            if s is None:
                return None, 'unknown'
            if not s.alive(t):
                return None, s.why_dead(t)
            return s, ''

    def mark_used(self, userid):
        with self._lock:
            s = self._by_user.get(str(userid))
            if s is not None:
                s.used_at = now()
                return True
        return False

    def revoke_userid(self, userid, why='manual'):
        with self._lock:
            s = self._by_user.pop(str(userid), None)
            if s is not None:
                s.revoked = True
                self._by_ticket.pop(s.ticket, None)
                self.revoked_total += 1
        if s is not None:
            self._fire(s, why)
        return s

    def revoke_account(self, aid, why='account'):
        """把某账号的**全部**会话作废（同账号重复登录时用）。"""
        with self._lock:
            hits = [s for s in self._by_user.values() if s.aid == aid]
            for s in hits:
                s.revoked = True
                self._by_user.pop(s.userid, None)
                self._by_ticket.pop(s.ticket, None)
                self.revoked_total += 1
        for s in hits:
            self._fire(s, why)
        return len(hits)

    def stats(self):
        with self._lock:
            t = now()
            alive = sum(1 for s in self._by_user.values() if s.alive(t))
            return {'sessions': len(self._by_user), 'alive': alive,
                    'issued_total': self.issued_total,
                    'revoked_total': self.revoked_total,
                    'expired_total': self.expired_total,
                    'ttl': self.ttl}

    def clear(self):
        with self._lock:
            self._by_user.clear()
            self._by_ticket.clear()
