# -*- coding: utf-8 -*-
r"""ygzz_diag.py —— **每一条响应的结构化诊断记录**（发送边界上的唯一埋点）。

## 为什么单独一个模块
与 `ygzz_auth.py` / `acct_identity.py` 同一个理由：`import game_server` 会把 `sys.stdout`
换成 `_Tee`（`game_server.py:81`），测试里一 import 就把测试输出写进**生产日志**。
诊断记录的正确定位是"服务的旁路"，它必须能在服务器之外被单独测试（本模块的
`_frame_compare_test.py` / `_diag_wire_test.py` 就是这么做的）。

## 它解决的问题
改造前"这一条响应发出去了没有 / 发的是什么"只能靠三类**互不对账**的痕迹回答：
* `log('sent msg(%s) …')` —— 只有 msgid 与长度，且**每个分支各写一份**（framing-A 里 60+ 处）；
* `dbg('RESP msg(%s)=…')` —— 只对 7 个白名单号打前 400 字节；
* `conn.sendall(...)` —— 真正的发送点，**一个字节都不记**。
于是"客户端说没收到某条响应"时，无法回答"服务端到底发没发、发了什么、发成功没有"：
`sendall` 返回 ≠ 客户端收到（TCP 只保证写进内核缓冲），而 `sendall` 抛异常时旧代码
在多数分支里**静默吞掉**（`if r: conn.sendall(r)` 之类）。

## 设计：一个出口，四类事件
所有出站字节都必须经过 `emit_*`。事件分四类（`kind`）：

| kind | 含义 | 必带字段 |
|---|---|---|
| `resp` | 对某条请求的**应答** | `mid`(应答号) `req_mid`(请求号) `corr`(请求关联号) `seq` |
| `push` | **主动推送**（无请求对应） | `mid` `reason`(推送原因) |
| `http` | HTTP 响应（含 /admin） | `path` `status` `mid=null` |
| `deny` | 因鉴权/限额**拒绝**而回的错误帧 | `mid` `code` `why` |

**一条响应只允许有一条 `resp`**：`req_mid`/`corr` 由 `begin_request()` 登记，
`emit_resp()` 结束时**核对并清空**；重复 emit 会抛 `DiagError`（不是打一行日志），
`_diag_wire_test.py` 的"无漏记、无重复"判据就是靠这个"一请求一应答"的账。

## 常态记录 vs 限时全量
* **常态**（永远开）：时间、连接 id、账号、请求关联号、消息号、结果码、耗时、字节数、
  **内容哈希**（sha256 前 16 位）、`ok`/`err`。→ `logs/diag/events-*.jsonl`
* **限时全量**（`capture`）：按 **账号** 或 **消息号** 开启，默认 120 秒，记录**脱敏后**的
  请求体与响应体全文。→ `logs/diag/full-*.jsonl`
  密码/票据/令牌等秘密**一律脱敏**（`redact()`），且"能不能开"由调用方（配置/管理端）决定，
  默认**关**。

## 轮转与保留
单文件上限 `MAX_BYTES`（默认 8 MB，与 `game_server._open_std_log` 同量级），
超过即轮转成 `<name>-<时间戳>.jsonl`；保留 `KEEP_FILES`（默认 8）份，更老的删除。
`MEM_EVENTS`（默认 2000）条常驻内存环形缓冲 —— 让 `/admin` 与测试**不必读盘**就能
回答"刚才那 20 条响应是什么"，也让"日志文件被删/滚掉"时仍有一份在手。
"""
import hashlib
import json
import os
import re
import threading
import time

# ---- 上限（全部可按需下调；上调前先算磁盘）--------------------------------------
MAX_BYTES = 8 * 1024 * 1024
KEEP_FILES = 8
MEM_EVENTS = 2000
DEFAULT_CAPTURE_SECONDS = 120.0
MAX_BODY_CHARS = 4096          # 单条全量记录里，请求/响应体各自最多留多少字符

