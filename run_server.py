# -*- coding: utf-8 -*-
"""run_server.py —— Yokai: Spirits Hunt (妖怪正传) · 便携服务端启动器

做五件事：
  1. 读 config.ini（端口 / 邀请码 / 手动指定 IP）；
  2. 自动挑一个本机局域网 IPv4（WiFi / 热点 / ZeroTier / Tailscale 都能用）；
  3. 检查 18080 / 27001 / 27002 是否被占用（占用就停，绝不抢端口）；
  4. 写 `使用说明.txt`（带本次实际 IP，给玩家照着填）；
  5. 前台起服务：`work/game_server.py`，数据在 `work/state.json` + `work/accounts/`。

想停止：在这个窗口按 Ctrl+C（或直接关窗口）。
"""
import configparser
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.path.join(HERE, 'work')
CFG_PATH = os.path.join(HERE, 'config.ini')
GUIDE = os.path.join(HERE, '使用说明.txt')

DEFAULTS = {
    'http_port': '18080',
    'game_port': '27001',
    'game_port2': '27002',
    'invite_code': '',
    # 2026-11-07: 发布默认关闭游客登录（客户端改为账号+密码注册/登录）。
    # 旧游客号用 `_migrate_guest_accounts.py` 转成可认领。
    'guest_login': '0',
    'advertise_ip': 'auto',   # auto 或手动填一个 IPv4
    'report_contact': '',     # 免责声明里的举报联系方式
}


def log(msg):
    print(msg, flush=True)


def load_cfg():
    cfg = configparser.ConfigParser()
    cfg['server'] = dict(DEFAULTS)
    if os.path.isfile(CFG_PATH):
        cfg.read(CFG_PATH, encoding='utf-8-sig')
        if 'server' not in cfg:
            cfg['server'] = dict(DEFAULTS)
        for k, v in DEFAULTS.items():
            if k not in cfg['server']:
                cfg['server'][k] = v
    return cfg['server']


def is_private(ip):
    o = ip.split('.')
    if len(o) != 4:
        return False
    try:
        a, b = int(o[0]), int(o[1])
    except ValueError:
        return False
    return (a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
            or (a == 100 and 64 <= b <= 127))   # 100.64/10 = ZeroTier/Tailscale 常用段


