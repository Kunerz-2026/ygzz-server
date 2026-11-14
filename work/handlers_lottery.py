# -*- coding: utf-8 -*-
# 妖怪正传 私服 —— 幸运抽奖 + 兑换商城 (LOTTERY 1401-1406; 2026-09-07 第三轮按 D101-D104 明文实装)
# 客户端明文 (sweepstake/* + network/lottery/* 全部以"注释为准"来源):
#   LotteryTagEnum: DEMON=1 妖魂 / GHOST=2 鬼道积分 (ControllerSweepstake)
#   D101_LuckDraw:   每抽棋盘 16 行/类型 (DT1 用 ID 1-16, DT2 用 ID 17-32; 客户端 SDLuckDraw 本地渲染,
#                    服务器只回"中奖行"引用+掉落); Rate1Th/Rate10Th=千分比权重; LuckK=幸运值加成行;
#                    MinRate/MaxRate=幸运加成区间; CleanLuck=中奖清幸运值
#   D102_LuckDrawSet: Cost1Th/Cost10Th=单抽/十抽费用(DrawCurrency 2=妖魂 5=鬼道积分); UPFull=幸运值上限;
#                     UPAdd=每抽+幸运; IsGive/ItemID=每抽赠品(1038 妖盟贡献币); FreeCD=-1/1=免费CD(秒)
#   D103_LuckDrawShop: Page1=妖魂兑换(PriceType8 天罡八卦=1039 八卦·乾), Page2=鬼道兑换(PriceType9
#                     地煞八卦=1040 八卦·坤); LimitType1=终身限购
#   D104_LuckDrawActivity: 阶段奖励 (type1: 1001-1006, type2: 2001-2006) RewardID -> T02 组
# 契约 (net/lottery/NetLottery*.lua 逐字段):
#   1401 -> [1,[lotteryNumber,lastLotteryTime,freeNum,luckyValue,currentIntegral(妖魂),currentGDIntegral(鬼道),
#            userItem(1038数),freeLuckyDraw, prizeList([{Type,itemId(D101行ID),Count}] 已拥有限定), rewardGearList]]
#   1402 req{num,type} -> [1,[roleInfo,dropInfo, lotteryNumber,lastLotteryTime,freeNum,luckyValue,
#            currentIntegral,currentGDIntegral,userItem,freeLuckyDraw,
#            prizeList(本次中奖行 {Type,itemId,Count}), rewardGearList]]
#   1403 req{type} -> [1,[curDemonPropCount(1039),curGhostPropCount(1040), [{Type,itemId(GoodsId),Count(已购)}..]]]
#   1404 req{type,id,num} -> [1,[roleInfo,dropInfo,consumeInfo([uc,id,count])]]
#   1405 req{taskid} -> [1,[roleInfo,dropInfo,rewardGearList]]
#   1406 req{type} -> [1,[winners]](客户端按钮隐藏, 空表)
import json, os, random, time
from state import EQ_STATE, save_state
from handlers_task import _grant_group
from handlers_game import _role_info, _drop, _grant_content_row

from state import table_dir as _table_dir   # ★2026-10-02 (审计 §1.6): 表根不再写死盘符
_DATA = _table_dir()

def _load_json(name):
    # ★v60 闭环 v58 §10 缺口 #3: 改走 state.load_table —— 按「尾字段 u32 长度」精确剥离,
    #   不再用 c[:c.rfind(']')+1]（长度字段含 0x5D 时会切错 -> 调用方 except -> 静默空表,
    #   实测 L98_AIPriority / T32_Achievement）。护栏: work/_v60_legacy_tail_guard_test.py
    #   ⚠ 必须**逐行复制**: load_table 的结果是**带缓存的同一个对象**, 而旧实现每次返回全新对象。
    #     调用方若原地改行(排序/填字段)会污染全局缓存 -> 这里保持"每次都是新对象"的原语义。
    from state import load_table as _lt
    return [list(_r) for _r in _lt(name, _DATA)]

