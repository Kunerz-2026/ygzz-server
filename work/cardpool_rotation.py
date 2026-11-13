# -*- coding: utf-8 -*-
r"""cardpool_rotation.py —— 私服扩展：活动卡池**每周轮换**（纯服务端；不改表、不改客户端）。

## 为什么要有这个文件

`D04_CardPool` 只有 1001（每日池）/ 1002（基础池）两行，`OpeningTime/ClosingTime/BGResources`
全是 0；而 `D01-DrawCard` 里其实有 **3–51 共 49 个 UP 卡组**（每组一个 featured SSR，
全部存在于 `L91_Status`）。客户端 `CardHome`/`CardConfig` 把
`carID ∉ {cd.daypoolid=1001, cd.basepoolid=1002}` 当**活动卡池**（`CardConfig.lua:176`），
活动页的底图/名称/剩余天数全部由 8001 下发、列表来自 1101。
⇒ 服务端只要给出「一个合成池 id + 当期卡组 + 一张真实存在的底图 + 本期结束时间」，
客户端就能显示并抽取，**不需要改客户端、不需要往策划表里加行**。

## 口径（2026-11-07 用户批准的方向）

- 一期 **7 天**，边界与排位周赛季同一把尺子（周一 05:00，复用 `handlers_qualifying._season_key`）。
- 卡组按 D01 的 3..51 顺序轮换（49 期一循环）。
- 底图按 1.30 内置的 41 张 `res/03_ClientImage/Raffle/*_1.png` 轮换（`ResConfig.lua:233`
  已把该目录加进搜索路径，所以发**裸文件名**即可）。
- ⚠ banner 与 featured 怪**不保证同主题** —— 官方没有「卡组 → 底图」的映射表；底图只作装饰，
  要换只改 `BANNERS` 的顺序。
- 池 id 固定 **1003**：D04 与 target client里都不存在，客户端只把它当 carID 匹配。
  这是**私服合成 id**，绝不写进 `data/tables`（`_cardpool_rotation_test` 静态钉死）。
- 关轮换：环境变量 `YGZZ_CARDPOOL_ROTATION=0`；此时 1101 回 `[1,[]]`，8001 退回旧的两页行为。

## 出口

    current()          -> {'pool_id','group','banner','title','start','end','index'} | None
    activity_rows()    -> [[pool_id, end_ts, banner]]   (1101 的行；关闭时 [])
    pool_group_map()   -> {pool_id: group_id}           (合并进 handlers_draw._pool_group_map)
    is_enabled()       -> bool
"""
import datetime
import os
import time

# 合成活动池 id（客户端只当 key；D04 / 1.30 包里都没有这个号）
POOL_ID = 1003

# D01 的 UP 卡组：3..51（Type=2/3 都存在；feat 怪见 handlers_draw._POOLS）
GROUPS = list(range(3, 52))

# target client内置的 Raffle 底图（`assets/res/03_ClientImage/Raffle/*_1.png`）。
#   ★ 顺序即轮换顺序；这批名字同时出现在 `the client file list`（1.30 清单）
#     与 `ygzz\extracted\...\Raffle`（解包资源）里，`_cardpool_rotation_test` 会逐个核对。
#   ⚠ 只收两边都在的：解包目录里没有的 7 张（QiXvRuLinZhiShu / wushengpinizhitong /
#     YeDanXueYeZhiLing / zhushenghuihunzhiyan / Raffle_EU_KeXueGuaiRen /
#     Raffle_EU_MangShenDaLuoZhiSheng / Raffle_EU_maoshanqumozhiye）先不收，
#     免得某个包缺图时活动页空白。
BANNERS = [
    'baihualiaoluan_1.png', 'budongrushanzhidun_1.png', 'dilingjiangshi_1.png',
    'huanhungaimingzhiye_1.png', 'junshenjianglinzhiye_1.png', 'MangShenDaLuoZhiSheng_1.png',
    'maoshanqumozhiye_1.png', 'menghuangluanruzhiye_1.png', 'mengwuluanru_1.png',
    'moshenjianglin_1.png', 'QianNianLunHuiZhiShan_1.png', 'qijirufengzhici_1.png',
    'qinlveruhuozhifa_1.png', 'rihuotianganzhiye_1.png',
    'riyuexinghe_1.png', 'SengJiaLanMoZhiShou_1.png', 'ShengSiBuYuZhiHun_1.png',
    'shishentuluzhiye_1.png', 'sijizhuzaizhiye_1.png', 'tianshenjianglin_1.png',
    'tianshenxiafan_1.png', 'WanWuCangShengZhiHuang_1.png',
    'WuWeiChiZiZhiXin_1.png', 'xueselangmanzhiye_1.png',
    'yinshanpanmingzhiye_1.png', 'yinyangchufen_1.png', 'ZhengQiLinRanZhiShi_1.png',
    'Raffle_EU_ChengYu_1.png', 'Raffle_EU_ChiYou_1.png',
    'Raffle_EU_LiangFu_1.png', 'Raffle_EU_PengLai_1.png', 'Raffle_EU_SanSan_1.png',
    'Raffle_EU_SiGui_1.png', 'Raffle_EU_ZhiFu_1.png',
]

