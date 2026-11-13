# -*- coding: utf-8 -*-
"""handlers_point.py —— 小红点链路 105 / 101 / 102 / 104 / 1053 / 1054 / 8082 (审计 §12.1 / §13 排名 8)。

**修前的缺口 (实测)**
  `SuccessEnterGame.lua:38-41` 每次进游戏**无条件按序发** 105 / 101 / 102 / 104:
      SocketManager:getMe():sendMessage(MSG_ID.PUSH_POINT)          -- 105
      SocketManager:getMe():sendMessage(MSG_ID.PUSH_SEVENREWARD)    -- 101
      SocketManager:getMe():sendMessage(MSG_ID.PUSH_TIPS)           -- 102
      SocketManager:getMe():sendMessage(MSG_ID.PUSH_FRIEND)         -- 104
  四个都落进通用兜底 `[1,[[]]]`。而 `server_debug.log:8101-8104` 里这四个号各出现 **71 次**
  —— 也就是说这条链路一直被请求、一直被空回。**四个号的应答体客户端都不读**
  (`NetPushTrackPoint` / `NetPushLoginReward` / `NetPushTips` / `NetPushFriend` 的 `recvMessage`
  只判 statecode), 所以"补应答"没有意义 —— **要亮灯必须服务端主动推**:
    · **8082 ATTENTIONPUSH**(`ConfigNet.lua:148` 小红点推送) —— 主界面按钮上的红点;
    · **1053 POINT_RECEIVE**(`ConfigNet.lua:217` 引导小红点推送) —— 子菜单红点。

**客户端的硬契约 (逐字段反推)**
  · 8082 `NetAttentionPush.recvMessage` -> `[1, [[type, subtype, subid, state], ...]]`
    (`NetAttentionPush.lua:182-190`); `getState(type)` 只要有**任意一条** `type` 命中且 `state`
    为真就返回 true(`:71-91`)。`VMainButton.lua:1052` 用 `ATTENTIONPUSH_TYPE.SEVENLOGIN(=2)`
    点七日登录红点、`:1060` 用 `ACHIVEMENT(=6)`、`:1064` 用 `MAIL(=7)`、`:1186` 用 `POWER(=10)`。
    ⚠ `msg[1]` 必须是**表** —— 给 nil 会让 `pairs(nil)` 直接崩(即 `config_data.py` 里记载的旧事故)。
  · 1053 `NetPointReveice.recvMessage` -> `self.pointList = msg[1]` -> `PointManager:updateList()`
    -> 每条 `[groupID, hint1, hint2, hint3, state]`(`PointManager.lua:69-77`);
    `getPointState(id)` 在 `id` 命中 hint1/2/3 时返回 `state`(`:81-94`)。
  · 1054 `NetPointUpdate` —— 客户端点掉红点后**主动发** `groupID`(`PointManager.lua:139`),
    服务端必须应答, 否则该按钮会一直"点不掉"。
    ⚠ **1054 不在这里实现**: `handlers_batch.py:114` 已有一份(且 `_v30d_selftest.py:57-58`
      钉住了它的 `[1,[]]` / `[0,{}]` 行为)。本轮**不重定义**它 —— 同名重复定义会被
      `game_server` 的 import 顺序静默换实现(审计 §12.4 记的正是这类事故)。

**点的取数口径 (只认真实状态; 没有数据源一律不亮)**
  每个红点都给一个"能不能领/有没有新东西"的**真判据**, 判据取不到就**不亮** ——
  铁律「没有真实数据源必须返回 0」。绝不"为了好看"点亮。
"""
import json
import time

from state import EQ_STATE

# ---- 8082 ATTENTIONPUSH_TYPE (NetAttentionPush.lua:4-23 逐字) ----
T_ACTIVITY = 1
T_SEVENLOGIN = 2
T_TEAM = 3
T_CARD = 4
T_PVE = 5
T_ACHIVEMENT = 6
T_MAIL = 7
T_STORY = 8
T_DAILTASK = 9
T_POWER = 10
T_UNOIN = 11
T_OTHER = 12
T_LOTTERY = 13
T_EVERYDAYLOGIN = 14

