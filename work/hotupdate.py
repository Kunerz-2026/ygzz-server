# -*- coding: utf-8 -*-
"""hotupdate.py —— 客户端**自带**热更通道（(b)，`cc.AssetsManagerEx`）的服务端一半。

## 它解决的是什么
(任务书 §2 任务 A。) 客户端 `main.lua:27 -> UpdateScene -> EightWordsSdk -> BundleUpdate:enterUpdate()`
本来会走 `AssetsManager:startUpdate()`，从 `<host>/project.manifest` 拉清单再下资源。
目标客户端里这条路的**入口被关死了**（`BundleUpdate.lua:16 = if false`，见 `_hotupdate_build.py` 抬头），
所以"服务端 assets 是空对象"从来不是卡点；本文件负责把**另一半**做对：

1. 从分发树 `ygzz\\hotfix_src\\` 逐文件算 md5，生成**真清单**（不再返回空的 `assets`）；
2. `engineVersion` / `version` 有确定来源，且护栏会断言它们与目标客户端一致；
3. 提供 `/hotfix/<相对路径>` 下发资源文件，越界即拒（与 CDN 同一个判据）。

## 三个必须对齐的常量（护栏 `_hotupdate_manifest_test.py` 逐条断言）
| 量 | 值 | 不对齐的后果 |
|---|---|---|
| `engineVersion` | `internal_600`（= 目标客户端 `assets/src/config.lua:32`） | `AssetsManagerEx` 判成"引擎版本不同" -> 走 `UPDATE_ENGINE` 去 openURL 下新客户端 |
| `version` | `hotfix_src/RELEASE.json` 的 `version` | 内容变了版本没变 -> 客户端永远认为"已是最新"，改动永远下不去 |
| `packageUrl` | `http_base() + '/hotfix/'` | 资源 404 -> `ERROR_UPDATING` -> 重试 4 次后 `updateError()`（**无限重启**）|

## ⚠ 自举（第一次为什么还需要别的手段）
`AssetsManager:getManifestPath()` 首选 `<storage>/project.manifest`，没有才回落到
**目标客户端里的** `assets/project.manifest` —— 那份的 `remoteManifestUrl` 指向**已死的CDN**
（`原 CDN 域名`，DNS 都不解析）。所以"第一次"必须由客户端先把引导清单写进
`<storage>/project.manifest`：这正是 `_hotupdate_build.py` 补丁 P2 干的事。
**⇒ (b) 不是"零客户端改动"的通道**；它需要一次性把 P2 那份 Lua 送进去（重打包 或 每个设备推一次），
之后才是自维持的（`AssetsManagerEx` 每次成功后会自己改写那份清单）。
"""
import hashlib
import json
import os

BASE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(BASE)                                   # <repo>\ygzz
HOTFIX_ROOT = os.environ.get('YGZZ_HOTFIX_ROOT') or os.path.join(REPO, 'hotfix_src')
RELEASE_FILE = os.path.join(HOTFIX_ROOT, 'RELEASE.json')

# ★ 必须 == 目标客户端 assets/src/config.lua:32（实测 internal_600）。护栏逐条断言。
ENGINE_VERSION = 'internal_600'
URL_PREFIX = '/hotfix/'
ASSET_DIR = 'src'          # 分发树里参与下发的子目录；相对路径就是客户端里的路径


def release():
    """读 RELEASE.json；缺失/损坏时返回一个**显眼的**兜底（version=0.0.0 会被护栏抓住）。"""
    try:
        with open(RELEASE_FILE, encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict) or not d.get('version'):
            raise ValueError('RELEASE.json 没有 version')
        return d
    except Exception as e:
        _warn_once('release() 读不到 %s: %r' % (RELEASE_FILE, e))
        return {'version': '0.0.0', 'error': repr(e)}


_WARNED = set()


def _warn_once(msg):
    """★2026-10-02: 分发树缺失时**必须留下痕迹**。

    为什么不能静默: `HOTFIX_ROOT` 是从本文件位置推出来的（`work/` 的上一级）。
    凡是"把服务端拷进私有布局"的测试（`_b4` / `_sec_hardening_test`）都会因此找不到
    `hotfix_src/`，于是 `release()` 兜底成 `0.0.0`、`assets()` 兜底成 `{}` ——
    那正好是**旧实现那个"看起来正常、其实什么都没下发"的形态**（客户端会一直认为已是最新）。
    实测被 `_b4` 当场抓住（`manifest.version '0.0.0' vs '1.0.0'`、`assets 0 条 vs 3 条`）。
    所以这里向 stderr 喊一声（落 `server-err.log`），并支持 `YGZZ_HOTFIX_ROOT` 显式指定。
    """
    if msg in _WARNED:
        return
    _WARNED.add(msg)
    import sys as _s
    try:
        _s.stderr.write('[HOTUPDATE] ★ %s\n' % msg)
        _s.stderr.write('[HOTUPDATE]   HOTFIX_ROOT=%s (存在=%s)\n'
                        % (HOTFIX_ROOT, os.path.isdir(HOTFIX_ROOT)))
        _s.stderr.flush()
    except Exception:
        pass


