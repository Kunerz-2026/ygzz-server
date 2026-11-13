# -*- coding: utf-8 -*-
r"""_acct_backup.py —— 多人单服的**存档备份与恢复**（账号索引 + 全部玩家档 + 世界档 + 口令库 + 事务日志）。

## 为什么不能用既有的 `state.backup_state()`
`state.backup_state()` 快照的是**单档** `state.json`（+ `.meta.json` 边车）。
改造后玩家数据在 `accounts\` 下分五类：

| 文件 | 是什么 | 丢了会怎样 |
|---|---|---|
| `_accounts.json` | 索引：peer / `user:<账号>` -> 档名 | 全部玩家**重新发号** ⇒ 所有人进空档 |
| `u000N.json` | 每个玩家的档 | 该玩家回档 |
| `_world.json` | 共用世界（妖盟 / 聊天） | 妖盟与聊天记录丢失 |
| `_auth.json` | **口令库** | 谁都进不去（或更糟：被人重新注册同名账号接管存档） |
| `_guild_txn.json` | 妖盟事务日志 | 未完成事务无法恢复 / 状态劈成两半 |

⇒ 备份单位是**整个数据根目录**，不是 `state.json`。

## 数据根：默认兼容，但真实部署常在别处
默认 `work\accounts`（与旧版一致）。**隔离部署**（`_deploy_isolated.py`）的数据在
`%TEMP%\dsh-ygzz-deploy\work\accounts`，与源码 `work\accounts` **不是同一个目录**。
所有子命令都接受 `--data-root PATH`（也可放在子命令前），或环境变量 `YGZZ_DATA_ROOT`。
每条命令开头都会打印**解析后的绝对路径 + 文件数**，`doctor` 会自检数据根是否像账号目录。

## 用法
```
python -X utf8 _acct_backup.py doctor [--data-root PATH]
python -X utf8 _acct_backup.py snapshot [--note "说明"] [--live] [--data-root PATH]
python -X utf8 _acct_backup.py list [--data-root PATH]
python -X utf8 _acct_backup.py verify <快照名> [--data-root PATH]
python -X utf8 _acct_backup.py restore <快照名> [--yes] [--force] [--data-root PATH]
python -X utf8 _acct_backup.py restore --auto-rollback [--data-root PATH]
python -X utf8 _acct_backup.py restore --rollback <pre-restore 名> [--data-root PATH]
python -X utf8 _acct_backup.py prune [--data-root PATH]
```

## 快照口径（MANIFEST version 2）
1. **停写优先**：数据根同级/上级出现 `server.pid`，或 18080/27001/27002 在 LISTENING
   时，`snapshot` 默认**拒绝**；`--live` 可显式强制。`--live` 的每个文件按
   "复制 → 对**目标副本**算 sha256 → 源在复制期间变了就重试一次" 处理，
   MANIFEST 记 `consistent: false` + warnings（**恢复时仍可用，但会打印警示**）。
   非 `--live` 记 `consistent: true`。
2. MANIFEST 里的 sha256/size 一律对**快照副本**计算，不对源文件计算。
3. 恢复 = verify 快照 → 暂存目录逐文件校验 → 给当前现场打一份带 MANIFEST 的
   `*-pre-restore` 快照 → 逐文件 `os.replace` 原子切换 → 删掉数据根里快照没有的
   旧数据文件（只删 `collect()` 范围，不碰 `_backups` / `.restore-staging-*`）。
   任何一步失败都从 `pre-restore` **自动回滚**，并把失败写进恢复日志；返回非 0。
4. `_backups` 里普通快照只保留 `KEEP=20` 份；`*-pre-restore` **永不被普通 prune 删掉**，
   显式 `prune` 也至少保留最近 `PRE_RESTORE_KEEP=3` 份。
5. 恢复日志 `数据根\.restore-journal.json`：started / finished / failed。若上次恢复
   未完成（status=started），`doctor`/`restore` 会提示，不会静默忽略。

## 测试钩子（**仅用于自动化测试，生产环境不要设置**）
- `YGZZ_BACKUP_FAIL_AFTER=<n>`：在第 n 个 `os.replace` 之后抛错，用于验证失败自动回滚。
- `--assume-running`：让 `snapshot`/`restore` 当作服务端正在运行（不真占端口）。
- `YGZZ_BACKUP_NO_PORTSCAN=1`：跳过端口探测（离线测试 / 非标准端口环境）。

## 已知边界（如实说明）
- `--live` 只做"复制完再核对源是否变化"的尽力一致性，**不是强一致**：索引/玩家档/
  世界/口令库仍可能来自不同时点；服务端内存里的档也可能在恢复后再次落盘覆盖。
- 恢复的原子性单位是**单个文件**，不是整个目录；所以必须依赖 pre-restore + 自动回滚。
- 实际运行中的服务端判断依赖 `server.pid` 与默认端口；改端口/改 pid 位置的部署请
  用 `--live`/`--force` 前先人工确认。
- 快照放在数据根自己的 `_backups\` 里；整个数据根被删时备份也会一起没了，
  生产上应另行定期把 `_backups\` 复制到别的盘。
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_ROOT = os.path.join(HERE, 'accounts')
DATA_ROOT_ENV = 'YGZZ_DATA_ROOT'
SNAPDIR_NAME = '_backups'
MANIFEST = 'MANIFEST.json'
JOURNAL = '.restore-journal.json'
STAGING_PREFIX = '.restore-staging-'
MANIFEST_VERSION = 2
KEEP = 20
PRE_RESTORE_KEEP = 3
PORTS = (18080, 27001, 27002)
PLAYER_RE = re.compile(r'^u[^/\\]*\.json$')
PRE_RESTORE_RE = re.compile(r'-pre-restore(?:-\d+)?$')
TMP_SUFFIXES = ('.tmp', '.corrupt')

# 兼容旧引用：它们只是**默认值**；所有命令实际用 `resolve_data_root()` 的结果。
ACCT = DEFAULT_DATA_ROOT
SNAPDIR = os.path.join(ACCT, SNAPDIR_NAME)


class BackupError(Exception):
    """可预期的备份/恢复配置错误 —— CLI 顶层转成非 0 退出码，不打印 traceback。"""


class InjectedFailure(RuntimeError):
    """测试钩子抛出的异常（不会被当成配置错误）。"""


# ---------------------------------------------------------------- 基础工具
def sha256_file(p, chunk=1 << 20):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for blk in iter(lambda: f.read(chunk), b''):
            h.update(blk)
    return h.hexdigest()


def default_data_root():
    return os.path.abspath(DEFAULT_DATA_ROOT)


def resolve_data_root(cli_value=None):
    """CLI > 环境变量 > 默认 `work\\accounts`。返回绝对路径。"""
    raw = cli_value or os.environ.get(DATA_ROOT_ENV) or DEFAULT_DATA_ROOT
    return os.path.abspath(os.path.expanduser(os.path.expandvars(raw)))


def _snapdir(root):
    return os.path.join(root, SNAPDIR_NAME)


def _journal_path(root):
    return os.path.join(root, JOURNAL)


def _is_transient_file(name):
    return (name == JOURNAL or name.startswith(JOURNAL + '.')
            or name == 'server.pid'
            or name.endswith(TMP_SUFFIXES) or '.corrupt-' in name)


def collect(root=None):
    """要备份/恢复的**数据文件**（相对路径 -> 绝对路径）。

    排除：`_backups\\`、`.restore-staging-*\\`、恢复日志、`server.pid`、`.tmp`/`.corrupt`
    残留。这个排除范围也就是 restore 允许删除的范围。
    """
    root = root or ACCT
    out = {}
    if not os.path.isdir(root):
        return out
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d != SNAPDIR_NAME and not d.startswith(STAGING_PREFIX)]
        for fn in files:
            if _is_transient_file(fn):
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, root).replace('\\', '/')
            out[rel] = fp
    return out


def _is_player_rel(rel):
    return '/' not in rel and bool(PLAYER_RE.match(rel))


def _count_players(files):
    return sum(1 for rel in files if _is_player_rel(rel))


def _is_pre_restore(name):
    return bool(PRE_RESTORE_RE.search(name or ''))


def _norm(path):
    return os.path.normcase(os.path.abspath(path or ''))


def _same_root(a, b):
    return _norm(a) == _norm(b)


def _safe_rel(rel):
    """MANIFEST 里的相对路径只允许普通子路径；防路径逃逸。"""
    if not isinstance(rel, str) or not rel:
        return None
    if rel.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', rel):
        return None
    parts = rel.replace('\\', '/').split('/')
    if any(p in ('', '.', '..') for p in parts):
        return None
    return '/'.join(parts)


def _tail(r, n=900):
    txt = ((r.stdout or '') + (r.stderr or '')).strip()
    return txt[-n:]


# ---------------------------------------------------------------- 数据根自检
def _root_problem(root):
    """返回 None 表示像账号目录；否则返回原因字符串。

    判据（与任务书一致）：`_accounts.json` 存在 **或** 存在任何根级 `u*.json`。
    """
    if not os.path.exists(root):
        return '目录不存在'
    if not os.path.isdir(root):
        return '不是目录'
    try:
        names = os.listdir(root)
    except OSError as e:
        return '无法列出目录: %r' % (e,)
    if '_accounts.json' in names and os.path.isfile(os.path.join(root, '_accounts.json')):
        return None
    for n in names:
        if _is_player_rel(n) and os.path.isfile(os.path.join(root, n)):
            return None
    return '缺 _accounts.json 且没有任何 u*.json'


def _root_blank(root):
    """目录不存在，或只有 `_backups`/日志/暂存/pid 等非数据项。"""
    if not os.path.exists(root):
        return True
    if not os.path.isdir(root):
        return False
    for n in os.listdir(root):
        if n == SNAPDIR_NAME or _is_transient_file(n) or n.startswith(STAGING_PREFIX):
            continue
        return False
    return True


def _root_hint(root):
    iso = os.path.join(tempfile.gettempdir(), 'dsh-ygzz-deploy', 'work', 'accounts')
    return ('  这不像账号数据根（%s）。\n'
            '  · 隔离部署（_deploy_isolated.py）的真实数据根通常是:\n'
            '      %s\n'
            '  · 用 --data-root <路径>（或环境变量 %s）指过去；默认是:\n'
            '      %s' % (_root_problem(root) or '未知', iso, DATA_ROOT_ENV,
                     default_data_root()))


def _staging_dirs(root):
    try:
        return sorted(n for n in os.listdir(root)
                      if n.startswith(STAGING_PREFIX) and os.path.isdir(os.path.join(root, n)))
    except OSError:
        return []


# ---------------------------------------------------------------- 服务端运行判据
_LISTEN_CACHE = None


def _listening_ports():
    """本机 LISTENING 的 TCP 端口集合。失败时退化为对本工具关心的端口做 connect 探测。"""
    global _LISTEN_CACHE
    if _LISTEN_CACHE is not None:
        return _LISTEN_CACHE
    ports = set()
    if os.name == 'nt':
        try:
            r = subprocess.run(['netstat', '-ano', '-p', 'tcp'], capture_output=True,
                               text=True, errors='replace', timeout=8)
            for line in (r.stdout or '').splitlines():
                up = line.upper()
                if 'LISTENING' not in up:
                    continue
                m = re.search(r':(\d+)\s+\S+\s+LISTENING', up)
                if m:
                    ports.add(int(m.group(1)))
        except Exception:
            pass
    if not ports:
        for p in PORTS:
            s = socket.socket()
            try:
                s.settimeout(0.2)
                if s.connect_ex(('127.0.0.1', p)) == 0:
                    ports.add(p)
            except OSError:
                pass
            finally:
                s.close()
    else:
        # netstat 输出可能被本地化/格式变化漏掉端口；对我们关心的三个再做一次连通探测。
        for p in PORTS:
            if p in ports:
                continue
            s = socket.socket()
            try:
                s.settimeout(0.2)
                if s.connect_ex(('127.0.0.1', p)) == 0:
                    ports.add(p)
            except OSError:
                pass
            finally:
                s.close()
    _LISTEN_CACHE = ports
    return ports


def _running_reasons(root, assume_running=False):
    """服务端是否可能正在写数据根。返回原因列表（空 = 没检测到）。

    判据：数据根本身/父级/祖父级有 `server.pid`，或默认端口在 LISTENING。
    注意 `server.pid` **按存在性**判定（宁可拒绝也不要边跑边拷贝）；如果确认它是
    崩溃残留，用 `--live`（snapshot）或 `--force`（restore）。
    """
    reasons = []
    if assume_running or os.environ.get('YGZZ_BACKUP_ASSUME_RUNNING') == '1':
        reasons.append('--assume-running（测试钩子：当作服务端正在运行）')
    seen = set()
    for d in (root, os.path.dirname(root), os.path.dirname(os.path.dirname(root))):
        if not d or d in seen:
            continue
        seen.add(d)
        p = os.path.join(d, 'server.pid')
        if os.path.isfile(p):
            try:
                txt = open(p, encoding='ascii', errors='replace').read().strip()[:32]
            except OSError as e:
                txt = '读取失败 %r' % (e,)
            reasons.append('发现 %s（内容 %r）' % (p, txt))
    if os.environ.get('YGZZ_BACKUP_NO_PORTSCAN') != '1':
        busy = sorted(set(PORTS) & _listening_ports())
        if busy:
            reasons.append('端口 %s 正在 LISTENING' % busy)
    return reasons


# ---------------------------------------------------------------- MANIFEST / 日志
def _atomic_json(path, obj):
    tmp = '%s.%d.tmp' % (path, os.getpid())
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _load_json(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _read_manifest(snap_dir):
    p = os.path.join(snap_dir, MANIFEST)
    if not os.path.isfile(p):
        raise BackupError('不是本工具的快照（缺 %s）: %s' % (MANIFEST, snap_dir))
    try:
        man = _load_json(p)
    except Exception as e:
        raise BackupError('MANIFEST 读取失败 %s: %r' % (p, e))
    if not isinstance(man, dict):
        raise BackupError('MANIFEST 顶层必须是 JSON object: %s' % p)
    files = man.get('files')
    if not isinstance(files, dict):
        raise BackupError('MANIFEST.files 必须是 object: %s' % p)
    for rel, info in files.items():
        if not isinstance(rel, str) or not isinstance(info, dict):
            raise BackupError('MANIFEST.files 含非法条目: %r -> %r' % (rel, info))
        if not isinstance(info.get('sha256'), str):
            raise BackupError('MANIFEST.files[%r] 缺 sha256' % (rel,))
    return man


def _read_journal(root):
    """返回 (dict|None, 错误字符串|None)。"""
    p = _journal_path(root)
    if not os.path.isfile(p):
        return None, None
    try:
        d = _load_json(p)
    except Exception as e:
        return None, '恢复日志无法解析 %s: %r' % (p, e)
    if not isinstance(d, dict):
        return None, '恢复日志顶层不是 object: %s' % p
    return d, None


def _write_journal(root, data):
    _atomic_json(_journal_path(root), data)


def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%S')


# ---------------------------------------------------------------- 快照
def _inside(path, base):
    """path 是否严格位于 base 之内（normcase + 末尾分隔符，避免 `_backups-evil` 误判）。"""
    b = os.path.normcase(os.path.abspath(base))
    c = os.path.normcase(os.path.abspath(path))
    return c.startswith(b + os.sep)


def _snap_path(root, name):
    """解析快照标识：既接受 `_backups` 下的目录名，也接受 snapshot 输出里的绝对路径。

    含分隔符/绝对路径必须在 `_backups` 之内（normpath + 前缀校验），否则拒绝；
    返回快照目录的绝对路径。运营会直接从 `snapshot` 输出复制路径，所以必须宽进严出。
    """
    if not name:
        raise BackupError('快照名不能为空')
    raw = str(name)
    snaproot = _snapdir(root)
    if os.path.isabs(raw) or '/' in raw or '\\' in raw:
        tries = [os.path.abspath(raw)]
        if not os.path.isabs(raw):
            tries.append(os.path.abspath(os.path.join(root, raw)))
        for cand in tries:
            if not _inside(cand, snaproot) or not os.path.isdir(cand):
                continue
            rel = os.path.relpath(os.path.abspath(cand), os.path.abspath(snaproot))
            parts = rel.split(os.sep)
            if any((p in ('', '.', '..')) or p.startswith('.') for p in parts):
                raise BackupError('非法快照路径: %r' % (raw,))
            return os.path.normpath(cand)
        raise BackupError('非法快照路径（必须在 %s 之内）: %r' % (snaproot, raw))
    if raw in ('.', '..') or os.path.basename(raw) != raw \
            or ':' in raw or raw.startswith('.'):
        raise BackupError('非法快照名: %r（只接受 _backups 下的目录名或其中的绝对路径）'
                          % (raw,))
    dest = os.path.join(snaproot, raw)
    if not os.path.isdir(dest):
        raise BackupError('找不到快照 %s（数据根 %s 的 %s 下）'
                          % (raw, root, SNAPDIR_NAME))
    return dest


def _copy_hashed(src, dst, rel, live):
    """copy2 到 dst，对 **dst** 算 sha256/size；live 时若源在复制期间变化就重试一次。"""
    warns = []
    sha = None
    size = None
    for attempt in (1, 2):
        st0 = os.stat(src)
        shutil.copy2(src, dst)
        sha = sha256_file(dst)
        size = os.path.getsize(dst)
        if not live:
            return sha, size, warns
        st1 = os.stat(src)
        # mtime/size 是快速判据；再比对一次源内容，避免“内容变了但时间戳没变”被漏掉。
        if (st0.st_size, st0.st_mtime_ns) == (st1.st_size, st1.st_mtime_ns) \
                and sha256_file(src) == sha:
            return sha, size, warns
        if attempt == 1:
            warns.append('%s 在复制期间被更新（%d -> %d 字节），已重试一次'
                         % (rel, st0.st_size, st1.st_size))
    warns.append('%s 重试后仍在变化，快照内容可能撕裂（consistent=false）' % rel)
    return sha, size, warns


def _new_snapshot_name(root, suffix=''):
    base = time.strftime('%Y%m%d-%H%M%S') + suffix
    name = base
    n = 1
    while os.path.exists(os.path.join(_snapdir(root), name)):
        name = '%s-%d' % (base, n)
        n += 1
    return name


def create_snapshot(root, note='', live=False, suffix='', allow_empty=False,
                    require_index=True, enforce_auth=True, prune_after=True):
    """打一份带 MANIFEST v2 的快照。返回 (快照目录绝对路径, manifest dict)。"""
    files = collect(root)
    players = _count_players(files)
    if not allow_empty:
        problem = _root_problem(root)
        if problem:
            raise BackupError('拒绝备份：%s\n%s' % (problem, _root_hint(root)))
        if require_index and '_accounts.json' not in files:
            raise BackupError('拒绝备份：缺账号索引 _accounts.json（没有它恢复等于全部玩家重新发号）')
        if not players:
            raise BackupError('拒绝备份：一份玩家档 u*.json 都没有（宁可失败也不要假备份）')
    elif require_index and files and '_accounts.json' not in files:
        raise BackupError('拒绝备份：有数据但缺账号索引 _accounts.json')
    auth_env = os.environ.get('YGZZ_AUTH') == '1'
    if enforce_auth and auth_env and files and '_auth.json' not in files:
        raise BackupError('拒绝备份：YGZZ_AUTH=1 但快照里没有口令库 _auth.json')

    warns = []
    if live:
        warns.append('--live 在线快照：各文件不是同一时点，恢复后请人工核对 '
                     '索引/玩家档/世界/口令库（consistent=false）')
    if '_world.json' not in files and files:
        warns.append('缺 _world.json —— 妖盟/共用世界数据不在快照里')
    if '_auth.json' not in files and files:
        warns.append('缺口令库 _auth.json —— 恢复后挡不住别人重新注册同名账号接管存档')
    if '_guild_txn.json' in files:
        warns.append('快照里含未完成妖盟事务日志 _guild_txn.json，恢复后服务端会尝试回滚该事务')

    os.makedirs(_snapdir(root), exist_ok=True)
    name = _new_snapshot_name(root, suffix)
    dest = os.path.join(_snapdir(root), name)
    tmpdir = os.path.join(_snapdir(root), '.building-%s-%d' % (name, os.getpid()))
    try:
        # 进程崩溃后可能留下同名构建目录（pid 复用）；先清掉，避免把陈旧文件发布成快照。
        if os.path.exists(tmpdir):
            shutil.rmtree(tmpdir, ignore_errors=True)
        os.makedirs(tmpdir)
        man_files = {}
        for rel, fp in sorted(files.items()):
            tgt = os.path.join(tmpdir, rel.replace('/', os.sep))
            os.makedirs(os.path.dirname(tgt), exist_ok=True)
            sha, size, cw = _copy_hashed(fp, tgt, rel, live)
            for w in cw:
                if w not in warns:
                    warns.append(w)
            man_files[rel] = {'sha256': sha, 'size': size}
        man = {
            'version': MANIFEST_VERSION,
            'created': _now(),
            'note': note or '',
            'data_root': os.path.abspath(root),
            'consistent': (not live),
            'files': man_files,
            'count': len(man_files),
            'players': players,
            'auth_enabled': bool(auth_env or '_auth.json' in files),
            'warnings': warns,
            'empty_root': (not files),
        }
        _atomic_json(os.path.join(tmpdir, MANIFEST), man)
        os.replace(tmpdir, dest)
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    if prune_after:
        removed = _prune(root)
        if removed:
            print('  已按保留策略清理旧快照 %d 份' % len(removed))
    return dest, man


def verify_snapshot(root, name, show=True):
    """逐文件核对 MANIFEST。返回 (rc, manifest)；rc: 0 完好 / 1 损坏 / 2 不可用。"""
    snap = _snap_path(root, name)
    man = _read_manifest(snap)
    files = man.get('files') or {}
    if not files and not man.get('empty_root'):
        raise BackupError('MANIFEST 没有任何文件，拒绝把 %s 当作可用快照' % name)
    bad = []
    if show:
        print('校验快照 %s（%d 个文件）:' % (name, len(files)))
    for rel in sorted(files):
        info = files.get(rel) or {}
        safe = _safe_rel(rel)
        if safe is None:
            raise BackupError('MANIFEST 含非法路径 %r' % (rel,))
        fp = os.path.join(snap, safe.replace('/', os.sep))
        want = info.get('sha256')
        if not os.path.isfile(fp):
            bad.append('%s（缺失）' % rel)
            if show:
                print('  [FAIL] %-28s 缺失' % rel)
            continue
        actual = sha256_file(fp)
        size = os.path.getsize(fp)
        if actual != want:
            bad.append('%s（哈希不符）' % rel)
            if show:
                print('  [FAIL] %-28s 哈希不符 MANIFEST=%s 实际=%s'
                      % (rel, want, actual))
        elif info.get('size') is not None and size != info.get('size'):
            bad.append('%s（大小不符）' % rel)
            if show:
                print('  [FAIL] %-28s 大小不符 MANIFEST=%s 实际=%s'
                      % (rel, info.get('size'), size))
        elif show:
            print('  [ok]   %-28s %s  %d 字节' % (rel, actual, size))
    # 发现 MANIFEST 未列出的文件（不阻断恢复，但要说出来）
    try:
        listed = set(files)
        extra = []
        for dirpath, _dirs, fns in os.walk(snap):
            for fn in fns:
                rel = os.path.relpath(os.path.join(dirpath, fn),
                                      snap).replace('\\', '/')
                if rel != MANIFEST and rel not in listed:
                    extra.append(rel)
        if extra and show:
            print('  [warn] 快照目录里有 MANIFEST 未列出的文件: %s' % extra[:8])
    except OSError:
        pass
    if bad:
        if show:
            print('★ 快照已损坏，**不能**用它恢复: %s' % bad[:8])
        return 1, man
    if show:
        print('快照 %s 完好：%d 个文件逐一一致（version=%s, consistent=%s）'
              % (name, len(files), man.get('version'), man.get('consistent')))
    return 0, man


def _prune(root):
    """普通快照只保留 KEEP 份；`*-pre-restore` 不参与（普通 prune 永不删它们）。"""
    snapdir = _snapdir(root)
    if not os.path.isdir(snapdir):
        return []
    rows = []
    for n in os.listdir(snapdir):
        p = os.path.join(snapdir, n)
        if n.startswith('.') or _is_pre_restore(n) or not os.path.isdir(p):
            continue
        if not os.path.isfile(os.path.join(p, MANIFEST)):
            continue   # 不认识/半成品目录：不自动删
        rows.append((os.path.getmtime(p), n))
    rows.sort()
    removed = []
    for _m, n in (rows[:-KEEP] if KEEP > 0 else rows):
        shutil.rmtree(os.path.join(snapdir, n), ignore_errors=True)
        removed.append(n)
    return removed


def _prune_pre_restore(root):
    """显式 prune：pre-restore 至少保留最近 3 份。"""
    snapdir = _snapdir(root)
    if not os.path.isdir(snapdir):
        return []
    rows = []
    for n in os.listdir(snapdir):
        p = os.path.join(snapdir, n)
        if not _is_pre_restore(n) or not os.path.isdir(p):
            continue
        if not os.path.isfile(os.path.join(p, MANIFEST)):
            continue
        rows.append((os.path.getmtime(p), n))
    rows.sort()
    removed = []
    for _m, n in (rows[:-PRE_RESTORE_KEEP] if PRE_RESTORE_KEEP > 0 else rows):
        shutil.rmtree(os.path.join(snapdir, n), ignore_errors=True)
        removed.append(n)
    return removed


# ---------------------------------------------------------------- 恢复
def _new_staging(root):
    base = os.path.join(root, STAGING_PREFIX + time.strftime('%Y%m%d-%H%M%S'))
    name = base
    n = 1
    while os.path.exists(name):
        name = '%s-%d' % (base, n)
        n += 1
    os.makedirs(name)
    return name


def _stage_snapshot(root, snap, man):
    """把快照文件拷进数据根下的暂存目录并逐文件校验。失败时清理暂存。"""
    staging = _new_staging(root)
    try:
        for rel in sorted(man.get('files') or {}):
            info = man['files'][rel]
            safe = _safe_rel(rel)
            if safe is None:
                raise BackupError('MANIFEST 含非法路径 %r' % (rel,))
            src = os.path.join(snap, safe.replace('/', os.sep))
            dst = os.path.join(staging, safe.replace('/', os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            actual = sha256_file(dst)
            if actual != info.get('sha256'):
                raise BackupError('暂存校验失败 %s（MANIFEST=%s 暂存=%s）'
                                  % (rel, info.get('sha256'), actual))
            if info.get('size') is not None and os.path.getsize(dst) != info.get('size'):
                raise BackupError('暂存大小不符 %s' % rel)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _replace(src, dst, tries=8):
    last = None
    for i in range(tries):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            last = e
            time.sleep(0.02 * (i + 1))
    raise BackupError('os.replace 失败 %s -> %s: %r' % (src, dst, last))


def _replace_files(root, staging, man, fail_after=None):
    """逐文件 os.replace(暂存 -> 数据根)，然后删掉快照没有的旧数据文件。

    `fail_after` 是测试钩子：在第 n 个 replace 之后抛 InjectedFailure。
    """
    replaced = 0
    files = man.get('files') or {}
    for rel in sorted(files):
        if fail_after is not None and replaced >= fail_after:
            raise InjectedFailure('YGZZ_BACKUP_FAIL_AFTER=%d 注入失败（已替换 %d 个文件）'
                                  % (fail_after, replaced))
        src = os.path.join(staging, rel.replace('/', os.sep))
        dst = os.path.join(root, rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        _replace(src, dst)
        replaced += 1
    # 删除数据根里快照没有的旧数据文件；collect() 已排除 _backups/暂存/日志/pid
    # 用 normcase 比较：Windows 大小写不敏感，避免把刚恢复的同名文件当成“快照外”删掉。
    known = set(os.path.normcase(r) for r in files)
    extras = []
    for rel, fp in sorted(collect(root).items()):
        if os.path.normcase(rel) in known:
            continue
        os.remove(fp)
        extras.append(rel)
    return replaced, extras


def _check_against_manifest(root, man):
    """逐文件核对当前数据根与 MANIFEST；返回 (bad 列表, 当前哈希 map)。"""
    files = man.get('files') or {}
    cur = collect(root)
    cur_by_norm = {os.path.normcase(k): k for k in cur}
    bad = []
    hashes = {}
    for rel in sorted(files):
        info = files[rel]
        actual_rel = cur_by_norm.get(os.path.normcase(rel))
        fp = cur.get(actual_rel) if actual_rel is not None else None
        if fp is None:
            bad.append('%s（缺失）' % rel)
            continue
        actual = sha256_file(fp)
        hashes[rel] = actual
        if actual != info.get('sha256'):
            bad.append('%s（哈希不符）' % rel)
        elif info.get('size') is not None and os.path.getsize(fp) != info.get('size'):
            bad.append('%s（大小不符）' % rel)
    known = set(os.path.normcase(r) for r in files)
    for rel in sorted(cur):
        if os.path.normcase(rel) not in known:
            bad.append('%s（快照外多余文件）' % rel)
    return bad, hashes


def _rollback(root, pre_name):
    """用 pre-restore 快照把现场逐文件换回去。返回 (成功?, 失败说明)。"""
    staging = None
    try:
        snap = _snap_path(root, pre_name)
        rc, man = verify_snapshot(root, pre_name, show=False)
        if rc != 0:
            return False, 'pre-restore 快照自身校验失败'
        staging = _stage_snapshot(root, snap, man)
        _replace_files(root, staging, man)          # 回滚不应用测试钩子
        bad, _h = _check_against_manifest(root, man)
        shutil.rmtree(staging, ignore_errors=True)
        staging = None
        if bad:
            return False, '回滚后校验失败: %s' % bad[:8]
        return True, ''
    except Exception as e:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        return False, '%s: %s' % (type(e).__name__, e)


def _fail_after_from_env():
    raw = os.environ.get('YGZZ_BACKUP_FAIL_AFTER')
    if raw in (None, ''):
        return None
    try:
        n = int(raw)
    except ValueError:
        raise BackupError('YGZZ_BACKUP_FAIL_AFTER 必须是整数（仅测试钩子）: %r' % raw)
    if n < 0:
        raise BackupError('YGZZ_BACKUP_FAIL_AFTER 不能为负: %d' % n)
    return n


def _print_hash_compare(pre_man, post_hashes):
    pre_files = pre_man.get('files') or {}
    print('\n恢复前后 sha256 对照（前 = pre-restore 现场 / 后 = 恢复后）:')
    for rel in sorted(set(pre_files) | set(post_hashes)):
        before = (pre_files.get(rel) or {}).get('sha256')
        after = post_hashes.get(rel)
        if before == after:
            tag = '一致'
        elif before is None:
            tag = '新增'
        elif after is None:
            tag = '删除'
        else:
            tag = '变更'
        print('  %-28s %s -> %s  [%s]'
              % (rel, before or '(无)', after or '(无)', tag))


def _print_summary(man, post_hashes):
    files = man.get('files') or {}
    print('\n汇总: 快照文件 %d 个 / 玩家档 %d 份；恢复后逐文件匹配 %d/%d'
          % (len(files), man.get('players', _count_players(files)),
             len(post_hashes), len(files)))
    for special in ('_accounts.json', '_auth.json', '_world.json', '_guild_txn.json'):
        if special in files:
            print('  %-16s %s' % (special, post_hashes.get(special, '(缺失)')))
    print('  consistent=%s  version=%s  快照数据根=%s'
          % (man.get('consistent'), man.get('version'), man.get('data_root')))


# ---------------------------------------------------------------- 子命令
def cmd_snapshot(root, args):
    problem = _root_problem(root)
    if problem:
        print('★ 拒绝 snapshot：%s' % problem)
        print(_root_hint(root))
        return 2
    j, jerr = _read_journal(root)
    if jerr:
        print('⚠ %s（继续，但请人工检查）' % jerr)
    if j and j.get('status') == 'started':
        pre = j.get('pre_restore') or '<pre-restore 名>'
        print('★ 上次恢复未完成（status=started，快照=%s）—— 先处理它再 snapshot。'
              % j.get('snapshot'))
        print('  python -X utf8 %s restore %s --rollback %s --data-root "%s"'
              % (os.path.basename(__file__), pre, pre, root))
        return 2
    reasons = _running_reasons(root, getattr(args, 'assume_running', False))
    if reasons and not getattr(args, 'live', False):
        print('★ 服务端可能正在运行，拒绝 snapshot（在线拷贝不是同一时点）:')
        for r in reasons:
            print('  · %s' % r)
        print('  正解：先停服（需人工确认）再 snapshot；或 --live 强制在线快照'
              '（MANIFEST 记 consistent=false，恢复时会警示）。')
        return 2
    if reasons:
        print('⚠ --live 强制在线快照；以下情况意味着快照不是同一时点:')
        for r in reasons:
            print('  · %s' % r)
    dest, man = create_snapshot(root, note=getattr(args, 'note', '') or '',
                                live=getattr(args, 'live', False))
    print('快照完成: %s' % dest)
    print('  文件 %d 个 / 玩家档 %d 份，consistent=%s，note=%r'
          % (len(man['files']), man['players'], man['consistent'], man['note']))
    for w in man.get('warnings') or []:
        print('  ⚠ %s' % w)
    print('  快照数据根: %s' % man['data_root'])
    print('  校验: python -X utf8 %s verify %s --data-root "%s"'
          % (os.path.basename(__file__), os.path.basename(dest), root))
    return 0


def cmd_list(root, args):
    snapdir = _snapdir(root)
    if not os.path.isdir(snapdir):
        print('还没有任何快照（%s）' % snapdir)
        return 0
    rows = []
    for n in os.listdir(snapdir):
        p = os.path.join(snapdir, n)
        if n.startswith('.') or not os.path.isdir(p):
            continue
        mp = os.path.join(p, MANIFEST)
        if not os.path.isfile(mp):
            rows.append((os.path.getmtime(p), n, None, None, None, None, None))
            continue
        try:
            m = _load_json(mp)
            if not isinstance(m, dict):
                raise ValueError('MANIFEST 顶层不是 object')
        except Exception:
            rows.append((os.path.getmtime(p), n, None, None, None, None, None))
            continue
        files = m.get('files') or {}
        rows.append((os.path.getmtime(p), n, m.get('version'), len(files),
                     m.get('players'), m.get('consistent'), m.get('note') or ''))
    rows.sort()
    print('  %-32s %-4s %-5s %-5s %-5s %s' % ('快照', 'ver', '文件', '玩家', '一致', 'note'))
    for _mt, n, ver, nf, np_, cons, note in rows:
        flag = 'PRE' if _is_pre_restore(n) else '   '
        con = '?' if cons is None else ('是' if cons else '否')
        print('  %s %-28s %-4s %-5s %-5s %-5s %s'
              % (flag, n, ver if ver is not None else '?',
                 nf if nf is not None else '?', np_ if np_ is not None else '?',
                 con, note))
    pres = [r for r in rows if _is_pre_restore(r[1])]
    normals = [r for r in rows if not _is_pre_restore(r[1])]
    print('共 %d 份（普通 %d 份，保留最近 %d；pre-restore %d 份，普通 prune 不删）'
          % (len(rows), len(normals), KEEP, len(pres)))
    return 0


def cmd_verify(root, args):
    rc, _man = verify_snapshot(root, args.name, show=True)
    return rc


def cmd_restore(root, args):
    # 1) 上次恢复未完成时，必须先显式确认（不能静默忽略）
    j, jerr = _read_journal(root)
    if jerr:
        print('⚠ %s' % jerr)
    if j and j.get('status') == 'started' and not (args.rollback or args.auto_rollback):
        pre = j.get('pre_restore') or '<pre-restore 名>'
        print('★ 上次恢复未完成（journal status=started，快照=%s，pre-restore=%s）。'
              % (j.get('snapshot'), pre))
        print('  先回滚，或明确指定要恢复的快照：')
        print('    python -X utf8 %s restore --auto-rollback --data-root "%s"'
              % (os.path.basename(__file__), root))
        print('    python -X utf8 %s restore %s --rollback %s --data-root "%s"'
              % (os.path.basename(__file__), pre, pre, root))
        return 2
    if j and j.get('status') == 'started' and args.rollback:
        print('ℹ 检测到上次恢复未完成；本次按 --rollback 继续（journal 会被覆盖）。')
    if j and j.get('status') == 'failed':
        if j.get('rolled_back'):
            print('ℹ 上次恢复失败但已自动回滚（快照=%s）。' % j.get('snapshot'))
        else:
            print('⚠ 上次恢复失败且自动回滚未完成；pre-restore=%s，请人工核对。'
                  % j.get('pre_restore'))

    # 2) 目标
    rollback_mode = bool(args.rollback or args.auto_rollback)
    if args.auto_rollback:
        if not j or not j.get('pre_restore'):
            print('★ --auto-rollback 需要恢复日志里的 pre_restore，但日志缺失/不完整。')
            return 2
        target = j['pre_restore']
    elif args.rollback:
        target = args.rollback
    else:
        target = args.name
    if not target:
        print('用法: restore <快照名> [--yes] [--force] 或 restore --auto-rollback')
        return 2

    # 3) 数据根自检（拒绝"明显不是账号目录"，避免对源码目录乱删）
    problem = _root_problem(root)
    if problem:
        if args.allow_empty_root and os.path.isdir(root) and _root_blank(root):
            print('⚠ 数据根是空的（%s），按 --allow-empty-root 继续恢复。' % root)
        else:
            print('★ 拒绝 restore：%s' % problem)
            print(_root_hint(root))
            print('  如果确实是数据文件被清空、只剩 _backups，请加 --allow-empty-root。')
            return 2
    if not os.path.isdir(root):
        print('★ 拒绝 restore：数据根不存在，无法在其中找到快照: %s' % root)
        return 2

    # 4) 服务端在跑：默认拒绝；--force 显式自担风险
    reasons = _running_reasons(root, getattr(args, 'assume_running', False))
    if reasons:
        if not args.force:
            print('★ 服务端可能正在运行，拒绝 restore（会把内存档与磁盘档劈成两半）:')
            for r in reasons:
                print('  · %s' % r)
            print('  正解：先停服（需人工确认）再 restore；或 --force 自担风险。')
            return 2
        msg = ('★ 危险：--force 将在服务端运行时恢复。服务端内存里的档可能在下次落盘时'
               '覆盖恢复结果；恢复后请立即停服并核对。')
        print(msg)
        print(msg, file=sys.stderr)

    # 5) 校验快照（缺文件/哈希不符立即拒绝，现场一个字节都不动）
    rc, man = verify_snapshot(root, target, show=True)
    if rc != 0:
        print('★ 快照校验失败，拒绝恢复（现场未改动）。')
        return rc
    if int(man.get('version') or 1) < MANIFEST_VERSION:
        print('⚠ 旧版 MANIFEST（version=%s）：没有 consistent/data_root 标记，按可用处理。'
              % man.get('version'))
    if man.get('consistent') is False:
        print('⚠ 该快照是**在线快照**（consistent=false），索引/玩家/世界/口令库可能不是'
              '同一时点；恢复后请人工核对。')
    snap_root = man.get('data_root')
    if snap_root and not _same_root(snap_root, root):
        print('⚠ 快照来自另一个数据根: %s（当前 %s）；跨根恢复请确认目标没错。'
              % (snap_root, root))
    for w in man.get('warnings') or []:
        print('  ⚠ 快照记录: %s' % w)
    if '_accounts.json' not in (man.get('files') or {}):
        print('⚠ 该快照不含 _accounts.json（索引）；恢复后全部玩家会重新发号，'
              '只有回滚 pre-restore 时才应继续。')
    fail_after = _fail_after_from_env()
    snap = _snap_path(root, target)

    # 6) 暂存 + 逐文件校验
    print('\n[1/4] 校验快照完成；把快照拷进数据根下的暂存目录并逐文件校验 …')
    staging = _stage_snapshot(root, snap, man)
    print('      暂存目录: %s' % staging)

    # 7) 给当前现场留底（带 MANIFEST，能再次 restore）
    print('[2/4] 给当前现场留底（pre-restore，带 MANIFEST）…')
    try:
        pre_dir, pre_man = create_snapshot(
            root, note='pre-restore for %s%s' % (target, '（在线）' if reasons else ''),
            live=bool(reasons), suffix='-pre-restore', allow_empty=True,
            require_index=False, enforce_auth=False, prune_after=False)
    except BaseException as e:
        shutil.rmtree(staging, ignore_errors=True)
        print('★ 无法创建 pre-restore 快照，恢复未开始（现场未改动）: %r' % (e,))
        return 1
    pre_name = os.path.basename(pre_dir)
    print('      pre-restore: %s（文件 %d 个）' % (pre_dir, len(pre_man['files'])))

    # 8) 恢复日志 + 逐文件原子替换
    journal = {
        'version': 1,
        'status': 'started',
        'snapshot': os.path.basename(snap),
        'pre_restore': pre_name,
        'data_root': os.path.abspath(root),
        'started': _now(),
        'ended': None,
        'files': len(man.get('files') or {}),
        'replaced': 0,
        'deleted': 0,
        'error': None,
        'rolled_back': None,
        'rollback_error': None,
        'force': bool(args.force),
        'rollback_mode': rollback_mode,
    }
    try:
        _write_journal(root, journal)
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        print('★ 恢复日志写入失败，恢复未开始（现场未改动）: %r' % (e,))
        return 1

    print('[3/4] 逐文件 os.replace 切换并删除快照外的旧数据文件 …')
    replaced = 0
    extras = []
    try:
        replaced, extras = _replace_files(root, staging, man, fail_after=fail_after)
        bad, post_hashes = _check_against_manifest(root, man)
        if bad:
            raise BackupError('恢复后逐文件校验失败: %s' % bad[:8])
    except BaseException as e:
        err = '%s: %s' % (type(e).__name__, e)
        print('\n★ 恢复失败: %s' % err, file=sys.stderr)
        print('★ 恢复失败: %s' % err)
        print('  正在从 pre-restore 自动回滚: %s' % pre_name, file=sys.stderr)
        ok, rb_err = _rollback(root, pre_name)
        journal.update({
            'status': 'failed',
            'ended': _now(),
            'replaced': replaced,
            'deleted': len(extras),
            'error': err,
            'rolled_back': bool(ok),
            'rollback_error': rb_err or None,
        })
        try:
            _write_journal(root, journal)
        except OSError as je:
            print('⚠ 恢复日志写入失败: %r' % (je,), file=sys.stderr)
        shutil.rmtree(staging, ignore_errors=True)
        if ok:
            print('  ✓ 已自动回滚，现场回到恢复前状态（逐文件哈希核对通过）。',
                  file=sys.stderr)
            print('  pre-restore 保留: %s' % pre_dir, file=sys.stderr)
            print('  可再次执行: python -X utf8 %s restore %s --data-root "%s"'
                  % (os.path.basename(__file__), pre_name, root), file=sys.stderr)
        else:
            print('  ✗ 自动回滚未完成: %s' % rb_err, file=sys.stderr)
            print('  pre-restore 保留: %s —— 请人工用该快照恢复。' % pre_dir,
                  file=sys.stderr)
        return 1

    # 9) 收尾
    print('[4/4] 恢复后逐文件校验通过 …')
    journal.update({
        'status': 'finished',
        'ended': _now(),
        'replaced': replaced,
        'deleted': len(extras),
        'error': None,
        'rolled_back': False,
        'rollback_error': None,
    })
    try:
        _write_journal(root, journal)
    except OSError as e:
        print('⚠ 恢复已完成但恢复日志写入失败: %r' % (e,), file=sys.stderr)
    shutil.rmtree(staging, ignore_errors=True)
    print('恢复完成：%d 个文件逐字节一致（删除快照外旧文件 %d 个）'
          % (len(man.get('files') or {}), len(extras)))
    _print_hash_compare(pre_man, post_hashes)
    _print_summary(man, post_hashes)
    print('\npre-restore: %s' % pre_dir)
    print('如需回退本次恢复：python -X utf8 %s restore %s --data-root "%s"'
          % (os.path.basename(__file__), pre_name, root))
    return 0


def cmd_doctor(root, args):
    print('== 数据根自检 ==')
    problem = _root_problem(root)
    files = collect(root)
    players = _count_players(files)
    isdir = os.path.isdir(root)
    print('  存在/目录: %s / %s' % ('是' if os.path.exists(root) else '否',
                                    '是' if isdir else '否'))
    print('  _accounts.json: %s' % ('有' if '_accounts.json' in files else '缺（必需）'))
    print('  玩家档 u*.json: %d 份%s' % (players, '' if players else '（必需，当前没有）'))
    for n, label in (('_world.json', '世界档'), ('_auth.json', '口令库')):
        print('  %-16s %s' % (n, '有' if n in files else '缺（%s）' % label))
    print('  %-16s %s' % ('_guild_txn.json',
                          '有（未完成妖盟事务，服务端启动时会回滚）' if '_guild_txn.json' in files
                          else '无（正常）'))
    auth_env = os.environ.get('YGZZ_AUTH') == '1'
    print('  YGZZ_AUTH=%s，auth_enabled=%s'
          % (os.environ.get('YGZZ_AUTH'), bool(auth_env or '_auth.json' in files)))
    reasons = _running_reasons(root)
    print('  服务端状态: %s' % ('检测到在运行 -> %s' % reasons if reasons
                                else '未检测到（可 snapshot）'))

    snapdir = _snapdir(root)
    snaps = []
    if os.path.isdir(snapdir):
        for n in os.listdir(snapdir):
            p = os.path.join(snapdir, n)
            if n.startswith('.') or not os.path.isdir(p):
                continue
            if os.path.isfile(os.path.join(p, MANIFEST)):
                snaps.append((os.path.getmtime(p), n, p))
    snaps.sort()
    normals = [s for s in snaps if not _is_pre_restore(s[1])]
    pres = [s for s in snaps if _is_pre_restore(s[1])]
    print('  快照: 普通 %d 份 / pre-restore %d 份（普通保留 %d，pre 至少保留 %d）'
          % (len(normals), len(pres), KEEP, PRE_RESTORE_KEEP))
    latest = snaps[-1] if snaps else None
    if latest:
        print('  最近快照: %s（%s）' % (latest[1],
                                      'pre-restore' if _is_pre_restore(latest[1]) else '普通'))
        try:
            rc, m = verify_snapshot(root, latest[1], show=False)
        except BackupError as e:
            rc, m = 2, None
            print('  ★ 最近快照校验不可用: %s' % (e,))
        if rc == 0:
            print('        校验: 通过（%d 个文件，consistent=%s）'
                  % (len(m.get('files') or {}), m.get('consistent')))
        else:
            print('        ★ 校验: 失败（rc=%d）' % rc)
    else:
        print('  最近快照: 无')

    j, jerr = _read_journal(root)
    unhealthy = False
    if jerr:
        print('  ★ 恢复日志: %s' % jerr)
        unhealthy = True
    elif j:
        print('  恢复日志: status=%s 快照=%s pre-restore=%s 开始=%s 结束=%s'
              % (j.get('status'), j.get('snapshot'), j.get('pre_restore'),
                 j.get('started'), j.get('ended')))
        if j.get('status') == 'started':
            pre = j.get('pre_restore') or '<pre-restore 名>'
            print('  ★ 上次恢复未完成！不会静默忽略。请先回滚：')
            print('    python -X utf8 %s restore --auto-rollback --data-root "%s"'
                  % (os.path.basename(__file__), root))
            print('    python -X utf8 %s restore %s --rollback %s --data-root "%s"'
                  % (os.path.basename(__file__), pre, pre, root))
            unhealthy = True
        elif j.get('status') == 'failed' and not j.get('rolled_back'):
            print('  ★ 上次恢复失败且自动回滚未完成: %s' % j.get('rollback_error'))
            print('    pre-restore 保留: %s' % (j.get('pre_restore') or '?'))
            unhealthy = True
        elif j.get('status') == 'failed':
            print('  ℹ 上次恢复失败但已自动回滚（pre-restore=%s）。' % j.get('pre_restore'))
    else:
        print('  恢复日志: 无')

    leftovers = _staging_dirs(root)
    if leftovers:
        print('  ⚠ 残留暂存目录（上次恢复中断？可人工确认后删除）: %s' % leftovers[:8])

    if problem:
        print('★ 数据根不合格: %s' % problem)
        print(_root_hint(root))
        unhealthy = True
    elif '_accounts.json' not in files:
        print('★ 缺账号索引 _accounts.json —— 恢复时所有人会重新发号。')
        unhealthy = True
    elif not players:
        print('★ 没有任何玩家档 u*.json —— snapshot 会拒绝，请确认数据根指对了。')
        unhealthy = True
    if auth_env and '_auth.json' not in files:
        print('★ YGZZ_AUTH=1 但缺口令库 _auth.json。')
        unhealthy = True
    if '_guild_txn.json' in files:
        print('  ⚠ 存在未完成的妖盟事务日志 _guild_txn.json（服务端启动时会尝试回滚）。')
    if latest and rc != 0:
        unhealthy = True
    print('结论: %s' % ('不合格，请按上面提示处理' if unhealthy else '数据根看起来可用'))
    return 1 if unhealthy else 0


def cmd_prune(root, args):
    removed_n = _prune(root)
    removed_p = _prune_pre_restore(root)
    print('普通快照: 清理 %d 份%s' % (len(removed_n), (': %s' % removed_n) if removed_n else ''))
    print('pre-restore: 清理 %d 份（至少保留最近 %d）%s'
          % (len(removed_p), PRE_RESTORE_KEEP, (': %s' % removed_p) if removed_p else ''))
    snapdir = _snapdir(root)
    if os.path.isdir(snapdir):
        names = [n for n in os.listdir(snapdir)
                 if not n.startswith('.') and os.path.isdir(os.path.join(snapdir, n))]
        print('剩余: 普通 %d 份 / pre-restore %d 份'
              % (len([n for n in names if not _is_pre_restore(n)]),
                 len([n for n in names if _is_pre_restore(n)])))
    return 0


# ---------------------------------------------------------------- CLI
def build_parser():
    ap = argparse.ArgumentParser(
        description='多人单服存档备份/恢复（数据根整体；默认 work\\accounts）')
    ap.add_argument('--data-root', default=None,
                    help='账号数据根（默认 work\\accounts；也可用环境变量 %s）' % DATA_ROOT_ENV)
    common = argparse.ArgumentParser(add_help=False)
    # 放在子命令后面也能用；SUPPRESS 保证不会把全局位置的值冲成 None
    common.add_argument('--data-root', default=argparse.SUPPRESS,
                        help='账号数据根（与全局 --data-root 等价）')
    sub = ap.add_subparsers(dest='cmd')

    p = sub.add_parser('snapshot', parents=[common], help='打一份带 MANIFEST 的快照')
    p.add_argument('--note', default='')
    p.add_argument('--live', action='store_true',
                   help='服务端在跑也强制在线快照（consistent=false）')
    p.add_argument('--assume-running', action='store_true',
                   help='测试钩子：当作服务端正在运行')
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser('list', parents=[common], help='列出快照')
    p.set_defaults(func=cmd_list)

    p = sub.add_parser('verify', parents=[common], help='逐文件核对快照 sha256')
    p.add_argument('name')
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser('restore', parents=[common], help='验证、暂存、留底、原子替换、失败回滚')
    p.add_argument('name', nargs='?', default=None)
    p.add_argument('--yes', action='store_true', help='保留兼容（当前无需交互确认）')
    p.add_argument('--force', action='store_true',
                   help='服务端在跑也恢复（危险，会打印警示）')
    p.add_argument('--rollback', default=None,
                   help='上次恢复未完成时，显式指定要回滚到的 pre-restore 名')
    p.add_argument('--auto-rollback', action='store_true',
                   help='用恢复日志里的 pre-restore 自动回滚')
    p.add_argument('--allow-empty-root', action='store_true',
                   help='数据文件被清空、只剩 _backups 时允许恢复')
    p.add_argument('--assume-running', action='store_true',
                   help='测试钩子：当作服务端正在运行')
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser('doctor', parents=[common], help='数据根/快照/恢复日志自检')
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser('prune', parents=[common], help='清理旧快照（不动最近 3 份 pre-restore）')
    p.set_defaults(func=cmd_prune)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, 'cmd', None):
        ap.print_help()
        return 1
    root = resolve_data_root(getattr(args, 'data_root', None))
    files = collect(root)
    print('数据根: %s  (文件 %d 个, 玩家档 %d 份)'
          % (root, len(files), _count_players(files)))
    if not os.path.isdir(root):
        print('  ⚠ 目录不存在（用 --data-root 指到真实数据根）')
    try:
        return args.func(root, args)
    except BackupError as e:
        print('★ %s' % e)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