# ---- 1053 POINT_GUIDE (PointManager.lua:12-44 摘用到的几个) ----
P_TEAM = 1
P_CARD = 2
P_PVE = 3
P_EXPLORE = 4
P_PVP = 5
P_DALAYTASK = 6
P_SOCIATY = 7
P_POWER = 10

# ---- 妖盟子类型（`NetAttentionPush.lua:34-43` 逐字）----
#   ⚠ 这三个常量是 **8082 的第二个/第三个槽**，不是新的 type：
#     `VSociatyInformation:setSignRedDotState()` 查的是
#     `getState(UNOIN, UNOIN_SUB_TYPE.MESSAGE, UNOIN_MESSAGE_TYPE.SIGN)`。
U_SUB_MESSAGE = 3      # UNOIN_SUB_TYPE.MESSAGE
U_MSG_SIGN = 1         # UNOIN_MESSAGE_TYPE.SIGN    妖盟签到
U_MSG_REDBAG = 2       # UNOIN_MESSAGE_TYPE.REDBAG  妖盟红包

# ⚠★ 「只含真实为真的项」这条原则对 **UNOIN 不适用**，必须写成**带显式 bool 的全量条目**：
#   `NetAttentionPush:updateState` 只在"同 (type,subtype,subid)"时**原地刷新**，
#   新列表里**缺席**的条目**不会**被清掉 ⇒ 只要推过一次 True，之后不再推就等于永远 True。
#   症状正是妖盟面板上那个擦不掉的红点（以及"签到按钮一直是亮的、点了提示已签到"）。
#   ⇒ 妖盟这两条**每次推送都带上**，state 用真实布尔值（没有会时也带上 False）。


# ---------------------------------------------------------------- 判据
# ★★ 每个判据都接受一个可选的 `acc`：
#   `acc is None` -> 走**当前上下文**（`EQ_STATE` 代理），与修前逐字节一致；
#   `acc` 给定     -> 只读**那个账号的档**（`acc.data`，绝不 bind 到别人 —— 铁律 2）。
#   为什么必须能按账号算：8082 是"每个账号各自一份"的载荷，而"发红包/领红包/签到"这些动作
#   改变的是**全会每个成员各自**的红点。拿当前账号算完群发，就是把发起人的签到状态
#   写进所有人的缓存（会串档，且表现为"别人的红点被我擦掉了"）。
def _d(acc):
    """要评估的账号档：`None` -> 当前上下文；否则那个账号的 `data`。"""
    if acc is None:
        return None
    return getattr(acc, 'data', None) or {}


def _val(acc, key, default=None):
    d = _d(acc)
    return EQ_STATE.get(key, default) if d is None else d.get(key, default)


def _login7_pending(acc=None):
    """七日登录有没有"今天还没领"的档。**只读**(不调 _login7(), 那会改状态并存盘)。"""
    try:
        st = _val(acc, 'login7') or {}
        days = int(st.get('days', 0) or 0)
        claimed = int(st.get('claimed', 0) or 0)
        return 1 <= days <= 8 and claimed < days
    except Exception:
        return False


def _power_pending(acc=None):
    """体力免费领取档位(午间/晚间)有没有还没领的。判据与 handle_vitality_info 同式。"""
    try:
        v = _val(acc, 'vitality') or {}
        hour = int(time.strftime('%H'))
        t1 = 2 if int(v.get('t1', 0) or 0) >= 2 else (1 if hour >= 12 else 0)
        t2 = 2 if int(v.get('t2', 0) or 0) >= 2 else (1 if hour >= 18 else 0)
        return t1 == 1 or t2 == 1
    except Exception:
        return False


