# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 周卡/月卡 (1098 GIFT_BAG_USE, baseType.card)
# 权威语义 = L06_CardGift 官方文案:
#   601 妖师周卡: "使用后立即获得120妖魂，每日可领取小仙桃x2、体力+5、经验120%，持续7日"
#   602 妖师月卡: "使用后立即获得500妖魂，每日可领取妖魂x80、体力+5、铜钱x1000，持续30日"
# 客户端: NetGiftBagUse.sendMessage({ucode}); recvMessage msg=[roleInfo, dropInfo]
#   m_errorCode: 0 消息体为空 / 53 包周礼包还未过期 / 56 包月礼包还未过期 / 583 礼包数据错误
#   -> 未过期 = 同一类卡已激活且未到期 => 拒绝; 到期后可再买/再用
# 每日发放: 服务器按 6:00 边界补发邮件 (1055 拉邮件时 top-up, 幂等; 邮件字段 [id,title,time,emailtype,state,rewards])
import json, time
from state import EQ_STATE, save_state
from handlers_game import _role_info

# 卡静态判定: 背包卡 item id (97) -> ('week'|'month', 持续天数, 立即妖魂)
#   兼容两套 id: L06_CardGift 601/602 (默认背包) 与 G02 Shop GoodsId 6011/6021 (商店购买的包装)
#   ★ 立即妖魂以 G03_SpellGroup 6011/6021 行为准 (客户端 SWeekMonthCard 界面"立得"渲染):
#     6011 -> 妖魂120 (G03#206); 6021 -> 妖魂800 (G03#209; 旧实现按 L06 文案 500 错误, 已纠正)
CARDS = {
    '601': ('week', 7, 120), '6011': ('week', 7, 120),
    '602': ('month', 30, 800), '6021': ('month', 30, 800),
}
# 每日奖励 (按文案): 周卡 小仙桃1013x2 + 体力1006x5 ; 月卡 妖魂x80 + 体力1006x5 + 铜钱1000
DAILY = {
    'week':  [['2', 1013, 2], ['2', 1006, 5]],
    'month': [['7', 0, 80], ['2', 1006, 5], ['3', 0, 1000]],
}

def _card_cls(gid):
    return CARDS.get(str(gid))

def handle_gift_bag_use(body_json):
    try:
        req = json.loads(body_json)
    except Exception:
        return b'[0,{}]'
    if not isinstance(req, dict):
        return b'[0,{}]'
    ucode = str(req.get('ucode') or '')
    if not ucode:
        return b'[0,{}]'
    # 定位背包卡条目 [ucode, type(97), id, count]
    gid = 0
    for g in EQ_STATE.get('giftbag', []):
        if len(g) >= 4 and str(g[0]) == ucode and int(g[1]) == 97:
            gid = int(g[2])
            break
    if not gid:
        return b'[583]'
    info = _card_cls(gid)
    if info is None:
        return b'[583]'
    cls, days, inst_soul = info
    # 每日任务联动: "今日登入领取周卡/月卡" (T37 1016/1017, ct=15/14) —— 放在过期检查前保证幂等补记;
    #   已激活的重复尝试([53]/[56])也视为"今日任务已完成" (2026-09-07 实装)
    from handlers_task import bump_task, CT_WEEKCARD, CT_MONTHCARD
    bump_task(CT_WEEKCARD if cls == 'week' else CT_MONTHCARD, 1)
    cd = EQ_STATE.setdefault('card_daily', {})
    st = cd.get(cls)
    if st and float(st.get('start', 0)) > 0 and time.time() < float(st['start']) + days * 86400:
        save_state()
        return b'[53]' if cls == 'week' else b'[56]'      # 还未过期
    # ----- 激活/续费 -----
    # 卡道具不消耗 (属性="通行证": 激活状态存 card_daily; 重复使用/未过期 -> [53]/[56]).
    #   实测语义推断: 客户端错误码 53/56 的存在说明"卡仍在有效期"是服务器要检查的常态
    if st is None:
        st = {'start': time.time(), 'granted': 0}
    else:
        st['start'] = time.time()                          # 重激活 (过期后重买)
        st['granted'] = 0
    cd[cls] = st
    EQ_STATE['soulcoin'] = int(EQ_STATE.get('soulcoin', 0)) + inst_soul
    st['granted'] = 1                                      # 立即发放 = 第1天 (随卡激活即刻到账)
    save_state()
    # 每日任务联动: "今日登入领取周卡/月卡" (T37 1016/1017, ct=15/14) 完成 1/1 (2026-09-07 实装)
    from handlers_task import bump_task, CT_WEEKCARD, CT_MONTHCARD
    bump_task(CT_WEEKCARD if cls == 'week' else CT_MONTHCARD, 1)
    save_state()
    drop_info = '[[],[[7,[[%d]]]]]' % inst_soul
    data = [_role_info(), json.loads(drop_info)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============================================================================
# 每日 6:00 补发 (1055 MAIL_GET 时幂等调用; 由 handlers_sys.handle_mail_get 钩入)
# ============================================================================
_MAIL_ID = 100          # 周/月卡每日邮件 id 起点 (与 _MAIL 静态邮件 1/2 不冲突)

def _days_since_6am(start_ts, now_ts):
    # "每日6点发放": 从激活时刻起, 经过的 6:00 边界数 = 应发放的"第 N 天"
    import datetime
    d0 = datetime.datetime.fromtimestamp(start_ts)
    dn = datetime.datetime.fromtimestamp(now_ts)
    days = (dn.date() - d0.date()).days
    today6 = dn.replace(hour=6, minute=0, second=0, microsecond=0)
    if dn < today6:
        days -= 1
    return max(0, days)

def topup_card_daily_mails(append_mail_fn):
    # append_mail_fn(id, title, time, emailtype, state, rewards) -> None (幂等)
    if not EQ_STATE.get('card_daily'):
        return
    now = time.time()
    for cls, days, title in (('week', 7, '周卡每日福利'), ('month', 30, '月卡每日福利')):
        st = EQ_STATE['card_daily'].get(cls)
        if not st or float(st.get('start', 0)) <= 0:
            continue
        target = min(days, _days_since_6am(float(st['start']), now))
        granted = int(st.get('granted', 1))
        if target <= granted:
            continue
        for d in range(granted, target):
            append_mail_fn(_MAIL_ID + (0 if cls == 'week' else 100) + d,
                           title, time.strftime('%Y%m%d%H%M%S'), 1, 0,
                           [list(r) for r in DAILY[cls]])
        st['granted'] = target
    save_state()