# ★★ 秘密字段名（**必须全部小写**）。判定用 `key.lower() in _SECRET_KEYS`。
#   踩过的坑：这张表原来写成 `('password', …, 'accessToken', …)` —— **混了大写**，
#   于是 `accessToken` 永远匹配不上（`'accesstoken' != 'accessToken'`），
#   而它恰恰是客户端登录回包里最敏感的那个字段之一 ⇒ 名义上"脱敏了"，实际漏了。
#   纪律：这张表只放小写；新增字段时先想"它 lower() 之后是什么"。
_SECRET_KEYS = frozenset((
    'password', 'userpwd', 'passwd', 'pwd', 'ticket', 'token', 'accesstoken',
    'access_token', 'logintoken', 'secret', 'sdkinfo', 'macid', 'openid',
    'sessionid', 'session', 'sign', 'signature',
))
_SECRET_RE = re.compile(
    r'(?i)\b(password|userpwd|passwd|pwd|ticket|token|secret|sdkinfo|macid|openid)=([^&\s,;"\']+)')
# 服务端签发的 userid 是 18 位数字（`ygzz_auth.new_userid`）。它本身不是秘密（客户端会
# 明文带回），但**它等价于一张已认证的入场券**（谁拿到它 + 一个打开的 socket 就能进档），
# 所以成品日志里只留前 4 位 + 后 2 位，够对账、不够复用。
_USERID_RE = re.compile(r'\b(\d{18})\b')


class DiagError(RuntimeError):
    """诊断契约被破坏（漏记/重复记/字段不全）。**故意抛异常**而不是记一行。"""


def sha16(data):
    """内容哈希（sha256 前 16 位）。**任何输入都不抛异常**。

    ⚠ 为什么必须容错：诊断是**旁路**，而它挂在每一个出站帧上。`sha16` 一旦对某种输入
      抛异常，异常会一路穿到 `handle_game` 的外层兜底 —— 症状是"**连接被整个接走、
      客户端一个字节都收不到**"，而日志里只有一行 `GAME err TypeError`。
      本轮实测：framing-A 路径传的是**原始字节**（bytes，没问题），而 LE 路径传的是
      **已解析的 JSON 对象**（dict），`hashlib.sha256(dict)` 直接
      `TypeError: object supporting the buffer API required` ⇒ 所有 LE 登录全断。
      ⇒ 口径：能编码就编码，不能编码就用 `repr`；**宁可哈希一个近似表示，也不能断业务**。
    """
    if data is None:
        return ''
    if isinstance(data, str):
        data = data.encode('utf-8', 'replace')
    elif not isinstance(data, (bytes, bytearray, memoryview)):
        try:
            data = json.dumps(data, ensure_ascii=False, sort_keys=True,
                              default=repr).encode('utf-8', 'replace')
        except Exception:
            data = repr(data).encode('utf-8', 'replace')
    try:
        return hashlib.sha256(bytes(data)).hexdigest()[:16]
    except Exception:
        return ''


def mask_userid(s):
    return _USERID_RE.sub(lambda m: m.group(1)[:4] + '********' + m.group(1)[-2:], s)


def redact(obj, _depth=0):
    """把秘密换成 `***`。递归处理 dict/list/str。

    ⚠ 这是**唯一**的脱敏实现：全量记录、事件记录、异常信息都走它。
      "某一条路径忘了脱敏"是本模块最危险的失败形态（日志比聊天记录还敏感），
      所以对外只暴露这一个入口，内部不再有第二份实现。
    """
    if _depth > 6:
        return '<深度截断>'
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEYS:
                out[k] = '***'
            else:
                out[k] = redact(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v, _depth + 1) for v in obj]
    if isinstance(obj, bytes):
        try:
            return redact(obj.decode('utf-8', 'replace'), _depth)
        except Exception:
            return '<bytes>'
    if isinstance(obj, str):
        s = _SECRET_RE.sub(lambda m: '%s=***' % m.group(1), obj)
        return mask_userid(s)
    return obj


