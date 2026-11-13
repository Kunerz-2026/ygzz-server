# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 体力系统 (2026-09-06 按 NetVitalityInfo/Buy/Time/PushBegin 契约; 2026-09-08 恢复计时实装)
#   体力 = 道具 1006, 上限 20; 官方恢复 1800s/点 (客户端 NetGetVitality.lua VITALITY_VAL = 1800 --多久增加一点体力)
#   计时器流 (客户端源码事实):
#     角色 roleInfo[13] = timerid (SQGamer TimerId=0 注释"恢复体力的计时器唯一标识"; gamer_setTimerId)
#     1076 PUSH_BEGIN 登录后发 -> 服务器回 ack + 【推 1077 push 倒计时】(NetVitalityPush:recvMessage -> setRemainSecond)
#     1077 推送体 msg[1] = 剩余秒 (满体 = -1; NetGetVitality.getTimeAllFecover 对 -1 返回 0)
#     3002 GET_VITALITY req {timerid} -> 服务器校验计时器 + 到点授权恢复 + 回 roleInfo + 【再推 1077】
#        (客户端 onTick: 倒计时到 0 -> 每300s重发 3002; 错误码 3001计时器为空 / 3002体力恢复时间未到)
#   1074 INFO  -> [1,[nextTime, fullTime, consume(本次购买妖魂), type1state, type2state]] 秒级
#   1075 BUY   -> [1,[roleInfo]] (购买1点体力; 阶梯价 12/30/60/120 妖魂/日)
#   1100 TIME  req {type 1午/2晚} -> [1,[roleInfo, dropInfo]] (领取体力+5; type1/2 每日一次)
#   1076 PUSH_BEGIN -> [1,[]] (ack; 1077 由调用方推送)
import json, time, os
from state import EQ_STATE, save_state, explore_role_id as _explore_role_id

# A/B 复现开关: YGZZ_VIT_LEGACY_PUSH=1 -> 恢复 v20 旧行为(满体 1077 推 -1), 仅用于复现 §3.4 心跳风暴
LEGACY_PUSH = os.environ.get('YGZZ_VIT_LEGACY_PUSH') == '1'

VIT_MAX = 20
RECOVER_SEC = 1800
BUY_PRICES = [12, 30, 60, 120]
TIME_VALUE = 5

def _vt():
    return EQ_STATE.setdefault('vitality', {'buy': 0, 'buy_date': '', 't1': 0, 't2': 0, 't_date': '',
                                            'last_ts': 0.0, 'timerid': 1})

def _reset():
    v = _vt()
    today = time.strftime('%Y-%m-%d')
    if v.get('buy_date') != today:
        v['buy_date'] = today
        v['buy'] = 0
    if v.get('t_date') != today:
        v['t_date'] = today
        v['t1'] = 0
        v['t2'] = 0

def _count():
    return min(VIT_MAX, int(EQ_STATE['prop'].get('1006', 0)))

def _add(n):
    c = _count()
    n = min(n, VIT_MAX - c)
    if n > 0:
        EQ_STATE['prop']['1006'] = c + n
    return n

