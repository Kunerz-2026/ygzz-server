# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 聊天 CHAT_TOTAL(8081) / CHAT_SEND(8080)
#   ★2026-09-12 (v26 P2) 新增实装。此前两条都未处理:
#     8081 是**服务端主动推送**的全量聊天记录 -> 未推 = 世界/私聊/妖盟三个页签永远空白;
#     8080 是发送 -> 落兜底 [1,[[]]] 时 recvMessage 只回调(不崩), 但消息丢进黑洞 = "发了没人看见"。
#
#   客户端契约 (逐行取自 client Lua):
#     NetChatTotal (app/network/chat/NetChatTotal.lua:65~152)
#       sendMessage(_callFunc): 请求体**为空** -> 服务器回全量记录 + 之后由服务器**主动推**增量
#       recvMessage:
#         msg = msg[1]                       ← ★再取一层: 真正的是 resp[1][1]
#         for i=1,#msg do                    ← 必须是"行的数组"
#           行 9 字段: [1]chattype [2]content [3]sendname [4]sendlevel
#                      [5]recvname [6]recvlevel [7]time [8]headID [9]headBGID
#         CHAT_TYPE = {WORLD=1, PRIVATE=2, SYSTEM=4, UNION=5, UNION_SYSTEM=6}
#         ⚠ 客户端 if/elseif 只处理 1/2/4/5/6 —— **chattype==3 会被完全丢弃** (别用 3)
#         ⚠ SYSTEM(4) 会被强制改名 "系統" 且塞进 worldMsg + sysMsg 两个列表
#     NetChatSend (app/network/chat/NetChatSend.lua:23~45)
#       sendMessage(_talktype,_content,_torole): msg={talktype, content, torole}
#       recvMessage: 只回调; 错误码 0/90 参数错误/91 禁言/92 CD中/93 找不到对象/94 离线/95 重复刷屏/97 等级不足
#     ⚠ ChatWorld.lua:67 与 ChatUnion.lua:62 只传 (chattype, content), ChatPrivate.lua:94 传 (2,content,friendName)
#       —— 所以 torole 对世界/妖盟为空, 参数校验不能强制要求 torole。
#
#   单档口径（多账号另走 world['chat_log']，按受众过滤后下发）:
#     · 聊天记录持久在 state['chat_log'] (最多保留最近 200 条, 越界丢最旧)
#     · 8081: 回全量 (客户端 setWorldMsg 之外还会 append; 注意 NetChatTotal 每次 recv 都会 append,
#             所以**不要**在没有 setWorldMsg 的情况下重复推全量 —— 见 §5 坑点 #41)
#     · 8080: 校验 -> 落库 -> 回 ack -> **主动推 8081 增量** (让玩家立刻在屏幕上看到自己那条)
#             并把消息回显成一个"系统/机器人回应"? 不 —— 私服无其他玩家, 只回显自己那条。
import contextvars
import json, time
from state import EQ_STATE, save_state
import accounts

CHAT_TYPE_WORLD = 1
CHAT_TYPE_PRIVATE = 2
CHAT_TYPE_SYSTEM = 4
CHAT_TYPE_UNION = 5
CHAT_TYPE_UNION_SYSTEM = 6

CHAT_LOG_KEEP = 200      # 服务端最多保留多少条 (客户端每个列表自己还有 50 条上限)
CHAT_CD_SEC = 1          # 同一条消息 1 秒内重复 -> 95 "禁止频繁重复发送消息"
CHAT_MIN_LEVEL = 1       # 世界频道最低等级 (客户端有 97 码; 私服默认放开)

# 多账号推送绑定到本次 8080 请求。LE 路径在 handler 返回后会释放请求锁，
# 再读 world['chat_log'][-1] 可能读到另一人的消息。
_delivery = contextvars.ContextVar('chat_delivery', default=None)
_recent_by_account = {}    # 仅作 1 秒重复检查；聊天真值在 world['chat_log']


def _multi_registry():
    return accounts.REGISTRY if accounts.ENABLED else None


def _current_account():
    fn = getattr(accounts.EQ_STATE, 'current', None)
    return fn() if callable(fn) else None


def _log():
    v = EQ_STATE.setdefault('chat_log', [])
    if not isinstance(v, list):
        v = []
        EQ_STATE['chat_log'] = v
    return v


def _role_name():
    head = EQ_STATE.get('head') or {}
    return str(EQ_STATE.get('role_name') or '测试')


def _head_ids():
    head = EQ_STATE.get('head') or {}
    return int(head.get('eq', 1001) or 1001), int(head.get('frame_eq', 1001) or 1001)