# 周期锚点：2026-01-05 是周一（period_index = (赛季起始日 - 锚点) // 7）
_ANCHOR = datetime.date(2026, 1, 5)

_L91_NAME = {}


def is_enabled():
    """轮换开关（默认开）。`YGZZ_CARDPOOL_ROTATION=0/false/off/no` 关。"""
    try:
        v = str(os.environ.get('YGZZ_CARDPOOL_ROTATION', '1')).strip().lower()
        return v not in ('0', 'false', 'off', 'no', '')
    except Exception:
        return True


def _season_key():
    """本周赛季 (key, 起始 epoch, 结束 epoch)；与排位同一把尺子（周一 05:00）。"""
    try:
        from handlers_qualifying import _season_key as _qsk
        return _qsk()
    except Exception:
        # 兜底：按自然周算，绝不让 1101 因为 imports 失败整个消失
        try:
            now = datetime.datetime.now()
            mon = (now - datetime.timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(hours=5)
            if now < mon:
                mon -= datetime.timedelta(days=7)
            return (mon.strftime('%Y-%m-%d'), int(mon.timestamp()),
                    int((mon + datetime.timedelta(days=7)).timestamp()))
        except Exception:
            t = int(time.time())
            return (time.strftime('%Y-%m-%d'), t, t + 7 * 86400)


def _period_index(start_epoch):
    """赛季起始日 -> 第几期（相对 2026-01-05 的整周数；负数按 0）。"""
    try:
        d = datetime.date.fromtimestamp(int(start_epoch))
        return max(0, (d - _ANCHOR).days // 7)
    except Exception:
        return 0


def _featured(group):
    """该卡组的 featured 静态怪 id：Type=2 里 Chance 最小的那一档的第一只。"""
    try:
        from handlers_draw import _POOLS
        tiers = _POOLS.get((2, int(group))) or []
        if tiers:
            return int(min(tiers, key=lambda t: int(t[0]))[1][0])
    except Exception:
        pass
    return 0


def _load_names():
    if _L91_NAME:
        return
    try:
        from state import load_table
        rows = load_table('L91_Status.json')
        h = {c: i for i, c in enumerate(rows[0])}
        ni = h.get('Name', 1)
        for r in rows[1:]:
            try:
                _L91_NAME[int(r[0])] = str(r[ni] or '')
            except Exception:
                continue
    except Exception:
        pass


def _hero_name(mid):
    _load_names()
    return _L91_NAME.get(int(mid or 0), '')


def current(now=None):
    """当期卡池；关闭时返回 None。"""
    if not is_enabled():
        return None
    _key, s0, s1 = _season_key()
    idx = _period_index(s0)
    group = GROUPS[idx % len(GROUPS)]
    banner = BANNERS[idx % len(BANNERS)]
    name = _hero_name(_featured(group)) or '妖怪'
    return {
        'pool_id': POOL_ID,
        'group': int(group),
        'banner': banner,
        'title': '限时召唤 · %s' % name,
        'start': int(s0),
        'end': int(s1),
        'index': int(idx),
    }


def activity_rows():
    """1101 的行：`[[pool_id, end_ts, banner]]`（客户端读 id/endtime/img）。"""
    r = current()
    if not r:
        return []
    return [[r['pool_id'], r['end'], r['banner']]]


def pool_group_map():
    """8002 用：`{pool_id: CardGroupID}`；关闭时 `{}`。"""
    r = current()
    if not r:
        return {}
    return {r['pool_id']: r['group']}