def body_preview(body):
    """把请求/响应体转成**可读、可脱敏**的字符串（不解析失败就原样截断）。"""
    s = None
    if body is None:
        return ''
    if isinstance(body, bytes):
        for enc in ('utf-8', 'latin-1'):
            try:
                s = body.decode(enc)
                break
            except Exception:
                continue
        if s is None:
            return '<%d bytes>' % len(body)
    elif isinstance(body, str):
        s = body
    else:
        try:
            s = json.dumps(body, ensure_ascii=False)
        except Exception:
            s = repr(body)
    s = s.strip()
    if len(s) > MAX_BODY_CHARS:
        s = s[:MAX_BODY_CHARS] + '…<截断>'
    try:
        parsed = json.loads(s)
        s = json.dumps(redact(parsed), ensure_ascii=False, sort_keys=True)
    except Exception:
        s = redact(s)
    if len(s) > MAX_BODY_CHARS:
        s = s[:MAX_BODY_CHARS] + '…<截断>'
    return s


def result_code(body):
    """从响应体里取 **结果码**（客户端 `parseMessage` 的 statecode 就是它）。

    · `[1, …]` / `[0, …]`  → 取第一个元素（int）
    · `{"code":1}`         → 取 code
    · 取不到 → `None`（**不编** 0/1；"没有结果码"与"结果码=0"是两件事）
    """
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray)):
        try:
            body = json.loads(body.decode('utf-8'))
        except Exception:
            return None
    elif isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return None
    if isinstance(body, list):
        if not body:
            return None
        v = body[0]
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    if isinstance(body, dict):
        v = body.get('code')
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return None


class _JsonlWriter(object):
    """追加写 + 按大小轮转 + 保留 N 份。线程安全（一把写锁）。"""

    def __init__(self, directory, prefix, max_bytes=MAX_BYTES, keep=KEEP_FILES):
        self.dir = directory
        self.prefix = prefix
        self.max_bytes = max_bytes
        self.keep = keep
        self._lock = threading.Lock()
        self._fh = None
        self._path = None
        self._n = 0
        self.rotations = 0

    def _ensure_dir(self):
        try:
            os.makedirs(self.dir, exist_ok=True)
            return True
        except Exception:
            return False

    def path(self):
        return self._path

    def _open(self):
        if not self._ensure_dir():
            return None
        self._path = os.path.join(
            self.dir, '%s-%s.jsonl' % (self.prefix, time.strftime('%Y%m%d')))
        try:
            self._fh = open(self._path, 'a', encoding='utf-8', buffering=1)
        except Exception:
            self._fh = None
        return self._fh

    def _rotate(self):
        """把当前文件改名为带时间戳的归档，然后重开。保留 keep 份。"""
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None
        base = self._path
        if base:
            arch = '%s.%s.jsonl' % (os.path.splitext(base)[0], time.strftime('%H%M%S'))
            try:
                os.replace(base, arch)
                self.rotations += 1
            except Exception:
                pass
        self._open()
        self._prune()

    def _prune(self):
        if not self.dir or self.keep <= 0:
            return
        try:
            files = [os.path.join(self.dir, f) for f in os.listdir(self.dir)
                     if f.startswith(self.prefix + '-') or f.startswith(self.prefix + '.')]
            files = [f for f in files if os.path.isfile(f)]
            files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for p in files[self.keep:]:
                try:
                    os.remove(p)
                except OSError:
                    pass
        except Exception:
            pass

    def write(self, obj):
        line = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
        with self._lock:
            if self._fh is None and self._open() is None:
                return False
            try:
                if self._fh.tell() + len(line.encode('utf-8')) + 1 > self.max_bytes:
                    self._rotate()
                self._fh.write(line + '\n')
                self._n += 1
                return True
            except Exception:
                return False

    def close(self):
        with self._lock:
            try:
                if self._fh is not None:
                    self._fh.close()
            except Exception:
                pass
            self._fh = None