def _make_row(chattype, content, sendname, recvname='', time_ms=None, level=None):
    h, hb = _head_ids()
    if level is None:
        level = int(EQ_STATE.get('player_lv', 1))
    if time_ms is None:
        time_ms = int(time.time() * 1000)
    return [int(chattype), str(content), str(sendname), int(level),
            str(recvname or ''), 0, int(time_ms), h, hb]


def chat_total_json():
    # 8081 响应体: [1,[[行 x N]]]  —— 外层 msg[1] 再由客户端 `msg = msg[1]` 取一次
    if accounts.ENABLED:
        reg = _multi_registry()
        acc = _current_account()
        if reg is None or acc is None:
            return b'[0]'
        try:
            log = _visible_rows(reg, acc)
        except Exception as exc:
            print('CHAT 8081 visibility failed: %r' % (exc,))
            return b'[0]'
    else:
        log = list(_log())
    return ('[1,' + json.dumps([log], separators=(',', ':')) + ']').encode('utf-8')


def _valid_row(row):
    return isinstance(row, list) and len(row) >= 9 and row[0] in (
        CHAT_TYPE_WORLD, CHAT_TYPE_PRIVATE, CHAT_TYPE_SYSTEM,
        CHAT_TYPE_UNION, CHAT_TYPE_UNION_SYSTEM)


def _visible_rows(reg, acc):
    """只把当前账号有权看见的九字段行交给 Lua；元数据不进协议。"""
    import guild_world

    gid, _ = guild_world.guild_of(acc, reg)
    out = []
    for entry in reg.world.get('chat_log', []):
        if isinstance(entry, list):
            # 旧 world 裸行没有受众标识：只有世界/系统两种可安全公开。
            if _valid_row(entry) and entry[0] in (CHAT_TYPE_WORLD, CHAT_TYPE_SYSTEM):
                out.append(list(entry[:9]))
            continue
        if not isinstance(entry, dict):
            continue
        row = entry.get('row')
        if not _valid_row(row):
            continue
        scope = entry.get('scope')
        if scope == 'world' and row[0] in (CHAT_TYPE_WORLD, CHAT_TYPE_SYSTEM):
            out.append(list(row[:9]))
        elif scope == 'private' and row[0] == CHAT_TYPE_PRIVATE:
            if acc.id in (entry.get('sender_aid'), entry.get('recipient_aid')):
                out.append(list(row[:9]))
        elif scope == 'guild' and row[0] in (CHAT_TYPE_UNION, CHAT_TYPE_UNION_SYSTEM):
            if gid and gid == entry.get('guild_id'):
                out.append(list(row[:9]))
    return out


def handle_chat_total(body_json=None):
    # 8081 CHAT_TOTAL: 回全量聊天记录 (客户端首开三个页签时拉一次)
    if accounts.ENABLED:
        reg = _multi_registry()
        if reg is None or _current_account() is None:
            return b'[0]'
        log = reg.world['chat_log']
        if not log:
            row = _make_row(CHAT_TYPE_SYSTEM, '欢迎来到妖怪世界！', '系統', '系統')
            try:
                reg.world_chat_append({'row': row, 'scope': 'world'})
                reg.save_world()
            except Exception as exc:
                log.clear()
                print('CHAT 8081 world save failed: %r' % (exc,))
                return b'[0]'
        return chat_total_json()
    log = _log()
    if not log:
        # 首次进游戏: 造一条系统欢迎语 (chattype=4 -> 客户端会同时进 worldMsg 与 sysMsg)
        log.append(_make_row(CHAT_TYPE_SYSTEM, '欢迎来到妖怪世界！', '系統', '系統'))
        save_state()
    return chat_total_json()


def _validate(talktype, content, torole, log=None):
    if talktype not in (CHAT_TYPE_WORLD, CHAT_TYPE_PRIVATE, CHAT_TYPE_UNION):
        return 90                                   # 参数错误 (只允许发世界/私聊/妖盟)
    if not content or not str(content).strip():
        return 90
    if talktype == CHAT_TYPE_PRIVATE and not torole:
        return 93                                   # 找不到当前聊天对象用户
    if int(EQ_STATE.get('player_lv', 1)) < CHAT_MIN_LEVEL:
        return 97                                   # 玩家等级不足
    if log is None:
        log = _log()
    if log:
        last = log[-1]
        try:
            if (str(last[2]) == _role_name() and str(last[1]) == str(content)
                    and int(time.time() * 1000) - int(last[6]) < CHAT_CD_SEC * 1000):
                return 95                           # 禁止频繁重复发送消息
        except Exception:
            pass
    return 0