def default_route_ip():
    """默认路由出口 IP（UDP connect 不实际发包）；失败返回 ''。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1.0)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith('127.'):
            return ip
    except Exception:
        pass
    return ''


def all_ips():
    """所有候选 IPv4，默认路由排第一（避免 VMware/Hyper-V/ZeroTier 虚拟网卡被选中）。"""
    cands = []
    ip = default_route_ip()
    if ip:
        cands.append(ip)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and ip not in cands:
                cands.append(ip)
    except Exception:
        pass
    return cands


def pick_ip():
    cands = all_ips()
    priv = [ip for ip in cands if is_private(ip)]
    return (priv or cands or ['127.0.0.1'])[0]


def port_free(p):
    s = socket.socket()
    try:
        s.settimeout(0.4)
        return s.connect_ex(('127.0.0.1', p)) != 0
    finally:
        s.close()


def write_guide(ip, ports, invite, cands=None):
    lines = [
        'Yokai: Spirits Hunt (妖怪正传) · 使用说明',
        '=' * 40,
        '',
        '1) 本机开服已启动，服务器地址：',
        '   http://%s:%s' % (ip, ports[0]),
    ]
    others = [x for x in (cands or []) if x != ip and is_private(x)]
    if others:
        lines.append('')
        lines.append('   其他网卡地址（自动选错时手动填）：')
        for x in others:
            lines.append('     http://%s:%s' % (x, ports[0]))
    lines += [
        '',
        '2) 手机/模拟器与本机连同一个 WiFi/热点后，打开游戏：',
        '   · 客户端里已保存地址时通常不用填；否则必须填下面这一行；',
        '   · 模拟器一般会自动填宿主地址；真机必须整行填写下面这一行。',
        '   · 真机把 http://%s:%s 整行填进「服务器地址」（含 http:// 和 :18080）。' % (ip, ports[0]),
        '   首次打开用「账号 + 密码」注册；之后会记住地址并自动登录。',
        '',
        '3) 账号名就是角色名（2-24 位字母/数字/汉字）；密码至少 6 位。',
        '   注册成功会给一个 6 位「重置码」，忘记密码时用它重置。',
    ]
    if invite:
        lines.append('   邀请码：%s' % invite)
    else:
        lines.append('   邀请码：无（本服没开邀请码限制，第 3 个框留空）')
    lines += [
        '',
        '4) 日志：work/logs/ ；账号数据：work/accounts/',
        '5) 备份：双击 备份.bat；恢复：备份.bat 里按提示执行',
        '6) 防火墙：首次开服请双击 允许防火墙.bat（需要管理员）',
        '',
        '换 IP（换 WiFi/热点）后：重启本程序；',
        '让玩家在游戏「服务器地址」里改成新的 http://<电脑IP>:18080。',
        '',
    ]
    with open(GUIDE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    return GUIDE


def main():
    cfg = load_cfg()
    ports = [int(cfg['http_port']), int(cfg['game_port']), int(cfg['game_port2'])]
    busy = [p for p in ports if not port_free(p)]
    if busy:
        log('★ 端口被占用: %s —— 请先关掉占用程序（或改 config.ini 的端口）。' % busy)
        return 2
    ip = cfg['advertise_ip'].strip()
    if not ip or ip.lower() == 'auto':
        ip = pick_ip()
    cands = all_ips()
    invite = (cfg['invite_code'] or '').strip()

    env = dict(os.environ)
    env.update({
        'YGZZ_AUTH': '1',
        'YGZZ_MULTI_ACCOUNT': '1',
        'YGZZ_PUBLIC': '1',
        'YGZZ_REQUIRE_NOTICE': '1',
        'YGZZ_NEW_PLAYER': 'starter',   # 新号走初始档（1 级 / 0 金 / 0 抽）；测试档只在裸 game_server
        'YGZZ_BIND_HOST': '0.0.0.0',
        'YGZZ_ADVERTISE_HOST': ip,
        'YGZZ_SOCK_PORT': str(ports[1]),
        'YGZZ_CDN_PORT': str(ports[0]),
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONDONTWRITEBYTECODE': '1',
    })
    if invite:
        env['YGZZ_INVITE_CODE'] = invite
    if (cfg.get('guest_login') or '1').strip().lower() in ('1', 'true', 'yes', 'on'):
        env['YGZZ_GUEST_LOGIN'] = '1'
    _contact = (cfg.get('report_contact') or '').strip()
    if _contact:
        env['YGZZ_REPORT_CONTACT'] = _contact
    # 客户端资源（可选；没有就不下发清单，热更本来就是关闭的）
    ext = os.path.join(HERE, 'extracted')
    if os.path.isdir(ext):
        env['YGZZ_ASSETS'] = ext

    log('=' * 56)
    log(' Yokai: Spirits Hunt (妖怪正传) · 便携服务端')
    log('=' * 56)
    log(' 服务器地址（客户端整行填写）:')
    log('    http://%s:%d' % (ip, ports[0]))
    _others = [x for x in cands if x != ip and is_private(x)]
    if _others:
        log(' 本机其他候选地址（自动选错时改 config.ini 的 advertise_ip，或让玩家手动填）:')
        for x in _others:
            log('    http://%s:%d' % (x, ports[0]))
    log(' 游戏端口: %d / %d    HTTP: %d' % (ports[1], ports[2], ports[0]))
    log(' 客户端: 在服务器地址框整行填上面的 http://地址:端口。')
    log('         换 WiFi/IP 后重启服务端，让玩家在游戏里改成新地址。')
    log('         真机用上面打印的 http://%s:%d' % (ip, ports[0]))
    log(' 邀请码: %s' % (invite or '（未设置，留空即可）'))
    log(' 后台管理: http://127.0.0.1:%d/admin（本机打开，自动带 token）' % ports[0])
    log('           18080 监听 0.0.0.0；局域网内知道 token 的人也能访问。')
    log('           token 见 work/admin_token.txt')
    log(' 数据目录: %s' % os.path.join(WORK, 'accounts'))
    log(' 日志目录: %s' % os.path.join(WORK, 'logs'))
    log('=' * 56)
    g = write_guide(ip, ports, invite, cands)
    log(' 已写说明: %s' % g)
    log(' 按 Ctrl+C 停止服务。')
    log('=' * 56)

    py = sys.executable
    try:
        p = subprocess.Popen([py, '-X', 'utf8', '-u', 'game_server.py'], cwd=WORK, env=env)
    except Exception as e:
        log('★ 启动失败: %r' % (e,))
        return 1
    try:
        while True:
            rc = p.poll()
            if rc is not None:
                log('服务进程已退出，返回码 %s' % rc)
                return rc
            time.sleep(0.5)
    except KeyboardInterrupt:
        log('\n正在停止…')
        p.terminate()
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
        return 0


if __name__ == '__main__':
    sys.exit(main())