# ---------- D101 棋盘: 类型(str) -> [ {id,itemType,itemId,itemNum,rate1,rate10,luckK,minR,maxR,clean} ] ----------
_D101 = {}
try:
    _t = _load_json('D101_LuckDraw.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            dt = str(_r[_h['DrawType']])
            _D101.setdefault(dt, []).append({
                'id': int(_r[_h['ID']]), 'type': int(_r[_h['ItemType']]),
                'item': str(_r[_h['ItemID']]), 'num': int(_r[_h['ItemNum']] or 1),
                'rate1': int(_r[_h['Rate1Th']] or 0), 'rate10': int(_r[_h['Rate10Th']] or 0),
                'luckK': int(_r[_h['LuckK']] or 0), 'minR': int(_r[_h['MinRate']] or 0),
                'maxR': int(_r[_h['MaxRate']] or 0), 'clean': int(_r[_h['CleanLuck']] or 0),
            })
        except Exception:
            continue
except Exception:
    pass
# ---------- D102 费用/幸运: 类型 -> dict ----------
_D102 = {}
try:
    _t = _load_json('D102_LuckDrawSet.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _D102[str(_r[_h['DrawType']])] = {
                'currency': int(_r[_h['DrawCurrency ']] if 'DrawCurrency ' in _h else _r[_h['DrawCurrency']] or 0),
                'cost1': int(_r[_h['Cost1Th']] or 0), 'cost10': int(_r[_h['Cost10Th']] or 0),
                'give_type': int(_r[_h['IsGive']] or 0), 'give_item': str(_r[_h['ItemID']] or '0'),
                'upfull': int(_r[_h['UPFull']] or 0), 'upadd': int(_r[_h['UPAdd']] or 1),
                'freecd': int(_r[_h['FreeCD']] or -1),
            }
        except Exception:
            continue
except Exception:
    pass
# ---------- D103 兑换商城: page(str) -> [row..] ----------
_D103 = {}
try:
    _t = _load_json('D103_LuckDrawShop.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            row = {'id': str(_r[_h['ID']]), 'page': str(_r[_h['Page']]),
                   'gt': int(_r[_h['GoodsType']] or 0), 'gid': str(_r[_h['GoodsId']]),
                   'count': int(_r[_h['GoodsCount']] or 1),
                   'limit': int(_r[_h['LimitType']] or 0), 'limitc': int(_r[_h['LimitCoun']] or 1),
                   'price': int(_r[_h['Price']] or 0)}
            row['title'] = str(_r[_h['Tittle']] or '')
            _D103.setdefault(row['page'], []).append(row)
        except Exception:
            continue
except Exception:
    pass
# ---------- D104 阶段奖励: 类型 -> [(id, req, reward)] ----------
_D104 = {}
try:
    _t = _load_json('D104_LuckDrawActivity.json'); _h = {c: k for k, c in enumerate(_t[0])}
    for _r in _t[1:]:
        try:
            _id = int(_r[_h['ID']])
            tp = '1' if _id < 2000 else '2'
            _D104.setdefault(tp, []).append((_id, int(_r[_h['ActivityRequirement']] or 0),
                                             int(_r[_h['RewardID']] or 0)))
        except Exception:
            continue
except Exception:
    pass

# 兑换币: 妖魂商城 = 1039 八卦·乾(天罡), 鬼道商城 = 1040 八卦·坤(地煞) (L02 Type39/40; PriceType 8/9)
SHOP_CURRENCY = {'1': '1039', '2': '1040'}

def _lot():
    # 累计周期 (2026-09-08): D104 阶段奖励按活动周期滚动; 私服采用自然周 (周一刷新, 与周活跃一致)
    lot = EQ_STATE.setdefault('lottery', {})
    try:
        import datetime as _dt
        _d = _dt.date.today()
        _pk = '%d-W%02d' % (_d.isocalendar()[0], _d.isocalendar()[1])
    except Exception:
        _pk = time.strftime('%Y-%m-%d')
    if lot.get('period') != _pk:
        lot['period'] = _pk
        for _k in ('1', '2'):
            if isinstance(lot.get(_k), dict):
                _s = lot[_k]
                _s.pop('num', None)
                _s.pop('luck', None)
                _s.pop('gear', None)
        save_state()
    return lot

def _st(tp):
    st = _lot().setdefault(str(tp), {})
    st.setdefault('num', 0)
    st.setdefault('luck', 0)
    st.setdefault('free_ts', 0)
    st.setdefault('gear', {})
    return st

def _stat(tp):
    # 面板字段 [(lotteryNumber,lastLotteryTime,freeNum,luckyValue,..)]; free: 每日 1 次免费/类型
    #   (2026-09-08 修复: 旧实现 FreeCD=1s -> 每秒可免费 -> 无限刷; 官方=每日首次免费, 客户端按钮以此驱动)
    st = _st(tp)
    today = time.strftime('%Y-%m-%d')
    free_ready = st.get('free_day') != today
    return {
        'num': int(st.get('num', 0)),
        'remain': 0,
        'freeNum': 1 if free_ready else 0,
        'luck': int(st.get('luck', 0)),
        'free_ok': 1 if free_ready else 0,
    }

def _gears(tp):
    # rewardGearList: [{gear=ActivityRequirement, type=0未完成/1可领/2已领}]
    st = _st(tp)
    out = []
    for (aid, req, rew) in _D104.get(str(tp), []):
        g = int(st['gear'].get(str(aid), 0))
        if g == 0:
            g = 1 if int(st.get('num', 0)) >= req else 0
        out.append({'gear': req, 'type': g})
    return out

def _owned_prizes(tp):
    # prizeList(1401): 客户端 setHaveIsVisible 用 (itemId=D101 行ID, Count>0 -> "已拥有"角标)
    out = []
    for r in _D101.get(str(tp), []):
        cnt = 0
        if int(r['type']) == 2:                       # 道具
            cnt = int(EQ_STATE.get('prop', {}).get(str(r['item']), 0))
        out.append({'Type': r['type'], 'itemId': r['id'], 'Count': cnt})
    return out

def _grant_row(gt, oid, num):
    # 棋盘/商城奖励发放 (REWARD_NEWTYPE 类型全覆盖) -> (dropgroups, ok)
    gt = int(gt); num = int(num or 1); oid = str(oid)
    groups = []
    if gt == 1:
        g = _grant_content_row(1, oid, num)
        if not g:
            return [], False
        groups.extend(g)
    elif gt == 2:
        g = _grant_content_row(2, oid, num)
        if not g:
            return [], False
        groups.extend(g)
    elif gt == 3:
        groups.extend(_grant_content_row(3, oid, num))
    elif gt == 6:                                     # 鬼道积分
        EQ_STATE['kidou'] = int(EQ_STATE.get('kidou', 0)) + num
        groups.append([6, [[num]]])
    elif gt == 4:                                     # 妖怪
        from handlers_bag import _add_monster
        made = []
        for _ in range(num):
            uc = _add_monster(int(oid))
            if uc:                                    # ★2026-11-14: unknown mid -> None（防砖档）
                made.append(uc)
        if made:
            from handlers_bag import _drop_monster
            groups.append(json.loads(_drop_monster(made))[1][0])
    elif gt == 5:                                     # 元灵
        # ★v39 修"发号互不推进": 旧实现自己算 ucode (`max(ghN)+1`) 再 append,
        #   那套发号与 add_ghost 的计数器互不推进, 计数器落后时会发出重号
        #   (ghost 是 list, 客户端 mBagList 以 ucode 为键 -> 后行覆盖前行 = 物品消失)。
        # ★v51: 完整元灵**不堆叠** (一格一个) -> 必须把**全部新建行**发给客户端,
        #   只回一行会让界面少格子 (要等下次整包 1031 才补齐)。
        from state import add_ghost_rows as _addgh
        groups.append([5, _addgh(int(oid), num, 0)])
    elif gt == 7:                                     # 妖魂
        EQ_STATE['soulcoin'] = int(EQ_STATE.get('soulcoin', 0)) + num
        groups.append([7, [[num]]])
    elif gt == 8:                                     # 唤妖符
        EQ_STATE.setdefault('draw', {})['ticket'] = int(EQ_STATE.get('draw', {}).get('ticket', 0)) + num
        groups.append([8, [[num]]])
    elif gt == 14:                                    # 皮肤 (抽奖棋盘)
        # ★2026-09-11 (v21 §3.3): 旧实现直接 set skin_owned -> 重复皮肤无反馈;
        #   统一走 handlers_bag._grant_skin_with_dup (重复 -> 皮肤碎片 x10, 新皮肤 -> 记录+按族解锁)
        from handlers_bag import _grant_skin_with_dup
        _dup, _g = _grant_skin_with_dup(int(oid))
        groups.extend(_g)
    elif gt == 10:                                    # 周/月卡礼包组合(20011-20013): G03 组直发
        from handlers_bag import _apply_gift_rewards
        di = _apply_gift_rewards(str(oid))
        if di != '[]':
            groups.extend(json.loads(di)[1])
    elif gt == 9:                                     # 体力 (★2026-09-12 v26 P1: 真入账)
        # 旧实现只展示不写状态 (铁律 #28)。上限与 handlers_game.py:252 / handlers_bag 礼包路径一致 = 20。
        try:
            _vn = max(1, int(num))
        except Exception:
            _vn = 1
        EQ_STATE.setdefault('prop', {})['1006'] = min(20, int(EQ_STATE['prop'].get('1006', 0)) + _vn)
        EQ_STATE.setdefault('prop_id', {})['1006'] = 1006
        groups.append([9, [[num]]])
    else:
        return [], False
    return groups, True

# ============ 1401 面板信息 ============
def handle_lottery_panel_info(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    tp = str((req or {}).get('type') or '1')
    if tp not in _D102:
        return b'[0,{}]'
    # 客户端事实 (VSweepstakeHome:setPageUI 每次页签切换/进入都 requestMsg(_type) -> 1401):
    #   1405 领取请求只带 taskid=档位值(5/10/30/60/150/200), 不带类型; 记录"当前面板类型"作为
    #   领取归属判断依据 (NetAchiveLotteryLivenessReward:sendMessage 仅 taskid).
    _lot()['last_panel_type'] = tp
    st = _stat(tp)
    data = [int(st['num']), int(st['remain']), int(st['freeNum']), int(st['luck']),
            int(EQ_STATE.get('soulcoin', 0)), int(EQ_STATE.get('kidou', 0)),
            int(EQ_STATE.get('prop', {}).get('1038', 0)), int(st['free_ok']),
            _owned_prizes(tp), _gears(tp)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============ 1402 开始抽奖 ============
def handle_lottery_start(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        return b'[0,{}]'
    try:
        num = int(req.get('num') or 1)
        tp = str(req.get('type') or '1')
    except Exception:
        return b'[0,{}]'
    if num not in (1, 10) or tp not in _D102:
        return b'[0,{}]'
    _lot()['last_panel_type'] = tp          # 抽奖面板当前类型 (1405 领取归属参考)
    cfg = _D102[tp]
    st = _st(tp)
    sat = _stat(tp)
    free_draw = (num == 1 and sat['free_ok'] == 1)
    cost = 0 if free_draw else (int(cfg['cost1']) if num == 1 else int(cfg['cost10']))
    # 付费货币
    if not free_draw:
        if int(cfg['currency']) == 2:                 # 妖魂
            if int(EQ_STATE.get('soulcoin', 0)) < cost:
                return b'[0,{}]'
            EQ_STATE['soulcoin'] = int(EQ_STATE.get('soulcoin', 0)) - cost
        elif int(cfg['currency']) == 5:               # 鬼道积分
            if int(EQ_STATE.get('kidou', 0)) < cost:
                return b'[0,{}]'
            EQ_STATE['kidou'] = int(EQ_STATE.get('kidou', 0)) - cost
        else:
            return b'[0,{}]'
    else:
        st['free_day'] = time.strftime('%Y-%m-%d')    # 每日首次免费已用 (次日自动恢复)
    # ---- 抽奖 (每抽独立权重; LuckK 行按幸运值加成; CleanLuck 中奖清零) ----
    rows = _D101.get(tp)
    if not rows:
        return b'[0,{}]'
    windows = []
    for _ in range(num):
        rates = []
        for r in rows:
            base = int(r['rate10'] if num == 10 else r['rate1'])
            eff = base
            if int(r['luckK']):
                eff = int(base) + int(st.get('luck', 0)) * int(cfg.get('upadd', 1))
                eff = min(eff, int(r['maxR']) or 99999)
                eff = max(eff, int(r['minR']) or 0)
            rates.append(max(1, eff))
        total = sum(rates)
        roll = random.randrange(total)
        pick = rows[-1]
        for r, w in zip(rows, rates):
            if roll < w:
                pick = r
                break
            roll -= w
        windows.append(pick)
        st['luck'] = min(int(cfg.get('upfull', 360)), int(st.get('luck', 0)) + int(cfg.get('upadd', 1)))
        if int(pick.get('clean', 0)):
            st['luck'] = 0
    # ---- 发放 ----
    st['num'] = int(st.get('num', 0)) + num
    if int(cfg.get('give_type', 0)) and str(cfg.get('give_item', '0')) != '0':
        g = _grant_row(2, cfg['give_item'], num)      # 每抽赠品 1038 (妖盟贡献币)
    groups = []
    prize_list = []
    for pick in windows:
        gr, ok = _grant_row(pick['type'], pick['item'], pick['num'])
        if ok:
            groups.extend(gr)
        prize_list.append({'Type': pick['type'], 'itemId': pick['id'], 'Count': pick['num']})
    save_state()
    sat2 = _stat(tp)
    data = [_role_info(), _drop(groups), int(sat2['num']), int(sat2['remain']), int(sat2['freeNum']),
            int(sat2['luck']), int(EQ_STATE.get('soulcoin', 0)), int(EQ_STATE.get('kidou', 0)),
            int(EQ_STATE.get('prop', {}).get('1038', 0)), int(sat2['free_ok']),
            prize_list, _gears(tp)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============ 1403 兑换商城信息 ============
def handle_lottery_shop_info(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    tp = str((req or {}).get('type') or '1')
    shop = []
    for row in _D103.get(tp, []):
        # ⚠ 客户端 MSweepstakeShop:refreshData 用 info.itemId 索引【本地 SDLuckDrawShop 行ID】
        #   (m_shopListInfo[type][itemId] = SDLuckDrawShop(ID)), 不是 GoodsId!
        shop.append({'Type': row['gt'], 'itemId': str(row['id']),
                     'Count': int(EQ_STATE.get('lottery_shop', {}).get('%s:%s' % (tp, row['id']), 0))})
    data = [int(EQ_STATE.get('prop', {}).get(SHOP_CURRENCY.get(tp, '1039'), 0)),
            int(EQ_STATE.get('prop', {}).get(SHOP_CURRENCY.get('2' if tp == '1' else '1', '1040'), 0)),
            shop]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============ 1404 兑换商城购买 ============
def handle_lottery_shop_buy(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        return b'[0,{}]'
    tp = str(req.get('type') or '1')
    sid = str(req.get('id') or '')
    try:
        num = int(req.get('num') or 1)
    except Exception:
        num = 1
    # ⚠ 客户端 buyInfoCallback(panelTouch:setTag(id)) / buyItemCallFunc -> sendMessage(type, getStatic_ID(), n)
    #   : static ID = D103 行ID (真机 logcat: "VSweepstakeShop buy 10050" -> row 10050, GoodsId=1100021)
    row = None
    for r in _D103.get(tp, []):
        if str(r['id']) == sid:
            row = r
            break
    if row is None:
        return b'[0,{}]'
    if num <= 0:
        return b'[0,{}]'
    # 终身限购
    if int(row['limit']) == 1:
        bought = int(EQ_STATE.get('lottery_shop', {}).get('%s:%s' % (tp, sid), 0))
        if bought + num > int(row['limitc']):
            return b'[0,{}]'
    # 兑换币 (妖魂商城=1039 天罡 / 鬼道商城=1040 地煞; 客户端 SQManager:prop_getCount(1039/1040))
    cur_id = SHOP_CURRENCY.get(tp, '1039')
    need = int(row['price']) * num
    have = int(EQ_STATE.get('prop', {}).get(cur_id, 0))
    if have < need:
        return b'[0,{}]'
    EQ_STATE['prop'][cur_id] = have - need
    if EQ_STATE['prop'][cur_id] <= 0:
        EQ_STATE['prop'].pop(cur_id, None)
        EQ_STATE.get('prop_id', {}).pop(cur_id, None)
    # 发放商品
    groups, ok = _grant_row(row['gt'], row['gid'], int(row['count']) * num)
    if not ok:
        return b'[0,{}]'
    EQ_STATE.setdefault('lottery_shop', {})
    key = '%s:%s' % (tp, sid)
    EQ_STATE['lottery_shop'][key] = int(EQ_STATE['lottery_shop'].get(key, 0)) + num
    save_state()
    # ⚠ consumeInfo[3]=**扣除后余额** (NetLotteryShopBuy:recvMessage -> SQProp:updateItem 覆盖式 SET,
    #   v={ucode,id,count} 的 count 会直接写进 SQProp.mBagList[ucode].count; 若发"扣除额", 客户端
    #   显示余额=价格(错误), v10 §2.4 "八卦似乎没扣" 的真相); 配合 game_server 1404 后 push 1033 双保险
    new_count = have - need
    data = [_role_info(), _drop(groups), [cur_id, int(cur_id), new_count]]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============ 1405 阶段奖励领取 ============
# 客户端契约 (NetAchiveLotteryLivenessReward.lua L18-24 + VSweepstakeHome.lua L1062-1077):
#   客户端发 taskid = data.gear = D104.ActivityRequirement (5/10/30/60/150/200), **不是 D104.ID**!
#   (VSweepstakeHome:achiveVitalityReward: local taskid = data.gear -> sendMessage(LOTTERY_BOX_RECEIVE, taskid))
#   且请求不带类型 -> 服务器用 last_panel_type (1401/1402 记录的用户当前面板类型) 做归属判定:
#   两套 D104 (type1: 1001-1006 -> T02 5001-5006 / type2: 2001-2006 -> T02 5021-5026) req 序列相同,
#   先按 last_panel_type 匹配, 未决时回退到"另一类型中可领"的行; 全部已领 -> 539, 存在未领未达 -> 538.
#   错误码 (客户端 m_errorCode): 537 任务ID错误 / 538 该任务未完成 / 539 该任务奖励已经领取
def handle_lottery_box_receive(body_json):
    try:
        req = json.loads(body_json) if body_json else {}
    except Exception:
        req = {}
    if not isinstance(req, dict):
        req = {}
    try:
        aid = int((req or {}).get('taskid') or 0)
    except Exception:
        return b'[537]'
    # 候选: 两类型中 ActivityRequirement == aid 的行
    cands = []
    for tp in ('1', '2'):
        for (xid, xreq, xrew) in _D104.get(tp, []):
            if xreq == aid:
                cands.append((tp, xid, xreq, xrew))
    if not cands:
        return b'[537]'

    def _row_state(tp, xid, reqv):
        st = _st(tp)
        g = int(st['gear'].get(str(xid), 0))
        if g == 2:
            return 'claimed'
        if int(st.get('num', 0)) >= reqv:
            return 'claimable'
        return 'unmet'

    last = str(_lot().get('last_panel_type') or '')
    pick = None
    # 1) 当前面板类型 (1401/1402 记录) 直接命中可领 -> 必是用户点击的档位
    for (tp, xid, xreq, xrew) in cands:
        if tp == last and _row_state(tp, xid, xreq) == 'claimable':
            pick = (tp, xid, xrew)
            break
    # 2) 回退: 任意类型可领 (用户未请求 1401 直接 1405 的兜底)
    if pick is None:
        for (tp, xid, xreq, xrew) in cands:
            if _row_state(tp, xid, xreq) == 'claimable':
                pick = (tp, xid, xrew)
                break
    if pick is None:
        # 无任何可领: 全部已领 -> 539; 否则(存在未领未达) -> 538
        if all(_row_state(tp, xid, xreq) == 'claimed' for (tp, xid, xreq, _x) in cands):
            return b'[539]'
        return b'[538]'
    tp, xid, xrew = pick
    if int(_st(tp)['gear'].get(str(xid), 0)) == 2:
        return b'[539]'
    groups, ok = _grant_group(int(xrew))
    if not ok:
        return b'[538]'
    _st(tp)['gear'][str(xid)] = 2
    save_state()
    data = [_role_info(), _drop(groups), _gears(tp)]
    return ('[1,' + json.dumps(data, separators=(',', ':')) + ']').encode('utf-8')

# ============ 1406 中奖名单 (客户端按钮隐藏 -> 空) ============
def handle_lottery_winners_list(body_json=None):
    return b'[1,[[]]]'