def _recipient_for_name(reg, name):
    # 协议只有角色名，缺少稳定 id；重名时不能猜一个账号发送。
    # 严格遍历：坏账号档不能被静默跳过，否则本来重名的收件人会被误判为唯一。
    matches = [a for a in reg.all_accounts_strict(require_index=True)
               if str((a.data or {}).get('role_name') or '') == name]
    return matches[0] if len(matches) == 1 else None


def _multi_send(reg, acc, talktype, content, torole):
    code = _validate(talktype, content, torole, log=[])
    if code:
        return ('[%d]' % code).encode('utf-8')
    now = int(time.time() * 1000)
    prev = _recent_by_account.get(acc.id)
    if prev and prev[0] == content and now - prev[1] < CHAT_CD_SEC * 1000:
        return b'[95]'

    row = _make_row(talktype, content, _role_name(), torole, time_ms=now)
    if talktype == CHAT_TYPE_WORLD:
        entry = {'row': row, 'scope': 'world', 'sender_aid': acc.id}
        targets = set(reg.online.copy()) | {acc.id}
    elif talktype == CHAT_TYPE_PRIVATE:
        recipient = _recipient_for_name(reg, torole)
        if recipient is None or recipient.id == acc.id:
            return b'[93]'
        if recipient.id not in reg.online:
            return b'[94]'
        entry = {'row': row, 'scope': 'private', 'sender_aid': acc.id,
                 'recipient_aid': recipient.id}
        targets = {acc.id, recipient.id}
    else:
        import guild_world
        gid, guild = guild_world.my_guild(acc, reg)
        if not gid or not guild:
            return b'[90]'
        entry = {'row': row, 'scope': 'guild', 'guild_id': gid,
                 'sender_aid': acc.id}
        targets = set(guild_world.member_aids(guild)) & set(reg.online.copy())
        targets.add(acc.id)

    log = reg.world['chat_log']
    before = list(log)
    try:
        reg.world_chat_append(entry)
        reg.save_world()
    except Exception as exc:
        log[:] = before
        print('CHAT 8080 world save failed: %r' % (exc,))
        return b'[0]'
    _recent_by_account[acc.id] = (content, now)
    payload = ('[1,' + json.dumps([[row]], separators=(',', ':')) + ']').encode('utf-8')
    _delivery.set((payload, tuple(sorted(targets))))
    return b'[1,[]]'


def handle_chat_send(body_json):
    # 8080 CHAT_SEND: req {talktype, content, torole}
    #   recv: 只回调 (不读 msg[N]) -> 回 [1,[]] 即可; 新消息靠**主动推 8081**
    _delivery.set(None)
    if accounts.ENABLED and (_multi_registry() is None or _current_account() is None):
        return b'[0]'
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        talktype = int(req.get('talktype') or 0)
    except Exception:
        talktype = 0
    content = str(req.get('content') or '')
    torole = str(req.get('torole') or '')
    if accounts.ENABLED:
        try:
            return _multi_send(_multi_registry(), _current_account(), talktype, content, torole)
        except Exception as exc:
            print('CHAT 8080 multi send failed: %r' % (exc,))
            return b'[0]'
    code = _validate(talktype, content, torole)
    if code:
        return ('[%d]' % code).encode('utf-8')
    log = _log()
    row = _make_row(talktype, content, _role_name(), torole)
    log.append(row)
    if len(log) > CHAT_LOG_KEEP:
        del log[:len(log) - CHAT_LOG_KEEP]
    EQ_STATE['chat_last_ts'] = int(row[6])
    save_state()
    return b'[1,[]]'


def chat_push_frame_payload():
    # 供 game_server 在 8080 之后**主动推 8081** 用的载荷 (只推最后一条, 客户端 append 语义)
    if accounts.ENABLED:
        d = _delivery.get()
        return d[0] if d else None
    log = _log()
    if not log:
        return None
    return ('[1,' + json.dumps([[list(log[-1])]], separators=(',', ':')) + ']').encode('utf-8')


def chat_push_targets():
    """多账号下本次 8080 的受众账号 id；失败或单档返回空元组。"""
    d = _delivery.get() if accounts.ENABLED else None
    return d[1] if d else ()


def chat_delivery():
    """本次成功发送的 (8081 增量载荷, 目标账号 ids)。"""
    return _delivery.get() if accounts.ENABLED else None


# ★别名: handlers_profile 里已有 `handle_chat_send/handle_chat_total` 同名冲突风险,
#   game_server 用 * 导入本模块; 同时 _contract_ironlaws_test 会用 `from handlers_chat import *`
#   再把 GAME_HANDLERS 的源码 exec 一遍 —— 所以这里必须导出与 GAME_HANDLERS 里写的**同名**符号。
handle_chat_send_msg = handle_chat_send
handle_chat_total_msg = handle_chat_total