class Capture(object):
    """限时全量抓取。`scope` 命中才记全文。"""

    __slots__ = ('kind', 'value', 'until', 'mid', 'aid', 'reason', 'started', 'n')

    def __init__(self, kind, value, seconds, reason=''):
        self.kind = kind                  # 'account' | 'mid' | 'all'
        self.value = value
        self.until = time.time() + max(1.0, float(seconds))
        self.started = time.time()
        self.reason = reason
        self.mid = value if kind == 'mid' else None
        self.aid = value if kind == 'account' else None
        self.n = 0

    def alive(self):
        return time.time() < self.until

    def hits(self, aid=None, mid=None):
        if not self.alive():
            return False
        if self.kind == 'all':
            return True
        if self.kind == 'mid':
            return mid is not None and str(mid) == str(self.value)
        if self.kind == 'account':
            return aid is not None and str(aid) == str(self.value)
        return False

    def to_public(self):
        return {'kind': self.kind, 'value': self.value, 'reason': self.reason,
                'seconds_left': round(max(0.0, self.until - time.time()), 1),
                'hits': self.n}


class Diag(object):
    """诊断总线。**一个进程一个实例**（`game_server` 在启动时构造，测试可自建）。"""

    def __init__(self, directory=None, enabled=True, log=None, max_bytes=MAX_BYTES,
                 keep=KEEP_FILES, mem_events=MEM_EVENTS):
        self.enabled = enabled
        self.dir = directory
        self._log = log
        self._lock = threading.RLock()
        self._events = []                 # 环形缓冲（最近 mem_events 条）
        self._mem_max = mem_events
        self._pending = {}                # (conn_id, mid, corr) -> request context
        self._captures = []
        self._counters = {}
        self.dropped = 0
        self.writer = (_JsonlWriter(directory, 'events', max_bytes, keep)
                       if directory else None)
        self.full_writer = (_JsonlWriter(directory, 'full', max_bytes, keep)
                            if directory else None)

    # ---- 低层 ----
    def _logln(self, s):
        if self._log:
            try:
                self._log(s)
            except Exception:
                pass

    def _bump(self, key, n=1):
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + n

    def counters(self):
        with self._lock:
            return dict(self._counters)

    def _record(self, ev):
        """写内存环 + 落盘。**写入失败不影响业务**（诊断是旁路）。"""
        with self._lock:
            self._events.append(ev)
            if len(self._events) > self._mem_max:
                del self._events[:len(self._events) - self._mem_max]
            w = self.writer
        if w is not None:
            if not w.write(ev):
                self.dropped += 1
        return ev

    def events(self, limit=None, **match):
        """读内存环形缓冲（不读盘）。`match` 里给出的键必须全等。"""
        with self._lock:
            evs = list(self._events)
        if match:
            evs = [e for e in evs if all(e.get(k) == v for k, v in match.items())]
        if limit:
            evs = evs[-limit:]
        return evs

    # ---- 请求上下文：`corr`（请求关联号）的来源 ----
    def begin_request(self, conn_id, mid, corr=None, seq=None, body=None,
                      aid=None, peer=None, frame_mode=None):
        """登记"我收到了这条请求"。返回 `ctx`（dict），必须交给 `emit_resp`。"""
        ctx = {
            'conn': conn_id, 'req_mid': mid, 'corr': corr if corr is not None else seq,
            'seq': seq, 'aid': aid, 'peer': peer, 'mode': frame_mode,
            't0': time.time(),
            'req_sha': sha16(body) if body is not None else '',
            'req_body': body,
        }
        if not self.enabled:
            return ctx
        with self._lock:
            self._pending[(conn_id, mid, ctx['corr'])] = ctx
        self._bump('req')
        if self._captures:
            self._maybe_full(ctx, 'req')
        return ctx

    def _maybe_full(self, ctx, phase, out_body=None, note=''):
        caps = [c for c in self._captures if c.alive()]
        if len(caps) != len(self._captures):
            with self._lock:
                self._captures = [c for c in self._captures if c.alive()]
        hit = [c for c in caps if c.hits(aid=ctx.get('aid'), mid=ctx.get('req_mid'))]
        if not hit:
            return False
        rec = {
            'ts': _iso(), 'phase': phase, 'conn': ctx.get('conn'),
            'aid': ctx.get('aid'), 'mid': ctx.get('req_mid'), 'corr': ctx.get('corr'),
            'capture': [c.to_public()['kind'] + ':' + str(c.to_public()['value']) for c in hit],
            'note': note,
        }
        if phase == 'req':
            rec['request'] = body_preview(ctx.get('req_body'))
        else:
            rec['response'] = body_preview(out_body)
            rec['response_sha16'] = sha16(out_body)
        with self._lock:
            for c in hit:
                c.n += 1
        w = self.full_writer
        if w is not None:
            w.write(rec)
        self._bump('full')
        return True

    def _take_pending(self, ctx, mid):
        """把这次应答从"待应答"账上摘下来。**漏记/重复记都在这里判**。"""
        if not self.enabled:
            return
        key = (ctx.get('conn'), ctx.get('req_mid'), ctx.get('corr'))
        with self._lock:
            if key in self._pending:
                self._pending.pop(key, None)
                return
        raise DiagError(
            '响应没有对应的待应答请求 (conn=%r req_mid=%r corr=%r -> resp_mid=%r)；'
            '要么重复 emit 了同一条响应，要么漏了 begin_request()'
            % (ctx.get('conn'), ctx.get('req_mid'), ctx.get('corr'), mid))

    def pending_count(self):
        with self._lock:
            return len(self._pending)

    # ---- 四类事件 ----
    def _base(self, kind, conn_id, aid, mid, corr, ok, elapsed_ms, nbytes, body,
              note='', extra=None):
        ev = {
            'ts': _iso(), 't': round(time.time(), 3), 'kind': kind,
            'conn': conn_id, 'aid': aid, 'acct': aid, 'mid': mid, 'corr': corr,
            'ok': bool(ok), 'ms': None if elapsed_ms is None else round(elapsed_ms, 3),
            'bytes': nbytes,
            'sha': sha16(body) if body is not None else '',
            'code': result_code(body),
        }
        if note:
            ev['note'] = note
        if extra:
            ev.update(extra)
        return ev

    def emit_resp(self, ctx, mid, body, ok=True, err=None, note='', now_ts=None):
        """**应答**（对某条请求的响应）。`ok=False` 表示 `sendall` 失败。

        `body=None` 时，字节数与内容哈希从 `ctx` 里取（`resp_bytes` / `resp_sha`）——
        调用方（`game_server.WIRE`）在**真正发出帧的那一刻**已经算过一遍，
        再算一次是浪费；而"记不到字节数"会让 `bytes=0` 这种**看不出真假**的值进日志。
        """
        if not self.enabled:
            return None
        # 先判"有没有对应的待应答"——它必须在**任何副作用之前**，否则漏记会被掩盖。
        self._take_pending(ctx, mid)
        t = time.time()
        elapsed = (t - ctx['t0']) * 1000.0 if ctx.get('t0') else None
        if body is None:
            nbytes = int(ctx.get('resp_bytes') or 0)
            sha = ctx.get('resp_sha') or ''
        else:
            nbytes = _nbytes(body)
            sha = sha16(body)
        ev = self._base('resp', ctx.get('conn'), ctx.get('aid'), mid,
                        ctx.get('corr'), ok, elapsed, nbytes, None, note,
                        {'req_mid': ctx.get('req_mid'), 'seq': ctx.get('seq'),
                         'req_sha': ctx.get('req_sha'), 'mode': ctx.get('mode')})
        ev['sha'] = sha
        ev['code'] = result_code(body) if body is not None else ctx.get('resp_code')
        if not ok:
            ev['err'] = str(err)[:300]
        self._bump('resp')
        self._bump('resp_err' if not ok else 'resp_ok')
        if self._captures and body is not None:
            self._maybe_full(dict(ctx), 'resp', out_body=body)
        return self._record(ev)

    def emit_push(self, conn_id, aid, mid, body, ok=True, err=None, reason='',
                  via=''):
        """**主动推送**（没有对应的请求）。`reason` 写清"为什么推这一帧"。"""
        if not self.enabled:
            return None
        ev = self._base('push', conn_id, aid, mid, None, ok, None, _nbytes(body), body,
                        reason, {'reason': reason, 'via': via})
        if not ok:
            ev['err'] = str(err)[:300]
        self._bump('push')
        self._bump('push_err' if not ok else 'push_ok')
        if self._captures:
            ctx = {'conn': conn_id, 'aid': aid, 'req_mid': mid, 'corr': None,
                   'req_body': None}
            self._maybe_full(ctx, 'push', out_body=body, note=reason)
        return self._record(ev)

    def emit_http(self, path, method, status, body, ok=True, err=None, aid=None,
                  peer=None, ms=None, note=''):
        """**HTTP 响应**（含 CDN 与 /admin）。"""
        if not self.enabled:
            return None
        ev = self._base('http', None, aid, None, None, ok, ms, _nbytes(body), body,
                        note, {'path': path, 'method': method, 'status': status,
                               'peer': peer})
        if not ok:
            ev['err'] = str(err)[:300]
        self._bump('http')
        return self._record(ev)

    def emit_deny(self, conn_id, aid, mid, body, why, code=None, ok=True, err=None):
        """**拒绝**（鉴权失败/超限/非法参数）。`why` 是机器可读的原因。"""
        if not self.enabled:
            return None
        ev = self._base('deny', conn_id, aid, mid, None, ok, None, _nbytes(body), body,
                        why, {'why': why, 'code': code if code is not None else
                              result_code(body)})
        if not ok:
            ev['err'] = str(err)[:300]
        self._bump('deny')
        self._bump('deny_' + str(why))
        return self._record(ev)

    # ---- 全量抓取开关 ----
    def capture(self, kind, value=None, seconds=DEFAULT_CAPTURE_SECONDS, reason=''):
        """开启一次限时全量抓取。返回 `Capture`。

        `kind`：`'account'`（按账号 id）/ `'mid'`（按消息号）/ `'all'`（全部，慎用）。
        ⚠ 调用方负责"谁能开"——本模块不做权限判断（它也不该知道有谁）。
        """
        if kind not in ('account', 'mid', 'all'):
            raise ValueError('capture kind 只能是 account/mid/all')
        c = Capture(kind, value, seconds, reason)
        with self._lock:
            self._captures.append(c)
        self._bump('capture_start')
        self._logln('DIAG 开启全量抓取: kind=%s value=%r %.0fs reason=%r'
                    % (kind, value, c.until - c.started, reason))
        return c

    def captures(self):
        with self._lock:
            self._captures = [c for c in self._captures if c.alive()]
            return [c.to_public() for c in self._captures]

    def stop_captures(self):
        with self._lock:
            n = len(self._captures)
            self._captures = []
        return n

    # ---- 汇总（给 /admin 与压测用）----
    def summary(self, window=200):
        evs = self.events(limit=window)
        by_mid = {}
        errors = 0
        for e in evs:
            k = e.get('mid')
            d = by_mid.setdefault(str(k), {'n': 0, 'err': 0, 'ms_max': 0.0})
            d['n'] += 1
            if not e.get('ok'):
                d['err'] += 1
                errors += 1
            if e.get('ms'):
                d['ms_max'] = max(d['ms_max'], e['ms'])
        with self._lock:
            mem = len(self._events)
        return {
            'enabled': self.enabled, 'dir': self.dir, 'mem_events': mem,
            'pending': self.pending_count(), 'dropped': self.dropped,
            'counters': self.counters(), 'errors_in_window': errors,
            'by_mid': by_mid, 'captures': self.captures(),
            'patterns': sorted({e.get('pattern') for e in evs if e.get('pattern')}),
        }


def _nbytes(body):
    if body is None:
        return 0
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    if isinstance(body, str):
        return len(body.encode('utf-8', 'replace'))
    return len(str(body).encode('utf-8', 'replace'))


def _iso():
    return time.strftime('%Y-%m-%dT%H:%M:%S') + ('.%03d' % int((time.time() % 1) * 1000))


# ---- 全局实例（`game_server` 启动时 configure；测试可自建）-------------------------
DIAG = None


def configure(directory=None, enabled=True, log=None, **kw):
    global DIAG
    DIAG = Diag(directory=directory, enabled=enabled, log=log, **kw)
    return DIAG


def get():
    return DIAG