def _walk_assets():
    """遍历分发树里的资源文件，返回 [(relpath(用 /), 绝对路径)]，按 relpath 排序。"""
    out = []
    root = os.path.join(HOTFIX_ROOT, ASSET_DIR)
    if not os.path.isdir(HOTFIX_ROOT):
        _warn_once('分发树不存在，本次下发会是空清单')
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, HOTFIX_ROOT).replace('\\', '/')
            out.append((rel, fp))
    out.sort()
    return out


def assets():
    """{相对路径: {'md5':…, 'size':…, 'compressed': False}} —— AssetsManagerEx 的 assets 段。"""
    d = {}
    for rel, fp in _walk_assets():
        with open(fp, 'rb') as f:
            data = f.read()
        d[rel] = {'md5': hashlib.md5(data).hexdigest(), 'size': len(data),
                  'compressed': False}
    return d


def tree_sha():
    """与 `_hotupdate_build.py` 同算法的内容哈希（护栏用它判"内容变了没升版本"）。"""
    h = hashlib.sha256()
    for rel, info in sorted(assets().items()):
        h.update(('%s|%s|%d\n' % (rel, info['md5'], info['size'])).encode('utf-8'))
    return h.hexdigest()


def manifest(base_url):
    """`/project.manifest` 的响应体。"""
    base = base_url.rstrip('/')
    return {
        'packageUrl': base + URL_PREFIX,
        'remoteVersionUrl': base + '/version.manifest',
        'remoteManifestUrl': base + '/project.manifest',
        'version': release()['version'],
        'engineVersion': ENGINE_VERSION,
        'buildVersion': release()['version'],
        'assets': assets(),
        'groupVersions': {},
        'searchPaths': [],
    }


def version_manifest(base_url):
    """`/version.manifest` —— 只有版本相关字段，客户端拿它做快速比对。"""
    base = base_url.rstrip('/')
    return {
        'packageUrl': base + URL_PREFIX,
        'remoteVersionUrl': base + '/version.manifest',
        'remoteManifestUrl': base + '/project.manifest',
        'version': release()['version'],
        'engineVersion': ENGINE_VERSION,
        'buildVersion': release()['version'],
    }


def resolve(rel):
    """把 `/hotfix/<rel>` 的 rel 解析成分发树内的绝对路径；越界/不存在一律 None。

    ★ 判据与 CDN 那段**同一条**（2026-10-02 审计 §7 的任意文件读修复）：
      先 `normpath` 再要求结果仍在 root 之内，且用 `root + sep` 而不是裸前缀
      （否则 `...hotfix_src-evil` 会因前缀匹配漏过）。
      这里**不做** `replace('..','')`：normpath + 前缀判定已经严格正确，
      而 replace 会把合法文件名里的 `..` 悄悄改掉（例如 `a..b.lua`）。
    """
    root = os.path.normpath(HOTFIX_ROOT)
    fp = os.path.normpath(os.path.join(root, str(rel).lstrip('/').replace('\\', '/')))
    if fp != root and not fp.startswith(root + os.sep):
        return None
    if not os.path.isfile(fp):
        return None
    return fp


def content_type(fp):
    return 'text/plain; charset=utf-8' if fp.endswith('.lua') else 'application/octet-stream'


if __name__ == '__main__':
    import io
    import sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    r = release()
    print('hotfix_root : %s' % HOTFIX_ROOT)
    print('release     : version=%s tree_sha=%s' % (r.get('version'), r.get('tree_sha')))
    print('tree_sha()  : %s' % tree_sha())
    print('engine      : %s' % ENGINE_VERSION)
    a = assets()
    print('assets      : %d 条' % len(a))
    for k in sorted(a):
        print('   %-52s %8d  %s' % (k, a[k]['size'], a[k]['md5']))
    print('\nmanifest(192.0.2.10:18080) =')
    print(json.dumps(manifest('http://192.0.2.10:18080'), ensure_ascii=False, indent=1)[:900])