def _ach_pending(acc=None):
    # ⚠ 成就红点只能**按当前上下文**算：`handlers_achievement.any_claimable()` 读的是
    #   `EQ_STATE`（当前账号）。对"别人的档"返回 False ⇒ 该条目**缺席** ⇒
    #   客户端保持原值（`updateState` 不清理缺席项），不会误清别人的红点。
    if acc is not None:
        return False
    try:
        from handlers_achievement import any_claimable
        return bool(any_claimable())
    except Exception:
        return False


def _mail_pending(acc=None):
    """有没有**带附件且未领**的邮件。取不到就不亮(不猜)。"""
    try:
        mails = _val(acc, 'mail') or []
        for m in mails:
            if not isinstance(m, dict):
                continue
            if m.get('got'):
                continue
            aff = m.get('affix') or m.get('affix1')
            if aff:
                return True
        return False
    except Exception:
        return False


def _sociaty_guild(acc=None):
    """我所在的会 `(gid, guild)`；单档/未入会 -> `(None, None)`。**只读**。"""
    try:
        import handlers_sociaty as _hs
        gw = _hs._gworld()
        if gw is None:
            return None, None
        gid, g = gw.my_guild(acc)
        return (gid, g) if g else (None, None)
    except Exception:
        return None, None


def _sign_pending(acc=None):
    """妖盟签到今天还能不能签。**只读**：没入会 -> False（不亮）。"""
    try:
        _gid, g = _sociaty_guild(acc)
        if g is None:
            return False
        sg = _val(acc, 'sociaty')
        sg = sg if isinstance(sg, dict) else {}
        return str((sg or {}).get('sign_date') or '') != time.strftime('%Y-%m-%d')
    except Exception:
        return False


def _redbag_pending(acc=None):
    """妖盟里有没有"我还没领且还有剩"的红包。**只读**：没入会 -> False。"""
    try:
        _gid, g = _sociaty_guild(acc)
        if g is None:
            return False
        import guild_world as _gw
        import handlers_sociaty as _hs
        who = acc if acc is not None else _hs._current_acc()
        aid = getattr(who, 'id', None) or _gw.my_aid()
        return _gw.redbag_unclaimed(g, aid) > 0
    except Exception:
        return False


def attention_list(acc=None):
    """8082 的 `[[type, subtype, subid, state], ...]`。**只含真实为真的项**。

    `acc is None` -> 当前上下文（进游戏时 `push_reddots` 走这条，与修前逐字节一致）；
    `acc` 给定     -> **那个账号自己的**一份（发/领红包后按成员逐个推时走这条）。

    ⚠ 例外是妖盟那两条（见上面的 `U_SUB_MESSAGE` 注释）：它们**每次都出现**，因为客户端
      对"新列表里缺席的条目"**不清理**（`updateState` 只原地刷新）—— 缺席 = 保持上一次的值。
      写成显式 bool 才能把推过的 True 收回来（否则就是"擦不掉的红点"）。
    """
    out = []
    if _login7_pending(acc):
        out.append([T_SEVENLOGIN, 0, 0, True])
    if _power_pending(acc):
        out.append([T_POWER, 0, 0, True])
    if _ach_pending(acc):
        out.append([T_ACHIVEMENT, 0, 0, True])
    if _mail_pending(acc):
        out.append([T_MAIL, 0, 0, True])
    if _login7_pending(acc):
        out.append([T_DAILTASK, 0, 0, True])
    in_guild = _sociaty_guild(acc)[1] is not None
    out.append([T_UNOIN, U_SUB_MESSAGE, U_MSG_SIGN, bool(in_guild and _sign_pending(acc))])
    out.append([T_UNOIN, U_SUB_MESSAGE, U_MSG_REDBAG, bool(in_guild and _redbag_pending(acc))])
    return out