def _recover():
    # 惰性结算自然恢复: 返回本次结算已恢复的点数 (并推进 last_ts/timerid; 满体时收敛锚点)
    v = _vt()
    now = time.time()
    cur = _count()
    if cur >= VIT_MAX:
        v['last_ts'] = float(v.get('last_ts') or 0) or now
        return 0
    anchor = float(v.get('last_ts') or 0)
    if anchor <= 0:
        anchor = now
        v['last_ts'] = anchor
    pts = int((now - anchor) // RECOVER_SEC)
    if pts <= 0:
        return 0
    pts = min(pts, VIT_MAX - cur)
    if pts > 0:
        EQ_STATE['prop']['1006'] = cur + pts
        anchor += pts * RECOVER_SEC
        v['timerid'] = int(v.get('timerid', 1)) + pts
    v['last_ts'] = anchor
    return pts

def _remain():
    # 距下一点恢复的剩余秒数; 满体 = -1 (客户端 1077 -1 语义)
    v = _vt()
    cur = _count()
    if cur >= VIT_MAX:
        return -1
    anchor = float(v.get('last_ts') or 0)
    if anchor <= 0:
        anchor = time.time()
        v['last_ts'] = anchor
    r = int(RECOVER_SEC - (time.time() - anchor))
    return max(0, r) if r > 0 else 1

def _full_time(cur, remain):
    # 距离回满体力 (客户端 NetGetVitality: (maxPower - curPower - 1)*VITALITY_VAL + mRemainSecond)
    if cur >= VIT_MAX or remain < 0:
        return 0
    return (VIT_MAX - cur - 1) * RECOVER_SEC + remain

def vitality_timerid():
    # roleInfo[13] 唯一计时器标识 (客户端 3002 回传校验)
    return int(_vt().get('timerid', 1))

def _role_info():
    # ★★2026-10-02 (审计 §8 / §13 排名 4, P0): 本函数原是 roleInfo 的第 N 份**独立副本**。
    #   副本的字段[11] 停在 v58 已修掉的坏值 `'1'` —— 客户端把该字段按 `id&状态` 拆
    #   (SQGamer.lua:200 guide = tostring(roleInfo[12]) -> :224-232 按 ':' 和 '&' 拆),
    #   发 '1' 得到 guideArray = {{ "1" }} -> info[2] 为 nil ->
    #   NetUpdateInfo:sendMessage 第 24 行 `info[1].."&"..info[2]` 抛错 ->
    #   抽卡回调中断 -> 用户现象「符咒一抽」(cardtype=3) 抽完不出动画、不弹结果。
    #   除 [11] 外, 各副本还与权威实现在 [4] 体力 / [6] 妖塔层数 / [13] 鬼道积分 /
    #   [17][18] 周月卡 buff / [25][26] 卡到期时间戳 等处**各自漂移**(详见 git 历史),
    #   每漂移一处就是一类"用某个功能后 roleInfo 被刷坏"的事故。
    #   ⇒ 统一走**唯一权威实现** state.role_info_array() (它的 docstring 记着 v32/v58 两次事故)。
    from state import role_info_array
    return json.loads(role_info_array())

def handle_vitality_info(body_json=None):
    _reset()
    c = _count()
    remain = _remain()
    next_time = remain if c < VIT_MAX else -1
    full_time = _full_time(c, remain)
    consume = BUY_PRICES[min(_vt().get('buy', 0), len(BUY_PRICES) - 1)] if c < VIT_MAX else 0
    hour = int(time.strftime('%H'))
    v = _vt()
    t1 = 2 if v.get('t1') >= 2 else (1 if hour >= 12 else 0)
    t2 = 2 if v.get('t2') >= 2 else (1 if hour >= 18 else 0)
    data = [next_time, full_time, consume, t1, t2]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

def handle_vitality_buy(body_json=None):
    _reset()
    _recover()
    v = _vt()
    c = _count()
    if c >= VIT_MAX:
        # ★2026-09-12: 客户端 NetVitalityBuy.m_errorCode 里 64 = "体力值达到上限"。
        #   旧实现给 [0,{}] -> 客户端弹的是"获取网络信息失败"(误导)。
        return b'[64]'
    price = BUY_PRICES[min(v.get('buy', 0), len(BUY_PRICES) - 1)]
    if EQ_STATE['soulcoin'] < price:
        return b'[60]'                          # 妖魂不足
    EQ_STATE['soulcoin'] -= price
    EQ_STATE['prop']['1006'] = _count() + 1
    v['buy'] = v.get('buy', 0) + 1
    save_state()
    return json.dumps([1, [_role_info()]], separators=(',', ':')).encode('utf-8')

def handle_vitality_time(body_json):
    # 1100 定点领取 (type 1=午间 2=晚间)
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):     # ★2026-10-11: `(req or {})` 挡不住 list/str（见 _body.py 注释）
        return b'[0,{}]'
    _reset()
    v = _vt()
    try:
        ttype = int(req.get('type') or 0)
    except Exception:
        return b'[0,{}]'
    if ttype not in (1, 2):
        return b'[0,{}]'
    key = 't%d' % ttype
    now_h = int(time.strftime('%H'))
    if v.get(key) >= 2:
        return b'[0,{}]'
    if v.get(key) < 1:
        if (ttype == 1 and now_h < 12) or (ttype == 2 and now_h < 18):
            return b'[0,{}]'
    _recover()
    got = _add(TIME_VALUE)
    v[key] = 2
    save_state()
    data = [_role_info(), [[], [[9, [[got]]]]]]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