def point_list():
    """1053 的 `[[groupID, hint1, hint2, hint3, state], ...]`。"""
    out = []
    if _power_pending():
        out.append([P_POWER, P_POWER, 0, 0, True])
    if _login7_pending():
        out.append([P_DALAYTASK, P_DALAYTASK, 0, 0, True])
    if _ach_pending():
        out.append([P_TEAM, P_TEAM, 0, 0, True])
    return out


# ---------------------------------------------------------------- 应答/推送
def _resp(data):
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')


def handle_point_request(body_json=None):
    """105 PUSH_POINT 的应答。

    ⚠ 客户端 `NetPushTrackPoint:recvMessage` **不读 body**, 所以这个应答只是把兜底换成显式形状
       —— **亮灯全靠 push_reddots()**。别再指望应答能点亮什么。
    ⚠ 必须是 `[1,[]]`(**数组**), 不能是 `[1,{}]`:
       全工程的 LAW-A 形状铁律(`_contract_ironlaws_test.py` / `_fix26k_selftest.py`)要求
       "成功响应里的 msg 必须能按 `msg[N]` 索引"。我第一版写 `[1,{}]` 被这两套**当场抓住**
       (报 `msgid=105 成功但 msg=object`) —— 这正是护栏该有的样子, 记在这里避免以后改回去。
    """
    return b'[1,[]]'


def handle_attention_push(body_json=None):
    """8082 的应答(客户端启动时也会拉一次全量)。"""
    return _resp([attention_list()])


def attention_payload(acc=None):
    """8082 的完整载荷 —— `acc=None` 取当前上下文，给定则取**那个账号自己的**一份。"""
    return _resp([attention_list(acc)])


def push_attention(account_id, acc=None):
    """把 8082 推给**一个**账号（`acc` 给定 = 用那个账号自己的判据算载荷）。返回连接数。

    ★ 只能一个账号一份：红点是"服务端算、客户端缓存"，而客户端**不清理缺席条目**
      （`NetAttentionPush:updateState` 只原地刷新）⇒ 任何改变红点真值的动作（签到、
      发红包、领红包）都必须在同一个请求里**主动推一次**。
    ★★ `acc` **不能**拿调用方自己的上下文去顶替别人 —— 那就是把"我的签到状态"写进
      所有人的缓存（表现为"别人的红点被我擦掉 / 我没签他却显示已签"）。
    """
    try:
        import game_server as _gs
        return _gs.push_account(account_id, 8082, attention_payload(acc))
    except Exception:
        return 0


def push_attention_members(member_aids, acc_of):
    """按成员逐个推：`member_aids` 是 aid 列表，`acc_of(aid) -> Account|None`。best-effort。

    返回成功连接数。取不到档的成员**跳过**（宁可这次不推，也不拿别人的状态顶替）。
    """
    n = 0
    for aid in list(member_aids or []):
        try:
            acc = acc_of(aid) if callable(acc_of) else None
            n += push_attention(aid, acc)
        except Exception:
            continue
    return n


def push_reddots(conn):
    """主动推 8082 + 1053 —— **这才是"真建推送链路"**。

    由 game_server 在客户端进游戏发完 105/101/102/104 之后调用。
    异常一律吞掉并留痕: 推送失败绝不能影响主应答。
    """
    ok = []
    try:
        al = attention_list()
        conn.sendall(_pack(8082, _resp([al])))
        ok.append('8082:%d' % len(al))
    except Exception as e:
        ok.append('8082 ERR %r' % e)
    try:
        pl = point_list()
        conn.sendall(_pack(1053, _resp([pl])))
        ok.append('1053:%d' % len(pl))
    except Exception as e:
        ok.append('1053 ERR %r' % e)
    return ok


def _pack(mid, payload):
    """用 game_server 的 framing-A 打包器 —— 登录连接上推送必须用同一帧格式。"""
    import game_server as _gs      # ★ import 已安全: game_server 有 __main__ 守卫(本轮 1b)
    return _gs.pack_login_frame(mid, 0, payload)