def handle_vitality_push_begin(body_json=None):
    # 1076: 客户端只回调; 1077 推送由 game_server 调用 _remain 后发出
    _reset()
    _recover()
    return b'[1,[]]'

def vitality_remain_push():
    # 1077 推送体: [1,[remainSec]] (客户端 NetVitalityPush:recvMessage msg[1] -> setRemainSecond)
    #   ★2026-09-11 修复 (v21 §3.4 P0 "界面首开错位/延迟" 真因):
    #     客户端 NetGetVitality:setRemainSecond(_s) 直接把该值写进 **轮询间隔** self.mUpdateDelay,
    #     而 onTick 是 `mUpdateDelay -= dt; if <=0 then mUpdateDelay = 300; sendMessage(3002)`。
    #     旧实现在满体时推 -1 -> mUpdateDelay = -1 -> 下一 tick 立即再发 3002 -> 服务器又回
    #     roleInfo + 推 1077(-1) -> 形成 **死循环反馈**, 实测 client 以 ~5 次/秒 持续发 3002:
    #       game_server.log 全档 18729 条 recv 中 17298 条 = msgid 3002 (92.4%), 每次还回带一帧 1077 推送
    #       -> 客户端网络/UI 线程被自己的体力心跳淹没 -> "响应慢/切换延迟/首开数据显示上一条数据".
    #     官方语义: 1077 的值 = "下一次需要再问服务器的秒数", 必须是正数; 满体时没有恢复窗口,
    #     取 RECOVER_SEC(1800) 作为"下一轮复查间隔"(客户端 UI 并未使用 getTimeNextFecover/getTimeAllFecover,
    #     全库 grep 无调用点, 故该值只承担轮询节流作用, 不影响任何显示)。
    _reset()
    _recover()
    save_state()
    r = _remain()
    if r is None or int(r) <= 0:
        r = -1 if LEGACY_PUSH else RECOVER_SEC
    return json.dumps([1, [int(r)]], separators=(',', ':')).encode('utf-8')

def handle_get_vitality(body_json=None):
    # 3002 GET_VITALITY: 核准恢复 + 回 roleInfo(含新 timerid); 1077 推送由调用方补发
    #   客户端 req = {timerid}; 错误码 3001 计时器为空 / 3002 恢复时间未到
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        return b'[3001]'
    tid = req.get('timerid')
    if tid is None:
        return b'[3001]'
    try:
        etid = int(tid)
    except Exception:
        return b'[3001]'
    _reset()
    cur = _count()
    if cur >= VIT_MAX:
        # 满体: 计时器收敛, 直接回当前 roleInfo (timerid 保持, remain 由 1077 给 -1)
        return role_info_resp()
    granted = _recover()
    if granted <= 0 and etid != vitality_timerid():
        # 客户端计时器与服务器当前窗口不一致 (跨服/回档等) -> 允许以服务器为准重取 (防卡死)
        pass
    save_state()
    return role_info_resp()

def role_info_resp():
    return ('[1,' + json.dumps([_role_info()], separators=(',', ':')) + ']').encode('utf-8')
